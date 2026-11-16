"""roadmap.yaml の商品化の条件（commercial）の形。合格線を決める前に観点だけを置いている"""
import os
import sys

import yaml

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import status_report as sr  # noqa: E402

STATES = {"measured", "partial", "none"}


def test_each_condition_has_views_with_known_state():
    spec = yaml.safe_load(sr.ROADMAP.read_text())
    conds = spec["commercial"]
    assert {c["id"] for c in conds} >= {"guardrails", "judgment_engine", "signal_improvement", "ui_quality", "outside_contact"}
    for c in conds:
        assert c.get("views"), c["id"]
        for v in c["views"]:
            assert v["ja"] and v["state"] in STATES, (c["id"], v)


def test_no_pass_line_until_decided():
    # 合格線（measure）が決まるまで日付の見込みを出さないので、まだ書かない
    spec = yaml.safe_load(sr.ROADMAP.read_text())
    assert all("measure" not in c for c in spec["commercial"])
    assert spec.get("commercial_note")
