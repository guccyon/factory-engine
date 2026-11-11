"""工場の操業レポート（report.factory_health、ADR 0032）の組み立てと見立て。中身は仮置き（definitions/reports.yaml の status: provisional）。

制御プレーンだけから数を出す。期間は引数で受けるので、推移（前の期間）も任意の期間も同じ物差しで作れる。
節の id ごとに関数を 1 つ持つ。項目は叩き台として多めに出し、受け取る人と見ながら削る（2026-11-06 オーナー）。
見立て（assess）は数の閾値で、順調か・危険な兆候・早めに打てる手を出す（閾値は仮置き。THRESHOLDS）。
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta

import line
import patrol_watch
import stage_roi
from report import Report, Section, console_link, entry, finding, link, metric, note

KIND = "report.factory_health"
TITLE = "工場の操業レポート"
# definitions/reports.yaml の report.factory_health の節の写し（食い違いはテストで見る）
SECTIONS = {
    "line": ("ライン", ("provider", "business_owner")),
    "jobs": ("ジョブ", ("provider", "business_owner")),
    "ship": ("出荷", ("provider", "business_owner")),
    "inbox": ("人の判断待ち", ("provider", "business_owner")),
    "cost": ("費用", ("provider", "business_owner")),
    "stages": ("工程と ROI", ("provider",)),
    "patrols": ("見回り", ("provider",)),
}
DOWN = ("idle:", "drain:")   # ラインが動いていない時間に数える停止（ラインが空・一時停止）
OPEN_MAX = 5                 # 開いている停止はここまで載せる（製品の作業を止めているものを先に、その中で長い順）
# 失敗しても製品の作業を止めないジョブ（工場の振り返り・委員会・記録の体裁）。停止が長く開いていても「危険」にはしない（2026-11-10 オーナー:
# 無害な古い停止で「危険」が埋まると、本当に危険なものへの対処が遅れる。rally #1031 の実装の時間切れが 5 件の上限で押し出されて見えなかった）
# 委員会・事例・事故の調査・工場の中での一次調査（ラインの外の枠のジョブ）と、PR の説明の体裁の直し。
# 枠の種類（line.GOVERNANCE_KINDS）から作るので、枠に種類を足せばここにも入る（investigate が漏れて一次調査の評価の失敗が「危険」に出た。11/11）
INTERNAL_KINDS = tuple(line.GOVERNANCE_KINDS) + ("body_fix",)
INTERNAL_KEYS = (patrol_watch.PREFIX,)   # 見回りが止まった停止（ADR 0050）も製品の作業は止めない
IMPACT_PRODUCT, IMPACT_INTERNAL = "製品の作業が止まっている", "工場の内部の作業"
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
    "impl_first_pass": (70.0, 50.0), # 実装が一発で通った割合（%）がこれを下回る（直近 ROI_DAYS 日、実装が MIN_JOBS 件以上のとき）
    "wait_share": (50.0, 75.0),      # ジョブの時間のうち、積まれてから始まるまでの待ちの割合（%）
    "defects_per_impl": (0.5, 1.0),  # 実装 1 件あたりに混入した Defect
    "trial_drop_pt": (10.0, 20.0),   # モデルの試しで、試す側の一発で通った割合が既定の側より何 pt 低いか（両側が 5 件以上のとき）
    "patrol_availability": (99.0, 90.0),  # 見回りの可用性の平均（%）がこれを下回る（ADR 0050。工場の内部なので製品の作業は止めない）
}
ROI_DAYS = 14   # 工程と ROI は 1 日だと少ないので、期間の終わりから直近 14 日で見る
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
    cur.execute("select s.key, s.category, s.started_at, s.ended_at, s.what, s.correlation_id, j.kind as job_kind from stoppages s "
                "left join jobs j on j.id = s.job_id where s.started_at < %s and coalesce(s.ended_at, %s) > %s",
                (end, end, start))
    rows = cur.fetchall()
    down = _merge([(max(r["started_at"], start), min(r["ended_at"] or end, end)) for r in rows if r["key"].startswith(DOWN)])
    span = (end - start).total_seconds()
    started = [r for r in rows if start <= r["started_at"] < end and not r["key"].startswith(DOWN)]
    ended = [r for r in rows if r["ended_at"] and start <= r["ended_at"] < end and not r["key"].startswith(DOWN)]
    recover = statistics.median([(r["ended_at"] - r["started_at"]).total_seconds() / 60 for r in ended]) if ended else None
    open_ = sorted([r for r in rows if r["ended_at"] is None or r["ended_at"] >= end], key=lambda r: (_impact(r) != IMPACT_PRODUCT, r["started_at"]))
    human = _merge([(max(r["started_at"], start), min(r["ended_at"] or end, end)) for r in rows
                    if r["category"] == "human" and not r["key"].startswith(DOWN)])
    items = [metric("ラインが動いていた割合", round(100 * (1 - down / span), 1) if span else None, "%"),
             metric("止まった回数（ラインが空・一時停止を除く）", len(started), "件"),
             metric("止まってから戻るまで（中央値）", round(recover) if recover is not None else None, "分"),
             metric("人の対応待ちで止まっていた時間", round(human / 60), "分"),
             metric("期間の終わりに開いていた停止", len(open_), "件"),
             metric("うち製品の作業を止めているもの", sum(1 for r in open_ if _impact(r) == IMPACT_PRODUCT), "件")]
    for r in open_[:OPEN_MAX]:
        hours = round((end - r["started_at"]).total_seconds() / 3600, 1)
        what = r["what"] or r["key"]
        if r.get("job_kind"):
            what = f"{what}（{r['job_kind']}）"
        items.append(entry(f"開いている停止: {what}", f"{r['category']}、{_impact(r)}、{hours} 時間", _stoppage_url(r)))
    return items


def _impact(r: dict) -> str:
    """停止が何を止めているか。ラインが空・一時停止は稼働の割合で見るので別にする。分からないものは製品の作業とみなす（見落とさない側に倒す）"""
    if (r.get("key") or "").startswith(DOWN):
        return "ラインの空き"
    if (r.get("key") or "").startswith(INTERNAL_KEYS):
        return IMPACT_INTERNAL
    return IMPACT_INTERNAL if r.get("job_kind") in INTERNAL_KINDS else IMPACT_PRODUCT


def _stoppage_url(r: dict) -> str:
    """停止の対象へのリンク: 鍵に入っている URL（judge:・ci:・sendback: の PR など）か、チケット（github:owner/repo#n）の Issue"""
    try:
        m = re.search(r"https://\S+", r.get("key") or "")
        if m:
            return m.group(0)
        m = re.fullmatch(r"github:([^/#\s]+/[^/#\s]+)#(\d+)", r.get("correlation_id") or "")
        return f"https://github.com/{m.group(1)}/issues/{m.group(2)}" if m else ""
    except Exception:  # noqa: BLE001  リンクが作れなくても停止は載せる
        return ""


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


def stages(cur, start: datetime, end: datetime):
    """工程と ROI（2026-11-08 オーナー「確信度の高い工程から安いモデルを試す」）。期間の終わりから直近 ROI_DAYS 日。
    どの工程が時間・費用を食い、一発で通らず、Defect を混入させているか。モデルの既定値を替える判断の根拠（集計は stage_roi.py）"""
    v = stage_roi.fetch(cur, end - timedelta(days=ROI_DAYS), end)
    if not v["jobs"]:
        return [note(f"直近 {ROI_DAYS} 日に終わったジョブはありません")]
    bn, per = v["bottleneck"], {}
    for r in v["stages"]:
        a = per.setdefault(r["stage"], {"jobs": 0, "fp": 0.0, "defects": 0})
        a["jobs"] += r["jobs"]; a["fp"] += r["first_pass"] * r["jobs"]; a["defects"] += r["defects"]
    impl = per.get("implement") or {"jobs": 0, "fp": 0.0, "defects": 0}
    pc = lambda x: round(100 * x, 1) if isinstance(x, (int, float)) else None   # noqa: E731
    # ボトルネックの工程は日によって替わるので、名前と割合を 1 つの値にする（項目の名前は推移で同じに保つ）
    items = [metric("時間のボトルネック", f"{bn['time']['ja']}（{pc(bn['time']['share'])}%）" if bn.get("time") else None),
             metric("費用のボトルネック", f"{bn['cost']['ja']}（{pc(bn['cost']['share'])}%）" if bn.get("cost") else None),
             metric("待ちの割合（積まれてから始まるまで）", pc(bn.get("wait_share")), "%"),
             metric("実装のジョブ", impl["jobs"], "件"),
             metric("実装が一発で通った割合", pc(impl["fp"] / impl["jobs"]) if impl["jobs"] else None, "%"),
             metric("実装 1 件あたりに混入した Defect", round(impl["defects"] / impl["jobs"], 2) if impl["jobs"] else None, "件")]
    x = v.get("experiment")
    if x:
        a, b = x["arms"]["trial"], x["arms"]["control"]
        items += [metric("モデルの試し: 試す側（Sonnet）のチケット", a["tickets"], "件"),
                  metric("モデルの試し: 試す側の一発で通った割合", pc(a["first_pass"]), "%"),
                  metric("モデルの試し: 既定の側（Opus）の一発で通った割合", pc(b["first_pass"]), "%"),
                  metric("モデルの試し: 通った 1 件あたりの費用（既定の側を 100）", pc(x.get("cost_ratio")), "%")]
    for r in v["stages"][:6]:
        items.append(entry(f"{r['ja']}（{r['model']}）", f"{r['jobs']} 件・${r['cost_usd']:,.2f}・一発 {pc(r['first_pass'])}%・"
                                                         f"待ち {stage_roi._m(r['wait_min'])}・Defect {r['defects']}"))
    items.append(note(f"直近 {ROI_DAYS} 日（期間の終わりまで）。一発で通った: 実装は後から直し（CI 落ち・指摘・途中終了の続き）が要らなかった割合。"
                      "Defect は PR の最新の版を書いた実装のジョブに付けて数える"))
    return items


def patrols(cur, start: datetime, end: datetime):
    """見回り（ADR 0050。2026-11-11 オーナー「見回りに id を振り実施を記録、費用は実行記録とともに、対象ごとの可用性を数で」）。
    期間の見回りごとの実施の回数・費用・可用性（停止 patrol:<id> が開いていなかった割合）。数は patrol_watch.summary"""
    rows = patrol_watch.summary(cur, start, end)
    if not rows:
        return [note("見回りの宣言がまだありません（supervisor の heartbeat に patrols がない）")]
    av = [r["availability"] for r in rows if r["availability"] is not None]
    items = [metric("見回りの可用性（平均）", round(sum(av) / len(av), 1) if av else None, "%"),
             metric("見回りの実施", sum(r["runs"] for r in rows), "回"),
             metric("見回りの費用（積んだジョブの消費）", round(sum(r["cost_usd"] for r in rows), 2), "USD"),
             metric("止まっていた見回り", sum(1 for r in rows if r["availability"] is not None and r["availability"] < 100), "件")]
    for r in rows:
        last = f"、最後 {r['last']:%m-%d %H:%M}（UTC）" if r["last"] else "、記録なし"
        items.append(entry(f"{r['name']}（{r['id']}）", f"{r['runs']} 回・${r['cost_usd']:,.2f}・可用性 {r['availability']}%{last}"))
    items.append(note("可用性は期間のうち見回りの停止（patrol:<id>）が開いていなかった割合。期限は毎日のもの 36 時間、週 1 回のもの 8 日（仮置き）。"
                      "費用は見回りが積んだジョブの消費で、決定論の見回りは $0"))
    return items


BUILDERS = {"line": line, "jobs": jobs, "ship": ship, "inbox": inbox, "cost": cost, "stages": stages, "patrols": patrols}
CHARTS = [("line", "ラインが動いていた割合", "ラインの稼働（%）"), ("jobs", "終わったジョブ", "終わったジョブ（件）"),
          ("ship", "マージした PR", "マージした PR（件）"), ("cost", "消費（API 単価に換算）", "消費（USD）"),
          ("patrols", "見回りの可用性（平均）", "見回りの可用性（%）")]


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

    def flag(sid, label, level, text, action, links=None):
        if not level:
            return
        if (sid, label) in items:
            items[(sid, label)].level = level
        found.append(finding(level, text, action, links))

    line_page = lambda: console_link("操業コンソールのライン", "/line")   # noqa: E731
    inbox_page = lambda: console_link("受信トレイ", "/inbox")             # noqa: E731

    up = val("line", "ラインが動いていた割合")
    flag("line", "ラインが動いていた割合", _lvl(up, *THRESHOLDS["uptime"], below=True), f"ラインが動いていたのは {up}% だけ",
         "止まっていた理由（ラインが空か、一時停止か）を確かめる。空なら次に流すチケットを積み、一時停止なら再開を忘れていないか見る", line_page)
    n, rate = val("jobs", "終わったジョブ"), val("jobs", "失敗の割合")
    if isinstance(n, int) and n >= MIN_JOBS:
        flag("jobs", "失敗の割合", _lvl(rate, *THRESHOLDS["fail_rate"]), f"ジョブの {rate}% が失敗している",
             "操業コンソールの失敗の一覧で原因を確かめる。同じ原因が続くなら工場の不具合として建設者に回す", line_page)
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
            if i.kind != "entry" or "ラインの空き" in i.state:   # ラインが空・一時停止は稼働の割合の兆候で見る
                continue
            h = _open_hours(i.state)
            internal = IMPACT_INTERNAL in i.state
            # 工場の内部の作業の失敗は、製品の作業を止めないので長くても注意まで（閾値は危険の線から数え始める）
            lv = ("warn" if _lvl(h, *THRESHOLDS["open_hours"]) == "bad" else "") if internal else _lvl(h, *THRESHOLDS["open_hours"])
            if lv:
                i.level = lv
                name = i.label.replace("開いている停止: ", "")
                text = f"工場の内部の作業の停止「{name}」が {h} 時間開いたまま" if internal else f"製品の作業が止まっている: 停止「{name}」が {h} 時間開いたまま"
                action = ("製品には影響しない。対象が片付いていれば閉じ（factoryctl stoppage close）、同じ失敗が続くなら工場の不具合として建設者に回す" if internal
                          else "対象のチケットか PR を開き、再実行（factoryctl rerun）か切り直し（factoryctl plan resplit）か、閉じてよいかを決める")
                found.append(finding(lv, text, action,
                                     (lambda n=name, u=i.url: [link(n, u)] if u else console_link("操業コンソールのライン", "/line"))))
    pav = val("patrols", "見回りの可用性（平均）")
    flag("patrols", "見回りの可用性（平均）", _lvl(pav, *THRESHOLDS["patrol_availability"], below=True), f"見回りの可用性が {pav}%（止まっていた見回りがある）",
         "見回りの節で止まっていた見回りを見て、supervisor のログで止まった理由を確かめる。直すのは工場の不具合として建設者に回す", line_page)
    left, wait = val("inbox", "期間の終わりに残っていた件"), val("inbox", "判断までの待ち時間（中央値）")
    flag("inbox", "期間の終わりに残っていた件", _lvl(left, *THRESHOLDS["inbox_left"]), f"人の判断待ちが {left} 件残っている",
         "受信トレイの判断を片付ける。滞りが続くなら、代わりに判断する人（INBOX_PROXY）を決める", inbox_page)
    flag("inbox", "判断までの待ち時間（中央値）", _lvl(wait, *THRESHOLDS["inbox_wait"]), f"判断までに中央値で {wait} 分かかっている",
         "通知が届いているか（宛先・夜の時間）を確かめ、判断の担当を決める", inbox_page)
    cost = items.get(("cost", "消費（API 単価に換算）"))
    if cost and isinstance(cost.value, (int, float)):
        prev = [x for x in cost.series[-8:-1] if isinstance(x, (int, float))]
        avg = sum(prev) / len(prev) if prev else None
        if avg and avg > 0:
            ratio = cost.value / avg
            flag("cost", "消費（API 単価に換算）", _lvl(ratio, *THRESHOLDS["cost_ratio"]),
                 f"消費が前の 7 日の平均の {ratio:.1f} 倍（{cost.value} USD）",
                 "費用の外れ値（factoryctl report process）で、どのチケットが膨らんだかを見る。切り直しやモデルの選び直しを検討する")
    bm = val("cost", "予算の上限で止まっていた時間")
    flag("cost", "予算の上限で止まっていた時間", _lvl(bm, *THRESHOLDS["budget_min"]), f"予算の上限で {bm} 分止まっていた",
         "1 日の枠を見直すか、流量を下げて止まらずに流れるようにする")

    # 工程と ROI（モデルの既定値を替える判断の根拠）
    fp = val("stages", "実装が一発で通った割合")
    impl_n = val("stages", "実装のジョブ")
    flag("stages", "実装が一発で通った割合", _lvl(fp, *THRESHOLDS["impl_first_pass"], below=True) if isinstance(impl_n, int) and impl_n >= MIN_JOBS else "",
         f"実装が一発で通ったのは {fp}% だけ",
         "建設日報の「工程ごとの ROI」で、どのモデルの実装が直しを呼んでいるかを見る。小さいチケットの試し（Sonnet）が原因なら MODEL_ROUTE_SHARE を下げる")
    ws = val("stages", "待ちの割合（積まれてから始まるまで）")
    flag("stages", "待ちの割合（積まれてから始まるまで）", _lvl(ws, *THRESHOLDS["wait_share"]), f"ジョブの時間の {ws}% が待ち",
         "流量（factoryctl line scale）か予算の枠が足りない見込み。速い・安いモデルに替えるより、詰まりをほどくのが先")
    dpi = val("stages", "実装 1 件あたりに混入した Defect")
    flag("stages", "実装 1 件あたりに混入した Defect", _lvl(dpi, *THRESHOLDS["defects_per_impl"]), f"実装 1 件あたり {dpi} 件の Defect を混入させている",
         "出やすい観点（日報の品質）を見て、実装役のモデルを上げるか、知見・検査のルールに足す")
    ta, tb = val("stages", "モデルの試し: 試す側の一発で通った割合"), val("stages", "モデルの試し: 既定の側（Opus）の一発で通った割合")
    tn = val("stages", "モデルの試し: 試す側（Sonnet）のチケット")
    if isinstance(ta, (int, float)) and isinstance(tb, (int, float)) and isinstance(tn, int) and tn >= stage_roi.MIN_N:
        flag("stages", "モデルの試し: 試す側の一発で通った割合", _lvl(tb - ta, *THRESHOLDS["trial_drop_pt"]),
             f"Sonnet の実装は Opus より一発で通る割合が {round(tb - ta, 1)} pt 低い",
             "費用の差で割に合うかを日報で見る。合わなければ MODEL_ROUTE_SHARE を下げるか 0 にする")
    cr = val("stages", "モデルの試し: 通った 1 件あたりの費用（既定の側を 100）")
    if isinstance(cr, (int, float)) and isinstance(tn, int) and tn >= stage_roi.MIN_N:
        flag("stages", "モデルの試し: 通った 1 件あたりの費用（既定の側を 100）", "warn" if cr >= 100 else "",
             f"Sonnet で通した 1 件の費用が Opus の {cr}%（安くなっていない）", "直しが増えて安さが消えている。試しを止めるか、条件を狭める")

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
