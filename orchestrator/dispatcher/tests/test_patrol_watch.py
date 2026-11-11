"""見回りの見回り（ADR 0050 の決定 1）と、PR の見張りが閉じた PR を記録する小さなポーリング（決定 2）。

TEST_DATABASE_URL の Postgres で確かめる（なければ飛ばす）。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
os.environ.setdefault("NOTIFIER", "log")

import patrol_watch  # noqa: E402
import stoppage  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def cur(monkeypatch):
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    import psycopg
    from psycopg.rows import dict_row
    monkeypatch.setattr(stoppage, "notify", lambda kind, s, cur=None: None)
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def ev(cur, name, at, payload=None, key=None):
    from psycopg.types.json import Jsonb
    cur.execute("insert into events(name, payload, occurred_at, dedupe_key) values (%s, %s, %s, %s)", (name, Jsonb(payload or {}), at, key))


def heartbeat(cur, patrols, at):
    ev(cur, "supervisor.heartbeat", at, {"patrols": patrols})


def open_patrols(cur):
    return sorted(stoppage.open_keys(cur, "patrol:"))


NOW = datetime(2026, 11, 20, 3, tzinfo=timezone.utc)


def test_opens_when_overdue_and_closes_when_it_runs(cur):
    """見回りを 1 つ止めると、期限を過ぎたら停止が開き、動いたら閉じる"""
    ev(cur, "patrol_watch.started", NOW - timedelta(days=10), key=patrol_watch.STARTED)
    heartbeat(cur, ["nightly_explore", "design_review"], NOW)
    ev(cur, "supervisor.nightly_explore_enqueued", NOW - timedelta(hours=20))   # 期限（36 時間）の内
    ev(cur, "supervisor.design_review_enqueued", NOW - timedelta(days=9))      # 期限（8 日）を過ぎた
    got = patrol_watch.step(cur, paused=False, heartbeat_stale=False, now=NOW)
    assert got["opened"] == ["design_review"] and open_patrols(cur) == ["patrol:design_review"]
    cur.execute("select payload from events where name='stoppage.opened'")
    p = cur.fetchone()["payload"]
    assert p["category"] == "factory-bug" and "設計の診断" in p["what"]
    assert patrol_watch.step(cur, paused=False, heartbeat_stale=False, now=NOW)["opened"] == []   # 重ねて開かない
    ev(cur, "supervisor.design_review_enqueued", NOW)   # 動いた
    got = patrol_watch.step(cur, paused=False, heartbeat_stale=False, now=NOW + timedelta(minutes=1))
    assert got["closed"] == ["design_review"] and open_patrols(cur) == []


def test_never_ran_counts_from_start_and_skips_undeclared(cur):
    """記録が 1 度もないものは見回りの見回りを入れた時刻から数える。宣言にない（止めた）見回りは見ない"""
    heartbeat(cur, ["nightly_explore"], NOW)
    assert patrol_watch.step(cur, paused=False, heartbeat_stale=False, now=NOW)["opened"] == []   # 入れた直後は開かない
    later = NOW + timedelta(hours=37)
    heartbeat(cur, ["nightly_explore"], later)
    assert patrol_watch.step(cur, paused=False, heartbeat_stale=False, now=later)["opened"] == ["nightly_explore"]
    heartbeat(cur, [], later)   # supervisor が夜間の探索を止めた
    assert patrol_watch.step(cur, paused=False, heartbeat_stale=False, now=later)["closed"] == ["nightly_explore"]


def test_does_not_open_while_paused_or_supervisor_down(cur):
    """一時停止中・予算の枠の使い切り中・supervisor の heartbeat が途絶えているときは開かない"""
    ev(cur, "patrol_watch.started", NOW - timedelta(days=10), key=patrol_watch.STARTED)
    heartbeat(cur, ["kpi_weekly"], NOW)
    assert patrol_watch.step(cur, paused=True, heartbeat_stale=False, now=NOW)["opened"] == []
    assert patrol_watch.step(cur, paused=False, heartbeat_stale=True, now=NOW)["opened"] == []
    assert patrol_watch.step(cur, paused=False, heartbeat_stale=False, now=NOW)["opened"] == ["kpi_weekly"]


def test_old_heartbeat_without_declaration_sees_nothing(cur):
    """宣言（patrols）を書かない古い版の supervisor のあいだは何も開かない"""
    ev(cur, "supervisor.heartbeat", NOW, {"repos": []})
    assert patrol_watch.overdue(cur, NOW) == {}


def test_enabled_ids_keep_table_order():
    assert patrol_watch.enabled_ids({"kpi_weekly": True, "nightly_explore": True, "nope": True, "design_review": False}) == \
        ["nightly_explore", "kpi_weekly"]


def test_pr_watch_records_closed_pr_once(cur, monkeypatch):
    """PR の見張りは使い捨て環境の PR が閉じたのを github.pr_closed に 1 回だけ残す（使い捨て環境の後片付けは GitHub を見ない）"""
    import pr_events
    state = {"state": "open"}
    monkeypatch.setattr(pr_events, "gh", lambda method, path, body=None: dict(state))
    assert pr_events.observe_closed(cur, "o/app", 7, "poll") is False
    state.update({"state": "closed", "merged_at": None, "closed_at": "2026-11-20T03:00:00Z"})
    assert pr_events.observe_closed(cur, "o/app", 7, "poll") is True
    assert pr_events.observe_closed(cur, "o/app", 7, "poll") is False
    cur.execute("select payload from events where name='github.pr_closed'")
    p = cur.fetchone()["payload"]
    assert (p["pr_url"], p["merged"]) == ("https://github.com/o/app/pull/7", False)
