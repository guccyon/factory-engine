"""GitHub の API の共通の入口。GET は ETag で条件付きにし、304 のときは前の本文を返す（上限に数えない）。"""
import io
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ghapi  # noqa: E402


class Resp(io.BytesIO):
    def __init__(self, body, etag):
        super().__init__(json.dumps(body).encode())
        self.headers = {"ETag": etag} if etag else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake(monkeypatch, responses):
    seen = []

    def urlopen(req, timeout=30):
        seen.append(dict(req.header_items()))
        r = responses.pop(0)
        if r == 304:
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)
        return r
    monkeypatch.setattr(ghapi.urllib.request, "urlopen", urlopen)
    ghapi._cache.clear()
    return seen


def test_unchanged_get_returns_cached_body_with_conditional_request(monkeypatch):
    seen = fake(monkeypatch, [Resp({"n": 1}, '"e1"'), 304])
    assert ghapi.request("GET", "repos/o/r/pulls/1") == {"n": 1}
    assert ghapi.request("GET", "repos/o/r/pulls/1") == {"n": 1}
    assert "If-none-match" not in seen[0] and seen[1]["If-none-match"] == '"e1"'


def test_changed_get_replaces_cache(monkeypatch):
    fake(monkeypatch, [Resp({"n": 1}, '"e1"'), Resp({"n": 2}, '"e2"'), 304])
    ghapi.request("GET", "p")
    assert ghapi.request("GET", "p") == {"n": 2}
    assert ghapi.request("GET", "p") == {"n": 2}


def test_writes_are_never_conditional(monkeypatch):
    seen = fake(monkeypatch, [Resp({"n": 1}, '"e1"'), Resp({}, '"e2"')])
    ghapi.request("GET", "p")
    ghapi.request("POST", "p", {"x": 1})
    assert "If-none-match" not in seen[1]


def test_other_errors_still_raise(monkeypatch):
    def urlopen(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
    monkeypatch.setattr(ghapi.urllib.request, "urlopen", urlopen)
    ghapi._cache.clear()
    try:
        ghapi.request("GET", "missing")
        raise AssertionError("should raise")
    except urllib.error.HTTPError as e:
        assert e.code == 404
