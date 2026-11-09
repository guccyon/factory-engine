"""定期便（ADR 0043 の O6 release_train）。便に Release を束ね、承認は便ごとに 1 回、S1 の修正は緊急便。docker と git は使わない。

  TEST_DATABASE_URL=postgresql://factory@localhost:5432/factory_test python -m pytest orchestrator/dispatcher/tests/test_train.py
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
    import ship
    import train
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

A, B = "o/app", "o/mobile"
NOW = datetime(2026, 11, 9, 9, 30, tzinfo=timezone.utc)   # JST 18:30


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def merged(cur, repo, n):
    url = f"https://github.com/{repo}/pull/{n}"
    cur.execute("insert into events(name, payload, dedupe_key) values ('pr.merged', %s, %s)",
                (Jsonb({"pr_url": url, "merged_at": f"2026-11-09T00:00:{n:02d}Z"}), f"pr.merged:{url}"))


def finish_ship(cur):
    for ev in ship.pending(cur):
        ship.record(cur, "release.deployed", ev["payload"]["release_id"],
                    {"repo": ship.release_of(cur, ev["payload"]["release_id"])["repo"], "env": "staging", "ok": True, "request_id": ev["id"]})


def test_regular_train_bundles_releases_of_several_repos_once_a_day(cur):
    merged(cur, A, 1)
    merged(cur, A, 2)
    merged(cur, B, 3)
    tid, rels = train.regular(cur, [A, B], NOW)
    assert [r["release_id"] for r in rels] == [f"{A}@0.1.0", f"{B}@0.1.0"] and rels[0]["prs"] == 2
    t = train.train_of(cur, tid)
    assert t["kind"] == "regular" and t["repos"] == [A, B]
    assert t["approval"] == {"unit": "train", "required": False, "pending": []}   # どちらも development（本番を立てない）
    assert len(ship.pending(cur)) == 2                                             # staging への出し方は今のまま
    finish_ship(cur)
    merged(cur, A, 4)
    assert train.regular(cur, [A, B], NOW + timedelta(hours=1)) == (None, [])     # 1 日 1 便
    tid2, rels2 = train.regular(cur, [A, B], NOW + timedelta(days=1))
    assert tid2 != tid and [r["release_id"] for r in rels2] == [f"{A}@0.2.0"]     # 翌日は PR のある製品だけ


def test_no_train_without_merged_prs(cur):
    assert train.regular(cur, [A], NOW) == (None, [])
    cur.execute("select count(*) as n from events where name='release.train'")
    assert cur.fetchone()["n"] == 0


def test_emergency_train_does_not_wait_for_the_daily_one(cur):
    merged(cur, A, 1)
    train.regular(cur, [A], NOW)
    finish_ship(cur)
    assert train.emergency(cur, A, "qa", "S1 の修正", NOW)[0] is False             # 修正の PR がまだない
    merged(cur, A, 9)
    assert train.emergency(cur, A, "qa", "  ", NOW)[0] is False                    # 理由は必須
    ok, msg = train.emergency(cur, A, "qa", "S1: 個人情報が見える画面の修正", NOW + timedelta(minutes=5))
    assert ok, msg
    t = train.recent(cur, 1)[0]
    assert t["kind"] == "emergency" and t["releases"][0]["release_id"] == f"{A}@0.2.0" and t["reason"].startswith("S1")
    assert train.emergency(cur, A, "qa", "もう一度", NOW)[0] is False               # 出荷の処理中は重ねない


def test_approval_is_once_per_train(cur):
    ship.observe_stage(cur, A, "launch")
    merged(cur, A, 1)
    merged(cur, B, 2)                                                              # B は development のまま
    tid, _ = train.regular(cur, [A, B], NOW)
    t = train.train_of(cur, tid)
    assert t["approval"]["required"] is True and t["approval"]["pending"] == [f"{A}@0.1.0"]
    assert train.approve(cur, tid, "owner", "test")[0] is False                    # まだ staging に出ていない
    finish_ship(cur)
    ok, msg = train.approve(cur, tid, "owner", "test")
    assert ok, msg
    cur.execute("select payload from events where name='release.approved'")
    assert [r["payload"]["release_id"] for r in cur.fetchall()] == [f"{A}@0.1.0"]  # 本番を立てない B は承認しない
    assert train.approve(cur, tid, "owner", "test")[0] is False                    # 2 回目はない
    assert train.recent(cur, 1)[0]["approved"]["by"] == "owner"
    assert train.approve(cur, "train:none", "owner", "test")[0] is False
