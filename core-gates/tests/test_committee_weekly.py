"""知見の委員会の週 1 の定例（core-gates/committee_weekly.py、ADR 0024）"""
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_runner
import committee_weekly as cw
import knowledge_check as kc

TODAY = date(2026, 12, 15)
K = """id: K-0001
theme: 4
kind: term_usage
statement: 利用者向けの文言では「スロット」ではなく「予約枠」と書く
scope: {contexts: [scheduling], roles: [implementer], paths: ["apps/**"]}
confidence: hypothesis
evidence:
  - case: C-0001
status: active
verified_at: 2026-11-01
review_by: 2026-11-30
decided_by: committee:D-0001
confidentiality: product
"""


def product(tmp_path: Path, with_k=True) -> Path:
    k = tmp_path / kc.ROOT
    (k / "cases").mkdir(parents=True)
    (k / "committee").mkdir()
    (k / "cases" / "C-0001.md").write_text("# 事例\n", encoding="utf-8")
    (k / "committee" / "D-0001.yaml").write_text("id: D-0001\n", encoding="utf-8")
    if with_k:
        (k / "K-0001.yaml").write_text(K, encoding="utf-8")
    return tmp_path


def test_facts_find_stale_and_counts(tmp_path):
    f = cw.facts(product(tmp_path), TODAY, 150)
    assert f["items"] == 1 and f["stale"][0]["id"] == "K-0001" and f["by_confidence"] == {"hypothesis": 1}
    assert cw.facts(product(tmp_path / "x"), TODAY, 0)["structure_signal"] is True


def test_main_runs_chair_on_a_copy(tmp_path, monkeypatch):
    repo = product(tmp_path / "p")
    seen = []

    def run(step, prompt, *, schema=None, tier=None, max_turns=3, retries=3):
        seen.append((step, sorted(p.name for p in Path(".").iterdir()), "期限切れ 1 件" in prompt))
        return agent_runner.Result(output={"summary": "s", "contradictions": [], "promote": [], "retire": [],
                                           "enforce": [], "structure": {"needed": False, "proposal": ""}}, cost_usd=0.2, model="m")
    monkeypatch.setattr(agent_runner, "run", run)
    (tmp_path / "in.json").write_text(json.dumps({"decisions": []}), encoding="utf-8")
    out = tmp_path / "out.json"
    assert cw.main(["--repo-dir", str(repo), "--input", str(tmp_path / "in.json"), "--out", str(out), "--today", str(TODAY)]) == 0
    r = json.loads(out.read_text(encoding="utf-8"))
    assert seen == [("committee_weekly", ["knowledge", "week.json"], True)] and r["facts"]["stale"][0]["id"] == "K-0001"


def test_empty_store_skips_agent(tmp_path, monkeypatch):
    repo = product(tmp_path / "p", with_k=False)
    monkeypatch.setattr(agent_runner, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("呼ばない")))
    (tmp_path / "in.json").write_text("{}", encoding="utf-8")
    out = tmp_path / "out.json"
    assert cw.main(["--repo-dir", str(repo), "--input", str(tmp_path / "in.json"), "--out", str(out), "--today", str(TODAY)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["result"]["summary"] == "知見も議決もまだない"
