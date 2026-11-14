"""判定に進まない PR（prwait:）と、枠のふさがり（slot:）をラインが空（idle:）と分けること（#308 の追加、窓口の分析 11-14）。
11-10 は rally #1358 が同時に流す枠 1 を約 33 時間ふさぎ、番人はこれを「ラインが空」と記録していた。

  TEST_DATABASE_URL=postgresql://factory@localhost:5432/factory_test python -m pytest orchestrator/dispatcher/tests
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    for k, v in {"DATABASE_URL": URL, "GH_TOKEN": "x", "CLAUDE_CODE_OAUTH_TOKEN": "x", "OBJECTSTORE_ENDPOINT": "localhost:1",
                 "OBJECTSTORE_ACCESS_KEY": "x", "OBJECTSTORE_SECRET_KEY": "x", "NOTIFIER": "log", "JOB_DB_ENABLED": "0"}.items():
        os.environ.setdefault(k, v)
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import dispatcher
    import stoppage

PR_URL = "https://github.com/o/r/pull/1358"
CORR = "github:o/r#1350"
SHA = "a" * 40


@pytest.fixture()
def cur(monkeypatch):
    monkeypatch.setattr(stoppage, "notify", lambda kind, s, cur=None: None)
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def ev(cur, name, payload, ago_min=0, corr=CORR):
    cur.execute("insert into events(name, correlation_id, payload, occurred_at) values (%s,%s,%s, now() - make_interval(mins => %s))",
                (name, corr, Jsonb(payload), ago_min))


def head(cur, ago_min, sha=SHA, draft=False):
    ev(cur, "pr.head_updated", {"pr_url": PR_URL, "head_sha": sha, "draft": draft}, ago_min)


def test_opens_when_ci_passed_but_not_decided_and_closes_on_decision(cur):
    head(cur, 30)
    ev(cur, "pr.ci_passed", {"pr_url": PR_URL, "head_sha": SHA}, 25)
    dispatcher.prwait_step(cur)
    assert stoppage.open_keys(cur, "prwait:") == []   # まだ短い
    cur.execute("update events set occurred_at = now() - interval '3 hours' where name='pr.head_updated'")
    dispatcher.prwait_step(cur)
    assert stoppage.open_keys(cur, "prwait:") == [f"prwait:{PR_URL}"]
    cur.execute("select payload from events where name='stoppage.opened'")
    p = cur.fetchone()["payload"]
    assert p["category"] == "factory-bug" and "CI は通った" in p["what"]
    ev(cur, "supervisor.merge_decided", {"pr_url": PR_URL, "head_sha": SHA, "table": "auto"})
    dispatcher.prwait_step(cur)
    assert stoppage.open_keys(cur, "prwait:") == []


def test_skips_human_wait_draft_running_job_and_finished(cur):
    head(cur, 300)
    stoppage.open_stoppage(cur, f"ci:{PR_URL}", "ticket", "CI が落ちた")
    dispatcher.prwait_step(cur)
    assert stoppage.open_keys(cur, "prwait:") == []   # 人を待つ停止がすでにある
    stoppage.close_stoppage(cur, f"ci:{PR_URL}")
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state) values (%s,'o/r',1350,'implement','running')", (CORR,))
    dispatcher.prwait_step(cur)
    assert stoppage.open_keys(cur, "prwait:") == []   # 続きのジョブが直している
    cur.execute("truncate jobs restart identity cascade")
    ev(cur, "pr.merged", {"pr_url": PR_URL})
    dispatcher.prwait_step(cur)
    assert stoppage.open_keys(cur, "prwait:") == []
    cur.execute("truncate events restart identity cascade")
    head(cur, 300, draft=True)
    dispatcher.prwait_step(cur)
    assert stoppage.open_keys(cur, "prwait:") == []   # ドラフト（途中終了）は続きの仕組みが拾う


def test_ci_unstarted_is_named(cur):
    head(cur, 300)
    ev(cur, "pr.ci_unstarted", {"pr_url": PR_URL, "head_sha": SHA, "workflow": "ci"}, 290)
    dispatcher.prwait_step(cur)
    cur.execute("select payload from events where name='stoppage.opened'")
    assert "CI が起動しなかった" in cur.fetchone()["payload"]["what"]


def test_slot_full_is_not_idle(cur, monkeypatch):
    monkeypatch.setattr(dispatcher, "budget_paused", lambda cur: None)
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, enqueued_at, finished_at) "
                "values ('github:o/r#1','o/r',1,'implement','succeeded', now() - interval '2 hours', now() - interval '2 hours')")
    ev(cur, "supervisor.heartbeat", {"tickets_inflight": 1}, 0, corr=None)
    monkeypatch.setattr(dispatcher.line, "spec", lambda cur: {"tickets": 1})
    dispatcher.watch_once()
    assert stoppage.open_keys(cur, "idle:") == []
    assert len(stoppage.open_keys(cur, "slot:")) == 1
    ev(cur, "supervisor.heartbeat", {"tickets_inflight": 0}, 0, corr=None)   # 枠が空いた
    dispatcher.watch_once()
    assert stoppage.open_keys(cur, "slot:") == []
    assert len(stoppage.open_keys(cur, "idle:")) == 1
