#!/usr/bin/env python3
"""sitemap2md: read a website's sitemap, fetch its pages politely, and save them as clean Markdown.

Optionally split the Markdown into heading-aware chunks (JSONL) ready for a RAG index.
Works with any site that publishes a sitemap.xml (or a sitemap index, gzipped or not).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urljoin, urlsplit

__version__ = "0.1.0"
REPO_URL = "https://github.com/fecklesspleb/sitemap2md"
DEFAULT_UA = f"sitemap2md/{__version__} (+{REPO_URL})"
HTML_TYPES = ("text/html", "application/xhtml+xml")
MAX_SITEMAP_BYTES = 50 * 1024 * 1024  # the sitemaps.org limit for one uncompressed sitemap
MAX_PAGE_BYTES = 10 * 1024 * 1024
MAX_ROBOTS_BYTES = 500 * 1024  # RFC 9309 asks crawlers to parse at least 500 KiB
MAX_REDIRECTS = 5
RETRY_STATUSES = {429, 500, 502, 503, 504}


class FetchError(Exception):
    """The request could not be completed (network error, or retries ran out)."""


class Blocked(Exception):
    """robots.txt (or a content signal) says this URL should not be fetched."""


class SitemapError(Exception):
    """The document is not a sitemap we can read."""


# --------------------------------------------------------------------------- HTTP


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are followed by Fetcher.get, so robots.txt is checked before every hop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_http(url, headers, timeout=30, max_bytes=MAX_PAGE_BYTES):
    """Default transport: one GET, no redirects. Returns (status, headers with lower-case keys, body)."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        body = e.read(65536) if e.fp else b""
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise FetchError(f"{type(e).__name__}: {getattr(e, 'reason', e)}") from e


def retry_after_seconds(value, now=None):
    """Parse a Retry-After header (seconds or an HTTP date). Returns None if absent or unreadable."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (when - now).total_seconds())


@dataclass
class Robots:
    """robots.txt rules for one origin."""

    parser: urllib.robotparser.RobotFileParser | None  # None means "no rules": everything allowed
    note: str
    unreachable: bool = False  # True: robots.txt failed with 429/5xx/network error, so fetch nothing
    signals: dict = field(default_factory=dict)  # Content-Signal values for our group, e.g. {"ai-input": "no"}
    sitemaps: list = field(default_factory=list)

    def allowed(self, url, user_agent):
        if self.unreachable:
            return False
        if self.parser is None:
            return True
        return self.parser.can_fetch(user_agent, url)

    def crawl_delay(self, user_agent):
        if self.parser is None:
            return None
        delay = self.parser.crawl_delay(user_agent)
        rate = self.parser.request_rate(user_agent)
        if rate and rate.requests:
            per_request = rate.seconds / rate.requests
            delay = max(float(delay or 0), per_request)
        return float(delay) if delay else None


def parse_content_signals(text, user_agent):
    """Content-Signal lines (contentsignals.org) for the group that matches us, else the '*' group."""
    token = user_agent.split("/")[0].strip().lower()
    groups, agents, in_agents = [], [], False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (s.strip() for s in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if not in_agents:
                agents = []
                groups.append((agents, {}))
                in_agents = True
            agents.append(value.lower())
            continue
        in_agents = False
        if key == "content-signal" and groups:
            for part in value.split(","):
                if "=" in part:
                    k, v = (s.strip().lower() for s in part.split("=", 1))
                    groups[-1][1][k] = v
    mine = [sig for ags, sig in groups if any(a != "*" and a in token for a in ags)]
    star = [sig for ags, sig in groups if "*" in ags]
    merged = {}
    for sig in (mine or star):
        merged.update(sig)
    return merged


class Fetcher:
    """Polite HTTP: identifies itself, obeys robots.txt and Crawl-delay, spaces requests per host, retries 429/5xx."""

    def __init__(self, user_agent=DEFAULT_UA, delay=1.0, timeout=30, retries=3, http=None,
                 clock=time.monotonic, sleep=time.sleep, max_retry_wait=120, log=None):
        self.user_agent = user_agent
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.http = http or urllib_http
        self.clock = clock
        self.sleep = sleep
        self.max_retry_wait = max_retry_wait
        self.log = log or (lambda msg: None)
        self.robots_cache = {}
        self._last = {}
        self.requests = 0

    @staticmethod
    def origin(url):
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}"

    def _interval(self, origin):
        robots = self.robots_cache.get(origin)
        crawl_delay = robots.crawl_delay(self.user_agent) if robots else None
        return max(self.delay, crawl_delay or 0)

    def _space(self, origin):
        last = self._last.get(origin)
        if last is not None:
            wait = last + self._interval(origin) - self.clock()
            if wait > 0:
                self.sleep(wait)
        self._last[origin] = self.clock()

    def _request(self, url, max_bytes):
        """One URL, with per-host spacing and retries on 429/5xx/network errors. No redirect handling."""
        origin = self.origin(url)
        headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        status = hdrs = body = None
        error = None
        for attempt in range(self.retries + 1):
            self._space(origin)
            self.requests += 1
            try:
                status, hdrs, body = self.http(url, headers, self.timeout, max_bytes)
                error = None
            except FetchError as e:
                error = e
                status = None
            if status is not None and status not in RETRY_STATUSES:
                break
            if attempt == self.retries:
                break
            wait = retry_after_seconds((hdrs or {}).get("retry-after")) if status else None
            if wait is None:
                wait = 2.0 ** (attempt + 1)
            if wait > self.max_retry_wait:
                self.log(f"  {url}: server asked to wait {wait:.0f}s, giving up on it for this run")
                break
            self.log(f"  {url}: {status or error}, retrying in {wait:.0f}s")
            self.sleep(wait)
        if status is None:
            raise FetchError(str(error))
        return status, {k.lower(): v for k, v in (hdrs or {}).items()}, body or b""

    def robots(self, url):
        origin = self.origin(url)
        if origin in self.robots_cache:
            return self.robots_cache[origin]
        robots_url = origin + "/robots.txt"
        rules = None
        try:
            target = robots_url
            for _ in range(MAX_REDIRECTS + 1):
                status, hdrs, body = self._request(target, MAX_ROBOTS_BYTES)
                if status in (301, 302, 303, 307, 308) and hdrs.get("location"):
                    target = urljoin(target, hdrs["location"])
                    continue
                break
            if status == 200:
                text = body[:MAX_ROBOTS_BYTES].decode("utf-8", errors="replace").lstrip("\ufeff")
                if text.lstrip()[:1] == "<":
                    rules = Robots(None, "robots.txt is an HTML page; treated as no rules")
                else:
                    parser = urllib.robotparser.RobotFileParser(robots_url)
                    parser.parse(text.splitlines())
                    rules = Robots(parser, "robots.txt read",
                                   signals=parse_content_signals(text, self.user_agent),
                                   sitemaps=list(parser.site_maps() or []))
            elif status in (301, 302, 303, 307, 308):
                rules = Robots(None, "robots.txt redirected too many times; treated as no rules")
            elif 400 <= status < 500 and status != 429:
                rules = Robots(None, f"robots.txt answered {status}; treated as no rules (RFC 9309)")
            else:
                rules = Robots(None, f"robots.txt answered {status}; not crawling this host", unreachable=True)
        except FetchError as e:
            rules = Robots(None, f"robots.txt unreachable ({e}); not crawling this host", unreachable=True)
        self.robots_cache[origin] = rules
        self.log(f"{origin}: {rules.note}")
        return rules

    def check(self, url, purpose="page"):
        rules = self.robots(url)
        if rules.unreachable:
            raise Blocked("robots.txt unavailable")
        if not rules.allowed(url, self.user_agent):
            raise Blocked("disallowed by robots.txt")
        if purpose == "page" and rules.signals.get("ai-input") == "no":
            raise Blocked("robots.txt Content-Signal ai-input=no")

    def get(self, url, max_bytes=MAX_PAGE_BYTES, purpose="page"):
        """GET with robots.txt checked for the URL and every redirect hop.
        Returns (final_url, status, headers, body)."""
        for _ in range(MAX_REDIRECTS + 1):
            self.check(url, purpose)
            status, hdrs, body = self._request(url, max_bytes)
            if status in (301, 302, 303, 307, 308) and hdrs.get("location"):
                url = urljoin(url, hdrs["location"])
                continue
            return url, status, hdrs, body
        raise FetchError("too many redirects")


# --------------------------------------------------------------------------- sitemaps


@dataclass
class Entry:
    loc: str
    lastmod: str | None = None


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(body):
    """Parse a sitemap, sitemap index, gzipped sitemap or plain-text sitemap.
    Returns (kind, entries) where kind is 'index' or 'urlset'."""
    if body[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz:
            body = gz.read(MAX_SITEMAP_BYTES + 1)
    if len(body) > MAX_SITEMAP_BYTES:
        raise SitemapError("sitemap is larger than 50 MB")
    data = body.lstrip(b"\xef\xbb\xbf \t\r\n")
    if data[:1] == b"<":
        try:
            root = ET.fromstring(data)
        except ET.ParseError as e:
            raise SitemapError(f"not valid XML ({e})") from e
        kind = _local(root.tag)
        if kind not in ("sitemapindex", "urlset"):
            raise SitemapError(f"root element is <{kind}>, not <urlset> or <sitemapindex>")
        child_tag = "sitemap" if kind == "sitemapindex" else "url"
        entries = []
        for el in root:
            if _local(el.tag) != child_tag:
                continue
            loc = lastmod = None
            for sub in el:
                name = _local(sub.tag)
                if name == "loc" and sub.text:
                    loc = sub.text.strip()
                elif name == "lastmod" and sub.text:
                    lastmod = sub.text.strip()
            if loc:
                entries.append(Entry(loc, lastmod))
        return ("index" if kind == "sitemapindex" else "urlset"), entries
    text = data.decode("utf-8", errors="replace")
    urls = [ln.strip() for ln in text.splitlines() if re.match(r"https?://\S+$", ln.strip())]
    if not urls:
        raise SitemapError("not XML and no URLs found")
    return "urlset", [Entry(u) for u in urls]


def is_site_root(source):
    p = urlsplit(source)
    return p.scheme in ("http", "https") and p.path in ("", "/") and not p.query


def discover_sitemaps(fetcher, site_url):
    """Sitemap: lines from robots.txt, else /sitemap.xml."""
    rules = fetcher.robots(site_url)
    if rules.sitemaps:
        return list(rules.sitemaps)
    return [fetcher.origin(site_url) + "/sitemap.xml"]


def collect(fetcher, sources, max_depth=5, log=None):
    """Walk sitemaps and nested sitemap indexes. Returns (entries in sitemap order, stats)."""
    log = log or (lambda msg: None)
    stats = {"sitemaps": 0, "sitemap_errors": 0}
    seen_maps, seen_urls, out = set(), set(), []

    def load(source):
        if not re.match(r"https?://", source):
            with open(source, "rb") as f:
                return f.read(MAX_SITEMAP_BYTES + 1)
        _, status, _, body = fetcher.get(source, MAX_SITEMAP_BYTES, purpose="sitemap")
        if status != 200:
            raise SitemapError(f"HTTP {status}")
        return body

    def walk(source, depth):
        if source in seen_maps:
            return
        seen_maps.add(source)
        try:
            kind, entries = parse_sitemap(load(source))
        except (SitemapError, FetchError, Blocked, OSError) as e:
            stats["sitemap_errors"] += 1
            log(f"sitemap {source}: {e}")
            return
        stats["sitemaps"] += 1
        log(f"sitemap {source}: {len(entries)} {'child sitemaps' if kind == 'index' else 'URLs'}")
        if kind == "index":
            if depth >= max_depth:
                log(f"sitemap {source}: nested deeper than --max-depth {max_depth}, not following")
                return
            for e in entries:
                walk(urljoin(source, e.loc) if re.match(r"https?://", source) else e.loc, depth + 1)
            return
        for e in entries:
            if e.loc not in seen_urls:
                seen_urls.add(e.loc)
                out.append(e)

    for source in sources:
        if is_site_root(source):
            for sm in discover_sitemaps(fetcher, source):
                walk(sm, 0)
        else:
            walk(source, 0)
    return out, stats


def select(entries, include=(), exclude=(), since=None, max_pages=0):
    """Filter sitemap entries. URLs without a lastmod are kept by --since (there is no way to tell)."""
    inc = [re.compile(p) for p in include]
    exc = [re.compile(p) for p in exclude]
    picked = []
    for e in entries:
        if inc and not any(r.search(e.loc) for r in inc):
            continue
        if any(r.search(e.loc) for r in exc):
            continue
        if since and e.lastmod and e.lastmod[:10] < since:
            continue
        picked.append(e)
        if max_pages and len(picked) >= max_pages:
            break
    return picked


# --------------------------------------------------------------------------- Markdown

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BLOCK_START = re.compile(r"^\s*([-*+>|]|\d+[.)])(\s|$)")
_HEADING_LINK = re.compile(r"^(#{1,6})\s+\[(.+?)\]\(([^)\s]*)#[^)]*\)\s*$")


def _unwrap_self_anchor(line, url):
    """'## [Install](https://docs.example.com/page/#install)' -> '## Install' when the link points back
    into the same page (or the bare site origin, which is how some sites' relative anchors resolve)."""
    m = _HEADING_LINK.match(line)
    if not m:
        return line
    target = m.group(3).rstrip("/")
    own = {""}
    if url:
        p = urlsplit(url)
        own |= {f"{p.scheme}://{p.netloc}", url.split("#")[0].rstrip("/")}
    return f"{m.group(1)} {m.group(2)}" if target in own else line


def clean_markdown(md, url=None):
    """Tidy trafilatura's Markdown: unwrap self-anchor links in headings, keep blank lines around
    code fences and headings, collapse runs of blank lines. Code blocks are left untouched."""
    out, in_code = [], False
    for line in md.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            if not in_code and out and out[-1].strip():
                out.append("")
            out.append(line.rstrip())
            in_code = not in_code
            if not in_code:
                out.append("")
            continue
        if in_code:
            out.append(line)
            continue
        line = _unwrap_self_anchor(line.rstrip(), url)
        prev = out[-1] if out else ""
        if (line[:1] == " " and prev.strip() and not _BLOCK_START.match(prev)
                and not _BLOCK_START.match(line)):
            out[-1] = prev + " " + line.lstrip()  # an indented, wrapped paragraph line (a trafilatura quirk)
            continue
        if _HEADING.match(line) and out and out[-1].strip():
            out.append("")
        out.append(line)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def html_to_markdown(html, url):
    """Main content as Markdown (trafilatura), plus the page title. Markdown is None if no main content."""
    import trafilatura

    md = trafilatura.extract(
        html, url=url, output_format="markdown", include_links=True, include_tables=True,
        include_formatting=True, include_images=False, include_comments=False,
        favor_recall=True, deduplicate=False,
    )
    title = None
    meta = trafilatura.extract_metadata(html, default_url=url)
    if meta is not None and meta.title:
        title = meta.title.strip()
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    return (clean_markdown(md, url) if md and md.strip() else None), title


def front_matter(fields):
    """YAML front matter. Strings are written as JSON strings, which are valid YAML double-quoted scalars."""
    lines = ["---"]
    for k, v in fields.items():
        if v is None:
            continue
        lines.append(f"{k}: {v}" if isinstance(v, int) else f"{k}: {json.dumps(str(v), ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def read_front_matter(text):
    """Returns (fields, body) for a file written by front_matter()."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    fields = {}
    for line in text[4:end].splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            try:
                fields[k] = json.loads(v)
            except ValueError:
                fields[k] = v
    return fields, text[end + 5:].lstrip("\n")


def _safe(segment):
    return re.sub(r"[^\w.-]+", "-", segment).strip("-.")[:100] or "_"


def url_to_path(url):
    """https://docs.example.com/guide/install/ -> docs.example.com/guide/install/index.md"""
    p = urlsplit(url)
    host = _safe((p.hostname or "unknown") + (f"_{p.port}" if p.port else ""))
    segs = [_safe(s) for s in unquote(p.path).split("/") if s not in ("", ".", "..")]
    if p.path.endswith("/") or not segs:
        segs.append("index")
    last = re.sub(r"\.(html?|php|aspx?|jsp|md)$", "", segs[-1], flags=re.I) or "index"
    if p.query:
        last += "-" + hashlib.sha1(p.query.encode()).hexdigest()[:8]
    segs[-1] = last + ".md"
    return os.path.join(host, *segs)


# --------------------------------------------------------------------------- chunks


def _words(text):
    return len(text.split())


def _blocks(lines):
    """Split section lines into paragraphs; a fenced code block is always one block."""
    blocks, cur, in_code = [], [], False
    for line in lines:
        if _FENCE.match(line):
            in_code = not in_code
            cur.append(line)
            continue
        if not in_code and not line.strip():
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        if not in_code and _HEADING.match(line):
            if cur:
                blocks.append("\n".join(cur))
            blocks.append(line)  # a heading is its own block, so it is never split from its text
            cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def chunk_markdown(md, max_words=300):
    """Heading-aware chunks: never cross a heading, never split a code block, pack paragraphs up to
    max_words (a long paragraph is split on word boundaries). Returns [{'headings': [...], 'text': str}]."""
    sections, path, cur, in_code = [], [], [], False
    for line in md.split("\n"):
        if _FENCE.match(line):
            in_code = not in_code
        m = None if in_code else _HEADING.match(line)
        if m and not _FENCE.match(line):
            sections.append((list(path), cur))
            level = len(m.group(1))
            path = [h for h in path if h[0] < level] + [(level, m.group(2))]
            cur = [line]
            continue
        cur.append(line)
    sections.append((list(path), cur))

    chunks = []
    for heads, lines in sections:
        blocks = _blocks(lines)
        if not blocks or all(_HEADING.match(b) for b in blocks):
            continue  # empty, or a heading with no text of its own (it is in the children's headings)
        pieces, buf, n = [], [], 0
        for b in blocks:
            is_code = bool(_FENCE.match(b))
            bw = _words(b)
            if bw > max_words and not is_code:
                if buf and not all(_HEADING.match(x) for x in buf):
                    pieces.append("\n\n".join(buf))
                    buf, n = [], 0
                # a heading left in buf goes in front of the first window, inside the word limit
                words = b.split()
                first = max(1, max_words - n)
                pieces.append("\n\n".join(buf + [" ".join(words[:first])]))
                for i in range(first, len(words), max_words):
                    pieces.append(" ".join(words[i:i + max_words]))
                buf, n = [], 0
                continue
            if buf and n + bw > max_words:
                pieces.append("\n\n".join(buf))
                buf, n = [], 0
            buf.append(b)
            n += bw
        if buf:
            pieces.append("\n\n".join(buf))
        for p in pieces:
            chunks.append({"headings": [h[1] for h in heads], "text": p})
    return chunks


def chunk_records(url, title, fetched_at, md, max_words=300):
    base = hashlib.sha1(url.encode()).hexdigest()[:12]
    records = []
    for i, c in enumerate(chunk_markdown(md, max_words)):
        records.append({
            "id": f"{base}-{i:04d}",
            "url": url,
            "title": title,
            "headings": c["headings"],
            "chunk_index": i,
            "text": c["text"],
            "words": _words(c["text"]),
            "content_sha256": hashlib.sha256(c["text"].encode()).hexdigest(),
            "fetched_at": fetched_at,
        })
    return records


# --------------------------------------------------------------------------- run


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(fetcher, entries, out_dir, chunks_path=None, chunk_words=300, skip_unchanged=False,
        now=utc_now, log=None):
    """Fetch, convert and save each entry. Writes <out>/_manifest.jsonl. Returns a summary dict."""
    log = log or (lambda msg: None)
    os.makedirs(out_dir, exist_ok=True)
    summary = {"selected": len(entries), "saved": 0, "unchanged": 0, "skipped": 0, "errors": 0, "chunks": 0}
    used_paths = set()
    chunk_file = open(chunks_path, "w", encoding="utf-8") if chunks_path else None
    manifest = open(os.path.join(out_dir, "_manifest.jsonl"), "w", encoding="utf-8")
    try:
        for n, e in enumerate(entries, 1):
            rel = url_to_path(e.loc)
            if rel in used_paths:
                root, ext = os.path.splitext(rel)
                rel = f"{root}-{hashlib.sha1(e.loc.encode()).hexdigest()[:8]}{ext}"
            used_paths.add(rel)
            path = os.path.join(out_dir, rel)
            rec = {"url": e.loc, "lastmod": e.lastmod, "file": None, "status": None}
            fields = body = None

            if skip_unchanged and e.lastmod and os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    old, old_body = read_front_matter(f.read())
                if old.get("lastmod") == e.lastmod:
                    fields, body = old, old_body
                    rec["status"] = "unchanged"
                    summary["unchanged"] += 1

            if fields is None:
                try:
                    final, status, hdrs, raw = fetcher.get(e.loc)
                except Blocked as b:
                    rec["status"], rec["reason"] = "skipped", str(b)
                except FetchError as f:
                    rec["status"], rec["reason"] = "error", str(f)
                else:
                    ctype = hdrs.get("content-type", "").split(";")[0].strip().lower()
                    if status != 200:
                        rec["status"], rec["reason"] = "error", f"HTTP {status}"
                    elif len(raw) > MAX_PAGE_BYTES:
                        rec["status"], rec["reason"] = "skipped", "page larger than 10 MB"
                    elif ctype and ctype not in HTML_TYPES:
                        rec["status"], rec["reason"] = "skipped", f"not HTML ({ctype})"
                    else:
                        charset = re.search(r"charset=([\w-]+)", hdrs.get("content-type", ""), re.I)
                        try:
                            html = raw.decode(charset.group(1) if charset else "utf-8", errors="replace")
                        except LookupError:
                            html = raw.decode("utf-8", errors="replace")
                        md, title = html_to_markdown(html, final)
                        if not md:
                            rec["status"], rec["reason"] = "skipped", "no main content found"
                        else:
                            fields = {"url": e.loc, "title": title, "fetched_at": now()}
                            if final != e.loc:
                                fields["final_url"] = final
                            if e.lastmod:
                                fields["lastmod"] = e.lastmod
                            fields["words"] = _words(md)
                            body = md
                            os.makedirs(os.path.dirname(path), exist_ok=True)
                            with open(path, "w", encoding="utf-8") as f:
                                f.write(front_matter(fields) + body)
                            rec["status"] = "saved"
                            summary["saved"] += 1
                if rec["status"] == "skipped":
                    summary["skipped"] += 1
                elif rec["status"] == "error":
                    summary["errors"] += 1

            if fields is not None:
                rec["file"] = rel
                rec["words"] = fields.get("words")
                if chunk_file:
                    records = chunk_records(e.loc, fields.get("title", ""), fields.get("fetched_at"), body, chunk_words)
                    for r in records:
                        chunk_file.write(json.dumps(r, ensure_ascii=False) + "\n")
                    rec["chunks"] = len(records)
                    summary["chunks"] += len(records)
            reason = f"  ({rec['reason']})" if rec.get("reason") else ""
            log(f"[{n}/{len(entries)}] {rec['status']:9} {e.loc}{reason}")
            manifest.write(json.dumps({k: v for k, v in rec.items() if v is not None}, ensure_ascii=False) + "\n")
    finally:
        manifest.close()
        if chunk_file:
            chunk_file.close()
    summary["requests"] = fetcher.requests
    return summary


def build_parser():
    ap = argparse.ArgumentParser(
        prog="sitemap2md",
        description="Read a sitemap (or a site's robots.txt Sitemap: lines), fetch the pages politely "
                    "and save them as Markdown with front matter. Optionally write RAG chunks as JSONL.")
    ap.add_argument("sources", nargs="+", metavar="SOURCE",
                    help="sitemap URL, local sitemap file, or a site root like https://docs.example.com/")
    ap.add_argument("-o", "--out", default="markdown", help="output folder (default: markdown)")
    ap.add_argument("--max-pages", type=int, default=50,
                    help="stop after this many pages, 0 for no limit (default: 50)")
    ap.add_argument("--include", action="append", default=[], metavar="REGEX", help="only URLs matching (repeatable)")
    ap.add_argument("--exclude", action="append", default=[], metavar="REGEX", help="drop URLs matching (repeatable)")
    ap.add_argument("--since", metavar="YYYY-MM-DD", help="only URLs whose <lastmod> is on or after this date")
    ap.add_argument("--chunks", metavar="FILE.jsonl", help="also write heading-aware chunks to this JSONL file")
    ap.add_argument("--chunk-words", type=int, default=300, help="max words per chunk (default: 300)")
    ap.add_argument("--skip-unchanged", action="store_true",
                    help="don't refetch a page whose saved file has the same <lastmod> as the sitemap")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds between requests to one host; a larger robots.txt Crawl-delay wins (default: 1.0)")
    ap.add_argument("--user-agent", default=DEFAULT_UA, help=f"User-Agent header (default: {DEFAULT_UA})")
    ap.add_argument("--timeout", type=float, default=30, help="seconds per request (default: 30)")
    ap.add_argument("--retries", type=int, default=3, help="retries on 429/5xx/network errors (default: 3)")
    ap.add_argument("--max-depth", type=int, default=5, help="how deep to follow nested sitemap indexes (default: 5)")
    ap.add_argument("--dry-run", action="store_true", help="list the URLs that would be fetched, fetch no pages")
    ap.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    ap.add_argument("--version", action="version", version=f"sitemap2md {__version__}")
    return ap


def main(argv=None, http=None, clock=time.monotonic, sleep=time.sleep, now=utc_now):
    args = build_parser().parse_args(argv)
    if args.since and not re.match(r"\d{4}-\d{2}-\d{2}$", args.since):
        print("--since must look like 2026-01-31", file=sys.stderr)
        return 2
    if args.delay < 0 or args.chunk_words < 1:
        print("--delay must be >= 0 and --chunk-words >= 1", file=sys.stderr)
        return 2
    log = (lambda m: None) if args.quiet else (lambda m: print(m, file=sys.stderr))
    fetcher = Fetcher(args.user_agent, args.delay, args.timeout, args.retries, http=http,
                      clock=clock, sleep=sleep, log=log)
    started = time.monotonic()
    entries, stats = collect(fetcher, args.sources, args.max_depth, log)
    picked = select(entries, args.include, args.exclude, args.since, args.max_pages)
    log(f"{len(entries)} URLs in {stats['sitemaps']} sitemap(s); {len(picked)} selected")
    if not entries:
        print("No URLs found in the sitemap(s).", file=sys.stderr)
        return 1
    if args.dry_run:
        for e in picked:
            print(f"{e.loc}\t{e.lastmod or ''}")
        return 0
    summary = run(fetcher, picked, args.out, args.chunks, args.chunk_words, args.skip_unchanged, now=now, log=log)
    summary["urls_in_sitemaps"] = len(entries)
    summary["seconds"] = round(time.monotonic() - started, 1)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
