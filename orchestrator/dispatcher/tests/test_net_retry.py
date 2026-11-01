"""一時的なネットワークの失敗（名前解決など）で落ちたジョブを、同じ入力のまま積み直す（11-01 job 1350・1640 ほか）。

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
                 "OBJECTSTORE_ACCESS_KEY": "x", "OBJECTSTORE_SECRET_KEY": "x"}.items():
        os.environ.setdefault(k, v)
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import dispatcher

REPO = "o/r"
CORR = f"github:{REPO}#514"


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def failed_job(cur, kind="body_fix"):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, enqueued_by) values (%s,%s,514,%s,'failed','supervisor') returning *",
                (CORR, REPO, kind))
    j = cur.fetchone()
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                (CORR, j["id"], Jsonb({"by": "supervisor", "input": {"pr_url": "https://github.com/o/r/pull/514"}})))
    return j


def test_detects_transient_markers():
    assert dispatcher.net_transient(["fatal: unable to access 'https://github.com/x.git/': Could not resolve host: github.com\n"])
    assert not dispatcher.net_transient(["error: pathspec 'x' did not match\n"])


def test_requeues_with_same_input_and_caps(cur):
    j = failed_job(cur)
    assert dispatcher.requeue_transient(cur, j)
    cur.execute("select id, kind, enqueued_by, attempt from jobs where id<>%s", (j["id"],))
    new = cur.fetchone()
    assert (new["kind"], new["enqueued_by"], new["attempt"]) == ("body_fix", "net_retry", j["attempt"] + 1)
    cur.execute("select payload from events where name='job.enqueued' and job_id=%s", (new["id"],))
    p = cur.fetchone()["payload"]
    assert p["by"] == "net_retry" and p["previous"] == j["id"] and p["input"]["pr_url"].endswith("/514")
    # 上限まで積んだら、それ以上は積まず人の調査（停止）に任せる
    for _ in range(dispatcher.NET_RETRY_MAX - 1):
        cur.execute("update jobs set state='failed' where enqueued_by='net_retry'")
        assert dispatcher.requeue_transient(cur, j)
    cur.execute("update jobs set state='failed' where enqueued_by='net_retry'")
    assert not dispatcher.requeue_transient(cur, j)


def test_does_not_double_enqueue_when_active_job_exists(cur):
    j = failed_job(cur)
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state) values (%s,%s,514,'implement','queued')", (CORR, REPO))
    assert not dispatcher.requeue_transient(cur, j)
