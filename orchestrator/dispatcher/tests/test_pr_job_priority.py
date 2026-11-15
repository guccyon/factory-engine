"""PR に紐づけて積む検査・judge が、もとのチケットの優先度を引き継ぐこと（#523）。
引き継がないと既定の 50 で積まれ、p1 の implement に抜かれ続けて、p0 の直しの PR が判定を何十分も待った。

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

    import supervisor

PR = {"repo": "o/r", "number": 976, "url": "https://github.com/o/r/pull/976", "corr": "github:o/r#970"}
SHA = "a" * 40


@pytest.fixture()
def cur(monkeypatch):
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    monkeypatch.setattr(supervisor, "INSPECT_ENABLED", True)
    yield c
    conn.close()


def _ticket(cur, ref: str, priority: int):
    cur.execute("insert into tickets(source, source_ref, repo, title, state, priority) values ('github', %s, 'o/r', 't', 'active', %s)",
                (ref, priority))


def _inspect_priority(cur) -> int:
    cur.execute("select priority, ticket_id from jobs where kind='inspect'")
    row = cur.fetchone()
    assert row["ticket_id"] is None   # 費用の付け先は変えない
    return row["priority"]


def test_inspect_takes_ticket_priority(cur):
    _ticket(cur, "o/r#970", 100)
    supervisor.enqueue_inspect(cur, PR, SHA)
    assert _inspect_priority(cur) == 100


def test_inspect_without_ticket_keeps_default(cur):
    _ticket(cur, "o/r#1", 100)   # ほかのチケットの優先度は拾わない
    supervisor.enqueue_inspect(cur, PR, SHA)
    assert _inspect_priority(cur) == 50


def test_pr_job_priority(cur):
    _ticket(cur, "o/r#970", 75)
    assert supervisor.pr_job_priority(cur, "github:o/r#970") == 75
    assert supervisor.pr_job_priority(cur, "github:o/r#971") == 50
