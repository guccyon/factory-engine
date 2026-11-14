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


def _qa(cur, n, exclusive=False, state="queued", repo="o/r", ago=0):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, kind, state, enqueued_at) "
                "values (%s,%s,%s,'t','qa',%s, now() - make_interval(secs => %s)) returning id", (f"github:{repo}#{n}", repo, n, state, ago))
    jid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                (f"github:{repo}#{n}", jid, Jsonb({"kind": "qa", "input": {"exclusive_env": True} if exclusive else {}})))
    return jid


def test_exclusive_qa_does_not_overlap_other_qa(cur):
    """ADR 0055 の決定 3: 時刻をまたぐ QA（exclusive_env）は、同じ製品のほかの QA と重ねて取り出さない"""
    line.scale(cur, "t", workers=5)
    _qa(cur, 1, state="running")                        # 探索が動いている
    t = _qa(cur, 2, exclusive=True, ago=60)
    assert line.claim_line(cur) is None                 # 時刻の QA は待つ
    late = _qa(cur, 3, ago=10)
    assert line.claim_line(cur) is None                 # 先に待っている時刻の QA を、後から来た QA は追い越さない
    _qa(cur, 4, repo="o/other")
    assert line.claim_line(cur)["repo"] == "o/other"    # ほかの製品の QA は流れる
    cur.execute("update jobs set state='succeeded' where issue_number=1")
    assert line.claim_line(cur)["id"] == t              # 空いたら時刻の QA
    assert line.claim_line(cur) is None                 # 時刻の QA が動いている間は、ほかの QA を取り出さない
    cur.execute("update jobs set state='succeeded' where id=%s", (t,))
    assert line.claim_line(cur)["id"] == late
