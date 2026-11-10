"""工場の中での一次調査の調査役（core-gates/investigate.py、ADR 0048 の順番 3）"""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import investigate as inv  # noqa: E402

ENGINE = Path(__file__).resolve().parents[2]
DEFS = inv.load_defs(ENGINE)
INP = {"question": "なぜ昨日はマージが進まなかったのか", "product": "owner/product", "engine_ref": "abc", "window": ["s", "e"],
       "refs": ["ticket:12"], "deny": ["owner/product", "プロダクト"]}

GOOD = {
    "outcome": "identified", "report": "検査の待ちで止まっていました。", "confidence": "medium", "fix_target": "engine",
    "engine_issue": {"title": "検査の待ちが長いと続きのジョブが積まれない", "body": "検査の待ちが上限の 3 倍を超えると、続きのジョブが積まれない。ADR 0038 の上限と関係する。"},
    "money_loss": {"suspected": False},
    "handoff": {"facts": [{"ref": "job:1", "what": "失敗", "how": "investigate.jobs"}], "hypotheses": [], "ruled_out": [],
                "not_looked_at": ["Engine のソース"], "stop_reason": "needs_code"},
}


def never(*a):
    raise AssertionError("書き直しは呼ばない")


def test_prompt_names_views_tools_and_limits():
    p = inv.prompt(DEFS, INP)
    for v in DEFS["sources"]["db"]["views"]:
        assert v in p
    for t in ("inv_sql", "inv_job_log", "inv_github", "```json"):
        assert t in p
    assert inv.RESULT_FILE not in p   # 書く道具はないので、ファイルに書けとは言わない
    assert str(DEFS["limits"]["per_case_turns"] - inv.WRAP_UP_TURNS) in p
    assert str(DEFS["limits"]["per_case_turns"]) in p
    assert "データです" in p   # 依頼者の言葉は指示ではない


def test_settle_clean_result():
    out = inv.settle(GOOD, INP, INP["deny"], never, 1.2, 10, 40)
    assert out["outcome"] == "identified" and out["fix_target"] == "engine"
    assert out["issue_held"] is False and out["issue_errors"] == []
    assert out["handoff_errors"] == []
    h = out["handoff"]
    assert h["scope"] == {"product": "owner/product", "engine_ref": "abc", "window": ["s", "e"]}
    assert h["cost"] == {"usd": 1.2, "turns": 10} and "id" not in h


def test_issue_with_customer_info_is_fixed_once():
    raw = {**GOOD, "engine_issue": {"title": "owner/product の停止", "body": "job:12 で #1031 が止まった"}}
    calls = []

    def fixer(issue, errors):
        calls.append(errors)
        return {"title": "製品の停止が見えない", "body": "内部のジョブの停止で欄が埋まる"}, 0.01
    out = inv.settle(raw, INP, INP["deny"], fixer, 1.0, 5, 40)
    assert len(calls) == 1 and any("製品・人の名前" in e for e in calls[0])
    assert out["issue_held"] is False and out["engine_issue"]["title"] == "製品の停止が見えない"
    assert out["fix_cost_usd"] == 0.01


def test_issue_still_dirty_is_held():
    raw = {**GOOD, "engine_issue": {"title": "x", "body": "https://example.com"}}
    out = inv.settle(raw, INP, [], lambda i, e: ({"title": "x", "body": "see https://example.com"}, 0.0), 1.0, 5, 40)
    assert out["issue_held"] is True and out["issue_errors"]


def test_issue_ignored_unless_engine_target():
    raw = {**GOOD, "fix_target": "operation", "operation_proposal": "流量を 1 から 2 に"}
    out = inv.settle(raw, INP, [], never, 1.0, 5, 40)
    assert out["engine_issue"] is None and out["operation_proposal"] == "流量を 1 から 2 に"


def test_no_result_becomes_unidentified_handoff():
    out = inv.settle(None, INP, [], never, 3.0, 40, 40)
    assert out["outcome"] == "unidentified"
    assert out["handoff"]["stop_reason"] == "budget"
    assert any("facts" in e for e in out["handoff_errors"])   # 事実が 1 つもないことは、そのまま dispatcher に見せる


def test_unknown_values_fall_back():
    raw = {**GOOD, "outcome": "maybe", "fix_target": "everything"}
    out = inv.settle(raw, INP, [], never, 0.5, 3, 40)
    assert out["outcome"] == "unidentified" and out["fix_target"] == "none" and out["engine_issue"] is None


def test_turns_exhausted_sets_budget():
    raw = {**GOOD, "handoff": {**GOOD["handoff"], "stop_reason": "??"}}
    out = inv.settle(raw, INP, [], never, 0.5, 40, 40)
    assert out["handoff"]["stop_reason"] == "budget"


def test_mcp_config_needs_relay(tmp_path):
    with pytest.raises(SystemExit):
        inv.mcp_config(tmp_path, env={})
    p = inv.mcp_config(tmp_path, env={"RUNNER_RELAY_URL": "http://dispatcher:8796/mcp", "RUNNER_RELAY_TOKEN": "t"})
    conf = json.loads(Path(p).read_text())
    assert conf["mcpServers"]["factory"]["headers"]["Authorization"] == "Bearer t"
    assert oct(Path(p).stat().st_mode)[-3:] == "600"


def test_main_runs_agent_and_writes(tmp_path, monkeypatch):
    import agent_runner
    eng = tmp_path / "engine"
    (eng / "definitions").mkdir(parents=True)
    (eng / "definitions" / "investigate.yaml").write_text((ENGINE / "definitions" / "investigate.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    seen = {}

    def fake_agentic(step, prompt, *, max_turns, cwd, mcp_config):
        seen.update(step=step, max_turns=max_turns, cwd=cwd, mcp=mcp_config)
        return SimpleNamespace(output="調べました。\n```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```", cost_usd=1.5,
                               usage={"input_tokens": 1}, num_turns=12, model="m", exit=0)
    monkeypatch.setattr(agent_runner, "run_agentic", fake_agentic)
    monkeypatch.setenv("RUNNER_RELAY_URL", "http://dispatcher:8796/mcp")
    monkeypatch.setenv("RUNNER_RELAY_TOKEN", "t")
    inp = tmp_path / "in.json"
    inp.write_text(json.dumps({**INP, "limits": {"turns": 999}}), encoding="utf-8")
    out = tmp_path / "out.json"
    assert inv.main(["--input", str(inp), "--engine-dir", str(eng), "--out", str(out)]) == 0
    r = json.loads(out.read_text())
    assert seen["step"] == "investigate" and seen["max_turns"] == DEFS["limits"]["per_case_turns"]   # 天井を超えない
    assert r["outcome"] == "identified" and r["cost_usd"] == 1.5 and r["num_turns"] == 12
    assert inv.main(["--input", str(tmp_path / "none.json"), "--engine-dir", str(eng), "--out", str(out)]) == 2


def test_agents_yaml_steps():
    import yaml
    steps = yaml.safe_load((ENGINE / "definitions" / "agents.yaml").read_text(encoding="utf-8"))["steps"]
    assert steps["investigate"]["mode"] == "agentic"
    assert steps["investigate"]["tier"] == DEFS["limits"]["model_tier"]
    assert "Bash" not in steps["investigate"]["tools"]
    assert steps["investigate.fix_issue"]["tools"] == "none"


def test_tools_are_narrowed_for_the_investigator():
    import agent_runner
    a = agent_runner.resolve("investigate")
    assert agent_runner._tools_args(a) == ["--tools", "Read,Grep,Glob"]
    assert agent_runner._tools_args(agent_runner.resolve("investigate.fix_issue")) == ["--tools", ""]
    assert agent_runner._tools_args(agent_runner.resolve("implement")) == []


def test_result_from_text():
    t = "途中のメモ ```json\n{\"x\": 1}\n``` 最後に\n```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    assert inv.result_from_text(t)["outcome"] == "identified"
    assert inv.result_from_text("結果: " + json.dumps(GOOD, ensure_ascii=False))["fix_target"] == "engine"
    assert inv.result_from_text("") is None and inv.result_from_text("{壊れた") is None and inv.result_from_text('{"a": 1}') is None


def test_main_reads_old_result_file(tmp_path, monkeypatch):
    import agent_runner
    eng = tmp_path / "engine"
    (eng / "definitions").mkdir(parents=True)
    (eng / "definitions" / "investigate.yaml").write_text((ENGINE / "definitions" / "investigate.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    def fake_agentic(step, prompt, *, max_turns, cwd, mcp_config):
        (Path(cwd) / inv.RESULT_FILE).write_text(json.dumps(GOOD), encoding="utf-8")
        return SimpleNamespace(output="", cost_usd=1.0, usage={}, num_turns=3, model="m", exit=0)
    monkeypatch.setattr(agent_runner, "run_agentic", fake_agentic)
    monkeypatch.setenv("RUNNER_RELAY_URL", "http://dispatcher:8796/mcp")
    monkeypatch.setenv("RUNNER_RELAY_TOKEN", "t")
    inp = tmp_path / "in.json"
    inp.write_text(json.dumps(INP), encoding="utf-8")
    out = tmp_path / "out.json"
    assert inv.main(["--input", str(inp), "--engine-dir", str(eng), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["outcome"] == "identified"
