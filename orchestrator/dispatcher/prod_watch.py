"""本番の見張り（ADR 0057 の決定 1）。本番を立てる段階（launch・operating）の製品の本番に、出したときと同じ確かめ方（ship.yaml の verify）で
版を聞き続ける。shipper の常駐が周回ごとに tick() を呼び、PROD_WATCH_SEC ごとに 1 回だけ実際に見る。

- 答えない・違う版を答えるのが PROD_WATCH_FAILS 回続いたら「止まっている」。まず 1 回だけ、いまの版で起動し直して立て直す（prod.self_healed）。
  立て直したあと HEAL_COOLDOWN_SEC の間にまた止まったら、立て直さずに落ちたとする
- 立て直せなければ prod.down を残し、Incident（S2、detected_by: prod_watch）と停止（factory-bug）を開く。S1 にしない（原因が製品と決まっていない）
- 答えるようになったら prod.up（止まっていた秒数）を残し、Incident と停止を閉じる
- 本番への出荷・戻しの依頼を処理している間は見ない（入れ替えの途中を「止まった」と数えない）

記録は境目（prod.down / prod.up / prod.self_healed）だけ。周回ごとには書かない。LLM は使わない。
"""
from __future__ import annotations

import os
import time

from psycopg.types.json import Jsonb

INTERVAL = float(os.environ.get("PROD_WATCH_SEC", "60"))
FAILS = int(os.environ.get("PROD_WATCH_FAILS", "3"))
HEAL_COOLDOWN_SEC = float(os.environ.get("PROD_WATCH_HEAL_COOLDOWN_SEC", "1800"))
ENV = "prod"

_state: dict[str, dict] = {}     # 製品ごとの続けての失敗の数と、最後に立て直した時刻（常駐の中だけ。再起動したら数え直す）
_last_tick: list[float | None] = [None]   # 最後に見た時刻（常駐が起きた直後は 1 回目をすぐ見る）


def _ship():
    import ship   # ship が常駐の本体で、ここを読み込む。読み込みの輪を避けるため使うときに読む
    return ship


def watched(cur) -> list[str]:
    """本番に版が出ていて、本番を立てる段階の製品"""
    ship = _ship()
    cur.execute("select distinct payload->>'repo' as repo from events where name in ('release.deployed','release.rolled_back') "
                "and payload->>'env'=%s and (payload->>'ok')::boolean order by 1", (ENV,))
    return [r["repo"] for r in cur.fetchall() if r["repo"] and not ship.prod_held(cur, r["repo"])]


def prod_busy(cur, repo: str) -> bool:
    """その製品の本番への出荷・戻しの依頼が処理待ち"""
    ship = _ship()
    cur.execute("select 1 from events e where name in ('release.approved','release.rollback_requested') and payload->>'release_id' like %s "
                "and coalesce(payload->>'env', 'prod')='prod' and not exists "
                "(select 1 from events r where r.name = any(%s) and (r.payload->>'request_id')::bigint = e.id) limit 1",
                (f"{repo}@%", list(ship.RESULTS)))
    return cur.fetchone() is not None


def down_since(cur, repo: str) -> dict | None:
    """いま止まっているとしたら、その prod.down の中身（＋ id・時刻）"""
    cur.execute("select id, name, payload, occurred_at from events where name in ('prod.down','prod.up') and payload->>'repo'=%s "
                "order by id desc limit 1", (repo,))
    r = cur.fetchone()
    return {**r["payload"], "event_id": r["id"], "at": r["occurred_at"]} if r and r["name"] == "prod.down" else None


def _record(cur, name: str, repo: str, payload: dict) -> int:
    cur.execute("insert into events(name, correlation_id, payload) values (%s,%s,%s) returning id",
                (name, f"prod:{repo}", Jsonb({"repo": repo, "env": ENV, **payload})))
    return cur.fetchone()["id"]


def probe(cur, repo: str) -> tuple[str | None, str | None]:
    """本番に版を聞く。(いまの本番の release_id, 失敗の文) を返す。答えが合えば失敗の文は None"""
    ship = _ship()
    rid = (ship.status(cur, repo)["environments"].get(ENV) or {}).get("release_id")
    rel = ship.release_of(cur, rid) if rid else None
    if not rel:
        return rid, None
    try:
        path, _ = ship.source(repo, rel["app_version"])
        ship.verify(ship.ship_config(path), repo, ENV, rel)
        return rid, None
    except Exception as e:  # noqa: BLE001  答えないのも違う版も同じ「答えが合わない」
        return rid, str(e)[-500:]


def heal(cur, repo: str, rid: str) -> tuple[bool, str | None]:
    """いまの版で本番を起動し直す（DB は残る）。(立て直せたか, 失敗の文)"""
    ship = _ship()
    rel = ship.release_of(cur, rid)
    t0 = time.monotonic()
    try:
        path, _ = ship.source(repo, rel["app_version"])
        ship.deploy(ship.ship_config(path), path, repo, ENV, rel)
        ok, err = True, None
    except Exception as e:  # noqa: BLE001
        ok, err = False, str(e)[-500:]
    _record(cur, "prod.self_healed", repo, {"release_id": rid, "ok": ok, "error": err, "seconds": round(time.monotonic() - t0, 1)})
    return ok, err


def step(cur, now: float | None = None, log=print) -> list[str]:
    """1 周。製品ごとに見て、境目の出来事を返す（ログと試験用）"""
    import incident
    import stoppage
    now = time.monotonic() if now is None else now
    out = []
    for repo in watched(cur):
        if prod_busy(cur, repo):
            continue
        s = _state.setdefault(repo, {"fails": 0, "healed_at": None})
        rid, err = probe(cur, repo)
        down = down_since(cur, repo)
        if err is None:
            s["fails"] = 0
            if down:
                secs = round((_ship().now() - down["at"]).total_seconds(), 1)
                _record(cur, "prod.up", repo, {"release_id": rid, "down_event_id": down["event_id"], "down_seconds": secs})
                incident.close_incident(cur, down["incident_id"], "prod_watch", f"本番が答えるようになった（{secs} 秒止まっていた）")
                stoppage.close_stoppage(cur, f"prod-down:{repo}", remedy="本番が答えるようになった", closed_by="prod_watch")
                out.append(f"{repo}: up")
            continue
        s["fails"] += 1
        if down or s["fails"] < FAILS:
            continue
        if s["healed_at"] is None or now - s["healed_at"] >= HEAL_COOLDOWN_SEC:
            s["healed_at"] = now
            ok, herr = heal(cur, repo, rid)
            out.append(f"{repo}: self_healed ok={ok}")
            if ok:
                s["fails"] = 0
                continue
            err = herr or err
        key = f"prod-down:{_ship().now().strftime('%Y%m%dT%H%M%SZ')}"
        _record(cur, "prod.down", repo, {"release_id": rid, "error": err, "fails": s["fails"], "incident_id": incident.incident_id(repo, key)})
        incident.open_incident(cur, repo, key, "s2", f"本番が答えない: {rid}", detected_by="prod_watch", env=ENV,
                               release_id=rid, impact=f"本番の確かめ（ship.yaml の verify）が {s['fails']} 回続けて合わない。{(err or '')[-200:]}",
                               correlation_id=f"prod:{repo}")
        stoppage.open_stoppage(cur, f"prod-down:{repo}", "factory-bug", f"本番が答えない: {repo}（{rid}）",
                               waiting_for="builder が原因を見る（立て直しは試した）", cause=(err or "")[-300:], detected_by="prod_watch",
                               correlation_id=f"prod:{repo}")
        out.append(f"{repo}: down")
    for line in out:
        log(f"prod_watch: {line}")
    return out


def tick(cur, log=print) -> list[str]:
    """shipper の周回から呼ぶ。INTERVAL ごとに 1 回だけ step を回す。失敗しても出荷の常駐は止めない"""
    now = time.monotonic()
    if INTERVAL <= 0 or (_last_tick[0] is not None and now - _last_tick[0] < INTERVAL):
        return []
    _last_tick[0] = now
    try:
        return step(cur, now, log)
    except Exception as e:  # noqa: BLE001
        log(f"prod_watch: {e}")
        return []
