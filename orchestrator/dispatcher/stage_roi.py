"""工程ごとの ROI（2026-11-08 オーナー「確信度の高い工程から安いモデルを試す」）。LLM は使わない。

集計はここ 1 か所。建設日報（health/daily/status_report.py）と工場の操業レポート（report_health.py の節 stages）が同じものを使う。

最終成果物の品質を、費用とリードタイムを含めて ROI で見て、ボトルネックがどの工程にあるかを分かるようにする。
問題がありそうな工程が見つかったら、その工程の既定のモデルを替える（definitions/agents.yaml）判断の根拠にする。

- 工程×モデルごと: ジョブ数、費用（合計・中央値）、作業の時間と待ちの時間（中央値）、一発で通った割合、混入させた Defect
- ボトルネック: 時間（待ち＋作業）と費用の割合がいちばん大きい工程、一発で通る割合がいちばん低い工程、Defect をいちばん混入させた工程
- モデルの試し（agents.yaml の experiments）: 条件に当たったチケットを試す側（trial）と既定の側（control）で比べる。チケットごとに
  費用の合計・リードタイム（最初の実装を積んでからマージまで）・一発で通ったか・混入した Defect。通った 1 件あたりの費用が ROI の物差し

数え方（仮置き。2026-11-08 建設者）:
- 工程は jobs.kind。実装（implement）は続きの理由で分ける（CI 落ちの直し・指摘の直し・衝突の解消・途中終了の続き）
- 一発で通った: 実装は、そのチケットに後から直し（CI 落ち・指摘・途中終了の続き）が積まれなかったこと（衝突の解消は数えない。他のチケットのせいなので）。
  それ以外の工程は、失敗せずに 1 回目（attempt 1）で終わったこと
- 作業の時間は started_at〜finished_at、待ちの時間は enqueued_at〜started_at（錠の待ちで積み直したときは最後に始まった時点から）
- 混入させた Defect は defect.recorded の introduced_job（PR の最新の版を書いた実装のジョブ）で数える。QA で見つけたものは直した PR が分かるまで数えない
"""
from __future__ import annotations

import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

WINDOW_DAYS = int(os.environ.get("ROI_WINDOW_DAYS", "14"))
# dispatcher のイメージには definitions/ がないので、読めなければ TIER_MODEL の写しを使う


def agents_candidates(here: Path) -> list[Path]:
    """agents.yaml の置き場所の候補。作業ツリーでは orchestrator/dispatcher/ の 2 つ上がリポジトリの根。
    コンテナでは /app/stage_roi.py で親が足りないので、あるときだけ足す（2026-11-09 読み込みで IndexError になり朝の報告と建設日報が止まった）"""
    out = [Path(os.environ["ROADMAP"]).parent / "agents.yaml"] if os.environ.get("ROADMAP") else []
    if len(here.parents) > 2:
        out.append(here.parents[2] / "definitions" / "agents.yaml")
    return out + [Path("/definitions/agents.yaml")]


AGENTS_CANDIDATES = agents_candidates(Path(__file__).resolve())
TIER_MODEL = {"tier-large": "Opus", "tier-judge": "Opus", "tier-small": "Sonnet", "tier-light": "Haiku"}   # agents.yaml の tiers の写し（テストで見る）
MIN_N = 5   # 比べる側ごとにこれより少なければ「参考」

STAGE_JA = {
    "implement": "実装", "implement:ci_failed": "直し（CI 落ち）", "implement:review_fix": "直し（指摘）",
    "implement:conflict": "衝突の解消", "implement:incomplete": "続き（途中終了）",
    "body_fix": "PR 本文の直し", "inspect": "検査", "judge": "判定（judge）", "plan": "プランナー", "clarify": "要求の確認",
    "ui_design": "デザイナー", "ui_design_review": "デザイン検査", "design_system": "デザインシステム", "qa": "QA",
    "design_review": "設計の診断", "case_review": "事例の分析", "committee_review": "知見の委員会", "committee_weekly": "委員会の定例",
    "desk_consult": "窓口の相談役", "desk_answer": "窓口の回答役",
}
FIX_STAGES = ("implement:ci_failed", "implement:review_fix", "implement:incomplete")   # 実装が一発で通らなかった印
MODEL_JA = {"claude-opus-5-5": "Opus", "claude-sonnet-5-5": "Sonnet", "claude-haiku-5-5": "Haiku"}


def tier_models(path: Path | None = None) -> dict[str, str]:
    """tier → モデルの短い名前（agents.yaml の tiers）。読めなければ写し"""
    for c in ([path] if path else AGENTS_CANDIDATES):
        try:
            tiers = (yaml.safe_load(c.read_text(encoding="utf-8")) or {}).get("tiers") or {}
        except Exception:  # noqa: BLE001
            continue
        return {k: MODEL_JA.get(v.get("model"), v.get("model") or k) for k, v in tiers.items() if isinstance(v, dict)}
    return dict(TIER_MODEL)


def stage_of(kind: str | None, cont: str | None) -> str:
    kind = kind or "implement"
    return f"implement:{cont}" if kind == "implement" and cont else kind


def _min(a, b) -> float | None:
    if not a or not b:
        return None
    a, b = (datetime.fromisoformat(x) if isinstance(x, str) else x for x in (a, b))
    return max((b - a).total_seconds() / 60, 0.0)


def _med(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def compute(jobs: list[dict], routes: dict[str, dict], defects: list[dict], merges: dict[str, object], tiers: dict[str, str]) -> dict:
    """jobs: id, corr, kind, model_tier, state, attempt, enqueued_at, started_at, finished_at, cost, cont
    routes: チケット → model.routed の最新の payload、defects: defect.recorded の payload、merges: チケット → 最初にマージした時刻"""
    by_corr: dict[str, list[dict]] = {}
    for j in sorted(jobs, key=lambda x: x["id"]):
        j["stage"] = stage_of(j.get("kind"), j.get("cont"))
        j["model"] = tiers.get(j.get("model_tier") or "", j.get("model_tier") or "不明")
        j["work"], j["wait"] = _min(j.get("started_at"), j.get("finished_at")), _min(j.get("enqueued_at"), j.get("started_at"))
        by_corr.setdefault(j["corr"], []).append(j)
    intro: dict[int, int] = {}
    for d in defects:
        if isinstance(d.get("introduced_job"), int):
            intro[d["introduced_job"]] = intro.get(d["introduced_job"], 0) + 1
    for corr, js in by_corr.items():
        for i, j in enumerate(js):
            ok = j.get("state") == "succeeded"
            if j["stage"] == "implement":
                j["first_pass"] = ok and not any(x["stage"] in FIX_STAGES for x in js[i + 1:])
            else:
                j["first_pass"] = ok and int(j.get("attempt") or 1) == 1
            j["defects"] = intro.get(j["id"], 0)

    # 工程×モデル
    groups: dict[tuple[str, str], list[dict]] = {}
    for j in jobs:
        groups.setdefault((j["stage"], j["model"]), []).append(j)
    total_cost = sum(float(j.get("cost") or 0) for j in jobs) or 0.0
    total_time = sum((j["work"] or 0) + (j["wait"] or 0) for j in jobs) or 0.0
    stages = []
    for (st, model), js in groups.items():
        cost = sum(float(j.get("cost") or 0) for j in js)
        time_ = sum((j["work"] or 0) + (j["wait"] or 0) for j in js)
        stages.append({"stage": st, "ja": STAGE_JA.get(st, st), "model": model, "jobs": len(js),
                       "cost_usd": round(cost, 2), "cost_median": _med([float(j.get("cost") or 0) for j in js]),
                       "work_min": _med([j["work"] for j in js]), "wait_min": _med([j["wait"] for j in js]),
                       "first_pass": sum(1 for j in js if j["first_pass"]) / len(js),
                       "defects": sum(j["defects"] for j in js), "defects_per_job": sum(j["defects"] for j in js) / len(js),
                       "cost_share": cost / total_cost if total_cost else None, "time_share": time_ / total_time if total_time else None})
    stages.sort(key=lambda r: -(r["cost_usd"] or 0))

    # ボトルネック（モデルをまとめた工程ごと。件数が MIN_N 未満の工程は一発の割合と Defect の比べから外す）
    per: dict[str, dict] = {}
    for r in stages:
        p = per.setdefault(r["stage"], {"stage": r["stage"], "ja": r["ja"], "jobs": 0, "cost": 0.0, "time": 0.0, "fp": 0.0, "defects": 0})
        p["jobs"] += r["jobs"]; p["cost"] += r["cost_usd"]; p["fp"] += r["first_pass"] * r["jobs"]; p["defects"] += r["defects"]
        p["time"] += (r["time_share"] or 0) * total_time
    bn = {}
    if per:
        t = max(per.values(), key=lambda p: p["time"])
        c = max(per.values(), key=lambda p: p["cost"])
        bn["time"] = {"stage": t["stage"], "ja": t["ja"], "share": t["time"] / total_time if total_time else None}
        bn["cost"] = {"stage": c["stage"], "ja": c["ja"], "share": c["cost"] / total_cost if total_cost else None}
        enough = [p for p in per.values() if p["jobs"] >= MIN_N]
        if enough:
            f = min(enough, key=lambda p: p["fp"] / p["jobs"])
            bn["first_pass"] = {"stage": f["stage"], "ja": f["ja"], "rate": f["fp"] / f["jobs"]}
        dz = [p for p in per.values() if p["defects"]]
        if dz:
            d = max(dz, key=lambda p: p["defects"] / p["jobs"])
            bn["defects"] = {"stage": d["stage"], "ja": d["ja"], "per_job": d["defects"] / d["jobs"], "n": d["defects"]}
        waits = [j["wait"] for j in jobs if j["wait"] is not None]
        works = [j["work"] for j in jobs if j["work"] is not None]
        bn["wait_share"] = sum(waits) / (sum(waits) + sum(works)) if (waits or works) and (sum(waits) + sum(works)) else None

    return {"window_days": WINDOW_DAYS, "jobs": len(jobs), "cost_usd": round(total_cost, 2), "stages": stages,
            "bottleneck": bn, "experiment": experiment(by_corr, routes, merges)}


def experiment(by_corr: dict[str, list[dict]], routes: dict[str, dict], merges: dict[str, object]) -> dict | None:
    """条件に当たったチケットを試す側と既定の側で比べる（チケットの単位。費用は検査・judge なども含めたチケットの全部）"""
    arms: dict[str, list[dict]] = {"trial": [], "control": []}
    exp_id, out = None, 0
    for corr, r in routes.items():
        exp_id = exp_id or r.get("experiment")
        if r.get("arm") not in arms:
            out += 1
            continue
        js = by_corr.get(corr) or []
        impl = [j for j in js if j["stage"] == "implement"]
        if not impl:
            continue
        first = impl[0]
        merged = merges.get(corr)
        lead = _min(first.get("enqueued_at"), merged) if merged else None
        work = sum(j["work"] or 0 for j in js)
        arms[r["arm"]].append({"corr": corr, "cost": sum(float(j.get("cost") or 0) for j in js), "merged": bool(merged),
                               "lead_h": lead / 60 if lead is not None else None, "work_h": work / 60,
                               "first_pass": bool(first["first_pass"]), "defects": sum(j["defects"] for j in js),
                               "jobs": len(js), "routed_back": bool(r.get("routed_back"))})
    if not routes:
        return None
    res = {"id": exp_id, "out": out, "arms": {}}
    for arm, ts in arms.items():
        n, merged = len(ts), sum(1 for t in ts if t["merged"])
        cost = sum(t["cost"] for t in ts)
        res["arms"][arm] = {"tickets": n, "merged": merged, "cost_usd": round(cost, 2), "cost_median": _med([t["cost"] for t in ts]),
                            "lead_h_median": _med([t["lead_h"] for t in ts]), "first_pass": (sum(1 for t in ts if t["first_pass"]) / n) if n else None,
                            "defects_per_ticket": (sum(t["defects"] for t in ts) / n) if n else None,
                            "cost_per_merged": (cost / merged) if merged else None, "routed_back": sum(1 for t in ts if t["routed_back"]),
                            "enough": n >= MIN_N}
    a, b = res["arms"]["trial"], res["arms"]["control"]
    if a["cost_per_merged"] and b["cost_per_merged"]:
        res["cost_ratio"] = a["cost_per_merged"] / b["cost_per_merged"]
    return res


# ------------------------------------------------------------------ 取得元
JOB_COLS = ["id", "corr", "kind", "model_tier", "state", "attempt", "enqueued_at", "started_at", "finished_at", "cost", "cont"]


def _rows(cur, cols: list[str]) -> list[dict]:
    """タプルの cursor でも dict_row の cursor でも同じ形に"""
    return [dict(r) if isinstance(r, dict) else dict(zip(cols, r)) for r in cur.fetchall()]


def fetch(cur, start, end) -> dict:
    """[start, end) に終わったジョブと、その周りの記録で集計する（操業レポートの期間と、日報の直近 N 日で共通）"""
    cur.execute("""
      select j.id, j.correlation_id as corr, j.kind, j.model_tier, j.state, j.attempt, j.enqueued_at, j.started_at, j.finished_at,
             (select (e.payload->>'cost_usd')::float from events e where e.name='job.finished' and e.job_id=j.id order by e.id desc limit 1) as cost,
             (select e.payload->'continue'->>'reason' from events e where e.name='job.enqueued' and e.job_id=j.id order by e.id limit 1) as cont
        from jobs j where j.finished_at >= %s and j.finished_at < %s and j.state in ('succeeded','failed')""", (start, end))
    jobs = _rows(cur, JOB_COLS)
    corrs = sorted({j["corr"] for j in jobs})
    cur.execute("select distinct on (correlation_id) correlation_id as corr, payload from events where name='model.routed' and correlation_id = any(%s) "
                "order by correlation_id, id desc", (corrs,))
    routes = {r["corr"]: r["payload"] for r in _rows(cur, ["corr", "payload"])}
    cur.execute("select payload from events where name='defect.recorded' and occurred_at >= %s and occurred_at < %s", (start, end))
    defects = [r["payload"] for r in _rows(cur, ["payload"])]
    cur.execute("select correlation_id as corr, min(occurred_at) as at from events where name='pr.merged' and correlation_id = any(%s) group by 1", (corrs,))
    merges = {r["corr"]: r["at"] for r in _rows(cur, ["corr", "at"])}
    v = compute(jobs, routes, defects, merges, tier_models())
    v["window_days"] = round((end - start).total_seconds() / 86400)
    return v


def view(conn, days: int = WINDOW_DAYS) -> dict | None:
    """建設日報の直近 days 日。数えられなくてもレポートは出す"""
    if conn is None:
        return None
    try:
        end = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            return fetch(cur, end - timedelta(days=days), end)
    except Exception as e:  # noqa: BLE001
        print("[status-report] 工程ごとの ROI を数えられない:", repr(e), file=sys.stderr)
        return None


# ------------------------------------------------------------------ 表示（Markdown）
def _p(x) -> str:
    return "-" if x is None else f"{round(x * 100)}%"


def _m(x) -> str:
    return "-" if x is None else (f"{x:.0f} 分" if x < 120 else f"{x / 60:.1f} 時間")


def _d(x) -> str:
    return "-" if x is None else f"${x:,.2f}"


def bottleneck_lines(v: dict) -> list[str]:
    bn = v.get("bottleneck") or {}
    out = []
    if bn.get("time"):
        out.append(f"時間は **{bn['time']['ja']}**（全体の {_p(bn['time']['share'])}）。待ちが全体の {_p(bn.get('wait_share'))}")
    if bn.get("cost"):
        out.append(f"費用は **{bn['cost']['ja']}**（全体の {_p(bn['cost']['share'])}）")
    if bn.get("first_pass"):
        out.append(f"一発で通りにくいのは **{bn['first_pass']['ja']}**（{_p(bn['first_pass']['rate'])}）")
    if bn.get("defects"):
        out.append(f"Defect を混入させたのは **{bn['defects']['ja']}**（{bn['defects']['n']} 件、1 ジョブあたり {bn['defects']['per_job']:.2f}）")
    return out


def experiment_lines(x: dict | None) -> list[str]:
    if not x:
        return ["（モデルの試しの記録はまだありません）"]
    a, b = x["arms"]["trial"], x["arms"]["control"]
    lines = ["| 側 | チケット | マージ | 通った 1 件あたりの費用 | 費用の中央値 | リードタイムの中央値 | 一発で通った | Defect／件 | 既定に戻した |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, r in (("試す側（Sonnet）", a), ("既定の側（Opus）", b)):
        lt = "-" if r["lead_h_median"] is None else f"{r['lead_h_median']:.1f} 時間"
        dpt = "-" if r["defects_per_ticket"] is None else f"{r['defects_per_ticket']:.2f}"
        lines.append(f"| {name} | {r['tickets']}{'' if r['enough'] else '（参考）'} | {r['merged']} | {_d(r['cost_per_merged'])} | {_d(r['cost_median'])} | "
                     f"{lt} | {_p(r['first_pass'])} | {dpt} | {r['routed_back']} |")
    lines.append("")
    if not (a["enough"] and b["enough"]):
        lines.append(f"どちらかの側が {MIN_N} 件に届くまでは参考。条件に当たらないチケット {x['out']} 件。")
    elif x.get("cost_ratio") is not None:
        lines.append(f"試す側の通った 1 件あたりの費用は既定の側の {x['cost_ratio'] * 100:.0f}%。一発で通った割合の差 "
                     f"{round(((a['first_pass'] or 0) - (b['first_pass'] or 0)) * 100)} pt。")
    return lines


def fmt(v: dict | None) -> list[str]:
    if not v:
        return ["（工程ごとの記録を読めませんでした）"]
    if not v["jobs"]:
        return [f"（直近 {v['window_days']} 日に終わったジョブはありません）"]
    lines = [f"直近 {v['window_days']} 日、終わったジョブ {v['jobs']} 件、{_d(v['cost_usd'])}（API 単価換算）。", ""]
    bl = bottleneck_lines(v)
    if bl:
        lines += ["**ボトルネック**", *[f"- {x}" for x in bl], ""]
    lines += ["| 工程 | モデル | ジョブ | 費用 | 費用の割合 | 時間の割合 | 作業（中央値） | 待ち（中央値） | 一発で通った | 混入した Defect |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in v["stages"][:15]:
        lines.append(f"| {r['ja']} | {r['model']} | {r['jobs']} | {_d(r['cost_usd'])} | {_p(r['cost_share'])} | {_p(r['time_share'])} | "
                     f"{_m(r['work_min'])} | {_m(r['wait_min'])} | {_p(r['first_pass'])} | {r['defects']} |")
    lines += ["", "**モデルの試し（小さいチケットの実装の一部を Sonnet で）**", "", *experiment_lines(v.get("experiment")), "",
              "一発で通った: 実装は後から直し（CI 落ち・指摘・途中終了の続き）が積まれなかった割合、ほかの工程は 1 回目で失敗せずに終わった割合。"
              "Defect は PR の最新の版を書いた実装のジョブに付けて数える"]
    return lines


def daily_line(v: dict | None) -> str | None:
    if not v or not v["jobs"]:
        return None
    bl = bottleneck_lines(v)
    return "- 工程ごとの ROI: " + ("。".join(x.replace("**", "") for x in bl[:2]) if bl else "まだ数えられる記録がない")
