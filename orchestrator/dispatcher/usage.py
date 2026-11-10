"""工場の使用量（トークンの消費と費用）を、制御プレーンの記録から数える。

操業コンソールの「使用量」（GET /api/usage、/usage）と `factoryctl report usage` が同じものを読む。
読み手は、工場を使う企業でアカウント（Claude の契約）を足すかを決める人。数字だけで判断できるよう、
契約の使用量上限に当たった回数と、予算の枠で投入を止めた時間を、消費と並べて出す。LLM は使わない。

- 取得元: events の job.finished（ジョブごとの費用とトークン）、session.cost_recorded（モデルとトークンの内訳）、
  stoppages の budget（予算の枠で止めた時間）。jobs の kind で役割に分ける
- 費用は API 単価に換算した値（ワーカーの CLI が出す cost_usd）。定額の契約では実請求ではなく、消費の物差し
- 日の区切りは予算の枠と同じ 7:00 JST（BUDGET_DAY_START_HOUR）。週の区切りは契約の週の上限がリセットされる時刻（PLAN_WEEK_RESET）
- 予算の枠の読み方は supervisor の投入の止め方と同じ（budget_windows）。値の正本はこの 1 か所
- 予算は宣言（events の budget.set。操業コンソールの設定・factoryctl budget）が正本。一度も宣言していなければ
  環境変数 BUDGET_WINDOWS_WEEKDAY / BUDGET_WINDOWS_WEEKEND の枠（ADR 0029 の追記）
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

ENV = os.environ
JST = timezone(timedelta(hours=9))
DEFAULT_WINDOWS_WEEKDAY = "07:10,12:15,17:35,22:25,03:15"
DEFAULT_WINDOWS_WEEKEND = "07:20,12:20,17:20,22:20,03:20"
DAY_START_HOUR = int(ENV.get("BUDGET_DAY_START_HOUR", "7"))   # 予算の日の区切り（JST）。Max/Ultra の使用量が 7:00 から数え直されるのに合わせた
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# 契約の週の上限がリセットされる曜日と時刻（JST）。Ultra は日曜 11:00（2026-11-04 オーナー）
PLAN_WEEK_RESET = ENV.get("PLAN_WEEK_RESET", "sun 11:00")
# 契約の週の上限を API 単価に換算した目安（USD）。わからなければ空のまま（割合と見込みを出さない）
PLAN_WEEKLY_LIMIT_USD = float(ENV["PLAN_WEEKLY_LIMIT_USD"]) if ENV.get("PLAN_WEEKLY_LIMIT_USD") else None

KIND_JA = {
    "implement": "実装", "review_fix": "指摘の直し", "conflict": "衝突の解消", "body_fix": "PR 本文の直し",
    "inspect": "検査", "judge": "判定（judge）", "clarify": "要求の確認", "plan": "プランナー",
    "ui_design": "デザイナー", "ui_design_review": "デザイン検査", "design_system": "デザインシステム",
    "design_review": "設計の診断", "case_review": "事例の分析", "committee_review": "知見の委員会",
    "committee_weekly": "委員会の定例",
    "incident_review": "事故の見回り",
}


def windows_spec(day: datetime) -> str:
    """その日（JST）の予算の枠の並び（環境変数。空なら予算で止めない）。宣言があればそちらが優先（windows）"""
    if day.weekday() >= 5:
        return ENV.get("BUDGET_WINDOWS_WEEKEND", DEFAULT_WINDOWS_WEEKEND)
    return ENV.get("BUDGET_WINDOWS_WEEKDAY", DEFAULT_WINDOWS_WEEKDAY)


def parse_windows(spec: str) -> list[tuple[int, float]]:
    """「開始時:ドル」の並びを (時, ドル) の昇順にする"""
    out = []
    for part in (spec or "").split(","):
        hm, _, usd = part.strip().partition(":")
        if hm and usd:
            out.append((int(hm), float(usd)))
    return sorted(out)


# ---- 予算の宣言（2026-11-08 オーナー「使用量の予算も設定で変更できるようにしたい。全体で 1 日あたりの予算を決めるか、曜日べつで決めるか」）
# 1 日は DAY_START_HOUR（7:00 JST）から翌日の同じ時まで。宣言の予算はその 1 日を 1 つの枠にする（5 時間ごとの枠には分けない）。
# 曜日は 1 日が始まった日の曜日（月曜 7:00〜火曜 7:00 が月曜の予算）。もっと柔軟な当て方は、この先で形を足す
BUDGET_EVENT = "budget.set"
BUDGET_MODES = ("daily", "weekday")   # daily: 毎日同じ額、weekday: 曜日ごとの額
BUDGET_MODE_JA = {"daily": "毎日同じ額", "weekday": "曜日ごと", "env": "環境変数の枠（宣言なし）"}
WEEKDAYS_JA = {"mon": "月", "tue": "火", "wed": "水", "thu": "木", "fri": "金", "sat": "土", "sun": "日"}
BUDGET_MAX_USD = 100000.0


def declared(cur) -> dict | None:
    """いちばん新しい予算の宣言（なければ None）。{mode, daily_usd, weekday_usd, by, reason, at}"""
    cur.execute("select payload, occurred_at from events where name=%s order by id desc limit 1", (BUDGET_EVENT,))
    r = cur.fetchone()
    if not r or r["payload"].get("mode") not in BUDGET_MODES:
        return None
    p = r["payload"]
    return {"mode": p["mode"], "daily_usd": float(p.get("daily_usd") or 0),
            "weekday_usd": {k: float((p.get("weekday_usd") or {}).get(k) or 0) for k in WEEKDAYS},
            "by": p.get("by"), "reason": p.get("reason") or "", "at": r["occurred_at"].isoformat()}


def declared_amount(decl: dict, day) -> float:
    """宣言のもとで、その日（予算の 1 日が始まった日）の予算"""
    if decl["mode"] == "weekday":
        return decl["weekday_usd"][WEEKDAYS[day.weekday()]]
    return decl["daily_usd"]


def windows(day: datetime, decl: dict | None = None) -> list[tuple[int, float]]:
    """その日（JST）に始まる枠 (時, ドル)。宣言があれば 1 日 1 枠（DAY_START_HOUR から）、なければ環境変数の枠"""
    if decl:
        return [(DAY_START_HOUR, declared_amount(decl, day))]
    return parse_windows(windows_spec(day))


def budget_spec(cur, now: datetime | None = None) -> dict:
    """いまの予算の決め方。宣言がなければ mode は env で、額は環境変数の枠から 1 日の合計を出した値（設定の画面の初めの値）"""
    decl = declared(cur)
    now = (now or datetime.now(JST)).astimezone(JST)
    if decl:
        out = dict(decl)
    else:
        base = budget_day(now)
        per_day = {}
        for i in range(7):
            d = base + timedelta(days=i)
            per_day[WEEKDAYS[d.weekday()]] = day_budget(d)
        today = day_budget(base)
        out = {"mode": "env", "daily_usd": today, "weekday_usd": per_day, "by": "env", "reason": "", "at": None,
               "env": {"weekday": ENV.get("BUDGET_WINDOWS_WEEKDAY", DEFAULT_WINDOWS_WEEKDAY),
                       "weekend": ENV.get("BUDGET_WINDOWS_WEEKEND", DEFAULT_WINDOWS_WEEKEND)}}
    out["today_usd"] = day_budget(budget_day(now), decl)
    out["day_start_hour"] = DAY_START_HOUR
    return out


def _usd(v, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= BUDGET_MAX_USD:
        raise ValueError(f"{name} は 0〜{BUDGET_MAX_USD:,.0f} のドル")
    return round(float(v), 2)


def set_budget(cur, by: str, reason: str = "", mode: str | None = None, daily_usd=None, weekday_usd: dict | None = None) -> dict:
    """予算を宣言する。渡したものだけ変え、ほかは今の値のまま（決め方を切り替えても、もう一方の額は残る）。返り値は新しい決め方。
    supervisor は周回ごとに読むので、上げ直さずに次の周回から効く"""
    cur_spec = budget_spec(cur)
    new_mode = mode or (cur_spec["mode"] if cur_spec["mode"] in BUDGET_MODES else "daily")
    if new_mode not in BUDGET_MODES:
        raise ValueError(f"mode は {' か '.join(BUDGET_MODES)}")
    daily = cur_spec.get("daily_usd") or 0.0
    week = {k: (cur_spec["weekday_usd"].get(k) or 0.0) for k in WEEKDAYS}
    if daily_usd is not None:
        daily = _usd(daily_usd, "daily_usd")
    for k, v in (weekday_usd or {}).items():
        if k not in WEEKDAYS:
            raise ValueError(f"曜日は {'・'.join(WEEKDAYS)}")
        if v is not None:
            week[k] = _usd(v, k)
    cur.execute("insert into events(name, payload) values (%s, %s)",
                (BUDGET_EVENT, Jsonb({"mode": new_mode, "daily_usd": daily, "weekday_usd": week, "by": by, "reason": reason})))
    return budget_spec(cur)


def budget_text(spec: dict) -> str:
    """決め方を 1 行で（factoryctl と知らせの文言）"""
    if spec["mode"] == "daily":
        body = f"毎日 ${spec['daily_usd']:,.0f}"
    elif spec["mode"] == "weekday":
        body = "曜日ごと " + " ".join(f"{WEEKDAYS_JA[k]} ${spec['weekday_usd'][k]:,.0f}" for k in WEEKDAYS)
    else:
        body = f"環境変数の枠（平日 {spec['env']['weekday'] or 'なし'}、週末 {spec['env']['weekend'] or 'なし'}）"
    return f"{body}（{spec['day_start_hour']}:00 区切り）"


def budget_windows(now: datetime, decl: dict | None = None) -> tuple[datetime, float] | None:
    """今（JST）の使用量の枠の開始時刻と予算。枠は前日からまたぐ（22 時の枠は翌 3 時まで）。週末は開始した日で決める"""
    for back in (0, 1):
        day = now - timedelta(days=back)
        for h, usd in reversed(windows(day, decl)):
            start = day.replace(hour=h, minute=0, second=0, microsecond=0)
            if start <= now:
                return start, usd
    return None


def day_budget(d, decl: dict | None = None) -> float | None:
    """予算の日（d の DAY_START_HOUR から翌日の同じ時まで）の予算の合計。枠がなければ None"""
    start = datetime(d.year, d.month, d.day, DAY_START_HOUR, tzinfo=JST)
    total, found = 0.0, False
    for day in (start, start + timedelta(days=1)):
        for h, usd in windows(day, decl):
            t = day.replace(hour=h, minute=0)
            if start <= t < start + timedelta(days=1):
                total, found = total + usd, True
    return round(total, 2) if found else None


def budget_day(t: datetime):
    """時刻が属する予算の日"""
    return (t.astimezone(JST) - timedelta(hours=DAY_START_HOUR)).date()


def week_start(now: datetime, spec: str = PLAN_WEEK_RESET) -> datetime:
    """now を含む契約の週の始まり（直近のリセット時刻）"""
    wd, _, hm = spec.strip().lower().partition(" ")
    h, _, m = (hm or "0:0").partition(":")
    now = now.astimezone(JST)
    t = now.replace(hour=int(h), minute=int(m or 0), second=0, microsecond=0) - timedelta(days=(now.weekday() - WEEKDAYS.index(wd[:3])) % 7)
    return t if t <= now else t - timedelta(days=7)


def _f(v) -> float:
    return float(v or 0)


def view(cur, now: datetime | None = None, days: int = 14) -> dict:
    """使用量の 1 枚。cur は dict_row のカーソル"""
    now = (now or datetime.now(JST)).astimezone(JST)
    today = budget_day(now)
    first = today - timedelta(days=days - 1)
    since = datetime(first.year, first.month, first.day, DAY_START_HOUR, tzinfo=JST)
    shift = f"{DAY_START_HOUR} hours"
    decl = declared(cur)   # 過去の日も今の決め方で予算の線を引く（日ごとの宣言の履歴はまだ見ない）

    cur.execute(
        "select ((e.occurred_at at time zone 'Asia/Tokyo') - %s::interval)::date as d, count(*) as jobs, "
        "sum(coalesce((e.payload->>'cost_usd')::numeric, 0)) as usd, "
        "sum(coalesce((e.payload->>'billable_tokens')::numeric, 0)) as billable, sum(coalesce((e.payload->>'total_tokens')::numeric, 0)) as total, "
        "sum(case when e.payload->>'state' = 'failed' then coalesce((e.payload->>'cost_usd')::numeric, 0) else 0 end) as failed_usd, "
        "count(*) filter (where (e.payload->>'usage_limited')::boolean) as limited "
        "from events e where e.name = 'job.finished' and e.occurred_at >= %s and e.occurred_at <= %s group by 1",
        (shift, since, now))
    by_day = {r["d"]: r for r in cur.fetchall()}
    cur.execute(
        "select ((greatest(started_at, %s) at time zone 'Asia/Tokyo') - %s::interval)::date as d, "
        "sum(extract(epoch from least(coalesce(ended_at, %s), %s) - greatest(started_at, %s))) as sec "
        "from stoppages where category = 'budget' and coalesce(ended_at, %s) > %s group by 1",
        (since, shift, now, now, since, now, since))
    stopped = {r["d"]: _f(r["sec"]) for r in cur.fetchall()}
    daily = []
    for i in range(days):
        d = first + timedelta(days=i)
        r = by_day.get(d) or {}
        daily.append({"date": d.isoformat(), "jobs": int(r.get("jobs") or 0), "cost_usd": round(_f(r.get("usd")), 2),
                      "billable_tokens": int(_f(r.get("billable"))), "total_tokens": int(_f(r.get("total"))),
                      "failed_cost_usd": round(_f(r.get("failed_usd")), 2), "usage_limited": int(r.get("limited") or 0),
                      "budget_usd": day_budget(d, decl), "budget_stopped_hours": round(stopped.get(d, 0) / 3600, 1)})

    last7 = now - timedelta(days=7)
    cur.execute(
        "select coalesce(j.kind, 'implement') as kind, count(*) as jobs, sum(coalesce((e.payload->>'cost_usd')::numeric, 0)) as usd, "
        "sum(coalesce((e.payload->>'billable_tokens')::numeric, 0)) as billable "
        "from events e join jobs j on j.id = e.job_id where e.name = 'job.finished' and e.occurred_at >= %s group by 1 order by usd desc",
        (last7,))
    kinds = cur.fetchall()
    k_total = sum(_f(r["usd"]) for r in kinds) or 1
    by_kind = [{"kind": r["kind"], "ja": KIND_JA.get(r["kind"], r["kind"]), "jobs": int(r["jobs"]), "cost_usd": round(_f(r["usd"]), 2),
                "billable_tokens": int(_f(r["billable"])), "share": round(_f(r["usd"]) / k_total, 3)} for r in kinds]

    cur.execute(
        "select coalesce(payload->>'model', '不明') as model, count(*) as n, sum(coalesce((payload->>'cost_usd')::numeric, 0)) as usd, "
        "sum(coalesce((payload->'usage'->>'input_tokens')::numeric, 0)) as inp, sum(coalesce((payload->'usage'->>'output_tokens')::numeric, 0)) as outp, "
        "sum(coalesce((payload->'usage'->>'cache_creation_input_tokens')::numeric, 0)) as cw, "
        "sum(coalesce((payload->'usage'->>'cache_read_input_tokens')::numeric, 0)) as cr "
        "from events where name = 'session.cost_recorded' and occurred_at >= %s group by 1 order by usd desc", (last7,))
    by_model = [{"model": r["model"], "sessions": int(r["n"]), "cost_usd": round(_f(r["usd"]), 2), "input_tokens": int(_f(r["inp"])),
                 "output_tokens": int(_f(r["outp"])), "cache_write_tokens": int(_f(r["cw"])), "cache_read_tokens": int(_f(r["cr"]))}
                for r in cur.fetchall()]

    ws = week_start(now)
    cur.execute(
        "select count(*) as jobs, sum(coalesce((payload->>'cost_usd')::numeric, 0)) as usd, sum(coalesce((payload->>'billable_tokens')::numeric, 0)) as billable, "
        "count(*) filter (where (payload->>'usage_limited')::boolean) as limited, "
        "max(occurred_at) filter (where (payload->>'usage_limited')::boolean) as last_limited "
        "from events where name = 'job.finished' and occurred_at >= %s and occurred_at <= %s", (ws, now))
    w = cur.fetchone() or {}
    elapsed = max((now - ws).total_seconds() / 86400, 1 / 24)
    spent = round(_f(w.get("usd")), 2)
    pace = spent / elapsed                       # 1 日あたり
    projected = round(pace * 7, 2)
    week = {"start": ws.isoformat(), "end": (ws + timedelta(days=7)).isoformat(), "elapsed_days": round(elapsed, 2),
            "jobs": int(w.get("jobs") or 0), "cost_usd": spent, "billable_tokens": int(_f(w.get("billable"))),
            "per_day_usd": round(pace, 2), "projected_usd": projected, "usage_limited": int(w.get("limited") or 0),
            "last_usage_limited": w["last_limited"].isoformat() if w.get("last_limited") else None,
            "limit_usd": PLAN_WEEKLY_LIMIT_USD, "share": None, "exhausted_at": None}
    if PLAN_WEEKLY_LIMIT_USD:
        week["share"] = round(spent / PLAN_WEEKLY_LIMIT_USD, 3)
        if pace > 0 and projected > PLAN_WEEKLY_LIMIT_USD:
            week["exhausted_at"] = (now + timedelta(days=max(PLAN_WEEKLY_LIMIT_USD - spent, 0) / pace)).isoformat()

    return {"generated_at": now.isoformat(), "day_start_hour": DAY_START_HOUR, "budget": budget_spec(cur, now), "daily": daily, "by_kind": by_kind,
            "by_model": by_model, "week": week, "signals": signals(daily, week)}


def signals(daily: list[dict], week: dict) -> list[dict]:
    """アカウントを足すかの判断の材料。決まった規則だけで出す（推測はしない）"""
    out = []
    if week["usage_limited"]:
        out.append({"level": "bad", "text": f"今週、契約の使用量上限に {week['usage_limited']} 回当たってジョブが止まった。アカウントを足すか、流量を下げる"})
    if week.get("exhausted_at"):
        out.append({"level": "warn", "text": f"このペース（1 日 ${week['per_day_usd']:,.0f}）だと、週の上限の目安に週の終わりより前に届く"})
    recent = daily[-7:]
    stop_h = sum(d["budget_stopped_hours"] for d in recent)
    if stop_h >= 1:
        out.append({"level": "warn", "text": f"直近 7 日で、予算の枠を使い切って新しいタスクを止めた時間が計 {stop_h:.1f} 時間ある。流したいのに止まっているなら、予算かアカウントを足す"})
    over = [d for d in recent if d["budget_usd"] and d["cost_usd"] < d["budget_usd"] * 0.5 and d["jobs"]]
    if len(over) >= 4 and not out:   # 上限・止めた時間の知らせがあるときは出さない（食い違って読めるため）
        out.append({"level": "ok", "text": f"直近 7 日のうち {len(over)} 日は予算の半分も使っていない。アカウントを足す必要はまだない"})
    if not out:
        out.append({"level": "ok", "text": "上限に当たっておらず、予算の枠で止まった時間もない"})
    return out


def report_lines(v: dict) -> list[str]:
    """Markdown（factoryctl report usage）"""
    wk = v["week"]
    out = ["## 判断の材料", *[f"- {s['text']}" for s in v["signals"]], "",
           f"## 今週（{wk['start'][:16]} から）",
           f"- 消費 ${wk['cost_usd']:,.2f}（1 日 ${wk['per_day_usd']:,.2f}、週の見込み ${wk['projected_usd']:,.2f}）、課金対象 {wk['billable_tokens'] / 1e6:,.1f}M トークン、ジョブ {wk['jobs']}",
           f"- 契約の上限に当たった回数 {wk['usage_limited']}" + (f"、週の上限の目安 ${wk['limit_usd']:,.0f} の {wk['share'] * 100:.0f}%" if wk["limit_usd"] else ""), "",
           f"## 日ごと（{v['day_start_hour']}:00 区切り）", "", "| 日 | 費用 | 予算 | 課金対象 | ジョブ | 失敗の費用 | 予算で止めた時間 | 上限 |", "|---|---|---|---|---|---|---|---|"]
    for d in v["daily"]:
        b = f"${d['budget_usd']:,.0f}" if d["budget_usd"] else "—"
        out.append(f"| {d['date']} | ${d['cost_usd']:,.2f} | {b} | {d['billable_tokens'] / 1e6:,.1f}M | {d['jobs']} | ${d['failed_cost_usd']:,.2f} | {d['budget_stopped_hours']:.1f} | {d['usage_limited']} |")
    out += ["", "## 役割ごと（直近 7 日）", "", "| 役割 | 費用 | 割合 | 課金対象 | ジョブ |", "|---|---|---|---|---|"]
    out += [f"| {r['ja']} | ${r['cost_usd']:,.2f} | {r['share'] * 100:.0f}% | {r['billable_tokens'] / 1e6:,.1f}M | {r['jobs']} |" for r in v["by_kind"]]
    out += ["", "## モデルごと（直近 7 日）", "", "| モデル | 費用 | 入力 | 出力 | キャッシュ書き込み | キャッシュ読み込み |", "|---|---|---|---|---|---|"]
    out += [f"| {r['model']} | ${r['cost_usd']:,.2f} | {r['input_tokens'] / 1e6:,.2f}M | {r['output_tokens'] / 1e6:,.2f}M | {r['cache_write_tokens'] / 1e6:,.2f}M | {r['cache_read_tokens'] / 1e6:,.2f}M |"
            for r in v["by_model"]]
    return out
