"""デザインシステムのデザイナー役（core-gates/design_system.py、ADR 0025・0026）: 段の決め方、案と正本の形の検査、プロンプト、会話と撮影"""
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_runner
import design_system as ds
import ui_design

DEFS = ds.load_defs()
TOKENS = {"color": {"primary": "#0b6e4f"}, "typography": {"body": 16}, "shape": {"radius": 8}, "spacing": {"base": 8}}


def concept(cid="calm", **kw):
    c = {"id": cid, "name": "落ち着いた上質", "impression": "安心して任せられる", "mood_words": ["静か", "上質", "誠実"], "anti_words": ["派手"],
         "voice_tone": "丁寧", "imagery": "写真は自然光", "tokens": TOKENS, "tradeoff": "元気さは弱い"}
    c.update(kw)
    return c


def concepts(**kw):
    s = {"product_understanding": "生徒とコーチのレッスン", "concepts": [concept("calm"), concept("sporty", name="スポーティ")],
         "recommended": "calm", "recommended_reason": "続けやすさ"}
    s.update(kw)
    return s


def full_spec():
    files = {f: yaml.safe_dump({"section": sec}, allow_unicode=True) for f, sec in DEFS["spec"]["files"].items()}
    files["tokens.yaml"] = yaml.safe_dump(TOKENS)
    return files


def test_phase_of(tmp_path):
    assert ds.phase_of({}, tmp_path) == "concept"                     # デザインシステムがない製品はコンセプトから
    (tmp_path / "specs" / "design").mkdir(parents=True)
    (tmp_path / "specs" / "design" / "tokens.yaml").write_text("color: {}\n")
    assert ds.phase_of({}, tmp_path) == "spec"
    assert ds.phase_of({"phase": "concept"}, tmp_path) == "concept"   # 作り直しは入力で指定する
    assert ds.phase_of({"phase": "bogus"}, tmp_path) == "spec"


def test_validate_concepts(tmp_path):
    for c in ("calm", "sporty"):
        (tmp_path / f"{c}.html").write_text("<p>x</p>")
    assert ds.validate_concepts(concepts(), DEFS, tmp_path) == []
    bad = concepts(recommended="z", concepts=[concept("calm", mood_words=["a"], tokens={"color": {}})])
    errs = "\n".join(ds.validate_concepts(bad, DEFS, tmp_path))
    assert "2〜3 案" in errs and "mood_words は 3〜5 個" in errs and "tokens.typography がない" in errs and "recommended" in errs
    errs = "\n".join(ds.validate_concepts(concepts(concepts=[concept("calm"), concept("calm")]), DEFS, tmp_path))
    assert "重複" in errs
    assert "見本 out/x.html がない" in "\n".join(ds.validate_concepts(concepts(concepts=[concept("x"), concept("calm")], recommended="x"), DEFS, tmp_path))


def test_validate_spec():
    assert ds.validate_spec(full_spec(), DEFS, delta=False) == []
    assert ds.validate_spec({}, DEFS, delta=True) == ["out/spec/ にファイルがない"]
    part = {"components.yaml": "button: {}\n"}
    assert ds.validate_spec(part, DEFS, delta=True) == []              # 追加は変わるファイルだけでよい
    errs = "\n".join(ds.validate_spec(part, DEFS, delta=False))
    assert "principles.yaml がない" in errs and "tokens.yaml がない" in errs
    errs = "\n".join(ds.validate_spec({"tokens.yaml": "color: {}\n", "bad.yaml": "a: [", "../x.yaml": "a: 1\n"}, DEFS, delta=True))
    assert "tokens.yaml に typography がない" in errs and "bad.yaml を YAML として読めない" in errs and "名前は" in errs
    big = {"principles.yaml": "a: '" + "x" * (DEFS["designer"]["max_spec_chars"] + 1) + "'\n"}
    assert "上限" in "\n".join(ds.validate_spec(big, DEFS, delta=True))


def test_prompt_by_phase():
    p = ds.prompt(DEFS, {"title": "t"}, {}, "concept", False, False)
    assert "out/concepts.yaml" in p and "まだない" in p and "--check-concepts" in p
    p = ds.prompt(DEFS, {"title": "t"}, {"phase": "concept", "note": "暗すぎる", "round": 2}, "concept", False, True)
    assert "暗すぎる" in p and "previous/concepts.yaml" in p
    p = ds.prompt(DEFS, {"title": "t"}, {"concept": concept()}, "spec", False, False)
    assert "落ち着いた上質" in p and "principles.yaml" in p and "オンボーディングです" in p and "concept_tone（コンセプトとトーン＆マナー）" in p
    p = ds.prompt(DEFS, {"title": "t"}, {}, "spec", True, True)
    assert "変わるファイルだけ" in p and "--delta" in p


def test_definitions_match_guideline():
    g = yaml.safe_load(ds.GUIDELINE.read_text(encoding="utf-8"))
    required = {s["id"] for s in g["sections"] if s.get("required_at_onboarding")}
    assert set(DEFS["spec"]["files"].values()) == required             # オンボーディングの必須の節を全部ファイルにする


def test_run_concept_retries_once_and_captures(tmp_path, monkeypatch):
    calls = []

    def fake(step, prompt, *, tier=None, max_turns=40, cwd=None, state_dir=None):
        calls.append(prompt)
        out = Path(cwd) / "out"
        assert (Path(cwd) / "product").is_symlink()
        (out / "concepts.yaml").write_text(yaml.safe_dump(concepts(), allow_unicode=True))
        (out / "calm.html").write_text("<p>calm</p>")
        if len(calls) == 2:
            (out / "sporty.html").write_text("<p>sporty</p>")
        return agent_runner.Result(output=None, cost_usd=0.5, model="m", num_turns=3, usage={"output_tokens": 10})

    def cap(html, png, size, chromium=None, timeout=60):
        png.write_bytes(b"png")
        return True

    monkeypatch.setattr(agent_runner, "run_agentic", fake)
    monkeypatch.setattr(ui_design, "capture", cap)
    repo = tmp_path / "product"
    repo.mkdir()
    rep = ds.run(repo, {"title": "t", "body": "b"}, {}, tmp_path / "out", DEFS)
    assert len(calls) == 2 and "sporty.html がない" in calls[1]
    assert rep["ok"] and rep["phase"] == "concept" and rep["cost_usd"] == 1.0 and rep["usage"] == {"output_tokens": 20}
    assert set(rep["files"]) >= {"calm.html", "calm.png", "sporty.html", "sporty.png"} and rep["concepts"]["recommended"] == "calm"


def test_run_spec_returns_files(tmp_path, monkeypatch):
    def fake(step, prompt, *, tier=None, max_turns=40, cwd=None, state_dir=None):
        for name, text in full_spec().items():
            (Path(cwd) / "out" / "spec" / name).write_text(text)
        return agent_runner.Result(output=None, cost_usd=0.7, model="m")

    monkeypatch.setattr(agent_runner, "run_agentic", fake)
    repo = tmp_path / "product"
    repo.mkdir()
    rep = ds.run(repo, {"title": "t"}, {"phase": "spec", "concept": concept()}, tmp_path / "out", DEFS)
    assert rep["ok"] and rep["phase"] == "spec" and rep["delta"] is False and set(rep["spec_files"]) == set(DEFS["spec"]["files"])


def test_check_cli(tmp_path):
    (tmp_path / "spec").mkdir()
    for n, t in full_spec().items():
        (tmp_path / "spec" / n).write_text(t)
    assert ds.main(["--check-spec", str(tmp_path / "spec")]) == 0
    (tmp_path / "concepts.yaml").write_text(yaml.safe_dump(concepts(), allow_unicode=True))
    assert ds.main(["--check-concepts", str(tmp_path / "concepts.yaml")]) == 1   # 見本がない


def test_main_writes_report(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "run", lambda *a, **k: {"ok": True, "phase": "spec", "spec_files": {}})
    (tmp_path / "issue.json").write_text(json.dumps({"title": "t"}))
    assert ds.main(["--repo-dir", str(tmp_path), "--issue", str(tmp_path / "issue.json"), "--out-dir", str(tmp_path / "o"), "--out", str(tmp_path / "r.json")]) == 0
    assert json.loads((tmp_path / "r.json").read_text())["phase"] == "spec"
