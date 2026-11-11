"""着手中（active）なのに実態のないチケットを台帳から外す（2026-11-10 オーナー「着手中なのにその実態がないことが問題」）。

台帳（tickets、ADR 0009）の active はジョブに渡した時点で付き、外れるのは工場の PR がマージされたときだけ。
窓口の確認だけで終わった相談や、人が Issue を閉じたチケットは、active のまま残り続け、開発状況レポートの
「進めている」「動いていない」を実態のない記録で埋めていた（rally の 10/31 の合成の相談 20 件）。

GitHub の Issue が閉じていて、積まれたジョブも動いているジョブもない active のチケットを closed にする。
済んだ時刻は分からないので updated_at は動かさない（その日の「済んだ」の数を膨らませない）。
GitHub を見るのは STEP_SEC ごとに 1 回、1 回に BATCH 件まで。読めなかったチケットは次の回に回す。

ADR 0050 で、見る（observe: PR の見張りのスレッドが Issue を見て github.issue_closed を記録する）と
片付ける（apply: supervisor が記録を読んでチケットを閉じる）に分けた。Webhook が使えるようになったら observe の入口を足すだけでよい。
"""
from __future__ import annotations

import os
import re
import time

from psycopg.types.json import Jsonb

STEP_SEC = int(os.environ.get("TICKET_SYNC_SEC", "1800"))   # GitHub を見に行く間隔（秒）
BATCH = int(os.environ.get("TICKET_SYNC_BATCH", "100"))      # 1 回に見るチケットの数
REF = re.compile(r"([^/#\s]+/[^/#\s]+)#(\d+)")

_last = 0.0


def candidates(cur, limit: int = BATCH) -> list[dict]:
    """積まれたジョブも動いているジョブもない、GitHub の active のチケット（回ごとに違う順で）"""
    cur.execute("select id, source_ref from tickets t where t.source = 'github' and t.state = 'active' "
                "and not exists (select 1 from jobs j where j.correlation_id = 'github:' || t.source_ref and j.state in ('queued', 'running')) "
                "order by random() limit %s", (limit,))   # 開いたままのものが多くても、回ごとに違うものを見る
    return cur.fetchall()


def observe(cur, gh, log=print, now: float | None = None, force: bool = False) -> list[str]:
    """小さなポーリング（ADR 0050 の決定 2）。片付けの候補のチケットの Issue を見て、閉じていたら github.issue_closed を記録する。
    PR の見張り（pr_events のポーリングのスレッド）から呼ぶ。STEP_SEC ごとに 1 回。記録した source_ref の一覧を返す"""
    global _last
    now = time.time() if now is None else now
    if not force and now - _last < STEP_SEC:
        return []
    _last = now
    seen = []
    for t in candidates(cur):
        m = REF.fullmatch(t["source_ref"])
        if not m:
            continue
        try:
            it = gh("GET", f"repos/{m.group(1)}/issues/{m.group(2)}") or {}
        except Exception as e:  # noqa: BLE001  読めなければ次の回に
            log("ticket sync read failed", t["source_ref"], repr(e)[:200])
            continue
        if it.get("state") != "closed":
            continue
        cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('github.issue_closed', %s, %s, %s) "
                    "on conflict (dedupe_key) do nothing returning id",
                    (f"github:{t['source_ref']}", Jsonb({"source_ref": t["source_ref"], "repo": m.group(1), "issue": int(m.group(2)),
                                                          "closed_at": it.get("closed_at"), "state_reason": it.get("state_reason")}),
                     f"github.issue_closed:{t['source_ref']}@{it.get('closed_at')}"))
        if cur.fetchone():
            seen.append(t["source_ref"])
    if seen:
        log("ticket sync saw closed issues", len(seen), seen[:10])
    return seen


def apply(cur, log=print) -> list[str]:
    """Issue が閉じた記録（github.issue_closed）があるチケットを閉じる。GitHub は見ない。supervisor の周回から呼ぶ。
    閉じた記録より後にジョブが積まれたチケット（Issue を開き直して作業した）は閉じない。閉じた source_ref の一覧を返す"""
    cur.execute("""select t.id, t.source_ref, e.payload from tickets t
                   join lateral (select payload, occurred_at from events where name = 'github.issue_closed' and correlation_id = 'github:' || t.source_ref
                                 order by id desc limit 1) e on true
                   where t.source = 'github' and t.state = 'active'
                     and not exists (select 1 from jobs j where j.correlation_id = 'github:' || t.source_ref
                                     and (j.state in ('queued', 'running') or j.enqueued_at > e.occurred_at))
                   order by t.id""")
    closed = []
    for t in cur.fetchall():
        cur.execute("update tickets set state = 'closed' where id = %s and state = 'active' returning id", (t["id"],))
        if cur.fetchone():
            cur.execute("insert into events(name, correlation_id, payload) values ('ticket.closed_with_source', %s, %s)",
                        (f"github:{t['source_ref']}", Jsonb({"source_ref": t["source_ref"], "closed_at": t["payload"].get("closed_at"),
                                                             "state_reason": t["payload"].get("state_reason")})))
            closed.append(t["source_ref"])
    if closed:
        log("ticket sync closed", len(closed), closed[:10])
    return closed
