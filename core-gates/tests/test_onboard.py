"""オンボーディングの棚卸しと解析（core-gates/onboard.py、ADR 0045 の追記 2026-11-15）: 材料の置き方・型・読むだけの道具。例は架空の製品"""
import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_runner
import onboard as ob

INP = {"composition": {"repos": [{"repo": "example/api", "role": "backend", "access": "read"},
                                 {"repo": "example/web", "role": "web", "access": "write"}]},
       "answers": {"web_scope": {"question": "最初に作るもの", "value": "予約の画面を Web に", "note": "まず閲覧だけ"}},
       "files": [{"question": "docs", "name": "運用.md", "bytes": 10, "text": "# 運用\n"}, {"question": "design", "name": "a.png", "bytes": 99}],
       "previous_questions": [{"id": "a-3-1", "question": "staging はあるか"}]}


def test_schema_and_read_only_step():
    s = ob.schema()
    assert {"repos", "api", "screens", "network", "docs", "levels", "met", "questions"} <= set(s["required"])
    assert s["properties"]["met"]["items"]["enum"] == ob.MET and set(s["properties"]["levels"]["required"]) == set(ob.BUILT)
    agents = yaml.safe_load((Path(ob.__file__).resolve().parent.parent / "definitions" / "agents.yaml").read_text(encoding="utf-8"))
    assert agents["steps"]["onboard"]["tools"] == ["Read", "Grep", "Glob"]   # 読むだけ


def test_stage_and_run(tmp_path, monkeypatch):
    (tmp_path / "example__api").mkdir()
    seen = {}

    def fake(step, prompt, **kw):
        seen.update(step=step, cwd=os.getcwd(), prompt=prompt, max_turns=kw["max_turns"])
        return agent_runner.Result(output={"summary": "s"}, cost_usd=1.0, model="m")

    monkeypatch.setattr(agent_runner, "run", fake)
    inp = tmp_path / "in.json"
    inp.write_text(json.dumps(INP, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out.json"
    assert ob.main(["--input", str(inp), "--root", str(tmp_path), "--out", str(out)]) == 0
    assert seen["step"] == "onboard" and Path(seen["cwd"]) == tmp_path and "読むだけ" in seen["prompt"]
    w = tmp_path / ob.WORKDIR
    repos = (w / "repos.md").read_text(encoding="utf-8")
    assert "| example__api | example/api | backend | 読み取りだけ |" in repos and "（取れなかった） | example/web" in repos
    assert "予約の画面を Web に" in (w / "answers.md").read_text(encoding="utf-8")
    assert (w / "files" / "01.txt").read_text(encoding="utf-8") == "# 運用\n" and "（本文は渡していない）a.png" in (w / "files.md").read_text(encoding="utf-8")
    assert "a-3-1" in (w / "previous_questions.md").read_text(encoding="utf-8")
    assert json.loads(out.read_text(encoding="utf-8"))["analysis"] == {"summary": "s"}
    monkeypatch.setattr(agent_runner, "run", lambda *a, **k: agent_runner.Result(output=None, cost_usd=0.0, model="m"))
    assert ob.main(["--input", str(inp), "--root", str(tmp_path), "--out", str(out)]) == 1
