# Test results

Run on 2026-11-01 between 20:55 and 21:03 EST (Linux x86_64).

## Offline suite (`python -m unittest -v`)

45 offline tests plus the live test, which is skipped unless `LIVE=1`. Every HTTP response comes from
`tests/fixtures` through `tests/fake_http.py`; the clock and sleep are faked, so no test waits.

| Python | trafilatura | Result |
|---|---|---|
| 3.13.5 | 2.3.1 | Ran 46 tests in 0.262s, OK (skipped=1) |
| 3.12.15 | 2.3.1 | Ran 46 tests in 0.238s, OK (skipped=1) |
| 3.10.22 | 2.3.1 | Ran 46 tests in 0.348s, OK (skipped=1) |

What it covers: urlset, sitemap index, nested and looping indexes, gzip, plain-text sitemaps, a
sitemap that is really HTML, relative locs, dedupe, depth limit, discovery from robots.txt and the
`/sitemap.xml` fallback; `--include`, `--exclude`, `--since`, `--max-pages`; robots.txt Disallow,
user-agent groups, `Crawl-delay`, `Request-rate`, per-host spacing, the User-Agent header, 4xx/5xx/429
robots.txt handling (RFC 9309), redirects into disallowed paths, Content-Signal `ai-input=no`, retries
with `Retry-After` in seconds and as an HTTP date, giving up on long waits; Markdown extraction and
cleanup, front matter round trip, URL-to-path mapping (query strings, Unicode, collisions); chunking
(headings, code fences, long paragraphs, ids, hashes); and the CLI end to end (full run, manifest,
`--skip-unchanged`, `--dry-run`, exit codes).

## Live test (`LIVE=1 python -m unittest tests.test_live -v`)

Python 3.13.5, 2026-11-01 21:02 EST, against https://docs.docker.com/ (robots.txt: `User-agent: *` with
no Disallow, `Content-Signal: ai-train=yes, search=yes, ai-input=yes`).

```text
live: 2168 URLs in sitemap, {'selected': 2, 'saved': 2, 'unchanged': 0, 'skipped': 0, 'errors': 0, 'chunks': 15, 'requests': 4}
ok
Ran 1 test in 3.093s
OK
```

4 requests: robots.txt, the sitemap, two pages.

## Real runs used in the README

| When (EST) | Command | Result |
|---|---|---|
| 2026-11-01 20:57 | `sitemap2md https://docs.docker.com/ --include '/get-started/' --max-pages 0 -o docker-md --chunks docker-chunks.jsonl` | 28 selected, 28 saved, 0 errors, 236 chunks, 30 requests, 29.1 s |
| 2026-11-01 20:58 | same command plus `--skip-unchanged` | 28 unchanged, 236 chunks, 2 requests, 1.1 s |
| 2026-11-01 20:58 | `sitemap2md https://kubernetes.io/ --include '^https://kubernetes.io/docs/concepts/' --max-pages 5 --dry-run` | index with 17 child sitemaps, 6,634 URLs in 18 sitemaps, 5 selected, no pages fetched |

Measured on the Docker run with tiktoken `cl100k_base` (used for measuring only; not a dependency):
the raw HTML of "Writing a Dockerfile" is 211,646 bytes / 95,642 tokens and its Markdown file is
5,953 bytes / 1,404 tokens. The 236 chunks average 114.6 words (min 10, max 459) and 184 tokens (max 780).

## Other checks

| Check | Result |
|---|---|
| `actionlint .github/workflows/ci.yml` (actionlint 1.7.12) | no findings |
| `ruff check --select E,F,W --line-length 120 .` (ruff 0.17.3) | All checks passed |
| `pip install .` into a clean Python 3.13 venv, then `sitemap2md --version` | `sitemap2md 0.1.0` |
| `python sitemap2md.py tests/fixtures/sitemap.txt --dry-run` (the CI smoke step) | 2 URLs listed, exit 0 |

The GitHub Actions workflow itself has not run yet; it runs on the first push.
