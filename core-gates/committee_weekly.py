#!/usr/bin/env python3
"""知見の委員会の週 1 の定例（ADR 0024）。製品の既定ブランチの知見の置き場を通して見直し、その週の議決をまとめる。

- 決定論で出すもの: 期限切れ（要再確認）の知見、状態・確かさ・テーマごとの数、置き場のファイル数（構造の見直しの合図）
- エージェント（委員会の 4 役の観点をまとめて持つ議長役）が出すもの: 矛盾・重複の候補、確かさを上げる候補（独立した事例 2 件以上）、
  廃止の候補、決定論で担保する候補、置き場の構造を変えるべきか
- 材料（知見の置き場・specs/・その週の記録）は一時ディレクトリに写してから読ませる。製品のチェックアウトには触らせない
- 委員会は実装しない。結果は dispatcher が固定 Issue に書き、見直しと担保と構造の変更を Issue にして工場のラインで作る

使い方:
  core-gates/committee_weekly.py --repo-dir <製品の既定ブランチ> --input week.json --out result.json

依存: python3、PyYAML、agent_runner（工程 committee_weekly。runner とモデルは definitions/agents.yaml）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import knowledge_check as kc
import yaml

HERE = Path(__file__).resolve().parent
KNOWLEDGE_DEFS = Path(os.environ.get("KNOWLEDGE_DEFINITIONS") or HERE.parent / "definitions" / "knowledge.yaml")
MAX_TURNS = 40


def facts(repo: Path, today: date, structure_over: int) -> dict:
    """決定論で出すもの"""
    items, cases, decisions, _ = kc.load(repo)
    n_files = len(list((repo / kc.ROOT).glob("K-*.yaml")))
    return {"items": len(items), "cases": len(cases), "decisions": len(decisions), "files_in_root": n_files,
            "structure_signal": n_files > structure_over,
            "by_status": dict(Counter(str(k.get("status")) for k in items)),
            "by_confidence": dict(Counter(str(k.get("confidence")) for k in items if k.get("status") == "active")),
            "by_theme": dict(Counter(str(k.get("theme")) for k in items if k.get("status") == "active")),
            "stale": kc.inventory(items, today)}


def schema(defs: dict) -> dict:
    ids = {"type": "array", "items": {"type": "string"}}
    item = {"type": "object", "properties": {"ids": ids, "why": {"type": "string"}, "proposal": {"type": "string"}}, "required": ["ids", "why", "proposal"]}
    return {"type": "object", "properties": {
        "summary": {"type": "string"},
        "contradictions": {"type": "array", "maxItems": 5, "items": item},
        "promote": {"type": "array", "maxItems": 5, "items": item},
        "retire": {"type": "array", "maxItems": 5, "items": item},
        "enforce": {"type": "array", "maxItems": 3, "items": {"type": "object", "properties": {
            "ids": ids, "why": {"type": "string"}, "proposal": {"type": "string"},
            "solution_kind": {"type": "string", "enum": defs["knowledge"]["enforcement"]["solution_kinds"]}},
            "required": ["ids", "why", "proposal", "solution_kind"]}},
        "structure": {"type": "object", "properties": {"needed": {"type": "boolean"}, "proposal": {"type": "string"}}, "required": ["needed", "proposal"]}},
        "required": ["summary", "contradictions", "promote", "retire", "enforce", "structure"]}


def prompt(defs: dict, f: dict) -> str:
    roles = "\n".join(f"- {v['ja']}: {v['question']}" for v in defs["committee"]["roles"].values())
    return f"""あなたは知見の委員会の週 1 の定例の議長です。4 役の観点をすべて持って、製品の知見の置き場を通して見直します。

4 役の観点:
{roles}

作業ディレクトリに材料があります。読むだけにし、何も書き換えないでください。
- knowledge/: 既定ブランチの知見の置き場（K-*.yaml、cases/、committee/、INDEX.md、README.md）
- specs/: 製品の規範
- week.json: この週の議決（committee.decided）、起票した知見の Issue、担保の Issue、決定論で出した数（facts）

決定論で出したこと（facts の要点）: 知見 {f['items']} 件、事例 {f['cases']} 件、議決 {f['decisions']} 件、期限切れ {len(f['stale'])} 件、置き場の直下の知見のファイル {f['files_in_root']} 件{'（構造を見直す目安を超えた）' if f['structure_signal'] else ''}

出すもの（どれも、無ければ空にする）:
- summary: この週のまとめ（3〜5 文）。議決の傾向、委員会の割れ方、気になること
- contradictions: 互いに矛盾する・重複する知見の組と、直し方（amend・supersede・retire のどれか）
- promote: 独立した事例 2 件以上で再現した hypothesis の知見（confirmed に上げる候補）。根拠の事例を why に書く
- retire: 根拠が崩れた・使われなくなった・規範（specs/）に取り込まれた知見
- enforce: 繰り返し起き、線が引け、外したときに痛い知見で、決定論（solution_kind）で担保すべきもの
- structure: 置き場のディレクトリの分け方を変えるべきか（needed）と案。量が少ないうちは変えない

決まり:
- id はそのまま書く。新しい知見は作らない（作るのはラインの変更セット）
- 日本語の平易な文で。個人情報は書かない
"""


def review(repo: Path, inp: dict, f: dict, defs: dict) -> dict:
    import agent_runner  # 工程からエージェントを呼ぶ唯一の入口（ADR 0010）
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        shutil.copytree(repo / kc.ROOT, root / "knowledge")
        if (repo / "specs").is_dir():
            shutil.copytree(repo / "specs", root / "specs", ignore=shutil.ignore_patterns("*.png", "*.jpg", "*.pdf"))
        (root / "week.json").write_text(json.dumps({**inp, "facts": f}, ensure_ascii=False, indent=1), encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(root)   # agent_runner はカレントディレクトリでエージェントを動かす
        try:
            res = agent_runner.run("committee_weekly", prompt(defs, f), schema=schema(defs), max_turns=MAX_TURNS)
        finally:
            os.chdir(cwd)
    out = res.output if isinstance(res.output, dict) else {}
    return {"result": out, "model": res.model, "cost_usd": res.cost_usd, "usage": res.usage}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-dir", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--today")
    a = ap.parse_args(argv)
    repo = Path(a.repo_dir)
    today = date.fromisoformat(a.today) if a.today else datetime.now(timezone(timedelta(hours=9))).date()   # JST
    defs = yaml.safe_load(KNOWLEDGE_DEFS.read_text(encoding="utf-8"))
    inp = json.loads(Path(a.input).read_text(encoding="utf-8") or "{}")
    if not (repo / kc.ROOT).is_dir():
        print("[committee_weekly] no knowledge root", file=sys.stderr)
        return 2
    f = facts(repo, today, int((defs["committee"].get("weekly") or {}).get("structure_review_over", 150)))
    r = review(repo, inp, f, defs) if f["items"] or inp.get("decisions") else {"result": {"summary": "知見も議決もまだない", "contradictions": [],
                                                                                          "promote": [], "retire": [], "enforce": [],
                                                                                          "structure": {"needed": False, "proposal": ""}},
                                                                               "model": None, "cost_usd": 0, "usage": {}}
    Path(a.out).write_text(json.dumps({"facts": f, **r}, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
