"""ship（ADR 0012）の依頼の受け付け・環境ごとの版の履歴・処理待ちの拾い方。docker と git は使わない。

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

    import ship

REPO = "o/app"


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def release(cur, version):
    rid = f"{REPO}@{version}"
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('release.created',%s,%s,%s)",
                (f"release:{rid}", Jsonb({"release_id": rid, "repo": REPO, "app_version": version, "prs": []}), f"release.created:{rid}"))
    return rid


def deployed(cur, rid, env, ok=True):
    ship.record(cur, "release.deployed", rid, {"repo": REPO, "env": env, "ok": ok, "request_id": 0})


def test_ship_requires_existing_release_and_safe_version(cur):
    assert ship.request_ship(cur, f"{REPO}@9.9.9", "t", "test")[0] is False
    rid = release(cur, "1.0.0")
    assert ship.request_ship(cur, rid, "t", "test")[0] is True
    bad = release(cur, "1.0;rm -rf")
    assert ship.request_ship(cur, bad, "t", "test")[0] is False


def test_approve_only_what_staging_runs(cur):
    a, b = release(cur, "1.0.0"), release(cur, "1.1.0")
    assert ship.request_approve(cur, a, "t", "test")[0] is False      # まだ staging に出ていない
    deployed(cur, a, "staging")
    deployed(cur, b, "staging")
    assert ship.request_approve(cur, a, "t", "test")[0] is False      # staging はもう 1.1.0
    assert ship.request_approve(cur, b, "t", "test")[0] is True


def test_status_and_rollback_history(cur):
    a, b, c = release(cur, "1.0.0"), release(cur, "1.1.0"), release(cur, "1.2.0")
    for r in (a, b, c):
        deployed(cur, r, "prod")
    deployed(cur, c, "staging", ok=False)        # 失敗は数えない
    st = ship.status(cur, REPO)["environments"]
    assert st["prod"] == {"release_id": c, "previous": b}
    assert "staging" not in st
    ship.record(cur, "release.rolled_back", b, {"repo": REPO, "env": "prod", "ok": True, "from": c, "request_id": 0})
    assert ship.status(cur, REPO)["environments"]["prod"] == {"release_id": b, "previous": a}   # もう一度戻すと、さらに前へ
    assert ship.request_rollback(cur, REPO, "prod", "t", "test")[0] is True
    assert ship.request_rollback(cur, REPO, "staging", "t", "test")[0] is False   # 戻す先がない


def test_pending_skips_handled_requests(cur):
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    reqs = ship.pending(cur)
    assert [r["name"] for r in reqs] == ["release.ship_requested"]
    ship.record(cur, "release.ship_failed", rid, {"repo": REPO, "request_id": reqs[0]["id"], "error": "x"})
    assert ship.pending(cur) == []
