"""ops（ADR 0009）の取り出し順・保留・割り込み・入口のラベルの扱い。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）:
  TEST_DATABASE_URL=postgresql://factory@localhost:5432/factory_test python -m pytest orchestrator/dispatcher/tests
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

    import ops


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def job(cur, ref, state="queued", labels=(), kind="implement"):
    repo, n = ref.split("#")
    t = ops.upsert_ticket(cur, "github", ref, repo, title="T" + n, priority=ops.priority_from_labels(list(labels)))
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, ticket_id, priority, started_at) "
                "values (%s,%s,%s,%s,%s,%s,%s, case when %s='running' then now() end) returning id",
                (f"github:{ref}", repo, int(n), kind, state, t["id"], t["priority"], state))
    return cur.fetchone()["id"], t


def claim(cur):
    cur.execute("select * from claim_job()")
    return cur.fetchone()["id"]


def test_levels():
    assert ops.priority_from_labels(["bug", "factory:priority:p1", "factory:priority:p3"]) == 75
    assert ops.priority_from_labels(["bug"]) is None
    assert [ops.level_of(v) for v in (100, 75, 60, 10)] == ["p0", "p1", "p2", "p3"]


def test_claim_by_priority_and_skip_held(cur):
    j1, t1 = job(cur, "o/r#1")
    j2, _ = job(cur, "o/r#2", labels=["factory:priority:p1"])
    j3, _ = job(cur, "o/r#3", labels=["factory:priority:p3"])
    ops.set_held(cur, t1["id"], True, "test")
    assert [claim(cur), claim(cur), claim(cur)] == [j2, j3, None]
    ops.set_held(cur, t1["id"], False, "test")
    assert claim(cur) == j1


def test_source_label_does_not_override_human(cur):
    _, t = job(cur, "o/r#1", labels=["factory:priority:p3"])
    ops.set_priority(cur, t["id"], 90, "console:test")
    assert ops.upsert_ticket(cur, "github", "o/r#1", "o/r", priority=25)["priority"] == 90
    _, t2 = job(cur, "o/r#2")
    assert ops.upsert_ticket(cur, "github", "o/r#2", "o/r", priority=75)["priority"] == 75


def test_preempt_lowest_running_once(cur):
    job(cur, "o/r#1", state="running")
    low, _ = job(cur, "o/r#2", state="running", labels=["factory:priority:p3"])
    job(cur, "o/r#3", state="running", kind="judge", labels=["factory:priority:p3"])   # 判定は止めない
    assert ops.preempt_victims(cur, 3) == []
    urgent, _ = job(cur, "o/r#4", labels=["factory:priority:p0"])
    v = ops.preempt_victims(cur, 3)
    assert [(a["id"], b["id"]) for a, b in v] == [(low, urgent)]
    assert ops.stop_job(cur, low, "dispatcher", preempted_for=urgent) == "running"
    assert ops.preempt_victims(cur, 3) == []   # 止める印を付けたら二度は選ばない
    assert ops.preempt_victims(cur, 4) == []   # 枠が空いていれば割り込まない


def test_stop_queued_cancels_now(cur):
    j, _ = job(cur, "o/r#1")
    assert ops.stop_job(cur, j, "test") == "queued"
    cur.execute("select state from jobs where id=%s", (j,))
    assert cur.fetchone()["state"] == "cancelled"
    assert ops.stop_job(cur, j, "test") is None


def test_keep_awake_defaults_to_prevent_and_follows_last_set(cur):
    assert ops.keep_awake(cur) is True
    ops.set_keep_awake(cur, False, "console:builder")
    assert ops.keep_awake(cur) is False
    ops.set_keep_awake(cur, True, "console:builder")
    assert ops.keep_awake(cur) is True
