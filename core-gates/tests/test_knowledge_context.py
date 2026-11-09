"""エージェントに渡す知見の選び方（core-gates/knowledge_context.py、ADR 0024 の 6）"""
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import knowledge_context as kx  # noqa: E402

TODAY = date(2026, 11, 5)


def k(kid, **over):
    base = {"status": "active", "confidence": "confirmed", "verified_at": "2026-11-01", "review_by": "2027-01-30",
            "confidentiality": "product", "scope": "{roles: [implement], paths: [\"apps/**\"]}",
            "statement": f"{kid} の主張"}
    base.update(over)
    return "\n".join([f"id: {kid}", "theme: 4", "kind: practice", "evidence:\n  - pr: 1", "decided_by: committee:D-0001"]
                     + [f"{key}: {v}" for key, v in base.items() if v is not None]) + "\n"


def repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / kx.kc.ROOT
    root.mkdir(parents=True)
    for kid, text in files.items():
        (root / f"{kid}.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def test_passes_only_active_confirmed_or_settled_for_the_role(tmp_path):
    r = repo(tmp_path, {
        "K-0001": k("K-0001"),
        "K-0002": k("K-0002", confidence="hypothesis"),
        "K-0003": k("K-0003", status="draft"),
        "K-0004": k("K-0004", confidence="settled", review_by=None, enforcement="{level: deterministic, solutions: [{kind: ci_check, what: x}]}"),
        "K-0005": k("K-0005", confidence="settled", review_by=None, enforcement="{level: checklist}"),
        "K-0006": k("K-0006", review_by="2026-11-01"),
        "K-0007": k("K-0007", scope="{roles: [judge]}"),
        "K-0008": k("K-0008", scope="{roles: [implementer]}"),
        "K-0009": k("K-0009", scope="{paths: [\"x\"]}"),
        "K-0010": k("K-0010", confidentiality="client"),
        "K-0011": k("K-0011", status="superseded"),
    })
    text, ids, skipped, _ = kx.build(r, "implement", TODAY)
    assert sorted(ids) == ["K-0001", "K-0005", "K-0008", "K-0009"]
    assert skipped["hypothesis"] == ["K-0002"]
    assert skipped["enforced"] == ["K-0004"]
    assert skipped["expired"] == ["K-0006"]
    assert skipped["other_role"] == ["K-0007"]
    assert skipped["confidential"] == ["K-0010"]
    assert sorted(skipped["not_active"]) == ["K-0003", "K-0011"]
    assert "## K-0001（確認済み、確認日 2026-11-01）" in text
    assert "## K-0005（確定、確認日 2026-11-01）" in text
    assert "効く範囲: パス apps/**" in text


def test_newest_first_and_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(kx, "MAX_ITEMS", 2)
    r = repo(tmp_path, {f"K-000{i}": k(f"K-000{i}", verified_at=f"2026-10-0{i}") for i in range(1, 5)})
    _, ids, skipped, _ = kx.build(r, "implement", TODAY)
    assert ids == ["K-0004", "K-0003"]
    assert skipped["over_limit"] == ["K-0002", "K-0001"]


def test_main_writes_nothing_when_nothing_to_pass(tmp_path, capsys):
    r = repo(tmp_path, {"K-0001": k("K-0001", confidence="hypothesis")})
    out = tmp_path / "ctx" / "knowledge.md"
    assert kx.main(["--repo", str(r), "--out", str(out), "--today", "2026-11-05"]) == 0
    assert not out.exists()
    assert json.loads(capsys.readouterr().out)["ids"] == []


def test_main_without_store_is_quiet(tmp_path, capsys):
    out = tmp_path / "knowledge.md"
    assert kx.main(["--repo", str(tmp_path), "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ids": [], "skipped": {}}


def test_main_writes_file_with_header(tmp_path, capsys):
    r = repo(tmp_path, {"K-0001": k("K-0001")})
    out = tmp_path / "ctx" / "knowledge.md"
    kx.main(["--repo", str(r), "--out", str(out), "--today", "2026-11-05"])
    assert json.loads(capsys.readouterr().out)["ids"] == ["K-0001"]
    body = out.read_text(encoding="utf-8")
    assert body.startswith("# この製品の知見") and "K-0001 の主張" in body


# 業務と適応度で選ぶ（ADR 0040 の決定 3・4）
def job(**values):
    work = values.pop("work", None)
    return kx.Job("implement", work=work, values=values)


def test_role_maps_to_work_nodes_from_work_yaml():
    j = kx.Job("implement")
    assert j.work == ["dev.build", "dev.describe"] and "PR.body" in j.artifacts and "dev" in j.covered
    assert kx.Job("body_fix").work == ["dev.describe"]


def test_candidates_by_work_ancestors_and_artifacts(tmp_path):
    r = repo(tmp_path, {
        "K-0001": k("K-0001", scope="{work: [dev.describe]}"),
        "K-0002": k("K-0002", scope="{work: [dev]}"),                    # 親のノード
        "K-0003": k("K-0003", scope="{work: [dev.design.proposal]}"),    # ほかの業務
        "K-0004": k("K-0004", scope="{artifacts: [PR]}"),                # 成果物の親の型
        "K-0005": k("K-0005", scope="{work: [dev.describe]}", acts_on="engine"),
    })
    _, ids, skipped, fit = kx.build(r, "implement", TODAY, job(request_form=["ticket"]))
    assert sorted(ids) == ["K-0001", "K-0002", "K-0004"]
    assert skipped["other_work"] == ["K-0003"] and skipped["not_context"] == ["K-0005"]
    assert ids[0] == "K-0001" and fit["K-0001"]["score"] > fit["K-0002"]["score"]   # 深いノードが先


def test_axes_exclude_penalize_and_unknown(tmp_path):
    r = repo(tmp_path, {
        "K-0001": k("K-0001", scope="{work: [dev.build], purpose: [adopt]}"),
        "K-0002": k("K-0002", scope="{work: [dev.build], purpose: [new_feature]}"),
        "K-0003": k("K-0003", scope="{work: [dev.build], product_stage: [operating]}"),
        "K-0004": k("K-0004", scope="{work: [dev.build], files: [\"apps/backend/**\"]}"),
        "K-0005": k("K-0005", scope="{work: [dev.build], request_form: [one_off]}"),
    })
    _, ids, skipped, fit = kx.build(r, "implement", TODAY, job(purpose=["adopt"], product_stage=["development"], request_form=["ticket"]))
    assert ids[0] == "K-0001" and fit["K-0001"]["parts"]["purpose"] == 2
    assert sorted(skipped["mismatch"]) == ["K-0002", "K-0003", "K-0005"]
    assert fit["K-0004"]["parts"]["files"] == -0.25          # ファイルは分からない
    _, ids, _, fit = kx.build(r, "implement", TODAY, kx.Job("implement", values={"purpose": ["adopt"]}, files=["apps/backend/src/a.rs"]))
    assert fit["K-0004"]["parts"]["files"] == 1


def test_main_reports_work_values_and_fit(tmp_path, capsys):
    r = repo(tmp_path, {"K-0001": k("K-0001", scope="{work: [dev.describe], request_form: [ticket]}")})
    (r / ".factory-config" / "manifest.yaml").write_text("product_stage:\n  stage: development\n", encoding="utf-8")
    kx.main(["--repo", str(r), "--out", str(tmp_path / "k.md"), "--today", "2026-11-05", "--set", "request_form=ticket"])
    out = json.loads(capsys.readouterr().out)
    assert out["ids"] == ["K-0001"] and out["work"] == ["dev.build", "dev.describe"]
    assert out["values"]["product_stage"] == ["development"] and out["fit"]["K-0001"]["parts"]["request_form"] == 2
    assert "業務 dev.describe" in (tmp_path / "k.md").read_text(encoding="utf-8")


def test_stage_writes_only_selected_for_role(tmp_path):
    """ADR 0040 の決定 7 の 3: プランナー・窓口は知見のディレクトリを丸写しにせず、業務で選んだものだけを読む"""
    repo = tmp_path / "repo"
    d = repo / ".factory-config" / "knowledge"
    d.mkdir(parents=True)
    base = {"theme": 4, "kind": "causal", "confidence": "confirmed", "evidence": [{"case": "C-0001"}], "status": "active",
            "verified_at": "2026-11-01", "review_by": "2026-12-31", "confidentiality": "product", "decided_by": "committee:D-0001"}
    (d / "K-0001.yaml").write_text(json.dumps({**base, "id": "K-0001", "statement": "計画の知見", "scope": {"work": ["plan.workflow"]}}), encoding="utf-8")
    (d / "K-0002.yaml").write_text(json.dumps({**base, "id": "K-0002", "statement": "本文の知見", "scope": {"work": ["dev.describe"]}}), encoding="utf-8")
    (d / "K-0003.yaml").write_text(json.dumps({**base, "id": "K-0003", "statement": "仮説", "confidence": "hypothesis", "scope": {"work": ["plan"]}}), encoding="utf-8")
    dest = tmp_path / "work" / "knowledge"
    sel = kx.stage(repo, dest, "plan", {"request_form": ["ticket"]}, today=date(2026, 11, 8))
    assert sel["ids"] == ["K-0001"] and sel["work"] == ["plan.workflow"]
    assert sorted(p.name for p in dest.iterdir()) == ["knowledge.md"]
    assert "計画の知見" in (dest / "knowledge.md").read_text(encoding="utf-8")


def test_stage_without_knowledge_writes_nothing(tmp_path):
    dest = tmp_path / "knowledge"
    assert kx.stage(tmp_path, dest, "plan") == {"ids": [], "work": [], "values": {}, "fit": {}}
    assert kx.stage(None, dest, "desk.consult")["ids"] == []
    assert not dest.exists()


# ------------------------------------------------------------------ ADR 0040 の決定 7 の 5: デザイン・検査と裁き・QA・窓口
def wk(kid, work, **over):
    return k(kid, scope="{work: [" + ", ".join(work) + "]}", **over)


def test_work_for_step_reads_by_path():
    assert kx.work_for_step("ui_design") == ["dev.design.screen"]
    assert kx.work_for_step("ui_design", "quick") == ["dev.design.proposal"]
    assert kx.work_for_step("ui_design", "ticket") == ["dev.design.screen"]
    assert kx.work_for_step("merge") is None and kx.work_for_step("no_such_step") is None


def test_stage_picks_by_role_work_and_writes_note(tmp_path):
    r = repo(tmp_path / "p", {
        "K-0001": wk("K-0001", ["dev.design"]),             # デザイン全般は画面にも案にも効く
        "K-0002": wk("K-0002", ["dev.design.proposal"]),    # 単発の案だけ
        "K-0003": wk("K-0003", ["dev.review.inspect"]),
        "K-0004": wk("K-0004", ["ops.qa"]),
        "K-0005": wk("K-0005", ["desk.converse"]),
        "K-0006": wk("K-0006", ["dev.design.screen"], confidence="hypothesis"),
    })
    got = {}
    for role, work in (("ui_design", None), ("ui_design", ["dev.design.proposal"]), ("ui_design_review", None), ("qa", None),
                       ("desk.answer", None), ("design_system", None)):
        sel = kx.stage(r, tmp_path / f"out-{role}-{bool(work)}", role, {"request_form": ["ticket"]}, today=TODAY, work=work)
        got[(role, bool(work))] = sel["ids"]
    assert got[("ui_design", False)] == ["K-0001"]
    assert sorted(got[("ui_design", True)]) == ["K-0001", "K-0002"]
    assert got[("ui_design_review", False)] == []        # dev.review.design はデザインの下ではない
    assert got[("qa", False)] == ["K-0004"]
    assert got[("desk.answer", False)] == ["K-0005"]
    assert got[("design_system", False)] == ["K-0001"]
    sel = kx.stage(r, tmp_path / "o", "qa", today=TODAY)
    assert (tmp_path / "o" / "knowledge.md").is_file()
    note = kx.prompt_note(sel)
    assert "knowledge/knowledge.md" in note and "ops.qa" in note and ".factory-config/knowledge/ は直接読まない" in note
    assert kx.result_fields(sel) == {"knowledge_ids": ["K-0004"], "knowledge_selection": {"work": ["ops.qa"], "values": sel["values"], "fit": sel["fit"]}}


def test_nothing_staged_means_no_note(tmp_path):
    assert kx.prompt_note({"ids": []}) == ""
    assert kx.result_fields({}) == {"knowledge_ids": [], "knowledge_selection": {"work": [], "values": {}, "fit": {}}}
    assert kx.stage(tmp_path, tmp_path / "o", "qa")["ids"] == []   # 知見のディレクトリがない製品


def test_file_section_reads_knowledge_file(tmp_path, monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_FILE", raising=False)
    assert kx.file_section("使い方") == ""
    f = tmp_path / "knowledge.md"
    f.write_text("## K-0003（確認済み）\n\n主張\n", encoding="utf-8")
    monkeypatch.setenv("KNOWLEDGE_FILE", str(f))
    s = kx.file_section("観点として踏まえる")
    assert "# この製品の知見（データ）" in s and "観点として踏まえる" in s and "K-0003" in s


def test_cli_files_axis_for_inspect(tmp_path, capsys):
    """inspect.sh・judge.sh は PR のファイルを --files で渡す。ファイルの合う知見が上に来る"""
    r = repo(tmp_path, {
        "K-0001": k("K-0001", scope="{work: [dev.review], files: [\"apps/backend/**\"]}"),
        "K-0002": k("K-0002", scope="{work: [dev.review.inspect], files: [\"apps/admin-web/**\"]}"),
    })
    kx.main(["--repo", str(r), "--role", "inspect", "--set", "request_form=ticket", "--files", "apps/backend/src/a.rs",
             "--out", str(tmp_path / "k.md"), "--today", TODAY.isoformat()])
    out = json.loads(capsys.readouterr().out)
    assert out["work"] == ["dev.review.inspect"] and out["ids"][0] == "K-0001"
    assert out["fit"]["K-0001"]["parts"]["files"] == 1 and out["fit"]["K-0002"]["parts"]["files"] == -1
