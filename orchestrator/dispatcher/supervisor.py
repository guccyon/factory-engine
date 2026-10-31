#!/usr/bin/env python3
"""工場長の supervisor（ADR 0004）。判断は持たず、状態と表を見て工程を起動するだけ。

60 秒ごとに:
- heartbeat: events に supervisor.heartbeat（番人が途絶えを見る。ADR 0005）
- マージ: 工場の PR が GitHub 上で mergeable_state=clean になったら、変更ファイルを change_type_rules.yaml で変更種別に写し、
  製品の manifest（autonomy の matrix・always_human_approve・phase_grants）を引いて判定を記録する。
  表が auto で、かつ工場の権限（phase_grants の merge）も auto のときだけ実際にマージする。それ以外は記録だけ（shadow）
- 結果: 判定を記録した PR が閉じたら、誰がマージしたか（しなかったか）を記録する（判定と人の判断の一致率の材料）
- 検証の起動: マージを判定した head ごとに検査ジョブ（kind=inspect、L4 の shadow 判定）を積む。結果は gate.evaluated と InspectionReport
- 続きのジョブ（ADR 0016・0017）: 工場の PR が衝突したら（head ごとに 1 回）、CI が落ちたら（PR ごとに CONTINUE_MAX 回まで）、
  同じチケットの実装役の続きのジョブを積む。直している間は停止を開かず、直せなかったら停止 ci: を開いて人へ
- 依存順の投入: `factory:planned` の Issue を工場のチケットに写し（ADR 0009）、本文の「依存: #12 #13」がすべて閉じたものを
  優先度の高い順に `factory:implement` に替える（同時に流すのは上限まで。緊急（p0）は上限を待たない。保留中は流さない）
- 建設状況の日次レポート: JST の DAILY_REPORT_HOUR 時を過ぎて今日の report.daily がなければ、health/daily/status_report.py を 1 回走らせる（LLM は使わない）
- 出荷の列車（ADR 0012）: JST の RELEASE_TRAIN_HOUR 時を過ぎて、その製品の Release が今日まだなく、まだ Release に入っていない
  マージ済みの工場の PR があり、出荷が処理中でも承認待ちでもなければ、次の版（minor を 1 つ上げる）の Release を切って staging への出荷を依頼する。
  prod へは承認ボタンを待つ

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

import case
import continuation
import design
import judgment
import learn
import ops
import ship
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
RELEASE_TRAIN_HOUR = int(ENV.get("RELEASE_TRAIN_HOUR", "17"))   # JST。空の RELEASE_TRAIN で止める。夕方に検証へ出し、夜に承認を押せるように
RELEASE_TRAIN = [r.strip() for r in ENV.get("RELEASE_TRAIN", "").split(",") if r.strip()]   # 列車を走らせる製品（出荷の形 ship.yaml を持つもの）
DAILY_REPORT_HOUR = int(ENV.get("DAILY_REPORT_HOUR", "5"))   # JST。claude.ai のページは 6 時にこの結果から描く。Mac が寝ていた日は起きたときに出す
# 使用量の枠ごとの予算（2026-10-29 オーナー: Max の使用量は毎朝 7:00 から 5 時間ごとにリセット。1 日 約 $100 を枠ごとに均し、
# 平日の日中は別の業務でも使うので夕方以降に寄せる）。「開始時:予算ドル」を JST で並べる。空にすると予算で止めない。値は仮置きで運用で直す
BUDGET_WINDOWS_WEEKDAY = ENV.get("BUDGET_WINDOWS_WEEKDAY", "07:10,12:15,17:35,22:25,03:15")
BUDGET_WINDOWS_WEEKEND = ENV.get("BUDGET_WINDOWS_WEEKEND", "07:20,12:20,17:20,22:20,03:20")
CONFLICT_FIX = ENV.get("SUPERVISOR_CONFLICT_FIX", "1") == "1"   # 衝突した工場の PR に、既定ブランチを取り込んで衝突を解くジョブを head ごとに 1 回積む。0 で止める
CI_FIX = ENV.get("SUPERVISOR_CI_FIX", "1") == "1"   # CI が落ちた工場の PR に、同じ担当の続きのジョブを積む（PR ごとに CONTINUE_MAX 回まで。ADR 0017）。0 で止める
DESIGN_REVIEW = ENV.get("DESIGN_REVIEW", "1") == "1"   # 週 1 回の設計の診断のジョブを積む（ADR 0022）。0 で止める
DESIGN_REVIEW_DEFS = ENV.get("DESIGN_REVIEW_DEFINITIONS", "/definitions/design_review.yaml")
QUALITY_REPOS = [r.strip() for r in (ENV.get("QUALITY_REPOS") or ENV.get("FACTORY_REPOS", "")).split(",") if r.strip()]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
LEARN_RULE_ISSUES = ENV.get("LEARN_RULE_ISSUES", "1") == "1"   # Retrospective の束からルール化の Issue を 1 日 1 本まで起票する（ADR 0013 の 3）。0 で止める
LEARN_RULE_TRANSITIONS = ENV.get("LEARN_RULE_TRANSITIONS", "1") == "1"   # overlay のルールの shadow → adopted と、カナリアで戻すのを自動で起票する（ADR 0013 の 6）。0 で止める
OVERLAY_RULES_DIR = ".factory-config/overlay/rules"
CASE_REVIEW = ENV.get("CASE_REVIEW", "1") == "1"   # 事例型の分析役（ADR 0023）。差し戻しと長い停止を 1 日に数件、なぜなぜで分析する。0 で止める
CASE_REVIEW_DEFS = ENV.get("LEARNING_TAXONOMY", "/definitions/learning_taxonomy.yaml")   # 事例型の分析役の数値（per_day）
KNOWLEDGE_ISSUES = ENV.get("KNOWLEDGE_ISSUES", "1") == "1"   # 事例の分析から、知見の変更セットの Issue を起票する（ADR 0024 の 3）。0 で止める
KNOWLEDGE_ISSUES_PER_DAY = int(ENV.get("KNOWLEDGE_ISSUES_PER_DAY", "2"))   # 委員会ができるまでは知見の PR は人の承認（knowledge 種別）なので少なく
_transitions_checked_at = 0.0   # 1 時間に 1 回だけ見る（GitHub の呼び出しを抑える）
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


def ci_failure_step(cur, p: dict, sha: str, mergeable_state: str | None = None):
    """工場の PR の CI が落ちた・衝突しているなら停止として開く（検査・judge は CI が通るまで積まないので、黙って止まらないように）。
    事実は PR の見張り（pr_events、ADR 0015）が発行したイベントから読む。GitHub には問い合わせない。次の head で CI が通れば閉じる。PR が閉じても閉じる"""
    key = f"ci:{p['url']}"
    cur.execute("select name, payload from events where name in ('pr.ci_failed','pr.ci_passed','pr.conflicted') "
                "and payload->>'pr_url'=%s and payload->>'head_sha'=%s", (p["url"], sha))
    seen = {r["name"]: r["payload"] for r in cur.fetchall()}
    if ("pr.conflicted" in seen or mergeable_state == "dirty" or "pr.ci_failed" in seen) and continuation.pending(cur, p["url"], sha):
        return   # 工場の続きのジョブが直している最中。人には回さない（ADR 0017）
    tried = continuation.attempts(cur, p["url"], "ci_failed")
    if "pr.conflicted" in seen or mergeable_state == "dirty":   # 衝突（2026-10-31。これまでは何も起きず止まっていた）
        stoppage.open_stoppage(cur, key, "ticket", "工場の PR が main と衝突している。検査と judge に進めない",
                               waiting_for="工場の衝突の解消が通らなかった。builder が衝突を解くか、差し戻す", correlation_id=p["corr"], detected_by="supervisor")
    elif "pr.ci_failed" in seen:
        stoppage.open_stoppage(cur, key, "ticket", f"工場の PR の CI が落ちた（{', '.join(seen['pr.ci_failed'].get('failed') or [])}）。検査と judge に進めない",
                               waiting_for=f"工場の続きのジョブ（{tried} 回）でも通らなかった。builder が直すか、差し戻す" if tried else "builder が直すか、差し戻す", correlation_id=p["corr"], detected_by="supervisor")
    elif "pr.ci_passed" in seen:
        stoppage.close_stoppage(cur, key, remedy="次の head で CI が通った（衝突も解けた）", closed_by="supervisor")


def waiting_for_human(cur, url: str) -> bool:
    """人を待っている PR か（judge が人に回した、差し戻しを流さなかった、CI 落ち・衝突、表が承認を求めた）。
    同時に流す件数の枠には数えない。承認待ちをラインのクリティカルパスにしない（2026-10-31 オーナー）"""
    for prefix in ("judge:", "sendback:", "ci:"):
        if stoppage.open_keys(cur, f"{prefix}{url}"):
            return True
    cur.execute("select payload->>'table' as t from events where name='supervisor.merge_decided' and payload->>'pr_url'=%s order by id desc limit 1", (url,))
    d = cur.fetchone()
    return bool(d and d["t"] == "human_approve")


def conflict_step(cur, p: dict, pr: dict):
    """工場の PR が既定ブランチと衝突したら（同時に流した PR どうしの CHANGELOG など）、その PR のブランチで衝突を解く
    続きのジョブを head ごとに 1 回積む。ワーカーは既定ブランチをマージコミットで取り込み、履歴は書き換えない。
    解けなかったら次の head は来ないので、同じ head では二度積まず、番人のマージ待ちの停止に任せる"""
    if not CONFLICT_FIX or (pr["head"].get("repo") or {}).get("full_name") != p["repo"]:
        return   # フォークの PR は触らない
    jid = continuation.enqueue(cur, repo=p["repo"], pr_number=p["number"], pr_url=p["url"], branch=pr["head"]["ref"],
                               head_sha=pr["head"]["sha"], reason="conflict", by="supervisor")
    if isinstance(jid, int):
        log("conflict fix enqueued", p["url"], "job", jid)


def ci_fix_step(cur, p: dict, pr: dict):
    """工場の PR の CI が落ちたら（PR の見張りの pr.ci_failed）、同じ担当の続きのジョブを積んで直させる（ADR 0017）。
    同じ PR で CONTINUE_MAX 回まで。超えたら ci_failure_step が停止を開いて人へ回す"""
    if not CI_FIX or (pr["head"].get("repo") or {}).get("full_name") != p["repo"]:
        return
    sha = pr["head"]["sha"]
    cur.execute("select payload from events where name='pr.ci_failed' and payload->>'pr_url'=%s and payload->>'head_sha'=%s limit 1", (p["url"], sha))
    ev = cur.fetchone()
    if not ev:
        return
    jid = continuation.enqueue(cur, repo=p["repo"], pr_number=p["number"], pr_url=p["url"], branch=pr["head"]["ref"], head_sha=sha,
                               reason="ci_failed", by="supervisor", signal={"head_sha": sha, "failed": ev["payload"].get("failed") or []})
    if isinstance(jid, int):
        log("ci fix enqueued", p["url"], "job", jid)


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
            for key in (f"judge:{p['url']}", f"sendback:{p['url']}", f"ci:{p['url']}"):   # judge が人に回した停止・流さなかった差し戻し・CI の失敗は、PR が閉じた（人が判断した）時点で閉じる
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
        if not pr.get("draft"):
            # 衝突と CI 落ちは、まず同じ担当の続きのジョブで直す（ADR 0016・0017）。直せない・上限に達したときだけ停止（ci:）を開いて人へ
            if pr.get("mergeable_state") == "dirty":
                conflict_step(cur, p, pr)
            else:
                ci_fix_step(cur, p, pr)
            ci_failure_step(cur, p, pr["head"]["sha"], pr.get("mergeable_state"))
        if not waiting_for_human(cur, p["url"]):
            open_count += 1
        if pr.get("draft") or pr.get("mergeable_state") != "clean":
            continue   # CI 待ち・衝突・ドラフト（途中終了）は表を引くまでもない
        sha = pr["head"]["sha"]
        if p["repo"] not in manifests:
            manifests[p["repo"]] = load_manifest(p["repo"])
        d = decide(classify(pr_files(p["repo"], p["number"]), rules), manifests[p["repo"]])
        d.update({"pr_url": p["url"], "head_sha": sha, "executed": False})
        # 判定は head と結論（表・権限）ごとに 1 回。manifest が変わって結論が変われば、同じ head でも記録し直して従う（2026-10-31）
        if record(cur, "supervisor.merge_decided", p["corr"], d, f"merge.decided:{p['url']}@{sha}:{d['table']}/{d['factory_grant']}"):
            log("merge decided", p["url"], d["table"], "grant", d["factory_grant"], d["change_types"])
            if DRY_RUN:
                continue
            enqueue_inspect(cur, p, sha)
            if d["table"] == "human_approve":
                gh("POST", f"repos/{p['repo']}/issues/{p['number']}/labels", {"labels": [APPROVAL_LABEL]})
        elif DRY_RUN:
            continue
        # マージは表が auto で、かつ検査役が承認してから（ADR 0014）。承認は周期ごとに見直すので、検査・judge が終わった周期でマージする
        if not d["execute"]:
            continue
        cur.execute("select 1 from events where dedupe_key=%s", (f"supervisor.merged:{p['url']}",))
        if cur.fetchone():
            continue   # マージ済み。pr.merged の記録待ち
        ok, basis = inspector_approved(cur, p["url"], sha)
        if not ok:
            continue
        record(cur, "supervisor.inspector_approved", p["corr"], {"pr_url": p["url"], "head_sha": sha, "basis": basis}, f"inspector.approved:{p['url']}@{sha}")
        gh("PUT", f"repos/{p['repo']}/pulls/{p['number']}/merge", {"merge_method": "squash", "sha": sha})
        record(cur, "supervisor.merged", p["corr"], {"pr_url": p["url"], "head_sha": sha, "basis": basis}, f"supervisor.merged:{p['url']}")
        log("merged", p["url"], basis)
        open_count = max(0, open_count - 1)
    return open_count


def inspector_approved(cur, pr_url: str, sha: str) -> tuple[bool, str]:
    """検査役の承認（ADR 0014）。(承認したか, 根拠) を返す。
    検査（inspect）と judge がその head で終わっていて、judge が人に回した判断がない（あれば、人が停止 judge: を閉じるまで待つ）ときに承認する。
    採用された指摘は差し戻しの Issue で直すので、マージは止めない（ラインを止めない方針 D7）"""
    if not INSPECT_ENABLED:
        return True, "検査なし（SUPERVISOR_INSPECT=0）"
    cur.execute("select occurred_at from events where dedupe_key=%s", (f"supervisor.inspect:{pr_url}@{sha}",))
    ins = cur.fetchone()
    if not ins:
        return False, "検査待ち"
    cur.execute("select 1 from events where name='gate.evaluated' and payload->>'pr_url'=%s and occurred_at > %s", (pr_url, ins["occurred_at"]))
    if not cur.fetchone():
        return False, "検査中"
    if not JUDGE_ENABLED:
        return True, "検査済み（judge なし）"
    cur.execute("select occurred_at, payload from events where dedupe_key=%s", (f"supervisor.judge:{pr_url}@{sha}",))
    jd = cur.fetchone()
    if not jd:
        return False, "判定待ち"
    if jd["payload"].get("skipped"):
        return True, "検査で仮決めも指摘もなかった"
    cur.execute("select payload->>'verdict' as verdict from events where name='judgment.judged' and payload->>'pr_url'=%s and occurred_at > %s",
                (pr_url, jd["occurred_at"]))
    verdicts = [r["verdict"] for r in cur.fetchall()]
    if not verdicts:
        return False, "判定中"
    if "escalate" in verdicts and stoppage.open_keys(cur, f"judge:{pr_url}"):
        return False, "judge が人に回した判断の待ち"
    if continuation.pending(cur, pr_url, sha, "review_fix"):
        return False, "採用した指摘を同じ担当が直している（ADR 0017）"
    n_adopt = sum(1 for v in verdicts if v == "adopt")
    return True, f"judge が {len(verdicts)} 件を裁いた（採用 {n_adopt}。採用した指摘は続きのジョブか差し戻しで直す）"


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


def budget_window(now: datetime) -> tuple[datetime, float] | None:
    """今（JST）の使用量の枠の開始時刻と予算。枠は前日からまたぐ（22 時の枠は翌 3 時まで）。週末は開始した日で決める"""
    def parse(spec):
        out = []
        for part in spec.split(","):
            hm, _, usd = part.strip().partition(":")
            if hm and usd:
                out.append((int(hm), float(usd)))
        return sorted(out)
    for back in (0, 1):
        day = now - timedelta(days=back)
        spec = BUDGET_WINDOWS_WEEKEND if day.weekday() >= 5 else BUDGET_WINDOWS_WEEKDAY
        for h, usd in reversed(parse(spec)):
            start = day.replace(hour=h, minute=0, second=0, microsecond=0)
            if start <= now:
                return start, usd
    return None


def over_budget(cur) -> dict | None:
    """今の枠の予算を使い切っていれば、その様子を返す（新しい実装を投入しない。作業中のもの・検査・judge は止めない）"""
    w = budget_window(datetime.now(timezone(timedelta(hours=9))))
    if w is None:
        return None
    start, usd = w
    cur.execute("select coalesce(sum((payload->>'cost_usd')::numeric), 0) as spent from events where name='job.finished' and occurred_at >= %s", (start,))
    spent = float(cur.fetchone()["spent"])
    return {"window_start": start.isoformat(), "budget_usd": usd, "spent_usd": round(spent, 2)} if spent >= usd else None


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
    paused = over_budget(cur) if ready else None
    if paused and record(cur, "supervisor.budget_paused", None, paused, f"supervisor.budget_paused:{paused['window_start']}"):
        log("budget paused", paused)
    for t, repo, it, deps in ready:
        if inflight >= MAX_INFLIGHT and t["priority"] < ops.PREEMPT_AT:
            continue
        if paused and t["priority"] < ops.PREEMPT_AT:   # 枠の予算を使い切ったら次の枠まで投入しない（p0 は止めない）
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


def release_train_step(cur):
    """1 日 1 回まで、製品ごとに Release を切って staging に出す（ADR 0012）。判断は持たず、条件を見るだけ"""
    if DRY_RUN or not RELEASE_TRAIN:
        return
    now = datetime.now(timezone(timedelta(hours=9)))
    if now.hour < RELEASE_TRAIN_HOUR:
        return
    for repo in RELEASE_TRAIN:
        cur.execute("select 1 from events where name='release.created' and payload->>'repo'=%s and occurred_at >= %s",
                    (repo, now.replace(hour=0, minute=0, second=0, microsecond=0)))
        if cur.fetchone() or ship.unreleased(cur, repo) == 0 or ship.busy(cur, repo):
            continue
        rid, prs = ship.create_release(cur, repo, ship.next_version(cur, repo))
        if rid:
            ship.request_ship(cur, rid, "supervisor", "release_train")
            log("release train", rid, len(prs))


def learn_step(cur):
    """1 日 1 回、教師信号を束ねて Retrospective を残す（ADR 0013 の 2）。決定的で、トークンを使わない。日報の後に回す"""
    if DRY_RUN:
        return
    now = datetime.now(timezone(timedelta(hours=9)))
    if now.hour < DAILY_REPORT_HOUR:
        return
    day = f"{now:%Y-%m-%d}"
    cur.execute("select 1 from events where dedupe_key=%s", (f"learn.retrospected:{day}",))
    if cur.fetchone():
        return
    retro = learn.retrospect(cur)
    if not learn.record_retrospective(cur, retro, day):
        return
    log("retrospective", day, "signals", retro["signals"], "clusters", len(retro["clusters"]))
    c = learn.pick_cluster(cur, retro) if LEARN_RULE_ISSUES else None
    if not c:
        return
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/\d+", c["eval_sources"][0]["positive"] or "")
    if not m or m.group(1) not in REPOS:
        return
    title, body = learn.rule_issue(c)
    try:
        issue = gh("POST", f"repos/{m.group(1)}/issues", {"title": title, "body": body, "labels": ["factory:planned", "factory:learn"]})
    except Exception as e:  # noqa: BLE001  起票に失敗しても翌日の束でやり直す
        log("rule issue failed", c["signature"], repr(e))
        return
    record(cur, "learn.rule_filed", f"github:{m.group(1)}#{issue['number']}", {"signature": c["signature"], "issue": issue["html_url"],
                                                                              "signals": c["signals"], "day": day}, f"learn.rule_filed:{c['signature']}")
    log("rule issue filed", c["signature"], issue["html_url"])


def overlay_rules(repo: str) -> list[dict]:
    """製品の既定ブランチの overlay のルール。{id, path, state, since}。since はそのファイルが最後に変わったコミットの時刻（状態に入った時刻とみなす）"""
    out = []
    for e in gh("GET", f"repos/{repo}/contents/{OVERLAY_RULES_DIR}") or []:
        if not re.fullmatch(r"OV-\d+\.ya?ml", e.get("name", "")):
            continue
        f = gh("GET", f"repos/{repo}/contents/{urllib.parse.quote(e['path'])}")
        rule = yaml.safe_load(base64.b64decode(f["content"]).decode()) or {}
        commits = gh("GET", f"repos/{repo}/commits?path={urllib.parse.quote(e['path'])}&per_page=1") or []
        if not commits or not rule.get("id"):
            continue
        since = datetime.fromisoformat(commits[0]["commit"]["committer"]["date"].replace("Z", "+00:00"))
        out.append({"id": rule["id"], "path": e["path"], "state": rule.get("state", ""), "since": since})
    return out


def rule_transition_step(cur, now: datetime | None = None):
    """overlay のルールの状態の移り変わりを判定し、変える Issue を起票する（ADR 0013 の 6・8）。判定は govern 側（ここ）が events から計算し、
    ワーカーは state の 1 行を書き換えるだけ。ルール変更 PR は factory_rule として検査を通って自動でマージされる"""
    global _transitions_checked_at
    if DRY_RUN or not LEARN_RULE_TRANSITIONS or time.time() - _transitions_checked_at < 3600:
        return
    _transitions_checked_at = time.time()
    now = now or datetime.now(timezone.utc)
    for repo in REPOS:
        try:
            rules = overlay_rules(repo)
        except urllib.error.HTTPError as e:
            if e.code == 404:   # overlay のない製品
                continue
            raise
        for r in rules:
            stats = {s["id"]: s for s in learn.rule_stats(cur, r["since"], now)}
            s = stats.get(r["id"])
            t = learn.transition(r["state"], r["since"], s, now)
            if not t:
                continue
            key = f"learn.rule_transition:{repo}:{r['id']}:{t['to']}:{r['since']:%Y%m%dT%H%M%S}"
            cur.execute("select 1 from events where dedupe_key=%s", (key,))
            if cur.fetchone():
                continue
            title, body = learn.transition_issue(r["id"], r["path"], r["state"], t, s)
            issue = gh("POST", f"repos/{repo}/issues", {"title": title, "body": body, "labels": ["factory:planned", "factory:learn"]})
            record(cur, "learn.rule_transition_filed", f"github:{repo}#{issue['number']}",
                   {"rule": r["id"], "from": r["state"], "to": t["to"], "why": t["why"], "issue": issue["html_url"], "stats": s}, key)
            log("rule transition filed", repo, r["id"], r["state"], "->", t["to"], issue["html_url"])


def case_review_step(cur, now: datetime | None = None):
    """1 日 1 回、分析する出来事を選んで事例型の分析のジョブを積む（ADR 0023）。入力は制御プレーンの記録だけで組み立てる。
    セッションの抜粋は dispatcher がジョブを起こすときに足す。予算の枠を使い切っているときは積まない"""
    if not CASE_REVIEW or DRY_RUN:
        return
    now = now or datetime.now(timezone.utc)
    day = f"{now.astimezone(timezone(timedelta(hours=9))):%Y-%m-%d}"
    cur.execute("select count(*) as n from events where name='case.enqueued' and payload->>'day'=%s", (day,))
    try:
        per_day = int((yaml.safe_load(open(CASE_REVIEW_DEFS, encoding="utf-8")).get("case_review") or {}).get("per_day", 3))
    except OSError:
        return
    room = per_day - cur.fetchone()["n"]
    if room <= 0 or over_budget(cur):
        return
    for inc in case.pick_incidents(cur, now, room):
        m = re.match(r"github:([^#]+)#(\d+)$", inc.get("corr") or "")
        repo, issue = (m.group(1), int(m.group(2))) if m else (REPOS[0], 0)   # チケットのない停止（ラインが空など）は最初の製品に付ける
        corr = f"case:{inc['key']}"
        inp = case.build_input(cur, inc)
        try:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, model_tier, kind) values (%s,%s,%s,'supervisor','tier-judge','case_review') returning id",
                        (corr, repo, issue))
        except psycopg.errors.UniqueViolation:
            continue
        jid = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                    (corr, jid, Jsonb({"by": "supervisor", "kind": "case_review", "input": inp})))
        record(cur, "case.enqueued", corr, {"incident": inc["key"], "kind": inc["kind"], "ticket": inc.get("corr"), "job_id": jid, "day": day},
               f"case.enqueued:{inc['key']}")
        log("case review enqueued", inc["key"], "job", jid)


def knowledge_root_exists(repo: str) -> bool:
    try:
        gh("GET", f"repos/{repo}/contents/{case.KNOWLEDGE_ROOT}/README.md")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def knowledge_issue_step(cur, now: datetime | None = None):
    """事例の分析（case.analyzed）のうち、最初に回すテーマの学びがあるものを、知見の変更セットの Issue にする（ADR 0024 の 3）。
    工場の implement ジョブが事例記録と知見の下書き（draft・hypothesis）を 1 本の PR に書く。PR は change_type_rules の knowledge 種別"""
    if not KNOWLEDGE_ISSUES or DRY_RUN:
        return
    now = now or datetime.now(timezone.utc)
    day = f"{now.astimezone(timezone(timedelta(hours=9))):%Y-%m-%d}"
    cur.execute("select count(*) as n from events where name='knowledge.case_filed' and payload->>'day'=%s", (day,))
    room = KNOWLEDGE_ISSUES_PER_DAY - cur.fetchone()["n"]
    if room <= 0:
        return
    try:
        with open(CASE_REVIEW_DEFS, encoding="utf-8") as f:
            themes = case.first_themes(yaml.safe_load(f) or {})
    except OSError:
        return
    roots: dict[str, bool] = {}
    for a in case.pending_knowledge(cur, themes, room):
        m = re.match(r"github:([^#]+)#\d+$", a.get("ticket") or "")
        repo = m.group(1) if m and m.group(1) in REPOS else REPOS[0]
        if repo not in roots:
            roots[repo] = knowledge_root_exists(repo)
        if not roots[repo]:
            continue
        title, body = case.knowledge_issue(a, themes)
        try:
            issue = gh("POST", f"repos/{repo}/issues", {"title": title, "body": body, "labels": ["factory:planned", "factory:learn"]})
        except Exception as e:  # noqa: BLE001  次の周回でやり直す
            log("knowledge issue failed", a["incident"], repr(e))
            continue
        record(cur, "knowledge.case_filed", f"github:{repo}#{issue['number']}",
               {"incident": a["incident"], "analysis_event": a["event_id"], "themes": sorted({x["theme"] for x in a["learnings"]}),
                "learnings": len(a["learnings"]), "issue": issue["html_url"], "day": day}, f"knowledge.case_filed:{a['incident']}")
        log("knowledge issue filed", a["incident"], issue["html_url"])


def design_review_due(now: datetime, schedule: dict) -> bool:
    """その週（ISO 週）の指定の曜日・時（JST）を過ぎたか。過ぎていれば週末まで拾う"""
    wd = WEEKDAYS.index(schedule.get("weekday", "saturday"))
    return (now.weekday(), now.hour) >= (wd, int(schedule.get("hour", 2)))


def design_review_step(cur, now: datetime | None = None):
    """週 1 回、製品ごとに設計の診断のジョブを積む（ADR 0022）。固定 Issue に積み、結果は dispatcher が書く。予算の枠は見ない（週 1 回で小さい）"""
    if not DESIGN_REVIEW or DRY_RUN:
        return
    now = (now or datetime.now(timezone.utc)).astimezone(design.JST)
    try:
        defs = yaml.safe_load(open(DESIGN_REVIEW_DEFS, encoding="utf-8"))
    except OSError:
        return
    if not design_review_due(now, defs.get("schedule") or {}):
        return
    week = design.week_of(now)
    for repo in QUALITY_REPOS:
        cur.execute("select 1 from events where dedupe_key=%s", (f"supervisor.design_review:{repo}:{week}",))
        if cur.fetchone():
            continue
        try:
            issue = design.ensure_issue(gh, repo)
        except Exception as e:  # noqa: BLE001  次の周回でやり直す
            log("design review issue failed", repo, repr(e))
            continue
        corr = f"github:{repo}#{issue}"
        prev = design.previous_findings(cur, repo)
        try:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, model_tier, kind) values (%s,%s,%s,'supervisor','tier-judge','design_review') returning id",
                        (corr, repo, issue))
        except psycopg.errors.UniqueViolation:
            continue   # 同じ Issue のジョブが待ち・実行中
        jid = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                    (corr, jid, Jsonb({"by": "supervisor", "kind": "design_review", "week": week, "input": {"previous": prev}})))
        record(cur, "supervisor.design_review_enqueued", corr, {"repo": repo, "week": week, "issue": issue, "job_id": jid}, f"supervisor.design_review:{repo}:{week}")
        log("design review enqueued", repo, week, "job", jid)


def tick():
    with db() as conn, conn.cursor() as cur:
        record(cur, "supervisor.heartbeat", None, {"repos": REPOS})
        rules, manifests = load_rules(), {}
        open_prs = merge_step(cur, rules, manifests)
        judge_step(cur, manifests)
        enqueue_step(cur, open_prs)
        daily_report_step(cur)
        release_train_step(cur)
        learn_step(cur)
        rule_transition_step(cur)
        case_review_step(cur)
        knowledge_issue_step(cur)
        design_review_step(cur)


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
