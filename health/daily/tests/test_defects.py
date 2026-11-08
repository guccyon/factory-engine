"""品質（Defect）の指標（ADR 0028 の決定 5。P5a の defect_metrics）: 流出防止率・発見工程の分布・出やすい観点・紐付け率"""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import status_report as sr  # noqa: E402

DEFS = yaml.safe_load(sr.DEFECTS.read_text(encoding="utf-8"))


def test_escape_prevention_and_distribution():
    rows = [{"found_in": "pr", "viewpoint": "R3", "change": "u", "caused_in": "implement"},
            {"found_in": "ci", "viewpoint": "ci:test", "change": "u", "caused_in": "implement"},
            {"found_in": "ci", "viewpoint": "ci:test", "change": "v", "caused_in": "implement"},
            {"found_in": "qa", "viewpoint": "flow", "release_id": "r1"}]
    dm = sr.defect_metrics(rows, DEFS)
    assert dm["total"] == 4 and dm["caught_before_merge"] == 3 and dm["escape_prevention"] == 0.75 and dm["prod"] == 0
    assert [(x["id"], x["n"]) for x in dm["by_found_in"]] == [("upstream", 0), ("pr", 1), ("ci", 2), ("qa", 1), ("diagnose", 0), ("prod", 0)]
    assert dm["hotspots"][0] == {"viewpoint": "ci:test", "n": 2}
    assert dm["link_rate"] == {"release_id": 0.25, "change": 0.75, "caused_in": 0.75}
    text = "\n".join(sr.fmt_defects(dm))
    assert "流出防止率 75%" in text and "参考" in text and "QA 1" in text and "xychart-beta" in text


def test_no_defects_yet():
    dm = sr.defect_metrics([], DEFS)
    assert dm["escape_prevention"] is None and "まだありません" in sr.fmt_defects(dm)[0]
    assert "読めません" in sr.fmt_defects(None)[0]
