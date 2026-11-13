"""ラインの流量の宣言（ADR 0028）。あるべき数を宣言し、dispatcher と supervisor が周回ごとに今の姿をそれに合わせる。

Kubernetes の ReplicaSet と同じ考え方（2026-11-05 オーナー）。上げ直さずに、操業コンソール・factoryctl・API から変えられる。
  workers     ラインのワーカーの数（同時に動かすジョブ。委員会と知見の流れは数えない）
  tickets     ラインに同時に流すチケットの数（ジョブ＋開いている工場の PR。supervisor が投入を止める）
  governance  委員会と知見の流れのワーカーの数（ADR 0024。ラインの一時停止でも止めない）
  desk        窓口の相談のワーカーの数（ADR 0030 の 6。相談役・回答役。ラインの一時停止でも止めず、ラインのワーカーを奪わない）

正本は events の line.scaled（最後のもの。変えた人・理由・時刻も残る）。一度も宣言していなければ環境変数の値。
数を下げても、動いているジョブは止めずに最後まで流す（新しく取り出さないだけ）。0 にすればその枠は取り出さない。
"""
from __future__ import annotations

import os

from psycopg.types.json import Jsonb

EVENT = "line.scaled"
FIELDS = ("workers", "tickets", "governance", "desk")
LIMIT = 50

# 委員会と知見の流れのジョブ（ADR 0024 の 2026-11-04 の追記）。jobs を q と呼ぶ SQL の条件
GOVERNANCE_KINDS = ("committee_review", "committee_weekly", "case_review", "incident_review", "investigate")   # incident_review は事故の調査委員会（ADR 0046）。知見の委員会とは別なので COMMITTEE_KINDS に入れない。investigate は工場の中での一次調査（ADR 0048。長くかかるので窓口の枠は塞がない）
GOVERNANCE_WHERE = ("(q.kind = any(%(gov_kinds)s) or q.correlation_id like 'committee:%%' "
                    "or q.correlation_id in (select f.correlation_id from events f where f.name='knowledge.case_filed'))")
# 窓口の相談のジョブ（ADR 0030 の 6）。ラインにも委員会の枠にも数えない
DESK_KINDS = ("desk_consult", "desk_answer", "desk_task")
DESK_WHERE = "(q.kind = any(%(desk_kinds)s))"
LINE_WHERE = f"(not {GOVERNANCE_WHERE} and not {DESK_WHERE})"
# 知見の委員会は同時に 1 つだけ（2026-11-08 オーナー）。並べて流すと知見が競合・矛盾する。後の検討が前の議決を直すこともあるので、
# 検討の課題はジョブとして順番待ちに積み、委員会は 1 つずつ順に処理する。governance の枠を増やしても委員会どうしは並ばない
COMMITTEE_KINDS = ("committee_review", "committee_weekly")
ARGS = {"gov_kinds": list(GOVERNANCE_KINDS), "desk_kinds": list(DESK_KINDS), "committee_kinds": list(COMMITTEE_KINDS)}


def defaults() -> dict:
    e = os.environ
    return {"workers": int(e.get("MAX_PARALLEL", "2")), "tickets": int(e.get("SUPERVISOR_MAX_INFLIGHT", "1")),
            "governance": int(e.get("GOVERNANCE_PARALLEL", "1")), "desk": int(e.get("DESK_PARALLEL", "1"))}


def spec(cur) -> dict:
    """いま宣言されている数。{workers, tickets, governance, desk, by, reason, at}。desk を足す前の宣言は desk だけ環境変数の値"""
    out = {**defaults(), "by": "env", "reason": "", "at": None}
    cur.execute("select payload, occurred_at from events where name=%s order by id desc limit 1", (EVENT,))
    r = cur.fetchone()
    if r:
        out.update({k: int(r["payload"][k]) for k in FIELDS if isinstance(r["payload"].get(k), int)})
        out.update({"by": r["payload"].get("by"), "reason": r["payload"].get("reason") or "", "at": r["occurred_at"].isoformat()})
    return out


def scale(cur, by: str, reason: str = "", **values) -> dict:
    """数を宣言する。渡したものだけ変え、ほかは今の値のまま。返り値は新しい宣言"""
    cur_spec = spec(cur)
    new = {k: cur_spec[k] for k in FIELDS}
    for k, v in values.items():
        if v is None:
            continue
        if k not in FIELDS or not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= LIMIT:
            raise ValueError(f"{k} は 0〜{LIMIT} の整数")
        new[k] = v
    cur.execute("insert into events(name, payload) values (%s, %s)", (EVENT, Jsonb({**new, "by": by, "reason": reason})))
    return spec(cur)


def running(cur) -> dict:
    """今動いているジョブの数。{workers: ラインのもの, governance: 委員会と知見の流れのもの, desk: 窓口の相談のもの}"""
    cur.execute(f"select count(*) filter (where {GOVERNANCE_WHERE}) as g, count(*) filter (where {DESK_WHERE}) as d, "
                f"count(*) filter (where {LINE_WHERE}) as w from jobs q where q.state='running'", ARGS)
    r = cur.fetchone()
    return {"workers": r["w"], "governance": r["g"], "desk": r["d"]}


# チケットの優先度が下限以上のジョブ。プランナーのジョブは ticket_id を持たないので、correlation_id（github:<owner/repo#n>）でもチケットを引く
MIN_PRIORITY_WHERE = ("(q.priority >= %(minp)s or exists (select 1 from tickets t where t.priority >= %(minp)s and "
                      "(t.id = q.ticket_id or (t.source = 'github' and 'github:' || t.source_ref = q.correlation_id))))")


def _claim(cur, where: str, corrs: list[str] | None = None, min_priority: int | None = None) -> dict | None:
    """corrs か min_priority を渡すと、そのどちらかに当たるジョブに絞る（一時停止中の drain allow）"""
    conds = (["q.correlation_id = any(%(corrs)s)"] if corrs else []) + ([MIN_PRIORITY_WHERE] if min_priority is not None else [])
    if corrs is not None and not conds:
        conds = ["false"]
    extra = f" and ({' or '.join(conds)})" if conds else ""
    cur.execute(f"""select q.id from jobs q where q.state='queued' and not q.cancel_requested and {where}{extra}
                      and not exists (select 1 from tickets t where t.held and (t.id = q.ticket_id or (t.source = 'github' and 'github:' || t.source_ref = q.correlation_id)))
                    order by q.priority desc, q.enqueued_at for update skip locked limit 1""",
                {**ARGS, "corrs": corrs or [], "minp": min_priority})
    r = cur.fetchone()
    if not r:
        return None
    cur.execute("update jobs set state='running', started_at=now() where id=%s returning *", (r["id"],))
    return cur.fetchone()


def claim_line(cur, corrs: list[str] | None = None, min_priority: int | None = None) -> dict | None:
    """ラインの枠に空きがあれば、ラインのジョブを 1 つ取り出す。corrs・min_priority を渡すとそれに絞る（一時停止中の drain allow）"""
    if running(cur)["workers"] >= spec(cur)["workers"]:
        return None
    if min_priority is not None and corrs is None:
        corrs = []
    return _claim(cur, LINE_WHERE, corrs, min_priority)


def claim_governance(cur) -> dict | None:
    """委員会と知見の流れの枠に空きがあれば、そのジョブを 1 つ取り出す。ラインの一時停止の間も取り出す"""
    if running(cur)["governance"] >= spec(cur)["governance"]:
        return None
    cur.execute("select 1 from jobs where state='running' and kind = any(%s) limit 1", (list(COMMITTEE_KINDS),))
    if cur.fetchone():   # 委員会が動いている間は、ほかの委員会のジョブを取り出さない（事例の分析などは取り出す）
        return _claim(cur, f"({GOVERNANCE_WHERE} and not (q.kind = any(%(committee_kinds)s)))")
    return _claim(cur, GOVERNANCE_WHERE)


def claim_desk(cur) -> dict | None:
    """窓口の枠に空きがあれば、相談のジョブを 1 つ取り出す。ラインの一時停止の間も取り出す（ADR 0030 の 6）"""
    if running(cur)["desk"] >= spec(cur)["desk"]:
        return None
    return _claim(cur, DESK_WHERE)


def tickets_inflight(cur) -> int | None:
    """同時に流しているチケットの数（supervisor が周回ごとに heartbeat に残す値。開いた PR も数えるので GitHub を見る supervisor が数える）"""
    cur.execute("select (payload->>'tickets_inflight')::int as n from events where name='supervisor.heartbeat' and payload ? 'tickets_inflight' "
                "order by occurred_at desc limit 1")
    row = cur.fetchone()
    return row["n"] if row else None


def status(cur) -> dict:
    s, r = spec(cur), {**running(cur), "tickets": tickets_inflight(cur)}
    return {"desired": {k: s[k] for k in FIELDS}, "running": r, "by": s["by"], "reason": s["reason"], "at": s["at"]}
