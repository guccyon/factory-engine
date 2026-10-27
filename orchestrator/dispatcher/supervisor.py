#!/usr/bin/env python3
"""工場長の supervisor（ADR 0004）。判断は持たず、状態と表を見て工程を起動するだけ。

60 秒ごとに:
- heartbeat: events に supervisor.heartbeat（番人が途絶えを見る。ADR 0005）
- マージ: 工場の PR が GitHub 上で mergeable_state=clean になったら、変更ファイルを change_type_rules.yaml で変更種別に写し、
  製品の manifest（autonomy の matrix・always_human_approve・phase_grants）を引いて判定を記録する。
  表が auto で、かつ工場の権限（phase_grants の merge）も auto のときだけ実際にマージする。それ以外は記録だけ（shadow）
- 結果: 判定を記録した PR が閉じたら、誰がマージしたか（しなかったか）を記録する（判定と人の判断の一致率の材料）
- 依存順の投入: `factory:planned` の Issue の本文の「依存: #12 #13」がすべて閉じたら `factory:implement` に替える（同時に流すのは上限まで）

Issue と PR の本文は正規表現で番号を拾うだけで、シェルにもプロンプトにも渡さない。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import psycopg
import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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
    vals, reasons = [], []
    for t in types:
        if t.startswith("always:"):
            v = "human_approve"   # always_human_approve（表にない id も承認に倒す）
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
            if pr.get("merged_at"):
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
        if d["table"] == "human_approve":
            gh("POST", f"repos/{p['repo']}/issues/{p['number']}/labels", {"labels": [APPROVAL_LABEL]})
        if d["execute"]:
            gh("PUT", f"repos/{p['repo']}/pulls/{p['number']}/merge", {"merge_method": "squash", "sha": sha})
            record(cur, "supervisor.merged", p["corr"], {"pr_url": p["url"], "head_sha": sha}, f"supervisor.merged:{p['url']}")
            open_count -= 1
    return open_count


def enqueue_step(cur, open_prs: int):
    cur.execute("select count(*) as n from jobs where state in ('queued','running')")
    inflight = cur.fetchone()["n"] + open_prs
    for repo in REPOS:
        issues = gh("GET", f"repos/{repo}/issues?state=open&labels={urllib.parse.quote(PLANNED_LABEL)}&per_page=50&sort=created&direction=asc") or []
        for it in issues:
            if "pull_request" in it or inflight >= MAX_INFLIGHT:
                continue
            deps = [int(x) for m in DEP_RE.finditer(it.get("body") or "") for x in re.findall(r"#(\d+)", m.group(1))]
            waiting = [n for n in deps if gh("GET", f"repos/{repo}/issues/{n}")["state"] != "closed"]
            if waiting:
                continue
            corr = f"github:{repo}#{it['number']}"
            if not record(cur, "supervisor.enqueued", corr, {"repo": repo, "issue": it["number"], "deps": deps},
                          f"supervisor.enqueued:{repo}#{it['number']}"):
                continue
            log("enqueue", repo, it["number"], "deps", deps)
            if not DRY_RUN:
                try:
                    gh("DELETE", f"repos/{repo}/issues/{it['number']}/labels/{urllib.parse.quote(PLANNED_LABEL, safe='')}")
                except urllib.error.HTTPError as e:
                    if e.code != 404:
                        raise
                gh("POST", f"repos/{repo}/issues/{it['number']}/labels", {"labels": [IMPLEMENT_LABEL]})
            inflight += 1


def tick():
    with db() as conn, conn.cursor() as cur:
        record(cur, "supervisor.heartbeat", None, {"repos": REPOS})
        rules, manifests = load_rules(), {}
        open_prs = merge_step(cur, rules, manifests)
        enqueue_step(cur, open_prs)


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
