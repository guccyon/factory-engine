"""週次の KPI の Report ライン（ADR 0043 の O2）と、KPI の予測誤差の教師信号（O3）。

流れ（どれも決定的で、トークンを使わない）:
1. supervisor が週に 1 回（SCHEDULE、JST）、製品ごとに staging で KPI を測る依頼（release.kpi_requested）を書く。
   測るのは shipper（docker を持つのは shipper だけ）で、製品の .factory-config/ship.yaml の kpi のコマンドを staging で実行し、
   値を release.kpi_measured に残す。factoryctl report kpi で今すぐ 1 回測れる
2. 測った結果が出たら、前に測った値（同じ製品・同じ環境のひとつ前の測定）と比べ、前回差分を説明する材料（期間にマージした PR、見込み）を付けて
   KPIReport に残す（report.kpi_weekly）
3. 目安の幅から外れた・前回より悪い向きに大きく動いた・見込みと逆に動いた値を「異常」とし、1 回 OPPORTUNITIES_MAX 件まで
   Opportunity の下書き（artifacts.yaml の Opportunity。題・問題・期待する効果）に起こし、改善の提案の口（opportunity.py。O4 の合成の利用者と共通）から
   opportunity.proposed と受信トレイ（プロダクトオーナーの依頼）に出す。起票はしない（人かプランナーが拾う）
4. 前回の測定から今回までにマージしたチケットのうち、プランナーが Requirements に KPI の見込み（kpi_forecast）を書いたものを、
   実測の差と突き合わせて誤差を kpi.forecast_scored に残す。Learn の Retrospective が教師信号 kpi_error として束ねる（learn.py）

Gen1 の KPI は staging の合成シードの値なので、異常や誤差の中身に意味はない（回路の確認。ADR 0043 の O3）。
閾値はすべて仮置き（運用パラメータ）。製品ごとの目安の幅と変化の閾値は ship.yaml の kpi.metrics（expected・change_alert）で持つ。
"""
from __future__ import annotations

from datetime import datetime, timezone

from psycopg.types.json import Jsonb

import design
import opportunity
import ship

SCHEDULE = {"weekday": "monday", "hour": 7}   # この曜日・時（JST）を過ぎたら、その週の分を測る
ENV = "staging"                               # Gen1 は staging（rally は本番を立てない。ADR 0012 の追記）
CHANGE_ALERT = 0.2                            # 前回からの変化（比）がこれ以上で悪い向きなら異常（ship.yaml の change_alert が優先）
OPPORTUNITIES_MAX = 3                         # 1 回の Report で起こす Opportunity の下書き
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
SOURCE = "report.kpi"                         # 改善の提案（opportunity.proposed）の出どころ
THEME = 1                                     # learning_taxonomy.yaml のテーマ 1（利用者の体験）。製品の KPI の異常はここに入れる


def due(now: datetime, schedule: dict | None = None) -> bool:
    s = schedule or SCHEDULE
    now = now.astimezone(design.JST)
    return (now.weekday(), now.hour) >= (WEEKDAYS.index(s["weekday"]), int(s["hour"]))


# ------------------------------------------------------------------ 1. 測る依頼
def request_step(cur, repos: list[str], now: datetime | None = None, log=print) -> list[str]:
    """週に 1 回、製品ごとに staging で KPI を測る依頼を書く。staging に版がない製品は週に 1 回だけ report.kpi_skipped に残す"""
    now = (now or datetime.now(timezone.utc)).astimezone(design.JST)
    if not due(now):
        return []
    week, out = design.week_of(now), []
    for repo in repos:
        cur.execute("select 1 from events where name in ('release.kpi_requested', 'report.kpi_skipped') and payload->>'repo'=%s "
                    "and payload->>'week'=%s and payload->>'by'='supervisor'", (repo, week))
        if cur.fetchone():
            continue
        ok, msg = ship.request_kpi(cur, repo, ENV, week, "supervisor", "report_line")
        if not ok:
            cur.execute("insert into events(name, payload, dedupe_key) values ('report.kpi_skipped', %s, %s) on conflict (dedupe_key) do nothing",
                        (Jsonb({"repo": repo, "week": week, "env": ENV, "reason": msg, "by": "supervisor"}), f"report.kpi_skipped:{repo}:{week}"))
            log("kpi skipped", repo, week, msg)
            continue
        out.append(repo)
        log("kpi requested", repo, week)
    return out


# ------------------------------------------------------------------ 2〜4. 測った結果から Report・異常・誤差
def pending(cur) -> list[dict]:
    """まだ Report にしていない、測れた結果"""
    cur.execute("select id, occurred_at, payload from events m where name='release.kpi_measured' and (payload->>'ok')::boolean "
                "and not exists (select 1 from events r where r.name='report.kpi_weekly' and (r.payload->>'measured_id')::bigint = m.id) order by id")
    return cur.fetchall()


def baseline(cur, repo: str, env: str, before_id: int) -> dict | None:
    """同じ製品・同じ環境のひとつ前の測れた結果"""
    cur.execute("select id, occurred_at, payload from events where name='release.kpi_measured' and (payload->>'ok')::boolean "
                "and payload->>'repo'=%s and payload->>'env'=%s and id < %s order by id desc limit 1", (repo, env, before_id))
    return cur.fetchone()


def merged_between(cur, repo: str, frm: datetime | None, to: datetime) -> list[dict]:
    """期間にマージした工場の PR と、そのチケットの Requirements の KPI の見込み（あれば）"""
    cur.execute("select e.correlation_id as corr, e.payload->>'pr_url' as pr_url, e.occurred_at, "
                "(select a.payload->'kpi_forecast' from artifacts a where a.type='Requirements' and a.correlation_id=e.correlation_id "
                " order by a.id desc limit 1) as forecast "
                "from events e where e.name='pr.merged' and e.correlation_id like %s and e.occurred_at < %s "
                "and (%s::timestamptz is null or e.occurred_at >= %s) order by e.id",
                (f"github:{repo}#%", to, frm, frm))
    return cur.fetchall()


def _fmt(v, unit: str = "") -> str:
    if v is None:
        return "不明"
    return f"{round(v, 4):g}{unit}"


def compare(metrics: list[dict], base: list[dict] | None) -> list[dict]:
    """値ごとに前回の値と差を付ける。change は前回比（前回が 0 か不明なら None）"""
    prev = {m["id"]: m.get("value") for m in base or []}
    rows = []
    for m in metrics:
        v, p = m.get("value"), prev.get(m["id"])
        delta = v - p if v is not None and p is not None else None
        rows.append({**m, "prev": p, "delta": delta, "change": delta / abs(p) if delta is not None and p else None,
                     "forecasts": [], "anomalies": []})
    return rows


def find_anomalies(row: dict) -> list[dict]:
    """値 1 つの異常。kind: range（目安の幅の外）・prev（前回より悪い向きに大きく動いた）・forecast（見込みと逆に動いた・見込みの倍より大きく外れた）"""
    v, unit, out = row.get("value"), row.get("unit") or "", []
    if v is None:
        return out
    good_up = row.get("direction") != "down_is_good"
    exp = row.get("expected")
    if exp and ((exp[0] is not None and v < exp[0]) or (exp[1] is not None and v > exp[1])):
        out.append({"kind": "range", "text": f"目安 {_fmt(exp[0], unit)}〜{_fmt(exp[1], unit)} の外（{_fmt(v, unit)}）"})
    ch, alert = row.get("change"), row.get("change_alert") or CHANGE_ALERT
    if ch is not None and abs(ch) >= alert and (ch < 0) == good_up:
        out.append({"kind": "prev", "text": f"前回 {_fmt(row['prev'], unit)} から {_fmt(v, unit)} に悪化（{round(100 * ch):+d}%）"})
    for f in row.get("forecasts") or []:
        if f.get("error") is None:
            continue
        if (f["forecast"] > 0 > f["actual"]) or (f["forecast"] < 0 < f["actual"]) or abs(f["error"]) > abs(f["forecast"]):
            out.append({"kind": "forecast", "text": f"{f['ticket']} の見込み {f['forecast']:+g}{unit} に対し実測 {round(f['actual'], 4):+g}{unit}",
                        "ticket": f["ticket"]})
    return out


def draft(row: dict, a: dict, repo: str, week: str) -> dict:
    """異常 1 つを Opportunity の下書き（artifacts.yaml の Opportunity: title・problem・expected_effect{metric, direction, range}）にする"""
    unit, good_up = row.get("unit") or "", row.get("direction") != "down_is_good"
    if a["kind"] == "range" and row.get("expected"):
        lo, hi = row["expected"]
        rng = f"目安 {_fmt(lo, unit)}〜{_fmt(hi, unit)} に戻す"
    elif a["kind"] == "prev":
        rng = f"前回の {_fmt(row['prev'], unit)} まで戻す"
    else:
        rng = "見込みと実測の食い違いの理由を確かめる（見込みの立て方か、変更の効き目か）"
    what = {"range": "目安から外れた", "prev": "前回より悪化した", "forecast": "見込みと食い違った"}[a["kind"]]
    return {"title": f"KPI「{row['ja']}」が{what}",
            "problem": f"{repo} の {week} の週次の KPI で、{row['ja']}が{a['text']}。",
            "expected_effect": {"metric": row["id"], "direction": "up" if good_up else "down", "range": rng},
            "evidence": {"value": row.get("value"), "prev": row.get("prev"), "unit": unit, "expected": row.get("expected"),
                         "anomaly": a["kind"], "ticket": a.get("ticket")},
            "repo": repo, "week": week, "source": "report.kpi"}


def score(cur, rows: list[dict], merged: list[dict], ev: dict, base: dict | None) -> list[dict]:
    """見込み（kpi_forecast）を書いたチケットごとに、実測の差と突き合わせて kpi.forecast_scored に残す（1 チケット・1 KPI に 1 回）。
    前回の測定がなく差が取れないものは残さない（次の測定でも期間に入らないので、そのまま数えない）"""
    by_id = {r["id"]: r for r in rows}
    p, scored = ev["payload"], []
    for m in merged:
        f = m["forecast"] if isinstance(m["forecast"], dict) else None
        if not f or f.get("kpi") not in by_id or not isinstance(f.get("change"), (int, float)) or isinstance(f.get("change"), bool):
            continue
        row = by_id[f["kpi"]]
        actual = row.get("delta")
        err = actual - f["change"] if actual is not None else None
        rec = {"ticket": m["corr"], "pr_url": m["pr_url"], "kpi": f["kpi"], "forecast": float(f["change"]), "actual": actual, "error": err,
               "unit": row.get("unit") or "", "why": str(f.get("why") or "")[:300]}
        row["forecasts"].append(rec)
        if err is None:
            continue
        cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('kpi.forecast_scored', %s, %s, %s) "
                    "on conflict (dedupe_key) do nothing returning id",
                    (m["corr"], Jsonb({**rec, "repo": p["repo"], "env": p["env"], "week": p.get("week"), "measured_id": ev["id"],
                                       "base_id": base["id"] if base else None}), f"kpi.forecast_scored:{m['corr']}:{f['kpi']}"))
        if cur.fetchone():
            scored.append(rec)
    return scored


def report_one(cur, ev: dict, log=print) -> dict | None:
    """測れた結果 1 つから KPIReport・Opportunity の下書き・予測誤差を作る。同じ測定には 1 回だけ"""
    p = ev["payload"]
    repo, env = p["repo"], p["env"]
    week = p.get("week") or design.week_of(ev["occurred_at"])
    base = baseline(cur, repo, env, ev["id"])
    rows = compare(p.get("metrics") or [], base["payload"].get("metrics") if base else None)
    merged = merged_between(cur, repo, base["occurred_at"] if base else None, ev["occurred_at"])
    scored = score(cur, rows, merged, ev, base)
    for r in rows:
        r["anomalies"] = find_anomalies(r)
    opps = [draft(r, a, repo, week) for r in rows for a in r["anomalies"]][:OPPORTUNITIES_MAX]
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('report.kpi_weekly', %s, %s, %s) "
                "on conflict (dedupe_key) do nothing returning id",
                (f"kpi:{repo}:{week}", Jsonb({"repo": repo, "env": env, "week": week, "measured_id": ev["id"], "base_id": base["id"] if base else None,
                                              "release_id": p.get("release_id"), "by": p.get("by"),
                                              "metrics": [{k: r.get(k) for k in ("id", "ja", "unit", "direction", "value", "prev", "delta")}
                                                          | {"anomalies": [a["kind"] for a in r["anomalies"]]} for r in rows],
                                              "anomalies": sum(len(r["anomalies"]) for r in rows), "opportunities": len(opps),
                                              "merged_prs": len(merged), "scored": len(scored)}), f"report.kpi_weekly:{ev['id']}"))
    if not cur.fetchone():
        return None
    explanations = [f"前回の測定（{base['occurred_at']:%Y-%m-%d}）からマージした工場の PR {len(merged)} 件" if base else "前回の測定がない（初回）"]
    explanations += [f"{f['ticket']}: {f['kpi']} の見込み {f['forecast']:+g}{f['unit']}、実測 {_fmt(f['actual'], f['unit'])}"
                     for r in rows for f in r["forecasts"]]
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values ('KPIReport', %s, 'routine', %s)",
                (f"kpi:{repo}:{week}", Jsonb({"repo": repo, "env": env, "period": week, "measured_id": ev["id"],
                                              "metrics": rows, "diff_from_previous": {r["id"]: r["delta"] for r in rows},
                                              "explanations": explanations})))
    for o in opps:   # 改善の提案の口（opportunity.py、O4 と共通）。同じ測定・同じ値・同じ種類の異常は 1 回だけ
        opportunity.propose(cur, repo, SOURCE, f"{ev['id']}:{o['expected_effect']['metric']}:{o['evidence']['anomaly']}",
                            title=o["title"], problem=o["problem"], expected_effect=o["expected_effect"], theme=THEME,
                            evidence=[o["evidence"]], correlation_id=f"kpi:{repo}:{week}", extra={"week": week})
    log("kpi weekly", repo, week, "anomalies", sum(len(r["anomalies"]) for r in rows), "opportunities", len(opps), "scored", len(scored))
    return {"repo": repo, "week": week, "rows": rows, "opportunities": opps, "scored": scored}


def report_step(cur, log=print) -> list[dict]:
    out = []
    for ev in pending(cur):
        r = report_one(cur, ev, log=log)
        if r:
            out.append(r)
    return out


# ------------------------------------------------------------------ 読む側（開発状況レポート・factoryctl）
def latest(cur, repo: str, before: datetime | None = None) -> dict | None:
    cur.execute("select occurred_at, payload from events where name='report.kpi_weekly' and payload->>'repo'=%s "
                "and (%s::timestamptz is null or occurred_at < %s) order by id desc limit 1", (repo, before, before))
    r = cur.fetchone()
    return {**r["payload"], "at": r["occurred_at"]} if r else None


def lines(rep: dict | None) -> list[str]:
    if not rep:
        return ["週次の KPI: まだない（factoryctl report kpi で測れる）"]
    out = [f"週次の KPI（{rep['repo']} {rep['week']}、{rep['env']}）: 異常 {rep['anomalies']} 件、Opportunity の下書き {rep['opportunities']} 件、"
           f"見込みの採点 {rep['scored']} 件"]
    for m in rep.get("metrics") or []:
        d = "" if m.get("delta") is None else f"（前回比 {round(m['delta'], 4):+g}{m.get('unit') or ''}）"
        mark = f"  ← {'・'.join(m['anomalies'])}" if m.get("anomalies") else ""
        out.append(f"  {m['ja']}: {_fmt(m.get('value'), m.get('unit') or '')}{d}{mark}")
    return out
