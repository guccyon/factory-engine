"""Learn ループの Retrospective（ADR 0013 の 1・2）。

教師信号を制御プレーンの記録（artifacts の Judgment、events の judgment.graded・sendback.*）から集め、signature で束ねる。
決定的（LLM を使わない）。同じ記録と同じ期間には同じ束を返す。Lesson を書く learn ジョブはこの束を入力にする（別の PR）。
数え方は definitions/learning.yaml（teacher_signals・retrospective・knowledge_stages）に合わせる。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

WINDOW_DAYS = 30
RUBRIC_MIN_SIGNALS = 3        # Stage 2（rubric）の候補: 同じ signature が窓の中で 3 件以上
RUBRIC_MIN_TICKETS = 2        # かつ 2 つ以上のチケットから（独立に発生した）
EVAL_SOURCES_MAX = 5          # 束 1 つにつき残す eval ケースの元（元 PR と修正 PR の組）の数


def signature_of(category: str | None, defect: dict | None) -> str:
    if category == "inspection":
        return f"rubric:{(defect or {}).get('rubric_id') or '?'}"
    return f"judgment:{category or 'unknown'}"


def collect_signals(cur, frm: datetime, to: datetime) -> list[dict]:
    """期間の教師信号。1 件ごとに {kind, signature, pr_url, ticket, at, artifact_id}"""
    out = []
    # 検査の指摘を judge が採用した（inspection_adopted）／仮決めを judge が差し替えた（judgment_replaced）
    cur.execute("""select id, correlation_id, created_at, payload->>'category' as c, payload->>'verdict' as v, payload->'defect' as d, payload->>'pr_url' as pr
                     from artifacts where type='Judgment' and payload->>'stage'='judged' and created_at >= %s and created_at < %s order by id""", (frm, to))
    for r in cur.fetchall():
        if r["c"] == "inspection" and r["v"] == "adopt":
            kind = "inspection_adopted"
        elif r["c"] != "inspection" and r["v"] in ("replace", "reject"):
            kind = "judgment_replaced"
        else:
            continue
        out.append({"kind": kind, "signature": signature_of(r["c"], r["d"]), "pr_url": r["pr"], "ticket": r["correlation_id"],
                    "at": r["created_at"], "artifact_id": r["id"]})
    # 人が judge に同意しなかった（human_disagree）。採点した記録の類型と指摘で束ねる
    cur.execute("""select e.occurred_at, a.id, a.correlation_id, a.payload->>'category' as c, a.payload->'defect' as d, a.payload->>'pr_url' as pr
                     from events e join artifacts a on a.id = (e.payload->>'artifact_id')::bigint
                    where e.name='judgment.graded' and e.payload->>'grade'='disagree' and e.occurred_at >= %s and e.occurred_at < %s order by e.id""", (frm, to))
    for r in cur.fetchall():
        out.append({"kind": "human_disagree", "signature": signature_of(r["c"], r["d"]), "pr_url": r["pr"], "ticket": r["correlation_id"],
                    "at": r["occurred_at"], "artifact_id": r["id"]})
    return out


def fix_pr_of(cur, origin_pr: str) -> str | None:
    """差し戻しの修正 PR（eval ケースの負例の元）。sendback.filed の fix_corr から、その Issue で工場が出した PR をたどる"""
    cur.execute("select payload->>'fix_corr' as c from events where name='sendback.filed' and payload->>'origin_pr'=%s order by id limit 1", (origin_pr,))
    row = cur.fetchone()
    if not row:
        return None
    cur.execute("select payload->>'pr_url' as u from artifacts where type='PR' and correlation_id=%s order by id limit 1", (row["c"],))
    pr = cur.fetchone()
    return pr["u"] if pr else None


def retrospect(cur, now: datetime | None = None, window_days: int = WINDOW_DAYS) -> dict:
    """直近 window_days 日の教師信号を signature で束ねる。並びは件数の多い順、同数は signature の順"""
    now = now or datetime.now(timezone.utc)
    frm = now - timedelta(days=window_days)
    signals = collect_signals(cur, frm, now)
    groups: dict[str, dict] = {}
    for s in signals:
        g = groups.setdefault(s["signature"], {"signature": s["signature"], "signals": 0, "by_kind": {}, "tickets": set(), "prs": set()})
        g["signals"] += 1
        g["by_kind"][s["kind"]] = g["by_kind"].get(s["kind"], 0) + 1
        g["tickets"].add(s["ticket"])
        if s["pr_url"]:
            g["prs"].add(s["pr_url"])
    clusters = []
    for g in sorted(groups.values(), key=lambda g: (-g["signals"], g["signature"])):
        eval_sources = [{"positive": pr, "negative": fix_pr_of(cur, pr)} for pr in sorted(g["prs"])[:EVAL_SOURCES_MAX]]
        stage = "rubric" if g["signals"] >= RUBRIC_MIN_SIGNALS and len(g["tickets"]) >= RUBRIC_MIN_TICKETS else "episodic"
        clusters.append({"signature": g["signature"], "signals": g["signals"], "by_kind": g["by_kind"], "tickets": len(g["tickets"]),
                         "prs": len(g["prs"]), "stage_candidate": stage, "eval_sources": eval_sources,
                         "eval_ready": any(e["negative"] for e in eval_sources)})
    return {"from": frm.isoformat(), "to": now.isoformat(), "window_days": window_days, "signals": len(signals), "clusters": clusters}


def record_retrospective(cur, retro: dict, day: str) -> bool:
    """Retrospective を artifacts と events（learn.retrospected）に 1 日 1 回残す。すでにあれば何もしない"""
    cur.execute("insert into events(name, payload, dedupe_key) values ('learn.retrospected', %s, %s) on conflict (dedupe_key) do nothing returning id",
                (Jsonb({"day": day, "signals": retro["signals"], "clusters": len(retro["clusters"]),
                        "rubric_candidates": sum(1 for c in retro["clusters"] if c["stage_candidate"] == "rubric")}), f"learn.retrospected:{day}"))
    if not cur.fetchone():
        return False
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values ('Retrospective', %s, 'learn', %s)",
                (f"learn:{day}", Jsonb({**retro, "day": day})))
    return True


def report_lines(retro: dict) -> list[str]:
    out = [f"## 教師信号の束（直近 {retro['window_days']} 日、{retro['signals']} 件）", ""]
    if not retro["clusters"]:
        return out + ["- なし"]
    out += ["| signature | 件数 | 内訳 | チケット | 段階の候補 | eval の元（修正 PR あり） |", "|---|---|---|---|---|---|"]
    for c in retro["clusters"]:
        kinds = "、".join(f"{k} {n}" for k, n in sorted(c["by_kind"].items()))
        ready = sum(1 for e in c["eval_sources"] if e["negative"])
        out.append(f"| {c['signature']} | {c['signals']} | {kinds} | {c['tickets']} | {c['stage_candidate']} | {ready}/{len(c['eval_sources'])} |")
    out += ["", "- 段階の候補 rubric は同じ signature が 3 件以上・2 チケット以上（definitions/learning.yaml）。eval の元は元 PR（正例）と差し戻しの修正 PR（負例）の組で、修正 PR がない束は負例を別に探す",
            "- まだ数えていない信号: revert・hotfix・Churn（pr_metrics）、Golden Set の負例（製品の evals/golden）"]
    return out
