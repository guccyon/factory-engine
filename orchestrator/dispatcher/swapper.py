#!/usr/bin/env python3
"""更新役（ADR 0031 の決定 1・4・6・7）。更新のたびに dispatcher が使い捨てのコンテナ（factory-swap-<id>）で起動し、終われば消える。

  python swapper.py run <swap_id>

手順（どのステップも events の swap.* に残す）:
  1. 事前の確かめ（ラインは止めない）: 作業ツリーがきれい・target が main に入っている・早送りできる、差分から種類を判定し直す（申告の重い方）、
     人に回す条件（DB の変更・秘密情報と権限）、出荷の処理中でない、今のイメージを前の版の名前で残す、
     新しい版のイメージを依存の順に作る（新しい版の scripts/build.sh）、新しい compose.yaml が今の .env で通る、新しいイメージの中のコードが読める
  2. 一時停止（control_plane のときだけ）: 入れてよくなる（実行中 0・出荷の処理なし）まで待つ
  3. 更新: 作業ツリーを早送りし、docker compose up -d ですべてのサービスを上げる
  4. 確かめ: すべてのサービスが target の版で動き、ヘルスチェックが通り、dispatcher と supervisor が新しい版で回り、コンソールと窓口が応え、
     実行中だったジョブがつなぎ直されている
  5. 再開（一時停止したとき）。失敗したら前の版（作業ツリーと前の版の名前のイメージ）に戻して確かめ直す。戻しても通らなければ一時停止のまま人に回す

- DB の変更（postgres/init/）を含む更新は自律の範囲の外。事前の確かめで止め、受信トレイに出す（承認ボタンで流すのは ADR 0031 の順番 5）
- 子のプロセス（git・docker compose）には秘密情報を渡さない。compose は .env だけから値を取る（環境変数が .env より優先されるため）
- 更新役は compose のサービスではないので、dispatcher を作り直しても最後まで続く。制御プレーンの DB が上げ直しで切れたら繋ぎ直す
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

import drain
import inbox
import ship
import stoppage
import swap
import version

ENV = os.environ
ROOT = ENV.get("SWAP_ROOT", "").rstrip("/")
PROJECT = ENV.get("COMPOSE_PROJECT", "factory")
# 子のプロセスに渡さないもの。GH_TOKEN などが環境にあると、compose の ${GH_TOKEN} が .env より優先されてしまう
CHILD_ENV_DROP = ("GH_TOKEN", "GH_APP_ID", "GH_APP_PRIVATE_KEY_B64", "GH_APP_INSTALLATION_ID", "DATABASE_URL", "NOTIFIER", "STATUS_REPO", "STATUS_NUMBER", "ENGINE_VERSION")
GIT_ENV = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*",   # マウントした作業ツリーの持ち主がコンテナの外の人
           "GIT_TERMINAL_PROMPT": "0"}
CONSOLE_URL = ENV.get("SWAP_CONSOLE_URL", "http://console:8790/api/state")
DESK_URL = ENV.get("SWAP_DESK_URL", "http://desk:8792/api/desk/me")


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), "[swapper]", *a, flush=True)


class Stop(Exception):
    """事前の確かめで止める。escalate なら人に回す（決定 7）、そうでなければ失敗（何も変えていない）"""
    def __init__(self, reason: str, *, escalate: str | None = None, waiting_for: str = "", detail: dict | None = None):
        super().__init__(reason)
        self.reason, self.escalate, self.waiting_for, self.detail = reason, escalate, waiting_for, detail or {}


# ------------------------------------------------------------------ 外とのやりとり（テストで差し替える）
def child_env() -> dict:
    return {**{k: v for k, v in ENV.items() if k not in CHILD_ENV_DROP and not k.startswith("SWAP_")}, **GIT_ENV}


def run_cmd(args: list[str], cwd: str | None = None, timeout: int = 600, check: bool = True) -> subprocess.CompletedProcess:
    log("+", " ".join(args))
    r = subprocess.run(args, cwd=cwd, env=child_env(), capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])} が失敗した（{r.returncode}）: {(r.stderr or r.stdout).strip()[-600:]}")
    return r


def http_status(url: str) -> int:
    req = urllib.request.Request(url, headers={"Host": "localhost"})   # コンソールと窓口は開いてよいホスト名だけを受ける
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001  まだ上がっていない
        return 0


def connect():
    """制御プレーンの DB。postgres を上げ直している間は繋がらないので、待って繋ぎ直す"""
    until = time.time() + 180
    while True:
        try:
            conn = psycopg.connect(ENV["DATABASE_URL"], row_factory=dict_row, autocommit=True, connect_timeout=5)
            return conn
        except psycopg.OperationalError:
            if time.time() > until:
                raise
            time.sleep(3)


class Swapper:
    def __init__(self, sid: int, root: str = ROOT, project: str = PROJECT, run=run_cmd, http=http_status, list_running=version.list_running,
                 db=connect, sleep=time.sleep, clock=time.time):
        self.sid, self.root, self.project = sid, root, project
        self.orch = os.path.join(root, "orchestrator")
        self.run, self.http, self.list_running, self.db, self.sleep, self.clock = run, http, list_running, db, sleep, clock
        self.drained_by_me = False
        self.drained_at: float | None = None
        self.rules = swap.load_rules(root)

    # -------------------------------------------------------------- 記録
    def rec(self, name: str, payload: dict | None = None) -> None:
        with self.db() as conn, conn.cursor() as cur:
            swap.record(cur, self.sid, name, payload)
        log(name, json.dumps(payload or {}, ensure_ascii=False)[:300])

    def git(self, *args, check=True, timeout=300) -> str:
        return self.run(["git", *args], cwd=self.root, timeout=timeout, check=check).stdout.strip()

    def docker(self, *args, check=True, timeout=600) -> subprocess.CompletedProcess:
        return self.run(["docker", *args], cwd=self.orch, timeout=timeout, check=check)

    # -------------------------------------------------------------- 全体
    def main(self) -> int:
        with self.db() as conn, conn.cursor() as cur:
            s = swap.get(cur, self.sid)
        if not s or s["status"] != "launched":
            log(f"更新 {self.sid} は起動された状態ではない（{s and s['status']}）。何もしない")
            return 2
        self.req = s
        self.rec("swap.started", {"container": swap.container(self.sid), "root": self.root})
        try:
            plan = self.preflight()
        except Stop as e:
            self.restore_tags()
            return self.stop(e)
        except Exception as e:  # noqa: BLE001  事前の確かめの間は何も変えていない（イメージの名前は戻す）
            self.restore_tags()
            self.rec("swap.preflight_failed", {"error": str(e)[:600]})
            return 1
        try:
            if plan["kind"] != "code":
                self.drain_wait(plan)
        except Stop as e:
            self.restore_tags()
            self.release_drain()
            return self.stop(e)
        running_before = self.running_jobs()
        before = {r["name"]: r["version"] for r in self.list_running(self.project) if r["kind"] == "service"}
        t0 = datetime.now(timezone.utc)
        try:
            self.git("merge", "--ff-only", plan["target_sha"])
            self.compose_up()
            self.rec("swap.applied", {"target_sha": plan["short"], "prev": plan["prev_short"], "kind": plan["kind"]})
            ok, checks = self.verify(plan["short"], t0, running_before)
        except Exception as e:  # noqa: BLE001
            ok, checks = False, {"error": str(e)[:600]}
        if ok:
            self.rec("swap.verified", {"checks": checks})
            self.release_drain()
            self.prune_images(keep={plan["prev_short"], plan["short"]})
            self.rec("swap.completed", {"target_sha": plan["short"], "prev": plan["prev_short"], "kind": plan["kind"], "kind_declared": plan["declared"],
                                        "stopped_sec": self.stopped_sec(), "reattached": len(running_before)})
            return 0
        return self.rollback(plan, checks, before)

    # -------------------------------------------------------------- 1. 事前の確かめ
    def preflight(self) -> dict:
        if self.git("status", "--porcelain", "--untracked-files=no"):
            raise Stop("工場の作業ツリーに未コミットの変更がある（版のラベルと中身がずれるので更新しない）", escalate="human",
                       waiting_for="作業ツリーの変更を片付けてから依頼し直す")
        url = fetch_url(self.git("remote", "get-url", "origin"))
        self.git("fetch", "--quiet", url, "+refs/heads/main:refs/remotes/origin/main", timeout=300)
        ref = "origin/main" if self.req["target"] == "main" else self.req["target"]
        sha = self.git("rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
        if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
            raise Stop(f"{self.req['target']} が見つからない")
        if self.run(["git", "merge-base", "--is-ancestor", sha, "origin/main"], cwd=self.root, check=False).returncode != 0:
            raise Stop(f"{sha[:7]} は main に入っていない（main に入った版だけ更新する）")
        head = self.git("rev-parse", "HEAD")
        if self.run(["git", "merge-base", "--is-ancestor", head, sha], cwd=self.root, check=False).returncode != 0:
            raise Stop(f"今の版 {head[:7]} から {sha[:7]} へ早送りできない（作業ツリーが main から外れている）", escalate="human",
                       waiting_for="作業ツリーの版を確かめる（git log）")
        # 前の版は動いているイメージの版から取る（#237）。作業ツリーを先に進めてあると、HEAD から比べたのでは差分が空になり、
        # 人に回す判定を素通りし、戻し先も誤る
        prev = self.running_base(head)
        if prev != head and self.run(["git", "merge-base", "--is-ancestor", prev, sha], cwd=self.root, check=False).returncode != 0:
            raise Stop(f"動いている版 {prev[:7]} から {sha[:7]} へ早送りできない", escalate="human",
                       waiting_for="動いているサービスの版を確かめる（factoryctl version）")
        files = [f for f in self.git("diff", "--name-only", prev, sha).splitlines() if f]
        judged = swap.classify(files, self.rules)
        kind = swap.heavier(self.req["kind"] or "code", judged)
        plan = {"target_sha": sha, "short": sha[:7], "prev": prev, "prev_short": prev[:7], "kind": kind, "declared": self.req["kind"], "judged": judged,
                "files": len(files), **({"worktree_head": head[:7]} if head != prev else {})}
        log("plan", json.dumps(plan, ensure_ascii=False))
        if kind == "db":
            sql = [f for f in files if f.startswith("orchestrator/postgres/init/")]
            raise Stop("DB の変更を含む（自律の範囲の外。承認してから流す）", escalate="db", detail={**plan, "sql": sql})
        esc = swap.escalations(files, lambda f: self.git("diff", prev, sha, "--", f), self.rules)
        if esc:
            raise Stop("確認が要る変更を含む: " + "、".join(f"{e['why']}（{', '.join(e['files'][:3])}）" for e in esc), escalate="human",
                       waiting_for="差分を確かめ、手で更新する", detail={**plan, "escalations": esc})
        with self.db() as conn, conn.cursor() as cur:
            if ship.pending(cur):
                raise Stop("出荷の処理中（終わってから更新する）")
            if kind != "code" and self.req.get("source", "manual") != "manual" and unattended(cur):
                # Issue の郵便受けからの依頼で、差分が一時停止を要る種類だった。一時停止を伴う更新は人が並走する時間帯だけ（決めどころ S4）。
                # 手で打った swap run は、打った人が近くにいるので止めない
                raise Stop(f"一時停止を伴う更新（判定 {kind}）は、並走モードの時間帯（attended）だけ自動で始める。いまは全自動モードの時間帯",
                           detail={**plan, "deferred": "unattended"})
        self.snapshot_tags(prev[:7])
        self.build(sha)
        return plan

    def running_base(self, head: str) -> str:
        """動いているサービス（工場が作るイメージ）の版のうち、いちばん古いコミット（重い側に倒す）。
        版が読めない（unknown・作業ツリーに無い）ときは作業ツリーの HEAD"""
        vers = sorted({r["version"] for r in self.list_running(self.project)
                       if r["kind"] == "service" and not r["external"] and r["version"] not in (version.UNKNOWN, "-")})
        shas = []
        for v in vers:
            s = self.git("rev-parse", "--verify", "--quiet", f"{v}^{{commit}}", check=False)
            if re.fullmatch(r"[0-9a-f]{40}", s or "") and s not in shas:
                shas.append(s)
        if not shas:
            return head
        anc = lambda a, b: a == b or self.run(["git", "merge-base", "--is-ancestor", a, b], cwd=self.root, check=False).returncode == 0
        oldest = next((c for c in shas if all(anc(c, o) for o in shas)), None)
        if oldest is None:
            base = self.git("merge-base", "--octopus", *shas, check=False)
            oldest = base if re.fullmatch(r"[0-9a-f]{40}", base or "") else head
        return oldest

    def build(self, sha: str) -> None:
        """新しい版のイメージを、新しい版の scripts/build.sh で依存の順に作る。作業ツリーとは別の場所に取り出して作る（今の工場はそのまま動く）"""
        src = f"/tmp/swap-src-{self.sid}"
        self.git("worktree", "add", "--force", "--detach", src, sha)
        try:
            env_file = os.path.join(self.orch, ".env")
            args = ["docker", "compose", "-p", self.project, "--project-directory", self.orch, "-f", f"{src}/orchestrator/compose.yaml"]
            if os.path.exists(env_file):
                args += ["--env-file", env_file]
            r = self.run(args + ["config", "-q"], cwd=self.orch, check=False, timeout=60)
            if r.returncode != 0:
                raise Stop("新しい版の compose.yaml が今の .env で通らない（足りない環境変数は手で入れる）", escalate="human",
                           waiting_for=".env に値を足してから依頼し直す", detail={"error": (r.stderr or r.stdout).strip()[-400:]})
            self.run(["bash", "scripts/build.sh"], cwd=f"{src}/orchestrator", timeout=5400)
            self.docker("run", "--rm", "--entrypoint", "python", f"factory-dispatcher:p0-{sha[:7]}", "-m", "compileall", "-q", "/app", timeout=120)
        finally:
            # 自分で作った一時の作業ツリーだけを外す。prune は使わない（このコンテナからはホストの他の worktree が見えず、
            # git がそれらを消えたとみなして登録を消してしまう）
            self.git("worktree", "remove", "--force", src, check=False)

    # -------------------------------------------------------------- イメージの名前（前の版に戻すときに作り直さない）
    def image_names(self) -> list[str]:
        """今の版の build.sh が作るイメージ（名前:タグ）。作る順と同じ"""
        try:
            with open(os.path.join(self.orch, "scripts/build.sh")) as f:
                return re.findall(r'^\s*"(factory-[\w-]+:[\w.-]+)\|', f.read(), re.M)
        except FileNotFoundError:
            return []

    def snapshot_tags(self, prev: str) -> None:
        """今のイメージに前の版の名前（<名前>:<タグ>-<コミット>）を付けておく。動いているサービスのイメージがあればそれを、なければ今の名前のものを"""
        running = {}
        for r in self.list_running(self.project):
            if r["kind"] == "service" and not r["external"] and r.get("image_id"):
                running.setdefault(r["image"], r["image_id"])
        for name in self.image_names():
            snap = f"{name}-{prev}"
            if self.docker("image", "inspect", snap, check=False).returncode == 0:
                continue
            for src in dict.fromkeys([running.get(name, name), name]):
                if self.docker("image", "inspect", src, check=False).returncode == 0:
                    self.docker("tag", src, snap)
                    break
        self.prev_short = prev

    def restore_tags(self) -> None:
        """今の名前を前の版のイメージに戻す（作った新しい版を、次に起動するコンテナが使わないように）"""
        prev = getattr(self, "prev_short", None)
        if not prev:
            return
        for name in self.image_names():
            snap = f"{name}-{prev}"
            if self.docker("image", "inspect", snap, check=False).returncode == 0:
                self.docker("tag", snap, name, check=False)

    def prune_images(self, keep: set[str]) -> None:
        """前の版の名前は、次に成功した更新まで残す（決定 6）。成功したら、今と 1 つ前より古いものを外す（名前を外すだけ。使っていなければ消える）"""
        names = self.image_names()
        out = self.docker("images", "--format", "{{.Repository}}:{{.Tag}}", check=False).stdout.split()
        for ref in out:
            for name in names:
                m = re.fullmatch(re.escape(name) + r"-([0-9a-f]{7})", ref)
                if m and m.group(1) not in keep:
                    self.docker("rmi", ref, check=False)

    # -------------------------------------------------------------- 2. 一時停止
    def drain_wait(self, plan: dict) -> None:
        wait = int(self.rules.get("drain_wait_sec") or 2700)
        with self.db() as conn, conn.cursor() as cur:
            self.drained_by_me = drain.set_drain(cur, True, "swapper", f"更新 {self.sid}（{plan['short']}、{plan['kind']}）")
        self.drained_at = self.clock()
        self.rec("swap.drained", {"by_me": self.drained_by_me})
        until = self.clock() + wait
        while True:
            with self.db() as conn, conn.cursor() as cur:
                st = drain.status(cur)
            if st["ready"]:
                return
            if self.clock() > until:
                raise Stop(f"{wait // 60} 分待っても更新してよい状態にならない（実行中 {len(st['running'])} 件・出荷 {len(st['shipping'])} 件・一時停止でも流れる順番待ち {len(st.get('queued') or [])} 件）",
                           escalate="human", waiting_for="実行中のジョブを確かめてから依頼し直す")
            self.sleep(10)

    def release_drain(self) -> None:
        if not self.drained_by_me:
            return
        with self.db() as conn, conn.cursor() as cur:
            drain.set_drain(cur, False, "swapper", f"更新 {self.sid} が終わった")
        self.drained_by_me = False

    def stopped_sec(self) -> int | None:
        return int(self.clock() - self.drained_at) if self.drained_at else None

    # -------------------------------------------------------------- 3. 更新
    def compose_up(self) -> None:
        """すべてのサービスを上げる（一部だけ上げない。2026-11-05、dispatcher が古いまま残った）。イメージは作らない（作ってある）"""
        self.docker("compose", "-p", self.project, "up", "-d", "--no-build", "--remove-orphans", timeout=600)

    def running_jobs(self) -> list[int]:
        with self.db() as conn, conn.cursor() as cur:
            cur.execute("select id from jobs where state='running' order by id")
            return [r["id"] for r in cur.fetchall()]

    # -------------------------------------------------------------- 4. 確かめ
    def checks(self, expect, since: datetime, running_before: list[int]) -> dict:
        """expect はコミット（新しい版）か、サービスの名前 → 版（前の版に戻したとき）"""
        rows = self.list_running(self.project)
        services = [r for r in rows if r["kind"] == "service"]
        if isinstance(expect, str):
            gaps = [r["name"] for r in version.mismatched(rows, expect)]
        else:
            gaps = [r["name"] for r in services if r["state"] != "running" or (not r["external"] and r["name"] in expect and r["version"] != expect[r["name"]])]
        out = {"versions": not gaps and bool(services), "version_gaps": gaps,
               "health": all(r.get("health") in (None, "", "healthy") for r in services),
               "console": self.http(CONSOLE_URL) == 200, "desk": 0 < self.http(DESK_URL) < 500}
        ver = expect if isinstance(expect, str) else None
        with self.db() as conn, conn.cursor() as cur:
            out["supervisor"] = loop_seen(cur, "supervisor.heartbeat", since, ver)
            out["dispatcher"] = loop_seen(cur, "dispatcher.started", since, ver) if ver else True   # 前の版の dispatcher は印を出さないことがある
            out["reattached"] = reattached(cur, running_before, since)
        return out

    def verify(self, expect, since: datetime, running_before: list[int]) -> tuple[bool, dict]:
        v = self.rules.get("verify") or {}
        until, interval = self.clock() + int(v.get("timeout_sec", 300)), int(v.get("interval_sec", 10))
        while True:
            c = self.checks(expect, since, running_before)
            if all(val for k, val in c.items() if k != "version_gaps"):
                return True, c
            if self.clock() > until:
                return False, c
            self.sleep(interval)

    # -------------------------------------------------------------- 5. 前の版に戻す
    def rollback(self, plan: dict, checks: dict, before: dict) -> int:
        failed = [k for k, v in checks.items() if not v and k != "version_gaps"]
        reason = "確かめに通らない: " + "・".join(failed) + (f"（版のずれ {', '.join(checks.get('version_gaps') or [])}）" if checks.get("version_gaps") else "")
        log("rollback", reason)
        t1 = datetime.now(timezone.utc)
        try:
            self.git("reset", "--keep", plan["prev"])
            self.restore_tags()
            self.compose_up()
            ok, back = self.verify(before, t1, [])
        except Exception as e:  # noqa: BLE001
            ok, back = False, {"error": str(e)[:600]}
        if ok:
            self.release_drain()
            self.rec("swap.rolled_back", {"target_sha": plan["short"], "prev": plan["prev_short"], "reason": reason, "checks": checks,
                                          "stopped_sec": self.stopped_sec()})
            with self.db() as conn, conn.cursor() as cur:
                inbox.request(cur, f"swap:{self.sid}:rolled_back", "swap_rolled_back", "builder",
                              f"工場の更新 {self.sid}（{plan['short']}）が確かめに通らず、前の版 {plan['prev_short']} に戻した",
                              action=f"{reason}。直した版で依頼し直す（同じ版では自動で更新しない）", correlation_id=swap.corr(self.sid), by="swapper")
            return 1
        with self.db() as conn, conn.cursor() as cur:
            drain.set_drain(cur, True, "swapper", f"更新 {self.sid} の戻しも確かめに通らない")   # 壊れた工場では流さない
        return self.stop(Stop(f"新しい版が {reason}。前の版 {plan['prev_short']} に戻しても通らない", escalate="human",
                              waiting_for="factoryctl version で版を見て、手で上げ直す。直ったら factoryctl resume",
                              detail={"checks": checks, "rollback_checks": back}))

    # -------------------------------------------------------------- 人に回す・止める
    def stop(self, e: Stop) -> int:
        if not e.escalate:
            self.rec("swap.preflight_failed", {"error": e.reason, **e.detail})
            return 1
        self.rec("swap.escalated", {"reason": e.reason, "category": e.escalate, **e.detail})
        title = f"工場の更新 {self.sid}（{self.req['target']}）: {e.reason}"
        with self.db() as conn, conn.cursor() as cur:
            if e.escalate == "db":
                d = e.detail
                inbox.request(cur, f"swap:{self.sid}:db", "swap_db", "owner", f"DB の変更を含む工場の更新（{d['prev_short']} → {d['short']}）",
                              url=compare_url(self.git("remote", "get-url", "origin", check=False), d["prev_short"], d["short"]),
                              action=f"流す SQL（{', '.join(os.path.basename(f) for f in d['sql'])}）を確かめる。承認ボタンができるまで（ADR 0031 の順番 5）は、"
                                     "一時停止して SQL を手で流し、factoryctl swap run で更新し直す",
                              correlation_id=swap.corr(self.sid), by="swapper")
            else:
                stoppage.open_stoppage(cur, f"swap:{self.sid}", "human", title[:300], waiting_for=e.waiting_for, cause=e.reason[:300],
                                       detected_by="swapper", correlation_id=swap.corr(self.sid))
        return 3


# ------------------------------------------------------------------ 小さな関数（テストで直接見る）
def unattended(cur) -> bool:
    import judgment
    return judgment.current_mode(cur, ENV.get("FACTORY_MODE") or "attended", ENV.get("MODE_SCHEDULE", ""))[0] == "unattended"


def fetch_url(remote: str) -> str:
    """ssh の remote（git@github.com:o/r.git）は https に直す（更新役のコンテナに ssh の鍵はない。工場のリポジトリは公開）"""
    m = re.fullmatch(r"(?:ssh://)?git@github\.com[:/](.+?)(?:\.git)?/?", remote.strip())
    return f"https://github.com/{m.group(1)}.git" if m else remote.strip()


def compare_url(remote: str, a: str, b: str) -> str:
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?/?$", (remote or "").strip())
    return f"https://github.com/{m.group(1)}/compare/{a}...{b}" if m else ""


def loop_seen(cur, name: str, since: datetime, ver: str | None) -> bool:
    """since より後にその印が出たか。ver を渡せば、印の engine_version がその版のものだけ"""
    cur.execute("select payload->>'engine_version' as v from events where name=%s and occurred_at > %s order by id desc limit 20", (name, since))
    vs = [r["v"] for r in cur.fetchall()]
    if ver is None:
        return bool(vs)
    return any(v and (v.startswith(ver) or ver.startswith(v)) and len(v) >= 7 for v in vs)


def reattached(cur, jobs: list[int], since: datetime) -> bool:
    """更新の前に動いていたジョブが、つなぎ直されたか、終わったか（ADR 0020）"""
    if not jobs:
        return True
    cur.execute("select j.id from jobs j where j.id = any(%s) and j.state = 'running' and not exists "
                "(select 1 from events e where e.job_id = j.id and e.name = 'job.reattached' and e.occurred_at > %s)", (jobs, since))
    return not cur.fetchall()


def main(argv) -> int:
    if len(argv) < 3 or argv[1] != "run" or not argv[2].isdigit():
        print(__doc__)
        return 2
    if not ROOT:
        print("SWAP_ROOT がない（dispatcher が起動するときに渡す）")
        return 2
    return Swapper(int(argv[2])).main()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
