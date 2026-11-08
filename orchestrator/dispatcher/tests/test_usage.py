"""使用量の集計（usage.py）とページ（usage_page.py）。

枠と週の区切りは DB なしで、集計は TEST_DATABASE_URL の Postgres で確かめる（なければ飛ばす）。
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")

import usage  # noqa: E402
import usage_page  # noqa: E402

JST = timezone(timedelta(hours=9))
URL = os.environ.get("TEST_DATABASE_URL")


def test_budget_windows_same_as_supervisor(monkeypatch):
    monkeypatch.setenv("BUDGET_WINDOWS_WEEKDAY", "07:10,22:25,03:15")
    monkeypatch.setenv("BUDGET_WINDOWS_WEEKEND", "07:700")
    # 水曜 02:00 は火曜の 22 時の枠
    start, usd = usage.budget_windows(datetime(2026, 11, 4, 2, 0, tzinfo=JST))
    assert (start.day, start.hour, usd) == (3, 22, 25)
    assert usage.budget_windows(datetime(2026, 11, 4, 3, 30, tzinfo=JST))[1] == 15
    assert usage.budget_windows(datetime(2026, 11, 7, 8, 0, tzinfo=JST))[1] == 700   # 土曜
    monkeypatch.setenv("BUDGET_WINDOWS_WEEKDAY", "")
    assert usage.budget_windows(datetime(2026, 11, 4, 12, 0, tzinfo=JST)) is None


def test_day_budget_counts_windows_until_next_morning(monkeypatch):
    monkeypatch.setenv("BUDGET_WINDOWS_WEEKDAY", "07:10,12:15,22:25,03:15")
    monkeypatch.setenv("BUDGET_WINDOWS_WEEKEND", "07:700")
    assert usage.day_budget(date(2026, 11, 4)) == 10 + 15 + 25 + 15   # 翌朝 3 時の枠まで
    assert usage.day_budget(date(2026, 11, 7)) == 700
    assert usage.day_budget(date(2026, 11, 6)) == 10 + 15 + 25         # 金曜の翌朝は土曜（3 時の枠がない）
    monkeypatch.setenv("BUDGET_WINDOWS_WEEKDAY", "")
    assert usage.day_budget(date(2026, 11, 4)) is None


def test_week_start_resets_sunday_11():
    assert usage.week_start(datetime(2026, 11, 4, 12, 0, tzinfo=JST), "sun 11:00") == datetime(2026, 11, 1, 11, 0, tzinfo=JST)
    assert usage.week_start(datetime(2026, 11, 8, 10, 59, tzinfo=JST), "sun 11:00") == datetime(2026, 11, 1, 11, 0, tzinfo=JST)
    assert usage.week_start(datetime(2026, 11, 8, 11, 0, tzinfo=JST), "sun 11:00") == datetime(2026, 11, 8, 11, 0, tzinfo=JST)


def test_budget_day_starts_at_7():
    assert usage.budget_day(datetime(2026, 11, 5, 6, 59, tzinfo=JST)) == date(2026, 11, 4)
    assert usage.budget_day(datetime(2026, 11, 5, 7, 0, tzinfo=JST)) == date(2026, 11, 5)


def test_signals_rules():
    day = {"date": "2026-11-04", "jobs": 3, "cost_usd": 100.0, "budget_usd": 700.0, "budget_stopped_hours": 0, "usage_limited": 0}
    week = {"usage_limited": 0, "exhausted_at": None, "per_day_usd": 100}
    assert [s["level"] for s in usage.signals([day] * 7, week)] == ["ok"]
    assert "足す必要はまだない" in usage.signals([day] * 7, week)[0]["text"]
    hot = dict(day, cost_usd=700.0, budget_stopped_hours=3.0)
    out = usage.signals([hot] * 7, dict(week, usage_limited=2))
    assert [s["level"] for s in out] == ["bad", "warn"]
    assert "21.0 時間" in out[1]["text"]


@pytest.fixture
def cur():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(URL, row_factory=dict_row) as conn:
        with conn.cursor() as c:
            c.execute("delete from events")
            c.execute("delete from jobs")
            yield c
        conn.rollback()


def _job(cur, kind, at, cost, billable, state="succeeded", limited=False, model="claude-x"):
    from psycopg.types.json import Jsonb
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state) values ('github:o/r#1','o/r',%s,%s,'succeeded') returning id",
                (int(at.timestamp()) % 100000, kind))
    jid = cur.fetchone()["id"]
    cur.execute("insert into events(name, occurred_at, job_id, payload) values ('job.finished',%s,%s,%s)",
                (at, jid, Jsonb({"state": state, "cost_usd": cost, "billable_tokens": billable, "total_tokens": billable * 3, "usage_limited": limited})))
    cur.execute("insert into events(name, occurred_at, job_id, payload) values ('session.cost_recorded',%s,%s,%s)",
                (at, jid, Jsonb({"model": model, "cost_usd": cost, "usage": {"input_tokens": billable // 2, "output_tokens": billable // 2,
                                                                            "cache_read_input_tokens": billable * 2}})))


def test_view_aggregates(cur, monkeypatch):
    from psycopg.types.json import Jsonb
    monkeypatch.setenv("BUDGET_WINDOWS_WEEKDAY", "07:700")
    monkeypatch.setenv("BUDGET_WINDOWS_WEEKEND", "07:700")
    now = datetime(2026, 11, 4, 12, 0, tzinfo=JST)   # 水曜。週は 11/1 11:00 から
    _job(cur, "implement", datetime(2026, 11, 4, 8, 0, tzinfo=JST), 30.0, 1_000_000)
    _job(cur, "implement", datetime(2026, 11, 4, 6, 0, tzinfo=JST), 20.0, 500_000, state="failed", limited=True)   # 前の日（7 時前）
    _job(cur, "judge", datetime(2026, 11, 3, 9, 0, tzinfo=JST), 10.0, 200_000, model="claude-y")
    _job(cur, "implement", datetime(2026, 10, 31, 9, 0, tzinfo=JST), 99.0, 1)   # 先週（週の数に入らない）
    cur.execute("insert into events(name, occurred_at, payload) values ('stoppage.opened',%s,%s)",
                (datetime(2026, 11, 3, 20, 0, tzinfo=JST), Jsonb({"key": "budget:x", "category": "budget"})))
    cur.execute("insert into events(name, occurred_at, payload) values ('stoppage.closed',%s,%s)",
                (datetime(2026, 11, 3, 22, 30, tzinfo=JST), Jsonb({"key": "budget:x"})))
    v = usage.view(cur, now=now, days=7)
    days = {d["date"]: d for d in v["daily"]}
    assert days["2026-11-04"]["cost_usd"] == 30.0 and days["2026-11-04"]["jobs"] == 1
    assert days["2026-11-03"]["cost_usd"] == 30.0 and days["2026-11-03"]["usage_limited"] == 1 and days["2026-11-03"]["failed_cost_usd"] == 20.0
    assert days["2026-11-03"]["budget_stopped_hours"] == 2.5 and days["2026-11-03"]["budget_usd"] == 700
    assert v["week"]["cost_usd"] == 60.0 and v["week"]["usage_limited"] == 1
    assert v["by_kind"][0]["kind"] == "implement" and v["by_kind"][0]["ja"] == "実装"
    assert {m["model"] for m in v["by_model"]} == {"claude-x", "claude-y"}
    assert v["signals"][0]["level"] == "bad"
    page = usage_page.render(v)
    assert "工場の使用量" in page and "<script" not in page and "実装" in page


def test_page_escapes():
    v = {"generated_at": "2026-11-04T12:00:00+09:00", "day_start_hour": 7,
         "daily": [{"date": "2026-11-04", "jobs": 0, "cost_usd": 0, "billable_tokens": 0, "total_tokens": 0, "failed_cost_usd": 0,
                    "usage_limited": 0, "budget_usd": None, "budget_stopped_hours": 0}],
         "by_kind": [{"kind": "x", "ja": "<b>x</b>", "jobs": 1, "cost_usd": 1, "billable_tokens": 1, "share": 1}], "by_model": [],
         "week": {"start": "2026-11-01T11:00:00+09:00", "end": "2026-11-08T11:00:00+09:00", "cost_usd": 0, "per_day_usd": 0, "projected_usd": 0,
                  "billable_tokens": 0, "jobs": 0, "usage_limited": 0, "last_usage_limited": None, "limit_usd": None, "share": None, "exhausted_at": None},
         "signals": [{"level": "ok", "text": "<script>"}]}
    page = usage_page.render(v)
    assert "<b>x</b>" not in page and "&lt;script&gt;" in page


def test_declared_windows_one_per_day():
    """宣言があれば 1 日（7:00 から）を 1 つの枠にする。曜日は 1 日が始まった日の曜日"""
    daily = {"mode": "daily", "daily_usd": 500.0, "weekday_usd": {k: 0.0 for k in usage.WEEKDAYS}}
    start, usd = usage.budget_windows(datetime(2026, 11, 4, 3, 0, tzinfo=JST), daily)   # 水曜 3 時は火曜 7 時からの 1 日
    assert (start.day, start.hour, usd) == (3, 7, 500)
    assert usage.day_budget(date(2026, 11, 4), daily) == 500
    week = {"mode": "weekday", "daily_usd": 500.0, "weekday_usd": {**{k: 300.0 for k in usage.WEEKDAYS}, "sat": 900.0, "sun": 0.0}}
    assert usage.budget_windows(datetime(2026, 11, 7, 6, 59, tzinfo=JST), week)[1] == 300   # 土曜 6:59 は金曜の 1 日
    assert usage.budget_windows(datetime(2026, 11, 7, 7, 0, tzinfo=JST), week)[1] == 900
    assert usage.day_budget(date(2026, 11, 8), week) == 0   # 0 はその日は新しいタスクを投入しない


def test_budget_declaration_overrides_env(cur, monkeypatch):
    monkeypatch.setenv("BUDGET_WINDOWS_WEEKDAY", "07:700")
    monkeypatch.setenv("BUDGET_WINDOWS_WEEKEND", "07:700")
    now = datetime(2026, 11, 4, 12, 0, tzinfo=JST)
    s = usage.budget_spec(cur, now)
    assert s["mode"] == "env" and s["daily_usd"] == 700 and s["weekday_usd"]["sat"] == 700 and s["today_usd"] == 700
    s = usage.set_budget(cur, "test", "絞る", daily_usd=400)
    assert s["mode"] == "daily" and s["daily_usd"] == 400 and s["by"] == "test" and s["reason"] == "絞る"
    assert s["weekday_usd"]["mon"] == 700   # 曜日ごとの額は環境変数の値から始まる
    assert usage.budget_spec(cur, now)["today_usd"] == 400
    s = usage.set_budget(cur, "test", mode="weekday", weekday_usd={"wed": 200, "sat": 1000})
    assert s["mode"] == "weekday" and s["daily_usd"] == 400 and s["weekday_usd"]["wed"] == 200 and s["weekday_usd"]["sat"] == 1000
    assert usage.budget_spec(cur, now)["today_usd"] == 200
    assert usage.budget_windows(now, usage.declared(cur)) == (datetime(2026, 11, 4, 7, 0, tzinfo=JST), 200)
    for bad in ({"mode": "hourly"}, {"daily_usd": -1}, {"daily_usd": True}, {"weekday_usd": {"xyz": 1}}, {"weekday_usd": {"mon": "a"}}):
        with pytest.raises(ValueError):
            usage.set_budget(cur, "test", **bad)
    assert "曜日ごと" in usage.budget_text(usage.budget_spec(cur, now))


def test_supervisor_over_budget_reads_declaration(cur, monkeypatch):
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    import supervisor
    from psycopg.types.json import Jsonb
    monkeypatch.setenv("BUDGET_WINDOWS_WEEKDAY", "07:700")
    monkeypatch.setenv("BUDGET_WINDOWS_WEEKEND", "07:700")
    cur.execute("insert into events(name, payload, occurred_at) values ('job.finished', %s, now())", (Jsonb({"cost_usd": 50}),))
    assert supervisor.over_budget(cur) is None
    usage.set_budget(cur, "test", daily_usd=10)
    ob = supervisor.over_budget(cur)
    assert ob and ob["budget_usd"] == 10 and ob["spent_usd"] == 50
