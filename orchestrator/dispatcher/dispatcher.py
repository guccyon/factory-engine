#!/usr/bin/env python3
"""工場の dispatcher（P0 の制御プレーン。ADR 0003）。

- poll:   対象リポジトリの TRIGGER_LABEL が付いた Issue を見つけてジョブに積む（30 秒ごと）
- webhook: WEBHOOK_SECRET があれば /webhook で issues.labeled を受けて同じことをする
- run:    キューから claim（SKIP LOCKED）→ アドバイザリロック → ワーカーコンテナを docker run --rm → 結果を記録

状態は postgres とオブジェクトストアにだけ置く。このプロセスは落ちても再起動すれば続きから動く。
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from minio import Minio
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ENV = os.environ
DATABASE_URL = ENV["DATABASE_URL"]
REPOS = [r.strip() for r in ENV.get("FACTORY_REPOS", "").split(",") if r.strip()]
TRIGGER_LABEL = ENV.get("TRIGGER_LABEL", "factory:implement")
LABELS = {"queued": "factory:queued", "running": "factory:running", "pr_open": "factory:pr-open", "failed": "factory:failed"}
POLL_INTERVAL = int(ENV.get("POLL_INTERVAL_SEC", "30"))
MAX_PARALLEL = int(ENV.get("MAX_PARALLEL", "2"))
JOB_TIMEOUT = int(ENV.get("JOB_TIMEOUT_SEC", "2400"))
TOKEN_BUDGET = int(ENV.get("TOKEN_BUDGET_PER_JOB", "2000000"))
WORKER_IMAGE = ENV.get("WORKER_IMAGE", "factory-worker:p0")
# リポジトリごとのワーカーイメージ（例: "guccyon/rally=factory-worker:rust,owner/other=factory-worker:p0"）。未指定は WORKER_IMAGE
WORKER_IMAGES = dict(kv.split("=", 1) for kv in ENV.get("WORKER_IMAGES", "").split(",") if "=" in kv)
MODEL_TIER = ENV.get("MODEL_TIER", "tier-large")
GH_TOKEN = ENV["GH_TOKEN"]
BUCKET = ENV.get("OBJECTSTORE_BUCKET", "factory-artifacts")
# ワーカーに渡すジョブ用 DB（制御プレーンと同じ Postgres サーバーに job_<id> を作る。ワーカーは自前で Postgres を立てずに済む）
JOB_DB_ENABLED = ENV.get("JOB_DB_ENABLED", "1") == "1"
JOB_DB_HOST = ENV.get("JOB_DB_HOST", "postgres")          # ワーカーコンテナから見たホスト名（compose のネットワーク上）
JOB_DB_NETWORK = ENV.get("JOB_DB_NETWORK", "factory_default")
JOB_DB_USER, JOB_DB_PASSWORD = "factory", ENV.get("POSTGRES_PASSWORD", "factory")

# S3 互換のオブジェクトストア（クライアントは minio SDK。サーバーは versitygw でも MinIO でもよい）
store = Minio(ENV["OBJECTSTORE_ENDPOINT"], access_key=ENV["OBJECTSTORE_ACCESS_KEY"], secret_key=ENV["OBJECTSTORE_SECRET_KEY"], secure=False)


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)


# ------------------------------------------------------------------ GitHub（REST、標準ライブラリのみ）
def gh(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(f"https://api.github.com/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json", "User-Agent": "factory-dispatcher"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        return json.loads(data) if data else None


def swap_label(repo: str, issue: int, remove: list[str], add: list[str]):
    if ENV.get("WORKER_DRY_RUN"):   # 試験運転では GitHub に触らない
        return
    for l in remove:
        try:
            gh("DELETE", f"repos/{repo}/issues/{issue}/labels/{urllib.parse.quote(l, safe='')}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    if add:
        gh("POST", f"repos/{repo}/issues/{issue}/labels", {"labels": add})


def image_for(repo: str, issue: int) -> str:
    """ワーカーのイメージを決める。Issue に `factory:image:<名前>` のラベルがあれば factory-worker:<名前>、
    なければリポジトリごとの WORKER_IMAGES、それもなければ WORKER_IMAGE。名前は英小文字・数字・ハイフンだけ許す"""
    if not ENV.get("WORKER_DRY_RUN"):
        try:
            for l in gh("GET", f"repos/{repo}/issues/{issue}/labels") or []:
                name = l.get("name", "")
                if name.startswith("factory:image:"):
                    tag = name.split(":", 2)[2]
                    if tag and all(c.isalnum() or c == "-" for c in tag) and tag.islower():
                        return f"factory-worker:{tag}"
        except Exception as e:  # noqa: BLE001
            log("image label lookup failed", repo, issue, repr(e))
    return WORKER_IMAGES.get(repo, WORKER_IMAGE)


def comment(repo: str, issue: int, text: str):
    if ENV.get("WORKER_DRY_RUN"):
        return
    gh("POST", f"repos/{repo}/issues/{issue}/comments", {"body": text})


# ------------------------------------------------------------------ キュー（薄いワークフロー I/F：enqueue / claim / finish / cancel）
def record_event(cur, name: str, correlation_id: str | None, job_id: int | None, payload: dict, dedupe_key: str | None = None):
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values (%s,%s,%s,%s,%s) on conflict (dedupe_key) do nothing",
                (name, correlation_id, job_id, Jsonb(payload), dedupe_key))


def enqueue(repo: str, issue: int, by: str, attempt: int = 1) -> int | None:
    corr = f"github:{repo}#{issue}"
    with db() as conn, conn.cursor() as cur:
        try:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier) values (%s,%s,%s,%s,%s,%s) returning id",
                        (corr, repo, issue, attempt, by, MODEL_TIER))
        except psycopg.errors.UniqueViolation:
            return None   # すでに queued / running（二重実行の抑止 1 段目）
        job_id = cur.fetchone()["id"]
        record_event(cur, "job.enqueued", corr, job_id, {"repo": repo, "issue": issue, "by": by, "attempt": attempt})
    return job_id


def poll_once():
    for repo in REPOS:
        try:
            issues = gh("GET", f"repos/{repo}/issues?state=open&labels={urllib.parse.quote(TRIGGER_LABEL)}&per_page=50")
        except Exception as e:  # noqa: BLE001
            log("poll error", repo, e)
            continue
        for it in issues or []:
            if "pull_request" in it:
                continue
            n = it["number"]
            job_id = enqueue(repo, n, "poll")
            if job_id:
                log(f"enqueued job {job_id} for {repo}#{n}")
                swap_label(repo, n, [TRIGGER_LABEL], [LABELS["queued"]])
                comment(repo, n, f"工場がジョブ {job_id} として受け付けました（attempt 1）。PR 作成まで自動で進めます。")
            else:
                # 既に active なジョブがある。ラベルだけ直す
                swap_label(repo, n, [TRIGGER_LABEL], [])


def poll_loop():
    while True:
        if not ENV.get("WORKER_DRY_RUN"):
            poll_once()
        time.sleep(POLL_INTERVAL)


# ------------------------------------------------------------------ ワーカーの実行
def run_job(job: dict):
    job_id, repo, issue, corr = job["id"], job["repo"], job["issue_number"], job["correlation_id"]
    lock_key = f"{repo}#{issue}"
    with db() as conn, conn.cursor() as cur:
        cur.execute("select pg_try_advisory_lock(hashtext(%s)) as ok", (lock_key,))
        if not cur.fetchone()["ok"]:   # 二重実行の抑止 2 段目
            cur.execute("update jobs set state='failed', finished_at=now(), error='lock_held' where id=%s", (job_id,))
            record_event(cur, "job.rejected", corr, job_id, {"reason": "advisory_lock_held"})
            return
        try:
            swap_label(repo, issue, [LABELS["queued"]], [LABELS["running"]])
            image = image_for(repo, issue)
            record_event(cur, "job.started", corr, job_id, {"attempt": job["attempt"], "model_tier": job["model_tier"], "image": image})
            name = f"factory-job-{job_id}"
            job_db_url = None
            if JOB_DB_ENABLED:
                dbname = f"job_{job_id}"
                cur.execute(f'drop database if exists "{dbname}"')
                cur.execute(f'create database "{dbname}"')
                job_db_url = f"postgres://{JOB_DB_USER}:{JOB_DB_PASSWORD}@{JOB_DB_HOST}:5432/{dbname}"
            cmd = ["docker", "run", "--rm", "--name", name,
                   *(["--network", JOB_DB_NETWORK, "-e", f"DATABASE_URL={job_db_url}"] if job_db_url else []),
                   "-e", f"JOB_ID={job_id}", "-e", f"REPO={repo}", "-e", f"ISSUE_NUMBER={issue}",
                   "-e", f"WORKER_DRY_RUN={ENV.get('WORKER_DRY_RUN', '')}",
                   "-e", f"MODEL_TIER={job['model_tier'] or MODEL_TIER}", "-e", f"MAX_TURNS={ENV.get('MAX_TURNS', '40')}",
                   "-e", f"GH_TOKEN={GH_TOKEN}", "-e", f"CLAUDE_CODE_OAUTH_TOKEN={ENV['CLAUDE_CODE_OAUTH_TOKEN']}",
                   image]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            cur.execute("update jobs set container_id=%s where id=%s", (name, job_id))
            lines: list[str] = []
            deadline = time.time() + JOB_TIMEOUT
            cancelled = False
            while True:
                line = proc.stdout.readline()
                if line:
                    lines.append(line)
                    if len(lines) % 20 == 0:
                        cur.execute("select cancel_requested from jobs where id=%s", (job_id,))
                        if cur.fetchone()["cancel_requested"]:
                            cancelled = True
                elif proc.poll() is not None:
                    break
                if cancelled or time.time() > deadline:
                    subprocess.run(["docker", "kill", name], capture_output=True)
                    proc.wait(timeout=30)
                    break
            # ログをオブジェクトストアへ（ワーカーには何も残さない）
            blob_key = f"jobs/{job_id}/worker.log"
            data = "".join(lines).encode()
            store.put_object(BUCKET, blob_key, io.BytesIO(data), len(data), content_type="text/plain")
            result = None
            for l in reversed(lines):
                if l.startswith("FACTORY_RESULT:"):
                    try:
                        result = json.loads(l[len("FACTORY_RESULT:"):])
                    except json.JSONDecodeError:
                        pass
                    break
            if cancelled:
                state, err = "cancelled", "cancel_requested"
            elif time.time() > deadline and proc.returncode not in (0,):
                state, err = "failed", "timeout"
            elif proc.returncode == 0 and result and result.get("pr_url"):
                state, err = "succeeded", None
            elif proc.returncode == 4 and result and result.get("pr_url"):
                state, err = "failed", f"incomplete(claude_exit={result.get('claude_exit')})"
            else:
                state, err = "failed", f"exit={proc.returncode}"
            cur.execute("update jobs set state=%s, finished_at=now(), result=%s, error=%s where id=%s",
                        (state, Jsonb(result) if result else None, err, job_id))
            usage = (result or {}).get("usage") or {}
            total_tokens = sum(int(v) for k, v in usage.items() if isinstance(v, (int, float)) and k.endswith("tokens"))
            billable_tokens = sum(int(usage.get(k, 0) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))   # 予算はキャッシュ読みを除く
            record_event(cur, "job.finished", corr, job_id, {"state": state, "error": err, "exit": proc.returncode,
                                                             "total_tokens": total_tokens, "billable_tokens": billable_tokens, "over_budget": billable_tokens > TOKEN_BUDGET,
                                                             "incomplete": bool((result or {}).get("incomplete")), "claude_exit": (result or {}).get("claude_exit"),
                                                             "cost_usd": (result or {}).get("cost_usd"), "blob_key": blob_key})
            if result and result.get("usage"):
                record_event(cur, "session.cost_recorded", corr, job_id, {"model": result.get("model"), "usage": usage, "cost_usd": result.get("cost_usd")})
            if state == "succeeded":
                cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload, blob_key) values ('PR',%s,%s,'implement',%s,%s)",
                            (corr, job_id, Jsonb({"repo": repo, "ticket_ids": [issue], "pr_url": result["pr_url"], "number": result.get("pr_number"),
                                                  "change_types": result.get("change_types", ["code_change"])}), blob_key))
                record_event(cur, "pr.opened", corr, job_id, {"pr_url": result["pr_url"]}, dedupe_key=f"pr.opened:{result['pr_url']}")
                for text in (result.get("lessons") or [])[:3]:   # ワーカーの「## 知見」を episodic の Lesson として残す（P0 は記録のみ。採用はしない）
                    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Lesson',%s,%s,'learn',%s)",
                                (corr, job_id, Jsonb({"knowledge_stage": "episodic", "teacher_signal": "worker_self_report", "summary": text,
                                                      "eval_cases": [], "state": "candidate", "source_pr": result["pr_url"]})))
                swap_label(repo, issue, [LABELS["running"]], [LABELS["pr_open"]])
                comment(repo, issue, f"ジョブ {job_id} が PR を作成しました: {result['pr_url']}\n消費: {total_tokens:,} tokens（予算 {TOKEN_BUDGET:,}）")
            elif state == "failed" and result and result.get("pr_url"):
                swap_label(repo, issue, [LABELS["running"]], [LABELS["failed"]])
                comment(repo, issue, f"ジョブ {job_id} は途中で終了しました（{err}）。差分はドラフト PR {result['pr_url']} に残してあります。範囲を満たしていれば建設者がドラフトを外してマージ、足りなければ `factoryctl rerun {job_id}`（MAX_TURNS を上げる）で続きを。")
            else:
                swap_label(repo, issue, [LABELS["running"]], [LABELS["failed"]])
                tail = "".join(lines[-15:])
                comment(repo, issue, f"ジョブ {job_id} は {state}（{err}）。`factoryctl rerun {job_id}` で再実行できます。\n\n```\n{tail[-1500:]}\n```")
            log(f"job {job_id} {state} ({err})")
        except Exception as e:  # noqa: BLE001
            log("run_job error", job_id, repr(e))
            cur.execute("update jobs set state='failed', finished_at=now(), error=%s where id=%s and state='running'", (repr(e)[:500], job_id))
            record_event(cur, "job.finished", corr, job_id, {"state": "failed", "error": repr(e)[:500]})
        finally:
            if JOB_DB_ENABLED:
                try:
                    cur.execute(f'drop database if exists "job_{job_id}" with (force)')   # ワーカーの状態を残さない
                except Exception as e:  # noqa: BLE001
                    log("job db drop failed", job_id, repr(e))
            cur.execute("select pg_advisory_unlock(hashtext(%s))", (lock_key,))


def run_loop():
    sem = threading.BoundedSemaphore(MAX_PARALLEL)
    while True:
        sem.acquire()
        job = None
        try:
            with db() as conn, conn.cursor() as cur:
                cur.execute("select * from claim_job()")   # (claim_job()).* は列ごとに関数が呼ばれるので使わない
                row = cur.fetchone()
                job = row if row and row.get("id") else None
        except Exception as e:  # noqa: BLE001
            log("claim error", e)
        if not job:
            sem.release()
            time.sleep(3)
            continue

        def _run(j=job):
            try:
                run_job(j)
            finally:
                sem.release()
        threading.Thread(target=_run, daemon=True).start()


# ------------------------------------------------------------------ Webhook（任意）
class Hook(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n)
        sig = self.headers.get("X-Hub-Signature-256", "")
        mac = "sha256=" + hmac.new(ENV["WEBHOOK_SECRET"].encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, mac):
            self.send_response(401); self.end_headers(); return
        ev = json.loads(body or b"{}")
        if self.headers.get("X-GitHub-Event") == "issues" and ev.get("action") == "labeled" and ev.get("label", {}).get("name") == TRIGGER_LABEL:
            repo, n = ev["repository"]["full_name"], ev["issue"]["number"]
            if repo in REPOS and enqueue(repo, n, "webhook"):
                swap_label(repo, n, [TRIGGER_LABEL], [LABELS["queued"]])
        self.send_response(204); self.end_headers()

    def log_message(self, *a):  # 静かに
        pass


def recover():
    """dispatcher の再起動で取り残された running ジョブを片付ける。
    ワーカーは最初からやり直せる（冪等）ので、コンテナを止めて failed にし、attempt+1 で再投入する。"""
    with db() as conn, conn.cursor() as cur:
        cur.execute("select id, repo, issue_number, attempt, container_id, correlation_id from jobs where state='running'")
        for j in cur.fetchall():
            if j["container_id"]:
                subprocess.run(["docker", "kill", j["container_id"]], capture_output=True)
            cur.execute("update jobs set state='failed', finished_at=now(), error='orphaned_by_restart' where id=%s", (j["id"],))
            record_event(cur, "job.finished", j["correlation_id"], j["id"], {"state": "failed", "error": "orphaned_by_restart"})
            cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier) values (%s,%s,%s,%s,'recover',%s) returning id",
                        (j["correlation_id"], j["repo"], j["issue_number"], j["attempt"] + 1, MODEL_TIER))
            new_id = cur.fetchone()["id"]
            record_event(cur, "job.enqueued", j["correlation_id"], new_id, {"by": "recover", "attempt": j["attempt"] + 1, "previous": j["id"]})
            log(f"recovered job {j['id']} -> re-enqueued as {new_id}")


def main():
    with db() as conn, conn.cursor() as cur:
        cur.execute("select 1")
    recover()
    if not store.bucket_exists(BUCKET):
        store.make_bucket(BUCKET)
    log(f"dispatcher up: repos={REPOS} label={TRIGGER_LABEL} parallel={MAX_PARALLEL} image={WORKER_IMAGE}")
    threading.Thread(target=poll_loop, daemon=True).start()
    if ENV.get("WEBHOOK_SECRET"):
        srv = ThreadingHTTPServer(("0.0.0.0", int(ENV.get("WEBHOOK_PORT", "8787"))), Hook)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log("webhook listening")
    run_loop()


if __name__ == "__main__":
    sys.exit(main())
