"""CHANGELOG の断片の取りまとめの Issue（ADR 0027 の 1）"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    import psycopg
    from psycopg.rows import dict_row

    import supervisor

JST = timezone(timedelta(hours=9))


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def test_due():
    morning, evening = datetime(2026, 11, 3, 9, tzinfo=JST), datetime(2026, 11, 3, 16, tzinfo=JST)
    assert not supervisor.changelog_due(0, evening, False)
    assert not supervisor.changelog_due(3, morning, False)
    assert supervisor.changelog_due(10, morning, True)
    assert supervisor.changelog_due(1, evening, False)
    assert not supervisor.changelog_due(1, evening, True)


def test_files_once_per_hour_and_skips_open_issue(cur, monkeypatch):
    calls = []

    def fake_gh(method, path, body=None):
        calls.append((method, path))
        if path.endswith("contents/changelog.d"):
            return [{"type": "file", "name": f"{i}.md"} for i in range(10)] + [{"type": "file", "name": "README.md"}]
        if "issues?labels" in path:
            return open_issues
        return {"number": 9, "html_url": "https://github.com/o/r/issues/9"}

    open_issues = []
    monkeypatch.setattr(supervisor, "gh", fake_gh)
    monkeypatch.setattr(supervisor, "REPOS", ["o/r"])
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    now = datetime(2026, 11, 3, 1, tzinfo=timezone.utc)
    supervisor.changelog_step(cur, now)
    assert ("POST", "repos/o/r/issues") in calls
    calls.clear()
    supervisor.changelog_step(cur, now)   # 同じ時間には見ない
    assert calls == []
    open_issues = [{"number": 9}]
    supervisor.changelog_step(cur, now + timedelta(hours=1))   # 取りまとめの Issue が開いていれば起こさない
    assert ("POST", "repos/o/r/issues") not in calls
