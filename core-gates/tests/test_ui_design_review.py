"""デザイン検査役（core-gates/ui_design_review.py、ADR 0025）: 委任の範囲の当て方（route）、材料の写し方、答えの形"""
import base64
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_runner
import ui_design_review as rv

DEFS = yaml.safe_load(rv.DEFS.read_text(encoding="utf-8"))
SPEC = {"screens": [{"id": "s", "new": False}], "brand_impact": False, "variants": [{"id": "a"}, {"id": "b"}], "recommended": "a"}


def V(decision="approve", conf=0.9, findings=()):
    return {"decision": decision, "chosen_variant": "a", "confidence": conf, "findings": list(findings)}


MUST = {"severity": "must", "viewpoint": "states", "variant": "a", "evidence": "失敗がない", "fix": "足す"}


@pytest.mark.parametrize("verdict,spec,round_,want", [
    (V(), SPEC, 1, ("approve", [])),
    (V(), {**SPEC, "screens": [{"id": "s", "new": True}]}, 1, ("human", ["new_screen"])),
    (V(), {**SPEC, "brand_impact": True}, 1, ("human", ["brand_impact"])),
    (V(conf=0.5), SPEC, 1, ("human", ["low_confidence"])),
    (V(conf=None), SPEC, 1, ("human", ["low_confidence"])),
    (V("human"), SPEC, 1, ("human", ["inspector"])),
    (V("bogus"), SPEC, 1, ("human", ["inspector"])),
    (V("revise"), SPEC, 1, ("revise", [])),
    (V("revise"), {**SPEC, "brand_impact": True}, 1, ("revise", [])),          # 直しは人に回す前に 1 回使う
    (V("revise"), SPEC, 2, ("human", ["revisions_exhausted"])),
    (V("approve", findings=[MUST]), SPEC, 1, ("revise", [])),                  # must が残る案は承認しない
    (V("approve", findings=[MUST]), SPEC, 2, ("human", ["revisions_exhausted"])),
    (V("approve", findings=[{**MUST, "severity": "should"}]), SPEC, 1, ("approve", [])),
])
def test_route(verdict, spec, round_, want):
    assert rv.route(verdict, spec, round_, DEFS) == want


def test_review_stages_copies_and_routes(tmp_path, monkeypatch):
    repo = tmp_path / "product"
    (repo / "specs" / "design").mkdir(parents=True)
    (repo / "specs" / "design" / "tokens.yaml").write_text("color: {}\n", encoding="utf-8")
    (repo / "specs" / "photo.jpg").write_bytes(b"x")
    seen = {}

    def fake(step, prompt, *, schema=None, tier=None, max_turns=3, retries=3):
        root = Path(".")
        seen["files"] = sorted(str(p) for p in root.rglob("*") if p.is_file())
        seen["enum"] = schema["properties"]["chosen_variant"]["enum"]
        seen["prompt"] = prompt
        return agent_runner.Result(output={**V(), "checked": ["states"], "evidence": ["mocks/a.png"], "interpretation": "i", "human_reason": ""},
                                   cost_usd=0.2, model="m")

    monkeypatch.setattr(agent_runner, "run", fake)
    inp = {"round": 1, "issue": {"title": "t", "body": "受け入れ条件"}, "uxspec": SPEC,
           "files": {"a.png": base64.b64encode(b"png").decode(), "a.html": base64.b64encode(b"<p>").decode(),
                     "../evil.html": base64.b64encode(b"x").decode()}}
    out = rv.review(inp, DEFS, repo)
    assert out["decision"] == "approve" and out["verdict"]["chosen_variant"] == "a"
    assert seen["enum"] == ["a", "b"] and "mocks/a.png" in seen["prompt"]
    assert seen["files"] == ["issue.md", "mocks/a.html", "mocks/a.png", "specs/design/tokens.yaml", "uxspec.yaml"]   # 名前の崩れたファイルと画像の規範は写さない


def test_main_rejects_input_without_variants(tmp_path):
    p = tmp_path / "in.json"
    p.write_text(json.dumps({"uxspec": {}}), encoding="utf-8")
    assert rv.main(["--input", str(p), "--out", str(tmp_path / "o.json")]) == 2
