"""見回りの見回り（ADR 0050 の決定 1）。見回りや定期のジョブが黙って止まったら、番人が停止 patrol:<id> を開く。

- 決定論で、LLM は使わない。番人（dispatcher の watch_once）が周回ごとに呼ぶ
- 対象と期限は下の小さな表（TARGETS）。id は Gen1.5 の見回りの台帳（definitions/patrols.yaml）の id に引き継ぐ
- 有効かどうかは supervisor が決める。supervisor は heartbeat の payload.patrols に、いま有効な見回りの id を書く（enabled_ids）。
  番人は最新の heartbeat に載っている id だけを見る。heartbeat が途絶えたときは停止 heartbeat: が出るので、ここでは開かない
- 最後に動いた記録（events）が期限より古ければ開き、新しい記録が付いたら閉じる。記録が 1 度もないものは、
  見回りの見回りが動き始めた時刻（patrol_watch.started）から数える
- ラインの一時停止中と、予算の枠を使い切っている間は新しく開かない（意図して止めている）。開いている停止は記録が付くまで残す
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import stoppage

DAY = 24
WEEK = 8 * 24   # 週 1 回のものは 1 日の余裕を見る
# (id, 名前, 動いた記録の events の name, 期限（時間）)。期限は仮置き（運用パラメータとして工場が調整してよい）
TARGETS: list[tuple[str, str, tuple[str, ...], int]] = [
    ("nightly_explore", "夜間の探索", ("supervisor.nightly_explore_enqueued", "supervisor.nightly_explore_skipped"), DAY + 12),
    ("synthetic_users", "合成の利用者", ("supervisor.synthetic_users_enqueued",), WEEK),
    ("design_review", "設計の診断", ("supervisor.design_review_enqueued",), WEEK),
    ("committee_weekly", "知見の委員会の定例", ("supervisor.committee_weekly_enqueued",), WEEK),
    ("incident_daily", "事故の日次の見回り", ("fincident.patrolled",), DAY + 12),
    ("incident_weekly", "事故の週次の見回り", ("supervisor.incident_review_enqueued",), WEEK),
    ("daily_report", "建設状況の日報", ("report.daily",), DAY + 12),
    ("notify_report", "定期レポート（操業・開発状況）", ("report.built",), DAY + 12),
    ("retrospective", "振り返り", ("learn.retrospected",), DAY + 12),
    ("rule_transition", "ルールの移り変わり", ("learn.rule_transitions_checked",), DAY + 12),
    ("quality_learn", "品質の Learn", ("quality.learned",), WEEK),
    ("kpi_weekly", "週次の KPI", ("report.kpi_weekly", "report.kpi_skipped"), WEEK),
]
IDS = [t[0] for t in TARGETS]
PREFIX = "patrol:"
STARTED = "patrol_watch.started:v1"


def enabled_ids(flags: dict[str, bool]) -> list[str]:
    """supervisor が heartbeat に書く一覧。flags は {id: 有効か}。表にない id は落とす"""
    return [i for i in IDS if flags.get(i)]


def _declared(cur) -> list[str] | None:
    """最新の heartbeat が宣言した有効な見回り。heartbeat がない・古い版（patrols がない）なら None"""
    cur.execute("select payload->'patrols' as p from events where name='supervisor.heartbeat' order by id desc limit 1")
    r = cur.fetchone()
    if not r or r["p"] is None:
        return None
    return [i for i in r["p"] if i in IDS]


def _last(cur, names: tuple[str, ...]):
    cur.execute("select max(occurred_at) as t from events where name = any(%s)", (list(names),))
    return cur.fetchone()["t"]


def overdue(cur, now: datetime | None = None) -> dict[str, dict]:
    """期限を過ぎた見回り {id: {name, last, hours, since}}。heartbeat の宣言にあるものだけ"""
    now = now or datetime.now(timezone.utc)
    declared = _declared(cur)
    if not declared:
        return {}
    cur.execute("insert into events(name, payload, dedupe_key, occurred_at) values ('patrol_watch.started', '{}', %s, %s) on conflict (dedupe_key) do nothing",
                (STARTED, now))
    cur.execute("select occurred_at from events where dedupe_key=%s", (STARTED,))
    started = cur.fetchone()["occurred_at"]
    out = {}
    for pid, name, names, hours in TARGETS:
        if pid not in declared:
            continue
        last = _last(cur, names)
        since = max(t for t in (last, started) if t is not None)
        if now - since > timedelta(hours=hours):
            out[pid] = {"name": name, "last": last, "hours": hours, "since": since}
    return out


def step(cur, *, paused: bool, heartbeat_stale: bool, now: datetime | None = None) -> dict:
    """停止を開け閉めする。paused: 一時停止中か予算の枠を使い切っている。heartbeat_stale: supervisor の heartbeat が途絶えている。
    返り値 {opened: [...], closed: [...]}"""
    opened, closed = [], []
    if heartbeat_stale:
        return {"opened": opened, "closed": closed}   # supervisor ごと止まっている。heartbeat: の停止に任せる
    late = overdue(cur, now)
    declared = _declared(cur) or []
    for k in stoppage.open_keys(cur, PREFIX):
        pid = k[len(PREFIX):]
        if pid not in late:
            why = "見回りが動いた" if pid in declared else "見回りを止めた（supervisor の宣言から外れた）"
            if stoppage.close_stoppage(cur, k, remedy=why):
                closed.append(pid)
    if paused:
        return {"opened": opened, "closed": closed}
    for pid, x in late.items():
        when = f"最後に動いたのは {x['last']:%m-%d %H:%M}（UTC）" if x["last"] else "見回りの見回りを入れてから一度も動いていない"
        if stoppage.open_stoppage(cur, f"{PREFIX}{pid}", "factory-bug", f"見回り「{x['name']}」（{pid}）が {x['hours']} 時間動いていない。{when}",
                                  waiting_for="supervisor のログで止まった理由を確かめる（設定・定義の読み込み・GitHub の失敗など）",
                                  started_at=x["since"] + timedelta(hours=x["hours"])):
            opened.append(pid)
    return {"opened": opened, "closed": closed}
