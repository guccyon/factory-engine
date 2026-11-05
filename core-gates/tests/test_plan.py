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
    assert set(DEFS["guards"]) == {"no_design_system", "low_confidence", "rounds_exhausted", "no_questions", "empty_steps", "oversized"}
    assert set(DEFS["advisories"]) == {"small_change_with_designer", "screen_without_designer", "design_after_implement", "no_size", "split_after_design"}
    assert s["size"]["properties"]["layers"]["items"]["enum"] == [x["layer"] for x in DEFS["sizing"]["layers"]] + ["other"]
    assert s["tasks"]["items"]["required"][:3] == ["key", "title", "kind"]   # 窓口の相談役と同じ型
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
        seen["sizes"] = Path("sizes.md").read_text(encoding="utf-8")
        return agent_runner.Result(output={**O(steps=["implement"], scale="small"), "summary": "s", "evidence": [], "interpretation": "i",
                                           "size": {"layers": ["web"], "turns_band": "S", "basis": "b"}},
                                   cost_usd=0.05, model="m")

    monkeypatch.setattr(agent_runner, "run", fake)
    issue = {"title": "項目を足す", "body": "本文", "comments": [{"createdAt": "t", "body": "返事です"}]}
    table = [{"layers": "backend+web", "n": 4, "files_median": 22, "turns_median": 48, "turns_p90": 70, "incomplete": 1,
              "examples": ["https://github.com/o/r/pull/9"]}]
    out = pl.plan(issue, {"round": 2, "size_table": table}, DEFS, repo)
    assert "| backend+web | 4 | 22 | 48 | 70 | 1 |" in seen["sizes"] and "sizes.md" in seen["prompt"]
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


def SZ(layers=("web",), band="M", files=5, tasks=(), steps=("implement",)):
    return {**O(steps=steps, scale="screen"), "size": {"layers": list(layers), "turns_band": band, "basis": "b"},
            "expected_files": [f"f{i}" for i in range(files)], "tasks": list(tasks)}


def T(key, kind="implement", deps=()):
    return {"key": key, "title": f"題 {key}", "kind": kind, "summary": "s", "do": ["する"], "acceptance": ["できる"],
            "depends_on": list(deps), "effort": "standard"}


TASKS = [T("t1", "contract"), T("t2", deps=["t1"]), T("t3", "design", deps=["t2"])]


@pytest.mark.parametrize("out,want", [
    (SZ(), ([], [])),
    (SZ(layers=["contract", "db", "backend", "web"]), (["layers"], "oversized")),           # 層が多いのにタスクがない → 人へ
    (SZ(layers=["backend", "docs", "other"], files=31), (["files"], "oversized")),           # docs・other は層に数えない
    (SZ(band="L"), (["turns"], "oversized")),
    (SZ(band="XL", tasks=TASKS), (["turns"], "split")),
    (SZ(band="XL", tasks=TASKS[:1]), (["turns"], "oversized")),                              # 1 件だけでは切ったことにならない
    (SZ(band="XL", tasks=[T("t1", deps=["t2"]), T("t2", deps=["t1"])]), (["turns"], "oversized")),   # 依存の輪
])
def test_oversize_and_split(out, want):
    hit, kind = want
    assert pl.oversize(out, DEFS) == hit
    decision, guards, marks, tasks = pl.size_check(out, "ready", pl.steps_of(out, DEFS), DEFS)
    if not hit:
        assert (decision, guards, marks, tasks) == ("ready", [], [], [])
    elif kind == "split":
        assert decision == "split" and guards == ["oversized"] and [t["key"] for t in tasks] == ["t1", "t2", "t3"]
    else:
        assert (decision, guards, tasks) == ("escalate", ["oversized"], [])


def test_size_check_waits_for_design_and_marks_missing_size():
    big = SZ(band="XL", steps=("ui_design", "implement"))
    assert pl.size_check(big, "ready", ["ui_design", "implement"], DEFS) == ("ready", [], ["split_after_design"], [])
    big["tasks"] = TASKS   # 実装の手前の判定（画面が決まった後）では切る
    assert pl.size_check(big, "ready", ["ui_design", "implement"], DEFS, phase="implement")[0] == "split"
    assert pl.size_check(O(steps=["implement"]), "ready", ["implement"], DEFS) == ("ready", [], ["no_size"], [])
    assert pl.size_check(O(steps=["design_system"]), "ready", ["design_system"], DEFS) == ("ready", [], [], [])   # 実装役を通らない経路は見ない
    assert pl.size_check(SZ(band="XL"), "ask", ["implement"], DEFS)[0] == "ask"
