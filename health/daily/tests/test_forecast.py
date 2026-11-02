"""建設日報の Gen1 の完了の見込み（health/daily/status_report.py の gen1_forecast）"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import status_report as sr  # noqa: E402


def progress(*phases):
    ps = [{"id": i, "ja": i, "weight": w, "progress": p} for i, w, p in phases]
    wsum = sum(w for _, w, _ in phases)
    return {"overall": sum(w * p for _, w, p in phases) / wsum, "phases": ps}


def test_done_phases_have_no_date_and_rest_follow_in_order():
    pr = progress(("P0", 2, 1.0), ("P1", 2, 0.5), ("P2", 4, 0.0))   # 全体 0.375
    a, half = sr.gen1_forecast(pr, "2026-10-29", start="2026-10-25")  # 4 日で 0.375 → 1 日 0.09375
    assert a["phases"][0] == {"id": "P0", "ja": "P0", "done": True, "date": None}
    # P1 まで残り 1/8 → 1.33 日、P2 まで残り 5/8 → 6.67 日
    assert a["phases"][1]["date"] == "2026-10-30"
    assert a["phases"][2]["date"] == "2026-11-04"
    assert half["phases"][2]["date"] == "2026-11-11"
    assert a["pace_pt"] == 9.4 and half["pace_pt"] == 4.7


def test_no_progress_gives_no_date():
    a, _ = sr.gen1_forecast(progress(("P0", 1, 0.0)), "2026-10-29", start="2026-10-25")
    assert a["phases"][0]["date"] is None
