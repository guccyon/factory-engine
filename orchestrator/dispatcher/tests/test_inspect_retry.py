"""検査の積み忘れ（2026-11-05 rally #976）。同じ PR の別のジョブと重なって積めなかった検査を、supervisor が次の周回で積み直すこと、
番人が「最新の head に検査がないまま」の PR を停止 inspect: として開き、検査が積まれたら閉じること。

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
    import supervisor

PR_URL = "https://github.com/o/r/pull/976"
CORR = "github:o/r#970"
SHA = "a" * 40


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


@pytest.fixture()
def gh_calls(monkeypatch):
    calls = []

    def fake_gh(method, path, body=None):
        calls.append((method, path))
        if method == "GET" and path.endswith("/pulls/976"):
            return {"state": "open", "draft": False, "mergeable_state": "clean", "labels": [],
                    "head": {"sha": SHA, "ref": "factory/970", "repo": {"full_name": "o/r"}}}
        return {}

    monkeypatch.setattr(supervisor, "gh", fake_gh)
    monkeypatch.setattr(supervisor, "factory_prs", lambda cur: [{"repo": "o/r", "number": 976, "url": PR_URL, "corr": CORR}])
    monkeypatch.setattr(supervisor, "pr_files", lambda repo, n: [])
    monkeypatch.setattr(supervisor, "classify", lambda files, rules: [])
    monkeypatch.setattr(supervisor, "decide", lambda types, manifest: {"table": "auto", "factory_grant": "auto", "change_types": [], "execute": True})
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    monkeypatch.setattr(supervisor, "INSPECT_ENABLED", True)
    return calls


def jobs(cur, kind):
    cur.execute("select id, state from jobs where repo='o/r' and issue_number=976 and kind=%s", (kind,))
    return cur.fetchall()


def test_inspect_is_retried_after_other_job_of_same_pr_finishes(cur, gh_calls):
    # 同じ PR の本文の直しが動いている間に、マージの判定が付く（rally #976）
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, enqueued_by) values (%s,'o/r',976,'body_fix','running','supervisor') returning id",
                (CORR,))
    fix = cur.fetchone()["id"]
    supervisor.merge_step(cur, {}, {"o/r": {}})
    assert jobs(cur, "inspect") == []   # 一意制約で積めない
    cur.execute("select 1 from events where dedupe_key=%s", (f"supervisor.inspect:{PR_URL}@{SHA}",))
    assert cur.fetchone() is None       # 記録は消して次の周回に回す
    cur.execute("select count(*) as n from events where name='supervisor.merge_decided'")
    assert cur.fetchone()["n"] == 1     # 判定は記録済み（以前はこの記録のせいで二度と検査を試さなかった）

    supervisor.merge_step(cur, {}, {"o/r": {}})   # まだ動いている間は積めないまま
    assert jobs(cur, "inspect") == []

    cur.execute("update jobs set state='succeeded', finished_at=now() where id=%s", (fix,))
    supervisor.merge_step(cur, {}, {"o/r": {}})
    assert [j["state"] for j in jobs(cur, "inspect")] == ["queued"]
    cur.execute("select 1 from events where dedupe_key=%s", (f"supervisor.inspect:{PR_URL}@{SHA}",))
    assert cur.fetchone() is not None

    supervisor.merge_step(cur, {}, {"o/r": {}})   # 積めた後は head ごとに 1 回だけ
    assert len(jobs(cur, "inspect")) == 1
    assert not any(m == "PUT" for m, _ in gh_calls)   # 検査が終わるまでマージしない


def decided(cur, sha=SHA, ago_min=60):
    cur.execute("insert into events(name, correlation_id, payload, occurred_at) values ('supervisor.merge_decided',%s,%s, now() - make_interval(mins => %s))",
                (CORR, Jsonb({"pr_url": PR_URL, "head_sha": sha, "table": "auto", "execute": True}), ago_min))


def test_watchdog_opens_and_closes_inspect_missing(cur, monkeypatch):
    monkeypatch.setattr(dispatcher, "INSPECT_ENABLED", True)
    decided(cur, ago_min=5)
    dispatcher.inspect_missing_step(cur)
    assert stoppage.open_keys(cur, "inspect:") == []   # 待ちが短いうちは開かない

    cur.execute("truncate events restart identity cascade")
    decided(cur, ago_min=60)
    dispatcher.inspect_missing_step(cur)
    assert stoppage.open_keys(cur, "inspect:") == [f"inspect:{PR_URL}"]

    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('supervisor.inspect_enqueued',%s,%s,%s)",
                (CORR, Jsonb({"pr_url": PR_URL, "head_sha": SHA}), f"supervisor.inspect:{PR_URL}@{SHA}"))
    dispatcher.inspect_missing_step(cur)
    assert stoppage.open_keys(cur, "inspect:") == []


def test_watchdog_ignores_moved_head_and_finished_pr(cur, monkeypatch):
    monkeypatch.setattr(dispatcher, "INSPECT_ENABLED", True)
    decided(cur, ago_min=60)
    cur.execute("insert into events(name, correlation_id, payload) values ('pr.head_updated',%s,%s)",
                (CORR, Jsonb({"pr_url": PR_URL, "head_sha": "b" * 40})))
    dispatcher.inspect_missing_step(cur)
    assert stoppage.open_keys(cur, "inspect:") == []   # 新しい head は CI を通ってから判定し直す

    cur.execute("truncate events restart identity cascade")
    decided(cur, ago_min=60)
    cur.execute("insert into events(name, correlation_id, payload) values ('pr.merged',%s,%s)", (CORR, Jsonb({"pr_url": PR_URL})))
    dispatcher.inspect_missing_step(cur)
    assert stoppage.open_keys(cur, "inspect:") == []
