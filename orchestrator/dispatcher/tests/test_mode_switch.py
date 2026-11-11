"""モードを操業コンソールの設定から切り替える（2026-11-11）。factoryctl mode set と同じ記録（factory.mode_set）を残し、
schedule で切り替えを外して時間帯の設定と既定に戻す。TEST_DATABASE_URL の Postgres で確かめる（なければ飛ばす）
"""
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
    os.environ.setdefault("DATABASE_URL", URL)
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row)
    c = conn.cursor()
    c.execute("delete from events where name='factory.mode_set'")
    yield c
    conn.rollback()
    conn.close()


def test_set_mode_and_back_to_schedule(cur):
    import judgment
    assert judgment.current_mode(cur, "attended", "") == ("attended", "default")
    judgment.set_mode(cur, "unattended", "console:x", reason="夜の試し")
    spec = judgment.mode_spec(cur, "attended", "unattended=22:00-08:00")
    assert spec["mode"] == "unattended" and spec["basis"] == "factoryctl"
    assert spec["override"]["by"] == "console:x" and spec["override"]["reason"] == "夜の試し" and spec["override"]["until"] is None
    judgment.set_mode(cur, "schedule", "console:x")
    spec = judgment.mode_spec(cur, "attended", "")
    assert spec["mode"] == "attended" and spec["basis"] == "default" and spec["override"] is None


def test_set_mode_until(cur):
    import judgment
    until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    judgment.set_mode(cur, "unattended", "console:x", until)
    assert judgment.mode_spec(cur)["override"]["until"] == until
    for bad in ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), "2026-11-12T08:00", "あした"):
        with pytest.raises(ValueError):
            judgment.set_mode(cur, "attended", "console:x", bad)
    with pytest.raises(ValueError):
        judgment.set_mode(cur, "schedule", "console:x", until)
    with pytest.raises(ValueError):
        judgment.set_mode(cur, "auto", "console:x")


def test_console_api(cur):
    import console
    code, out = console.act(cur, "/api/factory/mode", {"mode": "unattended", "until": "2999-01-01T00:00:00Z", "reason": "試し"})
    assert code == 200 and out["mode"]["mode"] == "unattended" and out["mode"]["override"]["until"].startswith("2999-01-01")
    assert console.act(cur, "/api/factory/mode", {"mode": "nope"})[0] == 400
    assert console.act(cur, "/api/factory/mode", {"mode": "attended", "until": "きのう"})[0] == 400
    code, out = console.act(cur, "/api/factory/mode", {"mode": "schedule"})
    assert code == 200 and out["mode"]["override"] is None
