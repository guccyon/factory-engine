"""動くプロトタイプの配り口（ADR 0026 の決定 3。2026-11-01 オーナー: 画像版とあわせて、操作できるプロトタイプも出す）。

デザイナー役が書いた案の HTML（out/<案>.html・<案>-<コマ>.html と CSS）を、人がブラウザで触れるように配る。
HTML はエージェントの出力で、外部の入力と同じに扱う。操業コンソールとは別のポート（＝別のオリジン）で、次の囲いの中だけで動かす:

- 応答の CSP に sandbox（allow-scripts だけ）を付ける。文書は不透明なオリジンになり、Cookie・ストレージ・コンソールの API に届かない
- 通信は止める（connect-src・form-action 'none'）。読めるのは同じ配り口の CSS・画像だけ。外の資源も読まない（CORP は付けない: 不透明なオリジンからの CSS・画像の読み込みが止まるため）
- 配るのは、その UXSpec の files に載っていて、オブジェクトストアの design/ の下にある .html・.css・.png だけ。名前は英数字・-・_ に限る
- GET だけ。ホスト名は操業コンソールと同じ許可リストで確かめる（DNS リバインディングよけ）
- 費用はデザイナー役の回の記録（ui_design.drafted の prototype）で見て、続けるか外すかをオーナーが決める
"""
from __future__ import annotations

import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENV = os.environ
PORT = int(ENV.get("PROTOTYPE_PORT", "8791"))
PUBLIC_URL = ENV.get("PROTOTYPE_PUBLIC_URL", f"http://localhost:{PORT}").rstrip("/")
PATH_RE = re.compile(r"/p/(\d{1,12})/([A-Za-z0-9][A-Za-z0-9_-]{0,31})\.(html|css|png)")
TYPES = {"html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8", "png": "image/png"}
MAX_BYTES = 2_000_000


def url(aid: int, variant: str) -> str:
    """推す案の入り口。デザイナー役の HTML どうしは相対リンクでつながる"""
    return f"{PUBLIC_URL}/p/{int(aid)}/{variant}.html"


def parse(path: str):
    """(UXSpec の id, ファイル名, 拡張子) か None"""
    m = PATH_RE.fullmatch(path.split("?", 1)[0])
    return (int(m.group(1)), f"{m.group(2)}.{m.group(3)}", m.group(3)) if m else None


def headers(origin: str) -> dict[str, str]:
    """sandbox で不透明なオリジンになるので、同じ配り口の資源は 'self' でなくオリジンを書いて許す"""
    return {
        "Content-Security-Policy": (f"sandbox allow-scripts; default-src 'none'; style-src {origin} 'unsafe-inline'; img-src {origin} data:; "
                                    f"font-src {origin} data:; script-src 'unsafe-inline'; connect-src 'none'; form-action 'none'; "
                                    "frame-ancestors 'none'; base-uri 'none'"),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }


def lookup(cur, aid: int, name: str) -> str | None:
    """その UXSpec の files に載っている、design/ の下のキー"""
    cur.execute("select payload->'files'->>%s as key from artifacts where id=%s and type='UXSpec'", (name, aid))
    row = cur.fetchone()
    key = (row or {}).get("key")
    return key if key and key.startswith("design/") and ".." not in key else None


def make_handler(db, read_object, allowed_hosts: set[str]):
    """db(): 接続の文脈、read_object(key) -> bytes | None。操業コンソールから渡す（テストでは差し替える）"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):   # アクセスの記録はコンソールと同じく出さない
            pass

        def _plain(self, code: int, text: str):
            body = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            host = (self.headers.get("Host") or "").strip()
            if host.rsplit(":", 1)[0].strip("[]") not in allowed_hosts:
                return self._plain(403, "host not allowed")
            p = parse(self.path)
            if not p:
                return self._plain(404, "not found")
            aid, name, ext = p
            with db() as conn, conn.cursor() as cur:
                key = lookup(cur, aid, name)
            data = read_object(key) if key else None
            if data is None or len(data) > MAX_BYTES:
                return self._plain(404, "not found")
            self.send_response(200)
            self.send_header("Content-Type", TYPES[ext])
            for k, v in headers(f"http://{host}").items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            self._plain(405, "read only")

    return Handler


def serve(db, read_object, allowed_hosts: set[str], port: int = PORT) -> ThreadingHTTPServer:
    """別スレッドで配り始める。操業コンソールの起動から呼ぶ"""
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(db, read_object, allowed_hosts))
    threading.Thread(target=srv.serve_forever, daemon=True, name="prototype").start()
    return srv
