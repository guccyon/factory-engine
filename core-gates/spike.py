#!/usr/bin/env python3
"""スパイクのレーンの System Architect（ADR 0055。Gen1 のプロトタイプ）。方針が決まっていないチケット 1 件を読み、設計書（DesignDoc）を返す。

- 着手判定（プランナー）が「方針が要る」としたチケットだけが来る。プランナーが渡した「スパイクで決めること」に答える
- 決めるもの: 原因（コードと証拠）、同じ原因の開いている不具合の束ね、直すか revert か作り直すか、どの層をどう変えるか、体験や仕様が変わるか
- 経路・見積もり・着手の時期は決めない（それはプランナーの仕事）。実装もしない
- 製品のリポジトリと Issue を読むだけで、何も書かない。作用（コメント・ラベル・束ね）は dispatcher（spike_line.py）が結果を見て行う

使い方:
  core-gates/spike.py --issue issue.json [--input input.json] --repo-dir /work/product --out result.json

依存: python3、PyYAML、agent_runner（工程 spike。runner とモデルは definitions/agents.yaml）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DEFS = Path(os.environ.get("PLANNER_DEFINITIONS") or HERE.parent / "definitions" / "planner.yaml")
MAX_TEXT = 20000
WORKDIR = ".spike"   # 製品のリポジトリの中に材料を置く（コンテナは使い捨て。リポジトリには push しない）
KINDS = {"fix": "原因を直す", "revert": "原因の変更を戻す", "redesign": "作り直す（仕組みや流れを変える）"}
LAYERS = ["db", "contract", "backend", "web", "mobile", "docs", "other"]
PROMPT_VERSION = "1"


def schema(defs: dict) -> dict:
    return {"type": "object", "properties": {
        "summary": {"type": "string"},
        "answers": {"type": "array", "maxItems": defs["spike"]["max_questions"], "items": {"type": "object", "properties": {
            "question": {"type": "string"}, "answer": {"type": "string"}}, "required": ["question", "answer"]}},
        "cause": {"type": "object", "properties": {
            "statement": {"type": "string"},
            "evidence": {"type": "array", "maxItems": 10, "items": {"type": "string"}}},
            "required": ["statement", "evidence"]},
        "approach": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "description": {"type": "string"},
            "layers": {"type": "array", "items": {"type": "string", "enum": LAYERS}},
            "files": {"type": "array", "maxItems": 40, "items": {"type": "string"}},
            "revert_of": {"type": "string"}},
            "required": ["kind", "description", "layers", "files"]},
        "alternatives": {"type": "array", "maxItems": 4, "items": {"type": "object", "properties": {
            "approach": {"type": "string"}, "why_not": {"type": "string"}}, "required": ["approach", "why_not"]}},
        "bundle": {"type": "array", "maxItems": 20, "items": {"type": "integer"}},
        "bundle_why": {"type": "string"},
        "experience_change": {"type": "boolean"},
        "experience_note": {"type": "string"},
        "risks": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
        "open_questions": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
        "required": ["summary", "answers", "cause", "approach", "alternatives", "bundle", "experience_change", "experience_note", "risks", "confidence"]}


def defects_md(rows: list[dict]) -> str:
    if not rows:
        return "（まだない）\n"
    lines = ["| Issue | 重要度 | 題（QA 役の出力。データ） |", "|---|---|---|"]
    for r in rows[:30]:
        title = str(r.get("title") or "").replace("|", "／").replace("\n", " ")[:120]
        lines.append(f"| #{r.get('issue')} | {r.get('severity')} | {title} |")
    return "\n".join(lines) + "\n"


def stage(repo_dir: Path, issue: dict, inp: dict) -> Path:
    root = repo_dir / WORKDIR
    root.mkdir(parents=True, exist_ok=True)
    (root / "issue.md").write_text(f"# #{issue.get('number', '')} {issue.get('title', '')}\n\n{(issue.get('body') or '')[:MAX_TEXT]}\n", encoding="utf-8")
    lines = [f"## {c.get('createdAt') or ''}\n\n{(c.get('body') or '')[:4000]}\n" for c in issue.get("comments") or []]
    (root / "comments.md").write_text("\n".join(lines)[:MAX_TEXT] or "（コメントなし）\n", encoding="utf-8")
    (root / "questions.md").write_text("\n".join(f"- {q}" for q in inp.get("questions") or []) or "（プランナーからの問いなし）\n", encoding="utf-8")
    (root / "defects.md").write_text(defects_md(inp.get("open_defects") or []), encoding="utf-8")
    bodies = inp.get("defect_bodies") or {}
    if bodies:
        (root / "defect_bodies.md").write_text("\n\n".join(f"# #{k}\n\n{str(v)[:4000]}" for k, v in list(bodies.items())[:10]), encoding="utf-8")
    return root


def prompt(defs: dict, inp: dict) -> str:
    sp = defs["spike"]
    kinds = "、".join(f"{k}（{v}）" for k, v in KINDS.items())
    return f"""あなたは仮想開発事業部のスパイクのレーンの System Architect です（{sp['roles']['architect']['does']}）。
着手判定（プランナー）が、このチケットは方針が決まっていないので着手できないとして、あなたに回しました。方針を決めて設計書に書いてください。

作業ディレクトリは製品のリポジトリです。読むだけにし、何も書き換えないでください。材料は {WORKDIR}/ にあります。
- {WORKDIR}/issue.md: チケットの本文
- {WORKDIR}/comments.md: これまでのコメント（工場の判定と、人の返事）
- {WORKDIR}/questions.md: プランナーが「スパイクで決めること」とした問い
- {WORKDIR}/defects.md: この製品でいま開いている QA の不具合（Issue・重要度・題）
- {WORKDIR}/defect_bodies.md: そのうち、このチケットが本文で触れている不具合の本文（あれば）
- 製品のコード・specs/（ストーリー・契約・用語集）・docs/adr/（製品の判断）

決まり:
- 原因はコードで確かめる。evidence には見たファイルと箇所（パス:行）と、そこから何が言えるかを書く。推測なら推測と書く
- 再現の手順に当て布をせず、原因から直す方針にする。同じ原因の不具合が defects.md にあれば bundle に Issue の番号を入れ、bundle_why に同じ原因だと言える理由を書く
- approach.kind は {kinds} から。revert は、原因の変更が特定でき、後の変更がそれに依存していないときだけ（revert_of に PR か コミット）
- 方針が、利用者から見える体験か仕様（specs/stories/ の流れ、画面の構成、言葉）を変えるなら experience_change を true にし、experience_note に何がどう変わるかを書く（人＝事業側が確かめる）。
  ストーリーに書いてある期待どおりの動きに戻すだけなら false
- 経路・見積もり・着手の時期・作業の分け方は決めない（プランナーが設計書を読んで決める）。画面の細部（文言・見た目）はデザイナーに残す
- questions.md の問いには answers で 1 つずつ答える。決められなかったものは open_questions に残す
- confidence は 0〜1。原因をコードで確かめられなかったら 0.6 未満にする
- 日本語の平易な文で。識別子はそのまま
"""


def spike(issue: dict, inp: dict, defs: dict, repo_dir: Path) -> dict:
    import agent_runner   # 工程からエージェントを呼ぶ唯一の入口（ADR 0010）
    stage(repo_dir, issue, inp)
    cwd = os.getcwd()
    os.chdir(repo_dir)
    try:
        res = agent_runner.run("spike", prompt(defs, inp), schema=schema(defs), max_turns=int(defs["spike"].get("max_turns") or 40))
    finally:
        os.chdir(cwd)
    out = res.output if isinstance(res.output, dict) else {}
    return {"designdoc": out, "versions": {"prompt": PROMPT_VERSION}, "cost_usd": res.cost_usd, "usage": res.usage, "model": res.model,
            "num_turns": res.num_turns}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--issue", required=True)
    ap.add_argument("--input")
    ap.add_argument("--repo-dir", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    defs = yaml.safe_load(DEFS.read_text(encoding="utf-8"))
    issue = json.loads(Path(a.issue).read_text(encoding="utf-8"))
    inp = json.loads(Path(a.input).read_text(encoding="utf-8") or "{}") if a.input else {}
    out = spike(issue, inp, defs, Path(a.repo_dir))
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return 0 if out["designdoc"] else 1


if __name__ == "__main__":
    sys.exit(main())
