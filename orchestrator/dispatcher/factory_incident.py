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
- 週次の見回り（weekly_due / weekly_input / enqueue_weekly）: 週 1 回（committee.patrol.weekly の曜日と時）、その週の記録を制御プレーンだけで組み立てて
  ジョブ（kind=incident_review、governance の枠）に積む。ワーカーの core-gates/incident_review.py が見回り＋調査・記録・査読の 3 つのエージェントを順に呼ぶ
- 週次の結果（record_weekly）: 所見で台帳を開く・足す。検査と査読を通った FIR の下書きは送り出し口（fincident.outboxed）に置き、
  Engine の改善 Issue の案（fincident.issue_proposed）を残す。起票は supervisor（file_issues）、送り出し口からの取り出しは人（factoryctl）
- 委員会は直さない（ADR 0046 の 6）。Engine の直しは改善 Issue にして建設者が直す

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
EVENTS = ("fincident.patrolled", "fincident.sign", "fincident.opened", "fincident.updated", "fincident.closed",
          "fincident.reviewed", "fincident.outboxed", "fincident.taken", "fincident.issue_proposed", "fincident.issue_filed")
KIND = "incident_review"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
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
    cur.execute("select payload from events where name='fincident.reviewed' and occurred_at >= %s order by id desc limit 1", (now - timedelta(days=7),))
    w = cur.fetchone()
    return {"patrolled": r["payload"] if r else None, "signs": signs,
            "open": [{"id": c["id"], "title": c["title"], "severity": c["severity"], "resource": c["resource"]} for c in list_cases(cur, "open", 20)],
            "weekly": w["payload"] if w else None, "outbox": len(outbox(cur, "pending"))}


# ------------------------------------------------------------------ 週次の見回り（ADR 0046 の順番 3・4）
def week_of(now: datetime) -> str:
    y, w, _ = now.astimezone(JST).isocalendar()
    return f"{y}-W{w:02d}"


def weekly_due(now: datetime, defs: dict) -> str | None:
    """その週の指定の曜日・時（JST）を過ぎていれば週の名前（2026-W46）。過ぎていなければ None"""
    wk = ((defs.get("committee") or {}).get("patrol") or {}).get("weekly") or {}
    if wk.get("enabled") is False:
        return None
    now = now.astimezone(JST)
    wd = WEEKDAYS.index(wk.get("weekday", "sunday"))
    if (now.weekday(), now.hour) < (wd, int(wk.get("hour_jst", 12))):
        return None
    return week_of(now)


def deny_names(repos: list[str]) -> list[str]:
    """製品の情報を抜く検査に渡す名前（制御プレーンの FACTORY_REPOS から。持ち主と名前の両方）"""
    out: list[str] = []
    for r in repos:
        for x in [r, *r.split("/")]:
            if len(x) >= 3 and x not in out:
                out.append(x)
    return out


def weekly_input(cur, now: datetime, repos: list[str]) -> dict:
    """週次の見回りの材料。制御プレーンの記録だけで組み立てる（ワーカーは制御プレーンに触れない）。台帳なので生の数字を含む"""
    now = now.astimezone(JST)
    since = now - timedelta(days=7)
    cur.execute("select payload from events where name='fincident.patrolled' and occurred_at >= %s order by id", (since,))
    patrols = [{k: v for k, v in r["payload"].items() if k in ("day", "llm_tokens", "plan_quota", "cloud_metered", "external_api", "signs", "opened")}
               for r in cur.fetchall()]
    cur.execute("select payload from events where name='fincident.sign' and occurred_at >= %s order by id", (since,))
    signs = [r["payload"] for r in cur.fetchall()]
    cases = list_cases(cur, "all", 30)   # 開いている件と、最近閉じた件（閉じた理由も調査の材料）
    shift = f"{usage.DAY_START_HOUR} hours"
    cur.execute(
        "select ((e.occurred_at at time zone 'Asia/Tokyo') - %s::interval)::date as d, coalesce(j.kind, 'implement') as kind, "
        "sum(coalesce((e.payload->>'cost_usd')::numeric, 0)) as usd, count(*) as jobs, "
        "count(*) filter (where e.payload->>'state' <> 'succeeded') as failed "
        "from events e left join jobs j on j.id = e.job_id where e.name = 'job.finished' and e.occurred_at >= %s and e.occurred_at <= %s group by 1, 2 order by 1, 2",
        (shift, now - timedelta(days=14), now))
    by_day = [{"day": r["d"].isoformat(), "kind": r["kind"], "usd": round(_f(r["usd"]), 2), "jobs": int(r["jobs"]), "failed": int(r["failed"])} for r in cur.fetchall()]
    cur.execute(
        "select coalesce(e.correlation_id, 'job:' || e.job_id::text) as corr, sum(coalesce((e.payload->>'cost_usd')::numeric, 0)) as usd, count(*) as jobs, "
        "array_agg(distinct coalesce(j.kind, 'implement')) as kinds, count(*) filter (where e.payload->>'state' <> 'succeeded') as failed "
        "from events e left join jobs j on j.id = e.job_id where e.name = 'job.finished' and e.occurred_at >= %s and e.occurred_at <= %s "
        "group by 1 order by 2 desc limit 10", (since, now))
    top = [{"correlation_id": r["corr"], "usd": round(_f(r["usd"]), 2), "jobs": int(r["jobs"]), "failed": int(r["failed"]), "kinds": sorted(r["kinds"])}
           for r in cur.fetchall()]
    days = sorted({x["day"] for x in by_day if x["day"] >= since.date().isoformat()})
    budgets = {d: usage.day_budget(datetime.fromisoformat(d).date(), usage.declared(cur)) for d in days}
    cur.execute("select payload->>'id' as id from events where name='fincident.outboxed'")
    return {"week": week_of(now), "from": since.isoformat(), "to": now.isoformat(),
            "spent_usd": round(sum(x["usd"] for x in by_day if x["day"] >= since.date().isoformat()), 2), "daily_budget_usd": budgets,
            "patrols": patrols, "signs": signs, "cases": cases, "cost_by_day_kind": by_day, "top_tickets": top,
            "ephemeral": [{"repo": a["repo"], "pr": a["pr"], "up_hours": round((now - a["at"]).total_seconds() / 3600, 1)} for a in ephemeral.active(cur)],
            "external_api": {"measured": False},
            "outbox_ids": [r["id"] for r in cur.fetchall()], "deny": deny_names(repos)}


def enqueue_weekly(cur, now: datetime, repos: list[str], defs: dict | None = None) -> dict | None:
    """週 1 回のジョブを積む。積んだら {week, job_id, corr}。今週はもう積んだ・まだ時刻でないなら None。
    ジョブは固定 Issue を持たない（issue_number 0）。結果は台帳と送り出し口に残し、Issue は Engine に起票する"""
    defs = defs if defs is not None else load_defs()
    week = weekly_due(now, defs)
    if not week or not repos:
        return None
    corr = f"fincident:weekly:{week}"
    cur.execute("select 1 from jobs where correlation_id=%s", (corr,))
    if cur.fetchone():
        return None
    inp = weekly_input(cur, now, repos)
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, model_tier, kind) values (%s,%s,0,'supervisor','tier-judge',%s) returning id",
                (corr, repos[0], KIND))
    jid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                (corr, jid, Jsonb({"by": "supervisor", "kind": KIND, "week": week, "input": inp})))
    return {"week": week, "job_id": jid, "corr": corr, "signs": len(inp["signs"]), "cases": len(inp["cases"])}


def record_weekly(cur, job_id: int, week: str, result: dict) -> dict:
    """週次の結果を残す（dispatcher が呼ぶ）。所見で台帳を開く・足す。通った下書きは送り出し口へ、Issue の案は supervisor の起票待ちへ"""
    rep = result.get("report") or {}
    opened, updated, outboxed, proposed = [], [], [], []
    by = "committee.weekly"
    for f in rep.get("findings") or []:
        if not f.get("is_incident"):
            continue
        mech = f.get("mechanism") if f.get("mechanism") != "unknown" else None
        note = f"{f.get('why') or ''}\n根拠: {f.get('evidence') or ''}".strip()
        if f.get("case"):
            if update_case(cur, f["case"], by, mechanism=mech, severity=f.get("severity"), note=note[:2000], fir=f.get("overlaps") or None):
                updated.append(f["case"])
        elif f.get("severity") in OPEN_FROM:
            c = open_case(cur, by, f.get("title") or "", f.get("resource"), f["severity"], {"evidence": f.get("evidence")}, mechanism=mech,
                          viewpoints=[f["viewpoint"]] if f.get("viewpoint", "free") != "free" else [], note=note,
                          dedupe=f"fincident.opened:{week}:{f.get('title')}")
            if c:
                f["case"] = c
                opened.append(c)
                if f.get("overlaps"):
                    update_case(cur, c, by, fir=f["overlaps"])
    by_title = {f.get("title"): f.get("case") for f in rep.get("findings") or []}
    for d in result.get("drafts") or []:
        cid = d.get("case") or by_title.get(d.get("title")) or None
        if not d.get("ok"):
            continue
        if _record(cur, "fincident.outboxed", {"id": d["id"], "case": cid, "week": week, "job_id": job_id, "title": d.get("title"),
                                               "resource": d.get("resource"), "mechanism": d.get("mechanism"), "severity": d.get("severity"),
                                               "yaml": d["yaml"]}, f"fincident.outboxed:{d['id']}", corr=f"fincident:{cid}" if cid else None):
            outboxed.append(d["id"])
            if cid:
                update_case(cur, cid, by, fir=d["id"])
        if d.get("issue") and d["issue"].get("title"):
            if _record(cur, "fincident.issue_proposed", {"key": d["id"], "fir": d["id"], "case": cid, "week": week, "title": d["issue"]["title"][:200],
                                                         "body": d["issue"].get("body") or ""}, f"fincident.issue_proposed:{d['id']}"):
                proposed.append(d["id"])
    checks = []
    for r in rep.get("check_reviews") or []:   # 専用のチェックの見直し（ADR 0046 の 5 の 3）。要らないものは外す Issue の案に
        checks.append({"name": r.get("name"), "keep": bool(r.get("keep")), "why": r.get("why") or ""})
        if r.get("keep") is False:
            key = f"check:{r.get('name')}"
            body = (f"事故の調査委員会が、見直しの期日（{r.get('review_by')}）の来た専用のチェックを外してよいと判断しました。\n\n"
                    f"- チェック: {r.get('name')}\n- 場所: {', '.join(r.get('where') or [])}\n- 関係する事故記録: {r.get('fir') or 'なし'}\n- 理由: {r.get('why') or ''}\n\n"
                    "外すときは、同じ PR で definitions/incident.yaml の dedicated_checks と事故記録の mitigation.dedicated_checks からも外す。")
            if _record(cur, "fincident.issue_proposed", {"key": key, "fir": r.get("fir"), "case": None, "week": week,
                                                         "title": f"専用のチェックを外す: {r.get('name')}"[:200], "body": body}, f"fincident.issue_proposed:{key}:{week}"):
                proposed.append(key)
    summary = {"week": week, "job_id": job_id, "summary": rep.get("summary") or "", "viewpoints": (result.get("facts") or {}).get("viewpoints") or [],
               "findings": len(rep.get("findings") or []), "incidents": sum(1 for f in rep.get("findings") or [] if f.get("is_incident")),
               "opened": opened, "updated": updated, "outboxed": outboxed, "issues_proposed": proposed,
               "rejected": [{"id": d.get("id"), "title": d.get("title"), "check": d.get("check") or [], "review": (d.get("review") or {}).get("problems") or []}
                            for d in result.get("drafts") or [] if not d.get("ok")],
               "new_viewpoints": rep.get("new_viewpoints") or [], "check_reviews": checks, "cost_usd": result.get("cost_usd")}
    _record(cur, "fincident.reviewed", summary, f"fincident.reviewed:{week}", corr=f"fincident:weekly:{week}")
    return summary


def pending_issues(cur, limit: int = 2) -> list[dict]:
    """起票を待つ Engine の改善 Issue の案（supervisor が起票する）"""
    cur.execute("select p.payload from events p where p.name='fincident.issue_proposed' and not exists "
                "(select 1 from events f where f.name='fincident.issue_filed' and f.payload->>'key' = p.payload->>'key' "
                "and f.payload->>'week' is not distinct from p.payload->>'week') order by p.id limit %s", (limit,))
    return [r["payload"] for r in cur.fetchall()]


def file_issues(cur, gh, engine_repo: str, label: str, limit: int = 2) -> list[str]:
    """案を Engine の改善 Issue にする。同じ題が開いていれば足さず、それを指す。台帳に Issue の URL を足す"""
    todo = pending_issues(cur, limit)
    if not todo:
        return []
    open_titles = {i.get("title", ""): i.get("html_url") for i in gh("GET", f"repos/{engine_repo}/issues?state=open&labels={label}&per_page=100") or []}
    filed = []
    for p in todo:
        url = open_titles.get(p["title"])
        dup = bool(url)
        if not url:
            src = f"事故記録の下書き {p['fir']}（送り出し口）" if p["key"] == p.get("fir") else "専用のチェックの見直し"
            body = p["body"] + f"\n\n---\n事故の調査委員会の週次の見回り（ADR 0046）が起票。{src}。直すのは建設者。"
            url = (gh("POST", f"repos/{engine_repo}/issues", {"title": p["title"], "body": body, "labels": [label]}) or {}).get("html_url")
        _record(cur, "fincident.issue_filed", {"key": p["key"], "fir": p.get("fir"), "week": p.get("week"), "case": p.get("case"), "issue": url, "duplicate": dup},
                f"fincident.issue_filed:{p['key']}:{p.get('week')}")
        if p.get("case") and url:
            update_case(cur, p["case"], "committee.weekly", engine_issue=url)
        filed.append(url)
    return filed


# ------------------------------------------------------------------ 送り出し口（ADR 0046 の 9）
def outbox(cur, status: str = "pending") -> list[dict]:
    """検査と査読を通った FIR の下書き。Gen1 は人が factoryctl で取り出し、Engine の incidents/ に PR で入れる（公開はオーナーの承認）。
    将来はメタ工場が吸い上げる。台帳の id（case）は配らない"""
    cur.execute("select o.payload, o.occurred_at, t.payload as taken from events o left join events t on t.name='fincident.taken' and t.payload->>'id' = o.payload->>'id' "
                "where o.name='fincident.outboxed' order by o.id")
    out = []
    for r in cur.fetchall():
        x = {**{k: v for k, v in r["payload"].items() if k != "case"}, "at": r["occurred_at"].isoformat(), "taken": bool(r["taken"])}
        if status == "all" or (status == "pending") != x["taken"]:
            out.append(x)
    return out


def take(cur, fid: str, by: str) -> bool:
    """送り出し口から取り出した印（人が集めた・メタ工場が吸い上げた）"""
    if not any(x["id"] == fid for x in outbox(cur, "all")):
        return False
    return bool(_record(cur, "fincident.taken", {"id": fid, "by": by}, f"fincident.taken:{fid}"))
