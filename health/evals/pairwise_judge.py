#!/usr/bin/env python3
"""Golden Set のペア比較で判定器を校正する（EvalSpec l4-pairwise、P0 Week 4）。

製品側 .factory-config/evals/golden/*.yaml の各ペア（a, b と建設者の選好）について、判定器（tier-judge）に
両方の差分を見せて「設計として望ましい方」を選ばせ、建設者の選好との一致率（pairwise_accuracy）と
Wilson 95% 区間を出す。N < 100 の暫定指標。ブレーカーには使わない。

使い方:
  health/evals/pairwise_judge.py --golden ~/src/github.com/guccyon/rally/main/.factory-config/evals/golden --out pairwise-2026-10-26.json

依存: python3、gh、claude（builder の Mac の CLI）、ruby（YAML を読むため。PyYAML があればそれを使う）
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TIER_MODEL = {"tier-judge": "claude-opus-5-5", "tier-small": "claude-sonnet-5-5"}
SCHEMA = {"type": "object", "properties": {
    "preferred": {"type": "string", "enum": ["a", "b", "tie"]},
    "confidence": {"type": "number"},
    "reason": {"type": "string"},
    "evidence": {"type": "array", "items": {"type": "string"}}},
    "required": ["preferred", "confidence", "reason", "evidence"]}


def sh(*a: str, retries: int = 3) -> str:
    """claude / gh は一時的に失敗することがある（stderr なしで rc=1 のことも。レート制限とみられる）ので、20・40・80 秒待って繰り返す"""
    import time
    last = None
    for i in range(retries + 1):
        r = subprocess.run(a, capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
        last = r
        if i < retries:
            time.sleep(20 * (2 ** i))
    raise RuntimeError(f"{a[0]} failed (rc={last.returncode}): stderr={last.stderr[-300:]} stdout={last.stdout[-300:]}")


def load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except ImportError:
        return json.loads(sh("ruby", "-ryaml", "-rjson", "-e", 'puts JSON.generate(YAML.load_file(ARGV[0]))', str(path)))


def diff_of(ref: str, max_chars: int = 40000) -> tuple[str, str]:
    """ref は github:<owner>/<repo>#<n>（PR 番号）。閉じた PR でも差分は取れる。"""
    m = re.match(r"github:([^#]+)#(\d+)", ref)
    repo, n = m.group(1), m.group(2)
    title = json.loads(sh("gh", "pr", "view", n, "-R", repo, "--json", "title"))["title"]
    d = sh("gh", "pr", "diff", n, "-R", repo)
    return title, (d[:max_chars] + "\n[... 切り詰め]" if len(d) > max_chars else d)


def judge(pair: dict, tier: str, swap: bool) -> dict:
    a_ref, b_ref = pair["pair"]["a"]["ref"], pair["pair"]["b"]["ref"]
    if swap:   # 位置バイアスを打ち消すため a/b を入れ替えてもう 1 回聞く
        a_ref, b_ref = b_ref, a_ref
    ta, da = diff_of(a_ref)
    tb, db = diff_of(b_ref)
    prompt = f"""あなたは工場の検査官です。同じ Ticket（{pair['ticket']}）に対する 2 つの成果物 A と B を比べ、
設計として望ましい方を選んでください。観点は L4（モデリング品質）: 用語が SSOT（specs/）と整合しているか、
不変条件や事業の制約が正しく反映されているか、境界づけられたコンテキストの割り当てが妥当か、推測で定義を作っていないか。
文体や分量の多さでは選ばないこと。根拠は差分の具体的な箇所を引くこと。判断できないときだけ tie。

# A: {ta}
{da}

# B: {tb}
{db}
"""
    out = sh("claude", "-p", prompt, "--model", TIER_MODEL[tier], "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--max-turns", "3")   # --bare はキーチェーンの認証を読まないので使わない
    res = json.loads(out)
    s = res.get("structured_output") or res.get("result")
    if isinstance(s, str):
        s = json.loads(s)
    pref = s["preferred"]
    if swap and pref in ("a", "b"):
        pref = "b" if pref == "a" else "a"
    return {"preferred": pref, "confidence": s.get("confidence"), "reason": s["reason"], "evidence": s.get("evidence", []),
            "swapped": swap, "cost_usd": res.get("total_cost_usd")}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0, c - h), 3), round(min(1, c + h), 3))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tier", default="tier-judge")
    ap.add_argument("--only-confirmed", action="store_true", help="scored_by: confirmed のペアだけを使う")
    ap.add_argument("--ids", default="", help="このペア id だけ（カンマ区切り。再判定用）")
    a = ap.parse_args()
    files = sorted(Path(a.golden).glob("*.yaml"))
    rows, agree, n, cost = [], 0, 0, 0.0
    for f in files:
        g = load_yaml(f)
        if a.only_confirmed and g.get("scored_by") != "confirmed":
            continue
        if a.ids and str(g["id"]) not in a.ids.split(","):
            continue
        try:
            j1 = judge(g, a.tier, swap=False)
            j2 = judge(g, a.tier, swap=True)
        except Exception as e:  # noqa: BLE001  判定器の失敗はそのペアを error にして続ける
            print(f"{g['id']}: judge error: {e}", file=sys.stderr)
            rows.append({"id": g["id"], "ticket": g["ticket"], "golden": g["preferred"], "judge": "error", "error": str(e)[-300:], "agree": False})
            continue
        cost += (j1["cost_usd"] or 0) + (j2["cost_usd"] or 0)
        consistent = j1["preferred"] == j2["preferred"]
        verdict = j1["preferred"] if consistent else "inconsistent"
        if verdict in ("a", "b"):
            n += 1
            agree += int(verdict == g["preferred"])
        rows.append({"id": g["id"], "ticket": g["ticket"], "golden": g["preferred"], "golden_scored_by": g.get("scored_by"),
                     "judge": verdict, "runs": [j1, j2], "agree": verdict == g["preferred"]})
        print(f"{g['id']}: golden={g['preferred']} judge={verdict} ({'一致' if verdict == g['preferred'] else '不一致'})", file=sys.stderr)
    lo, hi = wilson(agree, n)
    report = {"evalspec": "l4-pairwise", "metric": "pairwise_accuracy", "tier": a.tier, "model": TIER_MODEL[a.tier],
              "evaluated_at": datetime.now(timezone.utc).isoformat(), "n": n, "agree": agree,
              "pairwise_accuracy": round(agree / n, 3) if n else None, "wilson95": [lo, hi],
              "threshold": 0.80, "min_cases_for_release": 100, "release_granted": False,
              "note": "N < 100 の暫定指標。ブレーカーには使わない。tie / inconsistent は分母から除く",
              "cost_usd": round(cost, 4), "pairs": rows}
    Path(a.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("n", "agree", "pairwise_accuracy", "wilson95", "cost_usd")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
