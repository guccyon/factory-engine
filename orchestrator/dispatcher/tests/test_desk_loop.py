"""単発の依頼のループ（ADR 0039 の順番 1）: 会話の状態・閉じる理由・依頼どうしの結び・閉じたときの記録。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）。
"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")
REPO = "guccyon/rally"
ROOT = Path(__file__).resolve().parents[3]

if URL:
    import psycopg
    from psycopg.rows import dict_row

    import desk


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets, desk_conversations, desk_tokens restart identity cascade")
    yield c
    conn.close()


def requester(cur, name="cs-1"):
    desk.create_token(cur, name, "requester", [REPO], "test")
    return {"name": name, "role": "requester", "products": [REPO]}


def events(cur, cid):
    cur.execute("select name, payload from events where correlation_id=%s order by id", (desk.corr(cid),))
    return cur.fetchall()


def test_paths_match_workflow_definition():
    wf = yaml.safe_load((ROOT / "definitions" / "workflow.yaml").read_text())
    assert set(desk.PATHS) == set(wf["paths"])


def test_states_match_schema(cur):
    cur.execute("select pg_get_constraintdef(oid) as d from pg_constraint where conname='desk_conversations_state_check'")
    d = cur.fetchone()["d"]
    assert all(f"'{s}'" in d for s in desk.STATES)
    cur.execute("select pg_get_constraintdef(oid) as d from pg_constraint where conname='desk_conversations_close_reason_check'")
    d = cur.fetchone()["d"]
    assert all(f"'{r}'" in d for r in desk.CLOSE_REASONS)
    assert set(desk.TRANSITIONS) | {t for ts in desk.TRANSITIONS.values() for t in ts} <= set(desk.STATES)


def test_loop_through_delivery_and_acceptance(cur):
    who = requester(cur)
    c = desk.open_conversation(cur, who, "request", REPO, "今朝のレポートを送り直して")
    c = desk.advance(cur, c["id"], "routed", "desk.routed", {"path": "operate", "op": "report.send"}, path="operate")
    assert (c["state"], c["path"]) == ("routed", "operate")
    # 振り分けた後の言い足しは記録だけで、状態は戻らない（相談役に回し直さない）
    c = desk.post_message(cur, who, c["id"], "宛先は工場の健康のチャンネルで")
    assert c["state"] == "routed"
    for to, ev in (("awaiting_confirm", "desk.confirm_requested"), ("running", "desk.confirmed_run"), ("delivered", "desk.delivered")):
        c = desk.advance(cur, c["id"], to, ev, {})
        assert c["state"] == to
    with pytest.raises(desk.DeskError) as e:
        desk.advance(cur, c["id"], "running", "desk.confirmed_run")      # 決まった向きにしか進めない
    assert e.value.status == 409
    c = desk.finish(cur, c["id"], "accepted", who["name"])
    assert (c["state"], c["close_reason"], c["closed_by"]) == ("accepted", "accepted", "cs-1") and c["closed_at"]
    assert desk.finish(cur, c["id"], "declined", "x")["close_reason"] == "accepted"   # 終わった会話は変えない
    with pytest.raises(desk.DeskError):
        desk.post_message(cur, who, c["id"], "もう一度")
    evs = events(cur, c["id"])
    names = [e["name"] for e in evs]
    assert names == ["desk.opened", "desk.message", "desk.routed", "desk.message", "desk.confirm_requested", "desk.confirmed_run",
                     "desk.delivered", "desk.closed"]
    assert evs[2]["payload"]["from"] == "open" and evs[2]["payload"]["state"] == "routed"
    rec = evs[-1]["payload"]["record"]
    assert rec["path"] == "operate" and rec["outcome"] == "accepted" and rec["reroutes"] == 0
    assert rec["sec_routed"] is not None and rec["sec_delivered"] is not None


def test_advance_rejects_unknown_columns_and_paths(cur):
    who = requester(cur)
    c = desk.open_conversation(cur, who, "request", REPO, "x")
    with pytest.raises(ValueError):
        desk.advance(cur, c["id"], "routed", "desk.routed", state="closed")
    with pytest.raises(ValueError):
        desk.advance(cur, c["id"], "routed", "desk.routed", path="whatever")
    with pytest.raises(ValueError):
        desk.finish(cur, c["id"], "done", "x")


def test_withdraw_cancels_running_jobs(cur):
    who = requester(cur)
    c = desk.open_conversation(cur, who, "request", REPO, "集計して")
    desk.advance(cur, c["id"], "routed", "desk.routed", path="quick")
    cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, kind, state) values (%s,%s,%s,1,'desk','desk_consult','running') "
                "returning id", (desk.corr(c["id"]), REPO, -c["id"]))
    jid = cur.fetchone()["id"]
    c = desk.close_conversation(cur, who, c["id"], "要らなくなった")
    assert (c["state"], c["close_reason"]) == ("closed", "withdrawn")
    cur.execute("select cancel_requested from jobs where id=%s", (jid,))
    assert cur.fetchone()["cancel_requested"]
    p = events(cur, c["id"])[-1]["payload"]
    assert p["reason"] == "withdrawn" and p["note"] == "要らなくなった" and p["cancelled_jobs"] == [jid] and p["from"] == "routed"


def test_derived_request_links_back(cur):
    who = requester(cur)
    a = desk.open_conversation(cur, who, "request", REPO, "本番の売上を出して")
    b = desk.open_conversation(cur, who, "request", REPO, "staging の予約数を出して", derived_from=a["id"])
    assert b["derived_from"] == a["id"]
    assert events(cur, b["id"])[0]["payload"]["derived_from"] == a["id"]
    other = requester(cur, "cs-2")
    with pytest.raises(desk.DeskError):
        desk.open_conversation(cur, other, "request", REPO, "x", derived_from=a["id"])   # ほかの依頼者の会話からは結べない
    with pytest.raises(desk.DeskError):
        desk.open_conversation(cur, who, "request", REPO, "x", source_ref="javascript:alert(1)")
