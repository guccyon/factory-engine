"""学習の観測データの書き出し（世代や工場をまたいで、学習の効き目を観測し続けるため）。

知見・委員会・overlay のルール・教師信号の記録は、工場の制御プレーンの events と artifacts にある。
Gen1 の工場を閉じても観測を続けられるよう、読むだけで 1 つの JSON にまとめて書き出す。
中身は製品ごとの知見の文面ではなく、出来事の時刻と数（何がいつ起き、どれだけ減ったか）と、判定に使った基準の値。
LLM は使わない。DB は変えない。
"""
from __future__ import annotations

from datetime import datetime, timezone

import learn

FORMAT = "factory.learning-observation/v1"

# 書き出す出来事。学習の 3 つの観測（知見の蓄積・テーマの 1 周・ルールの採用）に要るものだけ
EVENTS = [
    # 知見と委員会（ADR 0024・0040）
    "case.analyzed", "knowledge.case_filed", "knowledge.used", "knowledge.missed",
    "committee.decided", "committee.revise", "committee.enforce_filed", "committee.enforce_joined", "committee.enforce_skipped",
    # overlay のルールの移り変わり（ADR 0013）
    "learn.rule_transition_filed",
    # 効き目を数える分母と分子（PR の流れ・本文の直し・続きのジョブ）
    "supervisor.merged", "pr.body_amended", "continue.enqueued", "taste.flagged",
]
# 出来事の payload から残す鍵。本文や理由の長い文は落とす（数と時刻を観測するため。製品の文面を外に持ち出さない）
KEEP = {"repo", "pr_url", "outcome", "decision", "knowledge", "solution_kind", "issue", "id", "reason", "signal", "work", "role",
        "rule", "to", "from", "kind", "case", "scope", "title"}

# 判定に使った基準の値（ADR 0013 の 6。definitions/learning.yaml の lifecycle と同じ）
CRITERIA = {
    "rule_lifecycle": {"promote_min_fires": learn.PROMOTE_MIN_FIRES, "promote_min_precision": learn.PROMOTE_MIN_PRECISION,
                       "promote_min_days": learn.PROMOTE_MIN_DAYS, "canary_days": learn.CANARY_DAYS,
                       "canary_max_false_positive": learn.CANARY_MAX_FALSE_POSITIVE, "canary_min_judged": learn.CANARY_MIN_JUDGED},
    "knowledge_review_days": {"hypothesis": 30, "confirmed": 90, "settled": None},   # definitions/knowledge.yaml（ADR 0024 の 3）
    "knowledge_unused_days": 60,
}


def _like(repo: str | None) -> str:
    return f"%{repo}%" if repo else "%"


def _slim(p: dict) -> dict:
    return {k: v for k, v in (p or {}).items() if k in KEEP and not (isinstance(v, str) and len(v) > 300)}


def events(cur, since: datetime, repo: str | None) -> list[dict]:
    cur.execute("""select id, name, occurred_at, correlation_id, job_id, payload from events
                    where name = any(%s) and occurred_at >= %s
                      and (correlation_id like %s or coalesce(payload->>'repo','') like %s or coalesce(payload->>'pr_url','') like %s
                           or coalesce(payload->>'issue','') like %s)
                    order by id""", (EVENTS, since, _like(repo), _like(repo), _like(repo), _like(repo)))
    return [{"id": r["id"], "name": r["name"], "at": r["occurred_at"].isoformat(), "corr": r["correlation_id"], "job_id": r["job_id"],
             "payload": _slim(r["payload"])} for r in cur.fetchall()]


def judgments(cur, since: datetime, repo: str | None) -> list[dict]:
    """検査の指摘を judge が裁いた記録（rubric ごと）。overlay のルール（OV-*）の的中率と、rubric の教師信号の推移の元"""
    cur.execute("""select created_at, correlation_id, payload->'defect'->>'rubric_id' as rubric, payload->>'verdict' as verdict,
                          coalesce((payload->'defect'->>'shadow')::boolean, false) as shadow
                     from artifacts where type='Judgment' and payload->>'stage'='judged' and payload->>'category'='inspection'
                      and created_at >= %s and correlation_id like %s order by id""", (since, _like(repo)))
    return [{"at": r["created_at"].isoformat(), "corr": r["correlation_id"], "rubric": r["rubric"], "verdict": r["verdict"],
             "shadow": r["shadow"]} for r in cur.fetchall()]


def jobs_daily(cur, since: datetime, repo: str | None) -> list[dict]:
    """日ごと（JST）・種類ごとのジョブ数。工場の流量の分母"""
    cur.execute("""select (enqueued_at at time zone 'Asia/Tokyo')::date as day, repo, kind, count(*) as n from jobs
                    where enqueued_at >= %s and repo like %s group by 1,2,3 order by 1,2,3""", (since, _like(repo)))
    return [{"day": r["day"].isoformat(), "repo": r["repo"], "kind": r["kind"], "n": r["n"]} for r in cur.fetchall()]


def export(cur, since: datetime, repo: str | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {"format": FORMAT, "generated_at": now.isoformat(), "since": since.isoformat(), "repo": repo,
            "criteria": CRITERIA,
            "rule_stats": learn.rule_stats(cur, since, now),
            "events": events(cur, since, repo), "judgments": judgments(cur, since, repo), "jobs_daily": jobs_daily(cur, since, repo)}
