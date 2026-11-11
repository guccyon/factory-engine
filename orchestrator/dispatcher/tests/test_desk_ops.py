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
    d = inbox.DESK_ANSWERS["desk_decline"]
    assert [o["id"] for o in d["options"] + d["extra"]] == [o["id"] for o in desk_line.DECLINE_OPTIONS]


def test_loop_values_match_definition():
    import desk
    d = yaml.safe_load((ROOT / "definitions" / "desk.yaml").read_text())
    assert d["loop"] == desk.LOOP


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
    c = state(cur, cid)                                                                 # 2. 振り分け → 3. 確認待ち（send のレベル）
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


def consult_decline(cur, cid, decline, why="本番の売上は工場から見られない"):
    jid = desk_line.enqueue(cur, "tier-small", [REPO])[0]
    cur.execute("update jobs set state='succeeded' where id=%s returning *", (jid,))
    desk_line.record(cur, cur.fetchone(), {"ok": True, "outcome": "decline", "decline": {**decline, "why": why}, "cost_usd": 0.1},
                     "succeeded", None)


@needs_db
def test_decline_is_delivered_and_requester_agrees(cur):
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    cid = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "先月の本番の売上を出して"})[1]["conversation"]["id"]
    consult_decline(cur, cid, {"reason": "whatever", "alternatives": []})
    c = state(cur, cid)
    assert c["state"] == "delivered" and c["path"] is None                              # 断りも提出（黙って閉じない）
    cur.execute("select payload from events where correlation_id=%s and name='desk.declined'", (desk.corr(cid),))
    d = cur.fetchone()["payload"]
    assert d["reason"] == "no_capability" and d["alternatives"][0]["kind"] == "to_human"  # 知らない理由は決まった語に。代わりの案は必ず 1 つ
    it = inbox.find(cur, f"task:desk_decline:{cid}:1")
    assert [o["id"] for o in it["answer"]["options"]] == ["accept"]
    assert call("POST", f"/api/desk/conversations/{cid}/messages", tok, {"answer": "return"})[0] == 400   # 差し戻しは理由が要る
    st, out = call("POST", f"/api/desk/conversations/{cid}/messages", tok, {"answer": "accept"})
    assert st == 200
    c = state(cur, cid)
    assert (c["state"], c["close_reason"], c["closed_by"]) == ("closed", "declined", "cs-1")      # 完了にするのは依頼者
    cur.execute("select payload from events where correlation_id=%s and name='desk.closed'", (desk.corr(cid),))
    assert cur.fetchone()["payload"]["record"]["declined"] == "no_capability"


@needs_db
def test_decline_alternative_opens_linked_request(cur):
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    cid = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "先月の本番の売上を出して"})[1]["conversation"]["id"]
    consult_decline(cur, cid, {"reason": "needs_production_data",
                               "alternatives": [{"kind": "other_path", "text": "staging の予約数を出す"}, {"kind": "add_capability", "text": "売上の集計を足す"}]})
    st, out = call("POST", f"/api/desk/conversations/{cid}/messages", tok, {"answer": "more", "body": "1 で"})   # 一言は任意
    assert st == 200
    old = state(cur, cid)
    assert (old["state"], old["close_reason"]) == ("closed", "superseded") and old["superseded_by"]
    new = state(cur, old["superseded_by"])
    assert new["derived_from"] == cid and new["opened_by"] == "cs-1" and new["state"] == "open"
    cur.execute("select payload from events where correlation_id=%s and name='desk.message' and payload->>'role'='requester'", (desk.corr(new["id"]),))
    body = cur.fetchone()["payload"]["body"]
    assert "staging の予約数を出す" in body and "1 で" in body
    assert [x["id"] for x in desk_line.pending(cur)] == [new["id"]]                       # 別案は新しい依頼として振り分けへ


@needs_db
def test_decline_can_be_returned(cur):
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    cid = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "レポートを送って"})[1]["conversation"]["id"]
    consult_decline(cur, cid, {"reason": "no_operation", "alternatives": [{"kind": "to_human", "text": "運用主に聞く"}]})
    assert inbox.answer(cur, f"task:desk_decline:{cid}:1", "return", "owner", "console", note="report.send で送れるはず")[0] == "answered"
    c = state(cur, cid)
    assert c["state"] == "open" and [x["id"] for x in desk_line.pending(cur)] == [cid]   # 同じ会話で振り分けから
    consult_route(cur, cid, {"path": "operate", "op": "report.send", "params": {}, "why": ""})
    assert state(cur, cid)["state"] == "awaiting_confirm" and inbox.find(cur, f"task:desk_confirm:{cid}:2")


@needs_db
def test_watch_writes_progress_before_target_and_late_after(cur):
    from datetime import timedelta
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    out = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "レポートを送り直して"})[1]
    cid = out["conversation"]["id"]
    assert out["messages"][-1]["route_by"] and "ごろまで" in out["messages"][-1]["body"]    # 受付で振り分けの目安の時刻を言う
    t0 = state(cur, cid)["created_at"]
    assert desk_line.watch(cur, t0 + timedelta(seconds=60)) == 0
    assert desk_line.watch(cur, t0 + timedelta(seconds=250)) == 1                         # 8 割を過ぎたら超える前に知らせる
    assert desk_line.watch(cur, t0 + timedelta(seconds=260)) == 0                         # 1 つのマイルストーンに 1 回
    cur.execute("select payload from events where correlation_id=%s and name='desk.message' and payload->>'kind'='progress'", (desk.corr(cid),))
    assert "相談役のジョブを積むところ" in cur.fetchone()["payload"]["body"]
    assert desk_line.watch(cur, t0 + timedelta(seconds=301)) == 1                         # 目安を過ぎたら desk.late
    assert "desk.late" in names(cur, cid)
    # 振り分けたらマイルストーンは閉じる。確認から提出までの目安（operate は 5 分）を同じく見る
    consult_route(cur, cid, {"path": "operate", "op": "report.send", "params": {}, "why": ""})
    call("POST", f"/api/desk/conversations/{cid}/confirm", tok, {"answer": "run"})
    cur.execute("select occurred_at from events where correlation_id=%s and name='desk.confirmed_run'", (desk.corr(cid),))
    t1 = cur.fetchone()["occurred_at"]
    assert desk_line.watch(cur, t1 + timedelta(seconds=100)) == 0
    assert desk_line.watch(cur, t1 + timedelta(seconds=400)) == 2                         # 途中の様子と遅れ
    desk_line.run_operations(cur, config=notify.parse(DOC), poster=lambda *a: True)
    call("POST", f"/api/desk/conversations/{cid}/messages", tok, {"answer": "accept"})
    cur.execute("select payload from events where correlation_id=%s and name='desk.closed'", (desk.corr(cid),))
    rec = cur.fetchone()["payload"]["record"]
    assert rec["late"] == ["deliver", "route"] and rec["met_ack"] is True
    assert rec["met_route"] is True and rec["met_deliver"] is True                     # 実際の時刻では目安の内（遅れの記録は watch の時計）


@needs_db
def test_cap_hit_delivers_partial_with_choices(cur):
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    cid = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "集計して"})[1]["conversation"]["id"]
    desk.advance(cur, cid, "routed", "desk.routed", {"round": 1, "path": "quick"}, path="quick")
    desk.advance(cur, cid, "running", "desk.confirmed_run", {"round": 1})
    desk_line.deliver(cur, cid, 1, {"ok": False, "summary": "3 か月のうち 2 か月分まで集計しました。", "cap_hit": "費用 $2", "remaining": "残りの 1 か月"})
    c = state(cur, cid)
    assert c["state"] == "delivered" and "desk.cap_hit" in names(cur, cid)
    cur.execute("select payload from events where correlation_id=%s and name='desk.message' and payload->>'kind'='delivered'", (desk.corr(cid),))
    body = cur.fetchone()["payload"]["body"]
    assert "上限（費用 $2）" in body and "残りの 1 か月" in body and "analysis" in body
    assert inbox.find(cur, f"task:desk_receipt:{cid}:1")                                # 受取確認を取る（選ぶまで完了にしない）


@needs_db
def test_requester_sees_own_desk_items_in_inbox(cur):
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    other = desk.create_token(cur, "cs-2", "requester", [REPO], "test")
    cid = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "レポートを送り直して"})[1]["conversation"]["id"]
    consult_route(cur, cid, {"path": "operate", "op": "report.send", "params": {}, "why": ""})
    st, out = call("GET", "/api/desk/inbox", tok)
    assert st == 200 and [i["key"] for i in out["items"]] == [f"task:desk_confirm:{cid}:1"] and out["items"][0]["product"] == REPO
    assert call("GET", "/api/desk/inbox", other)[1]["items"] == []                     # ほかの依頼者の件は見せない
    assert call("GET", f"/api/desk/inbox/task:desk_confirm:{cid}:1", other)[0] == 404
    st, out = call("POST", f"/api/desk/inbox/task:desk_confirm:{cid}:1/answer", tok, {"option": "run"})
    assert st == 200 and state(cur, cid)["state"] == "running"


@needs_db
def test_remind_then_operator_and_never_close(cur):
    from datetime import timedelta
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    cid = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "レポートを送り直して"})[1]["conversation"]["id"]
    consult_route(cur, cid, {"path": "operate", "op": "report.send", "params": {}, "why": ""})
    call("POST", f"/api/desk/conversations/{cid}/confirm", tok, {"answer": "run"})
    desk_line.run_operations(cur, config=notify.parse(DOC), poster=lambda *a: True)
    cur.execute("select occurred_at from events where correlation_id=%s and name='desk.delivered'", (desk.corr(cid),))
    t = cur.fetchone()["occurred_at"]
    assert desk_line.remind(cur, t + timedelta(days=2)) == 0
    assert desk_line.remind(cur, t + timedelta(days=3, minutes=1)) == 1                # 3 日で知らせ直す
    assert desk_line.remind(cur, t + timedelta(days=4)) == 0
    assert desk_line.remind(cur, t + timedelta(days=11)) == 2                          # 7 日の知らせ直しと、運用主へ
    it = inbox.find(cur, f"task:desk_waiting:{cid}:1")
    assert it["who"] == "owner" and it["answer"]["type"] == "ack"
    op = desk.create_token(cur, "ops", "operator", [REPO], "test")
    assert [i["key"] for i in call("GET", "/api/desk/inbox", op)[1]["items"]] == [f"task:desk_receipt:{cid}:1", f"task:desk_waiting:{cid}:1"]
    assert [i["key"] for i in call("GET", "/api/desk/inbox", tok)[1]["items"]] == [f"task:desk_receipt:{cid}:1"]   # 依頼者には運用主の件を見せない
    assert state(cur, cid)["state"] == "delivered"                                      # 勝手に完了にしない
    st, out = call("GET", f"/api/desk/conversations/{cid}", tok)
    assert out["status"]["step"] == "提出" and [o["id"] for o in out["status"]["answer"]["options"]] == ["accept", "more", "return"]
    call("POST", f"/api/desk/conversations/{cid}/messages", tok, {"answer": "accept"})
    assert inbox.find(cur, f"task:desk_waiting:{cid}:1") is None                        # 依頼者が答えたら運用主の件も消える


@needs_db
def test_withdraw_resolves_inbox_items(cur):
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    cid = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "レポートを送り直して"})[1]["conversation"]["id"]
    consult_route(cur, cid, {"path": "operate", "op": "report.send", "params": {}, "why": ""})
    assert inbox.find(cur, f"task:desk_confirm:{cid}:1")
    call("POST", f"/api/desk/conversations/{cid}/close", tok, {"reason": "要らなくなった"})
    assert inbox.find(cur, f"task:desk_confirm:{cid}:1") is None
    st, out = call("GET", f"/api/desk/conversations/{cid}", tok)
    assert out["status"]["step"] == "終わり" and out["status"]["closed"]["reason"] == "withdrawn"


@needs_db
def test_status_shows_decline_and_route(cur):
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    cid = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "本番の売上"})[1]["conversation"]["id"]
    st, out = call("GET", f"/api/desk/conversations/{cid}", tok)
    assert out["status"]["step"] == "振り分け" and out["status"]["by"]                    # いつ頃か
    consult_decline(cur, cid, {"reason": "needs_production_data", "alternatives": [{"kind": "other_path", "text": "staging で出す"}]})
    s = call("GET", f"/api/desk/conversations/{cid}", tok)[1]["status"]
    assert s["declined"]["reason_ja"].startswith("本番のデータ") and s["declined"]["alternatives"][0]["text"] == "staging で出す"
    assert [o["label"] for o in s["answer"]["options"]] == ["納得した", "別案で進める", "断りを差し戻す"] and s["route"]["declined"]


DOC_SLACK = {**DOC, "destinations": {**DOC["destinations"], "owner_ops": {"connection": "o", "mentions": {"cs-1": "<@U1>"}}}}


@needs_db
def test_notice_goes_to_default_destination_with_mention(cur):
    cur.execute("insert into events(name, payload) values ('notify.config_changed', %s)", (Jsonb({"repo": REPO, "doc": DOC_SLACK}),))
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    cid = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "本番の売上"})[1]["conversation"]["id"]
    consult_decline(cur, cid, {"reason": "needs_production_data", "alternatives": [{"kind": "to_human", "text": "運用主に聞く"}]})
    got = []
    cfg = notify.parse(DOC_SLACK)
    assert desk_line.send_notices(cur, config=cfg, poster=lambda d, t: got.append((d.name, t)) or True) == 1
    assert got[0][0] == "owner_ops" and got[0][1].startswith("<@U1> 窓口の依頼") and "受けられません" in got[0][1]   # 事業主の宛先（既定）
    assert desk_line.send_notices(cur, config=cfg, poster=lambda d, t: got.append(d.name) or True) == 0           # 1 回だけ
    assert "desk.notified" in names(cur, cid)


@needs_db
def test_notify_field_chooses_destination_or_desk_only(cur):
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    assert call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "x", "notify": ["https://hooks.slack.com/x"]})[0] == 400
    cid = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "本番の売上", "notify": ["provider_ops"]})[1]["conversation"]["id"]
    assert state(cur, cid)["wanted"]["notify"] == ["provider_ops"]
    consult_decline(cur, cid, {"reason": "needs_production_data", "alternatives": []})
    got = []
    desk_line.send_notices(cur, config=notify.parse(DOC), poster=lambda d, t: got.append(d.name) or True)
    assert got == ["provider_ops"]
    cid2 = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "本番の売上", "notify": "desk"})[1]["conversation"]["id"]
    consult_decline(cur, cid2, {"reason": "needs_production_data", "alternatives": []})
    got.clear()
    desk_line.send_notices(cur, config=notify.parse(DOC), poster=lambda d, t: got.append(d.name) or True)
    assert got == []                                                                    # 窓口の会話だけ
    cur.execute("select payload from events where correlation_id=%s and name='desk.notified'", (desk.corr(cid2),))
    assert cur.fetchone()["payload"]["desk_only"] is True


@needs_db
def test_notice_retries_then_operator(cur):
    from datetime import datetime, timedelta, timezone
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    cid = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "本番の売上"})[1]["conversation"]["id"]
    consult_decline(cur, cid, {"reason": "needs_production_data", "alternatives": []})
    cfg, now = notify.parse(DOC), datetime.now(timezone.utc)
    fail = lambda d, t: False  # noqa: E731
    desk_line.send_notices(cur, config=cfg, poster=fail, now=now)
    assert desk_line.send_notices(cur, config=cfg, poster=fail, now=now + timedelta(seconds=10)) == 0   # 間を空ける
    desk_line.send_notices(cur, config=cfg, poster=fail, now=now + timedelta(seconds=400))
    desk_line.send_notices(cur, config=cfg, poster=fail, now=now + timedelta(seconds=800))
    assert "desk.notice_gave_up" in names(cur, cid)
    cur.execute("select payload->>'key' as k from events where name='inbox.requested' and payload->>'kind'='desk_notice_failed'")
    assert cur.fetchone()["k"].startswith(f"desk_notice_failed:{cid}:")
    assert desk_line.send_notices(cur, config=cfg, poster=lambda d, t: True, now=now + timedelta(days=1)) == 0


def test_done_task_items_not_doubled_by_inbox_notice():
    import notify_inbox
    assert set(notify_inbox.DONE_TASK_KINDS) == {"desk_receipt", "desk_decline"}
    assert notify.KINDS["done.task"]["status"] == "ready" if URL else True


@needs_db
def test_notice_waits_for_quiet_hours(cur):
    from datetime import datetime, timezone
    doc = {**DOC, "destinations": {"owner_ops": {"connection": "o", "quiet": "22-7"}}}
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    cid = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": "本番の売上"})[1]["conversation"]["id"]
    consult_decline(cur, cid, {"reason": "needs_production_data", "alternatives": []})
    cfg, got = notify.parse(doc), []
    night = datetime(2026, 11, 8, 14, 0, tzinfo=timezone.utc)   # JST 23 時
    assert desk_line.send_notices(cur, config=cfg, poster=lambda d, t: got.append(d.name) or True, now=night) == 0
    morning = datetime(2026, 11, 8, 23, 0, tzinfo=timezone.utc)  # JST 8 時
    assert desk_line.send_notices(cur, config=cfg, poster=lambda d, t: got.append(d.name) or True, now=morning) == 1
    assert got == ["owner_ops"]
