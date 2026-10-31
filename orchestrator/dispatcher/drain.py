"""ラインの一時停止（drain）と再開（ADR 0018）。工場の入れ替えのために、外から中に干渉する口。

dispatcher・factoryctl・操業コンソール・番人が同じ関数を通る。状態の正本は events（最後の factory.drain_requested / factory.drain_released）。
一時停止の間、dispatcher は順番待ちのジョブを取り出さず、割り込みもしない。実行中のジョブは最後まで流す。
「入れ替えてよい」は、一時停止中で、実行中のジョブが 0 件で、出荷の依頼が処理中でないとき。
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

import ship

EVENTS = ("factory.drain_requested", "factory.drain_released")


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


def status(cur) -> dict:
    """入れ替えてよいか。running はつなぎ直しを含む実行中のジョブ、shipping は処理中の出荷の依頼"""
    st = state(cur)
    cur.execute("select id, kind, repo, issue_number, extract(epoch from now() - started_at)::int as elapsed_sec "
                "from jobs where state='running' order by id")
    running = [dict(r) for r in cur.fetchall()]
    shipping = [{"name": e["name"], "release_id": e["payload"].get("release_id") or e["payload"].get("repo")} for e in ship.pending(cur)]
    return {"draining": st is not None, "since": st["since"].isoformat() if st else None, "by": st["by"] if st else None,
            "reason": st["reason"] if st else "", "running": running, "shipping": shipping,
            "ready": st is not None and not running and not shipping}
