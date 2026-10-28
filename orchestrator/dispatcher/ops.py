"""工場の操業の操作（ADR 0009）。チケットの写し・優先度・保留・停止・割り込みの候補選び。

dispatcher・supervisor・factoryctl・操業コンソールが同じ関数を通る。入口ごとに振る舞いを変えないため。
優先度・保留の正本は制御プレーンの tickets。入口のラベル（GitHub の factory:priority:*）は最初の値を渡すだけで、
コンソールや factoryctl で変えた値は上書きしない。すべての操作は events に「誰が・何を・なぜ」で残す。
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

# 優先度の段（内部は 0〜100 の数。ラベルは 4 段から始める。2026-10-28 Tetsuro）
LEVELS = {
    "p0": (100, "緊急", "作業中のものを止めてでも先に流す（障害の調査・hotfix）"),
    "p1": (75, "急ぎ", "順番待ちの先頭に回す"),
    "p2": (50, "通常", "受け付けた順"),
    "p3": (25, "後回し", "ほかに流すものがないときに流す"),
}
DEFAULT_PRIORITY = LEVELS["p2"][0]
PREEMPT_AT = LEVELS["p0"][0]          # この値以上のジョブが待っていて枠が空いていなければ、低いジョブを止めて割り込む
PREEMPTIBLE_KINDS = ("implement", "clarify")   # 検査・judge・本文の追記は短いので止めない
PRIORITY_LABEL_PREFIX = "factory:priority:"
SETTERS_OVERRIDABLE_BY_SOURCE = ("default", "source")


def level_of(priority: int) -> str:
    """数から段の名前（その数以下で最も高い段）"""
    for key, (value, _, _) in sorted(LEVELS.items(), key=lambda kv: -kv[1][0]):
        if priority >= value:
            return key
    return "p3"


def priority_from_labels(labels: list[str]) -> int | None:
    """入口のラベル（factory:priority:p0 など）から数。複数付いていれば最も高いもの。なければ None"""
    values = [LEVELS[l[len(PRIORITY_LABEL_PREFIX):]][0] for l in labels
              if l.startswith(PRIORITY_LABEL_PREFIX) and l[len(PRIORITY_LABEL_PREFIX):] in LEVELS]
    return max(values) if values else None


def upsert_ticket(cur, source: str, source_ref: str, repo: str, *, title: str = "", url: str | None = None,
                  priority: int | None = None, state: str | None = None,
                  depends_on: list[str] | None = None, waiting_on: list[str] | None = None) -> dict:
    """入口のサービスのチケットを工場の tickets に写す。返り値は写したあとの行"""
    cur.execute("select * from tickets where source=%s and source_ref=%s for update", (source, source_ref))
    t = cur.fetchone()
    if not t:
        cur.execute("insert into tickets(source, source_ref, repo, title, url, state, priority, priority_set_by, depends_on, waiting_on) "
                    "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (source, source_ref) do nothing returning *",
                    (source, source_ref, repo, title, url, state or "active",
                     priority if priority is not None else DEFAULT_PRIORITY, "source" if priority is not None else "default",
                     depends_on or [], waiting_on or []))
        t = cur.fetchone()
        if t:
            record(cur, "ticket.received", t, {"source": source, "source_ref": source_ref, "priority": t["priority"]})
            return t
        cur.execute("select * from tickets where source=%s and source_ref=%s for update", (source, source_ref))   # 同時に写された
        t = cur.fetchone()
    sets, args = ["updated_at=now()"], []
    if title:
        sets.append("title=%s"); args.append(title)
    if url:
        sets.append("url=%s"); args.append(url)
    if state:
        sets.append("state=%s"); args.append(state)
    if depends_on is not None:
        sets.append("depends_on=%s"); args.append(depends_on)
    if waiting_on is not None:
        sets.append("waiting_on=%s"); args.append(waiting_on)
    if priority is not None and t["priority_set_by"] in SETTERS_OVERRIDABLE_BY_SOURCE and priority != t["priority"]:
        sets += ["priority=%s", "priority_set_by='source'"]; args.append(priority)
        cur.execute("update jobs set priority=%s where ticket_id=%s and state='queued'", (priority, t["id"]))
        record(cur, "ticket.priority_set", t, {"from": t["priority"], "to": priority, "by": "source"})
    cur.execute(f"update tickets set {', '.join(sets)} where id=%s returning *", (*args, t["id"]))
    return cur.fetchone()


def record(cur, name: str, ticket: dict | None, payload: dict, job_id: int | None = None):
    corr = f"{ticket['source']}:{ticket['source_ref']}" if ticket else None
    cur.execute("insert into events(name, correlation_id, job_id, payload) values (%s,%s,%s,%s)",
                (name, corr, job_id, Jsonb({**payload, **({"ticket_id": ticket["id"]} if ticket else {})})))


def get_ticket(cur, ticket_id: int) -> dict | None:
    cur.execute("select * from tickets where id=%s", (ticket_id,))
    return cur.fetchone()


def set_priority(cur, ticket_id: int, priority: int, by: str, reason: str = "") -> dict | None:
    """優先度を変える。順番待ちのジョブにも反映する。p0 にすると、枠が空いていなければ dispatcher が割り込む"""
    priority = max(0, min(100, int(priority)))
    t = get_ticket(cur, ticket_id)
    if not t:
        return None
    cur.execute("update tickets set priority=%s, priority_set_by=%s, updated_at=now() where id=%s returning *", (priority, by, ticket_id))
    nt = cur.fetchone()
    cur.execute("update jobs set priority=%s where ticket_id=%s and state='queued'", (priority, ticket_id))
    record(cur, "ticket.priority_set", t, {"from": t["priority"], "to": priority, "by": by, "reason": reason})
    return nt


def set_held(cur, ticket_id: int, held: bool, by: str, reason: str = "") -> dict | None:
    """保留（順番待ちから一時的に外す）と再開。作業中のジョブは止めない（止めるのは stop_job）"""
    t = get_ticket(cur, ticket_id)
    if not t:
        return None
    cur.execute("update tickets set held=%s, updated_at=now() where id=%s returning *", (held, ticket_id))
    nt = cur.fetchone()
    record(cur, "ticket.held" if held else "ticket.resumed", t, {"by": by, "reason": reason})
    return nt


def stop_job(cur, job_id: int, by: str, reason: str = "", preempted_for: int | None = None) -> str | None:
    """ジョブを止める。順番待ちならその場で取り消し、作業中なら印を付ける（コンテナは dispatcher が止める）。
    返り値は止める前の状態（動いていなければ None）"""
    cur.execute("select state, correlation_id from jobs where id=%s and state in ('queued','running') for update", (job_id,))
    r = cur.fetchone()
    if not r:
        return None
    cur.execute("update jobs set cancel_requested=true, preempted_for=coalesce(%s, preempted_for), "
                "state=case when state='queued' then 'cancelled' else state end, "
                "finished_at=case when state='queued' then now() else finished_at end where id=%s", (preempted_for, job_id))
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.cancel_requested',%s,%s,%s)",
                (r["correlation_id"], job_id, Jsonb({"by": by, "reason": reason, **({"preempted_for": preempted_for} if preempted_for else {})})))
    return r["state"]


def preempt_victims(cur, max_parallel: int) -> list[tuple[dict, dict]]:
    """割り込みの組（止めるジョブ, 先に流すジョブ）。p0 以上が待っていて、空いた枠で足りない分だけ低いジョブを選ぶ。
    止めたジョブは dispatcher が順番待ちに戻す"""
    cur.execute("select q.* from jobs q where q.state='queued' and not q.cancel_requested and q.priority >= %s "
                "and not exists (select 1 from tickets t where t.id=q.ticket_id and t.held) order by q.priority desc, q.enqueued_at",
                (PREEMPT_AT,))
    urgent = cur.fetchall()
    if not urgent:
        return []
    cur.execute("select count(*) as n from jobs where state='running' and not cancel_requested")
    free = max_parallel - cur.fetchone()["n"]
    need = len(urgent) - max(free, 0)
    if need <= 0:
        return []
    cur.execute("select * from jobs where state='running' and not cancel_requested and priority < %s and kind = any(%s) "
                "order by priority, started_at desc limit %s", (PREEMPT_AT, list(PREEMPTIBLE_KINDS), need))
    return list(zip(cur.fetchall(), urgent))
