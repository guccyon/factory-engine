"""工程の信号（ADR 0027 の 3、ADR 0023 のテーマ 6「工程の進め方」）。

工場の工程の無駄（衝突の解消・やり直し・費用の外れ値・待ち・停止）を制御プレーンの記録から集め、`process:<信号>:<鍵>` で束ねる。
決定的（LLM を使わない）。同じ記録と同じ期間には同じ束を返す。束が閾値を超えたら、事例型の分析役（case.py）に回す。
数え方は definitions/learning.yaml の teacher_signals（conflict_fix ほか）と process_retrospective に合わせる。
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import median

from psycopg.types.json import Jsonb

WINDOW_DAYS = 7
ROUTE_MIN_SIGNALS = 10        # 束を分析役に回す: 窓の中で 10 件以上
ROUTE_MIN_COST = 50.0         # または費用の合計が $50 以上
REWORK_MIN = 3                # 同じチケットのやり直しが 3 回以上
COST_OUTLIER_X = 3.0          # 1 マージあたりの費用が、同じ種類の中央値の 3 倍以上
COST_OUTLIER_MIN_SAMPLES = 5  # 中央値を言うのに要る、同じ種類のマージの数
WAIT_MIN_H = 3.0              # 人を待たない理由で 3 時間以上止まった
WAIT_SKIP_CATEGORIES = ("human", "budget", "idle")             # 人の判断待ち・予算の枠・ラインが空は「待ち」にしない
WAIT_SKIP_PREFIXES = ("idle:", "drain:", "budget:", "heartbeat:")
STOPPAGE_CATEGORIES = ("factory-bug", "ticket")
SAMPLES_MAX = 5               # 束 1 つにつき残す例（チケット・PR・ジョブ）の数
NO_CONFLICT = "(衝突なし)"     # 衝突の解消で、取り込みに衝突がなかった
UNKNOWN = "(不明)"            # 衝突したファイルの記録がない

# ワーカーのログの 1 行（worker/entrypoint.sh）。seen は機械的に解く前に衝突していたファイル（2026-11-03 から）。
# それより前のログは conflicts だけで、機械的に解いた CHANGELOG.md は空になっている
CONFLICT_LINE = re.compile(r"^\[worker\] continue conflict .*?conflicts=\[([^\]]*)\](?: seen=\[([^\]]*)\])?", re.M)


def parse_conflict_files(log: str) -> list[str] | None:
    """衝突の解消のジョブのログから、衝突したファイル。衝突の解消のジョブでなければ None"""
    m = CONFLICT_LINE.search(log or "")
    if not m:
        return None
    return sorted(set((m.group(2) if m.group(2) is not None else m.group(1)).split()))


def record_conflict_files(cur, job_id: int, corr: str | None, files: list[str], source: str = "worker_log") -> bool:
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('conflict.files',%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id",
                (corr, job_id, Jsonb({"files": files, "source": source}), f"conflict.files:{job_id}"))
    return cur.fetchone() is not None


def _prefix(key: str) -> str:
    return (key or "").split(":", 1)[0] or "?"


def _cost(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _job_costs(cur, job_ids: list[int]) -> dict[int, float]:
    if not job_ids:
        return {}
    cur.execute("select job_id, max((payload->>'cost_usd')::float) as c from events where name='job.finished' and job_id = any(%s) "
                "and payload->>'cost_usd' is not null group by job_id", (job_ids,))
    return {r["job_id"]: _cost(r["c"]) for r in cur.fetchall()}


# ------------------------------------------------------------------ 信号を集める
def conflict_fix(cur, frm: datetime, to: datetime) -> list[dict]:
    """衝突の解消のジョブ 1 件・衝突したファイル 1 つにつき 1 件。費用はファイルの数で割る"""
    cur.execute("""select e.job_id, e.correlation_id as corr, e.occurred_at as at, e.payload->>'pr_url' as pr,
                          (select f.payload->'files' from events f where f.name='conflict.files' and f.job_id=e.job_id order by f.id limit 1) as files
                     from events e where e.name='continue.enqueued' and e.payload->>'reason'='conflict' and e.occurred_at >= %s and e.occurred_at < %s
                    order by e.id""", (frm, to))
    rows = cur.fetchall()
    costs = _job_costs(cur, [r["job_id"] for r in rows if r["job_id"]])
    out = []
    for r in rows:
        files = r["files"] if r["files"] is not None else [UNKNOWN]
        files = files or [NO_CONFLICT]
        cost = costs.get(r["job_id"], 0.0)
        for f in files:
            out.append({"signal": "conflict_fix", "key": f, "ticket": r["corr"], "pr_url": r["pr"], "job_id": r["job_id"],
                        "cost": cost / len(files), "at": r["at"]})
    return out


def rework(cur, frm: datetime, to: datetime) -> list[dict]:
    """同じチケットのやり直し（途中終了の続き・CI の直し・指摘の直し・rerun・割り込みでの積み直し）が REWORK_MIN 回以上なら、チケット 1 つにつき 1 件。
    衝突の解消は conflict_fix で数えるので除く。鍵は一番多かった理由"""
    cur.execute("""select correlation_id as corr, job_id, occurred_at as at,
                          case when name='continue.enqueued' then payload->>'reason' when name='job.preempted' then 'preempted' else 'rerun' end as reason
                     from events
                    where ((name='continue.enqueued' and payload->>'reason' <> 'conflict') or (name='job.enqueued' and payload->>'by'='rerun')
                           or name='job.preempted')
                      and occurred_at >= %s and occurred_at < %s and correlation_id is not null order by id""", (frm, to))
    by: dict[str, list[dict]] = {}
    for r in cur.fetchall():
        by.setdefault(r["corr"], []).append(r)
    costs = _job_costs(cur, [r["job_id"] for rs in by.values() for r in rs if r["job_id"]])
    out = []
    for corr, rs in sorted(by.items()):
        if len(rs) < REWORK_MIN:
            continue
        reasons = Counter(r["reason"] for r in rs)
        key = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out.append({"signal": "rework", "key": key, "ticket": corr, "pr_url": None, "job_id": rs[-1]["job_id"],
                    "cost": sum(costs.get(r["job_id"], 0.0) for r in rs), "at": rs[-1]["at"],
                    "detail": dict(sorted(reasons.items()))})
    return out


def cost_outlier(cur, frm: datetime, to: datetime) -> list[dict]:
    """マージした PR 1 本あたりの費用（そのチケットの全ジョブ）が、同じ変更種別の中央値の COST_OUTLIER_X 倍以上。鍵は変更種別"""
    cur.execute("""select m.correlation_id as corr, m.payload->>'pr_url' as pr, m.occurred_at as at,
                          (select a.payload->'change_types'->>0 from artifacts a where a.type='PR' and a.payload->>'pr_url'=m.payload->>'pr_url'
                            order by a.id limit 1) as ct,
                          (select coalesce(sum((f.payload->>'cost_usd')::float), 0) from events f
                            where f.name='job.finished' and f.correlation_id=m.correlation_id and f.payload->>'cost_usd' is not null) as cost
                     from events m where m.name='pr.merged' and m.occurred_at >= %s and m.occurred_at < %s order by m.id""", (frm, to))
    rows = cur.fetchall()
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["ct"] or "unknown", []).append(r)
    out = []
    for ct, rs in sorted(by.items()):
        if len(rs) < COST_OUTLIER_MIN_SAMPLES:
            continue
        med = median(_cost(r["cost"]) for r in rs)
        if med <= 0:
            continue
        for r in rs:
            if _cost(r["cost"]) >= COST_OUTLIER_X * med:
                out.append({"signal": "cost_outlier", "key": ct, "ticket": r["corr"], "pr_url": r["pr"], "job_id": None,
                            "cost": _cost(r["cost"]), "at": r["at"], "detail": {"median": round(med, 2), "x": round(_cost(r["cost"]) / med, 1)}})
    return out


def waits_and_stoppages(cur, frm: datetime, to: datetime) -> list[dict]:
    """wait: チケットが人を待たない理由（衝突・CI・ジョブの失敗など）で WAIT_MIN_H 時間以上止まった。鍵は停止の key の頭（ci、job など）。
    stoppage: 閉じた停止（factory-bug・ticket）のうち、wait に数えなかったもの。鍵は同じく key の頭"""
    cur.execute("""select key, category, correlation_id as corr, job_id, started_at, ended_at from stoppages
                    where started_at >= %s and started_at < %s order by started_at, key""", (frm, to))
    out = []
    for r in cur.fetchall():
        end = r["ended_at"] or to
        hours = (end - r["started_at"]).total_seconds() / 3600
        is_wait = (r["corr"] and r["category"] not in WAIT_SKIP_CATEGORIES and not r["key"].startswith(WAIT_SKIP_PREFIXES)
                   and hours >= WAIT_MIN_H)
        if is_wait:
            out.append({"signal": "wait", "key": _prefix(r["key"]), "ticket": r["corr"], "pr_url": None, "job_id": r["job_id"],
                        "cost": 0.0, "at": r["started_at"], "detail": {"hours": round(hours, 1), "open": r["ended_at"] is None}})
        elif r["ended_at"] and r["category"] in STOPPAGE_CATEGORIES:
            out.append({"signal": "stoppage", "key": _prefix(r["key"]), "ticket": r["corr"], "pr_url": None, "job_id": r["job_id"],
                        "cost": 0.0, "at": r["started_at"], "detail": {"hours": round(hours, 1), "category": r["category"]}})
    return out


def collect(cur, frm: datetime, to: datetime) -> list[dict]:
    return conflict_fix(cur, frm, to) + rework(cur, frm, to) + cost_outlier(cur, frm, to) + waits_and_stoppages(cur, frm, to)


# ------------------------------------------------------------------ 束ねる
def bundle(signals: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for s in signals:
        sig = f"process:{s['signal']}:{s['key']}"
        g = groups.setdefault(sig, {"signature": sig, "signal": s["signal"], "key": s["key"], "signals": 0, "cost": 0.0,
                                     "tickets": set(), "samples": []})
        g["signals"] += 1
        g["cost"] += s["cost"]
        if s["ticket"]:
            g["tickets"].add(s["ticket"])
        if len(g["samples"]) < SAMPLES_MAX:
            g["samples"].append({k: s.get(k) for k in ("ticket", "pr_url", "job_id", "detail")} | {"cost": round(s["cost"], 2), "at": s["at"].isoformat()})
    out = []
    for g in groups.values():
        g["tickets"] = len(g["tickets"])
        g["cost"] = round(g["cost"], 2)
        g["route"] = g["signals"] >= ROUTE_MIN_SIGNALS or g["cost"] >= ROUTE_MIN_COST
        out.append(g)
    return sorted(out, key=lambda g: (-g["cost"], -g["signals"], g["signature"]))


def retrospect(cur, now: datetime | None = None, window_days: int = WINDOW_DAYS) -> dict:
    """直近 window_days 日の工程の信号を束ねる。前の同じ長さの窓の件数・費用も並べる（効き目を比べるため）"""
    now = now or datetime.now(timezone.utc)
    frm = now - timedelta(days=window_days)
    signals = collect(cur, frm, now)
    clusters = bundle(signals)
    prev = {c["signature"]: c for c in bundle(collect(cur, frm - timedelta(days=window_days), frm))}
    for c in clusters:
        p = prev.get(c["signature"])
        c["prev"] = {"signals": p["signals"], "cost": p["cost"]} if p else {"signals": 0, "cost": 0.0}
    return {"from": frm.isoformat(), "to": now.isoformat(), "window_days": window_days, "signals": len(signals),
            "cost": round(sum(s["cost"] for s in signals), 2), "clusters": clusters}


def record_retrospective(cur, retro: dict, day: str) -> bool:
    """工程の束を artifacts（ProcessRetrospective）と events（learn.process_retrospected）に 1 日 1 回残す"""
    cur.execute("insert into events(name, payload, dedupe_key) values ('learn.process_retrospected', %s, %s) on conflict (dedupe_key) do nothing returning id",
                (Jsonb({"day": day, "signals": retro["signals"], "cost": retro["cost"], "clusters": len(retro["clusters"]),
                        "routed": [c["signature"] for c in retro["clusters"] if c["route"]]}), f"learn.process_retrospected:{day}"))
    if not cur.fetchone():
        return False
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values ('ProcessRetrospective', %s, 'learn', %s)",
                (f"learn:{day}", Jsonb({**retro, "day": day})))
    return True


# ------------------------------------------------------------------ 分析役に回す（case.py の出来事の 1 種類）
def pick_bundles(cur, now: datetime | None = None, limit: int = 1) -> list[dict]:
    """閾値を超えた束のうち、直近 WINDOW_DAYS 日に分析役へ回していないもの。費用の多い順"""
    now = now or datetime.now(timezone.utc)
    retro = retrospect(cur, now)
    cur.execute("select payload->>'signature' as s from events where name='case.enqueued' and payload->>'kind'='process' and occurred_at >= %s",
                (now - timedelta(days=WINDOW_DAYS),))
    recent = {r["s"] for r in cur.fetchall()}
    out = []
    for c in retro["clusters"]:
        if c["route"] and c["signature"] not in recent:
            out.append({"key": f"{c['signature']}@{now:%Y-%m-%d}", "kind": "process", "corr": None, "signature": c["signature"],
                        "bundle": c, "others": [{k: o[k] for k in ("signature", "signals", "tickets", "cost", "prev")}
                                                for o in retro["clusters"][:15]],
                        "window": {"from": retro["from"], "to": retro["to"], "signals": retro["signals"], "cost": retro["cost"]}})
        if len(out) >= limit:
            break
    return out


def report_lines(retro: dict) -> list[str]:
    out = [f"## 工程の信号の束（直近 {retro['window_days']} 日、{retro['signals']} 件、${retro['cost']:,.2f}）", ""]
    if not retro["clusters"]:
        return out + ["- なし"]
    out += ["| signature | 件数 | チケット | 費用 | 前の窓（件数・費用） | 分析役へ |", "|---|---|---|---|---|---|"]
    for c in retro["clusters"]:
        out.append(f"| {c['signature']} | {c['signals']} | {c['tickets']} | ${c['cost']:,.2f} | {c['prev']['signals']}・${c['prev']['cost']:,.2f} | "
                   f"{'回す' if c['route'] else '—'} |")
    return out + ["", f"- 分析役に回す条件は {WINDOW_DAYS} 日で {ROUTE_MIN_SIGNALS} 件以上か ${ROUTE_MIN_COST:.0f} 以上（definitions/learning.yaml の "
                      "process_retrospective）。前の窓は、その前の同じ長さの期間（改善の効き目を比べる）"]
