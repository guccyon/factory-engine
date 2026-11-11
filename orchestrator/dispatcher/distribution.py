"""モバイルアプリの審査前の配布（ADR 0043 の O8 preview_distribution、ADR 0047）。工場の側の入口と記録。

工場は「何を・どこへ配るか」を決めて Mac ランナーに頼むだけ。署名の鍵と配布先へのログインは Mac にだけあり、
人が Mac の画面で「配布する」を押したときだけ進む（attended）。ここは依頼を events に書き、ランナーの道具
preview_distribute を呼び、build_status を待って結果を events に書く。

依頼と結果（events の name。correlation_id は dist:<依頼の events の id>）:
  ship.preview_requested    {app, channel, ref, by, via, notes_chars}
  ship.preview_distributed  {request_id, app, channel, sha, build_number, sha256, testers, runner, build_id, seconds}
  ship.preview_failed       {request_id, app, channel, stage, error, runner, build_id, seconds}
    stage: request（ランナーに届かない・断られた）| attend（人が押さなかった・やめた）| build | sign | upload | timeout

配布先（channel）は firebase_android だけ。testflight_ios は Gen2（パイロットの既存 iPhone アプリの出荷レーン）で足す。
"""
from __future__ import annotations

import json
import time

from psycopg.types.json import Jsonb

import macrunner_client

CHANNELS = ("firebase_android",)
DEFAULT_APP = "guccyon/rally:apps/student-app"


def corr(request_id: int) -> str:
    return f"dist:{request_id}"


def _record(cur, name: str, correlation: str | None, payload: dict) -> int:
    cur.execute("insert into events(name, correlation_id, payload) values (%s,%s,%s) returning id",
                (name, correlation, Jsonb(payload)))
    return cur.fetchone()["id"]


def _body(res: dict) -> dict:
    text = (res.get("content") or [{}])[0].get("text", "")
    if res.get("isError"):
        raise RuntimeError(text)
    return json.loads(text)


def distribute(cur, app: str = DEFAULT_APP, channel: str = "firebase_android", ref: str = "main", notes: str = "",
               defines: dict | None = None, by: str = "builder", via: str = "factoryctl",
               timeout: float = 3600, poll: float = 15, call=macrunner_client.call, sleep=time.sleep, log=print) -> tuple[bool, dict]:
    """1 本配る。(配れたか, 結果の payload)。人が Mac で押すまで待つので、長いときは 1 時間かかる。"""
    if channel not in CHANNELS:
        return False, {"error": f"{channel} という配布先はない（あるもの: {', '.join(CHANNELS)}）"}
    started = time.time()
    req = _record(cur, "ship.preview_requested", None,
                  {"app": app, "channel": channel, "ref": ref, "by": by, "via": via, "notes_chars": len(notes)})
    cur.execute("update events set correlation_id=%s where id=%s", (corr(req), req))
    base = {"request_id": req, "app": app, "channel": channel}

    def fail(stage, error, runner=None, build_id=None):
        p = {**base, "stage": stage, "error": str(error)[-2000:], "runner": runner, "build_id": build_id,
             "seconds": int(time.time() - started)}
        _record(cur, "ship.preview_failed", corr(req), p)
        return False, p

    args = {"app": app, "channel": channel, "ref": ref}
    if notes:
        args["release_notes"] = notes
    if defines:
        args["dart_defines"] = defines
    try:
        res = call("preview_distribute", args, timeout=60)
        b = _body(res)
    except (RuntimeError, ValueError, OSError) as e:
        return fail("request", e)
    runner, bid = res.get("runner_id"), b["build_id"]
    log(f"依頼した（{bid}）。Mac の画面に確かめの窓が出るので「配布する」を押す")
    last = None
    while time.time() - started < timeout:
        sleep(poll)
        try:
            b = _body(call("build_status", {"build_id": bid}, runner=runner, timeout=60))
        except (RuntimeError, ValueError, OSError) as e:
            log(f"状態を読めなかった（{e}）。読み直す")
            continue
        state = (b.get("state"), b.get("stage"))
        if state != last:
            log(f"  {b.get('state')}（{b.get('stage') or '-'}）")
            last = state
        if b.get("state") == "distributed":
            p = {**base, **{k: b.get(k) for k in ("sha", "build_number", "sha256", "testers")},
                 "runner": runner, "build_id": bid, "seconds": int(time.time() - started)}
            _record(cur, "ship.preview_distributed", corr(req), p)
            return True, p
        if b.get("state") == "failed":
            return fail(b.get("stage") or "build", b.get("error", ""), runner, bid)
    return fail("timeout", f"{int(timeout)} 秒で終わらなかった（Mac の側では続いているかもしれない。build_status {bid}）", runner, bid)


def recent(cur, limit: int = 10) -> list[dict]:
    """直近の配布の依頼と結果（新しい順）。"""
    cur.execute("""select r.id, r.occurred_at, r.payload as req, x.name as result, x.payload as res
                   from events r left join lateral (select name, payload from events x
                     where x.correlation_id = r.correlation_id and x.name in ('ship.preview_distributed','ship.preview_failed')
                     order by x.id desc limit 1) x on true
                   where r.name = 'ship.preview_requested' order by r.id desc limit %s""", (limit,))
    return cur.fetchall()
