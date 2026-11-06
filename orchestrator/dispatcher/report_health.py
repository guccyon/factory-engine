"""工場の操業レポート（report.factory_health、ADR 0032）の組み立てと見立て。中身は仮置き（definitions/reports.yaml の status: provisional）。

制御プレーンだけから数を出す。期間は引数で受けるので、推移（前の期間）も任意の期間も同じ物差しで作れる。
節の id ごとに関数を 1 つ持つ。項目は叩き台として多めに出し、受け取る人と見ながら削る（2026-11-06 オーナー）。
見立て（assess）は数の閾値で、順調か・危険な兆候・早めに打てる手を出す（閾値は仮置き。THRESHOLDS）。
"""
from __future__ import annotations

import statistics
from datetime import datetime

from report import Report, Section, entry, metric, note

KIND = "report.factory_health"
TITLE = "工場の操業レポート"
# definitions/reports.yaml の report.factory_health の節の写し（食い違いはテストで見る）
SECTIONS = {
    "line": ("ライン", ("provider", "business_owner")),
    "jobs": ("ジョブ", ("provider", "business_owner")),
    "ship": ("出荷", ("provider", "business_owner")),
    "inbox": ("人の判断待ち", ("provider", "business_owner")),
    "cost": ("費用", ("provider", "business_owner")),
}
DOWN = ("idle:", "drain:")   # ラインが動いていない時間に数える停止（ラインが空・一時停止）
OPEN_MAX = 5                 # 開いている停止は長い順にここまで載せる
# 見立ての閾値（仮置き）。注意（warn）と危険（bad）
THRESHOLDS = {
    "uptime": (80.0, 50.0),          # ラインが動いていた割合（%）がこれを下回る
    "fail_rate": (20.0, 40.0),       # 失敗の割合（%）がこれ以上（終わったジョブが MIN_JOBS 件以上のとき）
    "open_hours": (6.0, 24.0),       # 開いている停止の長さ（時間）
    "inbox_left": (3, 6),            # 期間の終わりに残っていた判断待ち（件）
    "inbox_wait": (240, 720),        # 判断までの待ち時間の中央値（分）
    "incomplete": (2, 4),            # 途中終了したジョブ（件）
    "cost_ratio": (1.5, 2.5),        # 消費が前の 7 期間の平均の何倍か
    "budget_min": (60, 240),         # 予算の上限で止まっていた時間（分）
}
MIN_JOBS = 3


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
    human = _merge([(max(r["started_at"], start), min(r["ended_at"] or end, end)) for r in rows
                    if r["category"] == "human" and not r["key"].startswith(DOWN)])
    items = [metric("ラインが動いていた割合", round(100 * (1 - down / span), 1) if span else None, "%"),
             metric("止まった回数（ラインが空・一時停止を除く）", len(started), "件"),
             metric("止まってから戻るまで（中央値）", round(recover) if recover is not None else None, "分"),
             metric("人の対応待ちで止まっていた時間", round(human / 60), "分"),
             metric("期間の終わりに開いていた停止", len(open_), "件")]
    for r in open_[:OPEN_MAX]:
        hours = round((end - r["started_at"]).total_seconds() / 3600, 1)
        items.append(entry(f"開いている停止: {r['what'] or r['key']}", f"{r['category']}、{hours} 時間"))
    return items


def _open_hours(state: str) -> float | None:
    """開いている停止の entry の状態（"human、6.5 時間"）から時間を読む"""
    try:
        return float(state.rsplit("、", 1)[1].split()[0])
    except (IndexError, ValueError):
        return None


def jobs(cur, start: datetime, end: datetime):
    cur.execute("select count(*) filter (where payload->>'state' = 'succeeded') as ok, count(*) filter (where payload->>'state' = 'failed') as ng "
                "from events where name = 'job.finished' and occurred_at >= %s and occurred_at < %s", (start, end))
    r = cur.fetchone()
    ok, ng = int(r["ok"] or 0), int(r["ng"] or 0)
    cur.execute("select count(*) filter (where (payload->>'incomplete')::boolean) as inc, "
                "count(*) filter (where (payload->>'usage_limited')::boolean) as lim from events "
                "where name = 'job.finished' and occurred_at >= %s and occurred_at < %s", (start, end))
    f = cur.fetchone()
    cur.execute("select extract(epoch from finished_at - started_at) / 60 as run, extract(epoch from started_at - enqueued_at) / 60 as wait "
                "from jobs where finished_at >= %s and finished_at < %s and started_at is not null", (start, end))
    rows = cur.fetchall()
    run = [float(x["run"]) for x in rows if x["run"] is not None]
    wait = [float(x["wait"]) for x in rows if x["wait"] is not None]
    return [metric("終わったジョブ", ok + ng, "件"), metric("失敗したジョブ", ng, "件"),
            metric("失敗の割合", round(100 * ng / (ok + ng), 1) if ok + ng else None, "%"),
            metric("途中終了したジョブ（ターンの上限など）", int(f["inc"] or 0), "件"),
            metric("契約の使用量の上限で止まったジョブ", int(f["lim"] or 0), "件"),
            metric("ジョブの実行時間（中央値）", round(statistics.median(run)) if run else None, "分"),
            metric("積まれてから始まるまで（中央値）", round(statistics.median(wait)) if wait else None, "分")]


def ship(cur, start: datetime, end: datetime):
    cur.execute("select count(*) filter (where name = 'pr.merged') as merged, count(*) filter (where name = 'pr.closed_unmerged') as closed "
                "from events where name in ('pr.merged', 'pr.closed_unmerged') and occurred_at >= %s and occurred_at < %s", (start, end))
    r = cur.fetchone()
    cur.execute("select count(*) as n from events where name = 'release.created' and occurred_at >= %s and occurred_at < %s", (start, end))
    rel = int(cur.fetchone()["n"] or 0)
    merged, closed = int(r["merged"] or 0), int(r["closed"] or 0)
    return [metric("マージした PR", merged, "件"), metric("マージせずに閉じた PR", closed, "件"),
            metric("マージできた割合", round(100 * merged / (merged + closed), 1) if merged + closed else None, "%"),
            metric("切った版", rel, "件")]


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
    cur.execute("select count(*) as n from events where name = 'pr.merged' and occurred_at >= %s and occurred_at < %s", (start, end))
    merged = int(cur.fetchone()["n"] or 0)
    cur.execute("select sum(coalesce((payload->>'billable_tokens')::numeric, 0)) as t from events where name = 'job.finished' "
                "and occurred_at >= %s and occurred_at < %s", (start, end))
    tok = cur.fetchone()["t"]
    usd = float(usd) if usd is not None else 0.0
    return [metric("消費（API 単価に換算）", round(usd, 2), "USD"),
            metric("マージした PR 1 本あたりの消費", round(usd / merged, 2) if merged else None, "USD"),
            metric("課金の対象のトークン", round(float(tok or 0) / 1e6, 2), "百万"),
            metric("予算の上限で止まっていた時間", round(stopped / 60), "分")]


BUILDERS = {"line": line, "jobs": jobs, "ship": ship, "inbox": inbox, "cost": cost}
CHARTS = [("line", "ラインが動いていた割合", "ラインの稼働（%）"), ("jobs", "終わったジョブ", "終わったジョブ（件）"),
          ("ship", "マージした PR", "マージした PR（件）"), ("cost", "消費（API 単価に換算）", "消費（USD）")]


def build(cur, start: datetime, end: datetime) -> Report:
    rep = Report(KIND, TITLE, start, end, provisional=True)
    for sid, (title, parties) in SECTIONS.items():
        try:
            items = BUILDERS[sid](cur, start, end)
        except Exception as e:  # noqa: BLE001  1 つの節が読めなくても残りは出す（不明として）
            items = [note(f"読めなかった（{type(e).__name__}）")]
        rep.sections.append(Section(sid, title, parties, items))
    rep.charts = [{"section": a, "label": b, "title": c} for a, b, c in CHARTS]
    return rep


# ------------------------------------------------------------------ 見立て
def _lvl(x, warn, bad, *, below: bool = False) -> str:
    if not isinstance(x, (int, float)):
        return ""
    if below:
        return "bad" if x < bad else "warn" if x < warn else ""
    return "bad" if x >= bad else "warn" if x >= warn else ""


def assess(rep: Report) -> Report:
    """数を閾値で見て、異常の印（Item.level）と、兆候と打ち手（findings）、全体の見立てと所感（summary）を詰める"""
    items = {(s.id, i.label): i for s in rep.sections for i in s.items if i.kind == "metric"}
    val = lambda sid, label: getattr(items.get((sid, label)), "value", None)   # noqa: E731
    found: list[dict] = []

    def flag(sid, label, level, text, action):
        if not level:
            return
        if (sid, label) in items:
            items[(sid, label)].level = level
        found.append({"level": level, "text": text, "action": action})

    up = val("line", "ラインが動いていた割合")
    flag("line", "ラインが動いていた割合", _lvl(up, *THRESHOLDS["uptime"], below=True), f"ラインが動いていたのは {up}% だけ",
         "止まっていた理由（ラインが空か、一時停止か）を確かめる。空なら次に流すチケットを積み、一時停止なら再開を忘れていないか見る")
    n, rate = val("jobs", "終わったジョブ"), val("jobs", "失敗の割合")
    if isinstance(n, int) and n >= MIN_JOBS:
        flag("jobs", "失敗の割合", _lvl(rate, *THRESHOLDS["fail_rate"]), f"ジョブの {rate}% が失敗している",
             "操業コンソールの失敗の一覧で原因を確かめる。同じ原因が続くなら工場の不具合として建設者に回す")
    inc = val("jobs", "途中終了したジョブ（ターンの上限など）")
    flag("jobs", "途中終了したジョブ（ターンの上限など）", _lvl(inc, *THRESHOLDS["incomplete"]), f"途中終了したジョブが {inc} 件",
         "大きすぎるチケットの見込み。プランナーで切り直す（factoryctl plan resplit）か、チケットを分ける")
    lim = val("jobs", "契約の使用量の上限で止まったジョブ")
    flag("jobs", "契約の使用量の上限で止まったジョブ", "bad" if isinstance(lim, int) and lim > 0 else "", f"契約の使用量の上限で {lim} 件止まった",
         "週の上限が近い。流量を下げる（factoryctl line scale）か、アカウントを足すかを決める")
    for s in rep.sections:
        if s.id != "line":
            continue
        for i in s.items:
            h = _open_hours(i.state) if i.kind == "entry" else None
            lv = _lvl(h, *THRESHOLDS["open_hours"])
            if lv:
                i.level = lv
                found.append({"level": lv, "text": f"停止「{i.label.replace('開いている停止: ', '')}」が {h} 時間開いたまま",
                              "action": "受信トレイかコンソールの停止の一覧で、誰の対応待ちかを確かめて先に片付ける"})
    left, wait = val("inbox", "期間の終わりに残っていた件"), val("inbox", "判断までの待ち時間（中央値）")
    flag("inbox", "期間の終わりに残っていた件", _lvl(left, *THRESHOLDS["inbox_left"]), f"人の判断待ちが {left} 件残っている",
         "受信トレイの判断を片付ける。滞りが続くなら、代わりに判断する人（INBOX_PROXY）を決める")
    flag("inbox", "判断までの待ち時間（中央値）", _lvl(wait, *THRESHOLDS["inbox_wait"]), f"判断までに中央値で {wait} 分かかっている",
         "通知が届いているか（宛先・夜の時間）を確かめ、判断の担当を決める")
    cost = items.get(("cost", "消費（API 単価に換算）"))
    if cost and isinstance(cost.value, (int, float)):
        prev = [x for x in cost.series[-8:-1] if isinstance(x, (int, float))]
        avg = sum(prev) / len(prev) if prev else None
        if avg and avg > 0:
            ratio = cost.value / avg
            flag("cost", "消費（API 単価に換算）", _lvl(ratio, *THRESHOLDS["cost_ratio"]),
                 f"消費が前の 7 日の平均の {ratio:.1f} 倍（{cost.value} USD）",
                 "費用の外れ値（factoryctl report process）で、どのチケットが膨らんだかを見る。切り直しやモデルの段の見直しを検討する")
    bm = val("cost", "予算の上限で止まっていた時間")
    flag("cost", "予算の上限で止まっていた時間", _lvl(bm, *THRESHOLDS["budget_min"]), f"予算の上限で {bm} 分止まっていた",
         "1 日の枠を見直すか、流量を下げて止まらずに流れるようにする")

    order = {"bad": 0, "warn": 1}
    found.sort(key=lambda f: order.get(f["level"], 2))
    rep.findings = found
    rep.level = "bad" if any(f["level"] == "bad" for f in found) else "warn" if found else "ok"
    jobs_n, merged = val("jobs", "終わったジョブ"), val("ship", "マージした PR")
    head = {"ok": "順調です", "warn": f"注意が {len(found)} 点あります", "bad": "危険な兆候があります"}[rep.level]
    lines = [f"{head}。ラインの稼働 {_v(up, '%')}、ジョブ {_v(jobs_n, '件')}、マージ {_v(merged, '件')}"]
    lines += [f"{f['text']}。{f['action']}" for f in found[:2]]
    if not found:
        lines.append("止まったままの停止や、溜まっている判断待ちはありません")
    rep.summary = lines
    return rep


def _v(x, unit: str) -> str:
    return "不明" if x is None else f"{x}{unit}"
