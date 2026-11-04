"""事業部の窓口（ADR 0030 の順番 1）: トークンと役割、会話の記録、明確な指示（優先度・保留・状態）。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    import psycopg
    from psycopg.rows import dict_row

    import desk
    import desk_api
    import ops


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets, desk_conversations, desk_tokens restart identity cascade")
    yield c
    conn.close()


def call(method, path, token=None, body=None, query=None):
    try:
        return desk_api.route(method, path, query or {}, body or {}, token, connect=lambda: psycopg.connect(URL, row_factory=dict_row, autocommit=True))
    except desk.DeskError as e:
        return e.status, {"error": str(e)}


def queued(cur, ref, priority=None):
    repo, n = ref.split("#")
    t = ops.upsert_ticket(cur, "github", ref, repo, title="T" + n, priority=priority)
    cur.execute("insert into jobs(correlation_id, repo, issue_number, ticket_id, priority) values (%s,%s,%s,%s,%s) returning id",
                (f"github:{ref}", repo, int(n), t["id"], t["priority"]))
    return t


def test_token_is_shown_once_and_only_hash_is_kept(cur):
    tok = desk.create_token(cur, "owner", "operator", [], "test")
    assert tok.startswith("fdk_")
    cur.execute("select token_hash from desk_tokens")
    assert cur.fetchone()["token_hash"] != tok
    cur.execute("select payload from events where name='desk.token_issued'")
    assert tok not in str(cur.fetchone()["payload"])
    assert desk.authenticate(cur, tok)["role"] == "operator"
    assert desk.authenticate(cur, "fdk_wrong") is None
    assert desk.revoke_token(cur, "owner", "test")
    assert desk.authenticate(cur, tok) is None
    with pytest.raises(desk.DeskError):
        desk.create_token(cur, "cs-1", "requester", [], "test")   # 依頼者には製品が要る


def test_unauthenticated_is_refused(cur):
    assert call("GET", "/api/desk/me")[0] == 401
    assert call("GET", "/api/desk/me", "fdk_nope")[0] == 401


def test_conversation_round_trip(cur):
    tok = desk.create_token(cur, "cs-1", "requester", ["guccyon/rally"], "test")
    st, out = call("POST", "/api/desk/conversations", tok,
                   {"kind": "request", "product": "guccyon/rally", "body": "予約まわりの通知を先に進めて\n理由: 苦情が多い", "level": "p1"})
    assert st == 201
    c = out["conversation"]
    assert (c["kind"], c["state"], c["opened_by"], c["title"], c["wanted"]) == ("request", "open", "cs-1", "予約まわりの通知を先に進めて", {"level": "p1"})
    assert [m["event"] for m in out["messages"]] == ["desk.opened", "desk.message"]
    last = out["last_id"]

    # 相手の問い（順番 2 で入るプランナーの相談役の代わりに、工場の側から直接書く）→ 依頼者の返事で往復が 1 つ進む
    desk.factory_message(cur, c["id"], "planner", "どの通知ですか？（予約の確定・前日・キャンセル）", kind="question", state="waiting_requester")
    st, out = call("GET", f"/api/desk/conversations/{c['id']}", tok, query={"after": str(last)})
    assert [m["role"] for m in out["messages"]] == ["planner"] and out["conversation"]["state"] == "waiting_requester"
    st, out = call("POST", f"/api/desk/conversations/{c['id']}/messages", tok, {"body": "前日の通知"}, query={"after": str(out["last_id"])})
    assert st == 200 and out["conversation"]["rounds"] == 1 and out["conversation"]["state"] == "open"
    assert [m["body"] for m in out["messages"]] == ["前日の通知"]

    st, out = call("POST", f"/api/desk/conversations/{c['id']}/close", tok, {"reason": "済み"})
    assert out["conversation"]["state"] == "closed"
    assert call("POST", f"/api/desk/conversations/{c['id']}/messages", tok, {"body": "まだある"})[0] == 409
    cur.execute("select count(*) as n from events where correlation_id=%s", (f"desk:{c['id']}",))
    assert cur.fetchone()["n"] == 5


def test_wait_returns_when_nothing_new(cur, monkeypatch):
    tok = desk.create_token(cur, "owner", "operator", [], "test")
    st, out = call("POST", "/api/desk/conversations", tok, {"kind": "inquiry", "product": "guccyon/rally", "body": "今週どこまで進んだ？"})
    monkeypatch.setattr(desk_api, "POLL_SEC", 0.01)
    st, again = call("GET", f"/api/desk/conversations/{out['conversation']['id']}", tok, query={"after": str(out["last_id"]), "wait": "1"})
    assert st == 200 and again["messages"] == [] and again["last_id"] == out["last_id"]


def test_scope_of_products_and_conversations(cur):
    a = desk.create_token(cur, "cs-1", "requester", ["guccyon/rally"], "test")
    b = desk.create_token(cur, "cs-2", "requester", ["guccyon/rally"], "test")
    op = desk.create_token(cur, "owner", "operator", [], "test")
    assert call("POST", "/api/desk/conversations", a, {"kind": "request", "product": "other/app", "body": "x"})[0] == 403
    assert call("POST", "/api/desk/conversations", a, {"kind": "deploy", "product": "guccyon/rally", "body": "x"})[0] == 400
    assert call("POST", "/api/desk/conversations", a, {"kind": "request", "product": "guccyon/rally", "body": " "})[0] == 400
    st, out = call("POST", "/api/desk/conversations", a, {"kind": "request", "product": "guccyon/rally", "body": "x"})
    cid = out["conversation"]["id"]
    assert call("GET", f"/api/desk/conversations/{cid}", b)[0] == 404        # ほかの依頼者の相談は見えない（あることも漏らさない）
    assert call("GET", f"/api/desk/conversations/{cid}", op)[0] == 200       # 運用主は見える
    assert len(call("GET", "/api/desk/conversations", b)[1]["conversations"]) == 0
    assert len(call("GET", "/api/desk/conversations", op)[1]["conversations"]) == 1


def test_explicit_priority_goes_through_ops(cur):
    queued(cur, "guccyon/rally#1")
    t2 = queued(cur, "guccyon/rally#2")
    op = desk.create_token(cur, "owner", "operator", ["guccyon/rally"], "test")
    st, out = call("GET", "/api/desk/tickets/guccyon/rally#2", op)
    assert st == 404   # 参照の / はパスの区切りになるので %23 と %2F で渡す
    st, out = call("GET", "/api/desk/tickets/guccyon%2Frally%232", op)
    assert st == 200 and (out["stage"], out["rank"], out["level"]) == ("queued", 2, "p2")
    st, out = call("POST", "/api/desk/tickets/guccyon%2Frally%232/priority", op, {"level": "p1", "reason": "先にこれを"})
    assert st == 200 and (out["level"], out["rank"], out["priority_set_by"]) == ("p1", 1, "desk:owner")
    cur.execute("select priority from jobs where ticket_id=%s", (t2["id"],))
    assert cur.fetchone()["priority"] == 75
    cur.execute("select name, payload from events where name in ('ticket.priority_set','desk.priority_changed') order by id")
    rows = cur.fetchall()
    assert [r["name"] for r in rows] == ["ticket.priority_set", "desk.priority_changed"]
    assert rows[1]["payload"]["reason"] == "先にこれを" and rows[1]["payload"]["by"] == "owner"
    # 入口のラベルは依頼者の指定を上書きしない
    ops.upsert_ticket(cur, "github", "guccyon/rally#2", "guccyon/rally", priority=25)
    assert ops.get_ticket(cur, t2["id"])["priority"] == 75
    st, out = call("POST", f"/api/desk/tickets/{t2['id']}/hold", op, {"reason": "待って"})
    assert st == 200 and out["held"] and out["rank"] is None
    assert call("POST", "/api/desk/tickets/guccyon%2Frally%232/priority", op, {"level": "p9"})[0] == 400


def test_requester_changes_only_own_tickets(cur):
    a = desk.create_token(cur, "cs-1", "requester", ["guccyon/rally"], "test")
    other = queued(cur, "guccyon/rally#3")
    assert call("GET", f"/api/desk/tickets/{other['id']}", a)[0] == 200        # 状態は読める
    assert call("POST", f"/api/desk/tickets/{other['id']}/priority", a, {"level": "p0"})[0] == 403
    st, out = call("POST", "/api/desk/conversations", a, {"kind": "request", "product": "guccyon/rally", "body": "CSV がほしい"})
    cid = out["conversation"]["id"]
    mine = ops.upsert_ticket(cur, "desk", f"desk-{cid}-1", "guccyon/rally", title="CSV を出す")
    cur.execute("update tickets set desk_conversation_id=%s where id=%s", (cid, mine["id"]))
    st, out = call("POST", f"/api/desk/tickets/{mine['id']}/priority", a, {"level": "p1", "conversation_id": cid})
    assert st == 200 and out["level"] == "p1" and out["conversation_id"] == cid
    st, out = call("GET", f"/api/desk/conversations/{cid}", a)
    assert out["messages"][-1]["event"] == "desk.priority_changed" and [t["id"] for t in out["tickets"]] == [mine["id"]]
    elsewhere = ops.upsert_ticket(cur, "github", "other/app#1", "other/app")
    assert call("GET", f"/api/desk/tickets/{elsewhere['id']}", a)[0] == 404   # 対象外の製品は見えない
