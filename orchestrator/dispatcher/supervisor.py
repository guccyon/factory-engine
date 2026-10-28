#!/usr/bin/env python3
"""工場長の supervisor（ADR 0004）。判断は持たず、状態と表を見て工程を起動するだけ。

60 秒ごとに:
- heartbeat: events に supervisor.heartbeat（番人が途絶えを見る。ADR 0005）
- マージ: 工場の PR が GitHub 上で mergeable_state=clean になったら、変更ファイルを change_type_rules.yaml で変更種別に写し、
  製品の manifest（autonomy の matrix・always_human_approve・phase_grants）を引いて判定を記録する。
  表が auto で、かつ工場の権限（phase_grants の merge）も auto のときだけ実際にマージする。それ以外は記録だけ（shadow）
- 結果: 判定を記録した PR が閉じたら、誰がマージしたか（しなかったか）を記録する（判定と人の判断の一致率の材料）
- 検証の起動: マージを判定した head ごとに検査ジョブ（kind=inspect、L4 の shadow 判定）を積む。結果は gate.evaluated と InspectionReport
- 依存順の投入: `factory:planned` の Issue を工場のチケットに写し（ADR 0009）、本文の「依存: #12 #13」がすべて閉じたものを
  優先度の高い順に `factory:implement` に替える（同時に流すのは上限まで。緊急（p0）は上限を待たない。保留中は流さない）
- 建設状況の日次レポート: JST の DAILY_REPORT_HOUR 時を過ぎて今日の report.daily がなければ、health/daily/status_report.py を 1 回走らせる（LLM は使わない）

Issue と PR の本文は正規表現で番号を拾うだけで、シェルにもプロンプトにも渡さない。
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import psycopg
import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import judgment
import ops
import stoppage

ENV = os.environ
DATABASE_URL = ENV["DATABASE_URL"]
REPOS = [r.strip() for r in ENV.get("FACTORY_REPOS", "").split(",") if r.strip()]
GH_TOKEN = ENV.get("SUPERVISOR_GH_TOKEN") or ENV["GH_TOKEN"]
INTERVAL = int(ENV.get("SUPERVISOR_INTERVAL_SEC", "60"))
MAX_INFLIGHT = int(ENV.get("SUPERVISOR_MAX_INFLIGHT", "1"))   # 工場で同時に流す Ticket（ジョブ＋開いている工場の PR）。衝突を避けて 1 から
RULES_PATH = ENV.get("CHANGE_TYPE_RULES", "/definitions/change_type_rules.yaml")
PLANNED_LABEL, IMPLEMENT_LABEL = "factory:planned", ENV.get("TRIGGER_LABEL", "factory:implement")
APPROVAL_LABEL = "factory:needs-approval"
DRY_RUN = bool(ENV.get("SUPERVISOR_DRY_RUN"))   # GitHub に書かない（読むだけ）
INSPECT_ENABLED = ENV.get("SUPERVISOR_INSPECT", "1") == "1"   # 工場の PR ごとに検査ジョブ（L4 の shadow 判定）を積む
JUDGE_ENABLED = ENV.get("SUPERVISOR_JUDGE", "1") == "1"       # 検査が終わったら、仮決めと指摘を裁く judge のジョブを積む（ADR 0006 後半）
MODE_DEFAULT, MODE_SCHEDULE = ENV.get("FACTORY_MODE") or "attended", ENV.get("MODE_SCHEDULE", "")
DAILY_REPORT = ENV.get("DAILY_REPORT_SCRIPT", "/health/daily/status_report.py")   # 空にすると日次レポートを出さない
DAILY_REPORT_HOUR = int(ENV.get("DAILY_REPORT_HOUR", "5"))   # JST。claude.ai のページは 6 時にこの結果から描く。Mac が寝ていた日は起きたときに出す
STRICTNESS = ["auto", "shadow", "human_approve", "forbidden"]
DEP_RE = re.compile(r"(?:依存|depends\s+on)\s*[:：]\s*((?:#\d+[\s,、]*)+)", re.IGNORECASE)


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), "[supervisor]", *a, flush=True)


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)


def gh(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(f"https://api.github.com/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json", "User-Agent": "factory-supervisor"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        return json.loads(data) if data else None


def record(cur, name: str, corr: str | None, payload: dict, dedupe_key: str | None = None) -> bool:
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values (%s,%s,%s,%s) on conflict (dedupe_key) do nothing returning id",
                (name, corr, Jsonb(payload), dedupe_key))
    return cur.fetchone() is not None


# ------------------------------------------------------------------ 変更種別の写像（表を引くだけ）
def glob_re(pat: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pat.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pat[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pat[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pat[i]), i + 1
    return re.compile(out + r"\Z")


def classify(files: list[dict], rules: dict) -> list[str]:
    """files: GitHub の pulls/{n}/files の要素（filename, patch）。返り値は変更種別の一覧（always:<id> を含む）"""
    types: list[str] = []
    for rule in rules.get("rules", []):
        pats = [glob_re(p) for p in rule["paths"]]
        diff_re = re.compile(rule["diff_pattern"]) if rule.get("diff_pattern") else None
        for f in files:
            if any(p.match(f["filename"]) for p in pats) and (diff_re is None or diff_re.search(f.get("patch") or "")):
                types.append(rule["type"])
                break
    if not types:
        docs = [glob_re(p) for p in rules.get("docs_only_paths", [])]
        types.append("docs_only" if files and all(any(p.match(f["filename"]) for p in docs) for f in files) else rules.get("default", "code_change"))
    return types


def strictest(values: list[str]) -> str:
    return max(values, key=lambda v: STRICTNESS.index(v) if v in STRICTNESS else len(STRICTNESS))


def decide(types: list[str], manifest: dict) -> dict:
    """表の判定（人の手元のマージ基準と同じ表）と、工場がそれを実行してよいか（phase_grants）を分けて返す"""
    au = manifest.get("autonomy", {})
    matrix = au.get("matrix", {})
    always = {a["id"]: a for a in au.get("always_human_approve", [])}
    vals, reasons = [], []
    for t in types:
        if t.startswith("always:"):
            # always_human_approve（表にない id も承認に倒す）。from: operation_start の項目は運用開始（autonomy.operation_started）までは効かない
            entry = always.get(t.split(":", 1)[1], {})
            dormant = entry.get("from") == "operation_start" and not au.get("operation_started")
            v = "auto" if dormant else "human_approve"
        else:
            v = (matrix.get(t) or {}).get("merge", "human_approve")                   # 表にない種別は承認に倒す（unanswered_default: hold）
        vals.append(v)
        reasons.append(f"{t}={v}")
    table = strictest(vals)
    phase = manifest.get("phase", "P0")
    grant = (au.get("phase_grants", {}).get(phase) or {}).get("merge", "human_approve")
    return {"change_types": types, "table": table, "reasons": reasons, "phase": phase, "factory_grant": grant,
            "execute": table == "auto" and grant == "auto"}


def load_rules() -> dict:
    with open(RULES_PATH) as f:
        return yaml.safe_load(f)


def judge_constraints(au: dict) -> str:
    """judge に渡す manifest の制約。from: operation_start の常時承認は、運用開始（autonomy.operation_started）までは
    マージの判定（上の always_human_approve の扱い）と同じく効かせないので、judge にも「今は人に回さない」と分けて渡す。
    以前は全項目を効いているものとして渡し、開発中の identity の変更を judge が人に回していた（rally #130）"""
    started = bool(au.get("operation_started"))
    active, dormant = [], []
    for a in au.get("always_human_approve", []):
        (dormant if a.get("from") == "operation_start" and not started else active).append(f"{a['id']}（{a.get('ja', '')}）")
    lines = ["always_human_approve（いま効いている。該当する判断は escalate）: " + (", ".join(active) or "なし")]
    if dormant:
        lines.append("運用開始から効く常時承認（運用開始前なので、これを理由に escalate しない。中身の妥当性だけで裁く）: " + ", ".join(dormant))
    return "\n".join(lines)


def load_manifest(repo: str) -> dict:
    c = gh("GET", f"repos/{repo}/contents/.factory-config/manifest.yaml")
    return yaml.safe_load(base64.b64decode(c["content"]))


def pr_files(repo: str, n: int) -> list[dict]:
    files, page = [], 1
    while page <= 10:
        got = gh("GET", f"repos/{repo}/pulls/{n}/files?per_page=100&page={page}") or []
        files += got
        if len(got) < 100:
            break
        page += 1
    return files


# ------------------------------------------------------------------ 各周期の仕事
def factory_prs(cur) -> list[dict]:
    """工場が作った PR（artifacts の PR）。同じ PR の重複は 1 つに"""
    cur.execute("select distinct on (payload->>'pr_url') correlation_id, payload from artifacts "
                "where type='PR' and payload->>'pr_url' is not null order by payload->>'pr_url', id desc")
    out = []
    for r in cur.fetchall():
        m = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", r["payload"]["pr_url"])
        if m and m.group(1) in REPOS:
            out.append({"repo": m.group(1), "number": int(m.group(2)), "url": r["payload"]["pr_url"], "corr": r["correlation_id"]})
    return out


def merge_step(cur, rules: dict, manifests: dict):
    open_count = 0
    for p in factory_prs(cur):
        cur.execute("select 1 from events where name in ('pr.merged','pr.closed_unmerged') and payload->>'pr_url'=%s", (p["url"],))
        if cur.fetchone():
            continue   # 結果まで記録済み
        pr = gh("GET", f"repos/{p['repo']}/pulls/{p['number']}")
        if pr["state"] == "closed":
            cur.execute("select payload from events where name='supervisor.merge_decided' and payload->>'pr_url'=%s order by id desc limit 1", (p["url"],))
            d = cur.fetchone()
            for key in (f"judge:{p['url']}", f"sendback:{p['url']}"):   # judge が人に回した停止・流さなかった差し戻しは、PR が閉じた（人が判断した）時点で閉じる
                stoppage.close_stoppage(cur, key, remedy="PR が閉じた（人が判断した）", closed_by="supervisor")
            if pr.get("merged_at"):
                cur.execute("update tickets set state='closed', updated_at=now() where source='github' and source_ref=%s and state<>'closed'",
                            (p["corr"].split(":", 1)[1],))   # チケットは済み（ADR 0009）
                record(cur, "pr.merged", p["corr"], {"pr_url": p["url"], "merged_at": pr["merged_at"], "merged_by": (pr.get("merged_by") or {}).get("login"),
                                                     "merge_commit": pr.get("merge_commit_sha"), "decided": (d or {}).get("payload")}, f"pr.merged:{p['url']}")
            else:
                record(cur, "pr.closed_unmerged", p["corr"], {"pr_url": p["url"], "closed_at": pr["closed_at"], "decided": (d or {}).get("payload")},
                       f"pr.closed_unmerged:{p['url']}")
            continue
        open_count += 1
        if pr.get("draft") or pr.get("mergeable_state") != "clean":
            continue   # CI 待ち・衝突・ドラフト（途中終了）は表を引くまでもない
        sha = pr["head"]["sha"]
        if p["repo"] not in manifests:
            manifests[p["repo"]] = load_manifest(p["repo"])
        d = decide(classify(pr_files(p["repo"], p["number"]), rules), manifests[p["repo"]])
        d.update({"pr_url": p["url"], "head_sha": sha, "executed": False})
        if not record(cur, "supervisor.merge_decided", p["corr"], d, f"merge.decided:{p['url']}@{sha}"):
            continue   # この head ではもう判定した
        log("merge decided", p["url"], d["table"], "grant", d["factory_grant"], d["change_types"])
        if DRY_RUN:
            continue
        enqueue_inspect(cur, p, sha)
        if d["table"] == "human_approve":
            gh("POST", f"repos/{p['repo']}/issues/{p['number']}/labels", {"labels": [APPROVAL_LABEL]})
        if d["execute"]:
            gh("PUT", f"repos/{p['repo']}/pulls/{p['number']}/merge", {"merge_method": "squash", "sha": sha})
            record(cur, "supervisor.merged", p["corr"], {"pr_url": p["url"], "head_sha": sha}, f"supervisor.merged:{p['url']}")
            open_count -= 1
    return open_count


def enqueue_inspect(cur, p: dict, sha: str):
    """検証の起動: CI を通った工場の PR の head ごとに 1 回、検査ジョブを積む。実行は dispatcher（worker の inspect.sh）"""
    if not INSPECT_ENABLED:
        return
    if not record(cur, "supervisor.inspect_enqueued", p["corr"], {"pr_url": p["url"], "head_sha": sha}, f"supervisor.inspect:{p['url']}@{sha}"):
        return
    try:
        cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, model_tier, kind) values (%s,%s,%s,'supervisor','tier-judge','inspect') returning id",
                    (p["corr"], p["repo"], p["number"]))
    except psycopg.errors.UniqueViolation:
        return   # 同じ PR の検査がすでに積まれている
    jid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                (p["corr"], jid, Jsonb({"by": "supervisor", "kind": "inspect", "pr_url": p["url"], "head_sha": sha})))
    log("inspect enqueued", p["url"], "job", jid)


def judge_step(cur, manifests: dict):
    """検査を積んだ head ごとに、検査の結果が出たら judge のジョブを 1 回積む。裁くもの（仮決め・指摘）がなければ積まない"""
    if not JUDGE_ENABLED:
        return
    cur.execute("""select i.correlation_id as corr, i.payload->>'pr_url' as pr_url, i.payload->>'head_sha' as sha, i.occurred_at
                     from events i
                    where i.name='supervisor.inspect_enqueued' and i.occurred_at > now() - interval '3 days'
                      and not exists (select 1 from events j where j.name='supervisor.judge_enqueued'
                                       and j.payload->>'pr_url'=i.payload->>'pr_url' and j.payload->>'head_sha'=i.payload->>'head_sha')
                      and exists (select 1 from events g where g.name='gate.evaluated' and g.payload->>'pr_url'=i.payload->>'pr_url' and g.occurred_at > i.occurred_at)""")
    for r in cur.fetchall():
        m = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)$", r["pr_url"])
        if not m:
            continue
        repo, number = m.group(1), int(m.group(2))
        mode, basis = judgment.current_mode(cur, MODE_DEFAULT, MODE_SCHEDULE)
        if repo not in manifests:
            manifests[repo] = load_manifest(repo)
        au = manifests[repo].get("autonomy", {})
        constraints = judge_constraints(au)
        inp = judgment.judge_input(cur, r["pr_url"], mode, constraints)
        nothing = not inp["provisional"] and not inp["defects"]
        if not record(cur, "supervisor.judge_enqueued", r["corr"], {"pr_url": r["pr_url"], "head_sha": r["sha"], "mode": mode, "mode_basis": basis,
                                                                  "skipped": nothing, "n_provisional": len(inp["provisional"]), "n_defects": len(inp["defects"])},
                      f"supervisor.judge:{r['pr_url']}@{r['sha']}"):
            continue
        if nothing or DRY_RUN:
            continue
        try:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, model_tier, kind) values (%s,%s,%s,'supervisor','tier-judge','judge') returning id",
                        (r["corr"], repo, number))
        except psycopg.errors.UniqueViolation:   # 同じ PR のジョブ（検査など）がまだ動いている。次の周期で積み直せるよう記録を消す
            cur.execute("delete from events where dedupe_key=%s", (f"supervisor.judge:{r['pr_url']}@{r['sha']}",))
            continue
        jid = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                    (r["corr"], jid, Jsonb({"by": "supervisor", "kind": "judge", "pr_url": r["pr_url"], "head_sha": r["sha"],
                                            "mode": mode, "mode_basis": basis, "input": inp})))
        log("judge enqueued", r["pr_url"], "job", jid, "mode", mode)


def enqueue_step(cur, open_prs: int):
    cur.execute("select count(*) as n from jobs where state in ('queued','running')")
    inflight = cur.fetchone()["n"] + open_prs
    state_cache: dict[str, str] = {}

    def issue_state(repo: str, n: int) -> str:
        key = f"{repo}#{n}"
        if key not in state_cache:
            state_cache[key] = gh("GET", f"repos/{repo}/issues/{n}")["state"]
        return state_cache[key]

    ready, seen = [], []
    for repo in REPOS:
        issues = gh("GET", f"repos/{repo}/issues?state=open&labels={urllib.parse.quote(PLANNED_LABEL)}&per_page=50&sort=created&direction=asc") or []
        for it in issues:
            if "pull_request" in it:
                continue
            deps = [int(x) for m in DEP_RE.finditer(it.get("body") or "") for x in re.findall(r"#(\d+)", m.group(1))]
            waiting = [n for n in deps if issue_state(repo, n) != "closed"]
            # 投入待ちのチケットも工場の台帳に写す（操業コンソールの「依存待ち・順番待ち」。ADR 0009）
            t = ops.upsert_ticket(cur, "github", f"{repo}#{it['number']}", repo, title=it.get("title") or "", url=it.get("html_url"),
                                  priority=ops.priority_from_labels([l.get("name", "") for l in it.get("labels") or []]), state="planned",
                                  depends_on=[f"{repo}#{n}" for n in deps], waiting_on=[f"{repo}#{n}" for n in waiting])
            seen.append(t["source_ref"])
            if not waiting and not t["held"]:
                ready.append((t, repo, it, deps))
    # planned のラベルが外れた（実装に回った・人が外した）のに台帳で planned のままのものは、台帳から外す
    cur.execute("update tickets set state='closed', updated_at=now() where source='github' and state='planned' and repo = any(%s) "
                "and not (source_ref = any(%s))", (REPOS, seen))
    ready.sort(key=lambda r: (-r[0]["priority"], r[2].get("created_at") or ""))   # 優先度の高い順、同じなら古い順
    for t, repo, it, deps in ready:
        if inflight >= MAX_INFLIGHT and t["priority"] < ops.PREEMPT_AT:
            continue
        corr = f"github:{repo}#{it['number']}"
        if not record(cur, "supervisor.enqueued", corr, {"repo": repo, "issue": it["number"], "deps": deps, "priority": t["priority"]},
                      f"supervisor.enqueued:{repo}#{it['number']}"):
            continue
        log("enqueue", repo, it["number"], "deps", deps, "priority", t["priority"])
        if not DRY_RUN:
            try:
                gh("DELETE", f"repos/{repo}/issues/{it['number']}/labels/{urllib.parse.quote(PLANNED_LABEL, safe='')}")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
            gh("POST", f"repos/{repo}/issues/{it['number']}/labels", {"labels": [IMPLEMENT_LABEL]})
            cur.execute("update tickets set state='active', updated_at=now() where id=%s", (t["id"],))
        inflight += 1


def daily_report_step(cur):
    """1 日 1 回、建設状況の日次レポートを出す。記録（report.daily）はスクリプトが書く。失敗しても次の周で再試行する"""
    if not DAILY_REPORT or DRY_RUN or not os.path.exists(DAILY_REPORT):
        return
    now = datetime.now(timezone(timedelta(hours=9)))
    if now.hour < DAILY_REPORT_HOUR:
        return
    cur.execute("select 1 from events where dedupe_key=%s", (f"report.daily:{now:%Y-%m-%d}",))
    if cur.fetchone():
        return
    r = subprocess.run([sys.executable, DAILY_REPORT, "--publish"], capture_output=True, text=True, timeout=300,
                       env={**ENV, "GH_TOKEN": GH_TOKEN})
    log("daily report", r.returncode, (r.stdout or r.stderr).strip()[-300:])


def tick():
    with db() as conn, conn.cursor() as cur:
        record(cur, "supervisor.heartbeat", None, {"repos": REPOS})
        rules, manifests = load_rules(), {}
        open_prs = merge_step(cur, rules, manifests)
        judge_step(cur, manifests)
        enqueue_step(cur, open_prs)
        daily_report_step(cur)


def main():
    log(f"supervisor up: repos={REPOS} interval={INTERVAL}s max_inflight={MAX_INFLIGHT} dry_run={DRY_RUN}")
    while True:
        try:
            tick()
        except Exception as e:  # noqa: BLE001  一周の失敗で止めない。heartbeat が途絶えれば番人が拾う
            log("tick error", repr(e))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
