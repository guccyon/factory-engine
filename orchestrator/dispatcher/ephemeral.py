"""PR ごとの使い捨て環境（ADR 0043 の O7 ephemeral_env）。Gen1 の最小の形で、回路の確認までにとどめる。

触れる環境（touch.py・ship.py）の仕組みを PR の単位に広げる。Mac の docker で compose のプロジェクトを PR ごとに分け（<製品>-pr-<番号>）、
合成シードを入れ、QA 役か人が触ったあと、PR が閉じたら壊す。同時に立てるのは EPHEMERAL_MAX（既定 2）まで。
Gen1.5 で製品が複数のリポジトリに分かれたら、環境の単位を「PR の束」に作り直す前提（ADR 0043 の持ち越しの節）。

入口（factoryctl・supervisor）は events に依頼を書くだけで、手を動かすのは常駐の shipper（ship.py の main から handle を呼ぶ）。
立てるのは、その製品のリポジトリの中のブランチから出た開いた PR だけ（fork の PR のコードはこの機械で作らない）。shipper が GitHub で確かめる。

依頼（events の name。correlation_id は env:<repo>#<番号>）:
  env.ephemeral_up_requested    {repo, pr, by, via}
  env.ephemeral_down_requested  {repo, pr, by, via, reason: pr_closed|ttl|manual}
結果（payload に request_id。処理済みの印も兼ねる）:
  env.ephemeral_up      {repo, pr, slot, project, head_sha, entries, accounts, seconds}
  env.ephemeral_down    {repo, pr, project, reason, touched: [..], lived_seconds}
  env.ephemeral_failed  {repo, pr, request, error}
触った印:
  env.ephemeral_touched {repo, pr, by, kind: human|qa, note}   人は factoryctl env pr-touch。QA 役をこの環境に向けて Verdict で残すのは Gen1.5

製品の形は ship.yaml の ephemeral（{base: staging, port_step: 1000, port_envs: [..]}）。ない製品は立てない。
ポートは base の環境の値に slot × port_step を足す（slot は 1 から EPHEMERAL_MAX）。イメージは <製品>-<部品>:pr-<番号>。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

REQUESTS = ("env.ephemeral_up_requested", "env.ephemeral_down_requested")
RESULTS = ("env.ephemeral_up", "env.ephemeral_down", "env.ephemeral_failed")
MAX = int(os.environ.get("EPHEMERAL_MAX", "2"))
TTL_HOURS = float(os.environ.get("EPHEMERAL_TTL_HOURS", "48"))   # PR が開いたままでも、これを過ぎたら壊す（Mac の容量を守る）


def corr(repo: str, pr: int) -> str:
    return f"env:{repo}#{pr}"


def name_of(repo: str, pr: int) -> str:
    """compose のプロジェクト名（ship.project の環境名に pr-<番号> を渡したものと同じ）"""
    return f"{repo.split('/')[-1]}-pr-{pr}"


def record(cur, name: str, repo: str, pr: int, payload: dict) -> int:
    cur.execute("insert into events(name, correlation_id, payload) values (%s,%s,%s) returning id",
                (name, corr(repo, pr), Jsonb({"repo": repo, "pr": pr, **payload})))
    return cur.fetchone()["id"]


# ------------------------------------------------------------------ いまの状態
def active(cur) -> list[dict]:
    """立っている環境（up のあとに down がないもの）。新しい順ではなく立てた順"""
    cur.execute("""select u.payload, u.occurred_at from events u where u.name='env.ephemeral_up'
                   and not exists (select 1 from events d where d.name='env.ephemeral_down' and d.correlation_id=u.correlation_id and d.id > u.id)
                   order by u.id""")
    return [{**r["payload"], "at": r["occurred_at"]} for r in cur.fetchall()]


def pending(cur) -> list[dict]:
    cur.execute("select id, name, payload, occurred_at from events e where name = any(%s) and not exists "
                "(select 1 from events r where r.name = any(%s) and (r.payload->>'request_id')::bigint = e.id) order by id",
                (list(REQUESTS), list(RESULTS)))
    return cur.fetchall()


def slot_for(cur, repo: str, pr: int) -> int | None:
    used = {int(a["slot"]) for a in active(cur) if (a["repo"], int(a["pr"])) != (repo, pr)}
    return next((s for s in range(1, MAX + 1) if s not in used), None)


# ------------------------------------------------------------------ 依頼（factoryctl・supervisor）
def request_up(cur, repo: str, pr: int, by: str, via: str) -> tuple[bool, str]:
    if any((a["repo"], int(a["pr"])) == (repo, pr) for a in active(cur)):
        return False, f"{repo}#{pr} の環境はもう立っている（factoryctl env pr-show で入口を見る）"
    waiting = pending(cur)
    if any(e["name"] == "env.ephemeral_up_requested" and (e["payload"]["repo"], int(e["payload"]["pr"])) == (repo, pr) for e in waiting):
        return False, f"{repo}#{pr} の環境はもう依頼してある"
    if len(active(cur)) + sum(1 for e in waiting if e["name"] == "env.ephemeral_up_requested") >= MAX:
        return False, f"同時に立てられるのは {MAX} つまで（factoryctl env pr-show で立っているものを見て、要らないものを pr-down する）"
    record(cur, "env.ephemeral_up_requested", repo, pr, {"by": by, "via": via})
    return True, f"{repo}#{pr} の使い捨て環境を立てる依頼をした"


def request_down(cur, repo: str, pr: int, by: str, via: str, reason: str = "manual") -> tuple[bool, str]:
    if not any((a["repo"], int(a["pr"])) == (repo, pr) for a in active(cur)):
        return False, f"{repo}#{pr} の環境は立っていない"
    if any(e["name"] == "env.ephemeral_down_requested" and (e["payload"]["repo"], int(e["payload"]["pr"])) == (repo, pr) for e in pending(cur)):
        return False, "もう壊す依頼をしてある"
    record(cur, "env.ephemeral_down_requested", repo, pr, {"by": by, "via": via, "reason": reason})
    return True, f"{repo}#{pr} の使い捨て環境を壊す依頼をした"


def touch(cur, repo: str, pr: int, by: str, kind: str = "human", note: str = "") -> tuple[bool, str]:
    """触った印。Gen1 は人が factoryctl env pr-touch で残す（kind=qa は QA 役をこの環境に向けたときのために空けておく）"""
    if not any((a["repo"], int(a["pr"])) == (repo, pr) for a in active(cur)):
        return False, f"{repo}#{pr} の環境は立っていない"
    record(cur, "env.ephemeral_touched", repo, pr, {"by": by, "kind": kind, "note": (note or "")[:300]})
    return True, f"{repo}#{pr} の環境を触った印を残した"


def due_down(cur, closed, now: datetime | None = None) -> list[tuple[str, int, str]]:
    """壊す時期が来た環境 [(repo, pr, reason)]。closed(repo, pr) は PR が閉じていれば真（supervisor が GitHub を見る）"""
    now = now or datetime.now(timezone.utc)
    asked = {(e["payload"]["repo"], int(e["payload"]["pr"])) for e in pending(cur) if e["name"] == "env.ephemeral_down_requested"}
    out = []
    for a in active(cur):
        key = (a["repo"], int(a["pr"]))
        if key in asked:
            continue
        if closed(*key):
            out.append((*key, "pr_closed"))
        elif (now - a["at"]).total_seconds() > TTL_HOURS * 3600:
            out.append((*key, "ttl"))
    return out


# ------------------------------------------------------------------ shipper（ship.py の main から）
def config(cfg: dict) -> dict:
    e = cfg.get("ephemeral")
    if not isinstance(e, dict):
        raise RuntimeError("この製品は ship.yaml に ephemeral がないので、PR ごとの環境を立てない")
    return e


def env_values(cfg: dict, slot: int) -> dict:
    """base の環境の値に、ポートだけ slot × port_step を足す"""
    e = config(cfg)
    base = dict(cfg["environments"][str(e.get("base") or "staging")].get("env") or {})
    step = int(e.get("port_step") or 1000)
    ports = e.get("port_envs") or [k for k in base if str(k).endswith("_PORT")]
    return {str(k): (int(v) + slot * step if k in ports else v) for k, v in base.items()}


def compose_env(cfg: dict, repo: str, pr: int, slot: int) -> dict:
    import ship
    tag = f"pr-{pr}"
    e = {k: str(v) for k, v in env_values(cfg, slot).items()}
    for part, spec in cfg["images"].items():
        e[spec["env"]] = ship.image(repo, part, tag)
    e.update({"COMPOSE_PROJECT_NAME": name_of(repo, pr), "APP_VERSION": tag, "RELEASE_ID": f"{repo}#{pr}"})
    t = cfg.get("touch") or {}
    if t.get("bind_env") and ship.TOUCH_BIND and ship.HOST_RE.match(ship.TOUCH_BIND):
        e[str(t["bind_env"])] = ship.TOUCH_BIND
    return e


def open_pr(repo: str, pr: int) -> str:
    """開いていて、その製品のリポジトリの中のブランチから出た PR の head のコミット。そうでなければ止める"""
    import ghapi
    p = ghapi.request("GET", f"repos/{repo}/pulls/{int(pr)}", user_agent="factory-shipper")
    if p.get("state") != "open":
        raise RuntimeError(f"{repo}#{pr} は開いていない")
    if ((p.get("head") or {}).get("repo") or {}).get("full_name") != repo:
        raise RuntimeError(f"{repo}#{pr} は別のリポジトリ（fork）から出た PR なので、この機械で作らない")
    return p["head"]["sha"]


def source_dir(repo: str, pr: int) -> str:
    import ship
    return os.path.join(ship.WORK, "src", repo.replace("/", "__"), f"pr-{pr}")


def up(cur, ev: dict):
    import ship
    p = ev["payload"]
    repo, pr = p["repo"], int(p["pr"])
    slot = slot_for(cur, repo, pr)
    if slot is None:
        raise RuntimeError(f"同時に立てられるのは {MAX} つまで")
    sha = open_pr(repo, pr)
    shutil.rmtree(source_dir(repo, pr), ignore_errors=True)
    path, commit = ship.source(repo, f"pr-{pr}", sha)
    cfg = ship.ship_config(path)
    config(cfg)
    t0 = time.monotonic()
    for part, spec in cfg["images"].items():
        args = [a for k, v in (spec.get("build_args") or {}).items() for a in ("--build-arg", f"{k}={v}")]
        ship.sh(["docker", "build", "-q", "-t", ship.image(repo, part, f"pr-{pr}"), *args, os.path.join(path, spec["context"])],
                env={"DOCKER_BUILDKIT": "1"}, timeout=3600)
    e = compose_env(cfg, repo, pr, slot)
    compose = ["docker", "compose", "-f", os.path.join(path, cfg["compose"])]
    ship.sh(compose + ["up", "-d", "--wait", "--remove-orphans"], env=e, timeout=600)
    ship.verify(cfg, repo, f"pr-{pr}", {"app_version": f"pr-{pr}"})
    if cfg.get("seed"):
        sd = cfg["seed"]
        ship.sh(["docker", "compose", "-p", name_of(repo, pr), "exec", "-T", sd["service"], *sd["command"]], timeout=900)
    entries, accounts = touch_points(cfg, repo, pr, slot)
    record(cur, "env.ephemeral_up", repo, pr, {"request_id": ev["id"], "slot": slot, "project": name_of(repo, pr), "head_sha": commit,
                                               "entries": entries, "accounts": accounts, "seconds": round(time.monotonic() - t0, 1),
                                               "by": p.get("by"), "via": p.get("via")})


def touch_points(cfg: dict, repo: str, pr: int, slot: int) -> tuple[list[dict], list[dict]]:
    """入口の URL とログインの手がかり（ship.yaml の touch をそのまま使い、ポートだけずらす）"""
    import ship
    t = cfg.get("touch") or {}
    host = ship.TOUCH_HOST if ship.TOUCH_HOST and ship.HOST_RE.match(ship.TOUCH_HOST) else "127.0.0.1"
    values = env_values(cfg, slot)
    entries = [{"label": str(x.get("label") or x.get("port_env")), "url": f"http://{host}:{int(values[x['port_env']])}{x.get('path') or '/'}",
                "qr": bool(x.get("qr"))} for x in t.get("entries") or [] if values.get(x.get("port_env")) is not None]
    accounts = []
    if t.get("accounts"):
        try:
            out = ship.sh(["docker", "compose", "-p", name_of(repo, pr), "exec", "-T", t["accounts"]["service"], *t["accounts"]["command"]], timeout=60)
            accounts = [{"label": str(a.get("label")), "value": str(a.get("value"))} for a in json.loads(out.strip() or "[]")][:20]
        except Exception:  # noqa: BLE001  手がかりが欠けるだけ
            accounts = []
    return entries, accounts


def down(cur, ev: dict):
    import ship
    p = ev["payload"]
    repo, pr = p["repo"], int(p["pr"])
    ship.sh(["docker", "compose", "-p", name_of(repo, pr), "down", "-v", "--remove-orphans"], timeout=300)
    cfg_images = []
    try:
        cfg_images = [ship.image(repo, part, f"pr-{pr}") for part in ship.ship_config(source_dir(repo, pr))["images"]]
    except Exception:  # noqa: BLE001  ソースがなければイメージは残る（docker image prune で消える）
        pass
    for img in cfg_images:
        try:
            ship.sh(["docker", "image", "rm", img], timeout=120)
        except Exception:  # noqa: BLE001
            pass
    shutil.rmtree(source_dir(repo, pr), ignore_errors=True)
    finish_down(cur, ev)


def finish_down(cur, ev: dict):
    p = ev["payload"]
    repo, pr = p["repo"], int(p["pr"])
    cur.execute("select occurred_at from events where name='env.ephemeral_up' and correlation_id=%s order by id desc limit 1", (corr(repo, pr),))
    r = cur.fetchone()
    cur.execute("select payload->>'by' as by, payload->>'kind' as kind from events where name='env.ephemeral_touched' and correlation_id=%s "
                "and occurred_at >= %s order by id", (corr(repo, pr), r["occurred_at"] if r else datetime.fromtimestamp(0, timezone.utc)))
    touched = [dict(x) for x in cur.fetchall()]
    lived = round((datetime.now(timezone.utc) - r["occurred_at"]).total_seconds()) if r else None
    record(cur, "env.ephemeral_down", repo, pr, {"request_id": ev["id"], "project": name_of(repo, pr), "reason": p.get("reason"),
                                                 "touched": touched, "lived_seconds": lived, "by": p.get("by")})


def cleanup(repo: str, pr: int):
    import ship
    try:
        ship.sh(["docker", "compose", "-p", name_of(repo, pr), "down", "-v", "--remove-orphans"], timeout=300)
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(source_dir(repo, pr), ignore_errors=True)


def handle(cur, ev: dict):
    p = ev["payload"]
    try:
        if ev["name"] == "env.ephemeral_up_requested":
            up(cur, ev)
        else:
            down(cur, ev)
    except Exception as e:  # noqa: BLE001  失敗は記録して次へ。立てかけた分はその場で片付ける（up の記録がないので pr-down では壊せない）
        if ev["name"] == "env.ephemeral_up_requested":
            cleanup(p["repo"], int(p["pr"]))
        record(cur, "env.ephemeral_failed", p["repo"], int(p["pr"]), {"request_id": ev["id"], "request": ev["name"], "error": str(e)[-1500:]})
