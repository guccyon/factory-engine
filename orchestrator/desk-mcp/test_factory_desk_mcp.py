"""窓口の MCP サーバー（ADR 0030 の順番 3）: JSON-RPC の受け答え、道具から API への写し、誤りの返し方。
TEST_DATABASE_URL があれば、本物の窓口の API（desk_api）を立てて stdio で 1 本通す。"""
import json
import os
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import factory_desk_mcp as m  # noqa: E402


class Rec:
    def __init__(self, reply=None, exc=None):
        self.calls, self.reply, self.exc = [], reply or {"ok": True}, exc

    def __call__(self, method, path, body=None, query=None):
        self.calls.append((method, path, body, query))
        if self.exc:
            raise self.exc
        return self.reply


def rpc(method, params=None, mid=1, call=None):
    return m.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}, call or Rec())


def test_initialize_and_list():
    r = rpc("initialize", {"protocolVersion": "2025-03-26"})["result"]
    assert r["protocolVersion"] == "2025-03-26" and "tools" in r["capabilities"] and r["serverInfo"]["name"] == "factory-desk"
    assert rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == m.PROTOCOLS[0]
    assert m.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = [t["name"] for t in rpc("tools/list")["result"]["tools"]]
    assert names == ["desk_open", "desk_reply", "desk_read", "desk_confirm", "desk_list", "desk_ticket", "desk_set_priority",
                     "inbox_list", "inbox_read", "inbox_answer", "desk_preferences", "desk_preference_set", "desk_preference_delete"]
    assert "inbox_answer" in r["instructions"] and "本人がはっきり選んだ" in r["instructions"]
    assert rpc("nope")["error"]["code"] == -32601


@pytest.mark.parametrize("name,args,want", [
    ("desk_open", {"kind": "request", "product": "o/r", "body": "通知を先に", "level": "p1"},
     ("POST", "/api/desk/conversations", {"kind": "request", "product": "o/r", "body": "通知を先に", "level": "p1"}, None)),
    ("desk_reply", {"conversation_id": 3, "body": "前日"}, ("POST", "/api/desk/conversations/3/messages", {"body": "前日"}, None)),
    ("desk_read", {"conversation_id": 3, "after": 9, "wait": 120}, ("GET", "/api/desk/conversations/3", None, {"after": 9, "wait": 60})),
    ("desk_confirm", {"conversation_id": 3, "draft_id": 42}, ("POST", "/api/desk/conversations/3/confirm", {"draft_id": 42}, None)),
    ("desk_list", {}, ("GET", "/api/desk/conversations", None, None)),
    ("desk_ticket", {"ticket": "o/r#812"}, ("GET", "/api/desk/tickets/o%2Fr%23812", None, None)),
    ("desk_set_priority", {"ticket": "o/r#812", "level": "p1", "reason": "先に"},
     ("POST", "/api/desk/tickets/o%2Fr%23812/priority", {"level": "p1", "reason": "先に"}, None)),
    ("inbox_list", {}, ("GET", "/api/desk/inbox", None, None)),
    ("inbox_list", {"include_answered": True}, ("GET", "/api/desk/inbox", None, {"all": 1})),
    ("inbox_read", {"key": "judgment:12"}, ("GET", "/api/desk/inbox/judgment%3A12", None, None)),
    ("inbox_read", {"key": "task:design_review:o/r#3"}, ("GET", "/api/desk/inbox/task%3Adesign_review%3Ao%2Fr%233", None, None)),
    ("inbox_answer", {"key": "judgment:12", "option": "change", "note": "2 文に", "raw": "2 文にして", "confirm": "yes"},
     ("POST", "/api/desk/inbox/judgment%3A12/answer", {"option": "change", "note": "2 文に", "raw": "2 文にして"}, None)),   # confirm は true だけ
    ("inbox_answer", {"key": "release:o/r@1.2.0", "option": "approve", "confirm": True},
     ("POST", "/api/desk/inbox/release%3Ao%2Fr%401.2.0/answer", {"option": "approve", "confirm": True}, None)),
    ("desk_preferences", {}, ("GET", "/api/desk/preferences", None, None)),
    ("desk_preferences", {"product": "o/r"}, ("GET", "/api/desk/preferences", None, {"product": "o/r"})),
    ("desk_preference_set", {"product": "o/r", "statement": "数字は表で", "work": ["desk.converse"]},
     ("POST", "/api/desk/preferences", {"product": "o/r", "statement": "数字は表で", "work": ["desk.converse"]}, None)),
    ("desk_preference_set", {"id": "P-0003", "statement": "数字は税抜きの表で"},
     ("POST", "/api/desk/preferences/P-0003", {"statement": "数字は税抜きの表で"}, None)),
    ("desk_preference_delete", {"id": "P-0003"}, ("POST", "/api/desk/preferences/P-0003/delete", {}, None)),
])
def test_tools_map_to_api(name, args, want):
    rec = Rec()
    r = rpc("tools/call", {"name": name, "arguments": args}, call=rec)["result"]
    assert r["isError"] is False and json.loads(r["content"][0]["text"]) == {"ok": True}
    assert rec.calls == [want]


def test_errors_are_tool_errors():
    r = rpc("tools/call", {"name": "desk_confirm", "arguments": {"conversation_id": 3, "draft_id": 1}},
            call=Rec(exc=m.DeskHTTPError(409, {"error": "最新の下書きではない"})))["result"]
    assert r["isError"] and json.loads(r["content"][0]["text"]) == {"status": 409, "error": "最新の下書きではない"}
    r = rpc("tools/call", {"name": "desk_reply", "arguments": {"conversation_id": 3}})["result"]
    assert r["isError"] and "引数" in r["content"][0]["text"]
    assert rpc("tools/call", {"name": "rm_rf", "arguments": {}})["error"]["code"] == -32602


def test_token_from_file(tmp_path, monkeypatch):
    p = tmp_path / "t"
    p.write_text("fdk_abc\n")
    monkeypatch.delenv("FACTORY_DESK_TOKEN", raising=False)
    monkeypatch.setenv("FACTORY_DESK_TOKEN_FILE", str(p))
    assert m.token() == "fdk_abc"
    monkeypatch.delenv("FACTORY_DESK_TOKEN_FILE")
    with pytest.raises(m.DeskHTTPError):
        m.token()


URL = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")
def test_stdio_end_to_end_against_desk_api(monkeypatch):
    """本物の窓口の API を立て、MCP のサーバーを子プロセスで動かし、相談を開いて読むところまで 1 本通す"""
    sys.path.insert(0, str(HERE.parent / "dispatcher"))
    monkeypatch.setenv("DATABASE_URL", URL)
    import psycopg
    from psycopg.rows import dict_row

    import desk
    import desk_api
    with psycopg.connect(URL, row_factory=dict_row, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute("truncate events, artifacts, jobs, tickets, desk_conversations, desk_tokens restart identity cascade")
        tok = desk.create_token(cur, "cs-1", "requester", ["guccyon/rally"], "test")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), desk_api.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = {**os.environ, "FACTORY_DESK_URL": f"http://127.0.0.1:{srv.server_address[1]}", "FACTORY_DESK_TOKEN": tok}
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "desk_open", "arguments": {
                "kind": "request", "product": "guccyon/rally", "body": "予約まわりの通知を先に進めて", "level": "p1"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "desk_read", "arguments": {"conversation_id": 1}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "desk_open", "arguments": {
                "kind": "request", "product": "someone/else", "body": "x"}}}]
    p = subprocess.run([sys.executable, str(HERE / "factory_desk_mcp.py")], input="\n".join(json.dumps(x) for x in msgs) + "\n",
                       capture_output=True, text=True, env=env, timeout=30)
    srv.shutdown()
    out = [json.loads(l) for l in p.stdout.splitlines()]
    assert [o["id"] for o in out] == [1, 2, 3, 4]
    opened = json.loads(out[1]["result"]["content"][0]["text"])
    assert opened["conversation"]["state"] == "open" and opened["conversation"]["wanted"] == {"level": "p1"}
    read = json.loads(out[2]["result"]["content"][0]["text"])
    assert read["messages"][-2]["body"] == "予約まわりの通知を先に進めて" and read["draft"] is None
    assert read["messages"][-1]["kind"] == "ack"   # 受付の即時の返事（ADR 0039）
    assert out[3]["result"]["isError"] and json.loads(out[3]["result"]["content"][0]["text"])["status"] == 403
