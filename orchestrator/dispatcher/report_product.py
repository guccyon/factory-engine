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
from report import Report, Section, entry, metric, note

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
}
LIST_MAX = 8   # 一覧はここまで。残りは件数だけ
PACE_DAYS = 7  # 消化のペースを測る期間（日）
THRESHOLDS = {
    "stale_days": (3.0, 7.0),        # 進めているチケットのうち、いちばん長く動いていないもの（日）
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
    cur.execute("select max(extract(epoch from %s - updated_at) / 86400) as stale from tickets where repo = %s and state = 'active'", (end, repo))
    stale = cur.fetchone()["stale"]
    cur.execute("select count(*) as n from tickets where repo = %s and created_at >= %s and created_at < %s", (repo, start, end))
    added = int(cur.fetchone()["n"] or 0)
    return [metric(f"消化のペース（直近 {PACE_DAYS} 日の 1 日あたり）", round(per_day, 1), "件"),
            metric("残り（進めている＋投入待ち）", left, "件"),
            metric("残りを今のペースで終えるまでの見込み", round(left / per_day, 1) if per_day else None, "日"),
            metric("チケットのリードタイム（作成から済みまで、中央値）", round(float(r["lead"]), 1) if r["lead"] is not None else None, "日"),
            metric("進めているもののうち最も長く動いていない", round(float(stale), 1) if stale is not None else None, "日"),
            metric("期間に増えたチケット", added, "件")]


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


BUILDERS = {"roadmap": roadmap, "pace": pace, "done": done, "doing": doing, "next": next_, "releases": releases}
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


def assess(rep: Report) -> Report:
    """計画が崩れる兆候を数で見て、異常の印（Item.level）と兆候・打ち手（findings）、所感（summary）を詰める"""
    items = {(s.id, i.label): i for s in rep.sections for i in s.items if i.kind == "metric"}
    val = lambda sid, label: getattr(items.get((sid, label)), "value", None)   # noqa: E731
    found: list[dict] = []

    def flag(sid, label, level, text, action):
        if not level:
            return
        if (sid, label) in items:
            items[(sid, label)].level = level
        found.append({"level": level, "text": text, "action": action})

    pace_label = f"消化のペース（直近 {PACE_DAYS} 日の 1 日あたり）"
    per_day, left = val("pace", pace_label), val("pace", "残り（進めている＋投入待ち）")
    if per_day == 0 and isinstance(left, int) and left > 0:
        flag("pace", pace_label, "bad", f"直近 {PACE_DAYS} 日に済んだチケットがない（残り {left} 件）",
             "ラインが止まっていないか、投入待ちが依存や保留で詰まっていないかを確かめる。止めているなら再開の時期を決める")
    fc = val("pace", "残りを今のペースで終えるまでの見込み")
    flag("pace", "残りを今のペースで終えるまでの見込み", _lvl(fc, *THRESHOLDS["forecast_days"]), f"残りを終えるまで今のペースで {fc} 日かかる見込み",
         "期日に間に合うかを確かめる。間に合わないなら、範囲を絞る（優先度の低いものを保留）か、流量を上げる（factoryctl line scale）")
    stale = val("pace", "進めているもののうち最も長く動いていない")
    flag("pace", "進めているもののうち最も長く動いていない", _lvl(stale, *THRESHOLDS["stale_days"]), f"進めているチケットが {stale} 日動いていない",
         "そのチケットが何を待っているか（判断・CI・衝突）を確かめる。大きすぎるならプランナーで切り直す")
    held = val("roadmap", "保留中")
    flag("roadmap", "保留中", _lvl(held, *THRESHOLDS["held"]), f"保留中のチケットが {held} 件ある",
         "保留の理由を見直し、再開するか、ロードマップから外すかを決める")
    nxt = [i for s in rep.sections if s.id == "next" for i in s.items if i.kind == "entry"]
    if nxt and all(i.state.endswith("完了待ち") for i in nxt):
        for i in nxt:
            i.level = "warn"
        found.append({"level": "warn", "text": "次に流すものがすべて、ほかのチケットの完了待ち",
                      "action": "依存の先のチケットを先に流すよう優先度を上げる（factoryctl priority）。依存が誤りなら外す"})

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


