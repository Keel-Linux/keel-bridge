"""Tests of keelbridge.http."""

import io
import json
import urllib.error

import pytest

from keelbridge import http as kh
from keelbridge.http import Http, HttpError, next_link


class Transport:
    def __init__(self, answers):
        self.answers = list(answers)
        self.sent = []

    def __call__(self, method, url, headers, data):
        self.sent.append((method, url, dict(headers), data))
        ans = self.answers.pop(0)
        if isinstance(ans, Exception):
            raise ans
        return ans


def mk(answers, cache=None):
    t = Transport(answers)
    return Http("https://api.x/", lambda: "token T", send=t, cache=cache, sleep=lambda s: None), t


def test_get_sends_auth_and_parses_json():
    h, t = mk([(200, {}, '{"a": 1}')])
    assert h.get("/p") == {"a": 1}
    method, url, headers, data = t.sent[0]
    assert (method, url, data) == ("GET", "https://api.x/p", None)
    assert headers["Authorization"] == "token T"


def test_post_patch_put_delete_send_json():
    h, t = mk([(201, {}, '{"id": 1}'), (200, {}, "{}"), (200, {}, ""), (204, {}, "")])
    assert h.post("/a", {"x": 1}) == {"id": 1}
    h.patch("/a", {"y": 2})
    assert h.put("/a", {}) is None
    assert h.delete("/a") is None
    assert json.loads(t.sent[0][3]) == {"x": 1}
    assert t.sent[0][2]["Content-Type"] == "application/json"
    assert [s[0] for s in t.sent] == ["POST", "PATCH", "PUT", "DELETE"]


def test_full_url_on_the_same_host_is_used_as_is():
    h, t = mk([(200, {}, "[]")])
    h.get("https://api.x/x?page=2")
    assert t.sent[0][1] == "https://api.x/x?page=2"


def test_full_url_on_another_host_is_refused():
    h, t = mk([])
    with pytest.raises(ValueError):
        h.get("https://evil.example/x")
    assert t.sent == []


def test_post_is_not_retried():
    h, t = mk([(502, {}, "bad")])
    with pytest.raises(HttpError):
        h.post("/a", {})
    assert len(t.sent) == 1


def test_rate_limit_raises_rate_limited():
    from keelbridge.http import RateLimited
    h, _ = mk([(403, {"X-RateLimit-Remaining": "0"}, "limit")])
    with pytest.raises(RateLimited):
        h.get("/p")
    h, _ = mk([(429, {"Retry-After": "60"}, "slow")])
    with pytest.raises(RateLimited):
        h.get("/p")
    h, _ = mk([(403, {}, "forbidden")])
    with pytest.raises(HttpError) as err:
        h.get("/p")
    assert not isinstance(err.value, RateLimited)


def test_no_redirect_handler_refuses():
    assert kh._NoRedirect().redirect_request(None, None, 302, "", {}, "https://x") is None


def test_multi_page_lists_are_not_cached():
    cache = Cache()
    h, _ = mk([(200, {"ETag": "e", "Link": '<https://api.x/p?page=2>; rel="next"'}, "[1]"),
               (200, {}, "[2]")], cache=cache)
    assert h.get_list("/p") == [1, 2]
    assert cache.d == {}


def test_get_list_without_the_cache():
    cache = Cache()
    cache.d["https://api.x/p"] = ("e", "[9]")
    h, t = mk([(200, {"ETag": "f"}, "[1]")], cache=cache)
    assert h.get_list("/p", cache=False) == [1]
    assert "If-None-Match" not in t.sent[0][2]


def test_retries_on_server_errors_and_network_errors():
    h, t = mk([(502, {}, "bad"), OSError("reset"), (200, {}, "[1]")])
    assert h.get("/p") == [1]
    assert len(t.sent) == 3


def test_gives_up_after_three_server_errors():
    h, _ = mk([(500, {}, "e")] * 3)
    with pytest.raises(HttpError) as err:
        h.get("/p")
    assert err.value.status == 500


def test_client_error_raises_with_status():
    h, _ = mk([(404, {}, "not found")])
    with pytest.raises(HttpError) as err:
        h.get("/p")
    assert err.value.status == 404 and "not found" in str(err.value)


def test_next_link():
    assert next_link({"Link": '<https://a/2>; rel="next", <https://a/9>; rel="last"'}) == "https://a/2"
    assert next_link({"link": '<https://a/1>; rel="prev"'}) is None
    assert next_link({}) is None


def test_get_list_follows_pages():
    h, t = mk([(200, {"Link": '<https://api.x/p?page=2>; rel="next"'}, "[1, 2]"),
               (200, {}, "[3]")])
    assert h.get_list("/p") == [1, 2, 3]
    assert t.sent[1][1] == "https://api.x/p?page=2"


class Cache:
    def __init__(self):
        self.d = {}

    def http_get(self, url):
        return self.d.get(url)

    def http_put(self, url, etag, body):
        self.d[url] = (etag, body)


def test_get_list_uses_the_etag_and_a_304_returns_the_cache():
    cache = Cache()
    h, t = mk([(200, {"ETag": '"e1"'}, "[1]"), (304, {}, "")], cache=cache)
    assert h.get_list("/p") == [1]
    assert h.get_list("/p") == [1]
    assert t.sent[1][2]["If-None-Match"] == '"e1"'


def test_get_list_without_etag_does_not_cache():
    cache = Cache()
    h, _ = mk([(200, {}, "[1]")], cache=cache)
    h.get_list("/p")
    assert cache.d == {}


class FakeResp(io.BytesIO):
    status = 200
    headers = {"ETag": "x"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_urllib_send_success_and_error(monkeypatch):
    def ok(req, timeout):
        return FakeResp(b'{"ok": true}')
    monkeypatch.setattr(kh._OPENER, "open", ok)
    assert kh.urllib_send("GET", "https://a", {}, None) == (200, {"ETag": "x"}, '{"ok": true}')

    def bad(req, timeout):
        raise urllib.error.HTTPError("https://a", 403, "no", {}, io.BytesIO(b"denied"))
    monkeypatch.setattr(kh._OPENER, "open", bad)
    status, _, body = kh.urllib_send("GET", "https://a", {}, None)
    assert (status, body) == (403, "denied")


def test_urllib_send_refuses_other_schemes():
    with pytest.raises(ValueError):
        kh.urllib_send("GET", "file:///etc/passwd", {}, None)
