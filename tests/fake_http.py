"""A stand-in for sitemap2md.urllib_http that serves saved responses by URL and records every request."""
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"
HTML = {"content-type": "text/html; charset=utf-8"}
XML = {"content-type": "application/xml"}
TEXT = {"content-type": "text/plain"}


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


class FakeHTTP:
    """routes: url -> (status, headers, body) or a list of those, served in order (the last one repeats).
    robots.txt answers 404 (no rules) unless routed. Any other URL raises, so tests never touch the network.
    A route value may also be an Exception instance, which is raised (to simulate network errors)."""

    def __init__(self, routes=None, clock=None):
        self.routes = {k: (v if isinstance(v, list) else [v]) for k, v in (routes or {}).items()}
        self.calls = []
        self.clock = clock

    def __call__(self, url, headers, timeout=30, max_bytes=None):
        self.calls.append((url, dict(headers), self.clock() if self.clock else None))
        if url not in self.routes:
            if url.endswith("/robots.txt"):
                return 404, {}, b"not found"
            raise AssertionError(f"unexpected request: {url}")
        queue = self.routes[url]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        status, hdrs, body = item
        if isinstance(body, str):
            body = body.encode()
        return status, hdrs, body

    def urls(self):
        return [u for u, _, _ in self.calls]

    def page_calls(self):
        return [u for u in self.urls() if not u.endswith("/robots.txt")]


class FakeClock:
    """time.monotonic and time.sleep replacement: sleeping moves the clock forward."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s
