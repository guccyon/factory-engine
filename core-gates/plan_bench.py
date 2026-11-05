#!/usr/bin/env python3
"""プランナーのベンチ（ADR 0036 の 4b）。固定の評価セットの各チケットをプランナーに判定させ、人が付けた正解と比べて採点する。

- 評価セット: eval-corpus/planner/cases.yaml（チケットの本文の写しと、正解: 切るべきか・触る層・大きさの帯・経路）
- 採点: 切る／切らないの一致、大きさの帯が ±1 帯の内か、触る層の一致（Jaccard）、経路の一致
- 合格線: definitions/planner.yaml の sizing.bench（パイロット導入の前に満たす）
- プランナーのプロンプト・基準・モデルを変えるときに回し、前の版の結果（--baseline）と比べてから入れる

使い方:
  core-gates/plan_bench.py --repo-dir <製品の main の写し> [--cases eval-corpus/planner/cases.yaml] [--sizes sizes.json] [--only rally-455,...] --out result.json
  core-gates/plan_bench.py --score result.json [--baseline before.json]     # 判定し直さず、残した結果を採点し直す・比べる

1 件につきプランナーの呼び出しが 1 回（工程 plan。tier-small）。--sizes は factoryctl plan sizes の元の表（plan_outcome.size_table の JSON）。
依存: python3、PyYAML、agent_runner
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CASES = HERE.parent / "eval-corpus" / "planner" / "cases.yaml"
BANDS = ["S", "M", "L", "XL"]


def predicted(res: dict) -> dict:
    """プランナーの結果から、採点に使う見立て。切るは split・画面の後で切る印・大きすぎて人へ、のどれか"""
    p = res.get("plan") or {}
    sz = p.get("size") if isinstance(p.get("size"), dict) else {}
    split = (res.get("decision") == "split" or "split_after_design" in (res.get("advisories") or [])
             or "oversized" in (res.get("guards") or []))
    return {"decision": res.get("decision"), "split": split, "band": sz.get("turns_band"),
            "layers": sorted({l for l in sz.get("layers") or [] if l not in ("docs", "other")}), "route": res.get("steps") or [],
            "tasks": len(res.get("tasks") or []), "cost_usd": res.get("cost_usd") or 0}


def score_case(case: dict, pred: dict) -> dict:
    exp = case.get("expected") or {}
    band_ok = None
    if exp.get("band") in BANDS and pred.get("band") in BANDS:
        band_ok = abs(BANDS.index(exp["band"]) - BANDS.index(pred["band"])) <= 1
    elif exp.get("band") in BANDS:
        band_ok = False   # 見積もりがない
    el, pl = set(exp.get("layers") or []), set(pred.get("layers") or [])
    jacc = round(len(el & pl) / len(el | pl), 3) if el | pl else 1.0
    return {"id": case["id"], "split_ok": bool(exp.get("split")) == bool(pred.get("split")), "band_ok": band_ok, "layers_jaccard": jacc,
            "route_ok": (list(exp.get("route") or []) == list(pred.get("route") or [])) if exp.get("route") else None,
            "expected": {k: exp.get(k) for k in ("split", "band", "layers", "route")}, "predicted": pred}


def summarize(scored: list[dict], bench: dict) -> dict:
    n = len(scored)
    banded = [s for s in scored if s["band_ok"] is not None]
    routed = [s for s in scored if s["route_ok"] is not None]
    split_ok = sum(1 for s in scored if s["split_ok"])
    should = [s for s in scored if s["expected"].get("split")]   # 切るべき件（合格線ではない。切らないだけのプランナーを見落とさないために出す）
    band_ok = sum(1 for s in banded if s["band_ok"])
    out = {"cases": n, "split_ok": split_ok, "band_cases": len(banded), "band_ok": band_ok,
           "layers_jaccard_mean": round(sum(s["layers_jaccard"] for s in scored) / n, 3) if n else None,
           "should_split": len(should), "should_split_ok": sum(1 for s in should if s["split_ok"]),
           "route_cases": len(routed), "route_ok": sum(1 for s in routed if s["route_ok"]),
           "cost_usd": round(sum(float(s["predicted"].get("cost_usd") or 0) for s in scored), 4),
           "lines": {"split_ok_min": bench["split_ok_min"], "band_ok_min": bench["band_ok_min"]}}
    out["pass"] = bool(n) and split_ok / n >= bench["split_ok_min"] and (not banded or band_ok / len(banded) >= bench["band_ok_min"])
    return out


def render(summary: dict, scored: list[dict], baseline: dict | None = None) -> str:
    lines = [f"{'id':<14} {'切る':<10} {'帯':<10} {'層':>5} {'経路':<6}"]
    for s in scored:
        e, p = s["expected"], s["predicted"]
        mark = lambda ok: "-" if ok is None else ("ok" if ok else "NG")  # noqa: E731
        lines.append(f"{s['id']:<14} {mark(s['split_ok']):<3}{str(e['split'])[0]}/{str(p['split'])[0]:<5} "
                     f"{mark(s['band_ok']):<3}{e['band'] or '-'}/{p['band'] or '-':<5} {s['layers_jaccard']:>5} {mark(s['route_ok']):<6}")
    sm = summary
    lines.append(f"切る／切らないの一致 {sm['split_ok']}/{sm['cases']}（合格線 {sm['lines']['split_ok_min']:.0%}）、"
                 f"うち切るべき件 {sm['should_split_ok']}/{sm['should_split']}、"
                 f"帯 ±1 {sm['band_ok']}/{sm['band_cases']}（合格線 {sm['lines']['band_ok_min']:.0%}）、"
                 f"層の一致 {sm['layers_jaccard_mean']}、経路 {sm['route_ok']}/{sm['route_cases']}、費用 ${sm['cost_usd']}  → {'合格' if sm['pass'] else '不合格'}")
    if baseline:
        b = baseline["summary"]
        lines.append(f"前の版: 切る {b['split_ok']}/{b['cases']}、帯 {b['band_ok']}/{b['band_cases']}、層 {b['layers_jaccard_mean']}"
                     f"（{'合格' if b['pass'] else '不合格'}）")
    return "\n".join(lines)


def run(cases: list[dict], defs: dict, repo_dir: Path | None, sizes: list[dict]) -> list[dict]:
    import plan as pl
    scored = []
    for c in cases:
        inp = {"round": 1, "size_table": sizes, **({"phase": c["phase"]} if c.get("phase") else {})}
        issue = {"title": (c.get("input") or {}).get("title") or c.get("title") or "", "body": (c.get("input") or {}).get("body") or "", "comments": []}
        res = pl.plan(issue, inp, defs, repo_dir)
        s = score_case(c, predicted(res))
        s["result"] = {k: res.get(k) for k in ("decision", "steps", "guards", "advisories", "oversize", "tasks", "versions", "cost_usd")}
        s["result"]["size"] = (res.get("plan") or {}).get("size")
        scored.append(s)
        print(f"{c['id']}: {res.get('decision')} {s['predicted']['band']} split_ok={s['split_ok']}", file=sys.stderr, flush=True)
    return scored


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cases", default=str(CASES))
    ap.add_argument("--repo-dir")
    ap.add_argument("--sizes")
    ap.add_argument("--only")
    ap.add_argument("--out")
    ap.add_argument("--score", help="残した結果を採点し直す")
    ap.add_argument("--baseline")
    a = ap.parse_args(argv)
    import plan as pl
    defs = yaml.safe_load(pl.DEFS.read_text(encoding="utf-8"))
    bench = defs["sizing"]["bench"]
    if a.score:
        prev = json.loads(Path(a.score).read_text(encoding="utf-8"))
        cases = {c["id"]: c for c in yaml.safe_load(Path(a.cases).read_text(encoding="utf-8"))["cases"]}
        scored = [{**score_case(cases[s["id"]], s["predicted"]), "result": s.get("result")} for s in prev["scored"] if s["id"] in cases]
    else:
        cases = yaml.safe_load(Path(a.cases).read_text(encoding="utf-8"))["cases"]
        if a.only:
            keep = set(a.only.split(","))
            cases = [c for c in cases if c["id"] in keep]
        sizes = json.loads(Path(a.sizes).read_text(encoding="utf-8")) if a.sizes else []
        scored = run(cases, defs, Path(a.repo_dir) if a.repo_dir else None, sizes)
    summary = summarize(scored, bench)
    baseline = json.loads(Path(a.baseline).read_text(encoding="utf-8")) if a.baseline else None
    print(render(summary, scored, baseline))
    if a.out:
        Path(a.out).write_text(json.dumps({"summary": summary, "scored": scored}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
