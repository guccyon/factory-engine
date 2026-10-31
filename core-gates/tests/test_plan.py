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


def S(*roles):
    return [{"role": r, "focus": "f", "why": "w", "effort": "standard"} for r in roles]


def O(ready=True, steps=("ui_design", "implement"), scale="screen", given=False, conf=0.9, questions=()):
    return {"ready": ready, "steps": S(*steps), "design_scale": scale, "design_direction_given": given, "confidence": conf,
            "questions": list(questions), "alternatives": []}


@pytest.mark.parametrize("out,facts,round_,want", [
    (O(steps=["implement"], scale="none"), DS, 1, ("ready", ["implement"], [], [])),
    (O(), DS, 1, ("ready", ["ui_design", "implement"], [], [])),
    (O(steps=["design_system", "ui_design", "implement"]), NO_DS, 1, ("ready", ["design_system", "ui_design", "implement"], [], [])),
    (O(steps=["design_system"], scale="system"), NO_DS, 1, ("ready", ["design_system"], [], [])),
    (O(steps=["ui_design"]), DS, 1, ("ready", ["ui_design"], [], [])),                                 # 型にない並びも組める
    # 経路は変えず、よくある例との食い違いに印だけ付ける
    (O(scale="small"), DS, 1, ("ready", ["ui_design", "implement"], [], ["small_change_with_designer"])),
    (O(steps=["implement"], scale="screen"), DS, 1, ("ready", ["implement"], [], ["screen_without_designer"])),
    (O(steps=["implement", "ui_design"]), DS, 1, ("ready", ["implement", "ui_design"], [], ["design_after_implement"])),
    # 安全のための最小限の決定論
    (O(), NO_DS, 1, ("ask", ["ui_design", "implement"], ["no_design_system"], [])),                   # デザインシステムがなく指定もない
    (O(given=True), NO_DS, 1, ("ready", ["ui_design", "implement"], [], [])),                         # 「既存の画面に倣う」と書いてある
    (O(conf=0.4), DS, 1, ("escalate", ["ui_design", "implement"], ["low_confidence"], [])),
    (O(conf=None), DS, 1, ("escalate", ["ui_design", "implement"], ["low_confidence"], [])),
    (O(ready=False, questions=Q), DS, 1, ("ask", ["ui_design", "implement"], [], [])),
    (O(ready=False, questions=Q), DS, 3, ("escalate", ["ui_design", "implement"], ["rounds_exhausted"], [])),
    (O(ready=False), DS, 1, ("escalate", ["ui_design", "implement"], ["no_questions"], [])),
    (O(steps=[], scale="none"), DS, 1, ("escalate", [], ["empty_steps"], [])),
])
def test_decide(out, facts, round_, want):
    assert pl.decide(out, facts, round_, DEFS) == want


def test_steps_of_drops_unknown_and_repeats():
    out = {"steps": [{"role": "bogus"}, {"role": "ui_design"}, {"role": "ui_design"}, "x", {"role": "implement"}]}
    assert pl.steps_of(out, DEFS) == ["ui_design", "implement"]


def test_no_design_system_adds_default_question():
    out = O()
    pl.decide(out, NO_DS, 1, DEFS)
    assert out["questions"] == [pl.NO_DESIGN_SYSTEM_QUESTION] and len(out["questions"][0]["options"]) == 3


def test_versions_reads_git_heads(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    v = pl.versions(DEFS, tmp_path)
    assert len(v["product"]) == 40 and v["planner_definitions"] == DEFS["schema_version"]


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
    assert s["steps"]["items"]["properties"]["role"]["enum"] == list(DEFS["roles"])
    assert s["steps"]["items"]["properties"]["effort"]["enum"] == list(DEFS["efforts"])
    assert "alternatives" in pl.schema(DEFS)["required"]
    assert set(DEFS["guards"]) == {"no_design_system", "low_confidence", "rounds_exhausted", "no_questions", "empty_steps"}
    assert set(DEFS["advisories"]) == {"small_change_with_designer", "screen_without_designer", "design_after_implement"}
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
        return agent_runner.Result(output={**O(steps=["implement"], scale="small"), "summary": "s", "evidence": [], "interpretation": "i"},
                                   cost_usd=0.05, model="m")

    monkeypatch.setattr(agent_runner, "run", fake)
    issue = {"title": "項目を足す", "body": "本文", "comments": [{"createdAt": "t", "body": "返事です"}]}
    out = pl.plan(issue, {"round": 2}, DEFS, repo)
    assert seen["step"] == "plan" and "2 回目" in seen["prompt"] and "デザインシステムは ある" in seen["prompt"]
    assert "specs/design/tokens.yaml" in seen["files"] and "knowledge/k1.yaml" in seen["files"] and "specs/shot.png" not in seen["files"]
    assert "返事です" in seen["comments"]
    assert (out["decision"], out["steps"], out["guards"], out["advisories"], out["round"]) == ("ready", ["implement"], [], [], 2)
    assert out["cost_usd"] == 0.05 and out["versions"]["prompt"] == pl.PROMPT_VERSION and out["versions"]["product"] is None
    assert "よくある並びの例（縛りではない" in seen["prompt"] and "alternatives" in seen["prompt"]


def test_main_writes_result(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runner, "run", lambda *a, **k: agent_runner.Result(output=O(steps=["implement"], scale="none"), cost_usd=0.0, model="m"))
    (tmp_path / "issue.json").write_text(json.dumps({"title": "t", "body": "b"}))
    assert pl.main(["--issue", str(tmp_path / "issue.json"), "--out", str(tmp_path / "out.json")]) == 0
    assert json.loads((tmp_path / "out.json").read_text())["decision"] == "ready"
