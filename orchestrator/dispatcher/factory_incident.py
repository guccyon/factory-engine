"""工場の事故の台帳と、日次の決定論の見回り（ADR 0046）。形は definitions/incident.yaml と definitions/events.yaml。

事故＝工場の振る舞いが原因で、事業主に実際のお金がかかった出来事。ラインの停止や人への承認依頼（可用性）は見ない。
製品の障害（Incident、S1〜S4。incident.py）とは別のもの。

- 日次の見回り（daily_step）: 1 日 1 回（definitions/incident.yaml の committee.patrol.daily.hour_jst を過ぎたら）、今の予算の日の記録を決定論で集計し、
  資源ごとに兆し（fincident.sign）を残す。LLM は使わない（見回りの体制の整理の P4）。見た結果は 1 日 1 件の fincident.patrolled
    - トークン: その日の消費 ÷ 予算の枠、ジョブの種類ごとの費用（枠の 30% 以上で、直近 14 日の中央値の 3 倍以上）、
      チケットごとの費用（直近 14 日の中央値の 5 倍以上で、枠の 5% 以上）
    - 契約の枠: 週の上限に対する消費の進み、契約の使用量上限に当たった回数
    - クラウドの従量課金: 使い捨て環境が壊す時期（EPHEMERAL_TTL_HOURS）を 12 時間過ぎても立っている
    - 外部サービスの API: まだ測っていない（記録の口がない）。見た結果に「測っていない」と残す
- 台帳（open_case / update_case / close_case）: 影響の帯が medium 以上の兆しは、台帳に 1 件開く（fincident.opened、id は IC-YYYYMMDD-N）。
  台帳は工場の中だけの生の事実で、金額や製品の名前を含んでよい。配る事故記録（FIR）は incidents/ に抽象化して別に書く。
  Gen1 は台帳を events に置く（新しい表を作らない）
- 委員会は直さない（ADR 0046 の 6）。台帳を見て Engine の改善 Issue にするのは調査（次の段）か人

記録の失敗で supervisor の周回を止めない。
"""
from __future__ import annotations

import os
import statistics
from datetime import datetime, timedelta, timezone

import yaml
from psycopg.types.json import Jsonb

import ephemeral
import usage

JST = timezone(timedelta(hours=9))
DEFS_PATH = os.environ.get("INCIDENT_DEFINITIONS", "/definitions/incident.yaml")
SEVERITIES = ("critical", "high", "medium", "low")
OPEN_FROM = ("critical", "high", "medium")   # definitions/incident.yaml の record_from（medium 以上を台帳に開く）
EVENTS = ("fincident.patrolled", "fincident.sign", "fincident.opened", "fincident.updated", "fincident.closed")
# 閾値（definitions/incident.yaml の severity を数にしたもの。仮置き）
KIND_SHARE_OF_BUDGET = 0.30
KIND_X_MEDIAN = 3.0
TICKET_X_MEDIAN = 5.0
TICKET_MIN_SHARE_OF_BUDGET = 0.05
HISTORY_DAYS = 14
LEAK_GRACE_HOURS = 12
QUOTA_AHEAD = 0.15                           # 週の上限の進みが、経過の割合よりこれ以上先なら兆し


def load_defs(path: str | None = None) -> dict:
    try:
        with open(path or DEFS_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return {}


def _record(cur, name: str, payload: dict, dedupe_key: str | None = None, corr: str | None = None) -> int | None:
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values (%s,%s,%s,%s) on conflict (dedupe_key) do nothing returning id",
                (name, corr, Jsonb(payload), dedupe_key))
    r = cur.fetchone()
    return r["id"] if r else None


def _f(v) -> float:
    return float(v or 0)


def _median(xs: list[float]) -> float:
    xs = [x for x in xs if x > 0]
    return statistics.median(xs) if xs else 0.0


# ------------------------------------------------------------------ 集計（決定論）
def day_window(now: datetime) -> tuple:
    """今の予算の日（usage.DAY_START_HOUR 区切り）と、その始まり"""
    day = usage.budget_day(now)
    return day, datetime(day.year, day.month, day.day, usage.DAY_START_HOUR, tzinfo=JST)


def token_signs(cur, now: datetime) -> tuple[dict, list[dict]]:
    day, start = day_window(now)
    budget = usage.day_budget(day, usage.declared(cur))
    since = start - timedelta(days=HISTORY_DAYS)
    shift = f"{usage.DAY_START_HOUR} hours"
    cur.execute(
        "select ((e.occurred_at at time zone 'Asia/Tokyo') - %s::interval)::date as d, coalesce(j.kind, 'implement') as kind, "
        "coalesce(e.correlation_id, 'job:' || e.job_id::text) as corr, sum(coalesce((e.payload->>'cost_usd')::numeric, 0)) as usd, count(*) as jobs "
        "from events e left join jobs j on j.id = e.job_id where e.name = 'job.finished' and e.occurred_at >= %s and e.occurred_at <= %s group by 1, 2, 3",
        (shift, since, now))
    rows = cur.fetchall()
    today = [r for r in rows if r["d"] == day]
    past = [r for r in rows if r["d"] != day]
    spent = round(sum(_f(r["usd"]) for r in today), 2)
    facts = {"day": day.isoformat(), "spent_usd": spent, "budget_usd": budget, "jobs": int(sum(r["jobs"] for r in today))}
    signs: list[dict] = []
    if budget:
        ratio = round(spent / budget, 2)
        facts["cost_vs_daily_budget"] = ratio
        if ratio >= 1.0:
            signs.append({"key": "tokens:day", "resource": "llm_tokens", "viewpoints": ["PV-tokens-repeat", "PV-tokens-contention"],
                          "severity": "critical" if ratio >= 3.0 else "high",
                          "title": f"その日の消費が予算の枠の {ratio} 倍", "facts": {"cost_vs_daily_budget": ratio, "spent_usd": spent, "budget_usd": budget}})
    # ジョブの種類ごと
    by_kind_today: dict[str, float] = {}
    for r in today:
        by_kind_today[r["kind"]] = by_kind_today.get(r["kind"], 0) + _f(r["usd"])
    by_kind_past: dict[str, dict] = {}
    for r in past:
        by_kind_past.setdefault(r["kind"], {}).setdefault(r["d"], 0.0)
        by_kind_past[r["kind"]][r["d"]] += _f(r["usd"])
    facts["by_kind"] = {k: round(v, 2) for k, v in sorted(by_kind_today.items(), key=lambda x: -x[1])}
    for kind, usd in by_kind_today.items():
        med = _median(list((by_kind_past.get(kind) or {}).values()))
        if budget and usd >= budget * KIND_SHARE_OF_BUDGET and (med == 0 or usd >= med * KIND_X_MEDIAN):
            signs.append({"key": f"tokens:kind:{kind}", "resource": "llm_tokens", "viewpoints": ["PV-tokens-repeat", "PV-tokens-contention", "PV-tokens-self-feedback"],
                          "severity": "medium", "title": f"{usage.KIND_JA.get(kind, kind)}のジョブが枠の {round(usd / budget * 100)}% を使った",
                          "facts": {"kind": kind, "usd": round(usd, 2), "share_of_budget": round(usd / budget, 2), "median_usd": round(med, 2)}})
    # チケットごと
    by_corr_today: dict[str, dict] = {}
    for r in today:
        x = by_corr_today.setdefault(r["corr"], {"usd": 0.0, "jobs": 0, "kinds": set()})
        x["usd"] += _f(r["usd"])
        x["jobs"] += int(r["jobs"])
        x["kinds"].add(r["kind"])
    per_ticket_day: dict[tuple, float] = {}
    for r in past:
        per_ticket_day[(r["d"], r["corr"])] = per_ticket_day.get((r["d"], r["corr"]), 0) + _f(r["usd"])
    med = _median(list(per_ticket_day.values()))
    facts["ticket_median_usd"] = round(med, 2)
    for corr, x in sorted(by_corr_today.items(), key=lambda kv: -kv[1]["usd"]):
        if med and budget and x["usd"] >= med * TICKET_X_MEDIAN and x["usd"] >= budget * TICKET_MIN_SHARE_OF_BUDGET:
            signs.append({"key": f"tokens:ticket:{corr}", "resource": "llm_tokens", "viewpoints": ["PV-tokens-repeat", "PV-tokens-double-pay"],
                          "severity": "medium", "title": f"1 つのチケットがその日に中央値の {round(x['usd'] / med, 1)} 倍を使った",
                          "facts": {"correlation_id": corr, "usd": round(x["usd"], 2), "jobs": x["jobs"], "kinds": sorted(x["kinds"]),
                                    "wasted_vs_median": round(x["usd"] / med, 1)}})
    return facts, signs


def quota_signs(cur, now: datetime) -> tuple[dict, list[dict]]:
    v = usage.view(cur, now, days=1)
    wk = v["week"]
    facts = {"week_share": wk.get("share"), "elapsed_days": wk.get("elapsed_days"), "usage_limited": wk.get("usage_limited")}
    signs = []
    if wk.get("share") is not None and wk["share"] > wk["elapsed_days"] / 7 + QUOTA_AHEAD:
        signs.append({"key": "quota:week", "resource": "plan_quota", "viewpoints": ["PV-quota-contention"], "severity": "low",
                      "title": f"週の上限の {round(wk['share'] * 100)}% を、週の {round(wk['elapsed_days'] / 7 * 100)}% の時点で使った", "facts": facts})
    if wk.get("usage_limited"):
        signs.append({"key": "quota:limited", "resource": "plan_quota", "viewpoints": ["PV-quota-contention"], "severity": "low",
                      "title": f"今週、契約の使用量上限に {wk['usage_limited']} 回当たった", "facts": facts})
    return facts, signs


def cloud_signs(cur, now: datetime) -> tuple[dict, list[dict]]:
    act = ephemeral.active(cur)
    late = [a for a in act if (now - a["at"]).total_seconds() > (ephemeral.TTL_HOURS + LEAK_GRACE_HOURS) * 3600]
    facts = {"ephemeral_active": len(act), "ephemeral_overdue": len(late)}
    signs = [{"key": f"cloud:ephemeral:{a['repo']}#{a['pr']}", "resource": "cloud_metered", "viewpoints": ["PV-cloud-leak"], "severity": "low",
              "title": "使い捨て環境が壊す時期を過ぎても立っている",
              "facts": {"repo": a["repo"], "pr": a["pr"], "up_hours": round((now - a["at"]).total_seconds() / 3600, 1)}} for a in late]
    return facts, signs


def patrol(cur, now: datetime | None = None) -> dict:
    """見回りの 1 回分。記録はしない（daily_step と factoryctl incident-record patrol が使う）"""
    now = (now or datetime.now(JST)).astimezone(JST)
    tf, ts = token_signs(cur, now)
    qf, qs = quota_signs(cur, now)
    cf, cs = cloud_signs(cur, now)
    return {"at": now.isoformat(), "day": tf["day"], "llm_tokens": tf, "plan_quota": qf, "cloud_metered": cf,
            "external_api": {"measured": False, "note": "外部サービスの API の呼び出しと Rate Limit を残す口がまだない"},
            "signs": ts + qs + cs}


def daily_step(cur, now: datetime | None = None, defs: dict | None = None) -> dict | None:
    """1 日 1 回の見回り。兆しを残し、medium 以上は台帳に開く。返り値は見た結果（今日はもう見た・まだ時刻でないなら None）"""
    defs = defs if defs is not None else load_defs()
    daily = ((defs.get("committee") or {}).get("patrol") or {}).get("daily") or {}
    if daily.get("enabled") is False:
        return None
    now = (now or datetime.now(JST)).astimezone(JST)
    if now.hour < int(daily.get("hour_jst", 4)):
        return None
    key = f"fincident.patrolled:{now:%Y-%m-%d}"
    cur.execute("select 1 from events where dedupe_key=%s", (key,))
    if cur.fetchone():
        return None
    snap = patrol(cur, now)
    opened = []
    for s in snap["signs"]:
        sid = _record(cur, "fincident.sign", {**s, "day": snap["day"], "by": "patrol.daily"}, f"fincident.sign:{snap['day']}:{s['key']}")
        if sid and s["severity"] in OPEN_FROM:
            c = open_case(cur, "patrol.daily", s["title"], s["resource"], s["severity"], s["facts"], viewpoints=s["viewpoints"],
                          sign_ids=[sid], dedupe=f"fincident.opened:{snap['day']}:{s['key']}", now=now)
            if c:
                opened.append(c)
    _record(cur, "fincident.patrolled", {**{k: v for k, v in snap.items() if k != "signs"}, "signs": len(snap["signs"]), "opened": opened}, key)
    return {**snap, "opened": opened}


# ------------------------------------------------------------------ 台帳
def _next_id(cur, now: datetime) -> str:
    prefix = f"IC-{now.astimezone(JST):%Y%m%d}-"
    cur.execute("select count(*) as n from events where name='fincident.opened' and payload->>'id' like %s", (prefix + "%",))
    return f"{prefix}{int(cur.fetchone()['n']) + 1}"


def open_case(cur, by: str, title: str, resource: str, severity: str, facts: dict | None = None, *, mechanism: str | None = None,
              viewpoints: list[str] | None = None, sign_ids: list[int] | None = None, note: str = "", dedupe: str | None = None,
              now: datetime | None = None) -> str | None:
    """台帳に 1 件開く。返り値は台帳の id（同じ dedupe で開いていれば None）"""
    if severity not in SEVERITIES:
        raise ValueError(f"severity は {'・'.join(SEVERITIES)} のどれか: {severity}")
    defs = load_defs()
    if defs.get("resources") and resource not in defs["resources"]:
        raise ValueError(f"resource は {'・'.join(defs['resources'])} のどれか: {resource}")
    if mechanism and defs.get("mechanisms") and mechanism not in defs["mechanisms"]:
        raise ValueError(f"mechanism は {'・'.join(defs['mechanisms'])} のどれか: {mechanism}")
    now = now or datetime.now(JST)
    cid = _next_id(cur, now)
    got = _record(cur, "fincident.opened", {"id": cid, "by": by, "title": title[:300], "resource": resource, "severity": severity,
                                            "mechanism": mechanism, "viewpoints": viewpoints or [], "facts": facts or {}, "sign_ids": sign_ids or [],
                                            "note": note[:2000]}, dedupe or f"fincident.opened:{cid}", corr=f"fincident:{cid}")
    return cid if got else None


def update_case(cur, cid: str, by: str, **fields) -> bool:
    """調べた結果（mechanism・severity・note・engine_issue・fir など）を足す。台帳は消さず、足した記録を重ねる"""
    if not get_case(cur, cid):
        return False
    if "severity" in fields and fields["severity"] not in SEVERITIES:
        raise ValueError(f"severity は {'・'.join(SEVERITIES)} のどれか")
    _record(cur, "fincident.updated", {"id": cid, "by": by, **{k: v for k, v in fields.items() if v is not None}}, corr=f"fincident:{cid}")
    return True


def close_case(cur, cid: str, by: str, reason: str) -> bool:
    c = get_case(cur, cid)
    if not c or c["status"] == "closed":
        return False
    _record(cur, "fincident.closed", {"id": cid, "by": by, "reason": reason[:1000]}, f"fincident.closed:{cid}", corr=f"fincident:{cid}")
    return True


def get_case(cur, cid: str) -> dict | None:
    cur.execute("select name, payload, occurred_at from events where correlation_id=%s and name = any(%s) order by id",
                (f"fincident:{cid}", ["fincident.opened", "fincident.updated", "fincident.closed"]))
    rows = cur.fetchall()
    if not rows or rows[0]["name"] != "fincident.opened":
        return None
    c = {**rows[0]["payload"], "opened_at": rows[0]["occurred_at"].isoformat(), "status": "open", "history": []}
    for r in rows[1:]:
        p = {k: v for k, v in r["payload"].items() if k not in ("id",)}
        c["history"].append({"name": r["name"], "at": r["occurred_at"].isoformat(), **p})
        if r["name"] == "fincident.updated":
            c.update({k: v for k, v in p.items() if k != "by"})
        else:
            c["status"], c["closed_reason"] = "closed", p.get("reason")
    return c


def list_cases(cur, status: str | None = "open", limit: int = 50) -> list[dict]:
    cur.execute("select payload->>'id' as id from events where name='fincident.opened' order by id desc limit %s", (limit * 4,))
    out = []
    for r in cur.fetchall():
        c = get_case(cur, r["id"])
        if c and (status in (None, "all") or c["status"] == status):
            out.append(c)
        if len(out) >= limit:
            break
    return out


def report_view(cur, now: datetime | None = None) -> dict:
    """日報の 1 節（昨日から今日の見回り）。直近の fincident.patrolled と、開いている台帳"""
    now = now or datetime.now(JST)
    cur.execute("select payload, occurred_at from events where name='fincident.patrolled' and occurred_at >= %s order by id desc limit 1",
                (now - timedelta(hours=30),))
    r = cur.fetchone()
    cur.execute("select payload from events where name='fincident.sign' and occurred_at >= %s order by id", (now - timedelta(hours=30),))
    signs = [x["payload"] for x in cur.fetchall()]
    return {"patrolled": r["payload"] if r else None, "signs": signs,
            "open": [{"id": c["id"], "title": c["title"], "severity": c["severity"], "resource": c["resource"]} for c in list_cases(cur, "open", 20)]}
