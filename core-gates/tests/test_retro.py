"""振り返りのワーカー（ADR 0061）: ロールの委員を 1 回ずつ呼び、意見を立場付きで集め、リーダーに渡す。"""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import retro  # noqa: E402

DEFS = yaml.safe_load((Path(__file__).resolve().parents[2] / "definitions" / "knowledge.yaml").read_text(encoding="utf-8"))
INP = {"sprint": 3, "tickets": [{"ref": "o/r#1"}, {"ref": "o/r#2"}],
       "roles": [{"role": "実装", "tickets": ["o/r#1", "o/r#2"]}, {"role": "QA", "tickets": ["o/r#2"]}, {"role": "検査", "tickets": ["o/r#1"]}],
       "prompts": DEFS["retro"]["prompts"], "domain_destinations": DEFS["retro"]["domain_destinations"]}
F = {"items": 0, "stale": [], "unused": [], "files_in_root": 0, "structure_signal": False}


def repo(tmp_path):
    (tmp_path / ".factory-config" / "knowledge").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "domain.md").write_text("# ドメイン", encoding="utf-8")
    return tmp_path


def test_roles_then_lead(tmp_path):
    calls = []

    def runner(step, prompt, schema=None, max_turns=0):
        calls.append((step, prompt, sorted(os.listdir("."))))
        if step == "retro.role":
            if "「検査」" in prompt:
                raise RuntimeError("down")
            return SimpleNamespace(output={"opinions": [{"perspective": "計画と分割", "tickets": ["o/r#1"], "kind": "problem",
                                                         "observation": "x", "proposal": "y"}]}, cost_usd=0.5, model="s", usage={})
        assert json.loads(Path("opinions.json").read_text(encoding="utf-8"))[0]["role"] == "実装"
        return SimpleNamespace(output={"summary": "まとめ", "knowledge": []}, cost_usd=1.0, model="o", usage={"t": 1})

    out = retro.run(repo(tmp_path), INP, F, DEFS, runner=runner)
    assert [c[0] for c in calls] == ["retro.role"] * 3 + ["retro.lead"]
    assert "docs" in calls[0][2] and "retro.json" in calls[0][2] and "knowledge" in calls[0][2]
    assert "立場は「実装」" in calls[0][1] and "計画と分割" in calls[0][1] and "観点はこれに限りません" in calls[0][1]
    assert [o["role"] for o in out["opinions"]] == ["実装", "QA"]
    assert out["failed_roles"][0]["role"] == "検査" and out["cost_usd"] == 2.0 and out["lead"]["summary"] == "まとめ"


def test_lead_schema_has_two_domain_destinations():
    s = retro.lead_schema(DEFS)
    k = s["properties"]["knowledge"]["items"]["properties"]
    assert k["destination"]["enum"] == ["knowledge", "product_docs"]
    assert "工場をやめても製品に残すべき知識か" in retro.lead_prompt(INP, [], F)
