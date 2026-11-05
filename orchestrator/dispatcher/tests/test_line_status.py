"""流量の表示（line.status の tickets）。supervisor が heartbeat に残した同時に流しているチケットの数を読む。TEST_DATABASE_URL がなければ飛ばす"""
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

    import line


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def test_tickets_inflight_from_latest_heartbeat(cur):
    assert line.status(cur)["running"]["tickets"] is None   # supervisor がまだ数えていない
    cur.execute("insert into events(name, occurred_at, payload) values ('supervisor.heartbeat', now() - interval '2 minutes', %s)", (Jsonb({"tickets_inflight": 4}),))
    cur.execute("insert into events(name, occurred_at, payload) values ('supervisor.heartbeat', now() - interval '1 minute', %s)", (Jsonb({"tickets_inflight": 2}),))
    cur.execute("insert into events(name, payload) values ('supervisor.heartbeat', %s)", (Jsonb({"repos": []}),))   # 周回の途中（まだ数えていない）
    st = line.status(cur)
    assert st["running"]["tickets"] == 2 and "workers" in st["running"] and "tickets" in st["desired"]
