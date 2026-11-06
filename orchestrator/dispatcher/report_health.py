"""工場の健康状態のレポート（report.factory_health、ADR 0032）の組み立て。中身は仮置き（definitions/reports.yaml の status: provisional）。

朝の報告の操業タブと同じ数を、制御プレーンだけから出す。期間は引数で受けるので、ダッシュボードから任意の期間でも同じ中身を作れる。
節の id ごとに関数を 1 つ持つ。中身を決めるタスクでは、ここと reports.yaml の節の一覧を書き換える。
"""
from __future__ import annotations

import statistics
from datetime import datetime

from report import Report, Section, entry, metric

KIND = "report.factory_health"
TITLE = "工場の健康状態"
# definitions/reports.yaml の report.factory_health の節の写し（食い違いはテストで見る）
SECTIONS = {
    "line": ("ライン", ("provider", "business_owner")),
    "jobs": ("ジョブ", ("provider", "business_owner")),
    "inbox": ("人の判断待ち", ("provider", "business_owner")),
    "cost": ("費用", ("provider", "business_owner")),
}
DOWN = ("idle:", "drain:")   # ラインが動いていない時間に数える停止（ラインが空・一時停止）
OPEN_MAX = 5                 # 開いている停止は長い順にここまで載せる


def _merge(spans: list[tuple[datetime, datetime]]) -> float:
    """区間の重なりを除いた和（秒）"""
    total, cur = 0.0, None
    for a, b in sorted(spans):
        if b <= a:
            continue
        if cur and a <= cur[1]:
            cur = (cur[0], max(cur[1], b))
        else:
            if cur:
                total += (cur[1] - cur[0]).total_seconds()
            cur = (a, b)
    if cur:
        total += (cur[1] - cur[0]).total_seconds()
    return total


def line(cur, start: datetime, end: datetime):
    cur.execute("select key, category, started_at, ended_at, what from stoppages where started_at < %s and coalesce(ended_at, %s) > %s",
                (end, end, start))
    rows = cur.fetchall()
    down = _merge([(max(r["started_at"], start), min(r["ended_at"] or end, end)) for r in rows if r["key"].startswith(DOWN)])
    span = (end - start).total_seconds()
    started = [r for r in rows if start <= r["started_at"] < end and not r["key"].startswith(DOWN)]
    ended = [r for r in rows if r["ended_at"] and start <= r["ended_at"] < end and not r["key"].startswith(DOWN)]
    recover = statistics.median([(r["ended_at"] - r["started_at"]).total_seconds() / 60 for r in ended]) if ended else None
    open_ = sorted([r for r in rows if r["ended_at"] is None or r["ended_at"] >= end], key=lambda r: r["started_at"])
    items = [metric("ラインが動いていた割合", round(100 * (1 - down / span), 1) if span else None, "%"),
             metric("止まった回数（ラインが空・一時停止を除く）", len(started), "件"),
             metric("止まってから戻るまで（中央値）", round(recover) if recover is not None else None, "分")]
    for r in open_[:OPEN_MAX]:
        hours = round((end - r["started_at"]).total_seconds() / 3600, 1)
        items.append(entry(f"開いている停止: {r['what'] or r['key']}", f"{r['category']}、{hours} 時間"))
    return items


def jobs(cur, start: datetime, end: datetime):
    cur.execute("select count(*) filter (where payload->>'state' = 'succeeded') as ok, count(*) filter (where payload->>'state' = 'failed') as ng "
                "from events where name = 'job.finished' and occurred_at >= %s and occurred_at < %s", (start, end))
    r = cur.fetchone()
    ok, ng = int(r["ok"] or 0), int(r["ng"] or 0)
    return [metric("終わったジョブ", ok + ng, "件"), metric("失敗したジョブ", ng, "件"),
            metric("失敗の割合", round(100 * ng / (ok + ng), 1) if ok + ng else None, "%")]


def inbox(cur, start: datetime, end: datetime):
    cur.execute("select payload from events where name = 'inbox.cleared' and occurred_at >= %s and occurred_at < %s", (start, end))
    waited = [int(r["payload"].get("waited_sec") or 0) / 60 for r in cur.fetchall()]
    cur.execute("select count(distinct payload->>'key') as n from events o where o.name = 'inbox.notified' and o.occurred_at < %s "
                "and not exists (select 1 from events c where c.name = 'inbox.cleared' and c.payload->>'key' = o.payload->>'key' "
                "and c.id > o.id and c.occurred_at < %s)", (end, end))
    left = int(cur.fetchone()["n"] or 0)
    return [metric("判断が済んだ件", len(waited), "件"),
            metric("判断までの待ち時間（中央値）", round(statistics.median(waited)) if waited else None, "分"),
            metric("期間の終わりに残っていた件", left, "件")]


def cost(cur, start: datetime, end: datetime):
    cur.execute("select sum(coalesce((payload->>'cost_usd')::numeric, 0)) as usd from events where name = 'job.finished' "
                "and occurred_at >= %s and occurred_at < %s", (start, end))
    usd = cur.fetchone()["usd"]
    cur.execute("select started_at, ended_at from stoppages where category = 'budget' and started_at < %s and coalesce(ended_at, %s) > %s",
                (end, end, start))
    stopped = _merge([(max(r["started_at"], start), min(r["ended_at"] or end, end)) for r in cur.fetchall()])
    return [metric("消費（API 単価に換算）", round(float(usd), 2) if usd is not None else 0.0, "USD"),
            metric("予算の上限で止まっていた時間", round(stopped / 60), "分")]


BUILDERS = {"line": line, "jobs": jobs, "inbox": inbox, "cost": cost}


def build(cur, start: datetime, end: datetime) -> Report:
    rep = Report(KIND, TITLE, start, end, provisional=True)
    for sid, (title, parties) in SECTIONS.items():
        try:
            items = BUILDERS[sid](cur, start, end)
        except Exception as e:  # noqa: BLE001  1 つの節が読めなくても残りは出す（不明として）
            from report import note
            items = [note(f"読めなかった（{type(e).__name__}）")]
        rep.sections.append(Section(sid, title, parties, items))
    rep.summary = summary(rep)
    return rep


def summary(rep: Report) -> list[str]:
    """ひとことの要約（3 行）。Slack の投稿の本文になる。詳しくは添付の報告書"""
    m = {(s.id, i.label): i.value for s in rep.sections for i in s.items if i.kind == "metric"}
    v = lambda sid, label, unit="": _v(m.get((sid, label)), unit)   # noqa: E731
    opened = sum(1 for s in rep.sections if s.id == "line" for i in s.items if i.kind == "entry")
    return [f"ラインの稼働 {v('line', 'ラインが動いていた割合', '%')}、止まった {v('line', '止まった回数（ラインが空・一時停止を除く）', '件')}"
            + (f"（開いている停止 {opened} 件）" if opened else ""),
            f"ジョブ {v('jobs', '終わったジョブ', '件')}、うち失敗 {v('jobs', '失敗したジョブ', '件')}",
            f"人の判断待ち 残り {v('inbox', '期間の終わりに残っていた件', '件')}、消費 {v('cost', '消費（API 単価に換算）', ' USD')}"]


def _v(x, unit: str) -> str:
    return "不明" if x is None else f"{x}{unit}"
