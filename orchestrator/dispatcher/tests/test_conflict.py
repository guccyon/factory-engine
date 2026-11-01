"""衝突した工場の PR に、そのブランチで衝突を解く実装ジョブを積む（supervisor.conflict_step）。

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
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    import psycopg
    from psycopg.rows import dict_row

    import ops
    import supervisor

REPO = "o/r"


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def pr(ref="factory/issue-249-job-273", sha="abc", repo=REPO):
    return {"head": {"ref": ref, "sha": sha, "repo": {"full_name": repo}}}


P = {"repo": REPO, "number": 256, "url": f"https://github.com/{REPO}/pull/256", "corr": f"github:{REPO}#249"}


def enqueued(cur):
    cur.execute("select j.id, j.issue_number, j.kind, j.ticket_id, j.priority, e.payload->'continue' as c from jobs j "
                "join events e on e.job_id=j.id and e.name='job.enqueued' order by j.id")
    return cur.fetchall()


def test_enqueues_once_per_head(cur):
    t = ops.upsert_ticket(cur, "github", f"{REPO}#249", REPO, title="D61", priority=70)
    supervisor.conflict_step(cur, P, pr())
    supervisor.conflict_step(cur, P, pr())   # 同じ head では二度積まない
    rows = enqueued(cur)
    assert len(rows) == 1
    r = rows[0]
    assert (r["issue_number"], r["kind"], r["ticket_id"], r["priority"]) == (249, "implement", t["id"], 70)
    assert r["c"] == {"branch": "factory/issue-249-job-273", "pr_number": 256, "reason": "conflict"}
    cur.execute("update jobs set state='failed'")
    supervisor.conflict_step(cur, P, pr(sha="def"))   # 次の head ではまた積む
    assert len(enqueued(cur)) == 2


def test_skips_non_factory_branch_and_forks(cur):
    supervisor.conflict_step(cur, P, pr(ref="claude/some-branch"))
    supervisor.conflict_step(cur, P, pr(repo="someone/fork"))
    supervisor.conflict_step(cur, P, pr(ref="factory/issue-249-job-273; rm -rf /"))
    assert enqueued(cur) == []


def test_active_job_on_issue_blocks(cur):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, state) values (%s,%s,249,'running')", (f"github:{REPO}#249", REPO))
    supervisor.conflict_step(cur, P, pr())
    assert [r for r in enqueued(cur)] == []


def test_waits_while_human_decision_pending(cur):
    # 人の判断待ち（ラベル・judge の停止）の間は解かない。待ちが外れたら、その時点の head で 1 回だけ解く（11-01 rally #505）
    ops.upsert_ticket(cur, "github", f"{REPO}#249", REPO, title="D61", priority=70)
    labeled = {**pr(), "labels": [{"name": "factory:needs-judgment"}]}
    supervisor.conflict_step(cur, P, labeled)
    assert enqueued(cur) == []
    import stoppage
    stoppage.open_stoppage(cur, f"judge:{P['url']}", "human", "judge が人に回した", correlation_id=P["corr"], detected_by="supervisor")
    supervisor.conflict_step(cur, P, pr(sha="def"))
    assert enqueued(cur) == []
    stoppage.close_stoppage(cur, f"judge:{P['url']}", remedy="人が判断した", closed_by="supervisor")
    supervisor.conflict_step(cur, P, pr(sha="def"))
    assert len(enqueued(cur)) == 1
