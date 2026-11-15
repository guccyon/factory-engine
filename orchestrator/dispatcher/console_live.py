"""操業コンソールの事業部の様子（/live）の補い（GET /api/live）。読むだけ。

- 工程（STEPS）とジョブの種類の対応: 画面は段を工程に揃え、段の中の左右の位置で状態（待ち・作業中・次へ渡す待ち）を出す
- 推移（trend）: 直近 TREND_HOURS 時間の作業中・順番待ちの数を、jobs の時刻から BUCKET_MIN 分ごとに数え直す。
  画面を開いた直後から線が見え、日をまたいでも同じ数になる
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import usage

# 工程の順（左から右へ流れる順ではなく、画面の段の上から下の順）。fixed は件がなくても段を出す
STEPS = [
    {"id": "plan", "name": "計画", "fixed": True, "kinds": ["plan", "clarify", "spike"]},
    {"id": "build", "name": "実装", "fixed": True, "kinds": ["implement", "review_fix", "conflict", "body_fix", "ui_design", "design_system"]},
    {"id": "review", "name": "検査", "fixed": True, "kinds": ["inspect", "judge", "ui_design_review"]},
    {"id": "qa", "name": "QA", "fixed": True, "kinds": ["qa"]},
    {"id": "ship", "name": "出荷", "fixed": True, "kinds": []},
    {"id": "governance", "name": "委員会・分析", "fixed": False,
     "kinds": ["committee_review", "committee_weekly", "case_review", "design_review", "incident_review", "investigate", "onboard"]},
    {"id": "desk", "name": "窓口", "fixed": False, "kinds": ["desk_consult", "desk_answer", "desk_task"]},
]
KIND_JA = {**usage.KIND_JA, "qa": "QA", "investigate": "一次調査", "onboard": "オンボーディングの解析",
           "desk_consult": "窓口の相談", "desk_answer": "窓口の返事", "desk_task": "窓口の作業"}
TREND_HOURS = 6
BUCKET_MIN = 10


def trend(cur, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(hours=TREND_HOURS)
    cur.execute("""select t,
                          count(*) filter (where j.started_at <= t and coalesce(j.finished_at, %(now)s) > t) as running,
                          count(*) filter (where j.enqueued_at <= t and coalesce(j.started_at, j.finished_at, %(now)s) > t) as queued
                     from generate_series(%(start)s::timestamptz, %(now)s::timestamptz, %(step)s::interval) t
                     left join jobs j on j.enqueued_at <= t and coalesce(j.finished_at, %(now)s) > t
                    group by t order by t""", {"now": now, "start": start, "step": f"{BUCKET_MIN} minutes"})
    return [{"at": r["t"], "running": int(r["running"]), "queued": int(r["queued"])} for r in cur.fetchall()]


def view(cur, now: datetime | None = None) -> dict:
    return {"steps": STEPS, "kinds": KIND_JA, "trend": trend(cur, now), "trend_hours": TREND_HOURS, "bucket_min": BUCKET_MIN}
