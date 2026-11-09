#!/usr/bin/env python3
"""合成の利用者の中継（ADR 0043 の O4 synthetic_users_loop）。ワーカーの中で、触れる環境の画面の入口ごとに手前に立つ。

- 利用者（ヘッドレスの Chromium）はこの中継の URL を開く。中継は上流（触れる環境）にそのまま渡し、1 回ごとの応答時間を JSON Lines に残す
  （行動ログの応答時間は、エージェントの申告ではなくここで測る）
- わざと仕込む詰まり（friction）はここで入れる。製品のコードにも環境の設定にも入れない（ADR 0043 の O4「製品のコードに入れない」）
  - slow: パスの頭が合う API の応答を delay_ms だけ遅らせる（例 {"path": "/api/bookings", "method": "GET", "delay_ms": 2500}）
  - hide: その入口の HTML に、selector を見えなくする style を足す（例 {"entry": "ADMIN_WEB_PORT", "selector": "a[href='/headquarters/bookings']"}）
- 仕込みを入れるかは dispatcher が決めて入力の friction で渡す（工場の環境変数 SYNTH_FRICTION。staging のときだけ）

依存は標準ライブラリだけ。
"""
from __future__ import annotations

import http.client
import json
import math
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
       "content-length", "accept-encoding", "host"}
ID_RES = [re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), re.compile(r"(?<=/)\d+(?=/|$)")]


def path_key(path: str) -> str:
    """応答時間を束ねる名前。クエリを落とし、id に見える部分を {id} にする"""
    p = urlsplit(path).path or "/"
    for r in ID_RES:
        p = r.sub("{id}", p)
    return p


def is_api(path: str) -> bool:
    return urlsplit(path).path.startswith("/api/")


def slow_hit(friction: dict | None, method: str, path: str) -> int:
    s = (friction or {}).get("slow") or {}
    if not s.get("path") or not urlsplit(path).path.startswith(str(s["path"])):
        return 0
    if s.get("method") and str(s["method"]).upper() != method.upper():
        return 0
    return max(0, int(s.get("delay_ms") or 0))


def inject_hide(body: bytes, selector: str) -> bytes:
    """HTML の head の終わり（なければ先頭）に、selector を見えなくする style を足す"""
    if not selector:
        return body
    tag = f"<style data-synthetic-friction>{selector}{{display:none !important}}</style>".encode()
    i = body.lower().find(b"</head>")
    return body[:i] + tag + body[i:] if i >= 0 else tag + body


class Relay:
    """入口 1 つ分の中継。start() で 127.0.0.1 の空いている口に立ち、url を返す"""

    def __init__(self, label: str, upstream: str, friction: dict | None, log, entry_id: str | None = None):
        u = urlsplit(upstream)
        self.label, self.entry_id = label, entry_id
        self.host, self.port, self.scheme = u.hostname, u.port or (443 if u.scheme == "https" else 80), u.scheme
        self.base = f"{u.scheme}://{u.netloc}"
        self.friction, self.log = friction or {}, log
        self.server: ThreadingHTTPServer | None = None
        self.url = ""

    def hide_selector(self) -> str:
        h = self.friction.get("hide") or {}
        return str(h.get("selector") or "") if h.get("entry") and h.get("entry") == self.entry_id else ""

    def handler(self):
        relay = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):   # 標準エラーを汚さない
                pass

            def _do(self):
                t0 = time.monotonic()
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else None
                headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
                headers["Host"] = f"{relay.host}:{relay.port}"
                delay = slow_hit(relay.friction, self.command, self.path)
                status, rh, data = 502, [], b""
                try:
                    c = (http.client.HTTPSConnection if relay.scheme == "https" else http.client.HTTPConnection)(relay.host, relay.port, timeout=60)
                    c.request(self.command, self.path, body=body, headers=headers)
                    r = c.getresponse()
                    status, rh, data = r.status, r.getheaders(), r.read()
                    c.close()
                except OSError as e:
                    data = f"synthetic relay: {e}".encode()
                if delay:
                    time.sleep(delay / 1000)
                ctype = next((v for k, v in rh if k.lower() == "content-type"), "")
                sel = relay.hide_selector()
                if sel and "text/html" in ctype:
                    data = inject_hide(data, sel)
                self.send_response(status)
                for k, v in rh:
                    if k.lower() in HOP:
                        continue
                    if k.lower() == "location" and v.startswith(relay.base):
                        v = relay.url.rstrip("/") + v[len(relay.base):]
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
                relay.log({"entry": relay.entry_id or relay.label, "method": self.command, "path": path_key(self.path), "api": is_api(self.path),
                           "status": status, "ms": int((time.monotonic() - t0) * 1000), "delayed_ms": delay,
                           "hidden": bool(sel and "text/html" in ctype)})

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _do

        return H

    def start(self) -> str:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.url

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class Relays:
    """環境の入口（core-gates/qa.py の env_entries）ごとに中継を立て、入口の URL を中継の URL に置き換える"""

    def __init__(self, env: dict, friction: dict | None, log_path):
        self.env, self.friction, self.log_path = env, friction or {}, log_path
        self.relays: list[Relay] = []
        self._lock = threading.Lock()

    def _log(self, row: dict):
        with self._lock, open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def __enter__(self) -> dict:
        entries = []
        for e in self.env.get("entries") or []:
            u = urlsplit(e["url"])
            r = Relay(e.get("label") or "", f"{u.scheme}://{u.netloc}", self.friction, self._log, e.get("port_env"))
            base = r.start()
            self.relays.append(r)
            entries.append({**e, "url": base + (u.path or "/") + (f"?{u.query}" if u.query else "")})
        return {**self.env, "entries": entries}

    def __exit__(self, *exc):
        for r in self.relays:
            r.stop()
        return False


def read_log(path) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


def timings(rows: list[dict], only_api: bool = True) -> list[dict]:
    """パスごとの応答時間（件数・中央値・p95・最大）。遅い順"""
    by: dict[tuple, list[int]] = {}
    for r in rows:
        if only_api and not r.get("api"):
            continue
        by.setdefault((r.get("entry"), r.get("method"), r.get("path")), []).append(int(r.get("ms") or 0))
    out = []
    for (entry, method, path), ms in by.items():
        ms = sorted(ms)
        p95 = ms[max(0, math.ceil(0.95 * len(ms)) - 1)]
        out.append({"entry": entry, "method": method, "path": path, "n": len(ms), "p50": ms[len(ms) // 2], "p95": p95, "max": ms[-1]})
    return sorted(out, key=lambda x: -x["p95"])
