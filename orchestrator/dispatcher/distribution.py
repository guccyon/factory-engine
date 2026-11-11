"""モバイルアプリの審査前の配布（ADR 0043 の O8 preview_distribution、ADR 0047）。工場の側の入口と記録。

工場は「何を・どこへ配るか」を決めて Mac ランナーに頼むだけ。署名の鍵と配布先へのログイン・宛先（Firebase のアプリの ID）は
Mac にだけあり、人が Mac の画面で「配布する」を押したときだけ進む（attended）。

入口は 2 つで、どちらも依頼を events に書く:
- 操業コンソールの出荷のページの「配布する」ボタン（POST /api/distribute）。依頼を書くだけで、dispatcher の distribute_loop が拾って動かす
- factoryctl distribute。依頼を書いて、その場で動かして待つ
拾ったものは ship.preview_started を dedupe_key つきで書いて自分のものにする（同じ依頼を 2 回動かさない）。

依頼と結果（events の name。correlation_id は dist:<依頼の events の id>）:
  ship.preview_target_set   {app, firebase_app_id, groups, by}  配布先（宛先）。操業コンソールで入れる。新しいものが効く
  ship.preview_requested    {app, channel, ref, by, via, notes, defines, destination}   destination は依頼のときの配布先の写し（なければ Mac の設定）
  ship.preview_started      {request_id, by}                     dedupe_key dist-start:<依頼の id>
  ship.preview_distributed  {request_id, app, channel, sha, build_number, sha256, testers, runner, build_id, seconds}
  ship.preview_failed       {request_id, app, channel, stage, error, runner, build_id, seconds}
    stage: request（ランナーに届かない・断られた）| attend（人が押さなかった・やめた）| build | sign | upload | timeout | lost（拾った dispatcher が途中で止まった）

配布先（channel）は firebase_android だけ。testflight_ios は Gen2（パイロットの既存 iPhone アプリの出荷レーン）で足す。
配ってよいアプリは DISTRIBUTE_APPS（<owner/repo>:<パス> のカンマ区切り）。Mac ランナーの MAC_RUNNER_APPS と同じ書き方。
"""
from __future__ import annotations

import json
import os
import re
import time

from psycopg.types.json import Jsonb

import macrunner_client

CHANNELS = ("firebase_android",)
DEFAULT_APP = "guccyon/rally:apps/student-app"
APPS = [a.strip() for a in os.environ.get("DISTRIBUTE_APPS", DEFAULT_APP).split(",") if a.strip()]
FIREBASE_APP_ID = re.compile(r"^1:[0-9]{1,20}:android:[0-9a-f]{1,40}$")   # Mac ランナーと同じ形
GROUPS = re.compile(r"^[a-z0-9-]{1,64}(,[a-z0-9-]{1,64}){0,9}$")
TIMEOUT = 3600          # 人が押すのを待つ時間を含む
POLL = 15


def corr(request_id: int) -> str:
    return f"dist:{request_id}"


def apps_for(repo: str) -> list[str]:
    return [a for a in APPS if a.split(":", 1)[0] == repo]


def _record(cur, name: str, correlation: str | None, payload: dict, dedupe: str | None = None):
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values (%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (name, correlation, Jsonb(payload), dedupe))
    row = cur.fetchone()
    return row["id"] if row else None


def _body(res: dict) -> dict:
    text = (res.get("content") or [{}])[0].get("text", "")
    if res.get("isError"):
        raise RuntimeError(text)
    return json.loads(text)


def target(cur, app: str) -> dict | None:
    """工場が覚えている配布先（宛先）。なければ None（Mac ランナーの設定を使う）。宛先は秘密ではない（ADR 0047）"""
    cur.execute("select payload, occurred_at from events where name='ship.preview_target_set' and payload->>'app'=%s order by id desc limit 1", (app,))
    r = cur.fetchone()
    if not r or not r["payload"].get("firebase_app_id"):
        return None
    return {"firebase_app_id": r["payload"]["firebase_app_id"], "groups": r["payload"].get("groups") or "testers",
            "by": r["payload"].get("by"), "at": r["occurred_at"]}


def set_target(cur, app: str, firebase_app_id: str, groups: str, by: str) -> tuple[bool, str]:
    """配布先を覚える。アプリの ID を空にすると工場の宛先を外し、Mac の設定に戻す"""
    if app not in APPS:
        return False, f"{app} は配布を許していない（DISTRIBUTE_APPS: {', '.join(APPS) or 'なし'}）"
    firebase_app_id, groups = firebase_app_id.strip(), (groups or "testers").strip().replace(" ", "")
    if firebase_app_id and not FIREBASE_APP_ID.match(firebase_app_id):
        return False, "アプリの ID の形が違う（Firebase の Android アプリの ID: 1:<番号>:android:<16 進>）"
    if not GROUPS.match(groups):
        return False, "グループの形が違う（英小文字・数字・-、カンマ区切りで 10 個まで）"
    _record(cur, "ship.preview_target_set", None, {"app": app, "firebase_app_id": firebase_app_id, "groups": groups, "by": by})
    return True, ("配布先を覚えた" if firebase_app_id else "工場の配布先を外した（Mac の設定を使う）")


def active(cur, app: str | None = None) -> list[dict]:
    """結果がまだない依頼（拾われる前と、Mac で動いている間）。古い順"""
    cur.execute("""select r.id, r.occurred_at, r.payload,
                     exists (select 1 from events s where s.dedupe_key = 'dist-start:' || r.id) as started
                   from events r where r.name = 'ship.preview_requested'
                     and not exists (select 1 from events x where x.correlation_id = r.correlation_id
                                     and x.name in ('ship.preview_distributed','ship.preview_failed'))
                     and (%s::text is null or r.payload->>'app' = %s) order by r.id""", (app, app))
    return cur.fetchall()


def request(cur, app: str = DEFAULT_APP, channel: str = "firebase_android", ref: str = "main", notes: str = "",
            defines: dict | None = None, by: str = "builder", via: str = "factoryctl") -> tuple[bool, int | str]:
    """配布の依頼を書く。(書けたか, 依頼の id か断った理由)。同じアプリの配布が終わっていなければ断る（ボタンの押し直しで 2 本走らせない）"""
    if channel not in CHANNELS:
        return False, f"{channel} という配布先はない（あるもの: {', '.join(CHANNELS)}）"
    if app not in APPS:
        return False, f"{app} は配布を許していない（DISTRIBUTE_APPS: {', '.join(APPS) or 'なし'}）"
    if len(notes) > 1000:
        return False, "変更点は 1000 文字まで"
    if active(cur, app):
        return False, "同じアプリの配布がまだ終わっていない（Mac の画面の窓を確かめる）"
    t = target(cur, app)
    dest = {"firebase_app_id": t["firebase_app_id"], "groups": t["groups"]} if t else None
    req = _record(cur, "ship.preview_requested", None,
                  {"app": app, "channel": channel, "ref": ref, "by": by, "via": via, "notes": notes, "defines": defines or {}, "destination": dest})
    cur.execute("update events set correlation_id=%s where id=%s", (corr(req), req))
    return True, req


def claim(cur, req: int, by: str) -> bool:
    """依頼を自分のものにする。ほかが先に拾っていれば False"""
    return _record(cur, "ship.preview_started", corr(req), {"request_id": req, "by": by}, dedupe=f"dist-start:{req}") is not None


def run(cur, req: int, payload: dict, timeout: float = TIMEOUT, poll: float = POLL,
        call=macrunner_client.call, sleep=time.sleep, log=print) -> tuple[bool, dict]:
    """拾った依頼を Mac ランナーで動かし、結果を events に書く。(配れたか, 結果の payload)。人が Mac で押すまで待つ"""
    started = time.time()
    app, channel = payload["app"], payload["channel"]
    base = {"request_id": req, "app": app, "channel": channel}

    def fail(stage, error, runner=None, build_id=None):
        p = {**base, "stage": stage, "error": str(error)[-2000:], "runner": runner, "build_id": build_id,
             "seconds": int(time.time() - started)}
        _record(cur, "ship.preview_failed", corr(req), p)
        return False, p

    args = {"app": app, "channel": channel, "ref": payload.get("ref") or "main"}
    if payload.get("notes"):
        args["release_notes"] = payload["notes"]
    if payload.get("defines"):
        args["dart_defines"] = payload["defines"]
    if payload.get("destination"):   # 工場が覚えた配布先。なければ Mac ランナーの設定
        args.update({k: v for k, v in payload["destination"].items() if v})
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
            p = {**base, **{k: b.get(k) for k in ("sha", "build_number", "sha256", "testers", "firebase_app_id")},
                 "runner": runner, "build_id": bid, "seconds": int(time.time() - started)}
            _record(cur, "ship.preview_distributed", corr(req), p)
            return True, p
        if b.get("state") == "failed":
            return fail(b.get("stage") or "build", b.get("error", ""), runner, bid)
    return fail("timeout", f"{int(timeout)} 秒で終わらなかった（Mac の側では続いているかもしれない。build_status {bid}）", runner, bid)


def distribute(cur, app: str = DEFAULT_APP, channel: str = "firebase_android", ref: str = "main", notes: str = "",
               defines: dict | None = None, by: str = "builder", via: str = "factoryctl",
               timeout: float = TIMEOUT, poll: float = POLL, call=macrunner_client.call, sleep=time.sleep, log=print) -> tuple[bool, dict]:
    """依頼を書いて、その場で動かして待つ（factoryctl distribute）。"""
    ok, req = request(cur, app, channel, ref, notes, defines, by, via)
    if not ok:
        return False, {"error": req}
    claim(cur, req, via)
    cur.execute("select payload from events where id=%s", (req,))
    payload = cur.fetchone()["payload"]
    return run(cur, req, payload, timeout=timeout, poll=poll, call=call, sleep=sleep, log=log)


def tick(cur, by: str = "dispatcher", log=print, **kw) -> int:
    """dispatcher の distribute_loop が呼ぶ。止まった dispatcher が残した依頼を失敗にし、拾われていない依頼を 1 つずつ動かす。動かした数を返す"""
    n = 0
    for r in active(cur):
        if r["started"]:
            cur.execute("select occurred_at from events where dedupe_key = %s", (f"dist-start:{r['id']}",))
            s = cur.fetchone()
            if s and time.time() - s["occurred_at"].timestamp() > TIMEOUT + 900:
                _record(cur, "ship.preview_failed", corr(r["id"]),
                        {"request_id": r["id"], "app": r["payload"].get("app"), "channel": r["payload"].get("channel"),
                         "stage": "lost", "error": "拾った dispatcher が結果を書く前に止まった", "runner": None, "build_id": None, "seconds": None})
            continue
        if claim(cur, r["id"], by):
            log(f"distribute: 依頼 {r['id']} を動かす（{r['payload'].get('app')}・{r['payload'].get('via')}）")
            run(cur, r["id"], r["payload"], log=log, **kw)
            n += 1
    return n


def recent(cur, app: str | None = None, limit: int = 10) -> list[dict]:
    """直近の配布の依頼と結果（新しい順）。"""
    cur.execute("""select r.id, r.occurred_at, r.payload as req, x.name as result, x.payload as res, x.occurred_at as finished_at,
                     exists (select 1 from events s where s.dedupe_key = 'dist-start:' || r.id) as started
                   from events r left join lateral (select name, payload, occurred_at from events x
                     where x.correlation_id = r.correlation_id and x.name in ('ship.preview_distributed','ship.preview_failed')
                     order by x.id desc limit 1) x on true
                   where r.name = 'ship.preview_requested' and (%s::text is null or r.payload->>'app' = %s)
                   order by r.id desc limit %s""", (app, app, limit))
    return cur.fetchall()
