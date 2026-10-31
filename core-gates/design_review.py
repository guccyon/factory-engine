#!/usr/bin/env python3
"""週 1 回の設計の診断（ADR 0022）。製品のチェックアウトをエージェントに読ませ、definitions/design_review.yaml の観点で所見を返させる。

数値の診断（health/quality/snapshot.py）に出ない、境界・用語・不変条件の置き場所を見る。読むだけで、何も書かない。
結果は人が読むためのもの。ワーカーのプロンプトには入れない。

使い方:
  core-gates/design_review.py --repo-dir /work/product --repo guccyon/rally --out design.json

依存: python3、PyYAML、agent_runner（工程 design_review。runner とモデルは definitions/agents.yaml）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

import agent_runner   # 工程からエージェントを呼ぶ唯一の入口（ADR 0010）

HERE = Path(__file__).resolve().parent
DEFINITIONS = Path(os.environ.get("DESIGN_REVIEW_DEFINITIONS") or HERE.parent / "definitions" / "design_review.yaml")

SEVERITIES = ["high", "mid", "low"]


def schema(defs: dict) -> dict:
    views = list(defs["viewpoints"])
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "strengths": {"type": "array", "maxItems": defs["output"]["max_strengths"], "items": {"type": "string"}},
            "findings": {"type": "array", "maxItems": defs["output"]["max_findings"], "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"}, "viewpoint": {"type": "string", "enum": views},
                    "severity": {"type": "string", "enum": SEVERITIES}, "body": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}}, "fix": {"type": "string"}},
                "required": ["title", "viewpoint", "severity", "body", "evidence", "fix"]}},
            "checked_viewpoints": {"type": "array", "items": {"type": "string", "enum": views}},
        },
        "required": ["summary", "strengths", "findings", "checked_viewpoints"],
    }


def prompt(defs: dict, repo: str, previous: list[dict] | None = None) -> str:
    views = "\n".join(f"- {k}（{v['ja']}）: {v['question']}" for k, v in defs["viewpoints"].items())
    sev = "\n".join(f"- {k}: {v}" for k, v in defs["severity"].items())
    prev = ""
    if previous:
        prev = "\n\n前回の所見（直っていれば挙げない。残っていれば同じ題で挙げ直す）:\n" + "\n".join(f"- [{f.get('severity')}] {f.get('title')}" for f in previous)
    return f"""あなたは {repo} の設計の診断役です。作業ディレクトリは {repo} の main のチェックアウトです。ファイルを読むだけにし、何も書き換えないでください。

まず specs/product-context.md、specs/glossary.yaml、specs/context-map.yaml、docs/adr/ を読み、決まりごとを押さえてから、コードを読んでください。
次の観点で、ドメインモデリングと設計の質を見ます:
{views}

重さ:
{sev}

書き方:
- 所見は重いものから最大 {defs['output']['max_findings']} 件。1 件ごとに、何が起きているか（body）、根拠のファイルのパス（evidence。リポジトリのルートからの相対パス）、直し方（fix）
- よくできているところ（strengths）も最大 {defs['output']['max_strengths']} 件。具体的な型やファイルの名前を挙げる
- summary は 2〜3 文で、全体の評価と、いちばん直す価値のあるところ
- 日本語の平易な文で書く。コードの名前はそのまま書く
- 推測で断定しない。読んで確かめたことだけを書く{prev}
"""


def run(repo_dir: str, repo: str, defs: dict, previous: list[dict] | None = None) -> dict:
    cwd = os.getcwd()
    os.chdir(repo_dir)   # agent_runner はカレントディレクトリでエージェントを動かす
    try:
        res = agent_runner.run("design_review", prompt(defs, repo, previous), schema=schema(defs), max_turns=defs["schedule"]["max_turns"])
    finally:
        os.chdir(cwd)
    out = res.output if isinstance(res.output, dict) else {}
    sha = subprocess.run(["git", "-C", repo_dir, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return {"repo": repo, "sha": sha, "result": out, "model": res.model, "cost_usd": res.cost_usd, "usage": res.usage, "num_turns": res.num_turns}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-dir", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--previous", help="前回の所見のファイル（JSON の配列か {\"previous\": [...]}）")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    defs = yaml.safe_load(DEFINITIONS.read_text(encoding="utf-8"))
    prev = None
    if a.previous and Path(a.previous).exists() and Path(a.previous).stat().st_size:
        data = json.loads(Path(a.previous).read_text(encoding="utf-8"))
        prev = data.get("previous") if isinstance(data, dict) else data   # dispatcher は job.enqueued の input（{"previous": [...]}）を渡す
    report = run(a.repo_dir, a.repo, defs, prev)
    Path(a.out).write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    if not report["result"].get("summary"):
        print("[design_review] empty result", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
