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
