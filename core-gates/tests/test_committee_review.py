"""知見の委員会（core-gates/committee_review.py、ADR 0024 の 4）: 議決の集め方、下書きの採用、PR のブランチへの記録"""
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_runner
import committee_review as cr

ROLES = ["consistency", "evidence", "enforcement", "dissent"]
TODAY = date(2026, 11, 1)

DRAFT = """id: K-0002
theme: 6
kind: causal
statement: 受け入れ条件に画面の状態（読み込み中・0 件・失敗）が無いと、差し戻しが起きやすい
scope: {contexts: [scheduling], roles: [implementer], paths: ["apps/**"]}
confidence: hypothesis
evidence:
  - case: C-0002
status: draft
verified_at: 2026-11-01
review_by: 2026-11-30
confidentiality: product
"""


def V(role, vote, **kw):
    return {"role": role, "vote": vote, "reasons": ["r"], "personal_data": False, "enforcement": [], **kw}


def test_aggregate():
    assert cr.aggregate([V(r, "adopt") for r in ROLES], ROLES) == "adopt"
    assert cr.aggregate([V(r, "adopt") for r in ROLES[:3]] + [V("dissent", "enforce")], ROLES) == "enforce"
    assert cr.aggregate([V(r, "sendback") for r in ROLES[:3]] + [V("dissent", "adopt")], ROLES) == "sendback"
    assert cr.aggregate([V(r, "adopt") for r in ROLES[:3]] + [V("dissent", "sendback")], ROLES) == "human"   # 割れたら人
    assert cr.aggregate([V(r, "adopt") for r in ROLES[:3]] + [V("dissent", "human")], ROLES) == "human"
    assert cr.aggregate([V(r, "adopt") for r in ROLES[:3]], ROLES) == "human"                               # 欠けた役
    assert cr.aggregate([V(r, "adopt") for r in ROLES[:3]] + [V("dissent", "adopt", personal_data=True)], ROLES) == "sendback"


def test_activate_keeps_format():
    out = cr.activate(DRAFT, "D-0003")
    assert "status: active" in out and "status: draft" not in out
    assert out.rstrip().endswith("decided_by: committee:D-0003") and "confidence: hypothesis" in out
    assert cr.activate("status: draft\ndecided_by: x\n", "D-0004") == "status: active\ndecided_by: committee:D-0004\n"


def git(d, *a):
    subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)


def product(tmp_path: Path) -> Path:
    d = tmp_path / "p"
    k = d / cr.ROOT
    (k / "cases").mkdir(parents=True)
    (k / "committee").mkdir()
    (k / "README.md").write_text("# knowledge\n", encoding="utf-8")
    (k / "committee" / ".gitkeep").write_text("", encoding="utf-8")
    (d / "specs").mkdir()
    (d / "specs" / "glossary.yaml").write_text("terms: []\n", encoding="utf-8")
    git(tmp_path, "init", "-q", "-b", "main", str(d))
    git(d, "config", "user.email", "t@example.invalid")
    git(d, "config", "user.name", "t")
    subprocess.run([sys.executable, str(Path(cr.__file__).parent / "knowledge_check.py"), "--repo", str(d), "--index"], check=True, capture_output=True)
    git(d, "add", "-A")
    git(d, "commit", "-qm", "base")
    git(d, "checkout", "-qb", "pr")
    (k / "cases" / "C-0002.md").write_text("# 事例\n画面の状態が受け入れ条件に無かった\n", encoding="utf-8")
    (k / "K-0002.yaml").write_text(DRAFT, encoding="utf-8")
    subprocess.run([sys.executable, str(Path(cr.__file__).parent / "knowledge_check.py"), "--repo", str(d), "--index"], check=True, capture_output=True)
    git(d, "add", "-A")
    git(d, "commit", "-qm", "pr")
    return d


def fake_run(votes: dict, seen: list):
    def run(step, prompt, *, schema=None, tier=None, max_turns=3, retries=3):
        role = next(r for r in ROLES if f"の「{ {'consistency': '整合', 'evidence': '根拠', 'enforcement': '担保', 'dissent': '反対'}[r] }」" in prompt)
        seen.append((role, sorted(p.name for p in Path(".").iterdir())))
        v = V(role, votes[role])
        v.pop("role")
        if votes[role] == "enforce":
            v["enforcement"] = [{"knowledge": "K-0002", "level": "deterministic", "solution_kind": "ci_check", "proposal": "受け入れ条件の検査"}]
        return agent_runner.Result(output=v, cost_usd=0.1, model="m")
    return run


def test_adopt_writes_decision_and_activates_draft(tmp_path, monkeypatch):
    d = product(tmp_path)
    seen = []
    monkeypatch.setattr(agent_runner, "run", fake_run({"consistency": "adopt", "evidence": "adopt", "enforcement": "enforce", "dissent": "adopt"}, seen))
    out = tmp_path / "out.json"
    assert cr.main(["--repo-dir", str(d), "--base", "main", "--pr-url", "https://github.com/o/r/pull/9", "--out", str(out), "--today", str(TODAY)]) == 0
    r = json.loads(out.read_text(encoding="utf-8"))
    assert r["outcome"] == "enforce" and r["decision"] == "D-0001" and r["enforce"][0]["knowledge"] == "K-0002"
    assert [s[0] for s in seen] == ROLES and all(s[1] == ["change.diff", "knowledge", "specs"] for s in seen)   # 材料は写しだけ
    k = (d / cr.ROOT / "K-0002.yaml").read_text(encoding="utf-8")
    assert "status: active" in k and "decided_by: committee:D-0001" in k
    dec = yaml.safe_load((d / cr.ROOT / "committee" / "D-0001.yaml").read_text(encoding="utf-8"))
    assert dec["outcome"] == "enforce" and dec["knowledge"] == ["K-0002"] and len(dec["votes"]) == 4
    assert "K-0002" in (d / cr.ROOT / "INDEX.md").read_text(encoding="utf-8")


def test_sendback_writes_nothing(tmp_path, monkeypatch):
    d = product(tmp_path)
    monkeypatch.setattr(agent_runner, "run", fake_run({r: "sendback" for r in ROLES}, []))
    out = tmp_path / "out.json"
    assert cr.main(["--repo-dir", str(d), "--base", "main", "--pr-url", "https://github.com/o/r/pull/9", "--out", str(out), "--today", str(TODAY)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["outcome"] == "sendback"
    assert not list((d / cr.ROOT / "committee").glob("D-*.yaml"))
    assert "status: draft" in (d / cr.ROOT / "K-0002.yaml").read_text(encoding="utf-8")
