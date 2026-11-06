"""ラインの一時停止（drain）と再開（ADR 0020）。工場の入れ替えのために、外から中に干渉する口。

dispatcher・factoryctl・操業コンソール・番人が同じ関数を通る。状態の正本は events（最後の factory.drain_requested / factory.drain_released）。
一時停止の間、dispatcher は順番待ちのジョブを取り出さず、割り込みもしない。実行中のジョブは最後まで流す。
「入れ替えてよい」は、一時停止中で、実行中のジョブが 0 件で、出荷の依頼が処理中でないとき。
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

import line
import ship

EVENTS = ("factory.drain_requested", "factory.drain_released")
# 一時停止の間だけ流すもの（2026-11-04 オーナー: ラインは止めたまま、知見の PR だけ流す）。今の一時停止が続く間だけ効き、再開で消える
ALLOW_EVENT = "factory.drain_allowed"


def state(cur) -> dict | None:
    """一時停止中なら {since, by, reason, minutes}、そうでなければ None"""
    cur.execute("select name, payload, occurred_at, extract(epoch from now() - occurred_at)::int / 60 as minutes "
                "from events where name = any(%s) order by id desc limit 1", (list(EVENTS),))
    r = cur.fetchone()
    if not r or r["name"] != "factory.drain_requested":
        return None
    return {"since": r["occurred_at"], "by": r["payload"].get("by"), "reason": r["payload"].get("reason") or "", "minutes": r["minutes"]}


def active(cur) -> bool:
    return state(cur) is not None


def set_drain(cur, on: bool, by: str, reason: str = "") -> bool:
    """一時停止にする（on）か再開する。すでにその状態なら何も記録しない。返り値は状態を変えたか"""
    if active(cur) == on:
        return False
    cur.execute("insert into events(name, payload) values (%s, %s)",
                ("factory.drain_requested" if on else "factory.drain_released", Jsonb({"by": by, "reason": reason})))
    return True


def allow(cur, corrs: list[str], by: str, reason: str = "", min_priority: int | None = None) -> bool:
    """一時停止の間も流してよいジョブの correlation_id を足す（今の一時停止の間だけ）。一時停止していなければ記録しない。
    min_priority を渡すと、優先度がその値以上のチケットのジョブも流す（2026-11-06 オーナー: ラインは止めたまま、検証のチケットを p0 で流す）"""
    if not active(cur):
        return False
    prev = _allow_payload(cur)
    corrs = sorted(set(prev.get("corrs") or []) | set(corrs))
    mp = min_priority if min_priority is not None else prev.get("min_priority")
    cur.execute("insert into events(name, payload) values (%s, %s)",
                (ALLOW_EVENT, Jsonb({"corrs": corrs, "min_priority": mp, "by": by, "reason": reason})))
    return True


def _allow_payload(cur) -> dict:
    """今の一時停止の間の最後の drain_allowed の中身。一時停止していない・足していなければ空"""
    cur.execute("select id from events where name = any(%s) order by id desc limit 1", (list(EVENTS),))
    last = cur.fetchone()
    if not last or not active(cur):
        return {}
    cur.execute("select payload from events where name=%s and id > %s order by id desc limit 1", (ALLOW_EVENT, last["id"]))
    r = cur.fetchone()
    return dict(r["payload"]) if r else {}


def allowed(cur) -> list[str]:
    """今の一時停止の間に流してよい correlation_id。一時停止していない・足していなければ空"""
    return list(_allow_payload(cur).get("corrs") or [])


def allowed_min_priority(cur) -> int | None:
    """今の一時停止の間に流してよいチケットの優先度の下限（factoryctl drain allow --priority）。なければ None"""
    v = _allow_payload(cur).get("min_priority")
    return int(v) if isinstance(v, int) else None


def claim_allowed(cur, corrs: list[str], min_priority: int | None = None) -> dict | None:
    """一時停止の間に、流してよいジョブだけを 1 つ取り出す（ラインの枠の空きの範囲で。line.claim_line）"""
    if not corrs and min_priority is None:
        return None
    return line.claim_line(cur, corrs, min_priority)


# 委員会と知見の流れ（ADR 0024 の 2026-11-04 の追記）はラインとは別の枠で、一時停止でも止めない。数と取り出し方は line に 1 か所
GOVERNANCE_KINDS = line.GOVERNANCE_KINDS


def claim_governance(cur) -> dict | None:
    return line.claim_governance(cur)


def status(cur) -> dict:
    """入れ替えてよいか。running はつなぎ直しを含む実行中のジョブ、shipping は処理中の出荷の依頼"""
    st = state(cur)
    cur.execute("select id, kind, repo, issue_number, extract(epoch from now() - started_at)::int as elapsed_sec "
                "from jobs where state='running' order by id")
    running = [dict(r) for r in cur.fetchall()]
    shipping = [{"name": e["name"], "release_id": e["payload"].get("release_id") or e["payload"].get("repo")} for e in ship.pending(cur)]
    return {"draining": st is not None, "since": st["since"].isoformat() if st else None, "by": st["by"] if st else None,
            "reason": st["reason"] if st else "", "running": running, "shipping": shipping, "allowed": allowed(cur),
            "allowed_min_priority": allowed_min_priority(cur),
            "ready": st is not None and not running and not shipping}
