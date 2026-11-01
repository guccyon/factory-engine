"""デザイナー役（core-gates/ui_design.py、ADR 0025）: UXSpec の形の検査、キャプチャ、直しの会話、渡すファイルの選び方"""
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_runner
import ui_design as ud

DEFS = ud.load_defs()
CHROME = os.environ.get("CHROMIUM_BIN") or shutil.which("chromium") or next(
    (str(p) for p in Path("/opt/pw-browsers").glob("chromium-*/chrome-linux64/chrome")), None)


def spec(**kw):
    s = {"ticket": "o/r#5", "summary": "本部のお知らせ送信に、送る前の確認を足す",
         "changes": [{"screen": "hq-announcements", "kind": "changed", "before": "押すとすぐ送る", "after": "確認の段を挟む"}],
         "prd": {"goal": "本部が生徒に一斉にお知らせを届けたい", "success": "誤送信なく届く", "scope": "送信と確認", "source": "o/r#5"},
         "user_flow": [{"from": "お知らせ一覧", "action": "新規作成", "to": "hq-announcements"}],
         "rationale": {"why": "取り消せない操作なので確認を挟む", "rejected": [{"idea": "即送信", "reason": "誤送信に弱い"}]},
         "screens": [{"id": "hq-announcements", "purpose": "お知らせを送る", "new": False,
                      "transitions": {"entries": ["お知らせ一覧"], "exits": ["送信後は一覧へ"]},
                      "elements": [{"name": "題", "component": "TextField", "required": True}, {"name": "本文", "component": "TextArea"}],
                      "behaviors": [{"target": "送信", "action": "押す", "result": "確認の段を出す"}]}],
         "states": ["読み込み中", "0 件", "失敗"], "design_system_constraints": ["specs/design/ の部品だけ"],
         "design_system_gaps": [], "brand_impact": False, "recommended": "a",
         "variants": [{"id": "a", "name": "確認の段", "tradeoff": "誤送信に強い", "viewport": "desktop"},
                      {"id": "b", "name": "並べる", "tradeoff": "届く姿が見える", "viewport": "desktop"}]}
    s.update(kw)
    return s


def test_validate_accepts_good_spec(tmp_path):
    for v in ("a", "b"):
        (tmp_path / f"{v}.html").write_text("<p>x</p>", encoding="utf-8")
    assert ud.validate(spec(), DEFS, tmp_path) == []


def test_validate_reports_breakage(tmp_path):
    bad = spec(recommended="z", brand_impact="yes", variants=[{"id": "../x", "name": "", "tradeoff": "t", "viewport": "tv"}] * 4)
    bad.pop("states")
    errs = "\n".join(ud.validate(bad, DEFS, tmp_path))
    for part in ("states がない", "brand_impact", "体験の案（axis: flow）は 1〜3 案", "id は英数字", "name がない", "viewport", "重なっている", "recommended"):
        assert part in errs
    assert ud.validate("x", DEFS) == ["uxspec.yaml が対応表（mapping）になっていない"]
    assert "out/b.html がない" in ud.validate(spec(), DEFS, tmp_path)


def test_emitted_files_filters_names_and_size(tmp_path):
    for n, size in (("a.html", 10), ("a.png", 10), ("base.css", 10), ("uxspec.yaml", 10), ("big.png", 50), (".hidden.html", 1)):
        (tmp_path / n).write_bytes(b"x" * size)
    assert ud.emitted_files(tmp_path, 20) == ["a.html", "a.png", "base.css"]


def test_prompt_carries_findings_on_revision():
    p = ud.prompt(DEFS, {"repo": "o/r", "number": 5}, {"round": 2, "findings": [
        {"severity": "must", "viewpoint": "states", "variant": "a", "evidence": "失敗の表示がない", "fix": "失敗の帯を足す"}]})
    assert "直し（2 回目）" in p and "失敗の帯を足す" in p and 'ticket: "o/r#5"' in p
    assert "直し" not in ud.prompt(DEFS, {"repo": "o/r", "number": 5}, {}).split("作るもの")[0]


@pytest.mark.skipif(not CHROME, reason="Chromium がない")
def test_capture_takes_png_without_network(tmp_path):
    html = tmp_path / "a.html"
    html.write_text('<html><head><link rel=stylesheet href="https://fonts.example.invalid/x.css"></head>'
                    '<body style="font-family:sans-serif"><h1>お知らせを送る</h1></body></html>', encoding="utf-8")
    assert ud.capture(html, tmp_path / "a.png", (390, 844), CHROME)
    assert (tmp_path / "a.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_run_fixes_broken_spec_in_same_conversation(tmp_path, monkeypatch):
    repo = tmp_path / "product"
    (repo / "specs").mkdir(parents=True)
    calls = []

    def fake(step, prompt, *, tier=None, max_turns=40, cwd=None, state_dir=None):
        root = Path(cwd)
        calls.append((step, prompt, state_dir, (root / "product" / "specs").is_dir(), (root / "issue.md").read_text(encoding="utf-8")))
        s = spec() if len(calls) > 1 else spec(recommended="z")
        (root / "out" / "uxspec.yaml").write_text(yaml.safe_dump(s, allow_unicode=True), encoding="utf-8")
        for v in ("a", "b"):
            (root / "out" / f"{v}.html").write_text("<p>案</p>", encoding="utf-8")
        return agent_runner.Result(output="", cost_usd=0.5, usage={"input_tokens": 10}, model="m", num_turns=3)

    monkeypatch.setattr(agent_runner, "run_agentic", fake)
    monkeypatch.setattr(ud, "capture_all", lambda s, out, defs, chromium=None: [] if (out / "a.html").exists() else ["a"])
    out = tmp_path / "out"
    r = ud.run(repo, {"repo": "o/r", "number": 5, "title": "お知らせ", "body": "受け入れ条件: 送る前に確認"}, {}, out, DEFS)
    assert r["ok"] and r["uxspec"]["recommended"] == "a" and r["cost_usd"] == 1.0 and r["usage"]["input_tokens"] == 20
    assert len(calls) == 2 and calls[0][2] == calls[1][2]   # 同じ会話（state_dir）で直させた
    assert calls[1][1].startswith("out/uxspec.yaml か案の HTML に次の崩れ") and "recommended が" in calls[1][1]
    assert calls[0][3] and "受け入れ条件" in calls[0][4]
    assert sorted(p.name for p in out.iterdir()) == ["a.html", "b.html", "uxspec.yaml"]


def test_main_check(tmp_path, capsys):
    p = tmp_path / "uxspec.yaml"
    p.write_text(yaml.safe_dump(spec()), encoding="utf-8")
    assert ud.main(["--check", str(p)]) == 1   # 案の HTML がない
    for v in ("a", "b"):
        (tmp_path / f"{v}.html").write_text("x", encoding="utf-8")
    assert ud.main(["--check", str(p)]) == 0 and capsys.readouterr().out.strip().endswith("ok")


def test_package_is_required_and_checked(tmp_path):
    for v in ("a", "b"):
        (tmp_path / f"{v}.html").write_text("<p>x</p>", encoding="utf-8")
    bare = {k: v for k, v in spec().items() if k not in ("prd", "user_flow", "rationale")}
    errs = ud.validate(bare, DEFS, tmp_path)
    assert {"prd がない", "user_flow がない", "rationale がない"} <= set(errs)
    broken = spec(prd={"goal": "x"}, user_flow=[{"from": "a"}], rationale={"why": "", "rejected": "なし"})
    errs = ud.validate(broken, DEFS, tmp_path)
    assert any(e.startswith("prd に goal") for e in errs) and any(e.startswith("user_flow が") for e in errs)
    assert any(e.startswith("rationale に why") for e in errs) and any(e.startswith("rationale.rejected") for e in errs)
    no_tr = spec(screens=[{"id": "s", "purpose": "p", "new": True, "elements": [], "behaviors": []}])
    assert "screens[0] に transitions がない" in ud.validate(no_tr, DEFS, tmp_path)


def test_review_request_needs_summary_changes_and_tables(tmp_path):
    """レビュー依頼の本文（概要・変更点）と、項目定義・振る舞いの表（2026-11-01 の見直し）"""
    for v in ("a", "b"):
        (tmp_path / f"{v}.html").write_text("<p>x</p>", encoding="utf-8")
    bare = {k: v for k, v in spec().items() if k not in ("summary", "changes")}
    assert {"summary がない", "changes がない"} <= set(ud.validate(bare, DEFS, tmp_path))
    loose = spec(summary=" ", changes=[{"screen": "s", "kind": "moved"}],
                 screens=[{"id": "s", "purpose": "p", "new": True, "transitions": {}, "elements": ["題"], "behaviors": ["送る"]}])
    errs = "\n".join(ud.validate(loose, DEFS, tmp_path))
    for part in ("summary（概要", "changes が変更点", "elements が項目定義の表", "behaviors が振る舞いの表"):
        assert part in errs


def test_frames_are_checked_and_captured_as_files(tmp_path):
    vs = [{"id": "a", "name": "確認の段", "tradeoff": "t", "frames": [{"id": "confirm", "caption": "送信を押した後"}, {"id": "empty", "caption": "0 件"}]},
          {"id": "b", "name": "並べる", "tradeoff": "t"}]
    for n in ("a", "b", "a-confirm"):
        (tmp_path / f"{n}.html").write_text("<p>x</p>", encoding="utf-8")
    assert ud.validate(spec(variants=vs), DEFS, tmp_path) == ["out/a-empty.html がない"]
    bad = [{**vs[0], "frames": [{"id": "x" * 40, "caption": "c"}, {"id": "y"}] + [{"id": f"f{i}", "caption": "c"} for i in range(8)]}, vs[1]]
    errs = "\n".join(ud.validate(spec(variants=bad), DEFS))
    for part in ("8 コマまで", "32 字まで", "id と caption"):
        assert part in errs


def test_prompt_asks_for_the_review_request():
    text = ud.prompt(DEFS, {"repo": "o/r", "number": 5}, {})
    for k in DEFS["package"]["screen"]:
        assert f"     - {k}: " in text
    for part in ("summary: 概要", "prd: 背景", "changes: 変更点", "frames（画面の流れ", "PR の説明"):
        assert part in text


def test_look_variants_only_with_brand_impact(tmp_path):
    vs = [{"id": "a", "name": "確認の段", "tradeoff": "t"}, {"id": "b", "name": "並べる", "tradeoff": "t"},
          {"id": "l1", "axis": "look", "name": "落ち着いた", "tradeoff": "t"}, {"id": "l2", "axis": "look", "name": "明るい", "tradeoff": "t"}]
    for v in vs:
        (tmp_path / f"{v['id']}.html").write_text("<p>x</p>", encoding="utf-8")
    assert ud.validate(spec(variants=vs, brand_impact=True, recommended_look="l1"), DEFS, tmp_path) == []
    errs = "\n".join(ud.validate(spec(variants=vs, brand_impact=False, recommended_look="a", recommended="l1"), DEFS, tmp_path))
    for part in ("brand_impact が true のときだけ", "recommended_look が", "recommended が体験の案"):
        assert part in errs
    one_look = vs[:2] + vs[2:3]
    assert any("見た目の案（axis: look）は作るなら" in e for e in ud.validate(spec(variants=one_look, brand_impact=True, recommended_look="l1"), DEFS, tmp_path))
