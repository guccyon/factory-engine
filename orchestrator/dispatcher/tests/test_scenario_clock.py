"""QA の時刻合わせ（ADR 0055）の引数の読み方。DB は使わない（断る・戻すは test_touch.py）"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scenario_clock as sc  # noqa: E402

NOW = datetime(2026, 11, 13, 10, 0, 0, tzinfo=timezone.utc)   # 19:00 JST


def test_set_to_becomes_an_offset_from_now():
    args, why = sc.parse({"set_to": "2026-11-13T19:51:00+09:00"}, NOW)
    assert why is None and args == {"offset_seconds": 51 * 60}
    assert sc.parse({"set_to": "2026-11-13T18:00:00+09:00"}, NOW)[0] is None              # 実際の時刻より前には戻さない
    assert "タイムゾーン" in sc.parse({"set_to": "2026-11-13T19:51:00"}, NOW)[1]
    assert "ISO 8601" in sc.parse({"set_to": "あした"}, NOW)[1]


def test_advance_and_reset():
    assert sc.parse({"advance_minutes": 5}, NOW)[0] == {"advance_seconds": 300}
    assert sc.parse({"advance_hours": 24}, NOW)[0] == {"advance_seconds": 86400}
    assert sc.parse({"advance_hours": 0.5}, NOW)[0] == {"advance_seconds": 1800}
    assert sc.parse({"reset_clock": True}, NOW)[0] == {"reset_clock": True, "advance_seconds": 0}
    assert sc.parse({}, NOW)[0] == {"reset_clock": False, "advance_seconds": 0}            # 再起動だけ
    assert sc.parse({"advance_minutes": -1}, NOW)[0] is None
    assert sc.parse({"advance_minutes": True}, NOW)[0] is None
    assert "1 つだけ" in sc.parse({"advance_minutes": 5, "reset_clock": True}, NOW)[1]
    assert "1 つだけ" in sc.parse({"advance_minutes": 5, "set_to": "2026-11-13T19:51:00+09:00"}, NOW)[1]
