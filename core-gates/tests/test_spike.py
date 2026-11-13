"""スパイクのレーンの System Architect（core-gates/spike.py、ADR 0055）: 材料の置き方・問い・型"""
import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_runner
import spike as sk

DEFS = yaml.safe_load(sk.DEFS.read_text(encoding="utf-8"))
DD = {"summary": "s", "answers": [{"question": "q", "answer": "a"}], "cause": {"statement": "段の記憶に頼る", "evidence": ["lib/a.dart:10"]},
      "approach": {"kind": "redesign", "description": "サーバーの状態から次の操作を出す", "layers": ["backend", "mobile"], "files": ["lib/a.dart"]},
      "alternatives": [], "bundle": [1424, 1425], "experience_change": True, "experience_note": "ホームに欄が増える", "risks": [], "confidence": 0.8}


def test_schema_and_definitions():
    s = sk.schema(DEFS)
    assert s["properties"]["approach"]["properties"]["kind"]["enum"] == ["fix", "revert", "redesign"]
    assert {"cause", "approach", "bundle", "experience_change", "confidence"} <= set(s["required"])
    assert DEFS["spike"]["roles"]["architect"]["ja"] == "System Architect"
    agents = yaml.safe_load((sk.HERE.parent / "definitions" / "agents.yaml").read_text(encoding="utf-8"))
    assert agents["steps"]["spike"]["tools"] == ["Read", "Grep", "Glob"]   # 読むだけ


def test_spike_stages_materials_and_returns_designdoc(tmp_path, monkeypatch):
    repo = tmp_path / "product"
    (repo / "lib").mkdir(parents=True)
    (repo / "lib" / "a.dart").write_text("x")
    seen = {}

    def fake(step, prompt, *, schema=None, tier=None, max_turns=3, retries=3):
        seen["step"], seen["prompt"], seen["turns"] = step, prompt, max_turns
        seen["q"] = Path(".spike/questions.md").read_text(encoding="utf-8")
        seen["d"] = Path(".spike/defects.md").read_text(encoding="utf-8")
        seen["b"] = Path(".spike/defect_bodies.md").read_text(encoding="utf-8")
        seen["cwd_has_code"] = Path("lib/a.dart").exists()
        return agent_runner.Result(output=DD, cost_usd=0.3, model="m")

    monkeypatch.setattr(agent_runner, "run", fake)
    inp = {"questions": ["どこで状態を失うか"], "open_defects": [{"issue": 1424, "severity": "s2", "title": "戻れない | 確定"}],
           "defect_bodies": {"1424": "再現の手順"}}
    out = sk.spike({"number": 1436, "title": "t", "body": "#1424", "comments": []}, inp, DEFS, repo)
    assert seen["step"] == "spike" and seen["turns"] == DEFS["spike"]["max_turns"] and seen["cwd_has_code"]
    assert "どこで状態を失うか" in seen["q"] and "#1424" in seen["d"] and "| 確定" not in seen["d"] and "再現の手順" in seen["b"]
    assert "経路・見積もり・着手の時期" in seen["prompt"] and "experience_change" in seen["prompt"]
    assert out["designdoc"]["approach"]["kind"] == "redesign" and out["cost_usd"] == 0.3


def test_main_fails_without_designdoc(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runner, "run", lambda *a, **k: agent_runner.Result(output=None, cost_usd=0.0, model="m"))
    (tmp_path / "issue.json").write_text(json.dumps({"title": "t", "body": "b"}))
    (tmp_path / "p").mkdir()
    assert sk.main(["--issue", str(tmp_path / "issue.json"), "--repo-dir", str(tmp_path / "p"), "--out", str(tmp_path / "o.json")]) == 1
