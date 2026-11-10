#!/usr/bin/env python3
"""工場の中での一次調査の決定論の検査（ADR 0048）。LLM を使わない。

  - Issue の本文（Engine の改善 Issue・引き継ぎの要約）: 顧客の情報の形がないこと。
    形は事故記録と同じ（incident_check.DEIDENT）に、電話番号・記録の参照（job:… など）・restricted の id を足したもの。
    --deny で渡した名前（製品・リポジトリ・事業主・依頼者）が出てこないこと。Engine の ADR の番号（ADR 0048）は残してよい
  - 引き継ぎ資料（INV-YYYY-NNNN）: 必須項目、id の形、止めた理由、事実と仮説の形、参照の形、確信度、費用。
    資料の本体は工場の中に置くので製品の名前や記録の参照は書いてよいが、秘密情報の形は書かない

値はここに持つ（正本は definitions/investigate.yaml。食い違いは core-gates/tests/test_investigate_check.py で見る）。

使い方:
  core-gates/investigate_check.py --issue body.md [--deny 名前 ...] [--deny-file names.txt]
  core-gates/investigate_check.py --handoff INV-2026-0001.yaml
  終了コード: 0 = 緑、1 = 通らない、2 = 読めなかった

依存: python3 と PyYAML
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from incident_check import ALLOWED_IPS, DEIDENT  # noqa: E402

STOP_REASONS = ["budget", "needs_code", "needs_host", "sensitive", "asked"]
CONFIDENCE = ["high", "medium", "low"]
HANDOFF_REQUIRED = ["id", "question", "scope", "facts", "hypotheses", "ruled_out", "not_looked_at", "cost", "stop_reason"]
SCOPE_KEYS = ["product", "engine_ref", "window"]
FACT_KEYS = ["ref", "what", "how"]
HYPOTHESIS_KEYS = ["claim", "confidence", "supported_by", "refuted_by", "next_check"]
COST_KEYS = ["usd", "turns"]
HANDOFF_ID = re.compile(r"^INV-\d{4}-\d{4,}$")
REF = re.compile(r"^(job|ticket|event|stoppage|judgment|desk|pr|issue|doc):\S+$")

# Issue に足す形（ADR 0048 の決定 5）
ISSUE_EXTRA = {
    "電話番号": re.compile(r"(?<![\d-])0\d{1,4}-\d{1,4}-\d{3,4}(?![\d-])|\+\d{1,3}[\s-]?\d{1,4}[\s-]?\d{2,4}[\s-]?\d{3,4}"),
    "記録の参照": re.compile(r"\b(?:job|ticket|event|stoppage|judgment|desk):\S+", re.IGNORECASE),
    "restricted の id": re.compile(r"\brestricted:R-\d+|\bR-\d{3,}\b"),
    "番号の参照": re.compile(r"(?<=[\w-])#\d+\b"),   # rally#1031 のように名前に続く番号（DEIDENT は前が語のときを見ない）
}
ADR_REF = re.compile(r"ADR\s?\d{4}")


def _deny_hits(text: str, deny: list[str]) -> list[str]:
    low = text.lower()
    return [d for d in deny if d and d.strip() and d.strip().lower() in low]


def check_issue(text: str, deny: list[str] | None = None) -> list[str]:
    """Engine の改善 Issue・引き継ぎの要約の本文に、顧客の情報の形がないか。見つけた形の名前と、その文字列を返す"""
    errs: list[str] = []
    body = ADR_REF.sub("ADR", text)                 # ADR の番号は Engine の文書なので残してよい
    for label, pat in {**DEIDENT, **ISSUE_EXTRA}.items():
        for m in pat.finditer(body):
            s = m.group(0)
            if label == "IP アドレス" and s in ALLOWED_IPS:
                continue
            errs.append(f"{label}: {s}")
    for d in _deny_hits(text, deny or []):
        errs.append(f"製品・人の名前: {d}")
    return errs


def _secrets(v) -> list[str]:
    text = json.dumps(v, ensure_ascii=False, default=str)
    return [m.group(0)[:12] + "…" for m in DEIDENT["秘密情報"].finditer(text)]


def check_handoff(rec) -> list[str]:
    """引き継ぎ資料の形。本体は工場の中に置くので、製品の名前や記録の参照は書いてよい（秘密情報は書かない）"""
    if not isinstance(rec, dict):
        return ["中身が mapping ではない"]
    errs: list[str] = []
    for k in HANDOFF_REQUIRED:
        if k not in rec or rec[k] in (None, ""):
            errs.append(f"{k} がない")
    if rec.get("id") is not None and not HANDOFF_ID.match(str(rec.get("id"))):
        errs.append(f"id {rec.get('id')!r} は INV-YYYY-NNNN の形")
    if rec.get("stop_reason") is not None and rec.get("stop_reason") not in STOP_REASONS:
        errs.append(f"stop_reason {rec.get('stop_reason')!r} は {STOP_REASONS} のどれか")
    scope = rec.get("scope")
    if scope is not None:
        if not isinstance(scope, dict):
            errs.append("scope は mapping")
        else:
            for k in SCOPE_KEYS:
                if not scope.get(k):
                    errs.append(f"scope.{k} がない")
    facts = rec.get("facts")
    if facts is not None:
        if not isinstance(facts, list) or not facts:
            errs.append("facts は 1 件以上の list（見た事実がなければ引き継げない）")
        else:
            for i, f in enumerate(facts):
                errs += _keyed(f, FACT_KEYS, f"facts[{i}]")
                if isinstance(f, dict) and f.get("ref") and not REF.match(str(f["ref"])):
                    errs.append(f"facts[{i}].ref {f['ref']!r} は 種類:id の形（job:… ticket:… など）")
    hyps = rec.get("hypotheses")
    if hyps is not None:
        if not isinstance(hyps, list):
            errs.append("hypotheses は list")
        else:
            for i, h in enumerate(hyps):
                errs += _keyed(h, ["claim", "confidence"], f"hypotheses[{i}]")
                if isinstance(h, dict):
                    for k in h:
                        if k not in HYPOTHESIS_KEYS:
                            errs.append(f"hypotheses[{i}].{k} は使えない（{HYPOTHESIS_KEYS}）")
                    if h.get("confidence") is not None and h.get("confidence") not in CONFIDENCE:
                        errs.append(f"hypotheses[{i}].confidence は {CONFIDENCE} のどれか")
                    for k in ("supported_by", "refuted_by"):
                        for r in h.get(k) or []:
                            if not REF.match(str(r)):
                                errs.append(f"hypotheses[{i}].{k} の {r!r} は 種類:id の形")
    for k in ("ruled_out", "not_looked_at"):
        if rec.get(k) is not None and not isinstance(rec.get(k), list):
            errs.append(f"{k} は list")
    cost = rec.get("cost")
    if cost is not None:
        if not isinstance(cost, dict):
            errs.append("cost は mapping")
        else:
            for k in COST_KEYS:
                if not isinstance(cost.get(k), (int, float)):
                    errs.append(f"cost.{k} は数")
    for s in _secrets(rec):
        errs.append(f"秘密情報の形がある: {s}")
    return errs


def _keyed(v, keys: list[str], where: str) -> list[str]:
    if not isinstance(v, dict):
        return [f"{where} は mapping"]
    return [f"{where}.{k} がない" for k in keys if not v.get(k)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--issue", help="Issue の本文のファイル")
    g.add_argument("--handoff", help="引き継ぎ資料（YAML）")
    ap.add_argument("--deny", nargs="*", default=[], help="出てきてはいけない名前（製品・リポジトリ・人）")
    ap.add_argument("--deny-file", help="出てきてはいけない名前を 1 行 1 つで書いたファイル")
    ap.add_argument("--json", help="結果を JSON で書く先")
    a = ap.parse_args(argv)
    deny = list(a.deny)
    try:
        if a.deny_file:
            deny += [ln.strip() for ln in Path(a.deny_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
        if a.issue:
            errs = check_issue(Path(a.issue).read_text(encoding="utf-8"), deny)
        else:
            errs = check_handoff(yaml.safe_load(Path(a.handoff).read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as e:
        print(f"読めなかった: {e}", file=sys.stderr)
        return 2
    if a.json:
        Path(a.json).write_text(json.dumps({"ok": not errs, "errors": errs}, ensure_ascii=False, indent=2), encoding="utf-8")
    for e in errs:
        print(e)
    print("緑" if not errs else f"通らない（{len(errs)} 件）")
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
