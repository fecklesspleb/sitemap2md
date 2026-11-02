# sitemap2md

Turn a website's sitemap into clean Markdown files, one per page, with front matter, and optionally
into heading-aware chunks (JSONL) ready for a RAG index or an LLM's context.

- Reads `sitemap.xml`, nested sitemap indexes, gzipped sitemaps (`.xml.gz`) and plain-text sitemaps.
  Give it a site root and it finds the sitemaps from the `Sitemap:` lines in robots.txt.
- Fetches politely: obeys robots.txt (including `Crawl-delay`), waits between requests to each host,
  sends an identifying User-Agent and backs off on 429/5xx with `Retry-After`.
- Keeps the main content only (via [trafilatura](https://github.com/adbar/trafilatura)), so navigation,
  sidebars and footers are gone and headings, lists, tables, links and code blocks are kept.
- One Python file, one direct dependency, MIT licensed. No API keys and no account.

## Quickstart

```bash
git clone https://github.com/fecklesspleb/sitemap2md
cd sitemap2md
python -m pip install .          # or: python -m pip install -r requirements.txt
sitemap2md https://docs.docker.com/ --include '/get-started/' --max-pages 0 \
  -o docker-md --chunks docker-chunks.jsonl
```

Real output from 2026-11-01 (20:57 EST):

```text
https://docs.docker.com: robots.txt read
sitemap https://docs.docker.com/sitemap.xml: 2168 URLs
2168 URLs in 1 sitemap(s); 28 selected
[1/28] saved     https://docs.docker.com/get-started/tutorials/run-an-app/
[2/28] saved     https://docs.docker.com/get-started/docker-concepts/running-containers/publishing-ports/
...
[28/28] saved     https://docs.docker.com/get-started/
{"selected": 28, "saved": 28, "unchanged": 0, "skipped": 0, "errors": 0, "chunks": 236, "requests": 30, "urls_in_sitemaps": 2168, "seconds": 29.1}
```

30 requests (robots.txt, the sitemap, 28 pages) one second apart, 28 Markdown files, 236 chunks.
Running without `sitemap2md` installed works too: `python sitemap2md.py ...`.

Each page becomes a file whose path mirrors the URL:

```text
docker-md/docs.docker.com/get-started/docker-concepts/building-images/writing-a-dockerfile/index.md
```

```markdown
---
url: "https://docs.docker.com/get-started/docker-concepts/building-images/writing-a-dockerfile/"
title: "Writing a Dockerfile"
fetched_at: "2026-11-02T01:57:41Z"
lastmod: "2026-05-20"
words: 796
---

# Writing a Dockerfile

## Explanation

A Dockerfile is a text-based document that's used to create a container image. ...
```

`fetched_at` is UTC. `lastmod` comes from the sitemap and is left out when the sitemap has none.
`final_url` is added when the page redirected.

### See what a sitemap holds before fetching anything

`--dry-run` reads the sitemaps (following nested indexes) and prints the selected URLs with their
`lastmod`, without fetching a single page:

```bash
sitemap2md https://kubernetes.io/ --include '^https://kubernetes.io/docs/concepts/' --max-pages 5 --dry-run
```

```text
https://kubernetes.io: robots.txt read
sitemap https://kubernetes.io/sitemap.xml: 17 child sitemaps
sitemap https://kubernetes.io/en/sitemap.xml: 1889 URLs
...
6634 URLs in 18 sitemap(s); 5 selected
https://kubernetes.io/docs/concepts/security/cloud-native-security/	2025-11-21T11:31:13-08:00
https://kubernetes.io/docs/concepts/workloads/compositepodgroup-api/lifecycle/	2026-07-24T15:44:21+02:00
...
```

### Keep a copy up to date

Run the same command again with `--skip-unchanged`. A page whose saved file has the same `lastmod` as
the sitemap is not fetched again (its chunks are rebuilt from the saved file, so the JSONL stays
complete). The second Docker run above took 2 requests and 1.1 seconds:

```text
{"selected": 28, "saved": 0, "unchanged": 28, "skipped": 0, "errors": 0, "chunks": 236, "requests": 2, "urls_in_sitemaps": 2168, "seconds": 1.1}
```

Pages without a `lastmod` in the sitemap are always fetched again.

## Options

| Option | Default | What it does |
|---|---|---|
| `SOURCE ...` | | Sitemap URL, local sitemap file, or a site root such as `https://docs.example.com/` |
| `-o, --out` | `markdown` | Output folder |
| `--max-pages` | `50` | Stop after this many pages; `0` means no limit |
| `--include REGEX` | | Only URLs matching (repeatable; any match counts) |
| `--exclude REGEX` | | Drop URLs matching (repeatable) |
| `--since YYYY-MM-DD` | | Only URLs whose `lastmod` is on or after this date (URLs without one are kept) |
| `--chunks FILE` | | Also write chunks to this JSONL file |
| `--chunk-words` | `300` | Max words per chunk |
| `--skip-unchanged` | off | Don't refetch pages whose `lastmod` has not changed |
| `--delay` | `1.0` | Seconds between requests to one host; a larger robots.txt `Crawl-delay` wins |
| `--user-agent` | `sitemap2md/0.1.0 (+https://github.com/fecklesspleb/sitemap2md)` | User-Agent header |
| `--timeout` | `30` | Seconds per request |
| `--retries` | `3` | Retries on 429, 5xx and network errors |
| `--max-depth` | `5` | How deep to follow nested sitemap indexes |
| `--dry-run` | off | List the selected URLs; fetch no pages |
| `-q, --quiet` | off | Print only the summary |

## Output

- `<out>/<host>/<path>.md`: one Markdown file per page (`/guide/` becomes `guide/index.md`,
  `/a.html` becomes `a.md`, a query string adds a short hash).
- `<out>/_manifest.jsonl`: one line per selected URL with `status` (`saved`, `unchanged`, `skipped`,
  `error`), the file, word and chunk counts, and a `reason` for anything not saved
  (for example `disallowed by robots.txt`, `not HTML (application/pdf)`, `HTTP 404`).
- `--chunks FILE`: one JSON object per chunk:

```json
{"id": "b2650e815a5e-0003", "url": "https://docs.docker.com/get-started/docker-concepts/building-images/writing-a-dockerfile/", "title": "Writing a Dockerfile", "headings": ["Writing a Dockerfile", "Try it out", "Set up"], "chunk_index": 3, "text": "### Set up\n\n[Download this ZIP file](https://github.com/docker/getting-started-todo-app/archive/refs/heads/build-image-from-scratch.zip) and extract the contents into a directory on your machine. ...", "words": 34, "content_sha256": "626447c5...", "fetched_at": "2026-11-02T01:57:41Z"}
```

(`text` and `content_sha256` shortened here.) `id` is the first 12 hex characters of the URL's SHA-1
plus the chunk number, so it stays the same between runs as long as the page's chunking does;
`content_sha256` tells you whether a chunk's text changed. `headings` is the heading path above the
chunk, which is useful to prepend when you embed it.

How chunking works: a chunk never crosses a heading and never splits a fenced code block; paragraphs
are packed together up to `--chunk-words`, and a single paragraph longer than that is split on word
boundaries. A code block longer than the limit stays whole, so a chunk can exceed it (in the Docker run
above: 236 chunks, mean 115 words, longest 459 words because of a code block).

## Using it from Python

```python
import sitemap2md as s

f = s.Fetcher(delay=1.0)
entries, stats = s.collect(f, ["https://docs.docker.com/"])
picked = s.select(entries, include=["/get-started/"], max_pages=10)
summary = s.run(f, picked, "docker-md", chunks_path="docker-chunks.jsonl")
```

## Being polite

- Requests go out one at a time, at least `--delay` seconds apart per host (default 1 second), or the
  robots.txt `Crawl-delay` / `Request-rate` if that is longer.
- robots.txt is read once per host and checked before every page, sitemap and redirect hop. Following
  [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309): a 4xx robots.txt means no rules; a 5xx, a 429 or a
  network error means the host is not crawled at all in that run.
- If robots.txt carries a [Content Signal](https://contentsignals.org/) of `ai-input=no` for us (or for
  `*`), pages on that host are skipped, because feeding them to an AI model is what this tool is for.
- 429 and 5xx answers are retried after `Retry-After` (or 2, 4, 8 seconds). If a server asks for more
  than 2 minutes, that URL is given up for the run instead of hammering it.
- The User-Agent names the tool and links here. If you change it, keep a way to reach you in it.

robots.txt is a floor, not a licence: check the site's terms before you republish what you fetch.

## Limits

- No JavaScript. Pages that build their content in the browser come back as `no main content found`.
- Main-content extraction is heuristic. Images are dropped (diagrams included) and an unusual layout can
  lose or keep a section it shouldn't. Look at a few files before you index thousands.
- Only `text/html` and `application/xhtml+xml` pages are converted; PDFs and other files are skipped
  and listed in the manifest.
- Sites behind a bot challenge answer robots.txt with 429 or a 5xx and get skipped (a 403 counts as "no rules", per RFC 9309, and the pages themselves then fail). On 2026-11-01 Read the Docs–hosted
  sites I tried (docs.pytest.org, requests.readthedocs.io) answered 429 with a challenge page.
- Word counts are words, not model tokens.
- One page at a time, by design. 1,000 pages on one host at the default delay take at least 17 minutes.

## Tests

```bash
python -m pip install -r requirements.txt
python -m unittest -v                          # offline: every response comes from tests/fixtures
LIVE=1 python -m unittest tests.test_live -v   # 4 real requests to docs.docker.com
```

The offline suite covers nested and looping sitemap indexes, gzip and text sitemaps, robots.txt
rules, `Crawl-delay`, per-host spacing, User-Agent, retries and `Retry-After`, redirects into
disallowed paths, content signals, Markdown cleanup, front matter, paths, chunking and the CLI end to
end. GitHub Actions runs it on Python 3.10, 3.12 and 3.13 (`.github/workflows/ci.yml`).

## License

MIT. Extraction is done by trafilatura (Apache-2.0).

---

Disclosure: for large sites, if you'd rather not run this yourself, my
[URL to Markdown Actor on Apify](https://apify.com/fecklesspleb/url-to-markdown) converts a list of
URLs (for example the output of `sitemap2md --dry-run | cut -f1`) at $0.50 per 1,000 pages (plus $0.00005 per run start at the default 256 MB memory). I built both.
