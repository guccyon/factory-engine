"""起動に失敗したワークフロー（ジョブ 0 本で失敗）: PR の見張り（pr_events.py、ADR 0015）は記録だけし、CI が通ったと見なさない。
再実行は内ループの処理（supervisor.ci_rerun_step）が記録を読んで 1 回だけ頼む（ADR 0050 の決定 2 の形）"""
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
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    global supervisor
    import supervisor
    yield c
    conn.close()


def snap(workflows, runs=None):
    runs = runs if runs is not None else [{"name": "diagnose", "status": "completed", "conclusion": "success", "suite": 1}]
    return {"state": "open", "draft": False, "head_sha": "abc", "mergeable_state": "blocked", "runs": runs, "workflows": workflows, "reviews": []}


def wf(id_, name, suite, conclusion="failure", attempt=1, status="completed"):
    return {"id": id_, "name": name, "status": status, "conclusion": conclusion, "attempt": attempt, "suite": suite}


def events(cur, name):
    cur.execute("select payload from events where name=%s order by id", (name,))
    return [r["payload"] for r in cur.fetchall()]


def test_unstarted_is_recorded_not_passed_and_rerun_once_by_the_inner_loop(cur, monkeypatch):
    monkeypatch.setattr(pr_events, "gh", lambda *a, **k: pytest.fail("見張りは GitHub に書かない"))
    monkeypatch.setattr(pr_events, "snapshot", lambda repo, n: snap([wf(10, "diagnose", 1, "success"), wf(11, "backend", 2)]))
    out = pr_events.observe(cur, P, "poll")
    assert "pr.ci_unstarted" in out and "pr.ci_passed" not in out and "pr.ci_failed" not in out
    assert pr_events.observe(cur, P, "poll") == []   # 同じ事実は 1 回だけ
    posts = []
    monkeypatch.setattr(supervisor, "gh", lambda m, path, body=None: posts.append((m, path)))
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    assert supervisor.ci_rerun_step(cur, P, "abc") == ["backend"]
    assert supervisor.ci_rerun_step(cur, P, "abc") == []   # 頼んだ記録があれば頼み直さない
    assert posts == [("POST", "repos/o/r/actions/runs/11/rerun")]
    assert [e["run_id"] for e in events(cur, "pr.ci_rerun")] == [11]


def test_rerun_not_recorded_when_github_refuses(cur, monkeypatch):
    monkeypatch.setattr(pr_events, "snapshot", lambda repo, n: snap([wf(11, "backend", 2)]))
    pr_events.observe(cur, P, "poll")

    def refuse(*a, **k):
        raise RuntimeError("403")
    monkeypatch.setattr(supervisor, "gh", refuse)
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    assert supervisor.ci_rerun_step(cur, P, "abc") == []
    assert events(cur, "pr.ci_rerun") == []   # 次の周回でもう一度試す


def test_still_unstarted_after_rerun_is_failed(cur, monkeypatch):
    monkeypatch.setattr(pr_events, "snapshot", lambda repo, n: snap([wf(11, "backend", 2, attempt=2)]))
    out = pr_events.observe(cur, P, "poll")
    assert "pr.ci_failed" in out
    assert events(cur, "pr.ci_failed")[0]["failed"] == ["backend（起動できなかった）"]
    assert events(cur, "defect.recorded") == []   # 起動の失敗は実装の Defect ではない
    monkeypatch.setattr(supervisor, "gh", lambda *a, **k: pytest.fail("2 回目は頼まない"))
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    assert supervisor.ci_rerun_step(cur, P, "abc") == []


def test_pending_workflow_holds_back_passed_and_all_started_passes(cur, monkeypatch):
    monkeypatch.setattr(pr_events, "snapshot", lambda repo, n: snap([wf(10, "diagnose", 1, "success"), wf(11, "backend", 2, None, status="queued")]))
    assert "pr.ci_passed" not in pr_events.observe(cur, P, "poll")
    runs = [{"name": "diagnose", "status": "completed", "conclusion": "success", "suite": 1},
            {"name": "backend", "status": "completed", "conclusion": "success", "suite": 2}]
    monkeypatch.setattr(pr_events, "snapshot", lambda repo, n: snap([wf(10, "diagnose", 1, "success"), wf(11, "backend", 2, "success", attempt=2)], runs))
    assert "pr.ci_passed" in pr_events.observe(cur, P, "poll")
