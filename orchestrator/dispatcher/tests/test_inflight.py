"""同時上限に数えるチケットの数（supervisor.inflight_count）。PR の直しの続きのジョブを、PR と二重に数えない（11-02 朝）。

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

    import supervisor


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def job(cur, issue, state="queued", kind="implement"):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, enqueued_by) values (%s,'o/r',%s,%s,%s,'supervisor')",
                (f"github:o/r#{issue}", issue, kind, state))


def test_continuation_job_on_open_pr_counts_once(cur):
    job(cur, 1, "running")            # PR #1 の衝突の直し
    job(cur, 2, "queued")             # 新しいタスク
    job(cur, 3, "succeeded")          # 終わったジョブは数えない
    assert supervisor.inflight_count(cur, {"github:o/r#1", "github:o/r#4"}) == 3   # #1・#2・#4


def test_job_for_pr_waiting_on_human_still_counts(cur):
    job(cur, 5, "running")            # 人を待つ PR（open_prs に入らない）の直しは、動いている間だけ数える
    assert supervisor.inflight_count(cur, set()) == 1
