"""GitHub の REST API を呼ぶ共通の入口（dispatcher・supervisor・PR の見張り）。

GET は条件付きリクエスト（ETag / If-None-Match）にする。変わっていなければ GitHub は 304 を返し、
304 は API の上限（1 時間 5,000 回）に数えられない。工場は開いている PR を毎分見に行くので、
人の判断・承認を待って動かない PR の見張りが上限を食い尽くしていた（11-01 06:41 JST に上限切れでラインが止まった）。
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from collections import OrderedDict

GH_TOKEN = os.environ.get("GH_TOKEN", "")
CACHE_MAX = int(os.environ.get("GH_ETAG_CACHE_MAX", "2000"))

_cache: OrderedDict[str, tuple[str, bytes]] = OrderedDict()   # path → (ETag, 本文)。プロセスの中だけ（再起動で空になるだけ）
_lock = threading.Lock()
stats = {"requests": 0, "not_modified": 0}


def _remember(path: str, etag: str | None, data: bytes):
    if not etag:
        return
    with _lock:
        _cache[path] = (etag, data)
        _cache.move_to_end(path)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)


def request(method: str, path: str, body: dict | None = None, *, user_agent: str = "factory", token: str | None = None):
    headers = {"Authorization": f"Bearer {token if token is not None else GH_TOKEN}", "Accept": "application/vnd.github+json",
               "Content-Type": "application/json", "User-Agent": user_agent}
    cached = None
    if method == "GET":
        with _lock:
            cached = _cache.get(path)
        if cached:
            headers["If-None-Match"] = cached[0]
    req = urllib.request.Request(f"https://api.github.com/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None, headers=headers)
    stats["requests"] += 1
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            if method == "GET":
                _remember(path, r.headers.get("ETag"), data)
    except urllib.error.HTTPError as e:
        if e.code != 304 or not cached:
            raise
        stats["not_modified"] += 1
        data = cached[1]
    return json.loads(data) if data else None
