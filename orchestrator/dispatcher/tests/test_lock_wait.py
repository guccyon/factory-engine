"""同じチケットの錠を待つジョブ（ADR 0016・0017）と、続きのジョブの積み直し（factoryctl rerun）。

続きのジョブ（衝突・CI 落ち・指摘・途中終了）は同じ head に 1 回しか積まれない。前のジョブが状態を書いた後の後処理の間に
錠を握っていても、失敗にせず積み直して待つ。待ち切れなければ番人が停止として開く error にする。
rerun は続きのジョブを、続ける先と合図の中身を引き継いだ続きとして積み直す（最初からの実装にして PR を 2 本にしない）。

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

    import continuation
    import dispatcher
    import factoryctl

REPO = "o/r"
ISSUE = 374
BRANCH = f"factory/issue-{ISSUE}-job-591"
PR_URL = f"https://github.com/{REPO}/pull/429"


@pytest.fixture()
def cur(monkeypatch):
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    monkeypatch.setattr(dispatcher.time, "sleep", lambda s: None)
    yield c
    conn.close()


@pytest.fixture()
def held():
    """同じチケットの前のジョブが後処理の間まだ錠を握っている状態（別の接続で錠を取る）"""
    conn = psycopg.connect(URL, autocommit=True)
    conn.execute("select pg_advisory_lock(hashtext(%s))", (f"{REPO}#{ISSUE}",))
    yield
    conn.close()


def claim(cur, jid):
    cur.execute("update jobs set state='running', started_at=now() where id=%s returning *", (jid,))
    return cur.fetchone()


def continuation_job(cur, reason="conflict", signal=None):
    return continuation.enqueue(cur, repo=REPO, pr_number=429, pr_url=PR_URL, branch=BRANCH, head_sha="abc", reason=reason,
                                by="supervisor", signal=signal)


def plain_job(cur):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, kind) values (%s,%s,%s,'poll','implement') returning id",
                (f"github:{REPO}#{ISSUE}", REPO, ISSUE))
    jid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                (f"github:{REPO}#{ISSUE}", jid, Jsonb({"by": "poll"})))
    return jid


def state(cur, jid):
    cur.execute("select state, error from jobs where id=%s", (jid,))
    return cur.fetchone()


def test_continuation_waits_for_lock_instead_of_failing(cur, held):
    jid = continuation_job(cur)
    dispatcher.run_job(claim(cur, jid))
    assert state(cur, jid) == {"state": "queued", "error": None}
    cur.execute("select count(*) as n from events where name='job.requeued' and job_id=%s", (jid,))
    assert cur.fetchone()["n"] == 1


@pytest.mark.parametrize("reason", ["conflict", "ci_failed", "review_fix", "incomplete"])
def test_every_continue_reason_waits(cur, held, reason):
    jid = continuation_job(cur, reason=reason, signal={"x": 1} if reason in continuation.SIGNALED else None)
    dispatcher.run_job(claim(cur, jid))
    assert state(cur, jid)["state"] == "queued"


def test_plain_implement_still_rejected(cur, held):
    jid = plain_job(cur)
    dispatcher.run_job(claim(cur, jid))
    assert state(cur, jid) == {"state": "failed", "error": "lock_held"}
    assert dispatcher.classify_failure({"error": "lock_held"}) is None   # 従来どおり停止には開かない


def test_gives_up_after_max_wait_and_opens_a_stoppage(cur, held):
    jid = continuation_job(cur)
    cur.execute("insert into events(name, correlation_id, job_id, payload, occurred_at) values ('job.requeued',%s,%s,%s, now() - make_interval(secs => %s))",
                (f"github:{REPO}#{ISSUE}", jid, Jsonb({"reason": "advisory_lock_held"}), dispatcher.LOCK_WAIT_MAX_SEC + 5))
    dispatcher.run_job(claim(cur, jid))
    assert state(cur, jid) == {"state": "failed", "error": "lock_held_timeout"}
    cur.execute("select payload from events where name='job.finished' and job_id=%s", (jid,))
    p = cur.fetchone()["payload"]
    assert p["error"] == "lock_held_timeout"
    assert dispatcher.classify_failure(p)[0] == "factory-bug"   # 番人が停止として開く


def test_rerun_keeps_a_continuation_a_continuation(cur, capsys):
    jid = continuation_job(cur, reason="review_fix", signal={"defects": [{"rubric_id": "R1"}]})
    cur.execute("update jobs set state='failed', error='lock_held' where id=%s", (jid,))
    assert factoryctl.main(["factoryctl", "rerun", str(jid)]) in (None, 0)
    cur.execute("select id from jobs where id > %s", (jid,))
    new_id = cur.fetchone()["id"]
    c = continuation.carry_over(cur, new_id)
    assert c["continue"] == {"branch": BRANCH, "pr_number": 429, "reason": "review_fix"}
    assert c["input"] == {"defects": [{"rubric_id": "R1"}]} and c["pr_url"] == PR_URL and c["head_sha"] == "abc"
    assert dispatcher.continue_target(cur, new_id, REPO, ISSUE)["branch"] == BRANCH
    assert "continue " + BRANCH in capsys.readouterr().out


def test_rerun_continue_of_a_continuation_uses_its_branch(cur):
    # 途中終了した続きのジョブを --continue で積み直しても、ブランチは元の工場のブランチ（失敗したジョブの番号で組まない）
    jid = continuation_job(cur, reason="conflict")
    cur.execute("update jobs set state='failed', error='incomplete(claude_exit=124)', result=%s where id=%s",
                (Jsonb({"pr_url": PR_URL, "pr_number": 429}), jid))
    factoryctl.main(["factoryctl", "rerun", str(jid), "--continue"])
    cur.execute("select id from jobs where id > %s", (jid,))
    assert continuation.carry_over(cur, cur.fetchone()["id"])["continue"]["branch"] == BRANCH


def test_rerun_of_a_plain_job_stays_plain(cur):
    jid = plain_job(cur)
    cur.execute("update jobs set state='failed', error='timeout' where id=%s", (jid,))
    factoryctl.main(["factoryctl", "rerun", str(jid)])
    cur.execute("select id from jobs where id > %s", (jid,))
    assert continuation.carry_over(cur, cur.fetchone()["id"]) == {}


def test_rerun_of_a_piped_job_carries_its_input(cur):
    # 本文の追記（body_fix）など、入力を job.enqueued の input で受け取るジョブは、積み直しでも同じ入力で走る（pr_url が無くて落ちない）
    inp = {"pr_url": PR_URL, "defects": [{"rubric_id": "R2"}]}
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, model_tier, kind, state, error) "
                "values (%s,%s,429,'judge','tier-small','body_fix','failed','exit 1') returning id", (f"github:{REPO}#{ISSUE}", REPO))
    jid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                (f"github:{REPO}#{ISSUE}", jid, Jsonb({"by": "judge", "kind": "body_fix", "input": inp})))
    factoryctl.main(["factoryctl", "rerun", str(jid)])
    cur.execute("select id, kind from jobs where id > %s", (jid,))
    new = cur.fetchone()
    assert new["kind"] == "body_fix"
    cur.execute("select payload->'input' as inp from events where name='job.enqueued' and job_id=%s", (new["id"],))
    assert cur.fetchone()["inp"] == inp
