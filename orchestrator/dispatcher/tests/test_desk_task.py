"""窓口の依頼に LLM の実行役がその場で応える（desk_task.py、ADR 0037 の決定 5 の quick）。

受付 → 振り分け（相談役の route、path: quick）→ 確認を飛ばして running → 実行役のジョブを窓口の枠に積む
→ 結果を成果物に残して提出 → 受取確認。DB が要るものは TEST_DATABASE_URL の Postgres で動かす。
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
DESK = yaml.safe_load((ROOT / "definitions" / "desk.yaml").read_text(encoding="utf-8"))

import desk  # noqa: E402
import desk_task as dt  # noqa: E402
import line  # noqa: E402
import runner_relay  # noqa: E402


def test_values_match_definition():
    t = DESK["task"]
    assert dt.LIMITS == t["limits"] and set(dt.FORMS) == set(t["forms"])
    assert set(runner_relay.KIND_TOOLS[dt.KIND]) == set(t["relay_tools"])
    agents = yaml.safe_load((ROOT / "definitions" / "agents.yaml").read_text(encoding="utf-8"))
    assert agents["steps"]["desk.task"]["tier"] == dt.MODEL_TIER and agents["steps"]["desk.task"]["mode"] == "agentic"


def test_path_is_declared_everywhere():
    assert set(dt.PATHS) <= set(desk.PATHS)
    for p in dt.PATHS:
        assert DESK["loop"]["targets"]["route"][p] and DESK["loop"]["targets"]["deliver"][p]
    assert dt.KIND in line.DESK_KINDS and dt.KIND not in line.GOVERNANCE_KINDS
    events = yaml.safe_load((ROOT / "definitions" / "events.yaml").read_text(encoding="utf-8"))["events"]
    for n in ("desk_task.enqueued", "desk_task.skipped", "desk_task.answered", "desk_task.failed"):
        assert n in events, n


def test_deliver_text():
    t = dt.deliver_text({"interpretation": "役員会向けの 1 枚", "answer": "## 今月の成果\n- 12 件", "evidence": [{"ref": "report:3", "what": "週報"}],
                         "unknowns": ["売り上げは見られない"], "next": "毎月にするならチケット化"})
    assert t.index("こう受け取りました") < t.index("今月の成果") < t.index("根拠") < t.index("分からないこと") < t.index("次にできること")
    assert "分からないこと" not in dt.deliver_text({"answer": "a"})


if URL:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import desk_api
    import desk_line
    import inbox


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets, desk_conversations, desk_tokens restart identity cascade")
    yield c
    conn.close()


def call(method, path, token=None, body=None):
    try:
        return desk_api.route(method, path, {}, body or {}, token, connect=lambda: psycopg.connect(URL, row_factory=dict_row, autocommit=True))
    except desk.DeskError as e:
        return e.status, {"error": str(e)}


def open_request(cur, body="新機能のお知らせの文面を 3 案ほしい"):
    tok = desk.create_token(cur, "mkt-1", "requester", [REPO], "test")
    st, out = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": body})
    return tok, out["conversation"]["id"]


def consult_route(cur, route):
    jid = desk_line.enqueue(cur, "tier-small", [REPO])[0]
    cur.execute("update jobs set state='succeeded' where id=%s returning *", (jid,))
    desk_line.record(cur, cur.fetchone(), {"ok": True, "outcome": "route", "route": route, "confidence": 0.8, "cost_usd": 0.2}, "succeeded", None)


def state(cur, cid):
    cur.execute("select * from desk_conversations where id=%s", (cid,))
    return cur.fetchone()


def names(cur, cid):
    cur.execute("select name from events where correlation_id=%s order by id", (desk.corr(cid),))
    return [r["name"] for r in cur.fetchall()]


def delivered(cur, cid):
    cur.execute("select payload->>'body' as b from events where correlation_id=%s and name='desk.message' and payload->>'kind'='delivered' "
                "order by id desc limit 1", (desk.corr(cid),))
    return cur.fetchone()["b"]


def finish(cur, jid, result, st="succeeded"):
    cur.execute("update jobs set state=%s, finished_at=now(), result=%s where id=%s returning *", (st, Jsonb(result), jid))
    dt.record(cur, cur.fetchone(), result, st, None if st == "succeeded" else "exit 1")


QUICK = {"path": "quick", "task": {"what": "新機能のお知らせの文面を 3 案", "form": "text"}, "why": "仕様を読めば書ける"}
GOOD = {"ok": True, "interpretation": "利用者向けのお知らせを 3 案", "answer": "案 1: …\n案 2: …\n案 3: …",
        "evidence": [{"ref": "doc:specs/x.md", "what": "機能の説明"}], "unknowns": [], "next": "", "cap_hit": False, "cost_usd": 0.3,
        "num_turns": 6, "model": "m"}


@needs_db
def test_request_to_quick_answer_to_receipt(cur):
    tok, cid = open_request(cur)
    consult_route(cur, QUICK)
    c = state(cur, cid)
    assert (c["state"], c["path"]) == ("running", "quick")                    # 読むだけなので確認を飛ばす
    assert inbox.find(cur, f"task:desk_confirm:{cid}:1") is None
    jids = dt.enqueue(cur, [REPO])
    assert len(jids) == 1 and dt.enqueue(cur, [REPO]) == []                   # 1 回に 1 本
    cur.execute("select * from jobs where id=%s", (jids[0],))
    j = cur.fetchone()
    assert (j["kind"], j["issue_number"], j["model_tier"]) == ("desk_task", -cid, dt.MODEL_TIER)
    cur.execute("select payload from events where name='job.enqueued' and job_id=%s", (jids[0],))
    inp = cur.fetchone()["payload"]["input"]
    assert inp["task"]["what"] == QUICK["task"]["what"] and inp["limits"] == dt.LIMITS["quick"] and "records" in inp
    assert line.claim_desk(cur)["id"] == jids[0]                              # 窓口の枠で取り出す
    finish(cur, jids[0], GOOD)
    c = state(cur, cid)
    assert c["state"] == "delivered" and abs(float(c["cost_usd"]) - 0.5) < 1e-6
    cur.execute("select payload from artifacts where type=%s", (dt.RESULT_TYPE,))
    a = cur.fetchone()["payload"]
    assert a["answer"].startswith("案 1") and a["conversation_id"] == cid and a["round"] == 1
    assert inbox.find(cur, f"task:desk_receipt:{cid}:1")
    body = delivered(cur, cid)
    assert "こう受け取りました" in body and "案 3" in body and "doc:specs/x.md" in body
    assert call("POST", f"/api/desk/conversations/{cid}/messages", tok, {"answer": "accept"})[0] == 200
    ns = names(cur, cid)
    for a_, b_ in (("desk.routed", "desk.confirmed_run"), ("desk.confirmed_run", "desk_task.enqueued"),
                   ("desk_task.enqueued", "desk_task.answered"), ("desk_task.answered", "desk.delivered")):
        assert ns.index(a_) < ns.index(b_), (a_, b_)


@needs_db
def test_route_without_task_asks_requester(cur):
    _, cid = open_request(cur)
    consult_route(cur, {"path": "quick", "task": {"what": " ", "form": ""}, "why": ""})
    assert state(cur, cid)["state"] == "waiting_requester" and "desk.routed" not in names(cur, cid)


@needs_db
def test_not_a_factory_repo(cur):
    _, cid = open_request(cur)
    consult_route(cur, QUICK)
    assert dt.enqueue(cur, ["other/repo"]) == [] and "desk_task.skipped" in names(cur, cid)


@needs_db
def test_cap_hit_and_failure_are_delivered(cur):
    tok, cid = open_request(cur)
    consult_route(cur, QUICK)
    jid = dt.enqueue(cur, [REPO])[0]
    finish(cur, jid, {**GOOD, "cap_hit": True, "next": "残りの 2 案"})
    assert state(cur, cid)["state"] == "delivered" and "desk.cap_hit" in names(cur, cid)
    assert "残りの 2 案" in delivered(cur, cid)
    # 続きを頼む → 2 回目。失敗も提出する
    assert call("POST", f"/api/desk/conversations/{cid}/messages", tok, {"answer": "more", "body": "残りも"})[0] == 200
    consult_route(cur, QUICK)
    jid2 = dt.enqueue(cur, [REPO])[0]
    cur.execute("select payload from events where name='job.enqueued' and job_id=%s", (jid2,))
    assert cur.fetchone()["payload"]["input"]["round"] == 2
    finish(cur, jid2, {"ok": False, "exit": 1}, st="failed")
    assert state(cur, cid)["state"] == "delivered" and "desk_task.failed" in names(cur, cid)
    assert "止まりました" in delivered(cur, cid)


@needs_db
def test_task_cost_does_not_eat_consult_budget(cur):
    _, cid = open_request(cur)
    consult_route(cur, QUICK)
    jid = dt.enqueue(cur, [REPO])[0]
    finish(cur, jid, {**GOOD, "cost_usd": desk_line.MAX_USD + 1})
    assert dt.spent_on(cur, cid) == pytest.approx(desk_line.MAX_USD + 1)
