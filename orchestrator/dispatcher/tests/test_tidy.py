"""後片付け（ADR 0021）: 済んだチケットのボリュームをそのままオブジェクトストアに置いてから消すこと、
開いている PR と動いているジョブは待つこと、置けなければ消さないこと、期限の片付けと二重に消さないこと。

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
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import continuation
    import tidy

REPO = "o/r"
CORR = f"github:{REPO}#249"
PR_URL = f"https://github.com/{REPO}/pull/256"
VOLUME = "factory-ticket-o-r-249"


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def saved(cur):
    cur.execute("insert into events(name, correlation_id, payload) values ('handoff.saved',%s,%s)", (CORR, Jsonb({"volume": VOLUME})))


def merged(cur, days_ago=0, name="pr.merged"):
    cur.execute("insert into events(name, correlation_id, payload, occurred_at) values (%s,%s,%s, now() - make_interval(days => %s))",
                (name, CORR, Jsonb({"pr_url": PR_URL}), days_ago))


class Run:
    """docker の呼び出しを記録する。archive_ok / remove_ok で失敗させる"""
    def __init__(self, archive_ok=True, remove_ok=True):
        self.calls, self.archive_ok, self.remove_ok = [], archive_ok, remove_ok

    def __call__(self, cmd, **k):
        self.calls.append(cmd)
        ok = self.archive_ok if cmd[:2] == ["docker", "run"] else self.remove_ok
        return type("R", (), {"returncode": 0 if ok else 1, "stderr": "", "stdout": b"TAR" if ok else b""})()

    def removes(self):
        return [c for c in self.calls if c[:3] == ["docker", "volume", "rm"]]


@pytest.fixture()
def run(monkeypatch):
    r = Run()
    monkeypatch.setattr(tidy.subprocess, "run", r)
    monkeypatch.setattr(continuation.subprocess, "run", r)
    return r


def events(cur, name):
    cur.execute("select payload from events where name=%s order by id", (name,))
    return [r["payload"] for r in cur.fetchall()]


def test_archives_whole_volume_then_removes(cur, run):
    puts = []
    saved(cur)
    merged(cur)
    assert tidy.run(cur, "img", lambda k, d: puts.append((k, d))) == 1
    assert puts[0][0].startswith("tickets/o/r/249/handoff-") and puts[0][0].endswith(".tar.gz") and puts[0][1] == b"TAR"
    assert run.calls[0][-5:] == ["czf", "-", "-C", "/h", "."] and f"{VOLUME}:/h:ro" in run.calls[0]   # 選り分けずに読み取りだけで固める
    assert run.removes() == [["docker", "volume", "rm", "-f", VOLUME]]
    archived = events(cur, "handoff.archived")[0]
    assert archived["outcome"] == "merged" and archived["bytes"] == 3 and archived["pr_url"] == PR_URL
    assert events(cur, "handoff.removed")[0]["why"] == "tidy"
    assert tidy.run(cur, "img", lambda k, d: puts.append(k)) == 0   # 片付けたら二度はしない
    assert continuation.cleanup(cur) == 0   # 期限の片付けも、片付け済みは数えない


def test_closed_without_merge_is_tidied_too(cur, run):
    saved(cur)
    merged(cur, name="pr.closed_unmerged")
    assert tidy.run(cur, "img", lambda k, d: None) == 1
    assert events(cur, "handoff.archived")[0]["outcome"] == "closed_unmerged"


def test_waits_for_open_pr_and_running_jobs(cur, run):
    saved(cur)
    assert tidy.run(cur, "img", lambda k, d: None) == 0   # PR がまだ開いている
    merged(cur)
    cur.execute("insert into jobs(correlation_id, repo, issue_number, state) values (%s,%s,249,'running')", (CORR, REPO))
    assert tidy.run(cur, "img", lambda k, d: None) == 0   # 同じチケットのジョブが動いている（続きの直し）
    assert run.calls == []


def test_keeps_volume_when_archive_or_put_fails(cur, monkeypatch):
    r = Run(archive_ok=False)
    monkeypatch.setattr(tidy.subprocess, "run", r)
    saved(cur)
    merged(cur)
    assert tidy.run(cur, "img", lambda k, d: None) == 0
    def broken(k, d):
        raise OSError("store down")
    r.archive_ok = True
    assert tidy.run(cur, "img", broken) == 0
    assert r.removes() == [] and events(cur, "handoff.archived") == []
    assert tidy.run(cur, "img", lambda k, d: None) == 1   # 次の回で置ければ消す


def test_does_not_archive_twice_when_remove_failed(cur, monkeypatch):
    r = Run(remove_ok=False)
    monkeypatch.setattr(tidy.subprocess, "run", r)
    puts = []
    saved(cur)
    merged(cur)
    assert tidy.run(cur, "img", lambda k, d: puts.append(k)) == 0
    r.remove_ok = True
    assert tidy.run(cur, "img", lambda k, d: puts.append(k)) == 1
    assert len(puts) == 1 and len(events(cur, "handoff.archived")) == 1   # 置き直さず、消すだけ


def test_new_volume_on_same_ticket_is_tidied_again(cur, run):
    saved(cur)
    merged(cur)
    assert tidy.run(cur, "img", lambda k, d: None) == 1
    saved(cur)   # マージ後に同じチケットで続きが動き、またボリュームができた
    assert tidy.run(cur, "img", lambda k, d: None) == 1
    assert len(events(cur, "handoff.archived")) == 2 and len(events(cur, "handoff.removed")) == 2


def test_disabled(cur, run, monkeypatch):
    monkeypatch.setattr(tidy, "TIDY_ENABLED", False)
    saved(cur)
    merged(cur)
    assert tidy.run(cur, "img", lambda k, d: None) == 0 and run.calls == []
