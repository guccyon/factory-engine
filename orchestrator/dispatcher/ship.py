"""出荷と Rollback（ADR 0012）。Release のイメージを作り、staging に出し、承認で prod に出し、前の版に戻す。

入口（factoryctl・操業コンソール）は events に依頼を書くだけで、手を動かすのは常駐の shipper だけ（ship.py を python で起動）。
コンソールに docker のソケットを渡さないため。依頼は決まった形（版は作った Release の中から選ぶ）で、自由な文字列をコマンドに渡さない。

依頼（events の name）:
  release.ship_requested      {release_id}            イメージを作り staging に出す（staging は自動。phase_grants P2 の deploy_test: auto）
  release.approved            {release_id, by, via}   承認ボタン。staging で確かめた版を prod に出す
  release.rollback_requested  {repo, env, by, via}    その環境を、いま出ている版の 1 つ前の版に戻す
結果（同じ release の correlation_id で）:
  release.built / release.deployed / release.rolled_back / release.ship_failed（payload に request_id。処理済みの印も兼ねる）

製品の出荷の形は製品の .factory-config/ship.yaml（compose のファイル、部品ごとのイメージの作り方、環境ごとの値、確かめ方）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone

import yaml
from psycopg.types.json import Jsonb

import stoppage

WORK = os.environ.get("SHIP_WORKDIR", "/ship")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,40}$")
REQUESTS = ("release.ship_requested", "release.approved", "release.rollback_requested")
RESULTS = ("release.built", "release.deployed", "release.rolled_back", "release.ship_failed")


def now() -> datetime:
    return datetime.now(timezone.utc)


def record(cur, name: str, release_id: str, payload: dict):
    cur.execute("insert into events(name, correlation_id, payload) values (%s,%s,%s) returning id",
                (name, f"release:{release_id}", Jsonb({"release_id": release_id, **payload})))
    return cur.fetchone()["id"]


# ------------------------------------------------------------------ Release を切る（factoryctl・supervisor の出荷の列車）
def create_release(cur, repo: str, version: str, prs: list[str] | None = None) -> tuple[str | None, list[str]]:
    """Release を作る（ADR 0007）。prs がなければ、まだどの Release にも入っていないマージ済みの工場の PR をまとめる。
    すでにあれば (None, [])"""
    rid = f"{repo}@{version}"
    if prs is None:
        cur.execute("select payload->>'pr_url' as u from events e where name='pr.merged' and payload->>'pr_url' like %s "
                    "and not exists (select 1 from events r where r.name='release.included' and r.payload->>'pr_url'=e.payload->>'pr_url') "
                    "order by payload->>'merged_at'", (f"https://github.com/{repo}/pull/%",))
        prs = [r["u"] for r in cur.fetchall()]
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('release.created',%s,%s,%s) on conflict (dedupe_key) do nothing returning id",
                (f"release:{rid}", Jsonb({"release_id": rid, "repo": repo, "app_version": version, "prs": prs}), f"release.created:{rid}"))
    if cur.fetchone() is None:
        return None, []
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values ('Release',%s,'ship',%s)",
                (f"release:{rid}", Jsonb({"release_id": rid, "repo": repo, "app_version": version, "prs": prs})))
    for u in prs:   # 1 つの PR が入る Release は最初の 1 つ
        cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('release.included',%s,%s,%s) on conflict (dedupe_key) do nothing",
                    (f"release:{rid}", Jsonb({"release_id": rid, "app_version": version, "pr_url": u}), f"release.included:{u}"))
    return rid, prs


def next_version(cur, repo: str) -> str:
    """直近の Release の版の minor を 1 つ上げる（0.1.0 → 0.2.0）。数字 3 つでない版や Release がなければ 0.1.0 から"""
    cur.execute("select payload->>'app_version' as v from events where name='release.created' and payload->>'repo'=%s order by id desc", (repo,))
    for r in cur.fetchall():
        m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", r["v"] or "")
        if m:
            return f"{m.group(1)}.{int(m.group(2)) + 1}.0"
    return "0.1.0"


def unreleased(cur, repo: str) -> int:
    cur.execute("select count(*) as n from events e where name='pr.merged' and payload->>'pr_url' like %s "
                "and not exists (select 1 from events r where r.name='release.included' and r.payload->>'pr_url'=e.payload->>'pr_url')",
                (f"https://github.com/{repo}/pull/%",))
    return cur.fetchone()["n"]


def busy(cur, repo: str) -> bool:
    """その製品の出荷の依頼が処理待ちか、staging に出した版が prod への承認を待っているか"""
    cur.execute("select 1 from events e where name = any(%s) and payload->>'release_id' like %s and not exists "
                "(select 1 from events r where r.name = any(%s) and (r.payload->>'request_id')::bigint = e.id) limit 1",
                (list(REQUESTS), f"{repo}@%", list(RESULTS)))
    if cur.fetchone():
        return True
    envs = status(cur, repo)["environments"]
    staged = envs.get("staging", {}).get("release_id")
    return bool(staged) and staged != envs.get("prod", {}).get("release_id")


# ------------------------------------------------------------------ 入口（factoryctl・コンソール）
def release_of(cur, release_id: str) -> dict | None:
    cur.execute("select payload from events where name='release.created' and payload->>'release_id'=%s", (release_id,))
    r = cur.fetchone()
    return r["payload"] if r else None


def request_ship(cur, release_id: str, by: str, via: str) -> tuple[bool, str]:
    rel = release_of(cur, release_id)
    if not rel:
        return False, "その Release はない"
    if not VERSION_RE.match(rel["app_version"]):
        return False, "版の形が出荷できない（英数字と . _ - だけ）"
    record(cur, "release.ship_requested", release_id, {"by": by, "via": via})
    return True, "staging への出荷を依頼した"


def request_approve(cur, release_id: str, by: str, via: str) -> tuple[bool, str]:
    st = status(cur, release_of(cur, release_id)["repo"]) if release_of(cur, release_id) else None
    if not st:
        return False, "その Release はない"
    staged = st["environments"].get("staging", {}).get("release_id")
    if staged != release_id:
        return False, "staging でいま確かめられている版ではない"
    record(cur, "release.approved", release_id, {"by": by, "via": via})
    return True, "prod への出荷を承認した"


def request_rollback(cur, repo: str, env: str, by: str, via: str) -> tuple[bool, str]:
    st = status(cur, repo)
    cur_env = st["environments"].get(env)
    if not cur_env or not cur_env.get("previous"):
        return False, "戻す先の版がない"
    record(cur, "release.rollback_requested", cur_env["release_id"], {"repo": repo, "env": env, "by": by, "via": via})
    return True, f"{env} を {cur_env['previous']} に戻す依頼をした"


def status(cur, repo: str) -> dict:
    """環境ごとのいまの版と、その前の版（出した順。Rollback で戻った版も出した版として数える）"""
    cur.execute("select name, payload, occurred_at from events where name in ('release.deployed','release.rolled_back') "
                "and payload->>'repo'=%s and (payload->>'ok')::boolean order by id", (repo,))
    history: dict[str, list[str]] = {}
    for r in cur.fetchall():
        p = r["payload"]
        h = history.setdefault(p["env"], [])
        if r["name"] == "release.rolled_back":
            while h and h[-1] != p["release_id"]:
                h.pop()            # 戻した版より後に出した版は履歴から外す（もう一度戻すと、さらに前へ）
        else:
            h.append(p["release_id"])
    envs = {}
    for env, h in history.items():
        envs[env] = {"release_id": h[-1] if h else None, "previous": h[-2] if len(h) > 1 else None}
    cur.execute("select payload->>'release_id' as rid from events where name='release.created' and payload->>'repo'=%s order by id desc limit 10", (repo,))
    return {"repo": repo, "environments": envs, "releases": [r["rid"] for r in cur.fetchall()]}


# ------------------------------------------------------------------ shipper（常駐）
def sh(args: list[str], cwd: str | None = None, env: dict | None = None, timeout: int = 1800) -> str:
    r = subprocess.run(args, cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])}… が失敗（{r.returncode}）: {(r.stderr or r.stdout)[-800:]}")
    return r.stdout


def source(repo: str, version: str, commit: str | None = None) -> tuple[str, str]:
    """その版のソース（製品の既定ブランチの先頭、または記録したコミット）。版ごとに残す"""
    path = os.path.join(WORK, "src", repo.replace("/", "__"), version)
    if not os.path.isdir(os.path.join(path, ".git")):
        shutil.rmtree(path, ignore_errors=True)
        token = os.environ.get("GH_TOKEN", "")
        url = f"https://x-access-token:{token}@github.com/{repo}.git" if token else f"https://github.com/{repo}.git"
        sh(["git", "clone", "--quiet", url, path], timeout=600)
        if commit:
            sh(["git", "-C", path, "checkout", "--quiet", commit])
    return path, sh(["git", "-C", path, "rev-parse", "HEAD"]).strip()


def ship_config(path: str) -> dict:
    with open(os.path.join(path, ".factory-config", "ship.yaml")) as f:
        return yaml.safe_load(f)


def project(repo: str, env: str) -> str:
    return f"{repo.split('/')[-1]}-{env}"


def image(repo: str, part: str, version: str) -> str:
    return f"{repo.split('/')[-1]}-{part}:{version}"


def compose_env(cfg: dict, repo: str, env: str, rel: dict) -> dict:
    e = {str(k): str(v) for k, v in (cfg["environments"][env].get("env") or {}).items()}
    for part, spec in cfg["images"].items():
        e[spec["env"]] = image(repo, part, rel["app_version"])
    e.update({"COMPOSE_PROJECT_NAME": project(repo, env), "APP_VERSION": rel["app_version"], "RELEASE_ID": rel["release_id"]})
    return e


def deploy(cfg: dict, path: str, repo: str, env: str, rel: dict) -> float:
    """compose で起動し、確かめ終わるまでの秒数を返す"""
    t0 = time.monotonic()
    compose = ["docker", "compose", "-f", os.path.join(path, cfg["compose"])]
    sh(compose + ["up", "-d", "--wait", "--remove-orphans"], env=compose_env(cfg, repo, env, rel), timeout=600)
    verify(cfg, repo, env, rel)
    return round(time.monotonic() - t0, 1)


def verify(cfg: dict, repo: str, env: str, rel: dict):
    v = cfg["verify"]
    out = sh(["docker", "compose", "-p", project(repo, env), "exec", "-T", v["service"], "curl", "-fsS", v["url"]], timeout=60)
    got = json.loads(out).get("app_version")
    if got != rel["app_version"]:
        raise RuntimeError(f"{env} の {v['service']} が答えた版が {got}（期待 {rel['app_version']}）")


def handle(cur, ev: dict):
    p, name = ev["payload"], ev["name"]
    rid = p["release_id"]
    rel = release_of(cur, rid)
    repo = rel["repo"]
    common = {"repo": repo, "request_id": ev["id"]}
    try:
        if name == "release.ship_requested":
            path, commit = source(repo, rel["app_version"])
            cfg = ship_config(path)
            t0 = time.monotonic()
            for part, spec in cfg["images"].items():
                args = [a for k, v in (spec.get("build_args") or {}).items() for a in ("--build-arg", f"{k}={v}")]
                sh(["docker", "build", "-q", "-t", image(repo, part, rel["app_version"]), *args, os.path.join(path, spec["context"])],
                   env={"DOCKER_BUILDKIT": "1"}, timeout=3600)
            record(cur, "release.built", rid, {**common, "commit": commit, "seconds": round(time.monotonic() - t0, 1),
                                               "images": [image(repo, part, rel["app_version"]) for part in cfg["images"]]})
            secs = deploy(cfg, path, repo, "staging", rel)
            record(cur, "release.deployed", rid, {**common, "env": "staging", "ok": True, "seconds": secs, "commit": commit})
        elif name == "release.approved":
            path, _ = source(repo, rel["app_version"])
            secs = deploy(ship_config(path), path, repo, "prod", rel)
            record(cur, "release.deployed", rid, {**common, "env": "prod", "ok": True, "seconds": secs,
                                                  "approved_by": p.get("by"), "approved_via": p.get("via")})
        elif name == "release.rollback_requested":
            env = p["env"]
            target_id = status(cur, repo)["environments"][env]["previous"]
            target = release_of(cur, target_id)
            path, _ = source(repo, target["app_version"])
            deploy(ship_config(path), path, repo, env, target)
            secs = round((now() - ev["occurred_at"]).total_seconds(), 1)   # 押してから確かめ終わるまで
            record(cur, "release.rolled_back", target_id, {**common, "env": env, "ok": True, "from": rid, "seconds": secs,
                                                           "by": p.get("by"), "via": p.get("via")})
    except Exception as e:  # noqa: BLE001  失敗は記録して停止に開き、次の依頼へ
        record(cur, "release.ship_failed", rid, {**common, "request": name, "error": str(e)[-1500:]})
        stoppage.open_stoppage(cur, f"ship:{ev['id']}", "factory-bug", f"出荷の失敗: {name} {rid}",
                               waiting_for="builder が原因を見る", cause=str(e)[-300:], detected_by="shipper",
                               correlation_id=f"release:{rid}")


def pending(cur) -> list[dict]:
    cur.execute("select id, name, payload, occurred_at from events e where name = any(%s) and not exists "
                "(select 1 from events r where r.name = any(%s) and (r.payload->>'request_id')::bigint = e.id) order by id",
                (list(REQUESTS), list(RESULTS)))
    return cur.fetchall()


def main():
    import psycopg
    from psycopg.rows import dict_row
    interval = float(os.environ.get("SHIP_POLL_SEC", "2"))
    os.makedirs(WORK, exist_ok=True)
    print("shipper: started", flush=True)
    while True:
        try:
            with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True) as conn, conn.cursor() as cur:
                cur.execute("insert into events(name, payload) values ('shipper.heartbeat', '{}')")
                while True:
                    reqs = pending(cur)
                    for ev in reqs:
                        print(f"shipper: {ev['name']} {ev['payload'].get('release_id')}", flush=True)
                        handle(cur, ev)
                    time.sleep(interval)
        except Exception as e:  # noqa: BLE001  DB が切れたらつなぎ直す
            print(f"shipper: {e}", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
