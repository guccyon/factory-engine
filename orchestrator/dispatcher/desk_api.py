#!/usr/bin/env python3
"""事業部の窓口の API（ADR 0030 の順番 1・2）。工場の外の人（とその代理の Claude）が依頼と問い合わせを持ち込む口。

操業コンソール（console.py）とは別のプロセスで、受けるのは相談と、依頼者に許した範囲のチケットの操作だけ（止める・承認する・流量を変えるはできない）。
API（JSON）が正本で、MCP のサーバー（orchestrator/desk-mcp/factory_desk_mcp.py）はこれを呼ぶだけの包み。

認証: Authorization: Bearer <トークン>（factoryctl desk token create で発行。役割と対象の製品を持つ）
  GET  /api/desk/me                                    トークンの名前・役割・対象の製品
  POST /api/desk/conversations                         {"kind": "request"|"inquiry", "product": "owner/repo", "body", "level"?: "p0".."p3", "due"?}
                                                       相談を開く。返事は会話を読んで取る（相談のジョブは数分かかるので非同期）
  GET  /api/desk/conversations                         自分の相談の一覧（運用主はすべて）
  GET  /api/desk/conversations/<id>?after=<id>&wait=<秒>  会話と、after より後の発言。wait（最大 60）を渡すと、新しい発言が来るまで待って返す
  POST /api/desk/conversations/<id>/messages           {"body"} 返事・言い足し。提出の後は {"answer": "accept"|"more"|"return", "body"?: 一言} で受取確認
                                                       （ADR 0039 の決定 5。more・return は一言が要る）
  POST /api/desk/conversations/<id>/confirm            {"draft_id", "level"?: "p0".."p3"} 下書きを確認して投入する（依頼者の確認 1 回。ADR 0030 の 3）。
                                                       チケットは dispatcher が次の周回で起票し、会話に「チケットを作った」の発言が付く。
                                                       決まった操作に振り分けた依頼は {"answer": "run"|"cancel", "note"?} で、動かしてよいかに答える（ADR 0039）
  POST /api/desk/conversations/<id>/close              {"reason"?}
  GET  /api/desk/tickets/<id か owner/repo#123>         チケットの状態（段・優先度・順番・作業中のジョブ・PR）
  POST /api/desk/tickets/<id か ref>/priority          {"level" か "priority", "reason"?, "conversation_id"?} 明確な指示（「#812 を先に」）は壁打ちを通さずここで即時に通す
  POST /api/desk/tickets/<id か ref>/hold | resume     {"reason"?}
  （参照の # は URL では %23 にする）

守り:
- 既定は Mac の中だけで開く（compose の ports を 127.0.0.1 に）。開いてよいホスト名だけを受ける（DNS rebinding）
- ブラウザからは使わせない（Origin の付いた要求は断る。トークンはヘッダーで渡すので CSRF は起きないが、念のため）
- 依頼の本文はデータとして記録するだけで、ここではシェルにもプロンプトにも渡さない
"""
from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import psycopg
from psycopg.rows import dict_row

import desk

ENV = os.environ
PORT = int(ENV.get("DESK_PORT", "8792"))
ALLOWED_HOSTS = {h.strip() for h in ENV.get("DESK_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()}
MAX_WAIT_SEC = 60
POLL_SEC = 1.0
MAX_REQUEST_BYTES = 64 * 1024


def db():
    return psycopg.connect(ENV["DATABASE_URL"], row_factory=dict_row, autocommit=True)


def _int(v, name: str, default: int = 0) -> int:
    if v in (None, ""):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        raise desk.DeskError(400, f"{name} は整数")


def route(method: str, path: str, query: dict, body: dict, token: str | None, connect=db) -> tuple[int, dict]:
    """要求を 1 つ処理して (状態コード, JSON)。HTTP から切り離してテストできるようにしてある"""
    parts = [unquote(p) for p in path.strip("/").split("/")]
    if parts[:2] != ["api", "desk"]:
        return 404, {"error": "not found"}
    rest = parts[2:]
    with connect() as conn:
        cur = conn.cursor()
        who = desk.authenticate(cur, token)
        if not who:
            return 401, {"error": "トークンがない・取り消された"}
        if method == "GET" and rest == ["me"]:
            return 200, {"name": who["name"], "role": who["role"], "products": who["products"]}
        if rest[:1] == ["conversations"]:
            if method == "POST" and len(rest) == 1:
                c = desk.open_conversation(cur, who, body.get("kind"), body.get("product"), body.get("body"),
                                           level=body.get("level"), due=body.get("due"))
                return 201, desk.read_conversation(cur, who, c["id"])
            if method == "GET" and len(rest) == 1:
                return 200, {"conversations": desk.list_conversations(cur, who)}
            cid = _int(rest[1], "相談の id") if len(rest) > 1 else None
            if method == "GET" and len(rest) == 2:
                after = _int(query.get("after"), "after")
                wait = min(max(_int(query.get("wait"), "wait"), 0), MAX_WAIT_SEC)
                deadline = time.monotonic() + wait
                while True:
                    out = desk.read_conversation(cur, who, cid, after)
                    if out["messages"] or time.monotonic() >= deadline:
                        return 200, out
                    time.sleep(POLL_SEC)
            if method == "POST" and len(rest) == 3 and rest[2] == "messages":
                if body.get("answer") is not None:   # 提出の受取確認（ADR 0039 の決定 5）。body は一言
                    desk.receive(cur, who, cid, body.get("answer"), body.get("body") or "")
                else:
                    desk.post_message(cur, who, cid, body.get("body"))
                return 200, desk.read_conversation(cur, who, cid, _int(query.get("after"), "after"))
            if method == "POST" and len(rest) == 3 and rest[2] == "confirm":
                if body.get("answer") is not None:   # 振り分けの確認（ADR 0039 の決定 2。決まった操作を動かしてよいか）
                    desk.confirm_route(cur, who, cid, body.get("answer"), str(body.get("note") or "")[:500])
                else:
                    desk.confirm_draft(cur, who, cid, _int(body.get("draft_id"), "draft_id", -1), level=body.get("level"))
                return 200, desk.read_conversation(cur, who, cid, _int(query.get("after"), "after"))
            if method == "POST" and len(rest) == 3 and rest[2] == "close":
                return 200, {"conversation": desk.view_conversation(desk.close_conversation(cur, who, cid, str(body.get("reason") or "")[:500]))}
        if rest[:1] == ["tickets"] and len(rest) >= 2:
            key = rest[1]
            if method == "GET" and len(rest) == 2:
                return 200, desk.ticket_status(cur, who, key)
            reason = str(body.get("reason") or "")[:500]
            if method == "POST" and len(rest) == 3 and rest[2] == "priority":
                cid = body.get("conversation_id")
                return 200, desk.set_priority(cur, who, key, level=body.get("level"), priority=body.get("priority"), reason=reason,
                                              conversation_id=_int(cid, "conversation_id") if cid is not None else None)
            if method == "POST" and len(rest) == 3 and rest[2] in ("hold", "resume"):
                return 200, desk.set_held(cur, who, key, rest[2] == "hold", reason)
    return 404, {"error": "not found"}


class Handler(BaseHTTPRequestHandler):
    server_version = "factory-desk"

    def _send(self, status: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ALLOWED_HOSTS:
            return self._send(421, {"error": "このホスト名では受けない"})
        if self.headers.get("Origin"):
            return self._send(403, {"error": "ブラウザからは使わない（API と MCP から）"})
        u = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(u.query).items()}
        body: dict = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_REQUEST_BYTES:
                return self._send(413, {"error": "要求が大きすぎる"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "JSON ではない"})
            if not isinstance(body, dict):
                return self._send(400, {"error": "JSON のオブジェクトを送る"})
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        try:
            status, obj = route(method, u.path, query, body, token)
        except desk.DeskError as e:
            status, obj = e.status, {"error": str(e)}
        except Exception as e:  # noqa: BLE001  落とさずに返し、ログに残す
            sys.stderr.write(f"[desk] {method} {u.path} failed: {e!r}\n")
            status, obj = 500, {"error": "窓口の中で失敗した"}
        self._send(status, obj)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, fmt, *a):
        sys.stdout.write(f"[desk] {self.command} {urlsplit(self.path).path} {a[1] if len(a) > 1 else ''}\n")


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"desk up: port={PORT} allowed_hosts={sorted(ALLOWED_HOSTS)}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
