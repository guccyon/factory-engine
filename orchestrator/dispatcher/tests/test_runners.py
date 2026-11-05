"""登録済みのランナー（runners.py、ADR 0033）: ペアリングの依頼と許可、窓口に来たときの記録、外す依頼、一覧。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    import psycopg
    from psycopg.rows import dict_row

    import runners

RID = "a" * 32
PID = "p" * 32


@pytest.fixture
def cur():
    with psycopg.connect(URL, row_factory=dict_row, autocommit=True) as conn, conn.cursor() as c:
        c.execute("delete from events where name like 'runner.%'")
        yield c


def info(**kw):
    return {"name": "mac1", "version": "0.3.0", "path": "/x/runner.py", "scope": "できる: …", **kw}


def test_pairing_needs_console_decision_and_expires(cur):
    assert runners.pair_state(cur, PID) == "unknown"
    runners.pair_requested(cur, PID, "123 456", info())
    assert runners.pair_state(cur, PID) == "pending"
    assert [p["code"] for p in runners.pending_pairs(cur)] == ["123 456"]
    later = datetime.now(timezone.utc) + timedelta(minutes=11)
    assert runners.pair_state(cur, PID, later) == "expired"
    assert runners.decide_pair(cur, PID, True, "console:t", now=later)[0] is False   # 切れた依頼は許可できない

    assert runners.decide_pair(cur, PID, True, "console:t")[0]
    assert runners.pair_state(cur, PID) == "approved"
    assert runners.pending_pairs(cur) == []
    assert runners.decide_pair(cur, PID, False, "console:t") == (False, "もう許可した")


def test_rejected_pairing(cur):
    runners.pair_requested(cur, PID, "000 001", info())
    assert runners.decide_pair(cur, PID, False, "console:t")[0]
    assert runners.pair_state(cur, PID) == "rejected"


def test_hello_uninstall_and_view(cur):
    assert runners.hello(cur, RID, info(), None) == (False, None)   # 登録していないランナーは何も書かない
    runners.register(cur, RID, info())
    pending, beat = runners.hello(cur, RID, info(), None)
    assert pending is False and beat is not None
    assert runners.hello(cur, RID, info(), beat)[1] == beat   # 4 分は heartbeat を書き直さない
    cur.execute("select count(*) as n from events where name='runner.registered'")
    assert cur.fetchone()["n"] == 1   # 同じ中身は書かない
    runners.hello(cur, RID, info(version="0.3.1"), beat)
    v = runners.view(cur)
    assert [r["id"] for r in v] == [RID] and v[0]["online"] and v[0]["version"] == "0.3.1"

    assert runners.request_uninstall(cur, RID, "console:t")[0]
    assert runners.view(cur)[0]["uninstall_pending"] and runners.uninstall_pending(cur) == [RID]
    assert runners.hello(cur, RID, {}, beat)[0] is True
    runners.uninstalled(cur, RID, "console")
    assert runners.view(cur) == [] and runners.uninstall_pending(cur) == []
    assert runners.request_uninstall(cur, RID, "t") == (False, "no such runner")


def test_rejects_bad_id_and_cleans_info(cur):
    assert runners.request_uninstall(cur, "../x", "t") == (False, "no such runner")
    assert runners.clean({"name": "a\x00b" + "x" * 500, "evil": "1"}) == {"name": "ab" + "x" * 198}
