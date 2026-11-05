#!/usr/bin/env python3
"""エージェントに渡す知見を選んで、作業の文脈のファイルにする（ADR 0024 の段取り 6、決定 7）。LLM を使わない。

選ぶ決まり（definitions/knowledge.yaml の context_for_agents）:
  - status が active で、confidence が confirmed か settled（仮説は渡さない）
  - settled で決定論の担保（enforcement.level: deterministic）が効いているものは渡さない（担保で防げるので文脈を減らす）
  - review_by を過ぎたものは渡さない（再確認されるまで。日次の棚卸しが拾う）
  - scope.roles が空か、渡す役割を含む（implement は implementer とも書かれる）
  - 機密区分は product と public だけ（client は渡さない）。restricted の中身はここに来ない（git には id しかない）
  - 確認日の新しい順に、件数と文字数の上限まで。上限で落としたものは結果の skipped に残す

渡すときは確かさと確認日を付ける。渡した id は結果の ids に出し、ワーカーが FACTORY_RESULT に載せ、dispatcher が events の
knowledge.used に残す（数えるのは測る側）。

使い方:
  core-gates/knowledge_context.py --repo . --role implement --out /work/context/knowledge.md [--today YYYY-MM-DD]
  標準出力に {"ids": [...], "skipped": {...}} の JSON を 1 行。渡すものがなければ --out は作らない。終了コードは常に 0（文脈が無くても作業は進める）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import knowledge_check as kc  # noqa: E402

MAX_ITEMS = 12          # 仮置き。文脈を太らせない
MAX_CHARS = 12000
ROLE_ALIASES = {"implement": {"implement", "implementer"}}
CONFIDENCE_JA = {"confirmed": "確認済み", "settled": "確定"}
PASSED_CONFIDENTIALITY = {"product", "public"}


def eligible(k: dict, role: str, today: date) -> str | None:
    """渡すなら None、渡さないならその理由"""
    if k.get("status") != "active":
        return "not_active"
    conf = k.get("confidence")
    if conf not in CONFIDENCE_JA:
        return "hypothesis"
    if conf == "settled" and ((k.get("enforcement") or {}).get("level") == "deterministic"):
        return "enforced"
    rb = kc.as_date(k.get("review_by")) if k.get("review_by") else None
    if rb and rb < today:
        return "expired"
    if k.get("confidentiality") not in PASSED_CONFIDENTIALITY:
        return "confidential"
    roles = set(((k.get("scope") or {}).get("roles")) or [])
    if roles and not (roles & ROLE_ALIASES.get(role, {role})):
        return "other_role"
    return None


def render(k: dict) -> str:
    scope = k.get("scope") or {}
    lines = [f"## {k['id']}（{CONFIDENCE_JA[k['confidence']]}、確認日 {k.get('verified_at')}）", "",
             " ".join(str(k.get("statement") or "").split())]
    where = []
    if scope.get("paths"):
        where.append("パス " + ", ".join(map(str, scope["paths"])))
    if scope.get("contexts"):
        where.append("コンテキスト " + ", ".join(map(str, scope["contexts"])))
    if where:
        lines += ["", "- 効く範囲: " + "、".join(where)]
    level = (k.get("enforcement") or {}).get("level")
    if level in ("checklist", "deterministic"):
        lines.append(f"- 担保: {level}（{'検査の観点にもなっている' if level == 'checklist' else '機械の検査もある'}）")
    return "\n".join(lines) + "\n"


HEADER = """# この製品の知見

工場の知見の委員会が採用した、この製品についての知見（`.factory-config/knowledge/`）のうち、確認済み以上で今の作業の役割に関係するものです。
確かさと確認日を見て使ってください。目の前のコードや specs/ と食い違うときは目の前の事実を優先し、食い違いを最後の出力の「## 補足」に知見の id を添えて 1 行で書いてください。
Issue と「約束」がこのファイルより優先します。

"""


def build(repo: Path, role: str, today: date) -> tuple[str, list[str], dict]:
    items, _, _, errors = kc.load(repo)
    skipped: dict[str, list[str]] = {}
    picked = []
    for k in items:
        if not isinstance(k, dict) or not k.get("id"):
            continue
        why = eligible(k, role, today)
        if why:
            skipped.setdefault(why, []).append(k["id"])
        else:
            picked.append(k)
    picked.sort(key=lambda k: (str(k.get("verified_at") or ""), k["id"]), reverse=True)
    text, ids = HEADER, []
    for k in picked:
        block = render(k)
        if len(ids) >= MAX_ITEMS or len(text) + len(block) + 1 > MAX_CHARS:
            skipped.setdefault("over_limit", []).append(k["id"])
            continue
        text += block + "\n"
        ids.append(k["id"])
    if errors:
        skipped["unreadable"] = errors[:5]
    return text, ids, skipped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--role", default="implement")
    ap.add_argument("--out", required=True)
    ap.add_argument("--today")
    a = ap.parse_args(argv)
    today = date.fromisoformat(a.today) if a.today else date.today()
    try:
        text, ids, skipped = build(Path(a.repo), a.role, today)
    except Exception as e:  # noqa: BLE001 文脈が作れなくても作業は止めない
        print(json.dumps({"ids": [], "error": repr(e)[:300]}, ensure_ascii=False))
        return 0
    if ids:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
    print(json.dumps({"ids": ids, "skipped": skipped}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
