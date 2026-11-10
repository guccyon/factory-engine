"""開発状況レポート（report.product、ADR 0032）の組み立てと見立て。中身は仮置き（definitions/reports.yaml の status: provisional）。

受け取る人が知りたいのは「計画どおりに進んでいるか、崩れる兆候はないか、早めに打てる手はないか」（2026-11-06 オーナー）。
消化のペースと残りの見込み、滞っているチケットを数え、見立て（assess）で兆候と打ち手を出す。閾値は仮置き（THRESHOLDS）。

まずはロードマップの進捗。工場のチケット（tickets、ADR 0009）と、supervisor が残す pr.merged・release.created から出す。
GitHub を直接見ないので、期間は引数で受けて、ダッシュボードから任意の期間でも同じ中身を作れる。
対象の製品は通知の設定を読む製品（notify.config_repo()）。中身を決めるタスクでは、ここと reports.yaml の節の一覧を書き換える。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import notify
from report import Report, Section, console_link, entry, finding, link, metric, note

KIND = "report.product"
TITLE = "開発状況レポート"
# definitions/reports.yaml の report.product の節の写し（食い違いはテストで見る）
SECTIONS = {
    "roadmap": ("ロードマップ", ("provider", "business_owner")),
    "pace": ("ペースと見込み", ("provider", "business_owner")),
    "done": ("済んだもの", ("provider", "business_owner")),
    "doing": ("進めているもの", ("provider", "business_owner")),
    "next": ("次に流すもの", ("provider", "business_owner")),
    "releases": ("版", ("provider", "business_owner")),
    "kpi": ("製品の KPI（週次）", ("provider", "business_owner")),
}
ANOMALY_JA = {"range": "目安の外", "prev": "前回より悪化", "forecast": "見込みと食い違い"}   # report_kpi.find_anomalies の kind
LIST_MAX = 8   # 一覧はここまで。残りは件数だけ
PACE_DAYS = 7  # 消化のペースを測る期間（日）
THRESHOLDS = {
    "stale_days": (3.0, 7.0),        # 着手したチケットのうち、いちばん長く動いていないもの（日）。着手前・保留・やることが残っていないものは数えない（STALEST）
    "forecast_days": (30.0, 60.0),   # 残りをいまのペースで終えるまでの見込み（日）
    "held": (3, 6),                  # 保留中のチケット（件）
}


def _more(items: list, total: int) -> list:
    return items + ([note(f"ほか {total - LIST_MAX} 件")] if total > LIST_MAX else [])


def roadmap(cur, repo: str, start: datetime, end: datetime):
    cur.execute("select count(*) filter (where state = 'closed' and updated_at < %s) as closed, "
                "count(*) filter (where state = 'active' or (state = 'closed' and updated_at >= %s)) as active, "
                "count(*) filter (where state = 'planned') as planned, count(*) filter (where held and state <> 'closed') as held "
                "from tickets where repo = %s and created_at < %s", (end, end, repo, end))
    r = cur.fetchone()
    closed, active, planned = int(r["closed"] or 0), int(r["active"] or 0), int(r["planned"] or 0)
    total = closed + active + planned
    return [metric("進み具合（済んだチケットの割合）", round(100 * closed / total, 1) if total else None, "%"),
            metric("済んだ", closed, "件"), metric("進めている", active, "件"), metric("投入待ち", planned, "件"),
            metric("保留中", int(r["held"] or 0), "件")]


def done(cur, repo: str, start: datetime, end: datetime):
    cur.execute("select title, url from tickets where repo = %s and state = 'closed' and updated_at >= %s and updated_at < %s order by updated_at",
                (repo, start, end))
    rows = cur.fetchall()
    cur.execute("select count(*) as n from events where name = 'pr.merged' and correlation_id like %s and occurred_at >= %s and occurred_at < %s",
                (f"github:{repo}#%", start, end))
    merged = int(cur.fetchone()["n"] or 0)
    return [metric("済んだチケット", len(rows), "件"), metric("マージした PR", merged, "件")] + \
        _more([entry(r["title"] or "(題なし)", url=r["url"] or "") for r in rows[:LIST_MAX]], len(rows))


def pace(cur, repo: str, start: datetime, end: datetime):
    since = end - timedelta(days=PACE_DAYS)
    cur.execute("select count(*) as n, percentile_cont(0.5) within group (order by extract(epoch from updated_at - created_at) / 86400) as lead "
                "from tickets where repo = %s and state = 'closed' and updated_at >= %s and updated_at < %s", (repo, since, end))
    r = cur.fetchone()
    per_day = int(r["n"] or 0) / PACE_DAYS
    cur.execute("select count(*) as n from tickets where repo = %s and created_at < %s and (state <> 'closed' or updated_at >= %s)", (repo, end, end))
    left = int(cur.fetchone()["n"] or 0)
    rows = _stale(cur, repo, end)
    stale = rows[0]["stale"] if rows else None
    cur.execute("select count(*) as n from tickets where repo = %s and created_at >= %s and created_at < %s", (repo, start, end))
    added = int(cur.fetchone()["n"] or 0)
    return [metric(f"消化のペース（直近 {PACE_DAYS} 日の 1 日あたり）", round(per_day, 1), "件"),
            metric("残り（進めている＋投入待ち）", left, "件"),
            metric("残りを今のペースで終えるまでの見込み", round(left / per_day, 1) if per_day else None, "日"),
            metric("チケットのリードタイム（作成から済みまで、中央値）", round(float(r["lead"]), 1) if r["lead"] is not None else None, "日"),
            metric("着手したもののうち最も長く動いていない", round(float(stale), 1) if stale is not None else None, "日"),
            metric("期間に増えたチケット", added, "件")] + \
        [entry(f"{STALE_PREFIX}{r['title'] or '(題なし)'}", f"{round(float(r['stale']), 1)} 日", r["url"] or "")
         for r in rows if float(r["stale"]) >= THRESHOLDS["stale_days"][0]][:LIST_MAX]


# 「動いていない」に数えるチケット（2026-11-10 オーナー）。着手前（バックログのまま）は何日たっても数えない。
# 着手は、ジョブが一度でも始まったこと。保留中は人が止めているので数えない。動いた時刻は、台帳の更新と
# そのチケットのイベント（ジョブ・PR・judge・窓口の確認など）のうち最も新しいもの。
# 着手したのに実態がない（Issue はもう閉じている）記録は、supervisor が台帳を閉じる（ticket_sync.py）
STALE = """
select t.title, t.url,
       extract(epoch from %(end)s - greatest(t.updated_at, coalesce(
         (select max(e.occurred_at) from events e where e.correlation_id = 'github:' || t.source_ref and e.occurred_at < %(end)s),
         t.updated_at))) / 86400 as stale
  from tickets t
 where t.repo = %(repo)s and t.state = 'active' and not t.held
   and exists (select 1 from jobs j where j.correlation_id = 'github:' || t.source_ref and j.started_at < %(end)s)
 order by stale desc
"""
STALE_PREFIX = "動いていない: "


def _stale(cur, repo: str, end: datetime) -> list[dict]:
    """着手したチケットを、長く動いていない順に（題・URL・日数）。読めなくてもペースの残りの数は出す"""
    try:
        cur.execute(STALE, {"repo": repo, "end": end})
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        notify._log("report stale failed", repr(e)[:200])
        return []


def doing(cur, repo: str, start: datetime, end: datetime):
    cur.execute("select title, url, held from tickets where repo = %s and state = 'active' order by priority desc, updated_at", (repo,))
    rows = cur.fetchall()
    return _more([entry(r["title"] or "(題なし)", "保留" if r["held"] else "", r["url"] or "") for r in rows[:LIST_MAX]], len(rows)) or [note("なし")]


def next_(cur, repo: str, start: datetime, end: datetime):
    cur.execute("select title, url, waiting_on from tickets where repo = %s and state = 'planned' and not held order by priority desc, created_at", (repo,))
    rows = cur.fetchall()
    items = [entry(r["title"] or "(題なし)", f"{len(r['waiting_on'])} 件の完了待ち" if r["waiting_on"] else "", r["url"] or "") for r in rows[:LIST_MAX]]
    return _more(items, len(rows)) or [note("なし")]


def releases(cur, repo: str, start: datetime, end: datetime):
    cur.execute("select payload from events where name = 'release.created' and payload->>'repo' = %s and occurred_at >= %s and occurred_at < %s "
                "order by id", (repo, start, end))
    rows = cur.fetchall()
    return [entry(f"版 {r['payload'].get('app_version')}", f"PR {len(r['payload'].get('prs') or [])} 件") for r in rows] or [note("なし")]


def kpi(cur, repo: str, start: datetime, end: datetime):
    """いちばん新しい週次の KPI（ADR 0043 の O2。report_kpi.py が週に 1 回 staging で測る）。異常の印は見立て（assess）が付ける"""
    import report_kpi
    rep = report_kpi.latest(cur, repo, end)
    if not rep:
        return [note("まだ測っていない（毎週月曜の朝に staging で測る）")]
    items = [note(f"{rep['week']}（{rep['env']}）の値。異常 {rep['anomalies']} 件を Opportunity の下書きとして受信トレイに出した"
                  if rep.get("opportunities") else f"{rep['week']}（{rep['env']}）の値")]
    for m in rep.get("metrics") or []:
        it = metric(m["ja"], round(m["value"], 4) if isinstance(m.get("value"), (int, float)) else None, m.get("unit") or "",
                    round(m["delta"], 4) if isinstance(m.get("delta"), (int, float)) else None)
        it.state = "・".join(ANOMALY_JA.get(a, a) for a in m.get("anomalies") or [])   # 異常の種類。assess が印にする
        items.append(it)
    return items


BUILDERS = {"roadmap": roadmap, "pace": pace, "done": done, "doing": doing, "next": next_, "releases": releases, "kpi": kpi}
CHARTS = [("roadmap", "進み具合（済んだチケットの割合）", "ロードマップの進み具合（%）"), ("done", "済んだチケット", "済んだチケット（件）"),
          ("pace", "残り（進めている＋投入待ち）", "残りのチケット（件）"), ("done", "マージした PR", "マージした PR（件）")]


def build(cur, start: datetime, end: datetime, repo: str | None = None) -> Report:
    repo = repo or notify.config_repo()
    rep = Report(KIND, TITLE, start, end, provisional=True)
    for sid, (title, parties) in SECTIONS.items():
        try:
            items = BUILDERS[sid](cur, repo, start, end)
        except Exception as e:  # noqa: BLE001  1 つの節が読めなくても残りは出す
            items = [note(f"読めなかった（{type(e).__name__}）")]
        rep.sections.append(Section(sid, title, parties, items))
    rep.charts = [{"section": a, "label": b, "title": c} for a, b, c in CHARTS]
    return rep


# ------------------------------------------------------------------ 見立て
def _lvl(x, warn, bad) -> str:
    if not isinstance(x, (int, float)):
        return ""
    return "bad" if x >= bad else "warn" if x >= warn else ""


def _days(state: str) -> float | None:
    """「動いていない」の entry の状態（"9.4 日"）から日数を読む"""
    try:
        return float(state.split()[0])
    except (IndexError, ValueError):
        return None


def assess(rep: Report) -> Report:
    """計画が崩れる兆候を数で見て、異常の印（Item.level）と兆候・打ち手（findings）、所感（summary）を詰める"""
    items = {(s.id, i.label): i for s in rep.sections for i in s.items if i.kind == "metric"}
    val = lambda sid, label: getattr(items.get((sid, label)), "value", None)   # noqa: E731
    found: list[dict] = []

    def flag(sid, label, level, text, action, links=None, needs_link=False):
        if not level:
            return
        if (sid, label) in items:
            items[(sid, label)].level = level
        found.append(finding(level, text, action, links, needs_link=needs_link))

    def entries(sid, pred=lambda i: True):
        return [i for s in rep.sections if s.id == sid for i in s.items if i.kind == "entry" and pred(i)]

    pace_label = f"消化のペース（直近 {PACE_DAYS} 日の 1 日あたり）"
    per_day, left = val("pace", pace_label), val("pace", "残り（進めている＋投入待ち）")
    if per_day == 0 and isinstance(left, int) and left > 0:
        flag("pace", pace_label, "bad", f"直近 {PACE_DAYS} 日に済んだチケットがない（残り {left} 件）",
             "ラインが止まっていないか、投入待ちが依存や保留で詰まっていないかを確かめる。止めているなら再開の時期を決める",
             lambda: console_link("操業コンソールのライン", "/line"))
    fc = val("pace", "残りを今のペースで終えるまでの見込み")
    flag("pace", "残りを今のペースで終えるまでの見込み", _lvl(fc, *THRESHOLDS["forecast_days"]), f"残りを終えるまで今のペースで {fc} 日かかる見込み",
         "期日に間に合うかを確かめる。間に合わないなら、範囲を絞る（優先度の低いものを保留）か、流量を上げる（factoryctl line scale）")
    stale = val("pace", "着手したもののうち最も長く動いていない")
    stuck = entries("pace", lambda i: i.label.startswith(STALE_PREFIX))
    for i in stuck:
        i.level = _lvl(_days(i.state), *THRESHOLDS["stale_days"])
    many = f" {len(stuck)} 件が {THRESHOLDS['stale_days'][0]:g} 日以上動いていない（最長 {stale} 日）" if len(stuck) > 1 else f"が {stale} 日動いていない"
    flag("pace", "着手したもののうち最も長く動いていない", _lvl(stale, *THRESHOLDS["stale_days"]),
         f"着手したチケット{many}",
         "対象のチケットが何を待っているか（判断・CI・衝突）を確かめる。大きすぎるならプランナーで切り直す。もう要らないなら Issue を閉じる",
         lambda: [link(i.label[len(STALE_PREFIX):], i.url) for i in stuck], needs_link=True)
    held = val("roadmap", "保留中")
    flag("roadmap", "保留中", _lvl(held, *THRESHOLDS["held"]), f"保留中のチケットが {held} 件ある",
         "保留の理由を見直し、再開するか、ロードマップから外すかを決める", lambda: console_link("操業コンソールのライン", "/line"))
    nxt = entries("next")
    if nxt and all(i.state.endswith("完了待ち") for i in nxt):
        for i in nxt:
            i.level = "warn"
        found.append(finding("warn", "次に流すものがすべて、ほかのチケットの完了待ち",
                             "依存の先のチケットを先に流すよう優先度を上げる（factoryctl priority）。依存が誤りなら外す",
                             lambda: [link(i.label, i.url) for i in nxt], needs_link=True))

    odd = [i for s in rep.sections if s.id == "kpi" for i in s.items if i.kind == "metric" and i.state]
    for i in odd:
        i.level = "warn"
    if odd:
        found.append(finding("warn", f"週次の KPI で {len(odd)} つの値が異常（{'、'.join(i.label for i in odd[:3])}）",
                             "受信トレイの Opportunity の下書きを見て、拾うなら窓口に相談として持ち込む。見送るなら閉じる",
                             lambda: console_link("受信トレイ", "/inbox")))

    order = {"bad": 0, "warn": 1}
    found.sort(key=lambda f: order.get(f["level"], 2))
    rep.findings = found
    rep.level = "bad" if any(f["level"] == "bad" for f in found) else "warn" if found else "ok"
    pr = val("roadmap", "進み具合（済んだチケットの割合）")
    head = {"ok": "計画どおりに進んでいます", "warn": f"注意が {len(found)} 点あります", "bad": "計画が崩れる兆候があります"}[rep.level]
    fmt = lambda x, u: "不明" if x is None else f"{x}{u}"   # noqa: E731
    lines = [f"{head}。進み具合 {fmt(pr, '%')}、残り {fmt(left, '件')}、ペース 1 日 {fmt(per_day, '件')}"]
    lines += [f"{f['text']}。{f['action']}" for f in found[:2]]
    if not found:
        lines.append(f"残りは今のペースで {fmt(fc, '日')}の見込みで、滞っているチケットもありません")
    rep.summary = lines
    return rep


