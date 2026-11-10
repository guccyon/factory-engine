"""着手中なのに実態のないチケットを台帳から外す（ticket_sync.py、2026-11-10 オーナー）。GitHub は偽物で、DB は TEST_DATABASE_URL の Postgres"""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    import psycopg
    from psycopg.rows import dict_row

    import ticket_sync

OLD = datetime(2026, 10, 31, 15, 19, tzinfo=timezone.utc)


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def ticket(cur, n, state="active", job=None):
    cur.execute("insert into tickets(source, source_ref, repo, title, state, updated_at) values ('github', %s, 'o/r', %s, %s, %s)",
                (f"o/r#{n}", f"#{n}", state, OLD))
    if job:
        cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state) values (%s, 'o/r', %s, 'clarify', %s)", (f"github:o/r#{n}", n, job))


def states(cur):
    cur.execute("select source_ref, state, updated_at from tickets order by id")
    return {r["source_ref"]: (r["state"], r["updated_at"]) for r in cur.fetchall()}


def test_closes_active_tickets_whose_issue_closed(cur):
    ticket(cur, 1, job="succeeded")   # 窓口の確認だけで終わり、Issue も閉じた → 閉じる
    ticket(cur, 2, job="succeeded")   # Issue がまだ開いている → 残す（レポートの「動いていない」に出る）
    ticket(cur, 3, job="queued")      # ジョブが積まれている → 見ない
    ticket(cur, 4, job="succeeded")   # GitHub が読めない → 次の回に
    ticket(cur, 5, state="planned")   # 投入待ちは別の仕組み（supervisor の enqueue_step）
    issues = {1: {"state": "closed", "closed_at": "2026-10-31T16:00:00Z", "state_reason": "completed"}, 2: {"state": "open"},
              3: {"state": "closed"}, 5: {"state": "closed"}}
    calls = []

    def gh(method, path):
        calls.append(path)
        n = int(path.rsplit("/", 1)[1])
        if n == 4:
            raise RuntimeError("502")
        return issues[n]

    assert ticket_sync.step(cur, gh, log=lambda *a: None, now=1e9, force=True) == ["o/r#1"]
    st = states(cur)
    assert st["o/r#1"] == ("closed", OLD)   # 済んだ時刻は分からないので updated_at は動かさない
    assert [st[f"o/r#{n}"][0] for n in (2, 3, 4, 5)] == ["active", "active", "active", "planned"]
    assert sorted(calls) == ["repos/o/r/issues/1", "repos/o/r/issues/2", "repos/o/r/issues/4"]
    cur.execute("select correlation_id, payload from events where name = 'ticket.closed_with_source'")
    assert [(r["correlation_id"], r["payload"]["state_reason"]) for r in cur.fetchall()] == [("github:o/r#1", "completed")]


def test_runs_once_per_interval(cur):
    ticket(cur, 1, job="succeeded")
    calls = []
    gh = lambda m, p: calls.append(p) or {"state": "open"}   # noqa: E731
    ticket_sync._last = 0.0
    ticket_sync.step(cur, gh, log=lambda *a: None, now=1e9)
    ticket_sync.step(cur, gh, log=lambda *a: None, now=1e9 + ticket_sync.STEP_SEC - 1)
    assert len(calls) == 1
    ticket_sync.step(cur, gh, log=lambda *a: None, now=1e9 + ticket_sync.STEP_SEC)
    assert len(calls) == 2
