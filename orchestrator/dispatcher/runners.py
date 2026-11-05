"""登録済みのランナー（ADR 0028 の決定 3、ADR 0033）。工場の機械として、ペアリングで登録し、束として把握し、外す。

- ランナーは工場のランナーの窓口（runner_gate.py）へ取りに行く。窓口がランナーの鍵を確かめ、ここの関数で events に書く
- 正本は events（鍵は書かない。鍵は窓口の置き場だけ）:
    runner.pair_requested  {pair_id, code, name, ...}   ペアリングの依頼（6 桁の番号。10 分で切れる）
    runner.pair_approved / runner.pair_rejected {pair_id, by}   操業コンソールの「許可」「拒否」
    runner.registered      {id, ...}   ペアリングが済んだとき・中身（版・道具・端末）が変わったとき
    runner.heartbeat       {id}        窓口が 5 分に 1 回まで（一覧の「動いている」）
    runner.uninstall_requested {id, by} / runner.uninstalled {id}   外す（鍵を捨てる）
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import ops

ID = re.compile(r"^[0-9a-f]{32}$")
FIELDS = ("name", "kind", "version", "label", "path", "platform", "tools", "devices", "home", "logs", "scope", "xcode", "flutter", "capacity")
FRESH_SEC = 300          # これより長く窓口に来なければ「連絡なし」
HEARTBEAT_SEC = 240      # 窓口が runner.heartbeat を書く間隔の下限
PAIR_TTL = timedelta(minutes=10)


def clean(q: dict) -> dict:
    """ランナーの自己紹介を形にそろえる（外の機械から来る文字なので、長さと文字を絞る）。"""
    out = {}
    for k in FIELDS:
        v = q.get(k)
        if v is None:
            continue
        out[k] = re.sub(r"[\x00-\x1f]", "", str(v))[:1000 if k == "scope" else 200]
    return out


def _now(now):
    return now or datetime.now(timezone.utc)


# ---- ペアリング ----

def pair_requested(cur, pair_id: str, code: str, info: dict):
    ops.record(cur, "runner.pair_requested", None, {"pair_id": pair_id, "code": code, **clean(info)})


def pair_state(cur, pair_id: str, now=None) -> str:
    """pending / approved / rejected / expired / unknown"""
    cur.execute("select name, occurred_at from events where name in ('runner.pair_requested','runner.pair_approved','runner.pair_rejected') "
                "and payload->>'pair_id' = %s order by id", (pair_id,))
    rows = cur.fetchall()
    if not rows or rows[0]["name"] != "runner.pair_requested":
        return "unknown"
    for r in rows[1:]:
        return "approved" if r["name"] == "runner.pair_approved" else "rejected"
    return "expired" if _now(now) - rows[0]["occurred_at"] > PAIR_TTL else "pending"


def pending_pairs(cur, now=None) -> list[dict]:
    """操業コンソールに出す、許可待ちのペアリング（番号つき）。"""
    cur.execute("select payload, occurred_at from events e where name = 'runner.pair_requested' and occurred_at > %s "
                "and not exists (select 1 from events d where d.name in ('runner.pair_approved','runner.pair_rejected') "
                "and d.payload->>'pair_id' = e.payload->>'pair_id') order by id",
                (_now(now) - PAIR_TTL,))
    return [{**r["payload"], "requested_at": r["occurred_at"].isoformat()} for r in cur.fetchall()]


def decide_pair(cur, pair_id: str, approve: bool, by: str, reason: str = "", now=None) -> tuple[bool, str]:
    st = pair_state(cur, pair_id, now)
    if st != "pending":
        return False, {"unknown": "no such pairing", "expired": "依頼が切れた（Mac でやり直す）",
                       "approved": "もう許可した", "rejected": "もう拒否した"}[st]
    ops.record(cur, "runner.pair_approved" if approve else "runner.pair_rejected", None, {"pair_id": pair_id, "by": by, "reason": reason})
    return True, "許可した（Mac 側で y を押すと繋がる）" if approve else "拒否した"


# ---- 登録済みのランナー ----

def _state(cur, rid: str) -> dict:
    cur.execute("select name, payload, occurred_at from events where name in ('runner.registered','runner.uninstall_requested','runner.uninstalled') "
                "and payload->>'id' = %s order by id", (rid,))
    st = {"info": None, "pending": False, "uninstalled_at": None, "registered_at": None}
    for r in cur.fetchall():
        if r["name"] == "runner.registered":
            st.update(info=r["payload"], registered_at=r["occurred_at"], uninstalled_at=None)   # 外す依頼は外れるまで残す
        elif r["name"] == "runner.uninstall_requested":
            st["pending"] = True
        else:
            st.update(pending=False, uninstalled_at=r["occurred_at"])
    return st


def register(cur, rid: str, info: dict):
    """ペアリングが済んだとき（窓口が呼ぶ）。"""
    ops.record(cur, "runner.registered", None, {"id": rid, **clean(info)})


def hello(cur, rid: str, info: dict, last_beat: datetime | None, now=None) -> tuple[bool, datetime | None]:
    """ランナーが窓口に来たとき（窓口が鍵を確かめてから呼ぶ）。返り値は (外すように頼まれているか, 書いた heartbeat の時刻)。"""
    now = _now(now)
    st = _state(cur, rid)
    if st["info"] is None or st["uninstalled_at"] is not None:
        return False, last_beat
    new = {"id": rid, **clean(info)}
    if info and any(st["info"].get(k) != new.get(k) for k in FIELDS if k in new):
        ops.record(cur, "runner.registered", None, {**st["info"], **new})
    if last_beat is None or (now - last_beat).total_seconds() >= HEARTBEAT_SEC:
        ops.record(cur, "runner.heartbeat", None, {"id": rid})
        last_beat = now
    return st["pending"], last_beat


def uninstalled(cur, rid: str, how: str):
    st = _state(cur, rid)
    if st["info"] is not None and st["uninstalled_at"] is None:
        ops.record(cur, "runner.uninstalled", None, {"id": rid, "name": st["info"].get("name"), "how": how})


def request_uninstall(cur, rid: str, by: str, reason: str = "") -> tuple[bool, str]:
    if not ID.match(rid):
        return False, "no such runner"
    st = _state(cur, rid)
    if st["info"] is None or st["uninstalled_at"] is not None:
        return False, "no such runner"
    if st["pending"]:
        return True, "外す依頼は出ている（次の連絡で外れる）"
    ops.record(cur, "runner.uninstall_requested", None, {"id": rid, "by": by, "reason": reason})
    return True, "外す依頼を出した（ランナーの次の連絡で外れ、鍵を捨てる）"


def uninstall_pending(cur) -> list[str]:
    cur.execute("select distinct payload->>'id' as id from events where name = 'runner.uninstall_requested'")
    return [r["id"] for r in cur.fetchall() if _state(cur, r["id"])["pending"] and _state(cur, r["id"])["uninstalled_at"] is None]


def view(cur, now=None) -> list[dict]:
    """登録済みのランナーの一覧（外したものは除く）。"""
    now = _now(now)
    cur.execute("select distinct payload->>'id' as id from events where name = 'runner.registered'")
    out = []
    for rid in sorted(r["id"] for r in cur.fetchall() if r["id"]):
        st = _state(cur, rid)
        if st["info"] is None or st["uninstalled_at"] is not None:
            continue
        cur.execute("select max(occurred_at) as t from events where name in ('runner.heartbeat','runner.registered') and payload->>'id' = %s", (rid,))
        last = cur.fetchone()["t"]
        age = int((now - last).total_seconds()) if last else None
        out.append({**st["info"], "registered_at": st["registered_at"].isoformat() if st["registered_at"] else None,
                    "last_seen_age_sec": age, "online": age is not None and age <= FRESH_SEC,
                    "uninstall_pending": st["pending"]})
    return out
