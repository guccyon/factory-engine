"""ワークフローの宣言の決定論（core-gates/workflow.py、ADR 0038）: 飛ばせないステップ・上限の天井と既定値・経路との合わせ方"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import plan as pl
import workflow as wf

DEFS = wf.load()
CODE = ["apps/backend/src/lib.rs"]
DOCS = ["specs/stories/a.yaml", "README.md", "changelog.d/12.md"]


def D(**kw):
    base = {"path": "ticket", "steps": [], "start": {"mode": "now"}, "basis": "b"}
    return {**base, **kw}


def step(norm, name):
    return next(s for s in norm["steps"] if s["step"] == name)


def test_no_declaration_fills_template_and_defaults():
    norm, marks = wf.normalize(None, ["implement"], [], CODE, None, DEFS)
    assert marks == ["no_workflow"]
    assert [s["step"] for s in norm["steps"]] == DEFS["paths"]["ticket"]
    assert step(norm, "implement")["run"] is True and step(norm, "ui_design")["run"] is False
    assert norm["caps"]["implement.turns"] == 60 and norm["caps"]["retries.conflict"] == 3   # 帯がなければ M
    assert norm["handoff"] == {"ship": "train", "learn": "signals"} and norm["start"]["mode"] == "now"


@pytest.mark.parametrize("name", ["checks", "merge", "learn"])
def test_always_mandatory_steps_cannot_be_skipped(name):
    norm, marks = wf.normalize(D(steps=[{"step": name, "run": "false", "why": "要らない"}]), ["implement"], [], CODE, "S", DEFS)
    assert step(norm, name)["run"] is True
    assert f"mandatory_skip:{name}" in marks


def test_inspect_skippable_only_for_docs():
    d = D(steps=[{"step": "inspect", "run": "false", "why": "文書だけ"}])
    norm, marks = wf.normalize(d, ["implement"], [], DOCS, "S", DEFS)
    assert step(norm, "inspect")["run"] is False and not marks
    norm, marks = wf.normalize(d, ["implement"], [], CODE, "S", DEFS)
    assert step(norm, "inspect")["run"] is True and "mandatory_skip:inspect" in marks


def test_inspect_may_be_light_but_checks_not():
    d = D(steps=[{"step": "inspect", "run": "true", "effort": "light", "why": "小さい"},
                 {"step": "checks", "run": "true", "effort": "light", "why": "x"}])
    norm, marks = wf.normalize(d, ["implement"], [], CODE, "S", DEFS)
    assert step(norm, "inspect")["effort"] == "light"
    assert step(norm, "checks")["effort"] == "standard" and "mandatory_skip:checks" in marks


def test_judge_skip_becomes_auto():
    norm, marks = wf.normalize(D(steps=[{"step": "judge", "run": "false", "why": "x"}]), ["implement"], [], CODE, "S", DEFS)
    assert step(norm, "judge")["run"] == "auto" and "mandatory_skip:judge" in marks


def test_qa_can_be_skipped_with_reason_and_scoped():
    d = D(steps=[{"step": "qa", "run": "false", "why": "画面がない"}])
    norm, _ = wf.normalize(d, ["implement"], [], CODE, "S", DEFS)
    assert step(norm, "qa") == {"step": "qa", "run": False, "effort": "standard", "why": "画面がない"}
    d = D(steps=[{"step": "qa", "run": "true", "why": "増やす", "scope": ["specs/stories/a.yaml"]}])
    norm, _ = wf.normalize(d, ["implement"], [], CODE, "S", DEFS)
    assert step(norm, "qa")["scope"] == ["specs/stories/a.yaml"]


def test_route_wins_for_upstream_and_implement():
    d = D(steps=[{"step": "ui_design", "run": "false", "why": "x"}, {"step": "design_system", "run": "true", "why": "y"}])
    details = [{"role": "ui_design", "effort": "deep", "why": "新しい画面"}, {"role": "implement", "effort": "standard", "why": "z"}]
    norm, marks = wf.normalize(d, ["ui_design", "implement"], details, CODE, "M", DEFS)
    assert step(norm, "ui_design") == {"step": "ui_design", "run": True, "effort": "deep", "why": "x"}
    assert step(norm, "design_system")["run"] is False
    assert not [m for m in marks if m.startswith("deep_without_why")]


def test_caps_band_defaults_declared_and_ceiling():
    d = D(caps=[{"kind": "retries.conflict", "value": 2}, {"kind": "implement.turns", "value": 500}, {"kind": "bogus", "value": 1}])
    norm, marks = wf.normalize(d, ["implement"], [], CODE, "L", DEFS)
    assert norm["caps"]["retries.conflict"] == 2
    assert norm["caps"]["implement.turns"] == DEFS["caps"]["ceilings"]["implement.turns"]
    assert "cap_clamped:implement.turns" in marks
    assert norm["caps"]["ticket.cost_usd"] == DEFS["caps"]["defaults"]["L"]["ticket.cost_usd"]
    assert norm["caps_declared"] == {"retries.conflict": 2, "implement.turns": 500}


def test_defaults_never_exceed_ceilings():
    c = DEFS["caps"]
    for band, d in c["defaults"].items():
        for k, v in d.items():
            assert k in c["kinds"], (band, k)
            assert v <= c["ceilings"][k], (band, k)
    assert set(c["ceilings"]) == set(c["kinds"])


def test_deep_without_why_is_marked():
    d = D(steps=[{"step": "inspect", "run": "true", "effort": "deep", "why": ""}])
    _, marks = wf.normalize(d, ["implement"], [], CODE, "M", DEFS)
    assert "deep_without_why:inspect" in marks


def test_start_after_and_unknown_step():
    d = D(start={"mode": "after", "after": [812, "813"], "files": ["apps/admin-web/screenshots/screens.ts"], "why": "同じ登録の箇所"},
          steps=[{"step": "deploy_prod", "run": "true", "why": "x"}])
    norm, marks = wf.normalize(d, ["implement"], [], CODE, "S", DEFS)
    assert norm["start"]["mode"] == "after" and norm["start"]["after"] == [812, 813]
    assert "unknown_step:deploy_prod" in marks
    norm, _ = wf.normalize(D(start={"mode": "after", "after": []}), ["implement"], [], CODE, "S", DEFS)
    assert norm["start"]["mode"] == "now"


def test_handoff_values():
    norm, _ = wf.normalize(D(handoff={"ship": "none", "learn": "case_review"}), ["implement"], [], DOCS, "S", DEFS)
    assert norm["handoff"] == {"ship": "none", "learn": "case_review"}
    norm, _ = wf.normalize(D(handoff={"ship": "prod_now"}), ["implement"], [], DOCS, "S", DEFS)
    assert norm["handoff"]["ship"] == "train"


def test_schema_and_prompt_mention_workflow():
    defs = __import__("yaml").safe_load(pl.DEFS.read_text(encoding="utf-8"))
    sc = pl.schema(defs)
    assert "workflow" in sc["properties"] and "workflow" not in sc["required"]   # 宣言がなくても判定は通る（ひな形で埋める）
    text = pl.prompt(defs, {"has_design_system": True}, 1)
    assert "workflow:" in text and "inflight.md" in text and "caps.md" in text


def test_stage_writes_inflight_and_caps(tmp_path):
    pl.stage(tmp_path, {"title": "t", "body": "b"}, None, [], {
        "inflight": [{"issue": 812, "title": "画面", "state": "active", "files": ["apps/admin-web/src/screens.ts"]}],
        "hot_files": {"apps/admin-web/src/screens.ts": {"conflicts": 4, "cost": 6.1}},
        "cap_hits": [{"issue": 700, "cap": "retries.conflict", "declared": 3, "actual": 5}]})
    assert "#812" in (tmp_path / "inflight.md").read_text() and "4 回" in (tmp_path / "inflight.md").read_text()
    assert "retries.conflict" in (tmp_path / "caps.md").read_text()
