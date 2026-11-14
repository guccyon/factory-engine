"""操業コンソールの工場の保守のページ（ADR 0059、#477）の中身。読むだけ（記録は書かない）。

ほかのページに散らばっていた工場の側の件をここに集める。工場の不調（束ねた停止、ADR 0058）と建設者宛ての件は /api/state にあるので画面が出す。
ここで足すのは、見回りの結果（patrol_watch の表と事故の日次の見回り、ADR 0046・0050）と、最近の失敗したジョブ（記録は /api/jobs/<id>/log）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import factory_incident
import patrol_watch

JOBS_HOURS = 24   # 最近のジョブとして出す範囲
JOBS_LIMIT = 30


def patrols(cur, now: datetime) -> list[dict]:
    """見回りごとの最後に動いた時刻と期限。patrol_watch.overdue と同じ考え方だが、記録は書かない"""
    declared = patrol_watch._declared(cur)
    cur.execute("select occurred_at from events where dedupe_key=%s", (patrol_watch.STARTED,))
    r = cur.fetchone()
    started = r["occurred_at"] if r else None
    out = []
    for pid, name, names, hours in patrol_watch.TARGETS:
        on = declared is not None and pid in declared
        last = patrol_watch._last(cur, names)
        since = max((t for t in (last, started) if t is not None), default=None)
        late = bool(on and since and now - since > timedelta(hours=hours))
        out.append({"id": pid, "name": name, "enabled": on, "last": last, "hours": hours, "overdue": late,
                    "age_sec": int((now - last).total_seconds()) if last else None})
    return out


def jobs(cur, now: datetime) -> list[dict]:
    """最近うまくいかなかったジョブ（失敗・取り消し）。新しい順"""
    cur.execute("""select id, repo, issue_number, kind, state, attempt, finished_at, error from jobs
                   where state in ('failed','cancelled') and finished_at >= %s order by finished_at desc limit %s""",
                (now - timedelta(hours=JOBS_HOURS), JOBS_LIMIT))
    return [{**r, "error": (r["error"] or "")[:300], "age_sec": int((now - r["finished_at"]).total_seconds())} for r in cur.fetchall()]


def view(cur, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    inc = factory_incident.report_view(cur, now)
    return {"patrols": patrols(cur, now), "incidents": inc["open"], "signs": inc["signs"], "patrolled": inc["patrolled"],
            "outbox": inc["outbox"], "jobs": jobs(cur, now), "jobs_hours": JOBS_HOURS}
