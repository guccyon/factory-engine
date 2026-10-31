"""PR の見張り（ADR 0015）。工場の PR に起きた動きを GitHub から拾い、抽象化したイベントとして events に発行する。

- 決定論的。LLM は使わず、GitHub の今の状態（snapshot）だけからイベントを決める
- 入口は 2 つ。Webhook（商用・クラウドで即時）とポーリング（オンプレミスや手元で PR_EVENTS_INTERVAL 秒ごと）。
  どちらも「見に行く合図」で、真実は snapshot。同じ事実は dedupe_key で 1 回しか発行しないので、両方動いても二重にならない
- オーケストレーター（supervisor）は GitHub を見ずに、このイベントを読んで動く
- dispatcher の中で動く（スレッド）。dispatcher が落ちれば止まるが、ポーリングは次の起動で取りこぼしを拾う

発行するイベント（definitions/events.yaml）:
  pr.head_updated   新しい head を見た（PR を開いた・push した）
  pr.ci_failed      その head の CI が落ちた（failure / timed_out / cancelled のチェック名つき）
  pr.ci_passed      その head の CI がすべて通った
  pr.conflicted     その head が base と衝突している（mergeable_state=dirty）
  pr.review_submitted  レビューが付いた（approved / changes_requested / commented）
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ENV = os.environ
DATABASE_URL = ENV.get("DATABASE_URL", "postgres://factory:factory@postgres:5432/factory")
GH_TOKEN = ENV.get("GH_TOKEN", "")
REPOS = [r.strip() for r in ENV.get("FACTORY_REPOS", "").split(",") if r.strip()]
INTERVAL = int(ENV.get("PR_EVENTS_INTERVAL_SEC", "60"))
SOURCE = ENV.get("PR_EVENTS_SOURCE", "both")   # both | poll | webhook（webhook だけなら見逃しを拾えないので、商用でも both を薦める）
FAILED = ("failure", "timed_out", "cancelled")
PR_RE = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)$")
WEBHOOK_EVENTS = ("pull_request", "pull_request_review", "check_suite", "check_run", "workflow_run")


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), "[pr_events]", *a, flush=True)


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)


def gh(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(f"https://api.github.com/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json", "User-Agent": "factory-pr-events"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        return json.loads(data) if data else None


def emit(cur, name: str, corr: str | None, payload: dict, dedupe_key: str) -> bool:
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values (%s,%s,%s,%s) on conflict (dedupe_key) do nothing returning id",
                (name, corr, Jsonb(payload), dedupe_key))
    return cur.fetchone() is not None


# ------------------------------------------------------------------ 見張る対象（工場が作った、まだ結果の出ていない PR）
def open_factory_prs(cur) -> list[dict]:
    cur.execute("select distinct on (payload->>'pr_url') correlation_id, payload->>'pr_url' as url from artifacts "
                "where type='PR' and payload->>'pr_url' is not null order by payload->>'pr_url', id desc")
    out = []
    for r in cur.fetchall():
        m = PR_RE.match(r["url"])
        if not m or m.group(1) not in REPOS:
            continue
        cur.execute("select 1 from events where name in ('pr.merged','pr.closed_unmerged') and payload->>'pr_url'=%s", (r["url"],))
        if cur.fetchone():
            continue
        out.append({"repo": m.group(1), "number": int(m.group(2)), "url": r["url"], "corr": r["correlation_id"]})
    return out


def lookup(cur, repo: str, number: int) -> dict | None:
    url = f"https://github.com/{repo}/pull/{number}"
    cur.execute("select correlation_id from artifacts where type='PR' and payload->>'pr_url'=%s order by id desc limit 1", (url,))
    r = cur.fetchone()
    return {"repo": repo, "number": number, "url": url, "corr": r["correlation_id"]} if r else None


# ------------------------------------------------------------------ snapshot → イベント（決定論的）
def snapshot(repo: str, number: int) -> dict:
    pr = gh("GET", f"repos/{repo}/pulls/{number}")
    sha = pr["head"]["sha"]
    runs = (gh("GET", f"repos/{repo}/commits/{sha}/check-runs?per_page=100") or {}).get("check_runs", [])
    reviews = gh("GET", f"repos/{repo}/pulls/{number}/reviews?per_page=100") or []
    return {"state": pr["state"], "draft": bool(pr.get("draft")), "head_sha": sha, "mergeable_state": pr.get("mergeable_state"),
            "runs": [{"name": r["name"], "status": r.get("status"), "conclusion": r.get("conclusion")} for r in runs],
            "reviews": [{"id": r["id"], "state": (r.get("state") or "").lower(), "author": (r.get("user") or {}).get("login"),
                         "submitted_at": r.get("submitted_at"), "commit_id": r.get("commit_id")} for r in reviews]}


def observe(cur, p: dict, source: str) -> list[str]:
    """PR を 1 回見て、新しく分かった事実だけイベントにする。返り値は発行したイベント名"""
    s = snapshot(p["repo"], p["number"])
    if s["state"] == "closed":
        return []   # マージ・閉じは supervisor が判定の記録と一緒に残す（pr.merged / pr.closed_unmerged）
    url, sha, out = p["url"], s["head_sha"], []
    base = {"pr_url": url, "pr_id": f"{p['repo']}#{p['number']}", "head_sha": sha, "source": source}
    if emit(cur, "pr.head_updated", p["corr"], {**base, "draft": s["draft"]}, f"pr.head_updated:{url}@{sha}"):
        out.append("pr.head_updated")
    runs = s["runs"]
    failed = sorted({r["name"] for r in runs if r["conclusion"] in FAILED})
    if failed:
        if emit(cur, "pr.ci_failed", p["corr"], {**base, "failed": failed}, f"pr.ci_failed:{url}@{sha}"):
            out.append("pr.ci_failed")
    elif runs and all(r["status"] == "completed" for r in runs):
        if emit(cur, "pr.ci_passed", p["corr"], {**base, "checks": sorted({r["name"] for r in runs})}, f"pr.ci_passed:{url}@{sha}"):
            out.append("pr.ci_passed")
    if s["mergeable_state"] == "dirty":
        if emit(cur, "pr.conflicted", p["corr"], base, f"pr.conflicted:{url}@{sha}"):
            out.append("pr.conflicted")
    for r in s["reviews"]:
        if r["state"] in ("approved", "changes_requested", "commented"):
            if emit(cur, "pr.review_submitted", p["corr"], {**base, "head_sha": r["commit_id"] or sha, "review_id": r["id"], "state": r["state"],
                                                            "author": r["author"], "submitted_at": r["submitted_at"]}, f"pr.review:{url}#{r['id']}"):
                out.append("pr.review_submitted")
    if out:
        log(source, url, sha[:7], ", ".join(out))
    return out


# ------------------------------------------------------------------ 入口 1: ポーリング
def poll_once():
    with db() as conn, conn.cursor() as cur:
        for p in open_factory_prs(cur):
            try:
                observe(cur, p, "poll")
            except Exception as e:  # noqa: BLE001  1 本の失敗で他を止めない
                log("poll error", p["url"], repr(e))


def poll_loop():
    while True:
        try:
            poll_once()
        except Exception as e:  # noqa: BLE001
            log("poll error", repr(e))
        time.sleep(INTERVAL)


# ------------------------------------------------------------------ 入口 2: Webhook（dispatcher の受け口から呼ばれる。payload は合図としてだけ使い、事実は snapshot で取り直す）
def handle_webhook(event: str, ev: dict):
    if event not in WEBHOOK_EVENTS or SOURCE == "poll":
        return
    repo = (ev.get("repository") or {}).get("full_name")
    if repo not in REPOS:
        return
    numbers: set[int] = set()
    if event in ("pull_request", "pull_request_review") and ev.get("pull_request"):
        numbers.add(int(ev["pull_request"]["number"]))
    for key in ("check_suite", "check_run", "workflow_run"):
        for pr in (ev.get(key) or {}).get("pull_requests") or []:
            numbers.add(int(pr["number"]))
    with db() as conn, conn.cursor() as cur:
        targets = [p for p in (lookup(cur, repo, n) for n in numbers) if p]
        if not targets:   # pull_requests が空（fork など）のときは head の sha で探す
            sha = ((ev.get("check_suite") or ev.get("workflow_run") or {}).get("head_sha")) or ((ev.get("check_run") or {}).get("head_sha"))
            if sha:
                cur.execute("select payload->>'pr_url' as url from events where name='pr.head_updated' and payload->>'head_sha'=%s order by id desc limit 1", (sha,))
                r = cur.fetchone()
                m = PR_RE.match(r["url"]) if r else None
                if m:
                    p = lookup(cur, m.group(1), int(m.group(2)))
                    if p:
                        targets.append(p)
        for p in targets:
            try:
                observe(cur, p, "webhook")
            except Exception as e:  # noqa: BLE001
                log("webhook error", p["url"], repr(e))


def start(threading_module):
    """dispatcher から呼ぶ。ポーリングのスレッドを起こす（PR_EVENTS_SOURCE=webhook なら起こさない）"""
    if ENV.get("PR_EVENTS_ENABLED", "1") != "1":
        return
    if SOURCE != "webhook":
        threading_module.Thread(target=poll_loop, daemon=True).start()
    log(f"up: source={SOURCE} interval={INTERVAL}s repos={REPOS}")
