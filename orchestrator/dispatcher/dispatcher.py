#!/usr/bin/env python3
"""工場の dispatcher（P0 の制御プレーン。ADR 0003）。

- poll:   対象リポジトリの TRIGGER_LABEL が付いた Issue を見つけてジョブに積む（30 秒ごと）
- webhook: WEBHOOK_SECRET があれば /webhook で issues.labeled を受けて同じことをする
- run:    キューから claim（SKIP LOCKED。優先度の高い順）→ アドバイザリロック → ワーカーコンテナを docker run --rm → 結果を記録
- ops:    止める印の付いたジョブのコンテナを止める。緊急（p0）のジョブが枠を待っていたら、低いジョブを止めて順番待ちに戻す（ADR 0009）

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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ghapi
import psycopg
from minio import Minio
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import case
import committee
import continuation
import design
import design_line
import planner
import drain
import judgment
import ops
import pr_events
import reattach
import stoppage
import tidy

ENV = os.environ
DATABASE_URL = ENV["DATABASE_URL"]
REPOS = [r.strip() for r in ENV.get("FACTORY_REPOS", "").split(",") if r.strip()]
TRIGGER_LABEL = ENV.get("TRIGGER_LABEL", "factory:implement")
REQUEST_LABEL = ENV.get("REQUEST_LABEL", "factory:request")   # intake: 要求者の曖昧な要求を clarify にかける（T20）
DESIGN_LABEL = ENV.get("DESIGN_LABEL", design_line.TRIGGER_LABEL)   # 画面を含むタスク: 実装の前にデザインの線を通す（ADR 0025）
PLAN_LABEL = ENV.get("PLAN_LABEL", planner.TRIGGER_LABEL)   # チケットの入口: プランナーが着手判定と経路を決める（ADR 0025・0026）
TRIGGERS = {TRIGGER_LABEL: "implement", REQUEST_LABEL: "clarify", DESIGN_LABEL: "ui_design", PLAN_LABEL: "plan"}
TRIGGER_JA = {"implement": "PR 作成まで自動で進めます", "clarify": "要求を確かめる質問をこの Issue に書きます（最大 3 往復）",
              "ui_design": "実装の前に、画面の要素と振る舞い（UXSpec）と案のモックを作り、デザイン検査役に見せます",
              "plan": "着手できるかを判定し、実装役・プロダクトデザイナー・デザインシステムのデザイナーのどれに回すかを決めます"}
PROVISIONAL_LABEL = "factory:provisional"   # ワーカーが仮決めした判断を含む PR（ADR 0006。出口で judge が裁く）
LABELS = {"queued": "factory:queued", "running": "factory:running", "pr_open": "factory:pr-open", "failed": "factory:failed"}
POLL_INTERVAL = int(ENV.get("POLL_INTERVAL_SEC", "30"))
MAX_PARALLEL = int(ENV.get("MAX_PARALLEL", "2"))
JOB_TIMEOUT = int(ENV.get("JOB_TIMEOUT_SEC", "2400"))
TOKEN_BUDGET = int(ENV.get("TOKEN_BUDGET_PER_JOB", "2000000"))
WORKER_IMAGE = ENV.get("WORKER_IMAGE", "factory-worker:p0")
# リポジトリごとのワーカーイメージ（例: "guccyon/rally=factory-worker:rust,owner/other=factory-worker:p0"）。未指定は WORKER_IMAGE
WORKER_IMAGES = dict(kv.split("=", 1) for kv in ENV.get("WORKER_IMAGES", "").split(",") if "=" in kv)
UI_DESIGN_IMAGE = ENV.get("UI_DESIGN_IMAGE", "factory-worker:design")   # デザイナー役（Chromium と同梱の書体つき。worker/Dockerfile.design）
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
STALL_MIN = int(ENV.get("WATCH_STALL_MIN", "15"))            # 実行中のジョブの出力がこの分数止まったら停止（完了まで定期的に見る。2026-10-31 オーナー）
LAST_OUTPUT: dict[int, float] = {}                           # 実行中のジョブが最後に出力した時刻（run_job が更新し、番人が読む）
MERGE_WAIT_MIN = int(ENV.get("WATCH_MERGE_WAIT_MIN", "30"))  # 工場がマージしない PR が、判定からこの分数マージも閉じもされないと停止
DRAIN_MIN = int(ENV.get("WATCH_DRAIN_MIN", "30"))            # ラインの一時停止がこの分数続いたら停止（再開の忘れ。ADR 0020）
LINE_LOW_BELOW = int(ENV.get("WATCH_LINE_LOW_BELOW", "2"))   # 投入待ちの段（planned で保留でないチケット）がこの数を切ったら知らせる。0 で無効
USAGE_LIMIT_MARKERS = ("usage limit", "rate limit", "rate_limit")
# 差し戻し（ADR 0006 後半 / D5）。上限は仮置き（差し戻しが 20 件溜まったら見直す）
SENDBACK_PER_PR = int(ENV.get("SENDBACK_PER_PR", "1"))
SENDBACK_DAILY_MAX = int(ENV.get("SENDBACK_DAILY_MAX", "5"))
LOW_CONFIDENCE = float(ENV.get("LOW_CONFIDENCE", "0.6"))
REVIEW_FIX = ENV.get("REVIEW_FIX", "1") == "1"   # マージ前に採用した指摘を、差し戻しの Issue でなく同じ担当の続きのジョブで直す（ADR 0017）。0 で従来の差し戻し
OPS_INTERVAL = int(ENV.get("OPS_INTERVAL_SEC", "5"))   # 停止・割り込みを見る間隔（トークンを使わない）

# S3 互換のオブジェクトストア（クライアントは minio SDK。サーバーは versitygw でも MinIO でもよい）
store = Minio(ENV["OBJECTSTORE_ENDPOINT"], access_key=ENV["OBJECTSTORE_ACCESS_KEY"], secret_key=ENV["OBJECTSTORE_SECRET_KEY"], secure=False)


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)


# ------------------------------------------------------------------ GitHub（REST、標準ライブラリのみ）
def gh(method: str, path: str, body: dict | None = None):
    return ghapi.request(method, path, body, user_agent="factory-dispatcher", token=GH_TOKEN)   # GET は ETag で条件付き（上限に数えない 304）


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


CONTINUE_BRANCH_RE = re.compile(r"^factory/issue-(\d+)-job-\d+$")


def continue_target(cur, job_id: int, repo: str, issue: int) -> dict | None:
    """job.enqueued に「続ける先」があれば返す（rerun --continue、衝突の解消、CI 落ち・検査の指摘の直し。ADR 0016・0017）。
    渡すのは工場のブランチの名前と PR の番号だけ。形が違えば続けずに普通の実装として流す"""
    cur.execute("select payload->'continue' as c from events where name='job.enqueued' and job_id=%s order by id limit 1", (job_id,))
    c = (cur.fetchone() or {}).get("c")
    if not c:
        return None
    m = CONTINUE_BRANCH_RE.match(str(c.get("branch") or ""))
    if not m or int(m.group(1)) != issue or not str(c.get("pr_number") or "").isdigit() or c.get("reason") not in continuation.REASONS:
        log("continue target rejected", job_id, c)
        return None
    return {"branch": c["branch"], "pr_number": int(c["pr_number"]), "reason": c["reason"]}


def provenance(repo: str, image: str) -> dict:
    """製造の来歴（ADR 0007）。全部 optional。取れなければ unknown にして、ラインは止めない"""
    prov = {"engine_version": ENV.get("ENGINE_VERSION") or "unknown", "worker_image": image, "worker_image_id": "unknown",
            "product_sha": "unknown", "mode": "unknown", "model_tier": MODEL_TIER}
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


def enqueue(repo: str, issue: int, by: str, attempt: int = 1, kind: str = "implement", issue_json: dict | None = None) -> int | None:
    corr = f"github:{repo}#{issue}"
    with db() as conn, conn.cursor() as cur:
        # GitHub の Issue を工場のチケットに写す（ADR 0009）。優先度の最初の値は factory:priority:* のラベルから
        it = issue_json or {}
        ticket = ops.upsert_ticket(cur, "github", f"{repo}#{issue}", repo, title=it.get("title") or "", url=it.get("html_url"),
                                   priority=ops.priority_from_labels([l.get("name", "") for l in it.get("labels") or []]), state="active")
        try:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind, ticket_id, priority) "
                        "values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
                        (corr, repo, issue, attempt, by, MODEL_TIER, kind, ticket["id"], ticket["priority"]))
        except psycopg.errors.UniqueViolation:
            return None   # すでに queued / running（二重実行の抑止 1 段目）
        job_id = cur.fetchone()["id"]
        record_event(cur, "job.enqueued", corr, job_id, {"repo": repo, "issue": issue, "by": by, "attempt": attempt, "kind": kind,
                                                         "ticket_id": ticket["id"], "priority": ticket["priority"]})
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
                job_id = enqueue(repo, n, "poll", kind=kind, issue_json=it)
                if job_id:
                    log(f"enqueued {kind} job {job_id} for {repo}#{n}")
                    swap_label(repo, n, [label], [LABELS["queued"]])
                    comment(repo, n, f"工場がジョブ {job_id} として受け付けました（attempt 1）。{TRIGGER_JA[kind]}。")
                else:
                    # 既に active なジョブがある。ラベルだけ直す
                    swap_label(repo, n, [label], [])


def store_put(key: str, data: bytes, content_type: str):
    store.put_object(BUCKET, key, io.BytesIO(data), len(data), content_type=content_type)


def store_get(key: str) -> bytes:
    obj = store.get_object(BUCKET, key)
    try:
        return obj.read()
    finally:
        obj.close()
        obj.release_conn()


def poll_loop():
    while True:
        if not ENV.get("WORKER_DRY_RUN"):
            poll_once()
            try:   # 起票者が問いに返事をしたチケットを、プランナーに判定し直させる（ADR 0026 の 0）
                with db() as conn, conn.cursor() as cur:
                    planner.poll_answers(cur, gh, swap_label, REPOS, MODEL_TIER)
            except Exception as e:  # noqa: BLE001
                log("plan answers poll failed", repr(e))
            try:   # 人が選んだ画面の案を Issue に反映する（factoryctl design pick・選択の画面。ADR 0025）
                with db() as conn, conn.cursor() as cur:
                    design_line.apply_choices(cur, gh, comment, store_get)
            except Exception as e:  # noqa: BLE001
                log("design choice apply failed", repr(e))
        time.sleep(POLL_INTERVAL)


# ------------------------------------------------------------------ ワーカーの実行
FOLLOWUP_KINDS = ("body_fix",)   # 同じ PR の別ジョブ（judge）の中から積まれる種類
CONTINUE_JA = {"incomplete": "途中終了の続き", "conflict": "衝突の解消", "ci_failed": "CI 落ちの直し", "review_fix": "検査の指摘の直し"}
LOCK_RETRY_SEC = 10
LOCK_WAIT_MAX_SEC = int(ENV.get("LOCK_WAIT_MAX_SEC", "1800"))   # 錠が空くのを待つ上限。超えたら lock_held_timeout で失敗にし、番人が停止として開く。値は仮置き


def waits_for_lock(cur, job: dict) -> bool:
    """錠が取れなかったとき、失敗にせず積み直して待つジョブか。
    本文の追記（judge が錠を放す数秒前に拾われる）と、続きのジョブ（衝突・CI 落ち・指摘・途中終了。同じチケットの前のジョブが
    状態を書いた後の後処理の間まだ錠を握っている）。続きのジョブは同じ head に 1 回しか積まれないので、ここで落とすと誰も積み直さない"""
    kind = job.get("kind") or "implement"
    if kind in FOLLOWUP_KINDS:
        return True
    if kind != "implement":
        return False
    cur.execute("select 1 from events where name='job.enqueued' and job_id=%s and payload ? 'continue' limit 1", (job["id"],))
    return cur.fetchone() is not None


def lock_waited_sec(cur, job_id: int) -> float:
    """このジョブが錠を待って積み直された最初の時点からの秒数。まだ待っていなければ 0"""
    cur.execute("select extract(epoch from now() - min(occurred_at)) as sec from events "
                "where name='job.requeued' and job_id=%s and payload->>'reason'='advisory_lock_held'", (job_id,))
    return float((cur.fetchone() or {}).get("sec") or 0)

def run_job(job: dict, reattaching: bool = False):
    """ジョブのコンテナを起動して、終わるまで出力を受け、結果を記録する。
    reattaching は dispatcher の上げ直しで、動いている（終わっている）コンテナにつなぎ直すとき（ADR 0020）。起動の前の準備はしない"""
    job_id, repo, issue, corr = job["id"], job["repo"], job["issue_number"], job["correlation_id"]
    lock_key = f"{repo}#{issue}"
    with db() as conn, conn.cursor() as cur:
        cur.execute("select pg_try_advisory_lock(hashtext(%s)) as ok", (lock_key,))
        if not cur.fetchone()["ok"]:   # 二重実行の抑止 2 段目
            waits = waits_for_lock(cur, job)
            if waits and lock_waited_sec(cur, job_id) < LOCK_WAIT_MAX_SEC:
                time.sleep(LOCK_RETRY_SEC)
                cur.execute("update jobs set state='queued', started_at=null where id=%s and state='running'", (job_id,))
                record_event(cur, "job.requeued", corr, job_id, {"reason": "advisory_lock_held"})
                return
            # 待つジョブが上限まで待った: 黙って落とさず、番人が停止として開く error にする（lock_held は自動で回復する扱い）
            err = "lock_held_timeout" if waits else "lock_held"
            cur.execute("update jobs set state='failed', finished_at=now(), error=%s where id=%s", (err, job_id))
            record_event(cur, "job.rejected", corr, job_id, {"reason": "advisory_lock_held"})
            if waits:
                record_event(cur, "job.finished", corr, job_id, {"state": "failed", "error": err, "waited_sec": int(lock_waited_sec(cur, job_id))})
            return
        kind = job.get("kind") or "implement"
        try:
            if reattaching:   # 起動は前の dispatcher が済ませた。来歴は job.started に残っている
                cur.execute("select payload from events where name='job.started' and job_id=%s order by id desc limit 1", (job_id,))
                prov = ((cur.fetchone() or {}).get("payload") or {}).get("provenance") or {}
                image = prov.get("worker_image") or WORKER_IMAGE
            elif kind in ("inspect", "judge", "body_fix", "design_review", "case_review", "committee_weekly", "committee_review", "ui_design_review"):   # 検査・judge・本文の追記・設計の診断（ADR 0004 / 0006 / 0022）・知見の委員会（ADR 0024）・デザイン検査役（ADR 0025）。issue_number は PR か固定 Issue の番号。ラベルは結果を見て付ける
                image = WORKER_IMAGE
            elif kind == "plan":   # プランナー（ADR 0025・0026）。読むだけなので製品によらず基本のイメージ
                swap_label(repo, issue, [LABELS["queued"]], [LABELS["running"]])
                image = WORKER_IMAGE
            elif kind == "ui_design":   # デザイナー役（ADR 0025）。製品によらず Chromium つきのイメージ
                swap_label(repo, issue, [LABELS["queued"]], [LABELS["running"]])
                image = UI_DESIGN_IMAGE
            else:
                swap_label(repo, issue, [LABELS["queued"]], [LABELS["running"]])
                image = image_for(repo, issue)
            if not reattaching:
                prov = provenance(repo, image)
                prov["model_tier"] = job["model_tier"] or MODEL_TIER
                prov["mode"], prov["mode_basis"] = judgment.current_mode(cur, ENV.get("FACTORY_MODE") or "attended", ENV.get("MODE_SCHEDULE", ""))   # 製造を始めた時点のモード（制御プレーンの切り替え > 時間帯 > 既定）
                record_event(cur, "job.started", corr, job_id, {"attempt": job["attempt"], "model_tier": job["model_tier"], "image": image, "provenance": prov})
            name = job.get("container_id") or f"factory-job-{job_id}"
            job_db_url = None
            if JOB_DB_ENABLED and kind not in ("inspect", "judge", "body_fix", "design_review", "case_review", "committee_weekly", "committee_review", "ui_design", "ui_design_review", "plan") and not reattaching:
                dbname = f"job_{job_id}"
                cur.execute(f'drop database if exists "{dbname}"')
                cur.execute(f'create database "{dbname}"')
                job_db_url = f"postgres://{JOB_DB_USER}:{JOB_DB_PASSWORD}@{JOB_DB_HOST}:5432/{dbname}"
            cont = continue_target(cur, job_id, repo, issue) if kind == "implement" else None   # 既存の工場の PR の続き（途中終了・衝突・CI 落ち・検査の指摘）
            signaled = bool(cont and cont["reason"] in continuation.SIGNALED)   # 合図の中身（CI のチェック名・指摘）を標準入力で渡す
            volume = continuation.volume_name(repo, issue) if kind == "implement" and continuation.HANDOFF_ENABLED else None   # チケットの引き継ぎのボリューム
            # --rm を付けない。dispatcher が落ちてもコンテナは動き続け、終わった後も出力と終了コードを残す。片付けは記録の後（ADR 0020）
            cmd = ["docker", "run", "--name", name, "--label", f"{reattach.JOB_LABEL}={job_id}",
                   *(["-v", f"{volume}:/handoff", "-e", "HANDOFF_DIR=/handoff"] if volume else []),
                   *(["-i"] if signaled else []),
                   *(["--network", JOB_DB_NETWORK, "-e", f"DATABASE_URL={job_db_url}"] if job_db_url else []),
                   "-e", f"JOB_ID={job_id}", "-e", f"REPO={repo}", "-e", f"ISSUE_NUMBER={issue}",
                   "-e", f"JOB_DEADLINE={int(time.time()) + JOB_TIMEOUT}",   # 打ち切りの時刻（epoch 秒）。ワーカーはエージェントをこれより前に止め、後処理の時間を残す
                   "-e", f"WORKER_DRY_RUN={ENV.get('WORKER_DRY_RUN', '')}",
                   "-e", f"MODEL_TIER={job['model_tier'] or MODEL_TIER}", "-e", f"MAX_TURNS={ENV.get('MAX_TURNS', '40')}",
                   "-e", f"GH_TOKEN={GH_TOKEN}", "-e", f"CLAUDE_CODE_OAUTH_TOKEN={ENV['CLAUDE_CODE_OAUTH_TOKEN']}",
                   *(["-e", f"CONTINUE_BRANCH={cont['branch']}", "-e", f"CONTINUE_PR={cont['pr_number']}", "-e", f"CONTINUE_REASON={cont['reason']}"] if cont else []),
                   *(["--entrypoint", "/usr/local/bin/clarify.sh"] if kind == "clarify" else []),
                   *(["--entrypoint", "/usr/local/bin/inspect.sh", "-e", f"PR_URL=https://github.com/{repo}/pull/{issue}"] if kind == "inspect" else []),
                   *(["-i", "--entrypoint", "/usr/local/bin/judge.sh", "-e", f"PR_URL=https://github.com/{repo}/pull/{issue}"] if kind == "judge" else []),
                   *(["-i", "--entrypoint", "/usr/local/bin/body_fix.sh", "-e", f"PR_URL=https://github.com/{repo}/pull/{issue}"] if kind == "body_fix" else []),
                   *(["-i", "--entrypoint", "/usr/local/bin/design_review.sh"] if kind == "design_review" else []),
                   *(["-i", "--entrypoint", "/usr/local/bin/case_review.sh"] if kind == "case_review" else []),
                   *(["-i", "--entrypoint", "/usr/local/bin/committee_weekly.sh"] if kind == "committee_weekly" else []),
                   *(["--entrypoint", "/usr/local/bin/committee_review.sh", "-e", f"PR_URL=https://github.com/{repo}/pull/{issue}"] if kind == "committee_review" else []),
                   *(["-i", "--entrypoint", "/usr/local/bin/ui_design.sh"] if kind == "ui_design" else []),
                   *(["-i", "--entrypoint", "/usr/local/bin/ui_design_review.sh"] if kind == "ui_design_review" else []),
                   *(["-i", "--entrypoint", "/usr/local/bin/plan.sh"] if kind == "plan" else []),
                   image]
            judge_inp = None
            if kind in ("judge", "body_fix", "design_review", "case_review", "committee_weekly", "ui_design", "ui_design_review", "plan") or signaled:   # 入力は supervisor / dispatcher が job.enqueued に残したもの（制御プレーンの記録だけで組み立てた JSON）
                cur.execute("select payload from events where name='job.enqueued' and job_id=%s order by id limit 1", (job_id,))
                ev = cur.fetchone()
                judge_inp = (ev["payload"] if ev else {}).get("input") or {}
                if kind == "case_review" and not reattaching:   # 後片付けのアーカイブからセッションの抜粋を足す（ADR 0023）。なければ記録だけで分析する
                    key = case.latest_archive_key(cur, judge_inp.get("correlation_id"))
                    if key:
                        try:
                            obj = store.get_object(BUCKET, key)
                            try:
                                judge_inp = case.attach_archive(judge_inp, obj.read())
                            finally:
                                obj.close()
                                obj.release_conn()
                        except Exception as e:  # noqa: BLE001
                            log("case archive read failed", key, repr(e))
                if kind == "ui_design_review" and not reattaching:   # 案（UXSpec とファイル）はオブジェクトストアから足す（ADR 0025）
                    judge_inp = design_line.review_input(cur, store_get, judge_inp)
            if reattaching:
                proc = reattach.Attached(name)
                record_event(cur, "job.reattached", corr, job_id, {"container": name})
                log(f"job {job_id} reattached to {name}")
            else:
                cur.execute("update jobs set container_id=%s where id=%s", (name, job_id))   # 起動の前に書く。上げ直した dispatcher がこの名前でつなぎ直す
                piped = kind in ("judge", "body_fix", "design_review", "case_review", "committee_weekly", "ui_design", "ui_design_review", "plan") or signaled
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if piped else None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                if piped:
                    proc.stdin.write(json.dumps(judge_inp, ensure_ascii=False))
                    proc.stdin.close()
            lines: list[str] = []
            deadline = (job["started_at"].timestamp() if reattaching and job.get("started_at") else time.time()) + JOB_TIMEOUT
            cancelled = False
            LAST_OUTPUT[job_id] = time.time()
            while True:
                line = proc.stdout.readline()
                if line:
                    lines.append(line)
                    LAST_OUTPUT[job_id] = time.time()
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
            LAST_OUTPUT.pop(job_id, None)
            # 止める印はログが出ない間にも付く（ops_loop がコンテナを止める）。終わった時点でもう一度見る
            cur.execute("select cancel_requested, preempted_for from jobs where id=%s", (job_id,))
            cj = cur.fetchone()
            cancelled = cancelled or cj["cancel_requested"]
            preempted_for = cj["preempted_for"] if cancelled else None
            # ログをオブジェクトストアへ（ワーカーには何も残さない）
            blob_key = f"jobs/{job_id}/worker.log"
            design_files, lines = design_line.extract_files(lines) if kind == "ui_design" else ({}, lines)   # 案のファイルはログに入れず別に置く
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
                state, err = "cancelled", ("preempted" if preempted_for else "cancel_requested")
            elif time.time() > deadline and proc.returncode not in (0,):
                state, err = "failed", "timeout"
            elif proc.returncode == 0 and result and result.get("pr_url"):
                state, err = "succeeded", None
            elif proc.returncode == 0 and result and result.get("kind") in ("design_review", "case_review", "committee_weekly", "committee_review", "ui_design", "ui_design_review", "plan") and result.get("ok"):
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
            if volume:
                saved = next((l for l in reversed(lines) if l.startswith("[worker] handoff saved")), None)
                if saved:   # 実装役の文脈をボリュームに残した（保留）。続きのジョブはここから再開する（ADR 0017）
                    record_event(cur, "handoff.saved", corr, job_id, {"volume": volume, "pr_url": (result or {}).get("pr_url"), "state": state,
                                                                     "agent_session": "agent_session=yes" in saved,
                                                                     "continue_reason": cont["reason"] if cont else None})
            if cont and cont["reason"] == "review_fix" and state != "succeeded" and not preempted_for:
                # 検査の指摘を続きのジョブで直せなかった。指摘は従来どおり差し戻しの Issue にしてラインは止めない（ADR 0017）
                try:
                    sig = judge_inp or {}
                    file_sendback(cur, repo, corr, job_id, sig.get("pr_url") or f"https://github.com/{repo}/pull/{cont['pr_number']}",
                                  sig.get("defects") or [], [], sig.get("mode") or "attended", fallback_from=job_id)
                except Exception as e:  # noqa: BLE001
                    log("review_fix fallback failed", job_id, repr(e))
            if result and result.get("pr_url") and (result.get("provisional_raw") or "").strip():
                # ワーカーの仮決め（ADR 0006）。記録の失敗でラインは止めない
                try:
                    mode, _ = judgment.current_mode(cur, ENV.get("FACTORY_MODE") or "attended", ENV.get("MODE_SCHEDULE", ""))   # 仮決めした時点のモード
                    items = judgment.record_provisional(cur, corr, job_id, result["pr_url"], result["provisional_raw"], mode)
                    if items and result.get("pr_number"):
                        swap_label(repo, int(result["pr_number"]), [], [PROVISIONAL_LABEL])
                except Exception as e:  # noqa: BLE001
                    log("provisional record failed", job_id, repr(e))
            if preempted_for:
                requeue_preempted(cur, job, preempted_for, (result or {}).get("cost_usd"))
            elif kind == "body_fix":
                record_event(cur, "pr.body_amended" if state == "succeeded" else "pr.body_amend_failed", corr, job_id,
                             {"pr_url": (result or {}).get("pr_url"), "defects": len(judge_inp.get("defects", [])), "chars": (result or {}).get("chars")})
            elif kind == "judge":
                if state == "succeeded" and (result or {}).get("verdicts"):
                    ev_mode = judge_inp.get("mode") or "attended"
                    cur.execute("select payload->>'mode_basis' as b from events where name='job.enqueued' and job_id=%s limit 1", (job_id,))
                    basis = (cur.fetchone() or {}).get("b") or "unknown"
                    summary = judgment.record_judged(cur, corr, job_id, result["pr_url"], judge_inp, result["verdicts"], ev_mode, basis)
                    act_on_judgment(cur, repo, corr, job_id, result["pr_url"], summary, ev_mode)
            elif kind == "design_review":
                # 設計の診断（ADR 0022）: 所見を残し、製品の固定 Issue に書く。人が読むためのもので、ワーカーには渡さない
                if state == "succeeded":
                    try:
                        design.record(cur, repo, issue, corr, job_id, result, gh, comment)
                    except Exception as e:  # noqa: BLE001  書けなくても所見は events に残る。ラインは止めない
                        log("design review publish failed", repo, repr(e))
            elif kind == "case_review":
                # 事例型の分析（ADR 0023）: 記録に残すだけ。作用先にもワーカーのプロンプトにも入れない
                if state == "succeeded":
                    case.record(cur, corr, job_id, judge_inp or {}, result)
            elif kind == "committee_weekly":
                # 知見の委員会の週 1 の定例（ADR 0024）: 固定 Issue にまとめを書き、見直し・担保・構造の変更を Issue にする
                if state == "succeeded":
                    try:
                        cur.execute("select payload from events where name='job.enqueued' and job_id=%s order by id limit 1", (job_id,))
                        enq = (cur.fetchone() or {}).get("payload") or {}
                        committee.weekly_record(cur, repo, issue, corr, job_id, result, enq.get("week") or "", gh, comment, int(enq.get("max_issues") or 4))
                    except Exception as e:  # noqa: BLE001  まとめは events に残る。ラインは止めない
                        log("committee weekly publish failed", repo, repr(e))
            elif kind == "committee_review":
                # 知見の委員会（ADR 0024）: 議決を残し、PR にまとめを書く。差し戻しは閉じ、人の判断は承認のラベル、担保は Issue に
                if state == "succeeded":
                    try:
                        committee.record(cur, repo, issue, corr, job_id, result, gh, comment)
                    except Exception as e:  # noqa: BLE001  議決は PR のブランチに残っている。作用の失敗でラインは止めない
                        log("committee act failed", result.get("pr_url"), repr(e))
            elif kind == "plan":
                # プランナー（ADR 0025・0026）: 判定を残し、経路のラベル・起票者への問い・人への回付のどれかを行う
                if state == "succeeded":
                    try:
                        pid = planner.record(cur, repo, issue, corr, job_id, result)
                        planner.act(cur, repo, issue, corr, job_id, pid, result, swap_label, comment)
                    except Exception as e:  # noqa: BLE001  判定は残っている。タスクは止めて人に知らせる
                        log("plan act failed", job_id, repr(e))
                        swap_label(repo, issue, [LABELS["running"]], [LABELS["failed"]])
                        comment(repo, issue, f"ジョブ {job_id}（プランナー）の後処理に失敗しました（{repr(e)[:300]}）。`factoryctl rerun {job_id}` で再実行できます。")
                else:
                    swap_label(repo, issue, [LABELS["running"]], [LABELS["failed"]])
                    comment(repo, issue, f"ジョブ {job_id}（プランナー）は {state}（{err}）。`factoryctl rerun {job_id}` で再実行できます。")
            elif kind in ("ui_design", "ui_design_review"):
                # デザインの線（ADR 0025）: 案を残して検査役に回す／検査役の判断で 承認・直し・人へ。失敗はタスクを止めて知らせる
                if state == "succeeded":
                    try:
                        if kind == "ui_design":
                            keys = design_line.store_files(store_put, job_id, design_files)
                            aid = design_line.record_draft(cur, repo, issue, corr, job_id, result, keys)
                            rid = design_line.enqueue(cur, repo, issue, corr, "ui_design_review", "tier-judge", {"design_artifact_id": aid})
                            log(f"ui design {aid} drafted, review job {rid}")
                        else:
                            mode, _ = judgment.current_mode(cur, ENV.get("FACTORY_MODE") or "attended", ENV.get("MODE_SCHEDULE", ""))
                            vid = design_line.record_review(cur, repo, issue, corr, job_id, judge_inp or {}, result, mode)
                            design_line.act_on_review(cur, repo, issue, corr, job_id, judge_inp or {}, result, vid, gh, comment, MODEL_TIER, store_get)
                    except Exception as e:  # noqa: BLE001  記録は残っている。タスクは止めて人に知らせる
                        log("design line act failed", job_id, repr(e))
                        swap_label(repo, issue, [LABELS["running"]], [LABELS["failed"]])
                        comment(repo, issue, f"ジョブ {job_id}（{kind}）の後処理に失敗しました（{repr(e)[:300]}）。`factoryctl rerun {job_id}` で再実行できます。")
                else:
                    swap_label(repo, issue, [LABELS["running"]], [LABELS["failed"]])
                    errs = "\n".join(f"- {x}" for x in ((result or {}).get("errors") or [])[:10])
                    comment(repo, issue, f"ジョブ {job_id}（{'デザイナー役' if kind == 'ui_design' else 'デザイン検査役'}）は {state}（{err}）。`factoryctl rerun {job_id}` で再実行できます。" + (f"\n\n{errs}" if errs else ""))
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
                                                  "rounds": result.get("rounds"), "converged": result.get("converged"),
                                                  "intent_match": result.get("intent_match"), **(result.get("request") or {})})))
                # converged（確定・往復 3 回以内・意図と一致）は P4 の完了条件の物差し。roadmap.yaml の clarify_3_rounds が読む
                record_event(cur, "request.clarified" if state == "succeeded" else "request.unresolved", corr, job_id,
                             {"rounds": result.get("rounds"), "requester": result.get("requester"), "cost_usd": result.get("cost_usd"),
                              "converged": result.get("converged"), "converge_rounds": result.get("converge_rounds"),
                              "intent_match": result.get("intent_match"), "missed": result.get("missed")})
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
                comment(repo, issue, (f"ジョブ {job_id} が PR {result['pr_url']} の続き（{CONTINUE_JA.get(cont['reason'], cont['reason'])}）を同じブランチに push しました。"
                                      if cont else f"ジョブ {job_id} が PR を作成しました: {result['pr_url']}")
                        + f"\n消費: {total_tokens:,} tokens（予算 {TOKEN_BUDGET:,}）")
            elif state == "failed" and result and result.get("pr_url"):
                # 途中終了のドラフトも PR として残す。建設者が仕上げてドラフトを外すと、supervisor が判定・検査・judge に回す
                cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload, blob_key) values ('PR',%s,%s,'implement',%s,%s)",
                            (corr, job_id, Jsonb({"repo": repo, "ticket_ids": [issue], "pr_url": result["pr_url"], "number": result.get("pr_number"),
                                                  "change_types": result.get("change_types", ["code_change"]), "provenance": prov, "incomplete": True}), blob_key))
                record_event(cur, "pr.opened", corr, job_id, {"pr_url": result["pr_url"], "incomplete": True}, dedupe_key=f"pr.opened:{result['pr_url']}")
                swap_label(repo, issue, [LABELS["running"]], [LABELS["failed"]])
                comment(repo, issue, f"ジョブ {job_id} は途中で終了しました（{err}）。差分はドラフト PR {result['pr_url']} に残してあります。範囲を満たしていれば建設者がドラフトを外してマージ、足りなければ `factoryctl rerun {job_id} --continue` でこのブランチから続きを（最初からやり直さない）。")
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
            reattach.remove(job.get("container_id") or f"factory-job-{job_id}")   # 記録を終えてから片付ける（ADR 0020）
            if JOB_DB_ENABLED:
                try:
                    cur.execute(f'drop database if exists "job_{job_id}" with (force)')   # ワーカーの状態を残さない
                except Exception as e:  # noqa: BLE001
                    log("job db drop failed", job_id, repr(e))
            cur.execute("select pg_advisory_unlock(hashtext(%s))", (lock_key,))


def requeue_preempted(cur, job: dict, preempted_for: int, cost_usd):
    """割り込みで止めたジョブを順番待ちに戻す（ADR 0009）。止めた時点までの作業は捨てる。捨てた時間は job.preempted に残す"""
    repo, issue, corr = job["repo"], job["issue_number"], job["correlation_id"]
    cur.execute("select extract(epoch from now() - started_at)::int as sec from jobs where id=%s", (job["id"],))
    sec = cur.fetchone()["sec"]
    cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind, ticket_id, priority) "
                "select correlation_id, repo, issue_number, attempt + 1, 'preempt', model_tier, kind, ticket_id, priority from jobs where id=%s returning id",
                (job["id"],))
    new_id = cur.fetchone()["id"]
    record_event(cur, "job.preempted", corr, job["id"], {"preempted_for": preempted_for, "discarded_sec": sec, "discarded_cost_usd": cost_usd,
                                                        "requeued_as": new_id})
    record_event(cur, "job.enqueued", corr, new_id, {"by": "preempt", "attempt": job["attempt"] + 1, "previous": job["id"],
                                                     "ticket_id": job.get("ticket_id"), "priority": job.get("priority"),
                                                     **continuation.carry_over(cur, job["id"])})   # 続ける先と合図の中身を引き継ぐ
    if (job.get("kind") or "implement") in ops.PREEMPTIBLE_KINDS:
        swap_label(repo, issue, [LABELS["running"]], [LABELS["queued"]])
        comment(repo, issue, f"緊急のジョブ {preempted_for} を先に流すため、ジョブ {job['id']} を止めて順番待ちに戻しました（ジョブ {new_id}）。止めた時点までの作業は捨てて、最初からやり直します。")
    log(f"job {job['id']} preempted for {preempted_for}, requeued as {new_id}")


def ops_once():
    with db() as conn, conn.cursor() as cur:
        # ラインの一時停止中は割り込まない（止めても次を流せない。ADR 0020）
        for victim, urgent in ([] if drain.active(cur) else ops.preempt_victims(cur, MAX_PARALLEL)):
            ops.stop_job(cur, victim["id"], "dispatcher", f"緊急のジョブ {urgent['id']} を先に流す", preempted_for=urgent["id"])
            log(f"preempt job {victim['id']} (priority {victim['priority']}) for job {urgent['id']} (priority {urgent['priority']})")
        # 止める印が付いた作業中のジョブのコンテナを止める（コンソール・factoryctl・割り込みの共通の出口）
        cur.execute("select id, container_id from jobs where state='running' and cancel_requested and container_id is not null")
        for r in cur.fetchall():
            subprocess.run(["docker", "kill", r["container_id"]], capture_output=True)


def ops_loop():
    while True:
        try:
            ops_once()
        except Exception as e:  # noqa: BLE001  操作の失敗でラインを止めない
            log("ops error", repr(e))
        time.sleep(OPS_INTERVAL)


def run_loop(reattached: list[dict] | None = None):
    sem = threading.BoundedSemaphore(MAX_PARALLEL)
    for j in reattached or []:   # 上げ直す前から動いているジョブ（ADR 0020）。枠を数えるため、ほかより先に枠を取る
        got = sem.acquire(blocking=False)   # 上限を下げて上げ直したときは枠を超えて流れる（つなぎ直しは捨てない）

        def _resume(j=j, got=got):
            try:
                run_job(j, reattaching=True)
            finally:
                if got:
                    sem.release()
        threading.Thread(target=_resume, daemon=True).start()
    while True:
        sem.acquire()
        job = None
        try:
            with db() as conn, conn.cursor() as cur:
                if drain.active(cur):   # ラインの一時停止中は取り出さない（ADR 0020）。実行中のジョブはそのまま流す
                    sem.release()
                    time.sleep(3)
                    continue
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
    # overlay の shadow のルールの指摘は、judge が裁いた記録（的中率の材料）だけ残し、差し戻さない（ADR 0013）
    live = [d for d in summary["adopted_defects"] if not d.get("shadow")]
    # PR 本文だけの指摘は Issue を切らず、本文の追記で直す（2026-10-27 オーナー）。差し戻しの回数にも数えない
    body_only = [d for d in live if is_body_only(d)]
    if body_only:
        enqueue_body_fix(cur, repo, corr, pr_url, pr_number, body_only)
    to_fix = [d for d in live if not is_body_only(d)]
    if to_fix and REVIEW_FIX:
        # マージ前の指摘は差し戻しの Issue を切らず、同じ担当の続きのジョブで直す（ADR 0017）。上限・工場のブランチでないときは差し戻しへ
        try:
            pr = gh("GET", f"repos/{repo}/pulls/{pr_number}")
            jid = None
            if pr["state"] == "open" and (pr["head"].get("repo") or {}).get("full_name") == repo:
                signal = {"pr_url": pr_url, "mode": mode, "head_sha": pr["head"]["sha"],
                          "defects": [{k: d.get(k) for k in ("rubric_id", "severity", "location", "evidence", "remediation_hint")} for d in to_fix]}
                jid = continuation.enqueue(cur, repo=repo, pr_number=pr_number, pr_url=pr_url, branch=pr["head"]["ref"], head_sha=pr["head"]["sha"],
                                           reason="review_fix", by="judge", signal=signal)
            if isinstance(jid, int):
                comment(repo, pr_number, f"judge（モード {mode}）が採用した指摘 {len(to_fix)} 件を、同じ担当の続きのジョブ {jid} で直します（差し戻しの Issue は切りません）。")
                log("review_fix enqueued", pr_url, "job", jid)
                to_fix = []
            elif jid == "limit":
                record_event(cur, "continue.limit_reached", corr, job_id, {"pr_url": pr_url, "reason": "review_fix", "max": continuation.CONTINUE_MAX})
        except Exception as e:  # noqa: BLE001
            log("review_fix enqueue failed", pr_url, repr(e))
    file_sendback(cur, repo, corr, job_id, pr_url, to_fix, summary["replaced"], mode)


def file_sendback(cur, repo: str, corr: str, job_id: int, pr_url: str, to_fix: list[dict], replaced: list[dict], mode: str, fallback_from: int | None = None):
    """差し戻しの Issue を起票する（ADR 0006）。マージ前の指摘は続きのジョブで直すので、ここに来るのは仮決めの差し替えと、
    続きのジョブで直せなかった・上限に達した指摘（ADR 0017）"""
    pr_number = int(pr_url.rsplit("/", 1)[1])
    items = to_fix + replaced
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
    for d in to_fix:   # 本文だけの指摘は body_fix に回したので載せない
        n += 1
        lines += ["", f"### {n}. 検査の指摘 [{d.get('rubric_id', '?')} / {d.get('severity', '?')}] {d.get('location', '')}", _quote(d.get("evidence", "")),
                  "", "直し方の手がかり:", _quote(d.get("remediation_hint", ""))]
    for r in replaced:
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
                                                     "mode": mode, "defects": len(to_fix), "replaced": len(replaced),
                                                     **({"fallback_from_job": fallback_from} if fallback_from else {})})
    comment(repo, pr_number, f"{'続きのジョブで直せなかった指摘を引き取り、' if fallback_from else ''}judge（モード {mode}）が指摘 {len(to_fix)} 件と仮決めの差し替え {len(replaced)} 件を採用し、差し戻しを {issue['html_url']} に起票しました。"
            + ("依存が閉じたら supervisor が流します。" if mode == "unattended" else "流すときは Issue のラベル factory:fix-proposed を factory:planned に替えてください。"))


# ------------------------------------------------------------------ 番人（ADR 0005）。トークンを使わず、制御プレーンの状態だけ見る
def classify_failure(p: dict) -> tuple[str, str, str] | None:
    """失敗したジョブを (分類, 何が止まったか, 待っているもの) にする。自動で回復するものは None"""
    err = p.get("error") or ""
    if err in ("orphaned_by_restart", "killed_by_shutdown", "lock_held", "cancel_requested"):
        return None
    if p.get("usage_limited"):
        return "budget", "契約の使用量上限でジョブが止まった", "使用量上限のリセット後の rerun"
    if err == "lock_held_timeout":
        return "factory-bug", f"続きのジョブが同じチケットの錠を {LOCK_WAIT_MAX_SEC // 60} 分待っても取れなかった", "錠を握っているジョブの調査と rerun"
    if err == "timeout":
        return "budget", f"時間の上限（{JOB_TIMEOUT // 60} 分）で打ち切られた", "チケットの分割か rerun"
    if err.startswith("incomplete"):
        return "budget", "ターンか時間の上限で途中終了した（ドラフト PR）", "建設者の仕上げか rerun"
    if err == "unresolved_after_max_rounds":
        return "ticket", "要求が質問の上限まで往復しても確定しなかった", "要求者か建設者の判断"
    return "factory-bug", "ジョブが失敗した（未分類。人が分類を直す）", "原因の調査と rerun"


def budget_paused(cur) -> dict | None:
    """supervisor が枠の予算の使い切りで投入を止めているか。止めた記録（supervisor.budget_paused）がその後の投入より新しく、
    枠の長さ（最大 5 時間）を過ぎていなければ止めている。枠の設定は supervisor の側にあるので、記録だけから判断する"""
    cur.execute("select occurred_at, payload from events where name='supervisor.budget_paused' and occurred_at > now() - interval '6 hours' "
                "and occurred_at > coalesce((select max(occurred_at) from events where name='supervisor.enqueued'), '-infinity') "
                "order by id desc limit 1")
    return cur.fetchone()


def line_supply(cur) -> dict:
    """ラインの段の残り。投入待ち（planned で保留でない）と、そのうち依存が片付いてすぐ流せる数"""
    cur.execute("select count(*) as planned, count(*) filter (where cardinality(waiting_on) = 0) as ready "
                "from tickets where state='planned' and not held")
    r = cur.fetchone()
    return {"planned": r["planned"], "ready": r["ready"], "low_below": LINE_LOW_BELOW,
            "low": LINE_LOW_BELOW > 0 and r["planned"] < LINE_LOW_BELOW}


def line_low_step(cur, draining) -> None:
    """段の残りが少なくなったら 1 度だけ知らせ、積み足されたら戻ったと記録する。停止ではない（ラインはまだ流れている）ので
    stoppages には開かない。プランナー（P4）ができるまで、建設者が段を積む合図にする（2026-10-31）"""
    if draining:
        return
    sup = line_supply(cur)
    cur.execute("select name from events where name in ('watcher.line_low','watcher.line_ok') order by id desc limit 1")
    last = cur.fetchone()
    is_low = bool(last and last["name"] == "watcher.line_low")
    if sup["low"] and not is_low:
        record_event(cur, "watcher.line_low", None, None, {"planned": sup["planned"], "ready": sup["ready"], "low_below": LINE_LOW_BELOW})
        stoppage.post_notice(f"**段の残りが少なくなりました**: 投入待ち {sup['planned']} 件（{LINE_LOW_BELOW} 件を切った）。"
                             "次の段を `factory:planned` で積んでください。積まないとラインが空になります。", cur)
    elif not sup["low"] and is_low:
        record_event(cur, "watcher.line_ok", None, None, {"planned": sup["planned"], "ready": sup["ready"]})


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
        budget_open = stoppage.open_keys(cur, "budget:")
        draining = drain.state(cur)
        paused = budget_paused(cur)
        if j["r"] == 0 and j["q"] == 0:
            if paused:   # 枠の予算を使い切って supervisor が投入を止めている。ラインが空（idle）と分ける（2026-10-31）
                for k in idle_open:
                    stoppage.close_stoppage(cur, k, remedy="予算の枠の使い切りによる停止として開き直した")
                if not budget_open and not draining:
                    p = paused["payload"]
                    stoppage.open_stoppage(cur, f"budget:{p.get('window_start')}", "budget",
                                           f"使用量の枠の予算を使い切ったので新しい段を投入していない（{p.get('spent_usd')} / ${p.get('budget_usd')}）",
                                           waiting_for="次の使用量の枠の始まり（supervisor が自分で投入を再開する）", started_at=paused["occurred_at"])
            elif not idle_open and not draining and j["last"] and j["long_idle"]:   # 一時停止中は drain: で知らせる
                stoppage.open_stoppage(cur, f"idle:{j['last']:%Y%m%dT%H%M%S}", "idle", "ラインが空（実行中・投入可能のジョブがない）",
                                       waiting_for="次の Ticket（factory:implement / factory:request のラベル）か、承認待ちの PR の判断", started_at=j["last"])
        else:
            for k in idle_open:
                stoppage.close_stoppage(cur, k, remedy="ジョブが投入された")
        if not paused or j["r"] or j["q"]:
            for k in budget_open:
                stoppage.close_stoppage(cur, k, remedy="次の枠に入り、投入を再開した")

        # 1b) ラインの一時停止（ADR 0020）が DRAIN_MIN 分続いている: 入れ替えの後の再開を忘れていないか
        drain_open = stoppage.open_keys(cur, "drain:")
        if draining and draining["minutes"] >= DRAIN_MIN:
            if not drain_open:
                stoppage.open_stoppage(cur, f"drain:{draining['since']:%Y%m%dT%H%M%S}", "human",
                                       f"ラインが一時停止のまま {DRAIN_MIN} 分（{draining['by']}: {draining['reason'] or '理由なし'}）",
                                       waiting_for="工場の入れ替えと再開（factoryctl resume か操業コンソール）", started_at=draining["since"])
        elif not draining:
            for k in drain_open:
                stoppage.close_stoppage(cur, k, remedy="ラインを再開した")

        # 1c) 段の残りが少ない（停止の手前の知らせ）
        line_low_step(cur, draining)

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

        # 5) 実行中のジョブの滞り: 出力が STALL_MIN 分止まっている（打ち切りを待たずに知らせる。完了まで毎周期見る）
        cur.execute("select id, correlation_id, started_at from jobs where state='running'")
        stalled = {}
        for r in cur.fetchall():
            last = LAST_OUTPUT.get(r["id"])
            if last is not None and time.time() - last > STALL_MIN * 60:
                stalled[r["id"]] = r
        for r in stalled.values():
            stoppage.open_stoppage(cur, f"stall:{r['id']}", "factory-bug", f"ジョブの出力が {STALL_MIN} 分止まっている",
                                   waiting_for="出力の再開か、dispatcher の調査（factoryctl stop / rerun）", started_at=r["started_at"],
                                   correlation_id=r["correlation_id"], job_id=r["id"])
        for k in stoppage.open_keys(cur, "stall:"):
            if int(k.split(":", 1)[1]) not in stalled:
                stoppage.close_stoppage(cur, k, remedy="出力が再開した、またはジョブが終了した")

        # 6) 結果の出た PR に紐づく停止（judge: / sendback: / ci: / merge:）が開いたままなら閉じる。
        #    judge がマージの直後に終わると、supervisor の「PR が閉じたら閉じる」をすり抜けて開いたまま残る（2026-10-31、rally #253）
        for k in stoppage.open_keys(cur, ""):
            prefix, _, url = k.partition(":")
            if prefix not in ("judge", "sendback", "ci", "merge") or not url.startswith("https://"):
                continue
            cur.execute("select name from events where name in ('pr.merged','pr.closed_unmerged') and payload->>'pr_url'=%s limit 1", (url,))
            r = cur.fetchone()
            if r:
                stoppage.close_stoppage(cur, k, remedy="PR が" + ("マージされた" if r["name"] == "pr.merged" else "閉じられた") + "（結果の出た PR の停止は閉じる）")

        # 7) マージ待ち: supervisor が「工場はマージしない」（execute=false）と記録した PR が、MERGE_WAIT_MIN 分たってもマージも閉じもされていない。
        #    ラインが空（idle）と分けて人の判断待ち（human）で開き、見回りがマージ役を起こせるようにする（2026-10-31 の夜間停止）
        cur.execute("select distinct on (payload->>'pr_url') payload->>'pr_url' as pr_url, occurred_at, correlation_id, payload, "
                    "now() - occurred_at > make_interval(mins => %s) as long_wait from events "
                    "where name='supervisor.merge_decided' order by payload->>'pr_url', id desc", (MERGE_WAIT_MIN,))
        waiting = {}
        for d in cur.fetchall():
            if d["payload"].get("execute"):
                continue
            cur.execute("select 1 from events where name in ('pr.merged','pr.closed_unmerged') and payload->>'pr_url'=%s", (d["pr_url"],))
            if cur.fetchone() is None:
                waiting[f"merge:{d['pr_url'].rsplit('/', 1)[-1]}"] = d
        for k, d in waiting.items():
            if d["long_wait"]:
                why = "承認の表" if d["payload"].get("table") != "auto" else f"phase {d['payload'].get('phase')} の権限（factory_grant）"
                stoppage.open_stoppage(cur, k, "human", f"マージ待ちの PR（{d['pr_url']}）。{why}で工場は自分でマージしない",
                                       waiting_for="マージ役（builder のスレッドか人）のマージか差し戻し", started_at=d["occurred_at"],
                                       correlation_id=d["correlation_id"])
        for k in stoppage.open_keys(cur, "merge:"):
            if k not in waiting:
                stoppage.close_stoppage(cur, k, remedy="PR がマージか閉じられた")


def watch_loop():
    last_cleanup = 0.0
    while True:
        try:
            watch_once()
        except Exception as e:  # noqa: BLE001  番人の失敗でラインを止めない
            log("watch error", repr(e))
        if time.time() - last_cleanup > 3600:   # 済んだチケットの引き継ぎのボリュームを消す（1 時間に 1 回。ADR 0017）
            last_cleanup = time.time()
            try:
                with db() as conn, conn.cursor() as cur:
                    n = continuation.cleanup(cur, log)
                    if n:
                        log(f"handoff volumes removed: {n}")
            except Exception as e:  # noqa: BLE001
                log("handoff cleanup error", repr(e))
        time.sleep(WATCH_INTERVAL)


def tidy_loop():
    """後片付け（ADR 0021）。ラインとは別の糸で、済んだチケットのボリュームをオブジェクトストアに置いて消す。
    ジョブの枠を使わず、ラインの完了を待たせない（片付けは成果物に含めない）"""
    def put(key: str, data: bytes):
        store.put_object(BUCKET, key, io.BytesIO(data), len(data), content_type="application/gzip")
    while True:
        try:
            with db() as conn, conn.cursor() as cur:
                n = tidy.run(cur, WORKER_IMAGE, put, log)
                if n:
                    log(f"tidy: archived and removed {n} volumes")
        except Exception as e:  # noqa: BLE001  片付けの失敗でラインを止めない
            log("tidy error", repr(e))
        time.sleep(tidy.TIDY_INTERVAL)


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
        event = self.headers.get("X-GitHub-Event", "")
        if event == "issues" and ev.get("action") == "labeled" and ev.get("label", {}).get("name") == TRIGGER_LABEL:
            repo, n = ev["repository"]["full_name"], ev["issue"]["number"]
            if repo in REPOS and enqueue(repo, n, "webhook", issue_json=ev["issue"]):
                swap_label(repo, n, [TRIGGER_LABEL], [LABELS["queued"]])
        self.send_response(204); self.end_headers()
        # PR の動き（push・CI・レビュー）は PR の見張りがイベントにする（ADR 0015）。payload は合図で、事実は見張りが取り直す
        if event in pr_events.WEBHOOK_EVENTS:
            threading.Thread(target=pr_events.handle_webhook, args=(event, ev), daemon=True).start()

    def log_message(self, *a):  # 静かに
        pass


def recover() -> list[dict]:
    """dispatcher の再起動で取り残された running ジョブを片付ける（ADR 0020）。
    コンテナが残っていれば（動いていても終わっていても）つなぎ直すジョブとして返す。run_loop が枠を取ってから run_job(reattaching=True) で続ける。
    コンテナが残っていなければ、ワーカーは最初からやり直せる（冪等）ので failed にし、attempt+1 で再投入する。
    コンテナが残っていても、シグナルで終わっていた（Docker ごと止まった）ものは同じく積み直す。
    記録を終えたのに残っているコンテナ（記録の後、片付けの前に落ちた）は消す。"""
    containers = reattach.job_containers()
    resume: list[dict] = []
    with db() as conn, conn.cursor() as cur:
        cur.execute("select * from jobs where state='running' order by id")
        for j in cur.fetchall():
            name = j["container_id"]
            error = "orphaned_by_restart"
            if name and (j["id"] in containers or reattach.exists(name)):
                if not reattach.killed(name):
                    resume.append(j)
                    continue
                # Docker ごと止まってワーカーがシグナルで終わっていた。つなぎ直すと失敗として記録されるので、積み直す（2026-10-31）
                error = "killed_by_shutdown"
                reattach.remove(name)
                containers.pop(j["id"], None)
            cur.execute("update jobs set state='failed', finished_at=now(), error=%s where id=%s", (error, j["id"]))
            record_event(cur, "job.finished", j["correlation_id"], j["id"], {"state": "failed", "error": error})
            cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind, ticket_id, priority) "
                        "values (%s,%s,%s,%s,'recover',%s,%s,%s,%s) returning id",
                        (j["correlation_id"], j["repo"], j["issue_number"], j["attempt"] + 1, MODEL_TIER, j["kind"], j["ticket_id"], j["priority"]))
            new_id = cur.fetchone()["id"]
            record_event(cur, "job.enqueued", j["correlation_id"], new_id, {"by": "recover", "attempt": j["attempt"] + 1, "previous": j["id"],
                                                                         **continuation.carry_over(cur, j["id"])})   # 続ける先と入力を引き継ぐ
            log(f"recovered job {j['id']} -> re-enqueued as {new_id}")
        running = {j["id"] for j in resume}
        for jid, name in containers.items():
            if jid not in running:
                reattach.remove(name)
                log(f"removed leftover container {name}")
    return resume


def main():
    with db() as conn, conn.cursor() as cur:
        cur.execute("select 1")
    reattached = recover()
    if not store.bucket_exists(BUCKET):
        store.make_bucket(BUCKET)
    log(f"dispatcher up: repos={REPOS} label={TRIGGER_LABEL} parallel={MAX_PARALLEL} image={WORKER_IMAGE}")
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=ops_loop, daemon=True).start()
    if ENV.get("WATCH_ENABLED", "1") == "1":
        threading.Thread(target=watch_loop, daemon=True).start()
    if tidy.TIDY_ENABLED:
        threading.Thread(target=tidy_loop, daemon=True).start()
    pr_events.start(threading)
    if ENV.get("WEBHOOK_SECRET"):
        srv = ThreadingHTTPServer(("0.0.0.0", int(ENV.get("WEBHOOK_PORT", "8787"))), Hook)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log("webhook listening")
    run_loop(reattached)


if __name__ == "__main__":
    sys.exit(main())
