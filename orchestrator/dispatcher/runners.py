"""登録済みのランナー（ADR 0028 の決定 3）。工場の外の機械に入れた常駐プロセスを、工場が把握して外せるようにする。

- ランナー（orchestrator/macrunner/runner.py）は 1 分ごとに操業コンソールへ連絡する（GET /api/runners/checkin。keep-awake.sh と同じ向き）。
  工場からランナーへは繋ぎに行かないので、コンソールはランナーのトークンを持たない
- 正本は events: 初めて見たときと中身（版・置き場・道具）が変わったときだけ runner.registered を書く。最後に連絡した時刻はコンソールの記憶だけ
- 外す: 画面のボタンで runner.uninstall_requested を書く → 次の連絡の返事で外せと伝える → ランナーが自分の LaunchAgent を外し、
  runner.uninstalled を知らせてから止まる。トークン・ビルドの作業場・ログは残す（消すのは人。残るものは画面に出す）
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import ops

ID = re.compile(r"^[0-9a-f]{32}$")
FIELDS = ("name", "kind", "version", "label", "path", "port", "platform", "tools", "devices", "home", "logs", "scope")
FRESH_SEC = 300   # これより長く連絡がなければ「連絡なし」と出す（ランナーは 60 秒ごと）
seen: dict[str, datetime] = {}   # id → 最後に連絡した時刻（コンソールの記憶だけ。上げ直すと次の連絡まで分からない）


def clean(q: dict) -> dict:
    """連絡の中身を形にそろえる（外の機械から来る文字なので、長さと文字を絞る）。"""
    out = {}
    for k in FIELDS:
        v = q.get(k)
        if v is None:
            continue
        out[k] = re.sub(r"[\x00-\x1f]", "", str(v))[:1000 if k == "scope" else 200]
    return out


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


def checkin(cur, q: dict, now: datetime | None = None) -> tuple[int, dict]:
    """ランナーからの連絡。返事に、外すように頼まれているかを入れる。"""
    rid = str(q.get("id") or "")
    if not ID.match(rid):
        return 400, {"error": "id は 32 桁の 16 進"}
    now = now or datetime.now(timezone.utc)
    info = {"id": rid, **clean(q)}
    st = _state(cur, rid)
    if q.get("state") == "uninstalled":
        seen.pop(rid, None)
        if st["info"] is not None and st["uninstalled_at"] is None:
            ops.record(cur, "runner.uninstalled", None, {"id": rid, "name": info.get("name") or (st["info"] or {}).get("name")})
        return 200, {"uninstall": False}
    seen[rid] = now
    prev = st["info"] or {}
    if st["uninstalled_at"] is not None or any(prev.get(k) != info.get(k) for k in ("id", *FIELDS)):
        ops.record(cur, "runner.registered", None, info)   # 外したあとに入れ直されたときも、ここで登録し直す（外す依頼は残らない）
    return 200, {"uninstall": st["pending"] and st["uninstalled_at"] is None}


def request_uninstall(cur, rid: str, by: str, reason: str = "") -> tuple[bool, str]:
    if not ID.match(rid):
        return False, "no such runner"
    st = _state(cur, rid)
    if st["info"] is None or st["uninstalled_at"] is not None:
        return False, "no such runner"
    if st["pending"]:
        return True, "外す依頼は出ている（次の連絡で外れる）"
    ops.record(cur, "runner.uninstall_requested", None, {"id": rid, "by": by, "reason": reason})
    return True, "外す依頼を出した（ランナーの次の連絡で外れる。1 分ほど）"


def view(cur, now: datetime | None = None) -> list[dict]:
    """登録済みのランナーの一覧（外したものは除く）。"""
    now = now or datetime.now(timezone.utc)
    cur.execute("select distinct payload->>'id' as id from events where name = 'runner.registered'")
    out = []
    for rid in sorted(r["id"] for r in cur.fetchall() if r["id"]):
        st = _state(cur, rid)
        if st["info"] is None or st["uninstalled_at"] is not None:
            continue
        last = seen.get(rid)
        age = int((now - last).total_seconds()) if last else None
        out.append({**st["info"], "registered_at": st["registered_at"].isoformat() if st["registered_at"] else None,
                    "last_seen_age_sec": age, "online": age is not None and age <= FRESH_SEC,
                    "uninstall_pending": st["pending"]})
    return out
