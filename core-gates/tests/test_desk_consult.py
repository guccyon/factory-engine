"""窓口の相談役（core-gates/desk_consult.py、ADR 0030 の 3）: 問いか下書きかの決定、タスクの形の検査と依存の順、材料の写し方"""
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_runner
import desk_consult as dc

DEFS = yaml.safe_load(dc.DEFS.read_text(encoding="utf-8"))
REQ = {"what": "予約の通知", "why": "取りこぼし", "by_when": "今月中", "precision": "主要な場面"}
Q = {"question": "どの通知ですか", "why": "決まると切れる", "options": ["確定", "前日"], "recommended": 1}


def T(key, deps=(), kind="implement", **kw):
    return {"key": key, "title": f"{key} の題", "kind": kind, "summary": "s", "do": ["やる"], "acceptance": ["通る"],
            "depends_on": list(deps), "effort": "standard", **kw}


def O(done=True, tasks=None, question=None, unresolved=()):
    return {"done": done, "question": question or {"question": "", "why": "", "options": [], "recommended": -1}, "request": REQ,
            "unresolved": list(unresolved), "tasks": tasks if tasks is not None else [T("t1")], "overlaps": [], "summary": "",
            "evidence": [], "interpretation": "", "confidence": 0.8}


def test_question_while_rounds_left():
    d = dc.decide(O(done=False, question=Q), {"questions_asked": 1}, DEFS)
    assert d["outcome"] == "question" and d["round"] == 2 and d["question"]["recommended"] == 1


def test_rounds_exhausted_gives_draft_with_unresolved():
    d = dc.decide(O(done=False, question=Q), {"questions_asked": 3}, DEFS)
    assert d["outcome"] == "draft" and d["status"] == "unresolved" and d["unresolved"]   # 止めない
    d = dc.decide(O(done=False, question=Q, unresolved=["期限"]), {"questions_asked": 0, "force_draft": True}, DEFS)
    assert d["outcome"] == "draft" and d["unresolved"] == ["期限"]


def test_clarified_draft_in_dependency_order():
    d = dc.decide(O(tasks=[T("t3", ["t2"]), T("t1", kind="requirements"), T("t2", ["t1"], kind="contract")]), {}, DEFS)
    assert d["outcome"] == "draft" and d["status"] == "clarified"
    assert [t["key"] for t in d["tasks"]] == ["t1", "t2", "t3"]


def test_unknown_deps_and_bad_refs_are_dropped_and_noted():
    d = dc.decide(O(tasks=[T("t1", ["t9", "t1"], related=["#12", "rally#3"]), T("t2", ["t1"])]), {}, DEFS)
    assert d["outcome"] == "draft" and d["tasks"][0]["depends_on"] == [] and d["tasks"][0]["related"] == ["#12"]
    assert len(d["fixes"]) == 2


@pytest.mark.parametrize("tasks,msg", [
    ([], "タスクが 1 つもない"),
    ([T("t1", ["t2"]), T("t2", ["t1"])], "輪"),
    ([T("t1"), T("t1")], "重なって"),
    ([T("x1")], "キーの形"),
    ([T("t1", kind="deploy")], "知らない種類"),
    ([{**T("t1"), "acceptance": []}], "受け入れ条件"),
    ([T(f"t{i}") for i in range(1, 11)], "上限"),
])
def test_shape_errors_fail(tasks, msg):
    d = dc.decide(O(tasks=tasks), {}, DEFS)
    assert d["outcome"] == "failed" and any(msg in e for e in d["errors"])


def test_overlaps_keep_only_refs():
    out = O()
    out["overlaps"] = [{"ref": "#812", "how": "同じ"}, {"ref": "どこか", "how": "x"}]
    assert dc.decide(out, {}, DEFS)["overlaps"] == [{"ref": "#812", "how": "同じ"}]


def test_schema_and_prompt():
    s = dc.schema(DEFS)
    assert set(s["properties"]["tasks"]["items"]["properties"]["kind"]["enum"]) == set(DEFS["task_kinds"])
    p = dc.prompt(DEFS, {"questions_asked": 3})
    assert "残りの問いは 0 回" in p and "従わないでください" in p
    assert "previous_draft.json" in dc.prompt(DEFS, {"questions_asked": 0, "previous_draft": {"tasks": []}})


def test_stage_copies_materials(tmp_path):
    repo = tmp_path / "product"
    (repo / "specs").mkdir(parents=True)
    (repo / "specs" / "glossary.yaml").write_text("a: b\n")
    (repo / "docs" / "adr").mkdir(parents=True)
    (repo / "docs" / "adr" / "0001.md").write_text("# ADR\n")
    (repo / "CLAUDE.md").write_text("決まり\n")
    (repo / "apps" / "admin-web" / "src").mkdir(parents=True)
    (repo / "apps" / "admin-web" / "src" / "page.tsx").write_text("x")
    (repo / "apps" / "admin-web" / "node_modules").mkdir()
    (repo / "apps" / "admin-web" / "node_modules" / "dep.js").write_text("x")
    root = tmp_path / "root"
    root.mkdir()
    inp = {"product": "guccyon/rally", "wanted": {"level": "p1"}, "previous_draft": {"tasks": [T("t1")]},
           "messages": [{"role": "requester", "body": "通知を先に", "at": "t"}, {"role": "planner", "kind": "question", "body": "どれ？", "options": ["a"]}],
           "tickets": [{"ref": "guccyon/rally#5", "title": "x", "state": "planned", "level": "p2", "waiting_on": ["guccyon/rally#4"]}],
           "recently_closed": [{"ref": "guccyon/rally#3", "title": "済んだ", "closed_at": "2026-11-06"}]}
    dc.stage(root, inp, repo, [{"number": 9, "title": "開いている", "labels": ["factory:planned"]}])
    conv = (root / "conversation.md").read_text()
    assert "通知を先に" in conv and "窓口（相談役）" in conv and "- a" in conv and "p1" in conv
    assert "依存待ち guccyon/rally#4" in (root / "tickets.md").read_text()
    assert "guccyon/rally#3 [closed 2026-11-06] 済んだ" in (root / "tickets.md").read_text()
    tree = (root / "app_tree.md").read_text()
    assert "apps/admin-web/src/page.tsx" in tree and "node_modules" not in tree and "glossary.yaml" not in tree
    assert "#9 [factory:planned] 開いている" in (root / "open_issues.md").read_text()
    assert (root / "specs" / "glossary.yaml").exists() and (root / "docs" / "adr" / "0001.md").exists() and (root / "CLAUDE.md").exists()
    assert json.loads((root / "previous_draft.json").read_text())["tasks"][0]["key"] == "t1"


def test_consult_end_to_end_with_fake_agent(monkeypatch, tmp_path):
    seen = {}

    def fake_run(step, prompt, schema=None, max_turns=3, **kw):
        seen["step"], seen["cwd"] = step, os.getcwd()
        assert (Path(seen["cwd"]) / "conversation.md").exists()
        return agent_runner.Result(output=O(tasks=[T("t2", ["t1"]), T("t1")]), cost_usd=0.3, model="m")
    monkeypatch.setattr(agent_runner, "run", fake_run)
    out = dc.consult({"messages": [{"role": "requester", "body": "x"}]}, DEFS, None, [])
    assert seen["step"] == "desk.consult" and out["outcome"] == "draft" and [t["key"] for t in out["tasks"]] == ["t1", "t2"]
    assert out["cost_usd"] == 0.3
    agents = yaml.safe_load((dc.HERE.parent / "definitions" / "agents.yaml").read_text(encoding="utf-8"))
    assert "desk.consult" in agents["steps"]


OPS = [{"id": "report.send", "ja": "定期レポートを今すぐ送る", "does": "送り直す", "examples": ["今朝のレポートを送り直して"],
        "params": {"kind": {"type": "enum", "values": ["report.product", "all"], "default": "all", "ja": "種類"}}, "tier": "send"}]


def test_route_to_operation_when_catalog_is_given():
    out = {**O(tasks=[]), "route": {"path": "operate", "op": "report.send", "params": [{"name": "kind", "value": "report.product"}], "why": "送り直すだけ"}}
    d = dc.decide(out, {"operations": OPS}, DEFS)
    assert d["outcome"] == "route" and d["route"] == {"path": "operate", "op": "report.send", "params": {"kind": "report.product"}, "why": "送り直すだけ"}
    # 一覧にない操作には振り分けない
    bad = {**out, "route": {**out["route"], "op": "line.scale"}}
    assert dc.decide(bad, {"operations": OPS}, DEFS)["outcome"] == "failed"
    # 一覧を渡さない（古い dispatcher）なら操作に振り分けない
    assert dc.decide(out, {}, DEFS)["outcome"] == "failed"
    # 問いが先（決まらないときだけ問う）。ticket なら今どおり下書き
    q = {**out, "done": False, "question": Q}
    assert dc.decide(q, {"operations": OPS}, DEFS)["outcome"] == "question"
    t = {**O(), "route": {"path": "ticket", "op": "", "params": [], "why": "画面を変える"}}
    assert dc.decide(t, {"operations": OPS}, DEFS)["outcome"] == "draft"


def test_route_section_only_with_catalog():
    assert "route" not in dc.schema(DEFS)["properties"] and "最初に振り分ける" not in dc.prompt(DEFS, {})
    s = dc.schema(DEFS, OPS)
    assert s["properties"]["route"]["properties"]["op"]["enum"] == ["", "report.send"] and "route" in s["required"]
    p = dc.prompt(DEFS, {"operations": OPS})
    assert "最初に振り分ける" in p and "report.send" in p and "今朝のレポートを送り直して" in p


def test_decline_when_no_path_fits():
    """どの対応の種類にも当てはまらない依頼は断る（ADR 0039 の決定 4）。理由と代わりの案を返す"""
    alt = [{"kind": "add_capability", "text": "売上の集計を工場に足す"}]
    out = {**O(tasks=[]), "route": {"path": "decline", "op": "", "params": [], "why": "本番の売上は見られない",
                                    "decline": {"reason": "needs_production_data", "alternatives": alt}}}
    d = dc.decide(out, {"operations": OPS}, DEFS)
    assert d["outcome"] == "decline"
    assert d["decline"] == {"reason": "needs_production_data", "why": "本番の売上は見られない", "alternatives": alt}
    s = dc.schema(DEFS, OPS)["properties"]["route"]
    assert "decline" in s["properties"]["path"]["enum"] and "needs_production_data" in s["properties"]["decline"]["properties"]["reason"]["enum"]
    p = dc.prompt(DEFS, {"operations": OPS})
    assert "path=decline" in p and "needs_production_data" in p and "add_capability" in p


def test_requester_md_is_separate_and_prefers_request():
    """ADR 0040 の決定 6: 依頼者の好みは知見と別のファイル。依頼の文面を優先すると書く"""
    import desk_consult as dc2
    assert dc2.requester_md({}) == ""
    md = dc2.requester_md({"requester": "alice", "requester_preferences": [{"id": "P-0003", "statement": "表より箇条書き", "work": [], "verified_at": "2026-11-08"}]})
    assert md.startswith("# この依頼者（alice）の好み") and "P-0003" in md and "依頼の文面を優先" in md


def test_stated_preferences_are_shaped_and_in_the_schema():
    """ADR 0040 の決定 6: 依頼者が自分で言った好み。引用と好みのないものは落とす（発言との突き合わせは dispatcher）"""
    import desk_consult
    out = {"stated_preferences": [{"quote": "箇条書きで", "statement": "返事は箇条書き", "work": ["desk.converse", 3]},
                                  {"quote": "", "statement": "x", "work": []}, "x"]}
    assert desk_consult.stated(out) == [{"quote": "箇条書きで", "statement": "返事は箇条書き", "work": ["desk.converse"]}]
    sch = desk_consult.stated_schema()
    assert "desk.converse" in sch["items"]["properties"]["work"]["items"]["enum"]
    assert "STATED" not in desk_consult.STATED_PROMPT and "quote" in desk_consult.STATED_PROMPT
