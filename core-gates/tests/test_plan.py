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


SP = {"why": "原因が分からない", "questions": ["どこで状態を失うか"]}


@pytest.mark.parametrize("out,facts,want", [
    ({**O(ready=False), "spike": SP}, DS, "spike"),                                                   # 方針が要る（ADR 0055）
    ({**O(ready=False), "spike": {"why": "w", "questions": [" "]}}, DS, "escalate"),                  # 決めることがない
    ({**O(ready=False), "spike": SP}, {**DS, "designdocs": 1}, "escalate"),                           # スパイクはもう通した
    ({**O(), "spike": SP}, DS, "ready"),                                                              # 着手できるならスパイクは飛ばす
])
def test_decide_spike(out, facts, want):
    assert pl.decide(out, facts, 1, DEFS)[0] == want


def test_designdocs_counts_marked_comments():
    issue = {"comments": [{"body": "<!-- factory:designdoc -->\n## 設計書"}, {"body": "ふつうのコメント"}]}
    assert pl.designdocs(issue, DEFS) == 1
    assert "spike" in pl.schema(DEFS)["properties"] and "spike:" in pl.prompt(DEFS, DS, 1)


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
    assert pl.facts(tmp_path) == {"has_design_system": False, "design_files": [], "has_knowledge": False, "story_ids": [],
                                 "adr_ids": [], "contract_files": [], "kpi_ids": []}
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
    assert set(DEFS["guards"]) == {"no_design_system", "low_confidence", "rounds_exhausted", "no_questions", "empty_steps", "oversized",
                                   "spike_exhausted", "spike_no_questions"}
    assert set(DEFS["advisories"]) == {"small_change_with_designer", "screen_without_designer", "design_after_implement", "no_size", "split_after_design",
                                       "no_requirements", "no_stories", "unknown_story", "story_exists", "unknown_kpi",
                                       "unknown_adr", "unknown_contract", "breaking_contract", "no_contract_diff"}
    assert s["stories"]["items"]["properties"]["action"]["enum"] == list(DEFS["upstream"]["story_actions"]) == ["add", "change", "keep"]
    assert s["requirements"]["required"] == DEFS["upstream"]["requirements"]["required"]
    assert {"requirements", "stories"} <= set(pl.schema(DEFS)["required"])
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
    (repo / "specs" / "stories").mkdir()
    (repo / "specs" / "stories" / "coach-replies.yaml").write_text("type: UserStory\nid: coach-replies\n")
    seen = {}

    def fake(step, prompt, *, schema=None, tier=None, max_turns=3, retries=3):
        seen["step"], seen["prompt"] = step, prompt
        seen["files"] = sorted(str(p) for p in Path(".").rglob("*") if p.is_file())
        seen["comments"] = Path("comments.md").read_text(encoding="utf-8")
        seen["sizes"] = Path("sizes.md").read_text(encoding="utf-8")
        return agent_runner.Result(output={**O(steps=["implement"], scale="small"), "summary": "s", "evidence": [], "interpretation": "i",
                                           "size": {"layers": ["web"], "turns_band": "S", "basis": "b"},
                                           "requirements": {"acceptance_criteria": ["一覧に項目が出る"], "non_goals": [], "success_definition": "出る"},
                                           "stories": [{"action": "keep", "id": "coach-replies", "why": "一覧の隣の流れ"}]},
                                   cost_usd=0.05, model="m")

    monkeypatch.setattr(agent_runner, "run", fake)
    issue = {"title": "項目を足す", "body": "本文", "comments": [{"createdAt": "t", "body": "返事です"}]}
    table = [{"layers": "backend+web", "n": 4, "files_median": 22, "turns_median": 48, "turns_p90": 70, "incomplete": 1,
              "examples": ["https://github.com/o/r/pull/9"]}]
    out = pl.plan(issue, {"round": 2, "size_table": table}, DEFS, repo)
    assert "| backend+web | 4 | 22 | 48 | 70 | 1 |" in seen["sizes"] and "sizes.md" in seen["prompt"]
    assert seen["step"] == "plan" and "2 回目" in seen["prompt"] and "デザインシステムは ある" in seen["prompt"]
    # 知見は丸写しにせず、計画の業務で選んだものだけ（ADR 0040 の決定 7 の 3）。形の崩れた k1 は渡らない
    assert "specs/design/tokens.yaml" in seen["files"] and "knowledge/k1.yaml" not in seen["files"] and "specs/shot.png" not in seen["files"]
    assert out["knowledge_ids"] == [] and out["knowledge_selection"]["work"] == ["plan.workflow"]
    assert "返事です" in seen["comments"]
    assert (out["decision"], out["steps"], out["guards"], out["advisories"], out["round"]) == ("ready", ["implement"], [], [], 2)
    assert out["cost_usd"] == 0.05 and out["versions"]["prompt"] == pl.PROMPT_VERSION and out["versions"]["product"] is None
    assert "よくある並びの例（縛りではない" in seen["prompt"] and "alternatives" in seen["prompt"]
    assert "いまある id: coach-replies" in seen["prompt"] and "specs/stories/coach-replies.yaml" in seen["files"]
    assert out["requirements"]["acceptance_criteria"] == ["一覧に項目が出る"] and out["stories"] == [{"action": "keep", "id": "coach-replies", "why": "一覧の隣の流れ"}]


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


def test_pre_implement_phase_keeps_only_the_implementer(tmp_path, monkeypatch):
    seen = {}

    def fake(step, prompt, **k):
        seen["prompt"] = prompt
        return agent_runner.Result(output={**SZ(steps=("ui_design", "implement")), "summary": "s", "evidence": [], "interpretation": "i"},
                                   cost_usd=0.01, model="m")
    monkeypatch.setattr(agent_runner, "run", fake)
    out = pl.plan({"title": "t", "body": "b"}, {"phase": "implement", "continuing_pr": "https://github.com/o/r/pull/9"}, DEFS, None)
    assert out["steps"] == ["implement"] and out["phase"] == "implement"
    assert "実装の手前の判定" in seen["prompt"] and "pull/9" in seen["prompt"] and "t1 にし" in seen["prompt"]
    out = pl.plan({"title": "t", "body": "b"}, {}, DEFS, None)
    assert out["steps"] == ["ui_design", "implement"] and "実装の手前の判定" not in seen["prompt"]


# ------------------------------------------------------------------ 型つきの上流の成果物（P4 の typed_upstream）
def test_story_ids_reads_only_user_stories(tmp_path):
    d = tmp_path / "specs" / "stories"
    d.mkdir(parents=True)
    (d / "actors.yaml").write_text("actors: []\n")
    (d / "a.yaml").write_text("type: UserStory\nid: a-flow\n")
    (d / "b.yaml").write_text(": : broken\n  - [")
    (d / "README.md").write_text("x")
    assert pl.story_ids(tmp_path) == ["a-flow"] and pl.story_ids(None) == []


REQ = {"acceptance_criteria": ["  通知が届く ", ""], "non_goals": ["メール"], "success_definition": "届く"}
KNOWN = {"story_ids": ["coach-replies", "cs-traces"]}


@pytest.mark.parametrize("stories,scale,want_ids,want_marks", [
    ([{"action": "keep", "id": "coach-replies", "why": "w"}], "screen", [("keep", "coach-replies")], []),
    ([{"action": "add", "id": "student-gets-reminder", "why": "w", "title": "生徒が前日に知らせを受け取れる", "actor": "student"}], "feature",
     [("add", "student-gets-reminder")], []),
    ([{"action": "change", "id": "nope", "why": "w"}], "small", [], ["unknown_story"]),                      # 製品にない id は落とす
    ([{"action": "add", "id": "cs-traces", "why": "w"}], "small", [("change", "cs-traces")], ["story_exists"]),   # もうある id を増やす → 変える
    ([{"action": "keep", "id": "Coach Replies", "why": "w"}, {"action": "bogus", "id": "cs-traces", "why": "w"}], "none", [], []),   # 形が誤ったもの
    ([{"action": "keep", "id": "coach-replies", "why": "w"}, {"action": "change", "id": "coach-replies", "why": "w"}], "none",
     [("keep", "coach-replies")], []),                                                                            # 同じ id は 1 度だけ
    ([], "screen", [], ["no_stories"]),
    ([], "none", [], []),                                                                                        # 流れに関係しない変更
])
def test_upstream_stories(stories, scale, want_ids, want_marks):
    req, st, marks = pl.upstream({"requirements": REQ, "stories": stories}, KNOWN, "ready", scale, DEFS)
    assert req == {"acceptance_criteria": ["通知が届く"], "non_goals": ["メール"], "success_definition": "届く"}
    assert [(x["action"], x["id"]) for x in st] == want_ids and marks == want_marks
    if want_ids and want_ids[0][0] == "add":
        assert st[0]["title"].startswith("生徒が") and st[0]["actor"] == "student"


def test_upstream_only_when_ready_and_marks_missing_requirements():
    assert pl.upstream({"requirements": REQ, "stories": []}, KNOWN, "ask", "screen", DEFS) == (None, [], [])
    req, st, marks = pl.upstream({"stories": [{"action": "keep", "id": "cs-traces", "why": "w"}]}, KNOWN, "split", "none", DEFS)
    assert req is None and marks == ["no_requirements"] and st[0]["id"] == "cs-traces"


def test_pre_implement_phase_does_not_reissue_upstream(monkeypatch):
    monkeypatch.setattr(agent_runner, "run", lambda *a, **k: agent_runner.Result(
        output={**SZ(steps=("ui_design", "implement")), "design_direction_given": True, "summary": "s", "evidence": [], "interpretation": "i",
                "requirements": REQ, "stories": []},
        cost_usd=0.0, model="m"))
    out = pl.plan({"title": "t", "body": "b"}, {"phase": "implement"}, DEFS, None)
    assert out["requirements"] is None and out["stories"] == [] and "no_stories" not in out["advisories"]
    out = pl.plan({"title": "t", "body": "b"}, {}, DEFS, None)
    assert out["requirements"]["acceptance_criteria"] == ["通知が届く"] and "no_stories" in out["advisories"]


# ------------------------------------------------------------------ 従う ADR・新しい判断と契約の差分（P4 の typed_upstream）
def test_adr_ids_and_contract_files(tmp_path):
    a = tmp_path / "docs" / "adr"
    a.mkdir(parents=True)
    for n in ("0003-backend-layers.md", "0004-schema-expand-contract.md", "README.md", "draft.md"):
        (a / n).write_text("x")
    c = tmp_path / "specs" / "contracts"
    (c / "api" / "student-app").mkdir(parents=True)
    (c / "api" / "coach-app").mkdir(parents=True)
    (c / "api" / "coach-app" / ".gitkeep").write_text("")
    (c / "api" / "student-app" / "openapi.yaml").write_text("x")
    (c / "README.md").write_text("x")
    assert pl.adr_ids(tmp_path) == ["0003", "0004"] and pl.adr_ids(None) == []
    assert pl.contract_files(tmp_path) == ["specs/contracts/api/", "specs/contracts/api/coach-app/", "specs/contracts/api/student-app/",
                                           "specs/contracts/api/student-app/openapi.yaml"]
    f = pl.facts(tmp_path)
    assert f["adr_ids"] == ["0003", "0004"] and "specs/contracts/api/student-app/openapi.yaml" in f["contract_files"]


def test_schema_has_adrs_and_contracts():
    s = pl.schema(DEFS)
    u = DEFS["upstream"]
    assert {"adrs", "contract_changes"} <= set(s["required"])
    p = s["properties"]
    assert p["adrs"]["properties"]["new"]["items"]["required"] == u["adr"]["required"] == ["title", "context", "decision", "alternatives", "consequences"]
    assert p["contract_changes"]["items"]["properties"]["kind"]["enum"] == list(u["contracts"]["kinds"]) == ["add", "change", "remove"]
    assert p["contract_changes"]["items"]["properties"]["compat_impact"]["enum"] == ["none", "additive", "breaking"]
    assert {"unknown_adr", "unknown_contract", "breaking_contract", "no_contract_diff"} <= set(DEFS["advisories"])


CF = {"adr_ids": ["0003", "0004"], "contract_files": ["specs/contracts/api/", "specs/contracts/api/student-app/",
                                                      "specs/contracts/api/student-app/openapi.yaml", "specs/contracts/api/coach-app/"]}
NEW_ADR = {"title": "予約の取り消しは開始 24 時間前まで", "context": "c", "decision": "d", "alternatives": ["前日まで", ""], "consequences": "r"}


def test_decisions_and_contracts_normalizes():
    out = {"adrs": {"follow": [{"id": "0004", "why": "列を足す"}, {"id": "ADR 3", "why": "層"}, {"id": "0099", "why": "x"}, {"id": "0004", "why": "dup"}],
                    "new": [NEW_ADR, {"title": "", "decision": "x"}]},
           "contract_changes": [
               {"contract": "specs/contracts/api/student-app/openapi.yaml", "kind": "change", "compat_impact": "additive",
                "operations": ["POST /bookings/{id}/cancel"], "summary": "取り消し"},
               {"contract": "specs/contracts/api/coach-app/openapi.yaml", "kind": "add", "compat_impact": "additive", "summary": "新しい枝"},
               {"contract": "specs/contracts/api/nope/openapi.yaml", "kind": "add", "compat_impact": "additive", "summary": "枝がない"},
               {"contract": "apps/backend/x.rs", "kind": "change", "compat_impact": "none", "summary": "契約の外"},
               {"contract": "specs/contracts/api/student-app/openapi.yaml", "kind": "remove", "compat_impact": "additive",
                "operations": ["GET /old"], "summary": "古い操作を消す"}]}
    adrs, cs, marks = pl.decisions_and_contracts(out, CF, "ready", DEFS)
    assert adrs["follow"] == [{"id": "0004", "why": "列を足す"}, {"id": "0003", "why": "層"}]
    assert adrs["new"] == [{"status": "proposed", "title": NEW_ADR["title"], "context": "c", "decision": "d", "alternatives": ["前日まで"], "consequences": "r"}]
    assert [(c["contract"].split("/")[3], c["kind"], c["compat_impact"]) for c in cs] == [
        ("student-app", "change", "additive"), ("coach-app", "add", "additive"), ("student-app", "remove", "breaking")]   # remove は breaking にする
    assert marks == ["unknown_adr", "unknown_contract", "breaking_contract"]


def test_decisions_and_contracts_only_when_ready_and_marks_missing_diff():
    assert pl.decisions_and_contracts({"adrs": {"follow": [{"id": "0004", "why": "w"}], "new": []}}, CF, "ask", DEFS) == ({"follow": [], "new": []}, [], [])
    adrs, cs, marks = pl.decisions_and_contracts({"size": {"layers": ["contract", "backend"]}}, CF, "split", DEFS)
    assert (adrs, cs, marks) == ({"follow": [], "new": []}, [], ["no_contract_diff"])


def test_plan_returns_adrs_and_contracts(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "docs" / "adr").mkdir(parents=True)
    (repo / "docs" / "adr" / "0004-schema-expand-contract.md").write_text("# ADR 0004")
    (repo / "specs" / "contracts" / "api" / "student-app").mkdir(parents=True)
    (repo / "specs" / "contracts" / "api" / "student-app" / "openapi.yaml").write_text("openapi: 3.1.0")
    seen = {}

    def fake(step, prompt, *, schema=None, tier=None, max_turns=3, retries=3):
        seen["prompt"], seen["adr"] = prompt, Path("adr/0004-schema-expand-contract.md").exists()
        return agent_runner.Result(output={**O(steps=["implement"], scale="none"), "summary": "s", "evidence": [], "interpretation": "i",
                                           "size": {"layers": ["contract", "backend"], "turns_band": "S", "basis": "b"},
                                           "requirements": REQ, "stories": [],
                                           "adrs": {"follow": [{"id": "0004", "why": "列を足す"}], "new": [NEW_ADR]},
                                           "contract_changes": [{"contract": "specs/contracts/api/student-app/openapi.yaml", "kind": "change",
                                                                 "compat_impact": "additive", "operations": ["POST /x"], "summary": "s"}]},
                                   cost_usd=0.0, model="m")

    monkeypatch.setattr(agent_runner, "run", fake)
    out = pl.plan({"title": "t", "body": "b"}, {}, DEFS, repo)
    assert seen["adr"] and "いまある番号: 0004" in seen["prompt"] and "specs/contracts/api/student-app/openapi.yaml" in seen["prompt"]
    assert out["adrs"]["follow"][0]["id"] == "0004" and out["adrs"]["new"][0]["status"] == "proposed"
    assert out["contract_changes"][0]["operations"] == ["POST /x"] and not {"no_contract_diff", "unknown_contract"} & set(out["advisories"])
    out = pl.plan({"title": "t", "body": "b"}, {"phase": "implement"}, DEFS, repo)   # 実装の手前の判定では出し直さない
    assert out["adrs"] == {"follow": [], "new": []} and out["contract_changes"] == []
