"""プロダクト開発のレポート（report.product、ADR 0032）の組み立て。中身は仮置き（definitions/reports.yaml の status: provisional）。

まずはロードマップの進捗。工場のチケット（tickets、ADR 0009）と、supervisor が残す pr.merged・release.created から出す。
GitHub を直接見ないので、期間は引数で受けて、ダッシュボードから任意の期間でも同じ中身を作れる。
対象の製品は通知の設定を読む製品（notify.config_repo()）。中身を決めるタスクでは、ここと reports.yaml の節の一覧を書き換える。
"""
from __future__ import annotations

from datetime import datetime

import notify
from report import Report, Section, entry, metric, note

KIND = "report.product"
TITLE = "プロダクト開発の進捗"
# definitions/reports.yaml の report.product の節の写し（食い違いはテストで見る）
SECTIONS = {
    "roadmap": ("ロードマップ", ("provider", "business_owner")),
    "done": ("済んだもの", ("provider", "business_owner")),
    "doing": ("進めているもの", ("provider", "business_owner")),
    "next": ("次に流すもの", ("provider", "business_owner")),
    "releases": ("版", ("provider", "business_owner")),
}
LIST_MAX = 8   # 一覧はここまで。残りは件数だけ


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


BUILDERS = {"roadmap": roadmap, "done": done, "doing": doing, "next": next_, "releases": releases}


def build(cur, start: datetime, end: datetime, repo: str | None = None) -> Report:
    repo = repo or notify.config_repo()
    rep = Report(KIND, TITLE, start, end, provisional=True)
    for sid, (title, parties) in SECTIONS.items():
        try:
            items = BUILDERS[sid](cur, repo, start, end)
        except Exception as e:  # noqa: BLE001  1 つの節が読めなくても残りは出す
            items = [note(f"読めなかった（{type(e).__name__}）")]
        rep.sections.append(Section(sid, title, parties, items))
    return rep
