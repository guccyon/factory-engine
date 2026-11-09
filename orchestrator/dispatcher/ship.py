"""出荷と Rollback（ADR 0012）。Release のイメージを作り、staging に出し、承認で prod に出し、前の版に戻す。

入口（factoryctl・操業コンソール）は events に依頼を書くだけで、手を動かすのは常駐の shipper だけ（ship.py を python で起動）。
コンソールに docker のソケットを渡さないため。依頼は決まった形（版は作った Release の中から選ぶ）で、自由な文字列をコマンドに渡さない。

依頼（events の name）:
  release.ship_requested      {release_id}            イメージを作り staging に出す（staging は自動。phase_grants P2 の deploy_test: auto）
  release.approved            {release_id, by, via}   承認ボタン。staging で確かめた版を prod に出す
  release.rollback_requested  {repo, env, by, via}    その環境を、いま出ている版の 1 つ前の版に戻す
  release.reseed_requested    {env, by, via}          触れる環境の DB を空にして合成シードから入れ直す（ADR 0028 の touchable_env。release_id はいまその環境の版）
  release.restart_requested   {repo, env, advance_seconds, reset_clock, by, via}
                                                      触れる環境の部品を再起動し、サーバーの時計を進める（ADR 0028 の scenario_time。DB は残す）
  release.kpi_requested       {repo, env, week, by, via}
                                                      その環境で製品の KPI を測る（ADR 0043 の O2 の週次の Report。ship.yaml の kpi のコマンドを読むだけで、環境を変えない）
結果（同じ release の correlation_id で）:
  release.built / release.deployed / release.rolled_back / release.reseeded / release.restarted / release.kpi_measured / release.ship_failed
  （payload に request_id。処理済みの印も兼ねる。KPI を測れなかったときは release.kpi_measured の ok: false で、停止には開かない）
  release.seeded（合成シードを書いた。ship.yaml の seed の環境に 1 回だけ。入れ直しのたびにも）
  release.touchable（触れる環境の入口。ship.yaml の touch の環境に出す・戻す・入れ直すたびに。URL・QR を付ける入口・ログインの手がかり）

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

import ghapp
import stoppage

WORK = os.environ.get("SHIP_WORKDIR", "/ship")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,40}$")
REQUESTS = ("release.ship_requested", "release.approved", "release.rollback_requested", "release.reseed_requested", "release.restart_requested",
            "release.kpi_requested")
RESULTS = ("release.built", "release.deployed", "release.rolled_back", "release.reseeded", "release.restarted", "release.kpi_measured",
           "release.ship_failed")
# 製品の KPI（ADR 0043 の O2）。ship.yaml の kpi.metrics の id の形と数の上限。コマンドの出力は {id: 数} の JSON
KPI_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,60}$")
KPI_MAX = 12
WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
# 時計を進める上限（ADR 0028 の scenario_time）。1 回に進める量と、進めた量の合計。翌日・暦月の境をまたぐのに足りる幅に限る
ADVANCE_MAX_SECONDS = 7 * 24 * 3600
OFFSET_MAX_SECONDS = 62 * 24 * 3600
# 時計を 0 に戻す出来事（compose の変数を渡し直さずに作り直すもの）。その後の時計は 0
CLOCK_RESETTING = ("release.deployed", "release.rolled_back", "release.reseeded")
# 触れる環境（ADR 0028 の touchable_env）。人の端末（スマートフォン）から届く工場の機械の名前と、画面の待ち受け。
# どちらも端末ごとの値なのでリポジトリに書かず、orchestrator/.env か docker-compose.override.yml で渡す。
# 待ち受けがなければ製品の compose の既定（127.0.0.1）のままで、入口の URL はこの機械からしか開けない
TOUCH_HOST = os.environ.get("SHIP_TOUCH_HOST", "").strip()
TOUCH_BIND = os.environ.get("SHIP_TOUCH_BIND", "").strip()
HOST_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.:-]{0,252}$")
# 製品の段階（ADR 0028 の決定 4。definitions/product_stages.yaml の写し。このコンテナには definitions/ がないので写す。食い違いはテストで見る）。
# 段階で出荷先が決まる。development は本番を立てない（承認を受け付けず、shipper も本番を起動しない。staging の版が本番待ちにならないので、
# 出荷の列車は staging に出し続ける）。宣言は製品の manifest.yaml の product_stage で、supervisor が読んで product.stage_observed に残す。
# 一度も読めていない製品は development に倒す（#160 の SHIP_PROD_HOLD を置き換えた）
STAGES = {
    "development": {"ja": "開発中（1st リリース前）", "ship_to": ["staging"], "operation_started": False},
    "launch": {"ja": "公開の準備（本番を立てて、最初の利用者に出すまで）", "ship_to": ["staging", "prod"], "operation_started": False},
    "operating": {"ja": "運用中（利用者に出した後）", "ship_to": ["staging", "prod"], "operation_started": True},
}
DEFAULT_STAGE = "development"


def declared_stage(manifest: dict) -> str | None:
    """manifest.yaml の product_stage の値。{stage: development, ...} の形のほか、product_stage: development とだけ書いたものも読む"""
    v = manifest.get("product_stage")
    v = v.get("stage") if isinstance(v, dict) else v
    return str(v) if v else None


def stage_of(cur, repo: str) -> dict:
    """製品のいまの段階 {stage, ja, ship_to, operation_started, declared}。declared は manifest に書かれていた値
    （manifest を一度も読めていなければ None、欄がなければ ""。どちらも development として扱う）"""
    cur.execute("select payload from events where name='product.stage_observed' and payload->>'repo'=%s "
                "order by id desc limit 1", (repo,))
    r = cur.fetchone()
    p = r["payload"] if r else {}
    s = p.get("stage") if p.get("stage") in STAGES else DEFAULT_STAGE
    return {"stage": s, **STAGES[s], "declared": p.get("declared") if r else None}


def observe_stage(cur, repo: str, stage: str | None, by: str = "supervisor") -> bool:
    """manifest の宣言を残す。変わったときだけ書く（知らない値は development として扱い、そのまま残して人が気づけるようにする）"""
    cur.execute("select payload->>'declared' as d from events where name='product.stage_observed' and payload->>'repo'=%s "
                "order by id desc limit 1", (repo,))
    r = cur.fetchone()
    declared = stage or ""
    if r and r["d"] == declared:
        return False
    effective = declared if declared in STAGES else DEFAULT_STAGE
    cur.execute("insert into events(name, correlation_id, payload) values ('product.stage_observed',%s,%s)",
                (f"product:{repo}", Jsonb({"repo": repo, "stage": effective, "declared": declared, "known": declared in STAGES, "by": by})))
    return True


def prod_held(cur, repo: str) -> bool:
    return "prod" not in stage_of(cur, repo)["ship_to"]


def held_msg(cur, repo: str) -> str:
    st = stage_of(cur, repo)
    return f"この製品は {st['stage']}（{st['ja']}）の段階なので本番には出さない（段階の切り替えは manifest.yaml の product_stage を人の承認で）"


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
    if prod_held(cur, repo):
        return False   # 本番に出さないので、staging の版は本番待ちにならない
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
    repo = release_of(cur, release_id)["repo"]
    if prod_held(cur, repo):
        return False, held_msg(cur, repo)
    staged = st["environments"].get("staging", {}).get("release_id")
    if staged != release_id:
        return False, "staging でいま確かめられている版ではない"
    record(cur, "release.approved", release_id, {"by": by, "via": via})
    return True, "prod への出荷を承認した"


def request_reseed(cur, repo: str, env: str, by: str, via: str) -> tuple[bool, str]:
    """触れる環境の DB を空にしてシードから入れ直す依頼。いまその環境に出ている版で行う（どの環境かは shipper が ship.yaml の touch で確かめる）"""
    if env == "prod":
        return False, "本番の DB は入れ直さない"
    rid = status(cur, repo)["environments"].get(env, {}).get("release_id")
    if not rid:
        return False, f"{env} にまだ版が出ていない"
    record(cur, "release.reseed_requested", rid, {"repo": repo, "env": env, "by": by, "via": via})
    return True, f"{env} の DB を空にして、{rid} の合成シードから入れ直す依頼をした"


def clock_offset(cur, repo: str, env: str) -> int:
    """その環境のサーバーの時計を、いま何秒進めているか。最後の再起動（release.restarted）の値で、その後に出す・戻す・入れ直すがあれば 0"""
    cur.execute("select name, payload from events where name = any(%s) and payload->>'repo'=%s and payload->>'env'=%s order by id desc limit 1",
                (list(CLOCK_RESETTING) + ["release.restarted"], repo, env))
    r = cur.fetchone()
    if not r or r["name"] != "release.restarted":
        return 0
    return int(r["payload"].get("clock_offset_seconds") or 0)


def request_restart(cur, repo: str, env: str, by: str, via: str, advance_seconds: int = 0, reset_clock: bool = False) -> tuple[bool, str]:
    """触れる環境の部品（ship.yaml の touch.restart）を再起動し、サーバーの時計を advance_seconds 進める（reset_clock なら 0 に戻す）依頼。
    DB は残す。どの環境で何を進められるかは shipper が ship.yaml の touch で確かめる"""
    if env == "prod":
        return False, "本番は再起動しない・時計を進めない"
    if not isinstance(advance_seconds, int) or isinstance(advance_seconds, bool) or not 0 <= advance_seconds <= ADVANCE_MAX_SECONDS:
        return False, f"1 回に進める時計は 0〜{ADVANCE_MAX_SECONDS // 3600} 時間"
    if reset_clock and advance_seconds:
        return False, "時計を戻すときは進めない（別々に依頼する）"
    if not reset_clock and clock_offset(cur, repo, env) + advance_seconds > OFFSET_MAX_SECONDS:
        return False, f"進めた時計の合計が {OFFSET_MAX_SECONDS // 86400} 日を超える。reset_clock で 0 に戻してから進める"
    rid = status(cur, repo)["environments"].get(env, {}).get("release_id")
    if not rid:
        return False, f"{env} にまだ版が出ていない"
    record(cur, "release.restart_requested", rid, {"repo": repo, "env": env, "advance_seconds": advance_seconds,
                                                   "reset_clock": bool(reset_clock), "by": by, "via": via})
    what = "時計を 0 に戻して" if reset_clock else (f"時計を {advance_seconds // 3600} 時間進めて" if advance_seconds else "")
    return True, f"{env}（{rid}）を{what}再起動する依頼をした"


def request_kpi(cur, repo: str, env: str, week: str, by: str, via: str) -> tuple[bool, str]:
    """その環境で製品の KPI を測る依頼（ADR 0043 の O2）。いまその環境に出ている版の ship.yaml の kpi を shipper が読む。
    week は集計の週（JST の ISO 週、例 2026-W46）。測るだけで環境を変えない"""
    if not WEEK_RE.match(week or ""):
        return False, "週は 2026-W46 の形"
    rid = status(cur, repo)["environments"].get(env, {}).get("release_id")
    if not rid:
        return False, f"{env} にまだ版が出ていない"
    record(cur, "release.kpi_requested", rid, {"repo": repo, "env": env, "week": week, "by": by, "via": via})
    return True, f"{env}（{rid}）で {week} の KPI を測る依頼をした"


def request_rollback(cur, repo: str, env: str, by: str, via: str) -> tuple[bool, str]:
    if env == "prod" and prod_held(cur, repo):
        return False, held_msg(cur, repo)   # 戻すのも本番を起動するので受け付けない
    st = status(cur, repo)
    cur_env = st["environments"].get(env)
    if not cur_env or not cur_env.get("previous"):
        return False, "戻す先の版がない"
    if cur_env.get("rollback_blocked"):
        return False, cur_env["rollback_blocked"]
    record(cur, "release.rollback_requested", cur_env["release_id"], {"repo": repo, "env": env, "by": by, "via": via})
    return True, f"{env} を {cur_env['previous']} に戻す依頼をした"


def contracts_of(cur, repo: str) -> dict[str, list[str]]:
    """Release ごとに、前の Release から足された contract のマイグレーション（release.built に記録）"""
    cur.execute("select payload->>'release_id' as rid, payload->'contract_migrations' as c from events "
                "where name='release.built' and payload->>'repo'=%s", (repo,))
    return {r["rid"]: list(r["c"] or []) for r in cur.fetchall()}


def status(cur, repo: str) -> dict:
    """環境ごとのいまの版と、その前の版（出した順。Rollback で戻った版も出した版として数える）。
    contract のマイグレーションは DB から消えないので、contract を含む版を出したら、その直前に出ていた版より前には戻せない
    （直前の版は消した列を使わない版であることが、contract を出す条件。ADR 0012 の 6、製品の ADR 0004）"""
    cur.execute("select payload->>'release_id' as rid from events where name='release.created' and payload->>'repo'=%s order by id", (repo,))
    rank = {r["rid"]: i for i, r in enumerate(cur.fetchall())}
    contracts = contracts_of(cur, repo)
    cur.execute("select name, payload, occurred_at from events where name in ('release.deployed','release.rolled_back') "
                "and payload->>'repo'=%s and (payload->>'ok')::boolean order by id", (repo,))
    history: dict[str, list[str]] = {}
    floor: dict[str, str] = {}
    for r in cur.fetchall():
        p = r["payload"]
        h = history.setdefault(p["env"], [])
        if r["name"] == "release.rolled_back":
            while h and h[-1] != p["release_id"]:
                h.pop()            # 戻した版より後に出した版は履歴から外す（もう一度戻すと、さらに前へ）
        else:
            if contracts.get(p["release_id"]) and h:
                f = floor.get(p["env"])
                if f is None or rank.get(h[-1], -1) > rank.get(f, -1):
                    floor[p["env"]] = h[-1]
            h.append(p["release_id"])
    envs = {}
    for env, h in history.items():
        prev = h[-2] if len(h) > 1 else None
        envs[env] = {"release_id": h[-1] if h else None, "previous": prev, "rollback_floor": floor.get(env)}
        if prev and env in floor and rank.get(prev, -1) < rank.get(floor[env], -1):
            envs[env]["rollback_blocked"] = (f"{prev} には戻せない（DB に contract のマイグレーションが当たっていて、"
                                             f"戻せるのは {floor[env]} まで）")
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
        token = ghapp.token(repo) if ghapp.available() else ""
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


def touch_config(cfg: dict, env: str) -> dict | None:
    """ship.yaml の touch（触れる環境）。その環境が入っていなければ None"""
    t = cfg.get("touch") or {}
    return t if env in (t.get("environments") or []) else None


def kpi_config(cfg: dict, env: str) -> dict | None:
    """ship.yaml の kpi（製品の KPI の測り方。ADR 0043 の O2）。その環境が入っていなければ None"""
    k = cfg.get("kpi") or {}
    return k if env in (k.get("environments") or []) and k.get("service") and k.get("command") else None


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def kpi_metrics(k: dict, values: dict) -> list[dict]:
    """ship.yaml の kpi.metrics の並びに、コマンドが返した値を当てる。形の誤った項目は落とし、値が数でなければ None（不明）"""
    out = []
    for m in (k.get("metrics") or [])[:KPI_MAX]:
        mid = str((m or {}).get("id") or "")
        if not KPI_ID_RE.match(mid):
            continue
        exp = m.get("expected")
        exp = [_num(exp[0]), _num(exp[1])] if isinstance(exp, list) and len(exp) == 2 else None
        out.append({"id": mid, "ja": str(m.get("ja") or mid)[:80], "unit": str(m.get("unit") or "")[:20],
                    "direction": m.get("direction") if m.get("direction") in ("up_is_good", "down_is_good") else "up_is_good",
                    "expected": exp, "change_alert": _num(m.get("change_alert")), "value": _num((values or {}).get(mid))})
    return out


def measure_kpi(cfg: dict, repo: str, env: str) -> list[dict]:
    k = kpi_config(cfg, env)
    if not k:
        raise RuntimeError(f"ship.yaml の kpi に {env} の測り方がない")
    out = sh(["docker", "compose", "-p", project(repo, env), "exec", "-T", k["service"], *k["command"]], timeout=120)
    values = json.loads(out.strip() or "{}")
    if not isinstance(values, dict):
        raise RuntimeError("kpi のコマンドの出力が {id: 数} の JSON ではない")
    return kpi_metrics(k, values)


def compose_env(cfg: dict, repo: str, env: str, rel: dict) -> dict:
    e = {str(k): str(v) for k, v in (cfg["environments"][env].get("env") or {}).items()}
    for part, spec in cfg["images"].items():
        e[spec["env"]] = image(repo, part, rel["app_version"])
    e.update({"COMPOSE_PROJECT_NAME": project(repo, env), "APP_VERSION": rel["app_version"], "RELEASE_ID": rel["release_id"]})
    t = touch_config(cfg, env)
    if t and t.get("bind_env") and TOUCH_BIND and HOST_RE.match(TOUCH_BIND):
        e[str(t["bind_env"])] = TOUCH_BIND   # 触れる環境の画面だけ、人の端末から届く待ち受けにする（prod には渡さない）
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


def added_contracts(cur, path: str, repo: str, commit: str) -> list[str]:
    """前に作った Release のコミットからこのコミットまでに足された contract のマイグレーション（ファイル名に _contract_）。
    前の Release がなければ空（まだどの環境の DB にも当たっていない）"""
    cur.execute("select payload->>'commit' as c from events where name='release.built' and payload->>'repo'=%s order by id desc limit 1", (repo,))
    r = cur.fetchone()
    if not r or not r["c"]:
        return []
    out = sh(["git", "-C", path, "diff", "--name-only", "--diff-filter=A", f"{r['c']}..{commit}"])
    return sorted(f for f in out.splitlines() if "/migrations/" in f and "_contract_" in f.rsplit("/", 1)[-1] and f.endswith(".sql"))


def seed_envs(cfg: dict) -> list[str]:
    return list((cfg.get("seed") or {}).get("environments") or [])


def seed(cur, cfg: dict, repo: str, env: str, rel: dict, common: dict):
    """合成シードを書く（ship.yaml の seed のコマンド）"""
    sd = cfg["seed"]
    t0 = time.monotonic()
    sh(["docker", "compose", "-p", project(repo, env), "exec", "-T", sd["service"], *sd["command"]], timeout=900)
    record(cur, "release.seeded", rel["release_id"], {**common, "env": env, "seconds": round(time.monotonic() - t0, 1),
                                                      "command": sd["command"]})


def seed_once(cur, cfg: dict, repo: str, env: str, rel: dict, common: dict):
    """ship.yaml の seed の環境なら、その環境に合成シードを 1 回だけ書く（DB は環境ごとのボリュームに残る）"""
    if env not in seed_envs(cfg):
        return
    cur.execute("select 1 from events where name='release.seeded' and payload->>'repo'=%s and payload->>'env'=%s limit 1", (repo, env))
    if cur.fetchone():
        return
    seed(cur, cfg, repo, env, rel, common)


def reseed(cur, cfg: dict, path: str, repo: str, env: str, rel: dict, common: dict) -> float:
    """触れる環境の DB を空にし（ship.yaml の touch.reset）、アプリを作り直してから合成シードを書く。
    作り直すのは、DB を消したあとに古い接続やメモリの状態を持ち越さないため（起動でマイグレーションも当たる）"""
    t = touch_config(cfg, env)
    if not t or not t.get("reset") or env not in seed_envs(cfg):
        raise RuntimeError(f"{env} は入れ直せる環境ではない（ship.yaml の touch.environments・touch.reset・seed.environments）")
    t0 = time.monotonic()
    sh(["docker", "compose", "-p", project(repo, env), "exec", "-T", t["reset"]["service"], *t["reset"]["command"]], timeout=300)
    compose = ["docker", "compose", "-f", os.path.join(path, cfg["compose"])]
    e = compose_env(cfg, repo, env, rel)
    sh(compose + ["up", "-d", "--wait", "--force-recreate", "--no-deps", cfg["verify"]["service"]], env=e, timeout=600)
    align(compose, e)
    verify(cfg, repo, env, rel)
    seed(cur, cfg, repo, env, rel, common)
    return round(time.monotonic() - t0, 1)


def restart(cfg: dict, path: str, repo: str, env: str, rel: dict, offset: int) -> tuple[float, list[str]]:
    """触れる環境の部品（touch.restart、既定は確かめる部品）を作り直して再起動する。DB は残す。
    時計の変数（touch.clock_env）に offset 秒を渡す。作り直すのは、compose の変数をコンテナに入れ直すため"""
    t = touch_config(cfg, env)
    if not t:
        raise RuntimeError(f"{env} は触れる環境ではない（ship.yaml の touch.environments）")
    if offset and not t.get("clock_env"):
        raise RuntimeError("この製品はサーバーの時計を進められない（ship.yaml の touch.clock_env がない）")
    services = [str(s) for s in (t.get("restart") or [cfg["verify"]["service"]])]
    e = compose_env(cfg, repo, env, rel)
    if t.get("clock_env"):
        e[str(t["clock_env"])] = str(int(offset))
    t0 = time.monotonic()
    compose = ["docker", "compose", "-f", os.path.join(path, cfg["compose"])]
    sh(compose + ["up", "-d", "--wait", "--force-recreate", "--no-deps", *services], env=e, timeout=600)
    align(compose, e)
    verify(cfg, repo, env, rel)
    return round(time.monotonic() - t0, 1), services


def align(compose: list[str], e: dict):
    """残りの部品を、いまの compose の変数にそろえる（#305）。docker compose up は、変数が変わって設定が前と違う部品だけを作り直し、
    同じ部品はそのまま残す。入れ直し・再起動は決まった部品しか作り直さないので、これがないと待ち受け（touch.bind_env、
    工場の SHIP_TOUCH_BIND）を変えたあとも画面の部品が前の待ち受けのまま残り、入口の記録（新しい SHIP_TOUCH_HOST）と食い違う"""
    sh(compose + ["up", "-d", "--wait"], env=e, timeout=600)


def touch_points(cur, cfg: dict, repo: str, env: str, rel: dict, common: dict):
    """触れる環境なら、入口（URL）とログインの手がかりを release.touchable に残す。失敗しても出荷は失敗にしない（手がかりが欠けるだけ）"""
    t = touch_config(cfg, env)
    if not t:
        return
    host = TOUCH_HOST if TOUCH_HOST and HOST_RE.match(TOUCH_HOST) else "127.0.0.1"
    values = (cfg["environments"][env].get("env") or {})
    entries = []
    for x in t.get("entries") or []:
        port = values.get(x.get("port_env"))
        if port is None:
            continue
        path = str(x.get("path") or "/")
        entries.append({"label": str(x.get("label") or x.get("port_env")), "url": f"http://{host}:{int(port)}{path}",
                        "qr": bool(x.get("qr"))})
    accounts, note = [], None
    if t.get("accounts"):
        try:
            out = sh(["docker", "compose", "-p", project(repo, env), "exec", "-T", t["accounts"]["service"], *t["accounts"]["command"]],
                     timeout=60)
            accounts = [{"label": str(a.get("label")), "value": str(a.get("value"))} for a in json.loads(out.strip() or "[]")][:20]
        except Exception as e:  # noqa: BLE001
            note = f"ログインの手がかりを読めなかった: {str(e)[-300:]}"
    record(cur, "release.touchable", rel["release_id"], {
        "repo": repo, "env": env, "host": host, "bind": TOUCH_BIND if t.get("bind_env") and TOUCH_BIND and HOST_RE.match(TOUCH_BIND) else None,
        "reachable_from_phone": bool(TOUCH_BIND and TOUCH_BIND != "127.0.0.1" and host != "127.0.0.1"),
        "entries": entries, "accounts": accounts, "note": note, "request_id": common.get("request_id")})


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
                                               "contract_migrations": added_contracts(cur, path, repo, commit),
                                               "images": [image(repo, part, rel["app_version"]) for part in cfg["images"]]})
            secs = deploy(cfg, path, repo, "staging", rel)
            record(cur, "release.deployed", rid, {**common, "env": "staging", "ok": True, "seconds": secs, "commit": commit})
            seed_once(cur, cfg, repo, "staging", rel, common)
            touch_points(cur, cfg, repo, "staging", rel, common)
        elif (name == "release.approved" or (name == "release.rollback_requested" and p.get("env") == "prod")) and prod_held(cur, repo):
            # 受け付けた後で止めた・直接書かれた承認も、本番は起動しない（停止には開かない。止めているのは意図どおり）
            record(cur, "release.ship_failed", rid, {**common, "request": name, "error": held_msg(cur, repo), "held": True,
                                               "stage": stage_of(cur, repo)["stage"]})
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
            cfg = ship_config(path)
            deploy(cfg, path, repo, env, target)
            secs = round((now() - ev["occurred_at"]).total_seconds(), 1)   # 押してから確かめ終わるまで
            record(cur, "release.rolled_back", target_id, {**common, "env": env, "ok": True, "from": rid, "seconds": secs,
                                                           "by": p.get("by"), "via": p.get("via")})
            touch_points(cur, cfg, repo, env, target, common)
        elif name == "release.reseed_requested":
            env = p["env"]
            if status(cur, repo)["environments"].get(env, {}).get("release_id") != rid:
                raise RuntimeError(f"{env} の版が依頼のあとで替わった（{rid} ではない）。もう一度依頼する")
            path, _ = source(repo, rel["app_version"])
            cfg = ship_config(path)
            secs = reseed(cur, cfg, path, repo, env, rel, common)
            record(cur, "release.reseeded", rid, {**common, "env": env, "ok": True, "seconds": secs, "by": p.get("by"), "via": p.get("via")})
            touch_points(cur, cfg, repo, env, rel, common)
        elif name == "release.restart_requested":
            env = p["env"]
            if status(cur, repo)["environments"].get(env, {}).get("release_id") != rid:
                raise RuntimeError(f"{env} の版が依頼のあとで替わった（{rid} ではない）。もう一度依頼する")
            offset = 0 if p.get("reset_clock") else clock_offset(cur, repo, env) + int(p.get("advance_seconds") or 0)
            path, _ = source(repo, rel["app_version"])
            cfg = ship_config(path)
            secs, services = restart(cfg, path, repo, env, rel, offset)
            record(cur, "release.restarted", rid, {**common, "env": env, "ok": True, "seconds": secs, "services": services,
                                                   "advance_seconds": int(p.get("advance_seconds") or 0), "clock_offset_seconds": offset,
                                                   "by": p.get("by"), "via": p.get("via")})
            touch_points(cur, cfg, repo, env, rel, common)
        elif name == "release.kpi_requested":
            env = p["env"]
            try:
                path, _ = source(repo, rel["app_version"])
                metrics, err = measure_kpi(ship_config(path), repo, env), None
            except Exception as e:  # noqa: BLE001  測れなかったことを残すだけ（報告の欠けで出荷を止めない）
                metrics, err = [], str(e)[-800:]
            record(cur, "release.kpi_measured", rid, {**common, "env": env, "week": p.get("week"), "ok": err is None, "metrics": metrics,
                                                      "error": err, "by": p.get("by"), "via": p.get("via")})
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
