"""操業コンソールの工場の保守（ADR 0059、#477）: 見回りの結果・最近うまくいかなかったジョブを読むだけで集める（記録は書かない）

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

URL = os.environ.get("TEST_DATABASE_URL")


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


def test_view(cur):
    import console_maint
    now = datetime.now(timezone.utc)
    cur.execute("insert into events(name, payload) values ('supervisor.heartbeat', %s)", (json.dumps({"patrols": ["daily_report", "kpi_weekly"]}),))
    cur.execute("insert into events(name, occurred_at) values ('patrol_watch.started', %s)", (now - timedelta(days=3),))
    cur.execute("update events set dedupe_key=%s where name='patrol_watch.started'", ("patrol_watch.started:v1",))
    cur.execute("insert into events(name, occurred_at) values ('report.daily', %s)", (now - timedelta(hours=2),))
    cur.execute("""insert into jobs(correlation_id, repo, issue_number, state, finished_at, error) values
                   ('github:o/r#1', 'o/r', 1, 'failed', %s, 'boom'), ('github:o/r#2', 'o/r', 2, 'failed', %s, ''),
                   ('github:o/r#3', 'o/r', 3, 'succeeded', %s, '')""", (now - timedelta(hours=1), now - timedelta(days=3), now))
    cur.execute("select count(*) as n from events")
    before = cur.fetchone()["n"]
    v = console_maint.view(cur, now)
    p = {x["id"]: x for x in v["patrols"]}
    assert p["daily_report"]["enabled"] and not p["daily_report"]["overdue"]
    assert p["kpi_weekly"]["enabled"] and not p["kpi_weekly"]["overdue"]   # 週 1 回は 8 日まで待つ
    assert not p["nightly_explore"]["enabled"] and not p["nightly_explore"]["overdue"]
    assert [j["issue_number"] for j in v["jobs"]] == [1] and v["jobs"][0]["error"] == "boom"
    assert v["incidents"] == []
    cur.execute("select count(*) as n from events")
    assert cur.fetchone()["n"] == before   # 読むだけ


def test_overdue(cur):
    import console_maint
    now = datetime.now(timezone.utc)
    cur.execute("insert into events(name, payload) values ('supervisor.heartbeat', %s)", (json.dumps({"patrols": ["daily_report"]}),))
    cur.execute("insert into events(name, occurred_at, dedupe_key) values ('patrol_watch.started', %s, 'patrol_watch.started:v1')", (now - timedelta(days=3),))
    p = {x["id"]: x for x in console_maint.view(cur, now)["patrols"]}
    assert p["daily_report"]["overdue"] and p["daily_report"]["last"] is None
