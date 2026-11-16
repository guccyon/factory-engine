"""マージを見届けた工場の PR の作業ブランチを消す（supervisor.delete_merged_branch）。

2026-11-16 rally: リポ設定の自動削除が無効で、マージ済みの工場のブランチ（factory/issue-N-job-M）が 659 本溜まっていた。
製品のリポの設定には頼らず、工場が pr.merged を記録した時点で自分のブランチを消す。

  TEST_DATABASE_URL=postgresql://factory@localhost:5432/factory_test python -m pytest orchestrator/dispatcher/tests
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    for k, v in {"DATABASE_URL": URL, "GH_TOKEN": "x", "NOTIFIER": "log"}.items():
        os.environ.setdefault(k, v)
    import psycopg
    from psycopg.rows import dict_row

    import supervisor

REPO = "o/r"
ISSUE, PR = 1500, 1510
PR_URL = f"https://github.com/{REPO}/pull/{PR}"
CORR = f"github:{REPO}#{ISSUE}"
BRANCH = f"factory/issue-{ISSUE}-job-42"
DELETE = ("DELETE", f"repos/{REPO}/git/refs/heads/{BRANCH}")


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


class FakeGitHub:
    def __init__(self):
        self.pr = {"state": "closed", "draft": False, "labels": [], "merged_at": "2026-11-16T12:00:00Z", "closed_at": "2026-11-16T12:00:00Z",
                   "merge_commit_sha": "m" * 40, "head": {"sha": "a" * 40, "ref": BRANCH, "repo": {"full_name": REPO}}}
        self.fail_delete = False
        self.calls = []

    def gh(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "GET" and path == f"repos/{REPO}/pulls/{PR}":
            return self.pr
        if method == "DELETE" and "/git/refs/heads/" in path and self.fail_delete:
            raise RuntimeError("HTTP 422 Reference does not exist")
        return {}


@pytest.fixture()
def gh(monkeypatch):
    f = FakeGitHub()
    monkeypatch.setattr(supervisor, "gh", f.gh)
    monkeypatch.setattr(supervisor, "REPOS", [REPO])
    monkeypatch.setattr(supervisor, "factory_prs", lambda cur: [{"repo": REPO, "number": PR, "url": PR_URL, "corr": CORR}])
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    return f


def tick(cur):
    supervisor.merge_step(cur, {}, {REPO: {}})


def events(cur, name):
    cur.execute("select count(*) as n from events where name=%s", (name,))
    return cur.fetchone()["n"]


def test_merged_factory_branch_is_deleted_once(cur, gh):
    tick(cur)
    assert events(cur, "pr.merged") == 1
    assert gh.calls.count(DELETE) == 1
    tick(cur)   # 記録済みなので二度は消しに行かない
    assert gh.calls.count(DELETE) == 1


def test_closed_unmerged_branch_is_kept(cur, gh):
    gh.pr["merged_at"] = None
    tick(cur)
    assert events(cur, "pr.closed_unmerged") == 1
    assert DELETE not in gh.calls


@pytest.mark.parametrize("head", [
    {"sha": "a" * 40, "ref": "fix-by-hand", "repo": {"full_name": REPO}},          # 人のブランチ
    {"sha": "a" * 40, "ref": BRANCH, "repo": {"full_name": "someone/fork"}},       # フォークから
    {"sha": "a" * 40, "ref": BRANCH, "repo": None},                                # フォークが消えている
])
def test_non_factory_branch_is_kept(cur, gh, head):
    gh.pr["head"] = head
    tick(cur)
    assert events(cur, "pr.merged") == 1
    assert not [c for c in gh.calls if c[0] == "DELETE"]


def test_delete_failure_does_not_stop_the_line(cur, gh):
    gh.fail_delete = True   # リポ設定の自動削除で先に消えていた、など
    tick(cur)
    assert events(cur, "pr.merged") == 1
    assert gh.calls.count(DELETE) == 1


def test_dry_run_does_not_delete(cur, gh, monkeypatch):
    monkeypatch.setattr(supervisor, "DRY_RUN", True)
    tick(cur)
    assert DELETE not in gh.calls
