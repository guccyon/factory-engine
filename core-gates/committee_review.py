#!/usr/bin/env python3
"""知見の委員会（ADR 0024）。知見の変更セットの PR 1 本を、観点別の 4 役（整合・根拠・担保・反対）が別々に審査し、議決を決める。

- 4 役はそれぞれ別のエージェント。知見を書いた分析役・実装役は入らない（学ぶ側に自分を採点させない）
- 材料（変更の差分、PR の head の知見の置き場、specs/）は一時ディレクトリに写してから読ませる。製品のチェックアウトには触らせない
- 議決は決定論で集める（aggregate）。全員が採る → adopt（担保の案があれば enforce）、3 役以上が差し戻す → sendback、
  個人情報を見つけた役がいる → sendback、それ以外（割れた・人に回すべきという票がある）→ human
- sendback 以外は、議決 committee/D-NNNN.yaml を書き、この PR で足した下書きの知見を active にして decided_by を付け、INDEX.md を作り直す。
  human のときも同じ形にし、人のマージを採用の承認とする（閉じれば不採用）
- 委員会は実装しない。担保の案（enforce）は結果に返し、dispatcher が Issue にして工場のラインで作る

使い方:
  core-gates/committee_review.py --repo-dir <PR の head のチェックアウト> --base <比べる ref> --pr-url <URL> --out result.json

依存: python3、PyYAML、git、agent_runner（工程 committee_review。runner とモデルは definitions/agents.yaml）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
KNOWLEDGE_DEFS = Path(os.environ.get("KNOWLEDGE_DEFINITIONS") or HERE.parent / "definitions" / "knowledge.yaml")
ROOT = ".factory-config/knowledge"
VOTES = ["adopt", "enforce", "human", "sendback"]
MAX_TURNS = 30


# ------------------------------------------------------------------ 議決（決定論）
def aggregate(votes: list[dict], roles: list[str]) -> str:
    got = {v.get("role"): v for v in votes}
    if any(r not in got or got[r].get("vote") not in VOTES for r in roles):
        return "human"   # 欠けた役がいれば人へ
    vs = [got[r]["vote"] for r in roles]
    if any(got[r].get("personal_data") for r in roles):
        return "sendback"
    if all(v in ("adopt", "enforce") for v in vs):
        return "enforce" if "enforce" in vs else "adopt"
    if vs.count("sendback") >= 3:
        return "sendback"
    return "human"


def next_id(d: Path, prefix: str) -> str:
    nums = [int(m.group(1)) for p in d.glob(f"{prefix}-*.yaml") if (m := re.fullmatch(rf"{prefix}-(\d+)", p.stem))]
    return f"{prefix}-{(max(nums) + 1) if nums else 1:04d}"


def activate(text: str, decision: str) -> str:
    """下書きの知見を active にし、議決を decided_by に付ける。書式（コメント・並び）は崩さない"""
    text = re.sub(r"(?m)^status:\s*draft\s*$", "status: active", text, count=1)
    if re.search(r"(?m)^decided_by:", text):
        return re.sub(r"(?m)^decided_by:.*$", f"decided_by: committee:{decision}", text, count=1)
    return text.rstrip("\n") + f"\ndecided_by: committee:{decision}\n"


# ------------------------------------------------------------------ 材料
def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def changed(repo: Path, base: str) -> tuple[list[str], list[str], str]:
    """この PR で知見の置き場に足した・変えたファイルと、差分"""
    status = git(repo, "diff", "--name-status", f"{base}...HEAD", "--", ROOT)
    added, modified = [], []
    for line in status.splitlines():
        parts = line.split("\t")
        (added if parts[0].startswith("A") else modified).append(parts[-1])
    return added, modified, git(repo, "diff", f"{base}...HEAD", "--", ROOT)


def schema(defs: dict) -> dict:
    enf = defs["knowledge"]["enforcement"]
    return {"type": "object", "properties": {
        "vote": {"type": "string", "enum": VOTES},
        "reasons": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "personal_data": {"type": "boolean"},
        "enforcement": {"type": "array", "maxItems": 3, "items": {"type": "object", "properties": {
            "knowledge": {"type": "string"}, "level": {"type": "string", "enum": enf["levels"]},
            "solution_kind": {"type": "string", "enum": enf["solution_kinds"]}, "proposal": {"type": "string"}},
            "required": ["knowledge", "level", "solution_kind", "proposal"]}}},
        "required": ["vote", "reasons", "personal_data", "enforcement"]}


def prompt(role: str, spec: dict, defs: dict, files: list[str]) -> str:
    others = "\n".join(f"- {k}（{v['ja']}）: {v['question']}" for k, v in defs["committee"]["roles"].items() if k != role)
    return f"""あなたは知見の委員会の「{spec['ja']}」の担当です。工場のラインが書いた知見の変更セット（PR）を、あなたの観点だけで審査します。

あなたの問い: {spec['question']}

ほかの担当（あなたは見なくてよい）:
{others}

作業ディレクトリに材料があります。読むだけにし、何も書き換えないでください。
- change.diff: この PR の知見の置き場の差分
- knowledge/: PR の head の知見の置き場（K-*.yaml が知見、cases/ が事例記録、committee/ が過去の議決、README.md が決まり）
- specs/: 製品の規範（用語集 glossary.yaml、コンテキスト context-map.yaml など）

この PR で足した・変えたファイル: {', '.join(files) or '（なし）'}

票の決め方:
- adopt: あなたの観点で問題がなく、採ってよい
- enforce: 採ってよく、さらに決定論（lint・テスト・型・スキーマ・CI・設定・overlay のルール）で担保すべき。enforcement に知見の id・強さ・解決策の種類・具体案を書く
- human: あなたの観点では決められない（新しい用語・業務ルールの定義、役割への指示・ラインの設定・Engine、Blueprint に作用するなど）
- sendback: あなたの観点で採れない（根拠に辿れない、矛盾する、範囲が広すぎる、など）。reasons に直し方を書く
- personal_data: 利用者や人を特定できる情報（氏名・連絡先・利用者 ID・本文の引用など）が残っていれば true

決まり:
- 知見は下書き（status: draft、confidence: hypothesis）。採っても、確かさが confirmed になるまでエージェントには渡らない
- 確かさを上げる・下げるべきなら reasons に書く
- reasons は 1 つ 1 文、日本語の平易な文で。識別子はそのまま書く
"""


def stage(repo: Path, diff: str, root: Path):
    (root / "change.diff").write_text(diff, encoding="utf-8")
    shutil.copytree(repo / ROOT, root / "knowledge")
    if (repo / "specs").is_dir():
        shutil.copytree(repo / "specs", root / "specs", ignore=shutil.ignore_patterns("*.png", "*.jpg", "*.pdf"))


def review(repo: Path, base: str, defs: dict) -> dict:
    import agent_runner  # 工程からエージェントを呼ぶ唯一の入口（ADR 0010）
    added, modified, diff = changed(repo, base)
    votes, cost, usage, model = [], 0.0, {}, None
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        stage(repo, diff, root)
        cwd = os.getcwd()
        os.chdir(root)   # agent_runner はカレントディレクトリでエージェントを動かす
        try:
            for role, spec in defs["committee"]["roles"].items():
                res = agent_runner.run("committee_review", prompt(role, spec, defs, added + modified), schema=schema(defs), max_turns=MAX_TURNS)
                out = res.output if isinstance(res.output, dict) else {}
                votes.append({"role": role, **out})
                cost += res.cost_usd or 0
                model = res.model
                for k, v in (res.usage or {}).items():
                    if isinstance(v, (int, float)):
                        usage[k] = usage.get(k, 0) + v
        finally:
            os.chdir(cwd)
    return {"added": added, "modified": modified, "votes": votes, "cost_usd": cost, "usage": usage, "model": model}


# ------------------------------------------------------------------ 議決を書く
def write_decision(repo: Path, pr_url: str, outcome: str, votes: list[dict], added: list[str], today: date) -> str | None:
    kdir = repo / ROOT
    (kdir / "committee").mkdir(exist_ok=True)
    did = next_id(kdir / "committee", "D")
    new_k = [p for p in added if re.fullmatch(rf"{re.escape(ROOT)}/K-\d+\.ya?ml", p)]
    for p in new_k:
        f = repo / p
        if re.search(r"(?m)^status:\s*draft\s*$", f.read_text(encoding="utf-8")):
            f.write_text(activate(f.read_text(encoding="utf-8"), did), encoding="utf-8")
    doc = {"id": did, "pr": pr_url, "decided_at": today.isoformat(), "outcome": outcome, "requires_human": outcome == "human",
           "knowledge": [Path(p).stem for p in new_k],
           "votes": [{"role": v["role"], "vote": v.get("vote"), "reasons": v.get("reasons", []), "personal_data": bool(v.get("personal_data"))}
                     for v in votes],
           "enforcement": [e for v in votes for e in v.get("enforcement") or []]}
    (kdir / "committee" / f"{did}.yaml").write_text(
        "# 知見の委員会の議決（ADR 0024）。反対の票も消さずに残す。core-gates/committee_review.py が書く\n"
        + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return did


def check(repo: Path, today: date) -> tuple[bool, str]:
    kc = [sys.executable, str(HERE / "knowledge_check.py"), "--repo", str(repo), "--today", today.isoformat()]
    subprocess.run([*kc, "--index"], check=False, capture_output=True, text=True)
    r = subprocess.run(kc, check=False, capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr)[-2000:]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-dir", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--pr-url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--today")
    a = ap.parse_args(argv)
    repo, today = Path(a.repo_dir), (date.fromisoformat(a.today) if a.today else datetime.now(timezone(timedelta(hours=9))).date())   # JST
    defs = yaml.safe_load(KNOWLEDGE_DEFS.read_text(encoding="utf-8"))
    ok, log = check(repo, today)
    if not ok:   # 決定論の検査が緑になってから持ち込む（ADR 0024）。CI と同じ検査なので、ここで落ちるのは CI の外の変化
        Path(a.out).write_text(json.dumps({"outcome": "sendback", "votes": [], "decision": None, "reason": "knowledge_check", "check": log},
                                          ensure_ascii=False), encoding="utf-8")
        return 0
    r = review(repo, a.base, defs)
    outcome = aggregate(r["votes"], list(defs["committee"]["roles"]))
    decision = None
    if outcome != "sendback":
        decision = write_decision(repo, a.pr_url, outcome, r["votes"], r["added"], today)
        ok, log = check(repo, today)
        if not ok:
            print(log, file=sys.stderr)
            return 1
    enforce = [e for v in r["votes"] for e in v.get("enforcement") or []] if outcome == "enforce" else []
    Path(a.out).write_text(json.dumps({"outcome": outcome, "decision": decision, "votes": r["votes"], "enforce": enforce,
                                       "added": r["added"], "modified": r["modified"], "cost_usd": r["cost_usd"], "usage": r["usage"],
                                       "model": r["model"]}, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
