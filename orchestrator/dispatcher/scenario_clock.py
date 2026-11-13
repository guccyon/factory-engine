"""QA が触れる環境の時刻を合わせる（ADR 0055。ADR 0028 の scenario_time の続き）。

中継の道具 env_restart・env_clock を dispatcher が受けたときの決まりを持つ。手を動かすのは shipper（ship.py）で、ここは
引数を読み、断るかどうかを決め、依頼の形にするだけ。

- env_restart の set_to（時刻を指定して合わせる）・advance_minutes・advance_hours・reset_clock を、touch.restart の引数にする
- 時計を動かす・再起動するのは、その製品で動いている QA のジョブが自分だけのとき（決定 3）。0 に戻すのはいつでもよい
- 1 ジョブで再起動してよい回数に上限を置く（definitions/qa.yaml の clock.max_restarts_per_job）
- 時計を進めたジョブが終わったら、戻し忘れを工場が戻す（決定 4）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import ship

JST = timezone(timedelta(hours=9))
CLEANUP_VIA = "dispatcher"


def by_of(job_id: int) -> str:
    return f"qa:{job_id}"


def parse(arguments: dict, now: datetime) -> tuple[dict | None, str | None]:
    """env_restart の引数を touch.restart の引数（advance_seconds / offset_seconds / reset_clock）にする。返り値 (引数, 断る理由)"""
    given = [k for k in ("set_to", "advance_minutes", "advance_hours") if arguments.get(k) not in (None, 0, "")]
    reset = bool(arguments.get("reset_clock"))
    if len(given) > 1 or (given and reset):
        return None, "set_to・advance_minutes・advance_hours・reset_clock は 1 つだけ渡す"
    if not given:
        return {"reset_clock": reset, "advance_seconds": 0}, None
    key, value = given[0], arguments[given[0]]
    if key == "set_to":
        try:
            target = datetime.fromisoformat(str(value))
        except ValueError:
            return None, f"set_to は ISO 8601 の時刻（例 2026-11-13T20:05:00+09:00）: {value!r}"
        if target.tzinfo is None:
            return None, "set_to にはタイムゾーンを付ける（例 +09:00）"
        offset = int((target - now).total_seconds())
        if offset < 0:
            return None, f"実際の時刻（{now.astimezone(JST):%Y-%m-%d %H:%M:%S}）より前には合わせられない"
        return {"offset_seconds": offset}, None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None, f"{key} は 0 以上の数"
    return {"advance_seconds": int(round(float(value) * (60 if key == "advance_minutes" else 3600)))}, None


def others_running(cur, repo: str, job_id: int) -> list[int]:
    """同じ製品で、自分のほかに動いている QA のジョブ（判定・探索・合成の利用者）。Gen1 の QA は staging だけを使う"""
    cur.execute("select id from jobs where repo=%s and kind='qa' and state='running' and id<>%s order by id", (repo, job_id))
    return [r["id"] for r in cur.fetchall()]


def restarts_used(cur, job_id: int) -> int:
    cur.execute("select count(*) as n from events where name='release.restart_requested' and payload->>'by'=%s", (by_of(job_id),))
    return int(cur.fetchone()["n"])


def refuse(cur, repo: str, job_id: int, args: dict, max_restarts: int) -> str | None:
    """この依頼を断る理由（なければ None）。0 に戻すのはいつでも通す（ほかのジョブのためにもなる）"""
    if args.get("reset_clock"):
        return None
    others = others_running(cur, repo, job_id)
    if others:
        return (f"ほかの QA のジョブ（{', '.join(map(str, others))}）が同じ環境を使っているので、時計を動かさない（ADR 0055 の決定 3）。"
                "この歩は skipped にし、exit がまたぐなら blocked にする")
    if restarts_used(cur, job_id) >= max_restarts:
        return f"このジョブで再起動できる回数（{max_restarts} 回）を使い切った。reset_clock で 0 に戻すことだけできる"
    return None


def describe(cur, repo: str, env: str, now: datetime) -> str:
    """env_clock の答え: サーバーの今の時刻と進めている量。製品の時計＝実際の時刻＋進めている量で出す"""
    offset = ship.clock_offset(cur, repo, env)
    server = (now + timedelta(seconds=offset)).astimezone(JST)
    return (f"{env} のサーバーの時刻は {server.isoformat(timespec='seconds')}（実際の時刻に {offset} 秒を足したもの）。"
            f"進めている量は {offset} 秒（{offset / 3600:g} 時間）")


def after_restart(cur, repo: str, env: str, now: datetime, msg: str) -> str:
    """env_restart が終わったあとの答えに、サーバーの今の時刻を足す（QA が次の歩を決めるため）"""
    return f"{msg}。{describe(cur, repo, env, now)}"


def cleanup(cur, repo: str, env: str, job_id: int) -> str | None:
    """ジョブの終わり: そのジョブが最後に時計を動かしたまま 0 に戻していなければ、0 に戻す再起動を依頼する。依頼したら説明を返す"""
    cur.execute("select payload from events where name='release.restart_requested' and payload->>'repo'=%s and payload->>'env'=%s "
                "order by id desc limit 1", (repo, env))
    r = cur.fetchone()
    if not r or r["payload"].get("by") != by_of(job_id) or r["payload"].get("reset_clock"):
        return None   # 最後に触ったのがほかの人か、もう戻した
    p = r["payload"]
    moved = int(p.get("advance_seconds") or 0) > 0 or int(p.get("offset_seconds") or 0) > 0 or ship.clock_offset(cur, repo, env) > 0
    if not moved:
        return None
    ok, m = ship.request_restart(cur, repo, env, f"{by_of(job_id)}:cleanup", CLEANUP_VIA, reset_clock=True)
    return m if ok else None
