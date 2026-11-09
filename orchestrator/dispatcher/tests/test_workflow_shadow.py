"""ワークフローの宣言を記録だけで突き合わせる（workflow_shadow.py、ADR 0038 の順番 1）: 台帳・上限に当たった・飛ばす宣言・着手の待ち"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
import workflow_shadow as ws  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
WDEFS = yaml.safe_load((Path(__file__).resolve().parents[3] / "definitions" / "workflow.yaml").read_text(encoding="utf-8"))
CORR = "github:o/r#5"


def test_constants_match_definitions():
    kinds = WDEFS["caps"]["kinds"]
    assert ws.ON_CAP == {k: v["on_cap"] for k, v in kinds.items()}
    assert ws.CAP_JA == {k: v["ja"] for k, v in kinds.items()}
    assert set(ws.ACTION_JA) == {v["on_cap"] for v in kinds.values()}
    assert set(ws.CONTINUE_CAP.values()) <= set(kinds)
    assert set(ws.SKIPPABLE_JOBS.values()) <= set(WDEFS["steps"])


def test_report_lines():
    assert ws.report_lines({"plans": 0, "plans_marked": 0, "cap_hit": {}, "would_skip": 0, "would_wait": 0}) == []
    lines = ws.report_lines({"plans": 4, "plans_marked": 1, "cap_hit": {"retries.conflict": 2}, "would_skip": 1, "would_wait": 0})
    assert "宣言 4 件" in lines[0] and any("衝突の解消 2 件" in x and "並べて待ち" in x for x in lines) and any("飛ばした: 1" in x for x in lines)


# ------------------------------------------------------------------ DB を使うもの
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")


@pytest.fixture()
def cur():
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def plan(cur, caps=None, skip=(), start=None, decision="ready"):
    from psycopg.types.json import Jsonb
    steps = [{"step": s, "run": s not in skip, "effort": "standard", "why": ""} for s in ("implement", "checks", "inspect", "judge", "qa")]
    wf = {"path": "ticket", "steps": steps, "caps": {**WDEFS["caps"]["defaults"]["any"], **WDEFS["caps"]["defaults"]["S"], **(caps or {})},
          "caps_declared": caps or {}, "handoff": {"ship": "train", "learn": "signals"}, "start": start or {"mode": "now", "after": []}}
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values ('Plan',%s,'plan',%s) returning id",
                (CORR, Jsonb({"repo": "o/r", "issue": 5, "decision": decision, "workflow": wf, "workflow_marks": []})))
    return cur.fetchone()["id"]


def job(cur, kind="implement", turns=10, cost=1.0, conflict=False):
    from psycopg.types.json import Jsonb
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, attempt, result, finished_at) "
                "values (%s,'o/r',5,%s,'succeeded',(select count(*)+1 from jobs),%s,now()) returning id",
                (CORR, kind, Jsonb({"num_turns": turns, "cost_usd": cost})))
    jid = cur.fetchone()["id"]
    if conflict:
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('continue.enqueued',%s,%s,%s)",
                    (CORR, jid, Jsonb({"reason": "conflict", "pr_url": "u"})))
    return jid


@needs_db
def test_old_plan_without_workflow_is_ignored(cur):
    from psycopg.types.json import Jsonb
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values ('Plan',%s,'plan',%s)", (CORR, Jsonb({"decision": "ready"})))
    job(cur)
    assert ws.evaluate(cur, CORR) == []


@needs_db
def test_ledger_and_conflict_cap_hit(cur):
    pid = plan(cur)                                  # S: 実装 30 ターン、衝突の解消 3 回
    job(cur, turns=20, cost=1.0)
    for _ in range(4):                               # 衝突の解消 4 回（ターンは実装のターンに数えない）
        job(cur, turns=50, cost=0.25, conflict=True)
    job(cur, kind="plan", turns=3, cost=0.1)
    led = ws.ledger(cur, CORR)
    assert led["retries.conflict"] == 4 and led["implement.turns"] == 20
    assert led["implement.cost_usd"] == 2.0 and led["ticket.cost_usd"] == 2.1
    hits = ws.evaluate(cur, CORR)
    assert [h["cap"] for h in hits] == ["retries.conflict"]
    assert hits[0]["action"] == "serialize_then_human" and hits[0]["declared"] == 3 and hits[0]["actual"] == 4 and hits[0]["plan_id"] == pid
    assert ws.evaluate(cur, CORR) == []              # 同じ当たりは 1 度だけ
    assert ws.cap_hits(cur, "o/r")[0]["cap"] == "retries.conflict"


@needs_db
def test_turns_and_cost_caps_use_declaration(cur):
    plan(cur, caps={"implement.turns": 15, "ticket.cost_usd": 1.5})
    job(cur, turns=16, cost=2.0)
    caps = {h["cap"]: h for h in ws.evaluate(cur, CORR)}
    assert set(caps) == {"implement.turns", "ticket.cost_usd"}
    assert caps["implement.turns"]["action"] == "replan" and caps["implement.turns"]["declared_by_planner"] is True


@needs_db
def test_would_skip_when_skipped_step_ran(cur):
    plan(cur, skip=("qa",))
    job(cur, kind="qa", turns=5, cost=0.2)
    job(cur, kind="inspect", turns=5, cost=0.2)       # inspect は飛ばしていない
    out = ws.evaluate(cur, CORR)
    assert [(o.get("step")) for o in out if "step" in o] == ["qa"]


@needs_db
def test_would_wait_for_declared_after(cur):
    plan(cur, start={"mode": "after", "after": [812], "files": ["apps/admin-web/src/screens.ts"], "why": "同じ登録の箇所"})
    assert ws.declared_after(cur, CORR) == [812]
    assert ws.would_wait(cur, CORR, [812]) is True
    assert ws.would_wait(cur, CORR, [812]) is False   # 1 度だけ
    assert ws.would_wait(cur, CORR, []) is False
    s = ws.summary(cur, datetime.now(timezone.utc) - timedelta(hours=1), datetime.now(timezone.utc) + timedelta(minutes=1))
    assert s["would_wait"] == 1 and s["plans"] == 1


@needs_db
def test_step_only_recently_active(cur):
    plan(cur)
    for _ in range(4):
        job(cur, cost=0.1, conflict=True)
    assert ws.step(cur) == 1
    assert ws.step(cur) == 0


@needs_db
def test_planner_records_workflow_and_attaches_context(cur):
    import planner as pn
    from psycopg.types.json import Jsonb
    wf = {"path": "ticket", "steps": [{"step": "qa", "run": False, "effort": "standard", "why": "画面がない"},
                                       {"step": "implement", "run": True, "effort": "deep", "why": "契約から"}],
          "caps": {"retries.conflict": 2}, "handoff": {"ship": "none", "learn": "signals"}, "start": {"mode": "after", "after": [9], "why": "w"}}
    result = {"decision": "ready", "steps": ["implement"], "guards": [], "advisories": [], "round": 1, "versions": {},
              "plan": {"steps": [], "expected_files": ["a.rs"], "confidence": 0.9}, "workflow": wf, "workflow_marks": ["mandatory_skip:checks"]}
    pid = pn.record(cur, "o/r", 5, CORR, None, result)
    cur.execute("select payload from artifacts where id=%s", (pid,))
    p = cur.fetchone()["payload"]
    assert p["workflow"]["caps"] == {"retries.conflict": 2} and p["workflow_marks"] == ["mandatory_skip:checks"]
    cur.execute("select payload from events where name='plan.decided'")
    ev = cur.fetchone()["payload"]
    assert ev["workflow"]["skip"] == ["qa"] and ev["workflow"]["deep"] == ["implement"] and ev["workflow"]["after"] == [9]
    text = "\n".join(pn.workflow_lines(wf, ["mandatory_skip:checks"]))
    assert "qa: 飛ばす" in text and "衝突の解消 2" in text and "#9 の後" in text
    # 流れているほかのチケットと、その見込みのファイル
    cur.execute("insert into tickets(source, source_ref, repo, title, state) values ('github','o/r#9','o/r','画面','active'), "
                "('github','o/r#5','o/r','これ','planned')")
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values ('Plan','github:o/r#9','plan',%s)",
                (Jsonb({"expected_files": ["apps/admin-web/src/screens.ts"]}),))
    inp = pn.attach_context(cur, "o/r", CORR, {"round": 1})
    assert inp["round"] == 1 and [t["issue"] for t in inp["inflight"]] == [9]
    assert inp["inflight"][0]["files"] == ["apps/admin-web/src/screens.ts"]
    assert "hot_files" in inp and "cap_hits" in inp


def test_default_caps_match_definitions():
    assert ws.DEFAULT_CAPS == WDEFS["caps"]["defaults"]
    assert ws.DEFAULT_BAND in WDEFS["caps"]["defaults"]


@needs_db
def test_ticket_without_plan_uses_default_caps(cur):
    for _ in range(4):                               # プランナーを通らずに入ったチケット（差し戻しの直しなど）
        job(cur, cost=0.1, conflict=True)
    caps, wf = ws.caps_of(cur, CORR)
    assert wf is None and caps["retries.conflict"] == 3 and caps["implement.turns"] == 60
    hits = ws.evaluate(cur, CORR)
    assert [(h["cap"], h["plan_id"], h["default"]) for h in hits] == [("retries.conflict", None, True)]
    assert ws.evaluate(cur, CORR) == []
