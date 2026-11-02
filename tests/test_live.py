"""Live test against Docker's public docs (robots.txt: "User-agent: *", no Disallow rules,
"Content-Signal: ai-train=yes, search=yes, ai-input=yes"). Skipped unless LIVE=1.
It makes 4 requests, one second apart: robots.txt, sitemap.xml and two pages."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sitemap2md as s  # noqa: E402

SITE = "https://docs.docker.com/"


@unittest.skipUnless(os.environ.get("LIVE") == "1", "set LIVE=1 to run the live test")
class LiveDockerDocs(unittest.TestCase):
    def test_sitemap_to_markdown_and_chunks(self):
        f = s.Fetcher(delay=1.0)
        rules = f.robots(SITE)
        self.assertFalse(rules.unreachable, rules.note)
        self.assertTrue(rules.allowed(SITE + "get-started/", f.user_agent))
        self.assertNotEqual(rules.signals.get("ai-input"), "no")

        entries, stats = s.collect(f, [SITE])
        self.assertGreater(len(entries), 100)
        self.assertEqual(stats["sitemap_errors"], 0)

        picked = s.select(entries, include=[r"/get-started/docker-concepts/building-images/"
                                            r"(writing-a-dockerfile|multi-stage-builds)/$"])
        self.assertEqual(len(picked), 2, [e.loc for e in picked])

        with tempfile.TemporaryDirectory() as tmp:
            chunks = os.path.join(tmp, "chunks.jsonl")
            summary = s.run(f, picked, tmp, chunks_path=chunks)
            self.assertEqual(summary["saved"], 2, summary)
            path = os.path.join(tmp, s.url_to_path(picked[0].loc))
            with open(path, encoding="utf-8") as fh:
                fields, body = s.read_front_matter(fh.read())
            self.assertEqual(fields["url"], picked[0].loc)
            self.assertTrue(fields["title"])
            self.assertRegex(fields["fetched_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertIn("```", body)
            self.assertIn("FROM", body)
            self.assertGreater(summary["chunks"], 2)
        print(f"\nlive: {len(entries)} URLs in sitemap, {summary}", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
