"""プランナー（core-gates/plan.py、ADR 0025・0026）: 決定論の当てはめ（decide）、製品から読む事実、材料の写し方"""
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_runner
import plan as pl

DEFS = yaml.safe_load(pl.DEFS.read_text(encoding="utf-8"))
DS = {"has_design_system": True}
NO_DS = {"has_design_system": False}
Q = [{"question": "どれにしますか", "why": "決まると着手できる", "options": ["a", "b"], "recommended": 0}]


def O(ready=True, route="ui_design", scale="screen", given=False, conf=0.9, questions=()):
    return {"ready": ready, "route": route, "design_scale": scale, "design_direction_given": given, "confidence": conf, "questions": list(questions)}


@pytest.mark.parametrize("out,facts,round_,want", [
    (O(route="implement", scale="none"), DS, 1, ("ready", "implement", [])),
    (O(scale="small"), DS, 1, ("ready", "implement", ["small_change_direct"])),               # 小さな改修はデザイナーを通さない
    (O(route="implement", scale="screen"), DS, 1, ("ready", "ui_design", ["screen_needs_design"])),
    (O(route="ui_design", scale="system"), DS, 1, ("ready", "design_system", ["system_scale"])),
    (O(), DS, 1, ("ready", "ui_design", [])),
    (O(), NO_DS, 1, ("ask", "ui_design", ["no_design_system"])),                               # デザインシステムがなく指定もない
    (O(given=True), NO_DS, 1, ("ready", "ui_design", [])),                                     # 「既存の画面に倣う」と書いてある
    (O(route="design_system", scale="system"), NO_DS, 1, ("ready", "design_system", [])),
    (O(conf=0.4), DS, 1, ("escalate", "ui_design", ["low_confidence"])),
    (O(conf=None), DS, 1, ("escalate", "ui_design", ["low_confidence"])),
    (O(ready=False, questions=Q), DS, 1, ("ask", "ui_design", [])),
    (O(ready=False, questions=Q), DS, 3, ("escalate", "ui_design", ["rounds_exhausted"])),
    (O(ready=False), DS, 1, ("escalate", "ui_design", ["no_questions"])),
    (O(route="bogus", scale="bogus"), DS, 1, ("ready", "implement", [])),
])
def test_decide(out, facts, round_, want):
    assert pl.decide(out, facts, round_, DEFS) == want


def test_no_design_system_adds_default_question():
    out = O()
    pl.decide(out, NO_DS, 1, DEFS)
    assert out["questions"] == [pl.NO_DESIGN_SYSTEM_QUESTION] and len(out["questions"][0]["options"]) == 3


def test_facts(tmp_path):
    assert pl.facts(None)["has_design_system"] is False
    assert pl.facts(tmp_path) == {"has_design_system": False, "design_files": [], "has_knowledge": False}
    (tmp_path / "specs" / "design").mkdir(parents=True)
    (tmp_path / "specs" / "design" / "README.md").write_text("x")
    assert pl.facts(tmp_path)["has_design_system"] is False      # トークンがなければ、まだない
    (tmp_path / "specs" / "design" / "tokens.yaml").write_text("color: {}\n")
    (tmp_path / ".factory-config" / "knowledge").mkdir(parents=True)
    f = pl.facts(tmp_path)
    assert f["has_design_system"] and f["has_knowledge"] and "specs/design/tokens.yaml" in f["design_files"]


def test_schema_matches_definitions():
    s = pl.schema(DEFS)["properties"]
    assert s["route"]["enum"] == ["implement", "ui_design", "design_system"]
    assert set(s["design_scale"]["enum"]) == set(DEFS["design_scales"])
    assert s["questions"]["maxItems"] == DEFS["asking"]["max_questions"]


def test_plan_stages_reads_and_decides(tmp_path, monkeypatch):
    repo = tmp_path / "product"
    (repo / "specs" / "design").mkdir(parents=True)
    (repo / "specs" / "design" / "tokens.yaml").write_text("color: {}\n")
    (repo / "specs" / "shot.png").write_bytes(b"x")
    (repo / ".factory-config" / "knowledge").mkdir(parents=True)
    (repo / ".factory-config" / "knowledge" / "k1.yaml").write_text("id: k1\n")
    seen = {}

    def fake(step, prompt, *, schema=None, tier=None, max_turns=3, retries=3):
        seen["step"], seen["prompt"] = step, prompt
        seen["files"] = sorted(str(p) for p in Path(".").rglob("*") if p.is_file())
        seen["comments"] = Path("comments.md").read_text(encoding="utf-8")
        return agent_runner.Result(output={**O(scale="small"), "summary": "s", "evidence": [], "interpretation": "i"}, cost_usd=0.05, model="m")

    monkeypatch.setattr(agent_runner, "run", fake)
    issue = {"title": "項目を足す", "body": "本文", "comments": [{"createdAt": "t", "body": "返事です"}]}
    out = pl.plan(issue, {"round": 2}, DEFS, repo)
    assert seen["step"] == "plan" and "2 回目" in seen["prompt"] and "デザインシステムは ある" in seen["prompt"]
    assert "specs/design/tokens.yaml" in seen["files"] and "knowledge/k1.yaml" in seen["files"] and "specs/shot.png" not in seen["files"]
    assert "返事です" in seen["comments"]
    assert (out["decision"], out["route"], out["notes"], out["round"]) == ("ready", "implement", ["small_change_direct"], 2)
    assert out["cost_usd"] == 0.05


def test_main_writes_result(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runner, "run", lambda *a, **k: agent_runner.Result(output=O(route="implement", scale="none"), cost_usd=0.0, model="m"))
    (tmp_path / "issue.json").write_text(json.dumps({"title": "t", "body": "b"}))
    assert pl.main(["--issue", str(tmp_path / "issue.json"), "--out", str(tmp_path / "out.json")]) == 0
    assert json.loads((tmp_path / "out.json").read_text())["decision"] == "ready"
