"""登録済みのランナー（runners.py）: 連絡で登録し、外す依頼を次の連絡の返事で伝え、外したら一覧から消える。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）。
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

    import runners

RID = "a" * 32


@pytest.fixture
def cur():
    with psycopg.connect(URL, row_factory=dict_row, autocommit=True) as conn, conn.cursor() as c:
        c.execute("delete from events where name like 'runner.%'")
        runners.seen.clear()
        yield c


def q(**kw):
    return {"id": RID, "name": "mac1", "version": "0.2.0", "path": "/x/runner.py", "scope": "できる: …", **kw}


def test_register_uninstall_and_reinstall(cur):
    assert runners.checkin(cur, q()) == (200, {"uninstall": False})
    assert runners.checkin(cur, q()) == (200, {"uninstall": False})
    cur.execute("select count(*) as n from events where name='runner.registered'")
    assert cur.fetchone()["n"] == 1   # 同じ中身の連絡は書かない
    v = runners.view(cur)
    assert [r["id"] for r in v] == [RID] and v[0]["online"] and v[0]["scope"] == "できる: …"

    assert runners.request_uninstall(cur, RID, "console:t")[0]
    assert runners.view(cur)[0]["uninstall_pending"]
    assert runners.checkin(cur, q(version="0.2.1"))[1] == {"uninstall": True}   # 版が変わっても依頼は残る
    assert runners.checkin(cur, q(state="uninstalled"))[1] == {"uninstall": False}
    assert runners.view(cur) == []

    assert runners.checkin(cur, q())[1] == {"uninstall": False}   # 入れ直したら、前の依頼は残らない
    assert len(runners.view(cur)) == 1


def test_rejects_bad_id(cur):
    assert runners.checkin(cur, {"id": "../x"})[0] == 400
    assert runners.request_uninstall(cur, "b" * 32, "t") == (False, "no such runner")
