"""操業コンソールの問い合わせ（ADR 0059、#474）: 窓口の相談をコンソールから開く・読む・返事する・取り下げる。
依頼ごとに見える人を記録し、MCP・API から出した相談も同じ一覧に並ぶこと。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

URL = os.environ.get("TEST_DATABASE_URL")
REPO = "o/r"


@pytest.fixture()
def cur():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets, desk_conversations, desk_tokens restart identity cascade")
    yield c
    conn.close()


def test_open_read_reply_close(cur):
    import console_ask
    import desk
    op = console_ask.operator("builder")
    code, out = console_ask.post(cur, "/api/ask", {"kind": "request", "product": REPO, "body": "確認メールに住所を入れて\n迷う人がいる",
                                                   "visibility": "me"}, op)
    assert code == 200
    cid = out["conversation"]["id"]
    assert out["conversation"]["title"] == "確認メールに住所を入れて"
    assert out["conversation"]["wanted"]["visibility"] == "me"
    # MCP・API（依頼者のトークン）から出した相談も同じ一覧に並ぶ
    desk.open_conversation(cur, {"name": "cs-1", "role": "requester", "products": [REPO]}, "inquiry", REPO, "今週の進みは？")
    code, out = console_ask.get(cur, "/api/ask", op)
    assert code == 200 and {c["opened_by"] for c in out["conversations"]} == {"builder", "cs-1"}
    code, out = console_ask.get(cur, f"/api/ask/{cid}", op)
    assert code == 200 and out["status"]["step"] == "振り分け"
    assert [m["role"] for m in out["messages"] if m["event"] == "desk.message"] == ["requester", "factory"]
    code, _ = console_ask.post(cur, f"/api/ask/{cid}/messages", {"body": "急ぎではない"}, op)
    assert code == 200
    code, _ = console_ask.post(cur, f"/api/ask/{cid}/close", {"reason": "withdrawn"}, op)
    assert code == 200
    cur.execute("select state, close_reason from desk_conversations where id=%s", (cid,))
    assert cur.fetchone() == {"state": "closed", "close_reason": "withdrawn"}


def test_errors(cur):
    import console_ask
    op = console_ask.operator("builder")
    assert console_ask.post(cur, "/api/ask", {"product": REPO, "body": "x", "visibility": "team"}, op)[0] == 400
    assert console_ask.post(cur, "/api/ask", {"product": "not a repo", "body": "x"}, op)[0] == 400
    assert console_ask.get(cur, "/api/ask/999", op)[0] == 404
    assert console_ask.post(cur, "/api/ask/999/close", {}, op)[0] == 404
    assert console_ask.get(cur, "/api/other", op) is None and console_ask.post(cur, "/api/other", {}, op) is None
