"""QA 役（core-gates/qa.py、ADR 0028 の決定 2）: ストーリーの読み込み、環境の入口、Verdict の形の検査、直しの会話、渡すファイル"""
import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_runner
import qa

DEFS = qa.load_defs()
STORY = {"type": "UserStory", "id": "coach-replies", "title": "コーチが返事できる", "actor": "coach", "goal": "待たせない",
         "reader": ["coach"], "entry": "チャットを開く", "exit": "返事が届く",
         "steps": [{"where": "a", "do": "送る", "sees": "出る"}, {"where": "b", "do": "確かめる", "sees": "出る"}],
         "must_not": ["他人のスレッドが読める"], "known_gaps": [{"step": 2, "note": "一覧が古い"}]}


def product(tmp: Path, stories=(STORY,)) -> Path:
    d = tmp / "product"
    (d / "specs" / "stories").mkdir(parents=True)
    (d / ".factory-config").mkdir()
    for s in stories:
        (d / "specs" / "stories" / f"{s['id']}.yaml").write_text(yaml.safe_dump(s, allow_unicode=True), encoding="utf-8")
    (d / "specs" / "stories" / "actors.yaml").write_text("actors: []\n", encoding="utf-8")
    (d / ".factory-config" / "ship.yaml").write_text(yaml.safe_dump({
        "environments": {"staging": {"env": {"BACKEND_PORT": 18080, "ADMIN_WEB_PORT": 13000, "STUDENT_WEB_PORT": 13100}}},
        "touch": {"environments": ["staging"], "entries": [{"label": "生徒", "port_env": "STUDENT_WEB_PORT", "path": "/"},
                                                           {"label": "管理", "port_env": "ADMIN_WEB_PORT", "path": "/"}]}},
        allow_unicode=True), encoding="utf-8")
    return d


def verdict(**kw):
    v = {"story_id": "coach-replies", "decision": "hold", "confidence": 0.8,
         "steps": [{"n": 1, "result": "ok", "saw": "出た"}, {"n": 2, "result": "ng", "saw": "出ない"}],
         "evidence": ["一覧が古い"], "interpretation": "2 歩目で止まった",
         "defects": [{"title": "一覧が古い", "step": 2, "viewpoint": "flow", "severity": "s2", "actual": "まだメッセージはありません"}]}
    v.update(kw)
    return v


def test_load_stories_only_active_and_type(tmp_path):
    d = product(tmp_path, [STORY, {**STORY, "id": "draft-one", "status": "draft"}])
    assert [s["id"] for s in qa.load_stories(d)] == ["coach-replies"]   # actors.yaml と draft は流さない
    assert qa.load_stories(d, ["nope"]) == []


def test_env_entries_use_worker_host_and_touch_ports(tmp_path):
    e = qa.env_entries(product(tmp_path), "staging", "host.docker.internal")
    assert [x["url"] for x in e["entries"]] == ["http://host.docker.internal:13100/", "http://host.docker.internal:13000/"]
    assert qa.env_entries(product(tmp_path / "x"), "prod", "h")["entries"] == []


def test_validate_ok_and_errors(tmp_path):
    assert qa.validate({"verdicts": [verdict()]}, [STORY], DEFS) == []
    assert qa.validate({"verdicts": [verdict(decision="pass", defects=[])]}, [STORY], DEFS) == []
    errs = qa.validate({"verdicts": [verdict(decision="maybe", confidence=2, steps=[{"n": 1, "result": "ok"}], defects=[])]}, [STORY], DEFS)
    assert any("decision" in e for e in errs) and any("confidence" in e for e in errs) and any("2 歩" in e for e in errs)
    assert any("defects が 1 件以上" in e for e in qa.validate({"verdicts": [verdict(defects=[])]}, [STORY], DEFS))
    assert any("判定のないストーリー" in e for e in qa.validate({"verdicts": []}, [STORY], DEFS))
    assert any("severity" in e for e in qa.validate({"verdicts": [verdict(defects=[{"title": "x", "actual": "y", "viewpoint": "flow"}])]}, [STORY], DEFS))
    assert any("flags" in e for e in qa.validate({"verdicts": [verdict(flags=[{"viewpoint": "ui", "note": "x"}])]}, [STORY], DEFS))
    # blocked は根拠がなくてもよい（確かめられなかった）
    assert qa.validate({"verdicts": [verdict(decision="blocked", evidence=[], defects=[])]}, [STORY], DEFS) == []
    # 根拠に挙げたスクリーンショットは out/ にあること
    assert any("01-a.png" in e for e in qa.validate({"verdicts": [verdict(evidence=["01-a.png"])]}, [STORY], DEFS, tmp_path))


def test_run_retries_once_and_returns_verdicts_with_known_gaps(tmp_path, monkeypatch):
    d = product(tmp_path)
    calls, seen = [], {}

    def fake(step, text, max_turns, cwd, state_dir, mcp_config=None):
        seen["mcp"] = mcp_config
        calls.append(text)
        out = Path(cwd) / "out"
        env = json.loads((Path(cwd) / "env.json").read_text())
        assert env["accounts"] == [{"label": "本部", "value": "mock.headquarters.x"}]
        if len(calls) == 1:
            (out / "verdicts.yaml").write_text("verdicts: [{story_id: coach-replies}]\n")
        else:
            (out / "01-list.png").write_bytes(b"\x89PNG")
            (out / "bad name.png").write_bytes(b"x")
            (out / "verdicts.yaml").write_text(yaml.safe_dump({"verdicts": [verdict(evidence=["01-list.png"])]}, allow_unicode=True))
        return sys.modules["agent_runner"].Result(output="", cost_usd=1.0, usage={"input_tokens": 10}, model="m", num_turns=3)

    monkeypatch.setattr(sys.modules["agent_runner"], "run_agentic", fake)   # test_agent_runner が sys.modules を差し替えることがある
    res = qa.run(d, {"release_id": "r1", "touchable": {"accounts": [{"label": "本部", "value": "mock.headquarters.x"}]}}, tmp_path / "o", DEFS)
    assert res["ok"] and len(calls) == 2 and "崩れ" in calls[1]
    assert res["files"] == ["01-list.png"] and (tmp_path / "o" / "01-list.png").is_file()
    assert res["verdicts"][0]["known_gaps"] == STORY["known_gaps"] and res["cost_usd"] == 2.0 and res["release_id"] == "r1"
    assert "coach-replies" in calls[0] and "host.docker.internal:13100" in calls[0]


def test_run_without_stories_or_entries(tmp_path):
    d = product(tmp_path, [])
    assert not qa.run(d, {}, tmp_path / "o", DEFS)["ok"]
    d2 = product(tmp_path / "b")
    assert "入口" in qa.run(d2, {"env": "prod"}, tmp_path / "o", DEFS)["errors"][0]


def test_rally_stories_load_if_present():
    rally = Path(__file__).resolve().parents[3] / "rally"
    if (rally / "specs" / "stories").is_dir():
        ids = [s["id"] for s in qa.load_stories(rally)]
        assert len(ids) >= 3
        assert qa.env_entries(rally, "staging", "h")["entries"]


def test_relay_config_and_mobile_prompt(tmp_path, monkeypatch):
    assert qa.relay_config(tmp_path, {}) is None
    p = qa.relay_config(tmp_path, {"RUNNER_RELAY_URL": "http://dispatcher:8796/mcp", "RUNNER_RELAY_TOKEN": "t0k"})
    cfg = json.loads(Path(p).read_text())["mcpServers"]["macrunner"]
    assert cfg == {"type": "http", "url": "http://dispatcher:8796/mcp", "headers": {"Authorization": "Bearer t0k"}}
    assert oct(Path(p).stat().st_mode & 0o777) == "0o600"
    text = qa.prompt(DEFS, [STORY], {"entries": []}, {}, mobile=True)
    assert "mcp__macrunner__" in text and '"RALLY_API_URL": "http://127.0.0.1:18080"' in text
    assert "ref は省く" in text
    pinned = qa.prompt(DEFS, [STORY], {"entries": []}, {"commit": "abc123", "release_id": "guccyon/rally@0.9.0"}, mobile=True)
    assert "ref: abc123" in pinned
    assert "mcp__macrunner__" not in qa.prompt(DEFS, [STORY], {"entries": []}, {})
    # 時間をまたぐ手順（scenario_time）: 中継があれば env_restart で起こし、最後に時計を戻させる。なければ skipped
    assert "mcp__macrunner__env_restart" in text and "reset_clock: true" in text
    assert "サーバーの再起動も時計の進めもできない" in qa.prompt(DEFS, [STORY], {"entries": []}, {})
    d = product(tmp_path / "p")
    seen = {}

    def fake(step, text, max_turns, cwd, state_dir, mcp_config=None):
        seen["mcp"] = mcp_config
        (Path(cwd) / "out" / "verdicts.yaml").write_text(yaml.safe_dump({"verdicts": [verdict()]}, allow_unicode=True))
        return sys.modules["agent_runner"].Result(output="")

    monkeypatch.setattr(sys.modules["agent_runner"], "run_agentic", fake)
    monkeypatch.setenv("RUNNER_RELAY_URL", "http://dispatcher:8796/mcp")
    monkeypatch.setenv("RUNNER_RELAY_TOKEN", "t0k")
    res = qa.run(d, {}, tmp_path / "o", DEFS)
    assert res["ok"] and res["mobile"] and seen["mcp"].endswith(".mcp-relay.json")


# ------------------------------------------------------------------ 夜間の探索（nightly_explore）
def explore_doc(**kw):
    d = {"charter": "detour", "confidence": 0.6, "interpretation": "戻るで崩れる",
         "tried": [{"actor": "coach", "where": "チャット", "did": "送ってすぐ戻る", "saw": "二重に出る（02-dup.png）"}],
         "findings": [{"title": "二重に出る", "viewpoint": "flow", "severity": "s3", "actual": "2 件", "story_id": "coach-replies"}],
         "flags": [{"viewpoint": "tone", "note": "硬い"}]}
    d.update(kw)
    return d


def test_validate_explore(tmp_path):
    ch = qa.charter_of(DEFS, "detour")
    assert qa.charter_of(DEFS, "nope")["id"] == DEFS["explore"]["charters"][0]["id"]
    (tmp_path / "02-dup.png").write_bytes(b"x")
    assert qa.validate_explore(explore_doc(), DEFS, ch, {"coach-replies"}, tmp_path) == []
    assert qa.validate_explore(explore_doc(findings=[]), DEFS, ch, set(), None) == []   # 何も見つからなくてよい
    errs = qa.validate_explore(explore_doc(tried=[], charter="other-user", confidence=2,
                                           findings=[{"title": "x", "actual": "y", "severity": "s9", "viewpoint": "flow", "story_id": "ghost"}]),
                               DEFS, ch, {"coach-replies"}, tmp_path)
    assert any("tried" in e for e in errs) and any("charter" in e for e in errs) and any("confidence" in e for e in errs)
    assert any("severity" in e for e in errs) and any("ghost" in e for e in errs)
    many = explore_doc(findings=[{"title": f"t{i}", "actual": "a", "severity": "s3", "viewpoint": "flow"} for i in range(9)])
    assert any("8 件まで" in e for e in qa.validate_explore(many, DEFS, ch, set(), None))
    assert any("02-dup.png" in e for e in qa.validate_explore(explore_doc(), DEFS, ch, {"coach-replies"}, tmp_path / "none"))


def test_explore_same_as_and_open_defects_in_prompt(tmp_path):
    """開いている不具合を探索に渡し、同じものは same_as に番号を書いてもらう（rally #1373 と #1402 の重複起票）"""
    ch = qa.charter_of(DEFS, "detour")
    f = {"title": "x", "actual": "y", "severity": "s2", "viewpoint": "flow"}
    ok = explore_doc(findings=[{**f, "same_as": 1373}, {**f, "same_as": None}])
    assert qa.validate_explore(ok, DEFS, ch, set(), None, {1373}) == []
    assert qa.validate_explore(ok, DEFS, ch, set(), None, None) == []   # 一覧がなければ数かだけ見る
    assert any("#1402" in e for e in qa.validate_explore(explore_doc(findings=[{**f, "same_as": 1402}]), DEFS, ch, set(), None, {1373}))
    assert any("same_as" in e for e in qa.validate_explore(explore_doc(findings=[{**f, "same_as": "#1373"}]), DEFS, ch, set(), None))
    inp = {"open_defects": [{"number": 1373, "title": "月額契約の画面へ進めない", "story_id": "student-subscription-persists", "severity": "s2"}]}
    text = qa.open_defects_text(inp)
    assert "#1373 [s2] 月額契約の画面へ進めない（ストーリー student-subscription-persists）" in text and "same_as" in text
    assert qa.open_defects_text({}) == ""
    prompt = qa.explore_prompt(DEFS, ch, [], {}, inp)
    assert "すでに開いている不具合（1 件）" in prompt and "same_as" in prompt


def test_run_explore(tmp_path, monkeypatch):
    d = product(tmp_path)
    calls = []

    def fake(step, text, max_turns, cwd, state_dir, mcp_config=None):
        calls.append(text)
        out = Path(cwd) / "out"
        if len(calls) == 1:
            (out / "explore.yaml").write_text("charter: detour\n")
        else:
            (out / "02-dup.png").write_bytes(b"\x89PNG")
            (out / "explore.yaml").write_text(yaml.safe_dump(explore_doc(charter="come-back"), allow_unicode=True))
        return sys.modules["agent_runner"].Result(output="", cost_usd=0.5, usage={}, model="m", num_turns=2)

    monkeypatch.setattr(sys.modules["agent_runner"], "run_agentic", fake)
    res = qa.run(d, {"mode": "explore", "charter": "come-back", "night": "2026-11-08", "release_id": "r1"}, tmp_path / "o", DEFS)
    assert res["ok"] and res["mode"] == "explore" and res["charter"] == "come-back" and res["night"] == "2026-11-08"
    assert len(calls) == 2 and "崩れ" in calls[1] and "今夜のチャーター: come-back" in calls[0] and "coach-replies" in calls[0]
    assert "--check-explore" in calls[0] and res["files"] == ["02-dup.png"] and res["verdicts"] == []
    assert res["explore"]["findings"][0]["known_gaps"] == STORY["known_gaps"]


def test_check_explore_cli(tmp_path, capsys):
    d = product(tmp_path)
    p = tmp_path / "out" / "explore.yaml"
    p.parent.mkdir()
    p.write_text(yaml.safe_dump(explore_doc(tried=[{"did": "a", "saw": "b"}]), allow_unicode=True))
    assert qa.main(["--check-explore", str(p), "--charter", "detour", "--repo-dir", str(d)]) == 0
    assert capsys.readouterr().out.strip() == "ok"


def test_product_severities_reach_the_prompt(tmp_path):
    """製品の品質方針の S1 の条件と S2〜S4 の基準を、重要度の説明に足す（ADR 0043 の O5）。名前（s1〜s4）は変えない"""
    d = product(tmp_path)
    assert qa.with_product_severities(DEFS, d) is DEFS   # 品質方針がなければそのまま
    (d / ".factory-config" / "quality_policy.yaml").write_text(yaml.safe_dump({
        "s1": {"conditions": [{"id": "data_leak", "ja": "他人のデータが見える"}]},
        "severities": {"s2": {"ja": "流れが止まる", "conditions": [{"id": "x", "ja": "予約が確定しない"}]}}}, allow_unicode=True), encoding="utf-8")
    got = qa.with_product_severities(DEFS, d)
    assert set(got["severities"]) == set(DEFS["severities"])
    assert "他人のデータが見える" in got["severities"]["s1"] and "予約が確定しない" in got["severities"]["s2"]
    assert got["severities"]["s3"] == DEFS["severities"]["s3"]
    assert "他人のデータが見える" in qa.prompt(got, [STORY], {"env": "staging", "entries": []}, {})
