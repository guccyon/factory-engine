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
    assert (st["prod"]["release_id"], st["prod"]["previous"]) == (c, b)
    assert "staging" not in st
    ship.record(cur, "release.rolled_back", b, {"repo": REPO, "env": "prod", "ok": True, "from": c, "request_id": 0})
    p0 = ship.status(cur, REPO)["environments"]["prod"]
    assert (p0["release_id"], p0["previous"]) == (b, a)   # もう一度戻すと、さらに前へ
    assert ship.request_rollback(cur, REPO, "prod", "t", "test")[0] is True
    assert ship.request_rollback(cur, REPO, "staging", "t", "test")[0] is False   # 戻す先がない


def test_pending_skips_handled_requests(cur):
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    reqs = ship.pending(cur)
    assert [r["name"] for r in reqs] == ["release.ship_requested"]
    ship.record(cur, "release.ship_failed", rid, {"repo": REPO, "request_id": reqs[0]["id"], "error": "x"})
    assert ship.pending(cur) == []


def merged(cur, n):
    url = f"https://github.com/{REPO}/pull/{n}"
    cur.execute("insert into events(name, payload, dedupe_key) values ('pr.merged', %s, %s)",
                (Jsonb({"pr_url": url, "merged_at": f"2026-10-30T00:00:{n:02d}Z"}), f"pr.merged:{url}"))


def test_release_train_helpers(cur):
    assert ship.next_version(cur, REPO) == "0.1.0"
    merged(cur, 1)
    merged(cur, 2)
    assert ship.unreleased(cur, REPO) == 2
    rid, prs = ship.create_release(cur, REPO, ship.next_version(cur, REPO))
    assert rid == f"{REPO}@0.1.0" and len(prs) == 2
    assert ship.create_release(cur, REPO, "0.1.0") == (None, [])   # 同じ版は作らない
    assert ship.unreleased(cur, REPO) == 0
    assert ship.next_version(cur, REPO) == "0.2.0"
    assert ship.busy(cur, REPO) is False
    ship.request_ship(cur, rid, "t", "test")
    assert ship.busy(cur, REPO) is True                             # 処理待ち
    ship.record(cur, "release.deployed", rid, {"repo": REPO, "env": "staging", "ok": True, "request_id": ship.pending(cur)[0]["id"]})
    assert ship.busy(cur, REPO) is True                             # 承認待ち（staging と prod が違う）
    deployed(cur, rid, "prod")
    assert ship.busy(cur, REPO) is False


def built(cur, rid, contracts):
    ship.record(cur, "release.built", rid, {"repo": REPO, "request_id": 0, "commit": "x", "contract_migrations": contracts})


def test_contract_sets_rollback_floor(cur):
    a, b, c, d = (release(cur, v) for v in ("1.0.0", "1.1.0", "1.2.0", "1.3.0"))
    for r in (a, b, c, d):
        built(cur, r, ["m/1_contract_drop_old.sql"] if r == c else [])
        deployed(cur, r, "prod")
    st = ship.status(cur, REPO)["environments"]["prod"]
    assert st["rollback_floor"] == b and "rollback_blocked" not in st     # d → c は戻せる
    ship.record(cur, "release.rolled_back", c, {"repo": REPO, "env": "prod", "ok": True, "from": d, "request_id": 0})
    assert "rollback_blocked" not in ship.status(cur, REPO)["environments"]["prod"]   # c → b も戻せる（b は列を使わない版）
    ship.record(cur, "release.rolled_back", b, {"repo": REPO, "env": "prod", "ok": True, "from": c, "request_id": 0})
    st = ship.status(cur, REPO)["environments"]["prod"]
    assert st["previous"] == a and "contract" in st["rollback_blocked"]   # b → a は戻せない（DB に contract が残る）
    assert ship.request_rollback(cur, REPO, "prod", "t", "test")[0] is False
