"""A small JSON client over urllib, with retries, paging and ETags.

A GET of a list sends the ETag of the last answer. A 304 answer costs no
GitHub rate limit, so a poll of a quiet repository is free. The ETags and
bodies are kept in the store, so they live across runs of the timer.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

RETRIES = 3
TIMEOUT = 30


IDEMPOTENT = ("GET", "PUT", "DELETE", "PATCH")


class HttpError(Exception):
    def __init__(self, status, method, url, body):
        super().__init__(f"{method} {url}: HTTP {status}: {body[:200]}")
        self.status = status


class RateLimited(HttpError):
    """The server refused the call because of its rate limit."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would carry the Authorization header."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_send(method, url, headers, data):
    """The real transport: (status, headers, body text). HTTPS only."""
    if not url.startswith("https://"):
        raise ValueError(f"not an https URL: {url}")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers or {}), err.read().decode(errors="replace")


def next_link(headers):
    link = headers.get("Link") or headers.get("link") or ""
    for part in link.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return None


class Http:
    def __init__(self, base, auth, send=urllib_send, cache=None, sleep=time.sleep,
                 user_agent="keel-bridge"):
        self.base = base.rstrip("/")
        self.auth = auth  # a callable: returns the Authorization header value
        self.send = send
        self.cache = cache
        self.sleep = sleep
        self.user_agent = user_agent

    def _url(self, path):
        if not path.startswith("https://"):
            return self.base + path
        if urllib.parse.urlsplit(path).netloc != urllib.parse.urlsplit(self.base).netloc:
            raise ValueError(f"refused: {path} is not on {self.base}")
        return path

    def request(self, method, path, body=None, etag=None):
        url = self._url(path)
        headers = {"Authorization": self.auth(), "Accept": "application/json",
                   "User-Agent": self.user_agent}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if etag:
            headers["If-None-Match"] = etag
        tries = RETRIES if method in IDEMPOTENT else 1
        for attempt in range(tries):
            try:
                status, rheaders, text = self.send(method, url, headers, data)
            except OSError as err:
                status, rheaders, text = 599, {}, str(err)
            if status < 500:
                break
            if attempt + 1 < tries:
                self.sleep(2 ** attempt)
        if status == 304:
            return status, rheaders, None
        low = {k.lower(): v for k, v in rheaders.items()}
        if status in (403, 429) and ("retry-after" in low or low.get("x-ratelimit-remaining") == "0"):
            raise RateLimited(status, method, url, text)
        if status >= 400:
            raise HttpError(status, method, url, text)
        return status, rheaders, (json.loads(text) if text.strip() else None)

    def get(self, path):
        return self.request("GET", path)[2]

    def post(self, path, body):
        return self.request("POST", path, body)[2]

    def patch(self, path, body):
        return self.request("PATCH", path, body)[2]

    def put(self, path, body):
        return self.request("PUT", path, body)[2]

    def delete(self, path):
        return self.request("DELETE", path)[2]

    def get_list(self, path, cache=True):
        """All pages of a list.

        With a cache, a list of one page uses its ETag. A list of more pages
        is not cached: a new entry at its end does not change page one.
        """
        url = self._url(path)
        cache = self.cache if cache else None
        cached = cache.http_get(url) if cache else None
        status, headers, page = self.request("GET", url, etag=cached[0] if cached else None)
        if status == 304:
            return json.loads(cached[1])
        first_etag = headers.get("ETag") or headers.get("etag")
        items = list(page or [])
        nxt = next_link(headers)
        pages = 1
        while nxt:
            _, headers, more = self.request("GET", nxt)
            items.extend(more or [])
            nxt = next_link(headers)
            pages += 1
        if cache and first_etag and pages == 1:
            cache.http_put(url, first_etag, json.dumps(items))
        return items
