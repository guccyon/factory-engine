#!/usr/bin/env python3
"""振り返り（ADR 0061）。工場が消化したチケット N 件（スプリント）を、関わったロールの委員と事業部のリーダーで振り返り、普遍的な知見を抽出する。

1. ロールの委員（工程 retro.role）: 議題のチケットに関わったロールごとに 1 回ずつ呼ぶ。そのロールの立場から、チケットの番号を付けて意見を出す
2. 事業部のリーダー（工程 retro.lead）: 委員の意見を突き合わせ、知見の候補（根拠のチケット・どの立場から来たか・置き場）を抽出し、
   採らなかった候補と理由、打ち手（Engine・プロセスの提案）、置き場の棚卸し（週 1 の定例から移したもの）を出す

- 材料（議題・知見の置き場・specs/・docs/）は一時ディレクトリに写してから読ませる。製品のチェックアウトには触らせない
- 委員会は実装しない。知見の変更セット・製品の docs/ の追記・打ち手は、dispatcher が結果を見て Issue にし、工場のラインで作る。
  知見の変更セットの PR は、知見のレビュー（ADR 0024 の 4 役）にかかる。振り返りの委員はレビューに入らない

使い方:
  core-gates/retro.py --repo-dir <製品の既定ブランチ> --input retro.json --out result.json

依存: python3、PyYAML、agent_runner（工程 retro.role・retro.lead。runner とモデルは definitions/agents.yaml）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import committee_weekly as cw   # 置き場の棚卸しの決定論（facts・unused）は週 1 の定例と同じもの
import knowledge_check as kc
import yaml

HERE = Path(__file__).resolve().parent
KNOWLEDGE_DEFS = Path(os.environ.get("KNOWLEDGE_DEFINITIONS") or HERE.parent / "definitions" / "knowledge.yaml")
ROLE_TURNS = 30
LEAD_TURNS = 40
MAX_OPINIONS = 8
MAX_KNOWLEDGE = 6


# ------------------------------------------------------------------ 形
def role_schema() -> dict:
    return {"type": "object", "properties": {"opinions": {"type": "array", "maxItems": MAX_OPINIONS, "items": {"type": "object", "properties": {
        "perspective": {"type": "string"},
        "tickets": {"type": "array", "items": {"type": "string"}},
        "kind": {"type": "string", "enum": ["problem", "good", "insight"]},
        "observation": {"type": "string"},
        "proposal": {"type": "string"}},
        "required": ["perspective", "tickets", "kind", "observation", "proposal"]}}},
        "required": ["opinions"]}


def lead_schema(defs: dict) -> dict:
    kinds = list(defs["knowledge"]["kinds"])
    themes = list(defs["knowledge"]["themes"])
    ids = {"type": "array", "items": {"type": "string"}}
    review_item = {"type": "object", "properties": {"ids": ids, "why": {"type": "string"}, "proposal": {"type": "string"}}, "required": ["ids", "why", "proposal"]}
    return {"type": "object", "properties": {
        "summary": {"type": "string"},
        "knowledge": {"type": "array", "maxItems": MAX_KNOWLEDGE, "items": {"type": "object", "properties": {
            "statement": {"type": "string"},
            "kind": {"type": "string", "enum": kinds},
            "theme": {"type": "integer", "enum": themes},
            "perspective": {"type": "string"},
            "evidence_tickets": ids,
            "from_roles": ids,
            "destination": {"type": "string", "enum": ["knowledge", "product_docs"]},
            "acts_on": {"type": "string", "enum": list(defs["knowledge"]["acts_on"])},
            "work": {"type": "string"},
            "affects": ids,
            "why_universal": {"type": "string"}},
            "required": ["statement", "kind", "theme", "perspective", "evidence_tickets", "from_roles", "destination", "acts_on", "work", "affects", "why_universal"]}},
        "rejected": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
            "statement": {"type": "string"}, "from_roles": ids, "why": {"type": "string"}}, "required": ["statement", "from_roles", "why"]}},
        "actions": {"type": "array", "maxItems": 4, "items": {"type": "object", "properties": {
            "what": {"type": "string"}, "target": {"type": "string", "enum": ["engine", "process", "product"]},
            "why": {"type": "string"}, "tickets": ids}, "required": ["what", "target", "why", "tickets"]}},
        "previous_effect": {"type": "string"},
        "inventory": {"type": "object", "properties": {
            "contradictions": {"type": "array", "maxItems": 5, "items": review_item},
            "promote": {"type": "array", "maxItems": 5, "items": review_item},
            "retire": {"type": "array", "maxItems": 5, "items": review_item},
            "widen": {"type": "array", "maxItems": 5, "items": review_item},
            "structure": {"type": "object", "properties": {"needed": {"type": "boolean"}, "proposal": {"type": "string"}}, "required": ["needed", "proposal"]}},
            "required": ["contradictions", "promote", "retire", "widen", "structure"]},
        "docs_structure": {"type": "object", "properties": {"needed": {"type": "boolean"}, "proposal": {"type": "string"}}, "required": ["needed", "proposal"]}},
        "required": ["summary", "knowledge", "rejected", "actions", "previous_effect", "inventory", "docs_structure"]}


# ------------------------------------------------------------------ 指示
def _prompts(inp: dict) -> str:
    return "\n".join(f"- {v.get('ja')}: {v.get('question')}" for v in (inp.get("prompts") or {}).values())


COMMON = """作業ディレクトリに材料があります。読むだけにし、何も書き換えないでください。
- retro.json: 振り返りの議題。tickets（対象のチケットごとのジョブ・出来事の流れ・事例の分析・費用・差し戻し・停止）、roles（委員のロールと関わったチケット）、
  operations（期間の製品の障害）、incidents（事故調査委員会の記録と兆し）、previous（前回の振り返りの知見と打ち手）、inventory（知見の効き目の信号）
- knowledge/: 事業部の知見の置き場（K-*.yaml、cases/、committee/、INDEX.md）
- specs/: 製品の規範。docs/: 製品のドメインの知識（あれば）

決まり:
- 日本語の平易な文で。個人情報（利用者・人の名前、メール、電話、住所など）は書かない。抽象化して書く（「予約の確定待ちの生徒」）
- チケットは retro.json の ref（owner/repo#番号）で指す。材料にないことを推測で書かない
"""


def role_prompt(role: str, tickets: list[str], inp: dict) -> str:
    return f"""あなたは開発事業部の振り返り（スプリントのレトロスペクティブ）の委員で、立場は「{role}」です。
工場が消化したチケット {len(inp.get('tickets') or [])} 件（スプリント {inp.get('sprint')}）を振り返ります。あなたが関わったのは {', '.join(tickets)} です。

{COMMON}
{role}の立場から、このスプリントで起きたことについて意見を出してください。まず自分が関わったチケットの記録を読み、ほかのチケットも必要なら読みます。

取っ掛かりの例（観点はこれに限りません。材料から別の観点を立ててかまいません）:
{_prompts(inp)}

出すもの（opinions、{MAX_OPINIONS} 件まで）:
- perspective: どの観点の意見か（上の例の名前か、自分で立てた観点の名前）
- tickets: 根拠のチケット（1 件以上）
- kind: problem（問題）・good（うまくいった、再現したいやり方）・insight（ドメインや利用者についての気づき）
- observation: 何が起きたか、なぜか（{role}の立場から見えたこと）
- proposal: 次からどうすればよいか（あなたのロールだけでなく、ほかのロールや工程への提案でもよい）

複数のチケットに共通することを優先してください。1 件だけのことも、痛みが大きければ出してかまいません。
"""


def lead_prompt(inp: dict, opinions: list[dict], f: dict) -> str:
    dest = inp.get("domain_destinations") or {}
    return f"""あなたは開発事業部のリーダーで、振り返り（スプリントのレトロスペクティブ）の議長です。
工場が消化したチケット {len(inp.get('tickets') or [])} 件（スプリント {inp.get('sprint')}）について、関わったロールの委員が意見を出しました（opinions.json）。

{COMMON}- opinions.json: 委員の意見（role がどの立場からの意見か）

意見を突き合わせ、事業部の誰もが使える普遍的な知見を抽出してください。委員の意見をそのまま写すのではなく、複数の立場・複数のチケットに共通する原因ややり方を言葉にします。

出すもの:
- summary: このスプリントのまとめ（3〜5 文）。何がうまくいき、何が詰まったか。立場ごとの見え方の違い
- knowledge（{MAX_KNOWLEDGE} 件まで）: 知見の候補
  - statement: 1 件 1 主張。誰が読んでも同じ行動になる書き方
  - kind・theme: 知見の種類と学習のテーマ（knowledge/README.md・INDEX.md の分け方に合わせる）
  - perspective: どの観点から出たか。from_roles: どの立場の意見から来たか。evidence_tickets: 根拠のチケット
  - destination: ドメインの気づきは 2 種類に分ける。product_docs={dest.get('product_docs', '')}。knowledge={dest.get('knowledge', '')}。
    迷ったら「工場をやめても製品に残すべき知識か」で決める。ドメイン以外の知見（工程・品質・言葉など）は knowledge
  - acts_on: context（作業の文脈に渡す）・engine（Engine の直しが要る）・process（役割への指示・ラインの設定の変更）
  - work: 効かせたい業務（広さの見当。例: 「実装」「計画」「事業部の開発すべて」）
  - affects: 影響を受ける既存の知見の id（直す・置き換える・廃止する候補）。なければ空
  - why_universal: なぜ一般に言えるか（何件のチケット・どの立場で起きたか）
- rejected: 採らなかった意見と理由（1 件だけの局所的な話、根拠が薄い、既存の知見と同じ、など）
- actions（4 件まで）: 知見にするのではなく、直す仕事として起こすもの。target は engine（工場のコード・設定）・process（役割への指示・ラインの設定。人の承認）・product（製品の改善）
- previous_effect: 前回の振り返りの知見と打ち手（previous）が効いたか。同じ問題が再び起きたか。前回がなければ空
- inventory: 置き場の棚卸し（決定論の数は下）。contradictions（矛盾・重複）・promote（独立した事例やチケット 2 件以上で再現した hypothesis）・
  retire（根拠が崩れた・使われない）・widen（届かなかった知見の効く範囲の広げ方）・structure（置き場の構造を変えるべきか。量が少ないうちは変えない）
- docs_structure: 製品の docs/ のドメインの知識の構造を整理し直すべきか（溜まってきたら。少ないうちは変えない）

決定論で出したこと: 知見 {f['items']} 件、期限切れ {len(f['stale'])} 件、{cw.UNUSED_DAYS} 日使われない知見 {len(f.get('unused') or [])} 件、
置き場の直下のファイル {f['files_in_root']} 件{'（構造を見直す目安を超えた）' if f['structure_signal'] else ''}

決まり: 知見の id は既存のものだけ書く（新しい id は作らない。番号は書くラインが振る）。普遍的に言えない知見は無理に作らず、rejected に回す。知見が 0 件でもかまわない。
"""


# ------------------------------------------------------------------ 実行
def materials(repo: Path, root: Path, inp: dict) -> None:
    shutil.copytree(repo / kc.ROOT, root / "knowledge")
    for d in ("specs", "docs"):
        if (repo / d).is_dir():
            shutil.copytree(repo / d, root / d, ignore=shutil.ignore_patterns("*.png", "*.jpg", "*.jpeg", "*.gif", "*.pdf", "*.mp4", "*.mov"))
    (root / "retro.json").write_text(json.dumps(inp, ensure_ascii=False, indent=1), encoding="utf-8")


def run(repo: Path, inp: dict, f: dict, defs: dict, runner=None) -> dict:
    if runner is None:
        import agent_runner  # 工程からエージェントを呼ぶ唯一の入口（ADR 0010）
        runner = agent_runner.run
    cost, usage, models, opinions, failed = 0.0, {}, set(), [], []
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        materials(repo, root, inp)
        cwd = os.getcwd()
        os.chdir(root)   # agent_runner はカレントディレクトリでエージェントを動かす
        try:
            for r in inp.get("roles") or []:
                try:
                    res = runner("retro.role", role_prompt(r["role"], r.get("tickets") or [], inp), schema=role_schema(), max_turns=ROLE_TURNS)
                except Exception as e:  # noqa: BLE001  1 つのロールが答えなくても振り返りは続ける（欠けたロールは議事録に残す）
                    failed.append({"role": r["role"], "error": str(e)[:200]})
                    continue
                cost += res.cost_usd or 0
                models.add(res.model)
                out = res.output if isinstance(res.output, dict) else {}
                opinions += [{**o, "role": r["role"]} for o in out.get("opinions") or []]
            (root / "opinions.json").write_text(json.dumps(opinions, ensure_ascii=False, indent=1), encoding="utf-8")
            res = runner("retro.lead", lead_prompt(inp, opinions, f), schema=lead_schema(defs), max_turns=LEAD_TURNS)
            cost += res.cost_usd or 0
            models.add(res.model)
            usage = res.usage or {}
            lead = res.output if isinstance(res.output, dict) else {}
        finally:
            os.chdir(cwd)
    return {"opinions": opinions, "failed_roles": failed, "lead": lead, "cost_usd": round(cost, 4), "usage": usage,
            "model": ",".join(sorted(m for m in models if m))}


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
        print("[retro] no knowledge root", file=sys.stderr)
        return 2
    f = cw.facts(repo, today, int((defs["committee"].get("weekly") or {}).get("structure_review_over", 150)))
    f["unused"] = cw.unused(kc.load(repo)[0], (inp.get("inventory") or {}).get("used") or {}, today)
    r = run(repo, inp, f, defs)
    Path(a.out).write_text(json.dumps({"facts": f, **r}, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
