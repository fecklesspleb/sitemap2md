"""Offline tests: every HTTP response comes from tests/fixtures through FakeHTTP. No network is used."""
import contextlib
import gzip
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sitemap2md as s  # noqa: E402
from tests.fake_http import HTML, TEXT, XML, FakeClock, FakeHTTP, fixture  # noqa: E402

BASE = "https://docs.example.com"
FIXED_NOW = "2026-11-01T12:00:00Z"


def site_routes(**overrides):
    """The whole fake docs.example.com site: robots.txt, a nested sitemap index, a gzipped sitemap, pages."""
    routes = {
        f"{BASE}/robots.txt": (200, TEXT, fixture("robots.txt")),
        f"{BASE}/sitemap_index.xml": (200, XML, fixture("sitemap_index.xml")),
        f"{BASE}/sitemap-guides.xml": (200, XML, fixture("sitemap-guides.xml")),
        f"{BASE}/sitemaps/sitemap-api.xml.gz": (200, {"content-type": "application/gzip"},
                                                gzip.compress(fixture("sitemap-api.xml"), mtime=0)),
        f"{BASE}/sitemaps/nested-index.xml": (200, XML, fixture("nested-index.xml")),
        f"{BASE}/sitemaps/sitemap-blog.xml": (200, XML, fixture("sitemap-blog.xml")),
        f"{BASE}/guide/install/": (200, HTML, fixture("install.html")),
        f"{BASE}/guide/quickstart.html": (200, HTML, fixture("quickstart.html")),
        f"{BASE}/files/manual.pdf": (200, {"content-type": "application/pdf"}, b"%PDF-1.7"),
        f"{BASE}/api/client": (200, HTML, fixture("empty.html")),
        f"{BASE}/blog/release-2-0?lang=en": (404, HTML, b"<html>gone</html>"),
    }
    routes.update(overrides)
    return routes


def make(routes=None, delay=1.0, **kw):
    clock = FakeClock()
    http = FakeHTTP(site_routes() if routes is None else routes, clock=clock)
    f = s.Fetcher(delay=delay, http=http, clock=clock, sleep=clock.sleep, **kw)
    return f, http, clock


class ParseSitemapTests(unittest.TestCase):
    def test_urlset_with_lastmod_and_extra_namespaces(self):
        kind, entries = s.parse_sitemap(fixture("sitemap-guides.xml"))
        self.assertEqual(kind, "urlset")
        self.assertEqual([e.loc for e in entries], [
            f"{BASE}/guide/install/", f"{BASE}/guide/quickstart.html",
            f"{BASE}/private/roadmap/", f"{BASE}/files/manual.pdf"])
        self.assertEqual(entries[0].lastmod, "2026-10-28T09:15:00+00:00")
        self.assertIsNone(entries[3].lastmod)

    def test_index(self):
        kind, entries = s.parse_sitemap(fixture("sitemap_index.xml"))
        self.assertEqual(kind, "index")
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[2].loc, "/sitemaps/nested-index.xml")

    def test_gzip(self):
        kind, entries = s.parse_sitemap(gzip.compress(fixture("sitemap-api.xml")))
        self.assertEqual((kind, entries[0].loc), ("urlset", f"{BASE}/api/client"))

    def test_text_sitemap(self):
        kind, entries = s.parse_sitemap(fixture("sitemap.txt"))
        self.assertEqual([e.loc for e in entries], [f"{BASE}/guide/install/", f"{BASE}/api/client"])

    def test_bom_and_whitespace(self):
        kind, entries = s.parse_sitemap(b"\xef\xbb\xbf\n  " + fixture("sitemap-blog.xml"))
        self.assertEqual(len(entries), 1)

    def test_rejects_html_and_garbage(self):
        with self.assertRaises(s.SitemapError):
            s.parse_sitemap(fixture("not-a-sitemap.html"))
        with self.assertRaises(s.SitemapError):
            s.parse_sitemap(b"hello, no urls here")
        with self.assertRaises(s.SitemapError):
            s.parse_sitemap(b"<urlset><url><loc>broken")


class CollectTests(unittest.TestCase):
    def test_discovers_sitemap_from_robots_and_walks_nested_indexes(self):
        f, http, _ = make()
        entries, stats = s.collect(f, [f"{BASE}/"])
        self.assertEqual([e.loc for e in entries], [
            f"{BASE}/guide/install/", f"{BASE}/guide/quickstart.html", f"{BASE}/private/roadmap/",
            f"{BASE}/files/manual.pdf", f"{BASE}/api/client", f"{BASE}/blog/release-2-0?lang=en"])
        # install/ is listed twice (guides and api sitemaps) but kept once; the index loop is not refollowed
        self.assertEqual(stats, {"sitemaps": 5, "sitemap_errors": 0})
        self.assertEqual(http.urls().count(f"{BASE}/sitemap_index.xml"), 1)
        self.assertEqual(http.urls().count(f"{BASE}/robots.txt"), 1)

    def test_falls_back_to_sitemap_xml_without_robots_sitemap_line(self):
        routes = {f"{BASE}/sitemap.xml": (200, XML, fixture("sitemap-blog.xml"))}
        f, http, _ = make(routes)
        entries, _ = s.collect(f, [BASE])
        self.assertEqual(len(entries), 1)
        self.assertEqual(http.urls(), [f"{BASE}/robots.txt", f"{BASE}/sitemap.xml"])

    def test_max_depth(self):
        f, http, _ = make()
        logs = []
        entries, stats = s.collect(f, [f"{BASE}/sitemap_index.xml"], max_depth=1, log=logs.append)
        self.assertNotIn(f"{BASE}/sitemaps/sitemap-blog.xml", http.urls())
        self.assertTrue(any("--max-depth" in m for m in logs))
        self.assertEqual(len(entries), 5)

    def test_broken_child_sitemap_is_reported_not_fatal(self):
        f, _, _ = make(site_routes(**{f"{BASE}/sitemap-guides.xml": (500, HTML, b"oops")}), retries=0)
        entries, stats = s.collect(f, [f"{BASE}/sitemap_index.xml"])
        self.assertEqual(stats["sitemap_errors"], 1)
        self.assertIn(f"{BASE}/api/client", [e.loc for e in entries])

    def test_local_sitemap_file(self):
        f, http, _ = make()
        entries, _ = s.collect(f, [str(Path(__file__).parent / "fixtures" / "sitemap.txt")])
        self.assertEqual(len(entries), 2)
        self.assertEqual(http.calls, [])


class SelectTests(unittest.TestCase):
    entries = [s.Entry("https://a.test/docs/one", "2026-10-30"), s.Entry("https://a.test/blog/two", "2026-01-01"),
               s.Entry("https://a.test/docs/three"), s.Entry("https://a.test/docs/old", "2024-05-05T10:00:00Z")]

    def test_include_exclude(self):
        got = s.select(self.entries, include=["/docs/"], exclude=["old$"])
        self.assertEqual([e.loc for e in got], ["https://a.test/docs/one", "https://a.test/docs/three"])

    def test_since_keeps_entries_without_lastmod(self):
        got = s.select(self.entries, since="2026-06-01")
        self.assertEqual([e.loc for e in got], ["https://a.test/docs/one", "https://a.test/docs/three"])

    def test_max_pages(self):
        self.assertEqual(len(s.select(self.entries, max_pages=2)), 2)
        self.assertEqual(len(s.select(self.entries, max_pages=0)), 4)


class PolitenessTests(unittest.TestCase):
    def test_user_agent_on_every_request(self):
        f, http, _ = make()
        s.collect(f, [f"{BASE}/"])
        f.get(f"{BASE}/guide/install/")
        self.assertTrue(http.calls)
        for _, headers, _ in http.calls:
            self.assertEqual(headers["User-Agent"], s.DEFAULT_UA)
        self.assertIn("+https://github.com/fecklesspleb/sitemap2md", s.DEFAULT_UA)

    def test_disallowed_url_is_never_requested(self):
        f, http, _ = make()
        with self.assertRaises(s.Blocked):
            f.get(f"{BASE}/private/roadmap/")
        self.assertEqual(http.urls(), [f"{BASE}/robots.txt"])

    def test_rules_for_other_agents_do_not_apply_to_us(self):
        f, _, _ = make()
        self.assertTrue(f.robots(BASE).allowed(f"{BASE}/guide/install/", s.DEFAULT_UA))
        self.assertFalse(f.robots(BASE).allowed(f"{BASE}/guide/install/", "GreedyBot/1.0"))

    def test_crawl_delay_from_robots_wins_over_smaller_delay(self):
        f, http, _ = make(delay=0.5)
        for path in ("/guide/install/", "/guide/quickstart.html", "/files/manual.pdf"):
            f.get(BASE + path)
        times = [t for u, _, t in http.calls]
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertEqual(gaps[1:], [2.0, 2.0])  # robots.txt says Crawl-delay: 2

    def test_request_rate_from_robots(self):
        robots = b"User-agent: *\nRequest-rate: 1/5\n"
        routes = {f"{BASE}/robots.txt": (200, TEXT, robots), f"{BASE}/a": (200, HTML, b"x"),
                  f"{BASE}/b": (200, HTML, b"x")}
        f, http, _ = make(routes)
        f.get(f"{BASE}/a")
        f.get(f"{BASE}/b")
        t = {u: when for u, _, when in http.calls}
        self.assertEqual(t[f"{BASE}/b"] - t[f"{BASE}/a"], 5.0)

    def test_default_delay_between_requests_to_one_host(self):
        routes = {f"{BASE}/a": (200, HTML, b"x"), f"{BASE}/b": (200, HTML, b"x"),
                  "https://other.example.org/c": (200, HTML, b"x")}
        f, http, clock = make(routes, delay=1.5)
        f.get(f"{BASE}/a")
        f.get(f"{BASE}/b")
        f.get("https://other.example.org/c")
        t = {u: when for u, _, when in http.calls}
        self.assertEqual(t[f"{BASE}/b"] - t[f"{BASE}/a"], 1.5)
        # a different host has its own clock, so no wait beyond its own robots.txt request
        self.assertEqual(t["https://other.example.org/c"] - t["https://other.example.org/robots.txt"], 1.5)

    def test_retry_after_on_503_then_success(self):
        routes = {f"{BASE}/a": [(503, {"retry-after": "7"}, b""), (200, HTML, b"ok")]}
        f, http, clock = make(routes)
        _, status, _, body = f.get(f"{BASE}/a")
        self.assertEqual((status, body), (200, b"ok"))
        self.assertIn(7.0, clock.sleeps)

    def test_429_retries_run_out(self):
        routes = {f"{BASE}/a": (429, {}, b"slow down")}
        f, http, clock = make(routes, retries=2)
        _, status, _, _ = f.get(f"{BASE}/a")
        self.assertEqual(status, 429)
        self.assertEqual(http.page_calls(), [f"{BASE}/a"] * 3)
        self.assertIn(2.0, clock.sleeps)
        self.assertIn(4.0, clock.sleeps)

    def test_does_not_wait_longer_than_max_retry_wait(self):
        routes = {f"{BASE}/a": (503, {"retry-after": "3600"}, b"")}
        f, http, clock = make(routes, retries=3)
        _, status, _, _ = f.get(f"{BASE}/a")
        self.assertEqual(status, 503)
        self.assertEqual(len(http.page_calls()), 1)
        self.assertNotIn(3600.0, clock.sleeps)

    def test_network_error_retried_then_raised(self):
        routes = {f"{BASE}/a": s.FetchError("URLError: timed out")}
        f, http, _ = make(routes, retries=1)
        with self.assertRaises(s.FetchError):
            f.get(f"{BASE}/a")
        self.assertEqual(len(http.page_calls()), 2)

    def test_robots_404_and_403_mean_no_rules(self):
        for code in (404, 403):
            f, _, _ = make({f"{BASE}/robots.txt": (code, HTML, b"no"), f"{BASE}/a": (200, HTML, b"ok")})
            self.assertEqual(f.get(f"{BASE}/a")[1], 200)

    def test_robots_5xx_or_429_or_network_error_means_stay_away(self):
        for answer in ((500, HTML, b"err"), (429, HTML, b"slow"), s.FetchError("refused")):
            f, http, _ = make({f"{BASE}/robots.txt": answer, f"{BASE}/a": (200, HTML, b"ok")}, retries=0)
            with self.assertRaises(s.Blocked):
                f.get(f"{BASE}/a")
            self.assertNotIn(f"{BASE}/a", http.urls())

    def test_robots_served_as_html_page_means_no_rules(self):
        f, _, _ = make({f"{BASE}/robots.txt": (200, HTML, fixture("not-a-sitemap.html")),
                        f"{BASE}/a": (200, HTML, b"ok")})
        self.assertEqual(f.get(f"{BASE}/a")[1], 200)

    def test_redirects_are_checked_against_robots(self):
        routes = site_routes(**{
            f"{BASE}/old-install": (301, {"location": "/guide/install/"}, b""),
            f"{BASE}/old-roadmap": (302, {"location": f"{BASE}/private/roadmap/"}, b""),
        })
        f, http, _ = make(routes)
        final, status, _, _ = f.get(f"{BASE}/old-install")
        self.assertEqual((final, status), (f"{BASE}/guide/install/", 200))
        with self.assertRaises(s.Blocked):
            f.get(f"{BASE}/old-roadmap")
        self.assertNotIn(f"{BASE}/private/roadmap/", http.urls())

    def test_content_signal_ai_input_no_is_respected_for_pages(self):
        robots = b"User-agent: *\nContent-Signal: search=yes, ai-input=no, ai-train=no\nAllow: /\n"
        routes = {f"{BASE}/robots.txt": (200, TEXT, robots), f"{BASE}/a": (200, HTML, b"ok"),
                  f"{BASE}/sitemap.xml": (200, XML, fixture("sitemap-blog.xml"))}
        f, http, _ = make(routes)
        with self.assertRaisesRegex(s.Blocked, "ai-input=no"):
            f.get(f"{BASE}/a")
        entries, _ = s.collect(f, [f"{BASE}/sitemap.xml"])  # reading the sitemap itself is fine
        self.assertEqual(len(entries), 1)

    def test_content_signal_parsing(self):
        txt = ("User-agent: *\nContent-Signal: ai-train=no, search=yes, ai-input=yes\n\n"
               "User-agent: sitemap2md\nUser-agent: OtherBot\nContent-Signal: ai-input=no\nDisallow:\n")
        self.assertEqual(s.parse_content_signals(txt, s.DEFAULT_UA), {"ai-input": "no"})
        self.assertEqual(s.parse_content_signals(txt, "SomeBot/2"),
                         {"ai-train": "no", "search": "yes", "ai-input": "yes"})
        self.assertEqual(s.parse_content_signals("User-agent: *\nDisallow: /x\n", s.DEFAULT_UA), {})

    def test_retry_after_http_date(self):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(s.retry_after_seconds("Sun, 01 Nov 2026 12:00:30 GMT", now), 30.0)
        self.assertEqual(s.retry_after_seconds("120"), 120.0)
        self.assertIsNone(s.retry_after_seconds("soon"))


class MarkdownTests(unittest.TestCase):
    def test_extracts_main_content_without_navigation_or_footer(self):
        md, title = s.html_to_markdown(fixture("install.html").decode(), f"{BASE}/guide/install/")
        self.assertEqual(title, "Installing Widget")
        self.assertTrue(md.startswith("# Installing Widget\n"))
        self.assertIn("## Requirements\n", md)          # self-anchor link unwrapped
        self.assertIn("## Install with pip\n", md)      # absolute self-anchor link unwrapped
        self.assertIn("### Upgrading", md)
        self.assertIn("```\npython -m venv .venv\nsource .venv/bin/activate\npip install widget\n```", md)
        self.assertIn("| Permission denied | Activate the virtual environment |", md)
        self.assertIn("A C compiler is **not** required", md)
        self.assertIn("You should see the version number printed on one line.", md)
        for boilerplate in ("Sign in to the dashboard", "Copyright 2026", "Configuration"):
            self.assertNotIn(boilerplate, md)

    def test_page_without_main_content(self):
        md, title = s.html_to_markdown(fixture("empty.html").decode(), f"{BASE}/api/client")
        self.assertIsNone(md)
        self.assertEqual(title, "Redirecting")

    def test_clean_markdown(self):
        raw = ("# T\n## [Setup](https://x.test#setup)\ntext\n```\n## not a heading\n    indented code\n```\n"
               "## [Elsewhere](https://other.test/page#a)\n\n\n\nend\n")
        self.assertEqual(s.clean_markdown(raw, "https://x.test/page/"),
                         "# T\n\n## Setup\ntext\n\n```\n## not a heading\n    indented code\n```\n\n"
                         "## [Elsewhere](https://other.test/page#a)\n\nend\n")

    def test_front_matter_round_trip_with_awkward_title(self):
        fields = {"url": f"{BASE}/a", "title": 'Say "hi": a #1 guide\n', "fetched_at": FIXED_NOW, "words": 12,
                  "lastmod": None}
        text = s.front_matter(fields) + "body\n"
        self.assertTrue(text.startswith('---\nurl: "https://docs.example.com/a"\ntitle: "Say \\"hi\\": a #1 guide\\n"'))
        back, body = s.read_front_matter(text)
        self.assertEqual(back, {k: v for k, v in fields.items() if v is not None})
        self.assertEqual(body, "body\n")

    def test_url_to_path(self):
        cases = {
            "https://docs.example.com/": "docs.example.com/index.md",
            "https://docs.example.com/guide/install/": "docs.example.com/guide/install/index.md",
            "https://docs.example.com/guide/quickstart.html": "docs.example.com/guide/quickstart.md",
            "https://docs.example.com:8443/a b/ü.php": "docs.example.com_8443/a-b/ü.md",
            "https://docs.example.com/../../etc/passwd": "docs.example.com/etc/passwd.md",
        }
        for url, want in cases.items():
            self.assertEqual(s.url_to_path(url), want.replace("/", os.sep), url)
        self.assertEqual(s.url_to_path("https://d.test/%E6%96%87%E6%A1%A3/"),
                         os.path.join("d.test", "文档", "index.md"))
        self.assertRegex(s.url_to_path("https://d.test/p?id=7"), r"^d\.test.p-[0-9a-f]{8}\.md$")


class ChunkTests(unittest.TestCase):
    def setUp(self):
        self.md, _ = s.html_to_markdown(fixture("install.html").decode(), f"{BASE}/guide/install/")

    def test_chunks_follow_headings(self):
        chunks = s.chunk_markdown(self.md, max_words=300)
        self.assertEqual([c["headings"] for c in chunks], [
            ["Installing Widget"],
            ["Installing Widget", "Requirements"],
            ["Installing Widget", "Install with pip"],
            ["Installing Widget", "Install with pip", "Upgrading"],
            ["Installing Widget", "Troubleshooting"]])
        self.assertTrue(chunks[3]["text"].startswith("### Upgrading"))

    def test_small_limit_splits_but_never_inside_code(self):
        chunks = s.chunk_markdown(self.md, max_words=12)
        for c in chunks:
            self.assertEqual(c["text"].count("```") % 2, 0, c["text"])
        self.assertTrue(any(c["text"].startswith("```\npython -m venv") for c in chunks))
        self.assertTrue(all(s._words(c["text"]) <= 12 or c["text"].startswith("```") for c in chunks))

    def test_heading_only_sections_are_dropped_and_long_paragraphs_split(self):
        md = "# A\n## B\n" + " ".join(["word"] * 25) + "\n"
        chunks = s.chunk_markdown(md, max_words=10)
        self.assertEqual([c["headings"] for c in chunks], [["A", "B"]] * 3)
        self.assertEqual([s._words(c["text"]) for c in chunks], [10, 10, 7])
        self.assertTrue(chunks[0]["text"].startswith("## B\n\nword word"))

    def test_records_have_stable_unique_ids(self):
        a = s.chunk_records(f"{BASE}/guide/install/", "Installing Widget", FIXED_NOW, self.md, 50)
        b = s.chunk_records(f"{BASE}/guide/install/", "Installing Widget", FIXED_NOW, self.md, 50)
        self.assertEqual(a, b)
        ids = [r["id"] for r in a]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertRegex(ids[0], r"^[0-9a-f]{12}-0000$")
        self.assertEqual(set(a[0]), {"id", "url", "title", "headings", "chunk_index", "text", "words",
                                     "content_sha256", "fetched_at"})


class EndToEndTests(unittest.TestCase):
    def run_cli(self, *args, routes=None):
        http = FakeHTTP(site_routes() if routes is None else routes)
        clock = FakeClock()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = s.main(list(args), http=http, clock=clock, sleep=clock.sleep, now=lambda: FIXED_NOW)
        return code, out.getvalue(), err.getvalue(), http

    def test_full_run_writes_markdown_manifest_and_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir, chunks = os.path.join(tmp, "md"), os.path.join(tmp, "chunks.jsonl")
            code, out, err, http = self.run_cli(f"{BASE}/", "-o", out_dir, "--chunks", chunks)
            self.assertEqual(code, 0, err)
            summary = json.loads(out)
            self.assertEqual({k: summary[k] for k in ("urls_in_sitemaps", "selected", "saved", "skipped", "errors")},
                             {"urls_in_sitemaps": 6, "selected": 6, "saved": 2, "skipped": 3, "errors": 1})

            with open(os.path.join(out_dir, "docs.example.com", "guide", "install", "index.md")) as f:
                text = f.read()
            fields, body = s.read_front_matter(text)
            self.assertEqual(fields, {"url": f"{BASE}/guide/install/", "title": "Installing Widget",
                                      "fetched_at": FIXED_NOW, "lastmod": "2026-10-28T09:15:00+00:00",
                                      "words": len(body.split())})
            self.assertTrue(body.startswith("# Installing Widget"))
            self.assertTrue(os.path.exists(os.path.join(out_dir, "docs.example.com", "guide", "quickstart.md")))

            with open(os.path.join(out_dir, "_manifest.jsonl")) as f:
                manifest = {r["url"]: r for r in map(json.loads, f)}
            self.assertEqual(manifest[f"{BASE}/private/roadmap/"]["reason"], "disallowed by robots.txt")
            self.assertEqual(manifest[f"{BASE}/files/manual.pdf"]["reason"], "not HTML (application/pdf)")
            self.assertEqual(manifest[f"{BASE}/api/client"]["reason"], "no main content found")
            self.assertEqual(manifest[f"{BASE}/blog/release-2-0?lang=en"]["reason"], "HTTP 404")
            self.assertNotIn(f"{BASE}/private/roadmap/", http.urls())

            with open(chunks) as f:
                recs = [json.loads(line) for line in f]
            self.assertEqual(len(recs), summary["chunks"])
            self.assertEqual({r["url"] for r in recs}, {f"{BASE}/guide/install/", f"{BASE}/guide/quickstart.html"})
            self.assertEqual(manifest[f"{BASE}/guide/install/"]["chunks"],
                             sum(r["url"] == f"{BASE}/guide/install/" for r in recs))

    def test_two_urls_with_the_same_path_get_separate_files(self):
        routes = site_routes(**{f"{BASE}/guide/install/index.html": (200, HTML, fixture("install.html"))})
        with tempfile.TemporaryDirectory() as tmp:
            src, out_dir = os.path.join(tmp, "urls.txt"), os.path.join(tmp, "md")
            with open(src, "w") as f:
                f.write(f"{BASE}/guide/install/\n{BASE}/guide/install/index.html\n")
            code, out, err, _ = self.run_cli(src, "-o", out_dir, routes=routes)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["saved"], 2)
            with open(os.path.join(out_dir, "_manifest.jsonl")) as f:
                files = [json.loads(line)["file"] for line in f]
            self.assertEqual(len(set(files)), 2)
            self.assertTrue(files[0].endswith(os.path.join("install", "index.md")))
            self.assertRegex(files[1], r"index-[0-9a-f]{8}\.md$")

    def test_skip_unchanged_rerun_fetches_no_pages_but_rewrites_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir, chunks = os.path.join(tmp, "md"), os.path.join(tmp, "chunks.jsonl")
            args = (f"{BASE}/sitemap-guides.xml", "-o", out_dir, "--chunks", chunks, "--skip-unchanged",
                    "--include", "/guide/")
            self.run_cli(*args)
            with open(chunks) as f:
                first = f.read()
            code, out, err, http = self.run_cli(*args)
            self.assertEqual(json.loads(out)["unchanged"], 2)
            self.assertEqual(http.page_calls(), [f"{BASE}/sitemap-guides.xml"])
            with open(chunks) as f:
                self.assertEqual(f.read(), first)

    def test_dry_run_lists_urls_and_fetches_no_pages(self):
        code, out, err, http = self.run_cli(f"{BASE}/sitemap_index.xml", "--dry-run", "--since", "2026-10-01",
                                            "--exclude", "private")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            f"{BASE}/guide/install/\t2026-10-28T09:15:00+00:00",
            f"{BASE}/files/manual.pdf\t",
            f"{BASE}/blog/release-2-0?lang=en\t2026-10-31"])
        self.assertFalse([u for u in http.page_calls() if "sitemap" not in u])

    def test_no_urls_exits_1_and_bad_args_exit_2(self):
        code, _, err, _ = self.run_cli(f"{BASE}/sitemap.xml", routes={f"{BASE}/sitemap.xml": (404, HTML, b"")})
        self.assertEqual(code, 1)
        self.assertIn("No URLs found", err)
        self.assertEqual(self.run_cli(f"{BASE}/", "--since", "yesterday")[0], 2)


if __name__ == "__main__":
    unittest.main()
