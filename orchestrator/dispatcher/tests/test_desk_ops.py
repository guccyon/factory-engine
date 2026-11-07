"""単発の依頼のループを決まった操作（report.send）で 1 本通す（ADR 0039 の順番 2、ADR 0037 の決定 3・4）。

受付 → 振り分け（相談役の route）→ 確認（受信トレイの件・窓口の confirm）→ 実行（dispatcher が操作を 1 回）→ 提出 → 受取確認。
空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ宣言の食い違いだけ見る）。
"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("NOTIFIER", "log")
URL = os.environ.get("TEST_DATABASE_URL")
REPO = "guccyon/rally"
ROOT = Path(__file__).resolve().parents[3]
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

import operations  # noqa: E402

DOC = {
    "operated_by": "business_owner",
    "connections": {"p": {"party": "provider", "via": "log"}, "o": {"party": "business_owner", "via": "log"}},
    "destinations": {"provider_ops": {"connection": "p"}, "owner_ops": {"connection": "o"}},
}


def test_operations_match_definition():
    d = yaml.safe_load((ROOT / "definitions" / "operations.yaml").read_text())
    assert set(d["tiers"]) == set(operations.TIERS)
    assert set(d["operations"]) == set(operations.OPS)
    for k, v in d["operations"].items():
        mine = operations.OPS[k]
        assert {f: v[f] for f in ("tier", "allowed", "impl", "params")} == {f: mine[f] for f in ("tier", "allowed", "impl", "params")}
        assert v["tier"] in d["tiers"]


def test_desk_answers_match_inbox():
    import desk_line
    import inbox
    c, r = inbox.DESK_ANSWERS["desk_confirm"], inbox.DESK_ANSWERS["desk_receipt"]
    assert [o["id"] for o in c["options"]] == [o["id"] for o in desk_line.CONFIRM_OPTIONS]
    assert [o["id"] for o in r["options"] + r["extra"]] == [o["id"] for o in desk_line.RECEIPT_OPTIONS]


def test_check_params():
    dests = ["owner_ops", "provider_ops"]
    p, e = operations.check("report.send", {}, destinations=dests)
    assert e == [] and p == {"kind": "all", "date": "latest"}
    p, e = operations.check("report.send", {"kind": "report.product", "date": "2026-11-06", "to": "owner_ops"}, destinations=dests)
    assert e == [] and p == {"kind": "report.product", "date": "2026-11-06", "to": "owner_ops"}
    for bad, msg in (({"kind": "x"}, "どれか"), ({"date": "きのう"}, "YYYY-MM-DD"), ({"date": "2999-01-01"}, "YYYY-MM-DD"),
                     ({"to": "https://hooks.slack.com/x"}, "依頼の本文の宛先には送らない"), ({"channel": "#general"}, "宣言にない")):
        assert any(msg in x for x in operations.check("report.send", bad, destinations=dests)[1]), bad
    assert operations.check("line.scale", {}, destinations=dests)[1]
    assert operations.tier("report.send") == "send"


if URL:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import desk
    import desk_api
    import desk_line
    import inbox
    import notify


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets, desk_conversations, desk_tokens restart identity cascade")
    c.execute("insert into events(name, payload) values ('notify.config_changed', %s)", (Jsonb({"repo": REPO, "doc": DOC}),))
    yield c
    conn.close()


def call(method, path, token=None, body=None):
    try:
        return desk_api.route(method, path, {}, body or {}, token, connect=lambda: psycopg.connect(URL, row_factory=dict_row, autocommit=True))
    except desk.DeskError as e:
        return e.status, {"error": str(e)}


def consult_route(cur, cid, route):
    """相談役のジョブを積み、route の答えを返したことにする"""
    jid = desk_line.enqueue(cur, "tier-small", [REPO])[0]
    cur.execute("select payload from events where name='job.enqueued' and job_id=%s", (jid,))
    assert cur.fetchone()["payload"]["input"]["operations"][0]["id"] == "report.send"   # 振り分けの材料に操作の一覧
    cur.execute("update jobs set state='succeeded' where id=%s returning *", (jid,))
    desk_line.record(cur, cur.fetchone(), {"ok": True, "outcome": "route", "route": route, "confidence": 0.8, "cost_usd": 0.3}, "succeeded", None)


def state(cur, cid):
    cur.execute("select * from desk_conversations where id=%s", (cid,))
    return cur.fetchone()


def names(cur, cid):
    cur.execute("select name from events where correlation_id=%s order by id", (desk.corr(cid),))
    return [r["name"] for r in cur.fetchall()]


@needs_db
def test_report_send_from_request_to_acceptance(cur):
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    st, out = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "今朝の開発の状況のレポートを送り直して"})
    cid = out["conversation"]["id"]
    assert out["messages"][-1]["kind"] == "ack"                                         # 1. 受付: すぐ返す
    consult_route(cur, cid, {"path": "operate", "op": "report.send", "params": {"kind": "report.product", "to": "owner_ops"}, "why": "送り直すだけ"})
    c = state(cur, cid)                                                                 # 2. 振り分け → 3. 確認待ち（send の段）
    assert (c["state"], c["path"]) == ("awaiting_confirm", "operate") and float(c["cost_usd"]) == 0.3
    it = inbox.find(cur, f"task:desk_confirm:{cid}:1")
    assert it["who"] == "requester" and [o["id"] for o in it["answer"]["options"]] == ["run", "cancel"]
    cur.execute("select payload from events where correlation_id=%s and name='desk.routed'", (desk.corr(cid),))
    r = cur.fetchone()["payload"]
    assert r["params"] == {"kind": "report.product", "date": "latest", "to": "owner_ops"} and r["tier"] == "send"
    assert r["preview"]["to"] == ["owner_ops"]
    assert desk_line.run_operations(cur) == 0                                           # 確認の前には動かさない
    other = desk.create_token(cur, "cs-2", "requester", [REPO], "test")
    assert call("POST", f"/api/desk/conversations/{cid}/confirm", other, {"answer": "run"})[0] == 404   # ほかの依頼者は答えられない
    st, out = call("POST", f"/api/desk/conversations/{cid}/confirm", tok, {"answer": "run"})
    assert st == 200 and out["conversation"]["state"] == "running"
    assert inbox.find(cur, f"task:desk_confirm:{cid}:1") is None
    sent = []                                                                           # 4. 実行（dispatcher が 1 回だけ）
    poster = lambda d, t, *a: sent.append(d.name) or True  # noqa: E731
    assert desk_line.run_operations(cur, config=notify.parse(DOC), poster=poster) == 1
    assert desk_line.run_operations(cur, config=notify.parse(DOC), poster=poster) == 0
    assert sent == ["owner_ops"]
    c = state(cur, cid)                                                                 # 5. 提出 → 受取確認待ち
    assert c["state"] == "delivered"
    assert inbox.find(cur, f"task:desk_receipt:{cid}:1")["answer"]["options"][0]["id"] == "accept"
    assert call("POST", f"/api/desk/conversations/{cid}/messages", tok, {"answer": "return"})[0] == 400   # 差し戻しは一言が要る
    st, out = call("POST", f"/api/desk/conversations/{cid}/messages", tok, {"answer": "accept"})
    assert st == 200
    c = state(cur, cid)
    assert (c["state"], c["close_reason"], c["closed_by"]) == ("accepted", "accepted", "cs-1")
    ns = names(cur, cid)
    for a, b in (("desk.routed", "desk.confirm_requested"), ("desk.confirm_requested", "desk.confirmed_run"), ("desk.confirmed_run", "op.executed"),
                 ("op.executed", "desk.delivered"), ("desk.delivered", "desk.received"), ("desk.received", "desk.closed")):
        assert ns.index(a) < ns.index(b), (a, b)
    cur.execute("select payload from events where correlation_id=%s and name='desk.closed'", (desk.corr(cid),))
    rec = cur.fetchone()["payload"]["record"]
    assert rec["path"] == "operate" and rec["outcome"] == "accepted" and rec["sec_delivered"] is not None


@needs_db
def test_cancel_at_confirmation_withdraws(cur):
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    cid = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "レポートを送り直して"})[1]["conversation"]["id"]
    consult_route(cur, cid, {"path": "operate", "op": "report.send", "params": {}, "why": ""})
    # 受信トレイ（運用主の操業コンソールなど）から答えても同じ口を通る
    assert inbox.answer(cur, f"task:desk_confirm:{cid}:1", "cancel", "owner", "console")[0] == "answered"
    c = state(cur, cid)
    assert (c["state"], c["close_reason"]) == ("closed", "withdrawn")
    assert "op.rejected" in names(cur, cid) and desk_line.run_operations(cur) == 0


@needs_db
def test_follow_up_goes_back_to_routing(cur):
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    cid = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "レポートを送り直して"})[1]["conversation"]["id"]
    consult_route(cur, cid, {"path": "operate", "op": "report.send", "params": {"to": "owner_ops"}, "why": ""})
    call("POST", f"/api/desk/conversations/{cid}/confirm", tok, {"answer": "run"})
    desk_line.run_operations(cur, config=notify.parse(DOC), poster=lambda *a: True)
    st, out = call("POST", f"/api/desk/conversations/{cid}/messages", tok, {"answer": "more", "body": "提供元の宛先にも送って"})
    assert st == 200
    c = state(cur, cid)
    assert (c["state"], c["follow_ups"]) == ("open", 1)
    assert [x["id"] for x in desk_line.pending(cur)] == [cid]                          # 相談役に回し直す（振り分けから）
    consult_route(cur, cid, {"path": "operate", "op": "report.send", "params": {"to": "provider_ops"}, "why": ""})
    assert state(cur, cid)["state"] == "awaiting_confirm" and inbox.find(cur, f"task:desk_confirm:{cid}:2")
    assert call("POST", f"/api/desk/conversations/{cid}/confirm", tok, {"answer": "run"})[0] == 200


@needs_db
def test_invalid_route_asks_requester(cur):
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    cid = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "#general に送って"})[1]["conversation"]["id"]
    consult_route(cur, cid, {"path": "operate", "op": "report.send", "params": {"to": "#general"}, "why": ""})
    c = state(cur, cid)
    assert c["state"] == "waiting_requester" and "desk.routed" not in names(cur, cid)
