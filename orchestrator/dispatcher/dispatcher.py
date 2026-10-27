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
import re
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

import judgment
import stoppage

ENV = os.environ
DATABASE_URL = ENV["DATABASE_URL"]
REPOS = [r.strip() for r in ENV.get("FACTORY_REPOS", "").split(",") if r.strip()]
TRIGGER_LABEL = ENV.get("TRIGGER_LABEL", "factory:implement")
REQUEST_LABEL = ENV.get("REQUEST_LABEL", "factory:request")   # intake: 要求者の曖昧な要求を clarify にかける（T20）
TRIGGERS = {TRIGGER_LABEL: "implement", REQUEST_LABEL: "clarify"}
PROVISIONAL_LABEL = "factory:provisional"   # ワーカーが仮決めした判断を含む PR（ADR 0006。出口で judge が裁く）
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
# 番人（ADR 0005）。閾値は仮置き（委任の範囲。運用で調整）
WATCH_INTERVAL = int(ENV.get("WATCH_INTERVAL_SEC", "60"))
IDLE_MIN = int(ENV.get("WATCH_IDLE_MIN", "20"))              # 実行中 0 かつ投入可能 0 がこの分数続いたら停止
HEARTBEAT_MIN = int(ENV.get("WATCH_HEARTBEAT_MIN", "30"))    # supervisor の heartbeat がこの分数ないと停止（heartbeat が 1 度でも来てから有効）
USAGE_LIMIT_MARKERS = ("usage limit", "rate limit", "rate_limit")
# 差し戻し（ADR 0006 後半 / D5）。上限は仮置き（差し戻しが 20 件溜まったら見直す）
SENDBACK_PER_PR = int(ENV.get("SENDBACK_PER_PR", "1"))
SENDBACK_DAILY_MAX = int(ENV.get("SENDBACK_DAILY_MAX", "5"))
LOW_CONFIDENCE = float(ENV.get("LOW_CONFIDENCE", "0.6"))

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


def provenance(repo: str, image: str) -> dict:
    """製造の来歴（ADR 0007）。全部 optional。取れなければ unknown にして、ラインは止めない"""
    prov = {"engine_version": ENV.get("ENGINE_VERSION") or "unknown", "worker_image": image, "worker_image_id": "unknown",
            "product_sha": "unknown", "mode": ENV.get("FACTORY_MODE") or "unknown", "model_tier": MODEL_TIER}
    try:
        r = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image], capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            prov["worker_image_id"] = r.stdout.strip()[:19]
    except Exception:  # noqa: BLE001
        pass
    if not ENV.get("WORKER_DRY_RUN"):
        try:
            branch = gh("GET", f"repos/{repo}")["default_branch"]
            prov["product_sha"] = gh("GET", f"repos/{repo}/commits/{urllib.parse.quote(branch)}")["sha"][:12]   # base/overlay の版（製品の既定ブランチの先頭）
        except Exception:  # noqa: BLE001
            pass
    return prov


def comment(repo: str, issue: int, text: str):
    if ENV.get("WORKER_DRY_RUN"):
        return
    gh("POST", f"repos/{repo}/issues/{issue}/comments", {"body": text})


# ------------------------------------------------------------------ キュー（薄いワークフロー I/F：enqueue / claim / finish / cancel）
def record_event(cur, name: str, correlation_id: str | None, job_id: int | None, payload: dict, dedupe_key: str | None = None):
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values (%s,%s,%s,%s,%s) on conflict (dedupe_key) do nothing",
                (name, correlation_id, job_id, Jsonb(payload), dedupe_key))


def enqueue(repo: str, issue: int, by: str, attempt: int = 1, kind: str = "implement") -> int | None:
    corr = f"github:{repo}#{issue}"
    with db() as conn, conn.cursor() as cur:
        try:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind) values (%s,%s,%s,%s,%s,%s,%s) returning id",
                        (corr, repo, issue, attempt, by, MODEL_TIER, kind))
        except psycopg.errors.UniqueViolation:
            return None   # すでに queued / running（二重実行の抑止 1 段目）
        job_id = cur.fetchone()["id"]
        record_event(cur, "job.enqueued", corr, job_id, {"repo": repo, "issue": issue, "by": by, "attempt": attempt, "kind": kind})
        if kind == "clarify":   # intake: 受け付けた要求を Request（raw）として残す
            cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Request',%s,%s,'intake',%s)",
                        (corr, job_id, Jsonb({"repo": repo, "issue": issue, "status": "raw"})))
            record_event(cur, "request.received", corr, job_id, {"issue": issue})
    return job_id


def poll_once():
    for repo in REPOS:
        for label, kind in TRIGGERS.items():
            try:
                issues = gh("GET", f"repos/{repo}/issues?state=open&labels={urllib.parse.quote(label)}&per_page=50")
            except Exception as e:  # noqa: BLE001
                log("poll error", repo, e)
                continue
            for it in issues or []:
                if "pull_request" in it:
                    continue
                n = it["number"]
                job_id = enqueue(repo, n, "poll", kind=kind)
                if job_id:
                    log(f"enqueued {kind} job {job_id} for {repo}#{n}")
                    swap_label(repo, n, [label], [LABELS["queued"]])
                    what = "PR 作成まで自動で進めます" if kind == "implement" else "要求を確かめる質問をこの Issue に書きます（最大 3 往復）"
                    comment(repo, n, f"工場がジョブ {job_id} として受け付けました（attempt 1）。{what}。")
                else:
                    # 既に active なジョブがある。ラベルだけ直す
                    swap_label(repo, n, [label], [])


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
        kind = job.get("kind") or "implement"
        try:
            if kind in ("inspect", "judge", "body_fix"):   # 検査・judge・本文の追記のジョブ（ADR 0004 / 0006）。issue_number は PR の番号。ラベルは結果を見て付ける
                image = WORKER_IMAGE
            else:
                swap_label(repo, issue, [LABELS["queued"]], [LABELS["running"]])
                image = image_for(repo, issue)
            prov = provenance(repo, image)
            prov["model_tier"] = job["model_tier"] or MODEL_TIER
            record_event(cur, "job.started", corr, job_id, {"attempt": job["attempt"], "model_tier": job["model_tier"], "image": image, "provenance": prov})
            name = f"factory-job-{job_id}"
            job_db_url = None
            if JOB_DB_ENABLED and kind not in ("inspect", "judge", "body_fix"):
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
                   *(["--entrypoint", "/usr/local/bin/clarify.sh"] if kind == "clarify" else []),
                   *(["--entrypoint", "/usr/local/bin/inspect.sh", "-e", f"PR_URL=https://github.com/{repo}/pull/{issue}"] if kind == "inspect" else []),
                   *(["-i", "--entrypoint", "/usr/local/bin/judge.sh", "-e", f"PR_URL=https://github.com/{repo}/pull/{issue}"] if kind == "judge" else []),
                   *(["-i", "--entrypoint", "/usr/local/bin/body_fix.sh", "-e", f"PR_URL=https://github.com/{repo}/pull/{issue}"] if kind == "body_fix" else []),
                   image]
            judge_inp = None
            if kind in ("judge", "body_fix"):   # 入力は supervisor / dispatcher が job.enqueued に残したもの（制御プレーンの記録だけで組み立てた JSON）
                cur.execute("select payload from events where name='job.enqueued' and job_id=%s order by id limit 1", (job_id,))
                ev = cur.fetchone()
                judge_inp = (ev["payload"] if ev else {}).get("input") or {}
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if kind in ("judge", "body_fix") else None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if kind in ("judge", "body_fix"):
                proc.stdin.write(json.dumps(judge_inp, ensure_ascii=False))
                proc.stdin.close()
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
            elif proc.returncode == 0 and result and result.get("kind") == "clarify":
                state, err = ("succeeded", None) if result.get("status") == "clarified" else ("failed", "unresolved_after_max_rounds")
            elif proc.returncode == 4 and result and result.get("pr_url"):
                state, err = "failed", f"incomplete(claude_exit={result.get('claude_exit')})"
            else:
                state, err = "failed", f"exit={proc.returncode}"
            cur.execute("update jobs set state=%s, finished_at=now(), result=%s, error=%s where id=%s",
                        (state, Jsonb(result) if result else None, err, job_id))
            usage = (result or {}).get("usage") or {}
            total_tokens = sum(int(v) for k, v in usage.items() if isinstance(v, (int, float)) and k.endswith("tokens"))
            billable_tokens = sum(int(usage.get(k, 0) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))   # 予算はキャッシュ読みを除く
            usage_limited = state == "failed" and any(m in l.lower() for l in lines[-30:] for m in USAGE_LIMIT_MARKERS)   # 契約の使用量上限（S15）
            record_event(cur, "job.finished", corr, job_id, {"state": state, "error": err, "exit": proc.returncode, "usage_limited": usage_limited,
                                                             "total_tokens": total_tokens, "billable_tokens": billable_tokens, "over_budget": billable_tokens > TOKEN_BUDGET,
                                                             "incomplete": bool((result or {}).get("incomplete")), "claude_exit": (result or {}).get("claude_exit"),
                                                             "cost_usd": (result or {}).get("cost_usd"), "blob_key": blob_key})
            if result and result.get("usage"):
                record_event(cur, "session.cost_recorded", corr, job_id, {"model": result.get("model"), "usage": usage, "cost_usd": result.get("cost_usd")})
            if result and result.get("pr_url") and (result.get("provisional_raw") or "").strip():
                # ワーカーの仮決め（ADR 0006）。記録の失敗でラインは止めない
                try:
                    mode, _ = judgment.current_mode(cur, ENV.get("FACTORY_MODE") or "attended", ENV.get("MODE_SCHEDULE", ""))   # 仮決めした時点のモード
                    items = judgment.record_provisional(cur, corr, job_id, result["pr_url"], result["provisional_raw"], mode)
                    if items and result.get("pr_number"):
                        swap_label(repo, int(result["pr_number"]), [], [PROVISIONAL_LABEL])
                except Exception as e:  # noqa: BLE001
                    log("provisional record failed", job_id, repr(e))
            if kind == "body_fix":
                record_event(cur, "pr.body_amended" if state == "succeeded" else "pr.body_amend_failed", corr, job_id,
                             {"pr_url": (result or {}).get("pr_url"), "defects": len(judge_inp.get("defects", [])), "chars": (result or {}).get("chars")})
            elif kind == "judge":
                if state == "succeeded" and (result or {}).get("verdicts"):
                    ev_mode = judge_inp.get("mode") or "attended"
                    cur.execute("select payload->>'mode_basis' as b from events where name='job.enqueued' and job_id=%s limit 1", (job_id,))
                    basis = (cur.fetchone() or {}).get("b") or "unknown"
                    summary = judgment.record_judged(cur, corr, job_id, result["pr_url"], judge_inp, result["verdicts"], ev_mode, basis)
                    act_on_judgment(cur, repo, corr, job_id, result["pr_url"], summary, ev_mode)
            elif kind == "inspect":
                # 検査（shadow）: gate.evaluated と InspectionReport を残すだけ。差し戻し（修正 Issue の起票）は judge の後半（ADR 0006）
                rep = ((result or {}).get("report") or {}).get("result") or {}
                if state == "succeeded" and rep:
                    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('InspectionReport',%s,%s,'verify',%s)",
                                (corr, job_id, Jsonb({"pr_url": result["pr_url"], "mode": "shadow", "gate": "rubric", "model": result.get("model"),
                                                      "defects": rep.get("defects", []), "checked": rep.get("checked_rubric_ids", []), "summary": rep.get("summary")})))
                    record_event(cur, "gate.evaluated", corr, job_id, {"pr_url": result["pr_url"], "mode": "shadow", "gate": "rubric", "model": result.get("model"),
                                                                     "defects": rep.get("defects", []), "checked": rep.get("checked_rubric_ids", []),
                                                                     "cost_usd": result.get("cost_usd")})
            elif result and result.get("kind") == "clarify":
                # clarify: 確定した Request を残す。往復回数と問いの回答待ちを Trust / Flow の計測に
                cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Request',%s,%s,'clarify',%s)",
                            (corr, job_id, Jsonb({"repo": repo, "issue": issue, "status": result.get("status"), "requester": result.get("requester"),
                                                  "rounds": result.get("rounds"), **(result.get("request") or {})})))
                record_event(cur, "request.clarified" if state == "succeeded" else "request.unresolved", corr, job_id,
                             {"rounds": result.get("rounds"), "requester": result.get("requester"), "cost_usd": result.get("cost_usd")})
                swap_label(repo, issue, [LABELS["running"]], ["factory:clarified" if state == "succeeded" else LABELS["failed"]])
            elif state == "succeeded":
                cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload, blob_key) values ('PR',%s,%s,'implement',%s,%s)",
                            (corr, job_id, Jsonb({"repo": repo, "ticket_ids": [issue], "pr_url": result["pr_url"], "number": result.get("pr_number"),
                                                  "change_types": result.get("change_types", ["code_change"]), "provenance": prov}), blob_key))
                record_event(cur, "pr.opened", corr, job_id, {"pr_url": result["pr_url"]}, dedupe_key=f"pr.opened:{result['pr_url']}")
                for text in (result.get("lessons") or [])[:3]:   # ワーカーの「## 知見」を episodic の Lesson として残す（P0 は記録のみ。採用はしない）
                    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Lesson',%s,%s,'learn',%s)",
                                (corr, job_id, Jsonb({"knowledge_stage": "episodic", "teacher_signal": "worker_self_report", "summary": text,
                                                      "eval_cases": [], "state": "candidate", "source_pr": result["pr_url"]})))
                swap_label(repo, issue, [LABELS["running"]], [LABELS["pr_open"]])
                comment(repo, issue, f"ジョブ {job_id} が PR を作成しました: {result['pr_url']}\n消費: {total_tokens:,} tokens（予算 {TOKEN_BUDGET:,}）")
            elif state == "failed" and result and result.get("pr_url"):
                # 途中終了のドラフトも PR として残す。建設者が仕上げてドラフトを外すと、supervisor が判定・検査・judge に回す
                cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload, blob_key) values ('PR',%s,%s,'implement',%s,%s)",
                            (corr, job_id, Jsonb({"repo": repo, "ticket_ids": [issue], "pr_url": result["pr_url"], "number": result.get("pr_number"),
                                                  "change_types": result.get("change_types", ["code_change"]), "provenance": prov, "incomplete": True}), blob_key))
                record_event(cur, "pr.opened", corr, job_id, {"pr_url": result["pr_url"], "incomplete": True}, dedupe_key=f"pr.opened:{result['pr_url']}")
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


# ------------------------------------------------------------------ judge の結果に従って印を付け、差し戻しを起票する（ADR 0006 後半 / D5）
def _quote(text: str) -> str:
    """指摘や判断の本文はデータとして引用する（ワーカーへの指示にしない）"""
    return "\n".join("> " + l for l in (text or "").strip().splitlines()[:20]) or "> （なし）"


def is_body_only(d: dict) -> bool:
    """指摘の箇所が PR の本文だけ（コード・文書のファイルではない）"""
    loc = (d.get("location") or "").strip()
    # 検査は「PR 本文」「PR #97 本文」「PR body」などと書く（番号付きは 2026-10-27 の rally #97 で Issue に化けた）
    return re.match(r"(?i)^pr\s*(#\d+\s*)?(本文|body)", loc) is not None


def enqueue_body_fix(cur, repo: str, corr: str, pr_url: str, pr_number: int, defects: list[dict]):
    try:
        cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, model_tier, kind) values (%s,%s,%s,'judge','tier-small','body_fix') returning id",
                    (corr, repo, pr_number))
    except psycopg.errors.UniqueViolation:
        log("body_fix skipped (active job)", pr_url)
        return
    jid = cur.fetchone()["id"]
    record_event(cur, "job.enqueued", corr, jid, {"by": "judge", "kind": "body_fix", "pr_url": pr_url,
                                                 "input": {"pr_url": pr_url, "defects": [{k: d.get(k) for k in ("rubric_id", "severity", "location", "evidence", "remediation_hint")} for d in defects]}})
    log("body_fix enqueued", pr_url, "job", jid)


def act_on_judgment(cur, repo: str, corr: str, job_id: int, pr_url: str, summary: dict, mode: str):
    pr_number = int(pr_url.rsplit("/", 1)[1])
    labels = [f"factory:judged:{mode}"]
    if summary["min_confidence"] is not None and summary["min_confidence"] < LOW_CONFIDENCE:
        labels.append("factory:confidence:low")
    if summary["escalated"]:
        labels.append("factory:needs-judgment")
        stoppage.open_stoppage(cur, f"judge:{pr_url}", "human", f"judge が {len(summary['escalated'])} 件を人に回した",
                               waiting_for="人の判断（PR の factory:needs-judgment）", correlation_id=corr, job_id=job_id, detected_by="supervisor")
    try:
        swap_label(repo, pr_number, [], labels)
    except Exception as e:  # noqa: BLE001
        log("judge label failed", pr_url, repr(e))
    # PR 本文だけの指摘は Issue を切らず、本文の追記で直す（2026-10-27 Tetsuro）。差し戻しの回数にも数えない
    body_only = [d for d in summary["adopted_defects"] if is_body_only(d)]
    if body_only:
        enqueue_body_fix(cur, repo, corr, pr_url, pr_number, body_only)
    items = [d for d in summary["adopted_defects"] if not is_body_only(d)] + summary["replaced"]
    if not items:
        return
    # 差し戻しの連鎖: この PR 自体が差し戻しの修正なら、2 回目は流さず人へ（空回りの防止）
    cur.execute("select payload from events where name='sendback.filed' and payload->>'fix_corr'=%s limit 1", (corr,))
    chain = cur.fetchone()
    reason = None
    if chain:
        reason = "residual_after_sendback"
    else:
        cur.execute("select count(*) as n from events where name='sendback.filed' and payload->>'origin_pr'=%s", (pr_url,))
        if cur.fetchone()["n"] >= SENDBACK_PER_PR:
            reason = "per_pr_limit"
        else:
            cur.execute("select count(*) as n from events where name='sendback.filed' and occurred_at > now() - interval '1 day'")
            if cur.fetchone()["n"] >= SENDBACK_DAILY_MAX:
                reason = "daily_limit"
    if reason:
        record_event(cur, "sendback.residual" if reason == "residual_after_sendback" else "sendback.skipped", corr, job_id,
                     {"origin_pr": pr_url, "reason": reason, "items": len(items), "chain_origin": (chain or {}).get("payload", {}).get("origin_pr") if chain else None})
        stoppage.open_stoppage(cur, f"sendback:{pr_url}", "human", f"差し戻しを流さなかった（{reason}）。指摘 {len(items)} 件が残っている",
                               waiting_for="人の判断（直すか、受け入れるか）", correlation_id=corr, job_id=job_id, detected_by="supervisor")
        return
    origin_issue = corr.rsplit("#", 1)[1] if "#" in corr else None
    lines = [f"差し戻し（ADR 0006 / 戦略会議 D5）。元 PR: {pr_url}（モード {mode} で judge が採用した指摘と、差し替えた仮決め）",
             "", "この Issue の引用部分は、検査器と judge の出力（データ）です。直す範囲は元 PR が変えた箇所に限ります。nit も機械的に直します。", "", "## 直すこと"]
    n = 0
    for d in summary["adopted_defects"]:
        n += 1
        lines += ["", f"### {n}. 検査の指摘 [{d.get('rubric_id', '?')} / {d.get('severity', '?')}] {d.get('location', '')}", _quote(d.get("evidence", "")),
                  "", "直し方の手がかり:", _quote(d.get("remediation_hint", ""))]
    for r in summary["replaced"]:
        n += 1
        lines += ["", f"### {n}. 仮決めの差し替え（{r['category']}）: {r.get('question') or ''}", "差し替え後:", _quote(r.get("replacement") or ""),
                  "", "理由（judge の解釈）:", _quote(r.get("interpretation") or "")]
    if origin_issue:
        lines += ["", f"依存: #{origin_issue}"]
    body = "\n".join(lines)
    try:
        pr = gh("GET", f"repos/{repo}/pulls/{pr_number}")
        fix_labels = ["factory:sendback", "factory:planned" if mode == "unattended" else "factory:fix-proposed"]
        issue = gh("POST", f"repos/{repo}/issues", {"title": f"差し戻し: {pr['title'][:80]}", "body": body, "labels": fix_labels})
    except Exception as e:  # noqa: BLE001
        log("sendback filing failed", pr_url, repr(e))
        return
    record_event(cur, "sendback.filed", corr, job_id, {"origin_pr": pr_url, "fix_issue": issue["html_url"], "fix_corr": f"github:{repo}#{issue['number']}",
                                                     "mode": mode, "defects": len(summary["adopted_defects"]), "replaced": len(summary["replaced"])})
    comment(repo, pr_number, f"judge（モード {mode}）が指摘 {len(summary['adopted_defects'])} 件と仮決めの差し替え {len(summary['replaced'])} 件を採用し、差し戻しを {issue['html_url']} に起票しました。"
            + ("依存が閉じたら supervisor が流します。" if mode == "unattended" else "流すときは Issue のラベル factory:fix-proposed を factory:planned に替えてください。"))


# ------------------------------------------------------------------ 番人（ADR 0005）。トークンを使わず、制御プレーンの状態だけ見る
def classify_failure(p: dict) -> tuple[str, str, str] | None:
    """失敗したジョブを (分類, 何が止まったか, 待っているもの) にする。自動で回復するものは None"""
    err = p.get("error") or ""
    if err in ("orphaned_by_restart", "lock_held", "cancel_requested"):
        return None
    if p.get("usage_limited"):
        return "budget", "契約の使用量上限でジョブが止まった", "使用量上限のリセット後の rerun"
    if err == "timeout":
        return "budget", f"時間の上限（{JOB_TIMEOUT // 60} 分）で打ち切られた", "チケットの分割か rerun"
    if err.startswith("incomplete"):
        return "budget", "ターン上限で途中終了した（ドラフト PR）", "建設者の仕上げか rerun"
    if err == "unresolved_after_max_rounds":
        return "ticket", "要求が質問の上限まで往復しても確定しなかった", "要求者か建設者の判断"
    return "factory-bug", "ジョブが失敗した（未分類。人が分類を直す）", "原因の調査と rerun"


def watch_once():
    with db() as conn, conn.cursor() as cur:
        # 番人が見始めた時点。これより前のジョブの失敗は遡って開かない（過去分は builder が手で記録している）
        record_event(cur, "watcher.started", None, None, {}, dedupe_key="watcher.started:v1")
        cur.execute("select occurred_at from events where dedupe_key='watcher.started:v1'")
        since = cur.fetchone()["occurred_at"]

        # 1) ラインが空: 実行中 0 かつ投入可能 0 が IDLE_MIN 分
        cur.execute("select count(*) filter (where state='running') as r, count(*) filter (where state='queued') as q, "
                    "max(greatest(enqueued_at, coalesce(started_at, enqueued_at), coalesce(finished_at, enqueued_at))) as last, "
                    "now() - max(greatest(enqueued_at, coalesce(started_at, enqueued_at), coalesce(finished_at, enqueued_at))) > make_interval(mins => %s) as long_idle "
                    "from jobs", (IDLE_MIN,))
        j = cur.fetchone()
        idle_open = stoppage.open_keys(cur, "idle:")
        if j["r"] == 0 and j["q"] == 0:
            if not idle_open and j["last"] and j["long_idle"]:
                stoppage.open_stoppage(cur, f"idle:{j['last']:%Y%m%dT%H%M%S}", "human", "ラインが空（実行中・投入可能のジョブがない）",
                                       waiting_for="次の Ticket（factory:implement / factory:request のラベル）か、承認待ちの PR の判断", started_at=j["last"])
        else:
            for k in idle_open:
                stoppage.close_stoppage(cur, k, remedy="ジョブが投入された")

        # 2) supervisor の heartbeat（1 度でも来てから有効）
        cur.execute("select max(occurred_at) as last, now() - max(occurred_at) > make_interval(mins => %s) as stale "
                    "from events where name='supervisor.heartbeat'", (HEARTBEAT_MIN,))
        h = cur.fetchone()
        hb_open = stoppage.open_keys(cur, "heartbeat:")
        if h["last"] and h["stale"]:
            if not hb_open:
                stoppage.open_stoppage(cur, f"heartbeat:{h['last']:%Y%m%dT%H%M%S}", "env", f"supervisor の heartbeat が {HEARTBEAT_MIN} 分ない",
                                       waiting_for="supervisor の再起動（Mac のスリープ・停止を確認）", started_at=h["last"], detected_by="watcher")
        else:
            for k in hb_open:
                stoppage.close_stoppage(cur, k, remedy="heartbeat が戻った")

        # 3) 予算・時間の超過と、人の手当てを待つ失敗
        cur.execute("select job_id, correlation_id, occurred_at, payload from events "
                    "where name='job.finished' and payload->>'state'='failed' and occurred_at >= %s order by id", (since,))
        for e in cur.fetchall():
            c = classify_failure(e["payload"])
            if c:
                stoppage.open_stoppage(cur, f"job:{e['job_id']}", c[0], c[1], waiting_for=c[2], cause=e["payload"].get("error") or "",
                                       started_at=e["occurred_at"], correlation_id=e["correlation_id"], job_id=e["job_id"])
        for k in stoppage.open_keys(cur, "job:"):
            cur.execute("select j2.id from jobs j1 join jobs j2 on j2.correlation_id=j1.correlation_id and j2.id>j1.id and j2.started_at is not null "
                        "where j1.id=%s order by j2.id limit 1", (int(k.split(":", 1)[1]),))
            nxt = cur.fetchone()
            if nxt:
                stoppage.close_stoppage(cur, k, remedy=f"再実行された（job {nxt['id']}）")

        # 4) 打ち切りの時刻を過ぎても running のまま（dispatcher の不具合）
        cur.execute("select id, correlation_id, started_at from jobs where state='running' and started_at < now() - make_interval(secs => %s)",
                    (JOB_TIMEOUT + 600,))
        stuck = {r["id"]: r for r in cur.fetchall()}
        for r in stuck.values():
            stoppage.open_stoppage(cur, f"stuck:{r['id']}", "factory-bug", "打ち切りの時刻を過ぎてもジョブが running のまま",
                                   waiting_for="dispatcher の調査（factoryctl stop / 再起動）", started_at=r["started_at"],
                                   correlation_id=r["correlation_id"], job_id=r["id"])
        for k in stoppage.open_keys(cur, "stuck:"):
            if int(k.split(":", 1)[1]) not in stuck:
                stoppage.close_stoppage(cur, k, remedy="ジョブが終了した")


def watch_loop():
    while True:
        try:
            watch_once()
        except Exception as e:  # noqa: BLE001  番人の失敗でラインを止めない
            log("watch error", repr(e))
        time.sleep(WATCH_INTERVAL)


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
        cur.execute("select id, repo, issue_number, attempt, container_id, correlation_id, kind from jobs where state='running'")
        for j in cur.fetchall():
            if j["container_id"]:
                subprocess.run(["docker", "kill", j["container_id"]], capture_output=True)
            cur.execute("update jobs set state='failed', finished_at=now(), error='orphaned_by_restart' where id=%s", (j["id"],))
            record_event(cur, "job.finished", j["correlation_id"], j["id"], {"state": "failed", "error": "orphaned_by_restart"})
            cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind) values (%s,%s,%s,%s,'recover',%s,%s) returning id",
                        (j["correlation_id"], j["repo"], j["issue_number"], j["attempt"] + 1, MODEL_TIER, j["kind"]))
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
    if ENV.get("WATCH_ENABLED", "1") == "1":
        threading.Thread(target=watch_loop, daemon=True).start()
    if ENV.get("WEBHOOK_SECRET"):
        srv = ThreadingHTTPServer(("0.0.0.0", int(ENV.get("WEBHOOK_PORT", "8787"))), Hook)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log("webhook listening")
    run_loop()


if __name__ == "__main__":
    sys.exit(main())
