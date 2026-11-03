"""建設日報の推移の費用は、配信日ではなく数えた日（前日）を軸に出す"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import status_report as sr  # noqa: E402


def test_cost_axis_is_the_counted_day():
    h = [{"date": "2026-11-03", "overall": 0.46, "cost_usd": 2102.85}, {"date": "2026-11-04", "overall": 0.46, "cost_usd": 944.66}]
    out = "\n".join(sr.fmt_trend(h))
    assert 'x-axis ["11-03", "11-04"]' in out           # 進捗は配信日
    assert 'x-axis ["11-02", "11-03"]' in out           # 費用は前日の分
    assert sr.cost_day({"date": "2026-11-01"}) == "2026-10-31"
    assert sr.cost_day({"date": "2026-11-04", "cost_date": "2026-11-02"}) == "2026-11-02"
