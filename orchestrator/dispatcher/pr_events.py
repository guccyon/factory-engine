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
  pr.ci_unstarted   その head のワークフローが起動に失敗した（ジョブが 0 本のまま失敗）。再実行は supervisor の ci_rerun_step
  pr.review_submitted  レビューが付いた（approved / changes_requested / commented）

ADR 0050 で、内ループの処理が GitHub に直接聞かずに済むよう、次の 2 つも同じポーリングで見て記録する:
  github.pr_closed     使い捨て環境が立っている PR（工場の PR でなくてもよい）が閉じた（ephemeral.watched）
  github.issue_closed  着手中のチケットの Issue が閉じた（ticket_sync.observe。TICKET_SYNC_SEC ごと）
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone

import defect
import ephemeral
import ghapi
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import ticket_sync

ENV = os.environ
DATABASE_URL = ENV.get("DATABASE_URL", "postgres://factory:factory@postgres:5432/factory")
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
    return ghapi.request(method, path, body, user_agent="factory-pr-events")   # GET は ETag で条件付き（上限に数えない 304）


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
    wf = (gh("GET", f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100") or {}).get("workflow_runs", [])
    reviews = gh("GET", f"repos/{repo}/pulls/{number}/reviews?per_page=100") or []
    return {"state": pr["state"], "draft": bool(pr.get("draft")), "head_sha": sha, "mergeable_state": pr.get("mergeable_state"),
            "runs": [{"name": r["name"], "status": r.get("status"), "conclusion": r.get("conclusion"),
                      "suite": (r.get("check_suite") or {}).get("id")} for r in runs],
            "workflows": [{"id": w["id"], "name": w.get("name"), "status": w.get("status"), "conclusion": w.get("conclusion"),
                           "attempt": w.get("run_attempt") or 1, "suite": w.get("check_suite_id")} for w in wf],
            "reviews": [{"id": r["id"], "state": (r.get("state") or "").lower(), "author": (r.get("user") or {}).get("login"),
                         "submitted_at": r.get("submitted_at"), "commit_id": r.get("commit_id")} for r in reviews]}


def unstarted_step(cur, p: dict, s: dict, base: dict, out: list) -> tuple[set, bool]:
    """起動に失敗したワークフロー（ジョブが 0 本のまま失敗。GitHub 側の一過性のことがある）はチェックが付かないので、チェックだけ見ると
    「全部通った」に見える。けれど必須のチェックがそろわず PR は clean にならず、検査も judge も積まれないまま黙って止まる
    （2026-11-10 rally #1358、本体の CI 8 本が起動せず約 24 時間チケットの枠を塞いだ）。
    見張りは見た事実を pr.ci_unstarted に記録するだけ。再実行は内ループの処理（supervisor の ci_rerun_step）が記録を読んで頼む（ADR 0050 の決定 2）。
    再実行（試行 2 回目以降）でも起動しなければ落ちた扱い。返り値は（落ちた扱いのチェックの名前、CI が通ったとまだ言えないか）"""
    url, sha = p["url"], s["head_sha"]
    suites = {r["suite"] for r in s["runs"] if r.get("suite")}
    workflows = s.get("workflows") or []
    unstarted = [w for w in workflows if w["status"] == "completed" and w["conclusion"] in FAILED and w["suite"] not in suites]
    extra: set = set()
    for w in unstarted:
        if emit(cur, "pr.ci_unstarted", p["corr"], {**base, "workflow": w["name"], "run_id": w["id"], "attempt": w["attempt"]},
                f"pr.ci_unstarted:{url}@{sha}:{w['id']}#{w['attempt']}"):
            out.append("pr.ci_unstarted")
        if w["attempt"] > 1:
            extra.add(f"{w['name']}（起動できなかった）")
    return extra, bool(unstarted) or any(w["status"] != "completed" for w in workflows)


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
    extra, waiting = unstarted_step(cur, p, s, base, out)
    failed = sorted(set(failed) | extra)
    if failed:
        if emit(cur, "pr.ci_failed", p["corr"], {**base, "failed": failed}, f"pr.ci_failed:{url}@{sha}"):
            out.append("pr.ci_failed")
            for name in [n for n in failed if not n.endswith("（起動できなかった）")]:   # CI で見つけた Defect（ADR 0028 の決定 5）。PR とチェックごとに 1 件。起動の失敗は実装の Defect ではない
                defect.safe_record(cur, "ci", f"{url}:{name}", "pr_events", log=log, correlation_id=p["corr"], repo=p["repo"],
                                   change=url, caused_in="implement", viewpoint=f"ci:{name}")
    elif runs and all(r["status"] == "completed" for r in runs) and not waiting:
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
def observe_closed(cur, repo: str, number: int, source: str) -> bool:
    """工場の PR でない PR（使い捨て環境を立てた PR など）が閉じたかを見て、閉じていたら github.pr_closed を記録する（ADR 0050 の決定 2）。
    工場の PR のマージ・閉じは supervisor が pr.merged・pr.closed_unmerged に残すので、ここでは見ない"""
    pr = gh("GET", f"repos/{repo}/pulls/{number}") or {}
    if pr.get("state") != "closed":
        return False
    url = f"https://github.com/{repo}/pull/{number}"
    return emit(cur, "github.pr_closed", f"env:{repo}#{number}", {"pr_url": url, "repo": repo, "pr": number, "merged": bool(pr.get("merged_at")),
                                                                  "closed_at": pr.get("closed_at"), "source": source}, f"github.pr_closed:{url}")


def poll_once():
    with db() as conn, conn.cursor() as cur:
        for p in open_factory_prs(cur):
            try:
                observe(cur, p, "poll")
            except Exception as e:  # noqa: BLE001  1 本の失敗で他を止めない
                log("poll error", p["url"], repr(e))
        # 内ループの処理が GitHub に直接聞かずに済むように、見た結果を出来事として残す（ADR 0050 の決定 2）
        for repo, number in ephemeral.watched(cur):   # 使い捨て環境が立っている PR（多くて EPHEMERAL_MAX 本）
            try:
                if observe_closed(cur, repo, number, "poll"):
                    log("poll", f"{repo}#{number}", "github.pr_closed")
            except Exception as e:  # noqa: BLE001
                log("poll error", f"{repo}#{number}", repr(e))
        try:
            ticket_sync.observe(cur, gh, log=log)   # 着手中のチケットの Issue（TICKET_SYNC_SEC ごと）
        except Exception as e:  # noqa: BLE001
            log("poll error", "ticket sync", repr(e))


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
