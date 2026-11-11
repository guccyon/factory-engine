"""PR の見張り（pr_events.py、ADR 0015）: 起動に失敗したワークフロー（ジョブ 0 本で失敗）を「CI が通った」と見なさず、1 回だけ再実行する"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pr_events  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
P = {"repo": "o/r", "number": 7, "url": "https://github.com/o/r/pull/7", "corr": "github:o/r#1"}


@pytest.fixture()
def cur():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def snap(workflows, runs=None):
    runs = runs if runs is not None else [{"name": "diagnose", "status": "completed", "conclusion": "success", "suite": 1}]
    return {"state": "open", "draft": False, "head_sha": "abc", "mergeable_state": "blocked", "runs": runs, "workflows": workflows, "reviews": []}


def wf(id_, name, suite, conclusion="failure", attempt=1, status="completed"):
    return {"id": id_, "name": name, "status": status, "conclusion": conclusion, "attempt": attempt, "suite": suite}


def names(cur):
    cur.execute("select name, payload from events order by id")
    return [(r["name"], r["payload"]) for r in cur.fetchall()]


def test_unstarted_workflow_is_rerun_once_and_not_passed(cur, monkeypatch):
    posts = []
    monkeypatch.setattr(pr_events, "gh", lambda m, path, body=None: posts.append((m, path)))
    monkeypatch.setattr(pr_events, "DRY_RUN", False)
    s = snap([wf(10, "diagnose", 1, "success"), wf(11, "backend", 2)])
    monkeypatch.setattr(pr_events, "snapshot", lambda repo, n: s)
    out = pr_events.observe(cur, P, "poll")
    assert "pr.ci_rerun" in out and "pr.ci_passed" not in out and "pr.ci_failed" not in out
    assert posts == [("POST", "repos/o/r/actions/runs/11/rerun")]
    pr_events.observe(cur, P, "poll")   # 再実行を頼んだ後、GitHub の試行がまだ 1 のままでも頼み直さない
    assert len(posts) == 1
    assert [n for n, _ in names(cur)].count("pr.ci_rerun") == 1


def test_still_unstarted_after_rerun_is_failed(cur, monkeypatch):
    monkeypatch.setattr(pr_events, "gh", lambda *a, **k: pytest.fail("頼み直さない"))
    monkeypatch.setattr(pr_events, "snapshot", lambda repo, n: snap([wf(11, "backend", 2, attempt=2)]))
    out = pr_events.observe(cur, P, "poll")
    assert out == ["pr.head_updated", "pr.ci_failed"]
    failed = [p for n, p in names(cur) if n == "pr.ci_failed"][0]["failed"]
    assert failed == ["backend（起動できなかった）"]
    cur.execute("select count(*) as n from events where name='defect.recorded'")
    assert cur.fetchone()["n"] == 0   # 起動の失敗は実装の Defect ではない


def test_pending_workflow_holds_back_passed_and_all_started_passes(cur, monkeypatch):
    monkeypatch.setattr(pr_events, "snapshot", lambda repo, n: snap([wf(10, "diagnose", 1, "success"), wf(11, "backend", 2, None, status="queued")]))
    assert "pr.ci_passed" not in pr_events.observe(cur, P, "poll")
    runs = [{"name": "diagnose", "status": "completed", "conclusion": "success", "suite": 1},
            {"name": "backend", "status": "completed", "conclusion": "success", "suite": 2}]
    monkeypatch.setattr(pr_events, "snapshot", lambda repo, n: snap([wf(10, "diagnose", 1, "success"), wf(11, "backend", 2, "success", attempt=2)], runs))
    assert "pr.ci_passed" in pr_events.observe(cur, P, "poll")
