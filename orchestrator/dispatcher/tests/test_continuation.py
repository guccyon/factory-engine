"""続きのジョブ（ADR 0017）: CI 落ち・検査の指摘で同じ PR の続きを積む。合図ごとの上限、直している間は人に回さないこと、
積み直し（再起動・割り込み）で続ける先と合図の中身を引き継ぐこと、直せなかった指摘は差し戻しに戻ること。

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
    for k, v in {"DATABASE_URL": URL, "GH_TOKEN": "x", "CLAUDE_CODE_OAUTH_TOKEN": "x", "OBJECTSTORE_ENDPOINT": "localhost:1",
                 "OBJECTSTORE_ACCESS_KEY": "x", "OBJECTSTORE_SECRET_KEY": "x"}.items():
        os.environ.setdefault(k, v)
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import continuation
    import dispatcher
    import ops
    import stoppage
    import supervisor

REPO = "o/r"
BRANCH = "factory/issue-249-job-273"
P = {"repo": REPO, "number": 256, "url": f"https://github.com/{REPO}/pull/256", "corr": f"github:{REPO}#249"}


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def pr(sha="abc", ref=BRANCH, repo=REPO):
    return {"head": {"ref": ref, "sha": sha, "repo": {"full_name": repo}}}


def ci_failed(cur, sha):
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('pr.ci_failed',%s,%s,%s)",
                (P["corr"], Jsonb({"pr_url": P["url"], "head_sha": sha, "failed": ["backend"]}), f"pr.ci_failed:{P['url']}@{sha}"))


def continues(cur):
    cur.execute("select j.id, j.issue_number, j.kind, e.payload from jobs j join events e on e.job_id=j.id and e.name='job.enqueued' "
                "where e.payload ? 'continue' order by j.id")
    return cur.fetchall()


def finish_all(cur, state="failed"):
    cur.execute("update jobs set state=%s where state in ('queued','running')", (state,))


def test_ci_failed_enqueues_with_signal_and_holds_back_the_stoppage(cur):
    ops.upsert_ticket(cur, "github", f"{REPO}#249", REPO, title="D61", priority=70)
    ci_failed(cur, "abc")
    supervisor.ci_fix_step(cur, P, pr())
    supervisor.ci_fix_step(cur, P, pr())   # 同じ head では二度積まない
    rows = continues(cur)
    assert len(rows) == 1
    p = rows[0]["payload"]
    assert (rows[0]["issue_number"], rows[0]["kind"]) == (249, "implement")
    assert p["continue"] == {"branch": BRANCH, "pr_number": 256, "reason": "ci_failed"}
    assert p["input"] == {"head_sha": "abc", "failed": ["backend"]}
    # 直している間は停止（ci:）を開かない（人に回さない）
    supervisor.ci_failure_step(cur, P, "abc")
    assert stoppage.open_keys(cur, "ci:") == []
    # 直せなかったら開く
    finish_all(cur)
    supervisor.ci_failure_step(cur, P, "abc")
    assert stoppage.open_keys(cur, "ci:") == [f"ci:{P['url']}"]


def test_ci_failed_limit_per_pr(cur):
    for sha in ("a1", "a2", "a3"):
        ci_failed(cur, sha)
        supervisor.ci_fix_step(cur, P, pr(sha=sha))
        finish_all(cur, "succeeded")
    assert len(continues(cur)) == continuation.CONTINUE_MAX == 2
    supervisor.ci_failure_step(cur, P, "a3")
    cur.execute("select waiting_for from stoppages where key=%s", (f"ci:{P['url']}",))
    assert "2 回" in cur.fetchone()["waiting_for"]


def test_no_ci_failed_event_no_job(cur):
    supervisor.ci_fix_step(cur, P, pr())
    assert continues(cur) == []


def test_conflict_is_not_capped(cur):
    for sha in ("c1", "c2", "c3"):
        supervisor.conflict_step(cur, P, pr(sha=sha))
        finish_all(cur, "succeeded")
    assert [r["payload"]["continue"]["reason"] for r in continues(cur)] == ["conflict"] * 3


def test_active_job_leaves_no_record_so_next_cycle_retries(cur):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, state) values (%s,%s,249,'running')", (P["corr"], REPO))
    ci_failed(cur, "abc")
    supervisor.ci_fix_step(cur, P, pr())
    assert continues(cur) == []
    finish_all(cur, "succeeded")
    supervisor.ci_fix_step(cur, P, pr())
    assert len(continues(cur)) == 1


def test_rejects_non_factory_branches_and_forks(cur):
    ci_failed(cur, "abc")
    supervisor.ci_fix_step(cur, P, pr(ref="claude/some-branch"))
    supervisor.ci_fix_step(cur, P, pr(repo="someone/fork"))
    assert continuation.enqueue(cur, repo=REPO, pr_number=256, pr_url=P["url"], branch="factory/issue-249-job-1; rm -rf /",
                                head_sha="abc", reason="ci_failed", by="t") is None
    assert continues(cur) == []


def test_carry_over_keeps_target_and_signal(cur):
    jid = continuation.enqueue(cur, repo=REPO, pr_number=256, pr_url=P["url"], branch=BRANCH, head_sha="abc", reason="review_fix",
                               by="judge", signal={"defects": [{"rubric_id": "R1"}]})
    c = continuation.carry_over(cur, jid)
    assert c["continue"]["reason"] == "review_fix" and c["input"] == {"defects": [{"rubric_id": "R1"}]}
    assert c["pr_url"] == P["url"] and c["head_sha"] == "abc"


def test_continue_target_accepts_new_reasons(cur):
    jid = continuation.enqueue(cur, repo=REPO, pr_number=256, pr_url=P["url"], branch=BRANCH, head_sha="abc", reason="ci_failed",
                               by="t", signal={"failed": []})
    assert dispatcher.continue_target(cur, jid, REPO, 249) == {"branch": BRANCH, "pr_number": 256, "reason": "ci_failed"}
    assert dispatcher.continue_target(cur, jid, REPO, 250) is None   # Issue の番号が合わない


def test_volume_name():
    assert continuation.volume_name("guccyon/rally", 249) == "factory-ticket-guccyon-rally-249"
    assert continuation.volume_name("o/r x", 1) == "factory-ticket-o-r-x-1"


class FakeGH:
    def __init__(self, state="open", repo=REPO):
        self.state, self.repo, self.posted, self.comments = state, repo, [], []

    def gh(self, method, path, body=None):
        if method == "GET" and "/pulls/" in path:
            return {"state": self.state, "title": "D61", "head": {"ref": BRANCH, "sha": "abc", "repo": {"full_name": self.repo}}}
        if method == "POST" and path.endswith("/issues"):
            self.posted.append(body)
            return {"html_url": f"https://github.com/{REPO}/issues/900", "number": 900}
        return {}


@pytest.fixture()
def fake_gh(monkeypatch):
    f = FakeGH()
    monkeypatch.setattr(dispatcher, "gh", f.gh)
    monkeypatch.setattr(dispatcher, "comment", lambda repo, n, text: f.comments.append(text))
    monkeypatch.setattr(dispatcher, "swap_label", lambda *a, **k: None)
    return f


DEFECT = {"rubric_id": "R1", "severity": "major", "location": "a.rs:3", "evidence": "x", "remediation_hint": "y"}


def summary(defects=(DEFECT,), replaced=()):
    return {"adopted_defects": list(defects), "replaced": list(replaced), "escalated": [], "min_confidence": 0.9}


def test_review_fix_replaces_sendback_before_merge(cur, fake_gh):
    dispatcher.act_on_judgment(cur, REPO, P["corr"], None, P["url"], summary(), "unattended")
    rows = continues(cur)
    assert len(rows) == 1 and rows[0]["payload"]["continue"]["reason"] == "review_fix"
    assert rows[0]["payload"]["input"]["defects"][0]["rubric_id"] == "R1"
    assert fake_gh.posted == []   # 差し戻しの Issue は切らない
    # 直している間は検査役が承認しない
    assert continuation.pending(cur, P["url"], "abc", "review_fix")


def test_replaced_provisional_still_goes_to_sendback(cur, fake_gh):
    rep = {"category": "ui", "question": "q", "replacement": "r", "interpretation": "i"}
    dispatcher.act_on_judgment(cur, REPO, P["corr"], None, P["url"], summary(defects=(), replaced=(rep,)), "unattended")
    assert continues(cur) == [] and len(fake_gh.posted) == 1


def test_review_fix_over_limit_falls_back_to_sendback(cur, fake_gh):
    for sha in ("x1", "x2"):
        continuation.enqueue(cur, repo=REPO, pr_number=256, pr_url=P["url"], branch=BRANCH, head_sha=sha, reason="review_fix", by="t", signal={})
        finish_all(cur, "succeeded")
    dispatcher.act_on_judgment(cur, REPO, P["corr"], None, P["url"], summary(), "unattended")
    assert len(continues(cur)) == 2 and len(fake_gh.posted) == 1
    cur.execute("select 1 from events where name='continue.limit_reached'")
    assert cur.fetchone()


def test_closed_pr_falls_back_to_sendback(cur, fake_gh):
    fake_gh.state = "closed"
    dispatcher.act_on_judgment(cur, REPO, P["corr"], None, P["url"], summary(), "unattended")
    assert continues(cur) == [] and len(fake_gh.posted) == 1


def test_failed_review_fix_job_files_sendback(cur, fake_gh):
    dispatcher.file_sendback(cur, REPO, P["corr"], None, P["url"], [DEFECT], [], "attended", fallback_from=42)
    cur.execute("select payload from events where name='sendback.filed'")
    assert cur.fetchone()["payload"]["fallback_from_job"] == 42
