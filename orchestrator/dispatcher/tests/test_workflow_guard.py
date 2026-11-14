"""ワークフローの上限を守らせる（workflow_guard.py、ADR 0038 の順番 2）: 衝突の解消の上限 → 待つ → 1 回 → 人へ"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import workflow_guard as wg

CORR, PR = "github:o/r#5", "https://github.com/o/r/pull/50"
SCREENS = "apps/admin-web/screenshots/screens.ts"


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def conflict(cur, n=1, files=(SCREENS,)):
    for i in range(n):
        cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, attempt) values (%s,'o/r',5,'implement','succeeded',"
                    "(select count(*)+1 from jobs)) returning id", (CORR,))
        jid = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('continue.enqueued',%s,%s,%s)",
                    (CORR, jid, Jsonb({"reason": "conflict", "pr_url": PR})))
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('conflict.files',%s,%s,%s)",
                    (CORR, jid, Jsonb({"files": list(files)})))


def test_under_cap_is_ok(cur):
    conflict(cur, 2)
    assert wg.conflict_gate(cur, CORR, PR, lambda: pytest.fail("上限の前に GitHub を引かない")) == "ok"


def test_wait_then_retry_once_then_human(cur):
    conflict(cur, 3)
    inflight = {"github:o/r#9": {SCREENS}, "github:o/r#10": {"apps/backend/src/lib.rs"}}
    assert wg.conflict_gate(cur, CORR, PR, lambda: inflight) == "wait"
    assert wg.waiting(cur, CORR) and wg.capped(cur, CORR) is None
    assert wg.conflict_gate(cur, CORR, PR, lambda: inflight) == "wait"      # 待ちの記録は 1 度だけ
    cur.execute("select count(*) as n from events where name='workflow.enforced'")
    assert cur.fetchone()["n"] == 1
    inflight.pop("github:o/r#9")                                             # 同じファイルに触るチケットが終わった
    assert wg.conflict_gate(cur, CORR, PR, lambda: inflight) == "ok"
    assert not wg.waiting(cur, CORR)
    conflict(cur, 1)                                                          # 待ちの後の 1 回でも衝突した
    assert wg.conflict_gate(cur, CORR, PR, lambda: inflight) == "human"
    assert wg.capped(cur, CORR)["conflicts"] == 4
    assert wg.conflict_gate(cur, CORR, PR, lambda: {}) == "human"


def test_nothing_to_wait_for_retries_once(cur):
    conflict(cur, 3)
    assert wg.conflict_gate(cur, CORR, PR, lambda: {"github:o/r#9": {"other.ts"}}) == "ok"
    cur.execute("select payload from events where name='workflow.enforced'")
    assert cur.fetchone()["payload"]["kind"] == "conflict_retry"


def test_declared_cap_is_used(cur):
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values ('Plan',%s,'plan',%s)",
                (CORR, Jsonb({"decision": "ready", "workflow": {"caps": {"retries.conflict": 1}}})))
    conflict(cur, 1)
    assert wg.conflict_gate(cur, CORR, PR, lambda: {"github:o/r#9": {SCREENS}}) == "wait"


def test_off_switch(cur, monkeypatch):
    monkeypatch.setattr(wg, "ENFORCE", set())
    conflict(cur, 9)
    assert wg.conflict_gate(cur, CORR, PR, lambda: {}) == "ok" and not wg.waiting(cur, CORR)


# ------------------------------------------------------------------ プランナーを通す（ADR 0038 の追記）
def test_planless_ticket_goes_to_planner(cur):
    assert wg.needs_planner(cur, "o/r", 5, ["factory:planned", "factory:priority:p1"])
    assert wg.record_rerouted(cur, "o/r", 5, ["factory:planned"])
    assert not wg.record_rerouted(cur, "o/r", 5, ["factory:planned"])   # 同じチケットの記録は 1 回


def test_planned_ticket_passes(cur):
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values ('Plan',%s,'plan',%s)", (CORR, Jsonb({"decision": "ready"})))
    assert wg.planned_by(cur, "o/r", 5) == "plan"
    assert not wg.needs_planner(cur, "o/r", 5, ["factory:planned"])


def test_split_children_pass(cur):
    cur.execute("insert into events(name, correlation_id, payload) values ('plan.split','github:o/r#1',%s)",
                (Jsonb({"repo": "o/r", "issue": 1, "child": 7, "steps": ["design_system"]}),))
    cur.execute("insert into events(name, correlation_id, payload) values ('plan.split','github:o/r#2',%s)",
                (Jsonb({"repo": "o/r", "issue": 2, "kind": "tasks", "child": None, "children": [8, 9], "steps": ["implement"]}),))
    assert wg.planned_by(cur, "o/r", 7) == "split_child"
    assert wg.planned_by(cur, "o/r", 9) == "split_child"
    assert wg.planned_by(cur, "o/r", 10) is None
    assert wg.planned_by(cur, "x/y", 7) is None   # 別のリポの同じ番号は別のチケット


def test_one_off_requests_skip_planner(cur, monkeypatch):
    assert not wg.needs_planner(cur, "o/r", 5, ["factory:planned", "factory:changelog"])
    assert not wg.needs_planner(cur, "o/r", 5, ["factory:planned", "factory:direct"])
    monkeypatch.setattr(wg, "PLAN_GATE", False)
    assert not wg.needs_planner(cur, "o/r", 5, ["factory:planned"])


# ------------------------------------------------------------------ 費用の上限（ADR 0038 の追記 2026-11-11）
def spend(cur, kind, cost):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, attempt, result) values (%s,'o/r',5,%s,'succeeded',"
                "(select count(*)+1 from jobs),%s)", (CORR, kind, Jsonb({"cost_usd": cost})))


def test_cost_cap_holds_then_release(cur):
    spend(cur, "implement", 5.0)
    assert wg.cost_gate(cur, CORR) is None
    assert wg.job_budget(cur, CORR) == 3.0            # 既定 M: 実装 $8 の残り
    spend(cur, "implement", 3.5)
    hit = wg.cost_gate(cur, CORR)
    assert hit and hit["hit"] == ["implement.cost_usd"] and hit["round"] == 0 and hit["default"] is True
    assert wg.job_budget(cur, CORR) == 0.0
    wg.cost_gate(cur, CORR)
    cur.execute("select count(*) as n from events where name='workflow.enforced' and payload->>'kind'='cost_human'")
    assert cur.fetchone()["n"] == 1                   # 同じ区切りでは 1 回だけ
    assert wg.release_cost(cur, CORR, 5, "test") == 1
    assert wg.cost_gate(cur, CORR) is None and wg.job_budget(cur, CORR) == 4.5
    assert wg.cost_stoppage_key(CORR, 1) == "cost:github:o/r#5#1"


def test_ticket_cost_counts_all_steps(cur):
    spend(cur, "implement", 7.0)
    spend(cur, "inspect", 4.0)
    spend(cur, "judge", 4.5)
    hit = wg.cost_gate(cur, CORR)
    assert hit and hit["hit"] == ["ticket.cost_usd"]   # 実装は $7 で上限の内、チケット全体は $15.5


def test_cost_cap_off(cur, monkeypatch):
    monkeypatch.setattr(wg, "ENFORCE", {"conflict"})
    spend(cur, "implement", 30.0)
    assert wg.cost_gate(cur, CORR) is None and wg.job_budget(cur, CORR) is None


# ------------------------------------------------------------------ 着手の宣言と飛ばし
def plan_with(cur, steps=None, start=None):
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values ('Plan',%s,'plan',%s)",
                (CORR, Jsonb({"decision": "ready", "workflow": {"path": "ticket", "steps": steps or [], "caps": {}, "start": start or {"mode": "now"}}})))


def test_start_wait_recorded_once_per_blockers(cur):
    assert wg.record_start_wait(cur, CORR, [7], {"why": "同じ画面"})
    assert not wg.record_start_wait(cur, CORR, [7], {})
    assert wg.record_start_wait(cur, CORR, [7, 8], {})


def test_skips_recorded_and_inspect_skipped_only_for_docs(cur):
    plan_with(cur, [{"step": "ui_design", "run": False, "why": "画面を変えない"}, {"step": "inspect", "run": False, "why": "文書だけ"},
                    {"step": "implement", "run": True, "effort": "light"}])
    assert wg.record_skips(cur, CORR) == 2
    assert wg.record_skips(cur, CORR) == 0
    assert wg.skip_inspect(cur, CORR, ["code_change"], PR, "a" * 40) is None    # コードが入っていれば検査する
    assert not wg.inspect_skipped(cur, PR, "a" * 40)
    sk = wg.skip_inspect(cur, CORR, ["docs_only"], PR, "b" * 40)
    assert sk and sk["why"] == "文書だけ" and "docs" in sk["rule"]
    assert wg.inspect_skipped(cur, PR, "b" * 40)


def test_inspect_not_skipped_without_declaration(cur):
    plan_with(cur, [{"step": "inspect", "run": True}])
    assert wg.skip_inspect(cur, CORR, ["docs_only"], PR, "c" * 40) is None


def test_standing_qa_issue_has_no_ticket_cap(cur):
    """QA の固定 Issue は、回を重ねても チケット全体の費用で止めない（ADR 0054 の決定 6）。上乗せの記録が残っていても同じ"""
    for _ in range(12):
        spend(cur, "qa", 3.0)
    assert wg.cost_gate(cur, CORR)["hit"] == ["ticket.cost_usd"]      # QA を積んだ記録がなければ、ふつうのチケットとして数える
    assert not wg.cost_cleared(cur, CORR)
    wg.release_cost(cur, CORR, 1, "test")
    cur.execute("insert into events(name, correlation_id, payload) values ('qa.enqueued',%s,%s)", (CORR, Jsonb({"repo": "o/r", "issue": 5})))
    assert wg.workflow_shadow.standing_qa(cur, CORR)
    assert wg.cost_gate(cur, CORR) is None and wg.cost_cleared(cur, CORR)
    assert wg.cost_left(cur, CORR)["ticket.cost_usd"] == {"cap": 0.0, "spent": 36.0, "left": -36.0}
    assert wg.workflow_shadow.evaluate(cur, CORR) == []              # 記録だけの突き合わせにも、上限を超えたと残さない


def test_qa_ticket_with_declared_caps_is_capped(cur):
    """上限を宣言した QA のチケット（版ごとのチケット。ADR 0054 の決定 2）は、宣言どおりに守らせる"""
    cur.execute("insert into events(name, correlation_id, payload) values ('qa.enqueued',%s,%s)", (CORR, Jsonb({"repo": "o/r", "issue": 5})))
    cur.execute("insert into artifacts(type, correlation_id, payload) values ('Plan',%s,%s)",
                (CORR, Jsonb({"decision": "ready", "workflow": {"caps": {"ticket.cost_usd": 10}}})))
    assert not wg.workflow_shadow.standing_qa(cur, CORR)
    for _ in range(4):
        spend(cur, "qa", 3.0)
    assert wg.cost_gate(cur, CORR)["hit"] == ["ticket.cost_usd"]


def ticket(cur, n, priority, held=False):
    cur.execute("insert into tickets(source, source_ref, repo, title, state, priority, held) values ('github',%s,'o/r','t','planned',%s,%s)",
                (f"o/r#{n}", priority, held))


def test_start_blockers_skip_lower_priority_and_held(cur):
    """p0 の修正を、優先度の低い・止まっている前の後ろに並べない（rally #1522）"""
    import ops
    p0, p1 = ops.LEVELS["p0"][0], ops.LEVELS["p1"][0]
    ticket(cur, 7, p1)
    ticket(cur, 8, p0)
    ticket(cur, 9, p0, held=True)
    assert wg.start_blockers(cur, CORR, "o/r", p0, [7, 8, 9, 10]) == [8, 10]   # 10 は台帳にない（待つ）
    cur.execute("select payload from events where name='workflow.enforced' and payload->>'kind'='start_skipped'")
    assert [x["why"] for x in cur.fetchone()["payload"]["skipped"]] == ["lower_priority", "held"]
    assert wg.start_blockers(cur, CORR, "o/r", p1, [7]) == [7]                  # 同じ優先度なら待つ
    assert wg.start_blockers(cur, CORR, "o/r", p0, []) == []


def test_start_blockers_skip_tickets_stopped_by_the_line_hold(cur):
    import ops
    p0, p1 = ops.LEVELS["p0"][0], ops.LEVELS["p1"][0]
    ticket(cur, 7, p1)
    ticket(cur, 8, p1)
    cur.execute("insert into events(name, payload) values ('line.held_by_qa', %s)",
                (Jsonb({"by": "qa", "incidents": [], "corrs": ["github:o/r#8"], "min_priority": p0}),))
    assert wg.start_blockers(cur, CORR, "o/r", p1, [7, 8]) == [8]   # 7 は差し止めで動かない。8 は差し止めた Incident のチケット
