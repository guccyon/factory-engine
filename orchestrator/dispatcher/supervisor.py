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
- 合成の利用者（ADR 0043 の O4）: 週に 1 回（definitions/qa.yaml の synthetic）、製品ごとに QA 役の合成の利用者のジョブを積む
- 品質の Learn（ADR 0043 の O9）: 週に 1 回、Defect と Incident から QA の重点と回帰のシナリオを足し、基準の変更案を知見の Issue にして委員会の列に入れる
- 夜間の探索（ADR 0028 の nightly_explore）: JST の夜（definitions/qa.yaml の explore.hours）に、製品ごとに 1 晩 1 回、QA 役の探索のジョブを積む。
  予算の枠を使い切っていれば次の枠を待つ。ラインの一時停止中は explore.through_pause が真ならこのジョブだけ流す
- 出荷の列車・定期便（ADR 0012、ADR 0043 の O6）: JST の RELEASE_TRAIN_HOUR 時を過ぎて、その製品が今日の定期便にまだ載っておらず、まだ Release に入っていない
  マージ済みの工場の PR があり、出荷が処理中でも承認待ちでもなければ、次の版（minor を 1 つ上げる）の Release を切って staging への出荷を依頼し、
  その日の便（release.train）に載せる。prod へは便ごとの承認を待つ（train.py）
- 工場の事故の週次の見回り（ADR 0046 の順番 3・4）: 週 1 回、その週の記録で調査委員会のジョブ（incident_review）を積み、結果の Engine の改善 Issue の案を起票する
- 工場の事故の日次の見回り（ADR 0046）: definitions/incident.yaml の時（JST）を過ぎたら 1 日 1 回、費用の記録を決定論で集計して兆しを残し、medium 以上は台帳に開く（factory_incident.py）
- PR ごとの使い捨て環境（ADR 0043 の O7）: 立っている環境の PR が閉じた記録（ADR 0050）があるか、EPHEMERAL_TTL_HOURS を過ぎたら壊す依頼を書く（ephemeral.py）

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

import ghapi
import ghapp
from globmatch import glob_re
import psycopg
import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import case
import committee
import continuation
import design
import desk_investigate
import drain
import ephemeral
import factory_incident
import judgment
import line
import ticket_sync
import learn
import version
import ops
import opportunity
import patrol_watch
import plan_outcome
import process_learn
import qa_line
import quality_learn
import report
import report_kpi
import sendback
import serialize
import ship
import stoppage
import train
import usage
import workflow_guard
import workflow_shadow

ENV = os.environ
DATABASE_URL = ENV["DATABASE_URL"]
REPOS = [r.strip() for r in ENV.get("FACTORY_REPOS", "").split(",") if r.strip()]
GH_TOKEN = ENV.get("SUPERVISOR_GH_TOKEN") or None   # 空なら App か GH_TOKEN（ghapp。ADR 0041）
INTERVAL = int(ENV.get("SUPERVISOR_INTERVAL_SEC", "60"))
SERIALIZE = ENV.get("SUPERVISOR_SERIALIZE", "1") == "1"   # よく衝突するファイルに触るタスクを並べて流す（ADR 0027 の 2）。0 で止める
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
CHANGELOG_COMPILE_EVERY = int(ENV.get("CHANGELOG_COMPILE_EVERY", "10"))   # 断片がこの数たまったら取りまとめる（ADR 0027 の 1。仮置き）
RELEASE_TRAIN = [r.strip() for r in ENV.get("RELEASE_TRAIN", "").split(",") if r.strip()]   # 列車を走らせる製品（出荷の形 ship.yaml を持つもの）
DAILY_REPORT_HOUR = int(ENV.get("DAILY_REPORT_HOUR", "5"))   # JST。claude.ai のページは 6 時にこの結果から描く。Mac が寝ていた日は起きたときに出す
# 使用量の枠ごとの予算（2026-10-29 オーナー: Max の使用量は毎朝 7:00 から 5 時間ごとにリセット。1 日 約 $100 を枠ごとに均し、
# 平日の日中は別の業務でも使うので夕方以降に寄せる）。「開始時:予算ドル」を JST で並べる。空にすると予算で止めない。値は仮置きで運用で直す
# BUDGET_WINDOWS_WEEKDAY / BUDGET_WINDOWS_WEEKEND（既定と読み方は usage.py）。予算の宣言（budget.set）があればそちらが優先
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
COMMITTEE = ENV.get("COMMITTEE", "1") == "1"   # 知見の変更セットの PR を委員会の 4 役に審査させる（ADR 0024 の 4）。0 で止める（人の承認だけになる）
COMMITTEE_WEEKLY = ENV.get("COMMITTEE_WEEKLY", "1") == "1"   # 知見の委員会の週 1 の定例（ADR 0024）。0 で止める
KNOWLEDGE_DEFS = ENV.get("KNOWLEDGE_DEFINITIONS", "/definitions/knowledge.yaml")
KNOWLEDGE_ISSUES_PER_DAY = int(ENV.get("KNOWLEDGE_ISSUES_PER_DAY", "2"))   # 委員会ができるまでは知見の PR は人の承認（knowledge 種別）なので少なく
PROCESS_ISSUES = ENV.get("PROCESS_ISSUES", "1") == "1"   # 工程の信号の束の分析から、工場への改善案の Issue を起票する（ADR 0027 の 3）。0 で止める
PROCESS_ISSUES_PER_DAY = int(ENV.get("PROCESS_ISSUES_PER_DAY", "1"))   # 束の分析は 1 日 1 件なので、起票も 1 件まで
ENGINE_REPO = ENV.get("ENGINE_REPO", "guccyon/factory-engine")   # 工場自体のリポジトリ（改善案の起票先）
PROCESS_LABEL = "factory:process-proposal"
# 夜間の探索（ADR 0028 の nightly_explore）。探索する製品（触れる環境を持つもの）。既定は出荷の列車の製品。空で止める
NIGHTLY_EXPLORE = [r.strip() for r in ENV.get("NIGHTLY_EXPLORE", ",".join(RELEASE_TRAIN)).split(",") if r.strip()]
# 週次の KPI の Report ライン（ADR 0043 の O2）を回す製品。既定は出荷の列車の製品（staging と ship.yaml を持つもの）。空で止める
KPI_WEEKLY = [r.strip() for r in ENV.get("KPI_WEEKLY", ",".join(RELEASE_TRAIN)).split(",") if r.strip()]
QA_DEFS = ENV.get("QA_DEFINITIONS", "/definitions/qa.yaml")
# 出荷の QA の関門（ADR 0049）。staging に配備した版に判定と探索を並行して積み、報告を承認に添える製品。既定は出荷の列車の製品。空で止める
QA_GATE = [r.strip() for r in ENV.get("QA_GATE", ",".join(RELEASE_TRAIN)).split(",") if r.strip()]
# 合成の利用者（ADR 0043 の O4）。週に 1 回（definitions/qa.yaml の synthetic.weekday・hours）歩かせる製品。既定は夜間の探索と同じ。空で止める。
# わざと仕込む詰まりは SYNTH_FRICTION（example か JSON。staging だけ）
SYNTHETIC_USERS = [r.strip() for r in ENV.get("SYNTHETIC_USERS", ",".join(NIGHTLY_EXPLORE)).split(",") if r.strip()]
# 品質の Learn（ADR 0043 の O9）。週に 1 回（definitions/defects.yaml の learn）Defect と Incident を集計し、QA の重点とシナリオを足し、基準の変更案を委員会へ。0 で止める
QUALITY_LEARN = ENV.get("QUALITY_LEARN", "1") == "1"
DEFECT_DEFS = ENV.get("DEFECT_DEFINITIONS", "/definitions/defects.yaml")
STRICTNESS = ["auto", "shadow", "human_approve", "forbidden"]
DEP_RE = re.compile(r"(?:依存|depends\s+on)\s*[:：]\s*((?:#\d+[\s,、]*)+)", re.IGNORECASE)


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), "[supervisor]", *a, flush=True)


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)


def gh(method: str, path: str, body: dict | None = None):
    return ghapi.request(method, path, body, user_agent="factory-supervisor", token=GH_TOKEN)   # GET は ETag で条件付き（上限に数えない 304）


def record(cur, name: str, corr: str | None, payload: dict, dedupe_key: str | None = None) -> bool:
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values (%s,%s,%s,%s) on conflict (dedupe_key) do nothing returning id",
                (name, corr, Jsonb(payload), dedupe_key))
    return cur.fetchone() is not None


# ------------------------------------------------------------------ 変更種別の写像（表を引くだけ）
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
            dormant = entry.get("from") == "operation_start" and not operation_started(manifest)
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


def operation_started(manifest: dict) -> bool:
    """運用開始か。製品の段階（product_stage）が operating なら運用開始（definitions/product_stages.yaml）。
    段階の欄がなかった頃の autonomy.operation_started も読む"""
    return bool(ship.STAGES.get(ship.declared_stage(manifest), {}).get("operation_started")) or bool(manifest.get("autonomy", {}).get("operation_started"))


def judge_constraints(au: dict, started: bool | None = None) -> str:
    """judge に渡す manifest の制約。from: operation_start の常時承認は、運用開始（autonomy.operation_started）までは
    マージの判定（上の always_human_approve の扱い）と同じく効かせないので、judge にも「今は人に回さない」と分けて渡す。
    以前は全項目を効いているものとして渡し、開発中の identity の変更を judge が人に回していた（rally #130）"""
    started = bool(au.get("operation_started")) if started is None else started
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


def ci_failure_step(cur, p: dict, sha: str, mergeable_state: str | None = None, pr: dict | None = None):
    """工場の PR の CI が落ちた・衝突しているなら停止として開く（検査・judge は CI が通るまで積まないので、黙って止まらないように）。
    事実は PR の見張り（pr_events、ADR 0015）が発行したイベントから読む。GitHub には問い合わせない。次の head で CI が通れば閉じる。PR が閉じても閉じる"""
    key = f"ci:{p['url']}"
    cur.execute("select name, payload from events where name in ('pr.ci_failed','pr.ci_passed','pr.conflicted') "
                "and payload->>'pr_url'=%s and payload->>'head_sha'=%s", (p["url"], sha))
    seen = {r["name"]: r["payload"] for r in cur.fetchall()}
    if ("pr.conflicted" in seen or mergeable_state == "dirty" or "pr.ci_failed" in seen) and continuation.pending(cur, p["url"], sha):
        return   # 工場の続きのジョブが直している最中。人には回さない（ADR 0017）
    tried = continuation.attempts(cur, p["url"], "ci_failed")
    if ("pr.conflicted" in seen or mergeable_state == "dirty") and pr is not None and (awaiting_decision(cur, p["url"], pr) or over_budget(cur)):
        # 人の判断待ちと予算の枠を使い切っている間は衝突を解かない（conflict_step）。解けなかった扱いの停止は開かず、開いていれば閉じる
        stoppage.close_stoppage(cur, key, remedy="人の判断待ち。判断が付いたら衝突を解く", closed_by="supervisor")
        return
    if ("pr.conflicted" in seen or mergeable_state == "dirty") and workflow_guard.waiting(cur, p["corr"]):
        # 衝突の解消の上限に当たり、同じファイルに触るチケットが終わるのを待っている（ADR 0038 の順番 2）。待ちの間は人に回さない
        stoppage.close_stoppage(cur, key, remedy="衝突の解消の上限に当たり、同じファイルに触るチケットを待っている", closed_by="supervisor", quiet=True)
        return
    if "pr.conflicted" in seen or mergeable_state == "dirty":   # 衝突（2026-10-31。これまでは何も起きず止まっていた）
        cap = workflow_guard.capped(cur, p["corr"])
        stoppage.open_stoppage(cur, key, "ticket", "工場の PR が main と衝突している。検査と judge に進めない",
                               waiting_for=(f"衝突の解消が上限（{cap.get('cap')} 回）に当たり、待った後の 1 回でも衝突した（計 {cap.get('conflicts')} 回）。"
                                            "工場はもう解かない。builder が衝突を解くか、チケットを切り直すか、差し戻す") if cap else
                               "工場の衝突の解消が通らなかった。builder が衝突を解くか、差し戻す", correlation_id=p["corr"], detected_by="supervisor")
    elif "pr.ci_failed" in seen:
        stoppage.open_stoppage(cur, key, "ticket", f"工場の PR の CI が落ちた（{', '.join(seen['pr.ci_failed'].get('failed') or [])}）。検査と judge に進めない",
                               waiting_for=f"工場の続きのジョブ（{tried} 回）でも通らなかった。builder が直すか、差し戻す" if tried else "builder が直すか、差し戻す", correlation_id=p["corr"], detected_by="supervisor")
    elif "pr.ci_passed" in seen:
        stoppage.close_stoppage(cur, key, remedy="次の head で CI が通った（衝突も解けた）", closed_by="supervisor")


def waiting_for_human(cur, url: str, pr: dict | None = None) -> bool:
    """人を待っている PR か（judge が人に回した、差し戻しを流さなかった、CI 落ち・衝突、表が承認を求めた、知見の委員会が人に回した、
    factory:needs-judgment のラベルが付いている、ほかの PR を待つ保留）。
    同時に流す件数の枠には数えない。承認待ちをラインのクリティカルパスにしない（2026-10-31 オーナー、11-01 に委員会の分を足した）。
    ラベルと保留は 11-02 に足した。ラベルの付いた PR は衝突を解かずに止まる（awaiting_decision）のに枠を取り、
    rally #527・#534・#589 が動かないまま 3 枠を埋めていた"""
    if pr is not None and any(l.get("name") == "factory:needs-judgment" for l in pr.get("labels") or []) and not judgment.decisions_settled(cur, url):
        return True   # 人に回した判断がすべて決まっていれば、ラベルが残っていても人は待っていない（awaiting_decision と同じ見方）
    for prefix in ("judge:", "sendback:", "ci:", "hold:"):
        if stoppage.open_keys(cur, f"{prefix}{url}"):
            return True
    d = committee.decided(cur, url)
    if d and d.get("outcome") == "human":
        return True
    cur.execute("select payload->>'table' as t from events where name='supervisor.merge_decided' and payload->>'pr_url'=%s order by id desc limit 1", (url,))
    d = cur.fetchone()
    return bool(d and d["t"] == "human_approve")


def awaiting_decision(cur, url: str, pr: dict) -> bool:
    """人の判断を待っている PR か（judge が人に回した・差し戻しを流さなかった・委員会が人に回した・表が承認を求めた、
    ほかの PR を待つ保留、または factory:needs-judgment のラベルが付いている）。CI 落ち・衝突の停止（ci:）は含めない（工場が自分で直す対象）。
    judge が人に回した項目にすべて人の判断が付いていれば、ラベルと judge: の停止は待ちに数えない。差し戻しの直しのあとに
    衝突すると、衝突を解かない → judge をやり直せない → ラベルも停止も外れない、で止まっていた（11-02 rally #592）"""
    settled = judgment.decisions_settled(cur, url)
    if not settled and any(l.get("name") == "factory:needs-judgment" for l in pr.get("labels") or []):
        return True
    # ほかの PR を待つ保留（hold:）の間も解かない。待っている PR が入るまで main が動くたびに衝突し直す（11-02 rally #589、5 回・$64）
    for prefix in (("sendback:", "hold:") if settled else ("judge:", "sendback:", "hold:")):
        if stoppage.open_keys(cur, f"{prefix}{url}"):
            return True
    d = committee.decided(cur, url)
    if d and d.get("outcome") == "human":
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
    if over_budget(cur):
        # 予算の枠を使い切っている間は、衝突の解消も積まない（枠が戻った最初の周期で、その時点の head で 1 回だけ解く）。
        # 新しい実装だけを止めていた 11-02 は、枠の上限 $700 に対して衝突の解消だけで $1,420 使った
        return
    if awaiting_decision(cur, p["url"], pr):
        # 人の判断待ちの間は解かない。main が動くたびに解き直して費用がかさむ（11-01 rally #505、約 3 時間で 6 回・$36）。
        # 判断が付いて待ちが外れたら、その時点の head で 1 回だけ解く
        return
    cur.execute("select 1 from events where dedupe_key=%s", (f"continue:{p['url']}@{pr['head']['sha']}:conflict",))
    if cur.fetchone():
        return   # この head はもう積んである（解いている途中か、解けなかった）。上限を数え直さない
    # 衝突の解消の上限（ADR 0038 の順番 2）。チケットの合計で上限に当たったら、同じファイルに触るチケットが終わるまで待ち、
    # 待ちの後に 1 回だけ解く。それでも衝突したら、もう解かない（ci_failure_step が人への停止を開く）。rally #1054 は 13 回解いていた
    gate = workflow_guard.conflict_gate(cur, p["corr"], p["url"], lambda: inflight_touched_files(cur))
    if gate != "ok":
        log("conflict fix held", p["url"], gate)
        return
    jid = continuation.enqueue(cur, repo=p["repo"], pr_number=p["number"], pr_url=p["url"], branch=pr["head"]["ref"],
                               head_sha=pr["head"]["sha"], reason="conflict", by="supervisor")
    if isinstance(jid, int):
        log("conflict fix enqueued", p["url"], "job", jid)


def ci_rerun_step(cur, p: dict, sha: str) -> list[str]:
    """PR の見張りが記録した「起動に失敗したワークフロー」（pr.ci_unstarted、試行 1 回目）を 1 回だけ再実行する（ADR 0050 の決定 2 の形:
    見張りが記録し、内ループの処理が記録を読んで動く）。GitHub の一過性の起動の失敗で、検査も judge も積まれずに黙って止まらないように
    （2026-11-10 rally #1358）。再実行でも起動しなければ、見張りが CI 落ちとして記録し、CI 落ちの流れ（続きのジョブ → 人）に乗る。
    返り値は再実行を頼んだワークフローの名前"""
    cur.execute("select payload from events u where u.name='pr.ci_unstarted' and u.payload->>'pr_url'=%s and u.payload->>'head_sha'=%s "
                "and (u.payload->>'attempt')::int = 1 and not exists (select 1 from events r where r.dedupe_key = "
                "'pr.ci_rerun:' || (u.payload->>'pr_url') || '@' || (u.payload->>'head_sha') || ':' || (u.payload->>'run_id')) order by u.id",
                (p["url"], sha))
    done = []
    for r in cur.fetchall():
        u = r["payload"]
        if DRY_RUN:
            continue
        try:
            gh("POST", f"repos/{p['repo']}/actions/runs/{u['run_id']}/rerun")
        except Exception as e:  # noqa: BLE001  頼めなければ次の周回でもう一度試す（記録は頼めたときだけ残す）
            log("ci rerun failed", p["url"], u.get("workflow"), repr(e))
            continue
        record(cur, "pr.ci_rerun", p["corr"], {"pr_url": p["url"], "head_sha": sha, "workflow": u.get("workflow"), "run_id": u["run_id"]},
               f"pr.ci_rerun:{p['url']}@{sha}:{u['run_id']}")
        done.append(u.get("workflow"))
    if done:
        log("ci rerun", p["url"], ", ".join(str(d) for d in done))
    return done


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


def human_fix_step(cur, p: dict, pr: dict):
    """judge が人に回した判断に人が直す答えを出したら（受信トレイの judgment:<id>。judgment.decided の fix）、
    その指摘を同じ担当の続きのジョブ（review_fix）に渡す。人の答えは人が出した数だけなので、続きの上限（CONTINUE_MAX）では切らない。
    判断がそろって直しがなければ、残った factory:needs-judgment のラベルを外す（ラベルは次の judge を待たずに外れる）"""
    if (pr["head"].get("repo") or {}).get("full_name") != p["repo"]:
        return
    fixes = judgment.pending_fixes(cur, p["url"])
    if fixes:
        sha = pr["head"]["sha"]
        cur.execute("select payload->>'mode' as m from artifacts where type='Judgment' and payload->>'stage'='judged' and payload->>'pr_url'=%s "
                    "order by id desc limit 1", (p["url"],))
        mode = (cur.fetchone() or {}).get("m") or "attended"
        signal = {"pr_url": p["url"], "mode": mode, "head_sha": sha, "by": "human", "defects": [f["fix"] for f in fixes]}
        jid = continuation.enqueue(cur, repo=p["repo"], pr_number=p["number"], pr_url=p["url"], branch=pr["head"]["ref"], head_sha=sha,
                                   reason="review_fix", by="human", signal=signal, limit=False)
        if isinstance(jid, int):
            record(cur, "judgment.fix_enqueued", p["corr"], {"pr_url": p["url"], "head_sha": sha, "job_id": jid, "decided": [f["id"] for f in fixes]})
            log("human fix enqueued", p["url"], "job", jid, "fixes", len(fixes))
        return
    if (any(l.get("name") == "factory:needs-judgment" for l in pr.get("labels") or []) and judgment.decisions_settled(cur, p["url"])
            and not stoppage.open_keys(cur, f"judge:{p['url']}")):
        try:
            gh("DELETE", f"repos/{p['repo']}/issues/{p['number']}/labels/{urllib.parse.quote('factory:needs-judgment', safe='')}")
        except Exception as e:  # noqa: BLE001
            log("needs-judgment label removal failed", p["url"], repr(e))


def merge_step(cur, rules: dict, manifests: dict) -> set:
    """返り値は、同時上限に数える開いた工場の PR の correlation_id（人を待つ PR は数えない）"""
    open_corrs: set = set()
    knowledge_turns: dict[str, tuple[set, str | None]] = {}
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
        if p["repo"] not in knowledge_turns:   # 周期ごとに製品 1 回だけ引く
            knowledge_turns[p["repo"]] = ({q["corr"] for q in committee.queue(cur, p["repo"])}, committee.turn(cur, p["repo"]))
        queued, current = knowledge_turns[p["repo"]]
        if p["corr"] in queued and p["corr"] != current:
            continue   # 知見は製品ごとに 1 件ずつ直列に流す。順番が来るまで衝突も解かず委員会にも持ち込まない（同時上限にも数えない）
        # 差し戻しの Issue を人が直しに回したら（factory:planned）、流さなかった差し戻しの停止を閉じてから衝突・マージを見る。
        # 停止が開いたままだと衝突を解かず、直しの Issue は元 PR のマージを待つので循環して止まる（11-09 rally #1228・#1251）
        # 受信トレイで「流す」と答えたものは、ここでラベルを替える（受信トレイの入口は GitHub のトークンを持たない）
        if not DRY_RUN:
            sendback.apply_flow(cur, p["url"], gh)
        sendback.release_if_fixing(cur, p["url"], p["corr"])
        if not pr.get("draft"):
            # 衝突と CI 落ちは、まず同じ担当の続きのジョブで直す（ADR 0016・0017）。直せない・上限に達したときだけ停止（ci:）を開いて人へ
            if pr.get("mergeable_state") == "dirty":
                conflict_step(cur, p, pr)
            else:
                ci_rerun_step(cur, p, pr["head"]["sha"])
                ci_fix_step(cur, p, pr)
            ci_failure_step(cur, p, pr["head"]["sha"], pr.get("mergeable_state"), pr)
            human_fix_step(cur, p, pr)
        if not waiting_for_human(cur, p["url"], pr):
            open_corrs.add(p["corr"])
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
            if d["table"] == "human_approve":
                gh("POST", f"repos/{p['repo']}/issues/{p['number']}/labels", {"labels": [APPROVAL_LABEL]})
        elif DRY_RUN:
            continue
        # 検査は判定の記録と切り離して周回ごとに試す（head ごとに 1 回積めば、以後は記録で素通り）。
        # 判定を記録した周回でしか試さないと、同じ PR の別のジョブ（body_fix など）と重なって積めなかった head の検査が
        # 二度と積まれず、検査のない PR が同時に流す枠を塞ぎ続ける（2026-11-05 rally #976、約 6 時間半ラインが止まった）
        enqueue_inspect(cur, p, sha)
        if "knowledge" in d["change_types"]:
            enqueue_committee(cur, p, sha)   # 周回ごとに試す。同じ PR の検査ジョブが動いている間は積めない（1 PR 1 ジョブ）
        # マージは表が auto で、かつ検査役が承認してから（ADR 0014）。承認は周期ごとに見直すので、検査・judge が終わった周期でマージする
        if not d["execute"]:
            continue
        cur.execute("select 1 from events where dedupe_key=%s", (f"supervisor.merged:{p['url']}",))
        if cur.fetchone():
            continue   # マージ済み。pr.merged の記録待ち
        ok, basis = inspector_approved(cur, p["url"], sha)
        if not ok:
            continue
        if "knowledge" in d["change_types"] and not committee.approved(cur, p["url"]):
            continue   # 知見の変更セットは委員会の採用を待つ（ADR 0024）
        record(cur, "supervisor.inspector_approved", p["corr"], {"pr_url": p["url"], "head_sha": sha, "basis": basis}, f"inspector.approved:{p['url']}@{sha}")
        gh("PUT", f"repos/{p['repo']}/pulls/{p['number']}/merge", {"merge_method": "squash", "sha": sha})
        record(cur, "supervisor.merged", p["corr"], {"pr_url": p["url"], "head_sha": sha, "basis": basis}, f"supervisor.merged:{p['url']}")
        log("merged", p["url"], basis)
        open_corrs.discard(p["corr"])
    return open_corrs


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
    if judgment.pending_fixes(cur, pr_url):
        return False, "人が直す答えを出した判断を、同じ担当に渡す前"
    if body_fix_pending(cur, pr_url):
        return False, "採用した本文の指摘を直している（body_fix）"
    n_adopt = sum(1 for v in verdicts if v == "adopt")
    return True, f"judge が {len(verdicts)} 件を裁いた（採用 {n_adopt}。採用した指摘は続きのジョブか差し戻しで直す）"


def body_fix_pending(cur, pr_url: str) -> bool:
    """その PR の本文の直し（judge が積む body_fix）が順番待ちか作業中か。終わる前にマージすると、直しがマージの後の本文に入る
    （2026-11-05 rally #911: マージが body_fix の終わりより 2 秒早かった）"""
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)$", pr_url)
    if not m:
        return False
    cur.execute("select 1 from jobs where repo=%s and issue_number=%s and kind='body_fix' and state in ('queued','running') limit 1",
                (m[1], int(m[2])))
    return cur.fetchone() is not None


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
        # 同じ PR のジョブ（judge・委員会・body_fix など）がまだ動いている。次の周回で積み直せるよう記録を消す（judge_step と同じ）。
        # 消さないと、この head の検査は二度と積まれず、検査役の承認が「検査待ち」のまま止まる（rally #458）。
        # 積み直しは merge_step が周回ごとに呼ぶことで行う（rally #976）
        cur.execute("delete from events where dedupe_key=%s", (f"supervisor.inspect:{p['url']}@{sha}",))
        return
    jid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                (p["corr"], jid, Jsonb({"by": "supervisor", "kind": "inspect", "pr_url": p["url"], "head_sha": sha})))
    log("inspect enqueued", p["url"], "job", jid)


def enqueue_committee(cur, p: dict, sha: str):
    """知見の変更セットの PR を、CI を通った後に委員会へ持ち込む（ADR 0024 の 4）。PR ごとに 1 回。
    委員会は議決を PR のブランチに積むので head が変わるが、議決があれば積み直さない"""
    if not COMMITTEE or committee.decided(cur, p["url"]):
        return
    rnd = committee.rounds(cur, p["url"])   # 直してかけ直す（revise）・かけ直しの指示（reopen）のたびに区切りが進む
    key = f"supervisor.committee:{p['url']}" + (f":{rnd}" if rnd else "")
    if rnd and not committee.revised_head_moved(cur, p["url"], sha):
        return   # revise の直しがまだ push されていない
    cur.execute("select 1 from events where dedupe_key=%s", (key,))
    if cur.fetchone():
        return
    try:
        cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, model_tier, kind) values (%s,%s,%s,'supervisor','tier-judge','committee_review') returning id",
                    (f"committee:{p['url']}", p["repo"], p["number"]))
    except psycopg.errors.UniqueViolation:
        return   # 同じ PR の検査・judge が待ち・実行中。次の周回で積む
    jid = cur.fetchone()["id"]
    record(cur, "supervisor.committee_enqueued", p["corr"], {"pr_url": p["url"], "head_sha": sha, "job_id": jid, "round": rnd}, key)
    if rnd:
        try:   # 人に回したときの承認待ちのラベルを外す（かけ直しの結果でまた付く）
            gh("DELETE", f"repos/{p['repo']}/issues/{p['number']}/labels/{urllib.parse.quote(committee.APPROVAL_LABEL)}")
        except Exception:  # noqa: BLE001  付いていなければ 404
            pass
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                (f"committee:{p['url']}", jid, Jsonb({"by": "supervisor", "kind": "committee_review", "pr_url": p["url"], "head_sha": sha})))
    log("committee enqueued", p["url"], "job", jid)


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
        constraints = judge_constraints(au, operation_started(manifests[repo]))
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


def budget_window(now: datetime, decl: dict | None = None) -> tuple[datetime, float] | None:
    """今（JST）の使用量の枠の開始時刻と予算。枠の読み方は使用量の集計と 1 か所にまとめた（usage.py）"""
    return usage.budget_windows(now, decl)


def over_budget(cur) -> dict | None:
    """今の枠の予算を使い切っていれば、その様子を返す（新しい実装と衝突の解消を積まない。作業中のもの・検査・judge・指摘の直しは止めない）"""
    w = budget_window(datetime.now(timezone(timedelta(hours=9))), usage.declared(cur))   # 宣言（設定・factoryctl budget）を周回ごとに読む
    if w is None:
        return None
    start, usd = w
    cur.execute("select coalesce(sum((payload->>'cost_usd')::numeric), 0) as spent from events where name='job.finished' and occurred_at >= %s", (start,))
    spent = float(cur.fetchone()["spent"])
    return {"window_start": start.isoformat(), "budget_usd": usd, "spent_usd": round(spent, 2)} if spent >= usd else None


def inflight_count(cur, open_prs) -> int:
    """同時に流しているチケットの数。待ち・実行中のジョブと、開いた工場の PR（人を待たないもの）を、チケット（correlation_id）ごとに 1 と数える。
    以前はジョブの数と PR の数を足していたので、PR の衝突・CI の直しの続きのジョブが PR と二重に数えられ、
    直しが重なると新しいタスクが投入されなくなった（11-02 朝、21 件と数えて上限 13 で止まった）"""
    cur.execute("select coalesce(correlation_id, 'job:' || id::text) as corr from jobs where state in ('queued','running')")
    return len({r["corr"] for r in cur.fetchall()} | set(open_prs))


_pr_files_cache: dict[str, set[str]] = {}   # PR の url@head → 変更ファイル
_issue_body_cache: dict[str, str] = {}


def inflight_touched_files(cur) -> dict[str, set[str]]:
    """流れているチケットが触る（触る見込みの）ファイル。開いた工場の PR はその変更ファイル、PR のまだないジョブは見込みのファイル"""
    out: dict[str, set[str]] = {}
    for p in factory_prs(cur):
        cur.execute("select 1 from events where name in ('pr.merged','pr.closed_unmerged') and payload->>'pr_url'=%s", (p["url"],))
        if cur.fetchone():
            continue
        try:
            pr = gh("GET", f"repos/{p['repo']}/pulls/{p['number']}")
            if pr["state"] != "open" or waiting_for_human(cur, p["url"], pr):
                continue   # 人の判断を待つ PR では並べない（何日も待つと、同じファイルに触るタスクが止まってしまう）
            key = f"{p['url']}@{pr['head']['sha']}"
            if key not in _pr_files_cache:
                _pr_files_cache[key] = {f["filename"] for f in pr_files(p["repo"], p["number"])}
            out.setdefault(p["corr"], set()).update(_pr_files_cache[key])
        except Exception as e:  # noqa: BLE001  分からない PR は数えない（並べ損ねても、衝突は今までどおり続きのジョブが解く）
            log("inflight files failed", p["url"], repr(e))
    cur.execute("select distinct correlation_id as corr, repo, issue_number as n from jobs where state in ('queued','running') and kind='implement'")
    for j in cur.fetchall():
        if j["corr"] in out or not j["n"]:
            continue
        if j["corr"] not in _issue_body_cache:
            try:
                _issue_body_cache[j["corr"]] = gh("GET", f"repos/{j['repo']}/issues/{j['n']}").get("body") or ""
            except Exception as e:  # noqa: BLE001
                log("inflight issue failed", j["corr"], repr(e))
                continue
        out[j["corr"]] = serialize.expected_files(cur, j["corr"], _issue_body_cache[j["corr"]])
    return out


def enqueue_step(cur, open_prs: set):
    inflight = inflight_count(cur, open_prs)
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
            labels = [l.get("name", "") for l in it.get("labels") or []]
            if workflow_guard.needs_planner(cur, repo, it["number"], labels):
                # 複数の役を通るタスクは必ずプランナーを通す（ADR 0038 の追記）。Plan のないチケットは実装に回さず、プランナーの入口へ
                if workflow_guard.record_rerouted(cur, repo, it["number"], labels):
                    log("rerouted to planner", repo, it["number"])
                if not DRY_RUN:
                    try:
                        gh("DELETE", f"repos/{repo}/issues/{it['number']}/labels/{urllib.parse.quote(PLANNED_LABEL, safe='')}")
                    except urllib.error.HTTPError as e:
                        if e.code != 404:
                            raise
                    gh("POST", f"repos/{repo}/issues/{it['number']}/labels", {"labels": [workflow_guard.PLANNER_LABEL]})
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
    hot = serialize.hot_files(cur) if ready and SERIALIZE else {}
    inflight_files: dict[str, set[str]] | None = None   # よく衝突するファイルに触るタスクがあるときだけ、流れているものの変更ファイルを集める
    if paused and record(cur, "supervisor.budget_paused", None, paused, f"supervisor.budget_paused:{paused['window_start']}"):
        log("budget paused", paused)
    max_inflight = line.spec(cur)["tickets"]   # 宣言（factoryctl line scale）から周回ごとに読む（ADR 0028）
    for t, repo, it, deps in ready:
        if inflight >= max_inflight and t["priority"] < ops.PREEMPT_AT:
            continue
        if paused and t["priority"] < ops.PREEMPT_AT:   # 枠の予算を使い切ったら次の枠まで投入しない（p0 は止めない）
            continue
        corr = f"github:{repo}#{it['number']}"
        dup = serialize.same_purpose(cur, repo, corr, it.get("title") or "")
        if dup:   # 同じ目的のチケットが流れていれば投入しない（ADR 0044 の決定 8 の 4。人の判断を待つ PR でも待つ）
            if serialize.record_duplicate(cur, corr, it.get("title") or "", dup):
                log("duplicate held", corr, "behind", dup)
            continue
        expected = serialize.expected_files(cur, corr, it.get("body")) if hot else set()
        if hot and any(serialize.touches(expected, h) for h in hot):
            # 衝突を避ける並べ方（ADR 0027 の 2）: 同じよく衝突するファイルに触るタスクが流れていれば、終わるまで投入しない
            if inflight_files is None:
                inflight_files = inflight_touched_files(cur)
            b = serialize.blocker(expected, hot, inflight_files, corr)
            if b:
                if serialize.record(cur, corr, b[0], b[1], hot[b[0]]):
                    log("serialized", corr, b[0], "behind", b[1])
                continue
        try:   # 宣言の start: after（ADR 0038 の 4b）。順番 1 は守らせず、前のチケットがまだ開いていれば待たせたはずと記録するだけ
            after = workflow_shadow.declared_after(cur, corr)
            open_after = [n for n in after if issue_state(repo, n) != "closed"] if after else []
            if open_after and workflow_shadow.would_wait(cur, corr, open_after):
                log("workflow would wait", corr, open_after)
        except Exception as e:  # noqa: BLE001
            log("workflow start check failed", corr, repr(e)[:200])
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
        if inflight_files is not None:
            inflight_files[corr] = expected


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
                       env={**ENV, "GH_TOKEN": GH_TOKEN or ghapp.token()})
    log("daily report", r.returncode, (r.stdout or r.stderr).strip()[-300:])


def notify_report_step(cur):
    """定期レポート（ADR 0032 の決定 9）。前日の分を毎朝 1 回組み立てて report.built に残し、通知の割り当てどおりに送る。
    設定（製品の notifications.yaml）がない工場は組み立てて残すだけで、どこにも送らない"""
    if DRY_RUN:
        return
    try:
        r = report.daily_step(cur)
    except Exception as e:  # noqa: BLE001  レポートの失敗で周回を止めない
        log("notify report failed", repr(e)[:300])
        return
    for kind, x in (r or {}).items():
        if x.get("sent") or x.get("failed") or x.get("gave_up"):
            log("notify report", kind, x)


def stage_step(cur, manifests: dict):
    """製品の段階の宣言（manifest.yaml の product_stage）を読み、変わったときだけ product.stage_observed に残す（ADR 0028 の決定 4）。
    shipper・コンソール・受け箱は GitHub を見ずにこの記録を読む。manifest を読めない製品（Engine など）は飛ばし、記録がなければ development に倒す"""
    for repo in dict.fromkeys(RELEASE_TRAIN + REPOS):
        try:
            if repo not in manifests:
                manifests[repo] = load_manifest(repo)
        except Exception:  # noqa: BLE001, S112  manifest がない・読めないときは前の記録のまま
            continue
        stage = ship.declared_stage(manifests[repo])
        if ship.observe_stage(cur, repo, stage):
            log("product stage", repo, stage or "(未宣言)", "→", ship.stage_of(cur, repo)["stage"])


def release_train_step(cur):
    """定期便（ADR 0012、ADR 0043 の O6）。1 日 1 便、製品ごとに Release を切ってまとめて staging に出し、release.train に残す。
    判断は持たず、条件を見るだけ（便の中身と承認は train.py）"""
    if DRY_RUN or not RELEASE_TRAIN:
        return
    now = datetime.now(timezone.utc)
    if now.astimezone(timezone(timedelta(hours=9))).hour < RELEASE_TRAIN_HOUR:
        return
    tid, rels = train.regular(cur, RELEASE_TRAIN, now)
    if tid:
        log("release train", tid, [r["release_id"] for r in rels])


def changelog_due(n: int, now_jst: datetime, compiled_today: bool) -> bool:
    """取りまとめの Issue を起こすか。断片が CHANGELOG_COMPILE_EVERY 件以上、または出荷の列車の 1 時間前を過ぎて断片があり、今日まだ取りまとめていない"""
    if n <= 0:
        return False
    return n >= CHANGELOG_COMPILE_EVERY or (now_jst.hour >= RELEASE_TRAIN_HOUR - 1 and not compiled_today)


def changelog_step(cur, now: datetime | None = None):
    """CHANGELOG の断片（changelog.d/）の取りまとめの Issue を起こす（ADR 0027 の 1）。1 時間に 1 回、製品ごとに見る。
    取りまとめはワーカーが core-gates/changelog_compile.py で決定的に行う（factory:changelog のラベル）"""
    if DRY_RUN:
        return
    now = (now or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=9)))
    for repo in REPOS:
        if not record(cur, "changelog.checked", None, {"repo": repo}, f"changelog.checked:{repo}:{now:%Y-%m-%dT%H}"):
            continue
        try:
            items = gh("GET", f"repos/{repo}/contents/changelog.d")
        except Exception:  # noqa: BLE001  断片の置き場がない製品（404）や一時的な失敗は次の周回で
            continue
        n = sum(1 for x in items or [] if x.get("type") == "file" and x.get("name", "").endswith(".md") and x.get("name") != "README.md")
        cur.execute("select 1 from events where name='changelog.compile_filed' and payload->>'repo'=%s and payload->>'day'=%s",
                    (repo, f"{now:%Y-%m-%d}"))
        if not changelog_due(n, now, cur.fetchone() is not None):
            continue
        try:
            if gh("GET", f"repos/{repo}/issues?labels=factory:changelog&state=open&per_page=1"):
                continue   # 取りまとめの Issue がまだ開いている
            issue = gh("POST", f"repos/{repo}/issues", {
                "title": f"CHANGELOG の取りまとめ（断片 {n} 件）",
                "body": "changelog.d/ の断片を CHANGELOG.md の [Unreleased] に移し、移した断片を消す（ADR 0027 の 1）。"
                        "ワーカーがエージェントを使わずに core-gates/changelog_compile.py で行う。",
                "labels": ["factory:planned", "factory:changelog"]})
        except Exception as e:  # noqa: BLE001
            log("changelog issue failed", repo, repr(e))
            continue
        record(cur, "changelog.compile_filed", f"github:{repo}#{issue['number']}", {"repo": repo, "fragments": n, "day": f"{now:%Y-%m-%d}",
                                                                                   "issue": issue["html_url"]})
        log("changelog compile filed", repo, n, issue["html_url"])


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
    proc = process_learn.retrospect(cur)   # 工程の信号（ADR 0027 の 3）。分析役へは case_review_step が回す
    process_learn.record_retrospective(cur, proc, day)
    log("process retrospective", day, "signals", proc["signals"], "cost", proc["cost"],
        "routed", [c["signature"] for c in proc["clusters"] if c["route"]])
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
    """overlay のルールの状態の移り変わりを JST の 1 日 1 回判定し、変える Issue を起票する（ADR 0013 の 6・8）。判定は govern 側（ここ）が events から計算し、
    ワーカーは state の 1 行を書き換えるだけ。ルール変更 PR は factory_rule として検査を通って自動でマージされる"""
    if DRY_RUN or not LEARN_RULE_TRANSITIONS:
        return
    now = now or datetime.now(timezone.utc)
    day = f"{now.astimezone(timezone(timedelta(hours=9))):%Y-%m-%d}"
    # 1 日 1 回（ADR 0050 の決定 3）。記録で数えるので、プロセスを入れ直しても 1 日 1 回のまま。見回りの見回りもこの記録を見る。
    # 記録は見終わってから付ける（GitHub が読めずに途中で落ちたら、次の周回でやり直す）
    cur.execute("select 1 from events where dedupe_key=%s", (f"learn.rule_transitions_checked:{day}",))
    if cur.fetchone():
        return
    for repo in REPOS:
        try:
            rules = overlay_rules(repo)
        except urllib.error.HTTPError as e:
            if e.code == 404:   # overlay のない製品
                continue
            log("rule transition read failed", repo, repr(e)[:200])
            return   # 記録を付けずに抜ける（次の周回でやり直す。後ろの処理は止めない）
        except Exception as e:  # noqa: BLE001
            log("rule transition read failed", repo, repr(e)[:200])
            return
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
    record(cur, "learn.rule_transitions_checked", None, {"day": day, "repos": REPOS}, f"learn.rule_transitions_checked:{day}")


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
        record(cur, "case.enqueued", corr, {"incident": inc["key"], "kind": inc["kind"], "ticket": inc.get("corr"), "job_id": jid, "day": day,
                                            **({"signature": inc["signature"]} if inc.get("signature") else {})},
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
    工場の implement ジョブが事例記録と知見の下書き（draft・hypothesis）を 1 本の PR に書く。PR は change_type_rules の knowledge 種別。
    まだ起票していない分析がキューで、製品ごとに 1 件ずつ取り出す（committee.turn）。前の知見がマージされてから書くので、番号がぶつからない"""
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
    busy: set[str] = set()
    for a in case.pending_knowledge(cur, themes, room):
        m = re.match(r"github:([^#]+)#\d+$", a.get("ticket") or "")
        repo = m.group(1) if m and m.group(1) in REPOS else REPOS[0]
        if repo not in roots:
            roots[repo] = knowledge_root_exists(repo)
        if not roots[repo]:
            continue
        if repo in busy or committee.turn(cur, repo):
            busy.add(repo)
            continue   # 知見は製品ごとに 1 件ずつ。流れている知見が済む（または人の判断待ちに移る）まで次を起票しない
        busy.add(repo)
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


def process_issue_step(cur, now: datetime | None = None):
    """工程の信号の束の分析（case.analyzed の kind=process）を、工場のリポジトリへの改善案の Issue にする（ADR 0027 の 3）。
    同じ signature の改善案が開いていれば足さない（記録だけ）。実装とマージは人の承認"""
    if not PROCESS_ISSUES or DRY_RUN:
        return
    now = now or datetime.now(timezone.utc)
    day = f"{now.astimezone(timezone(timedelta(hours=9))):%Y-%m-%d}"
    cur.execute("select count(*) as n from events where name='process.proposal_filed' and payload->>'day'=%s and payload->>'issue' is not null", (day,))
    room = PROCESS_ISSUES_PER_DAY - cur.fetchone()["n"]
    if room <= 0:
        return
    pending = case.pending_process(cur, room)
    if not pending:
        return
    try:
        open_titles = {i.get("title", ""): i.get("html_url") for i in gh("GET", f"repos/{ENGINE_REPO}/issues?state=open&labels={PROCESS_LABEL}&per_page=100") or []}
    except Exception as e:  # noqa: BLE001  次の周回でやり直す
        log("process proposals list failed", repr(e))
        return
    clusters = {c["signature"]: c for c in process_learn.retrospect(cur, now)["clusters"]}
    for a in pending:
        sig = case.signature_of(a["incident"])
        dup = next((u for t, u in open_titles.items() if f"工程の改善案: {sig}（" in t), None)
        if dup:
            record(cur, "process.proposal_filed", None, {"incident": a["incident"], "signature": sig, "duplicate_of": dup, "day": day},
                   f"process.proposal_filed:{a['incident']}")
            continue
        title, body = case.process_issue(a, clusters.get(sig) if clusters.get(sig, {}).get("route") else None)
        try:
            issue = gh("POST", f"repos/{ENGINE_REPO}/issues", {"title": title, "body": body, "labels": [PROCESS_LABEL]})
        except Exception as e:  # noqa: BLE001
            log("process proposal failed", a["incident"], repr(e))
            continue
        record(cur, "process.proposal_filed", None, {"incident": a["incident"], "signature": sig, "analysis_event": a["event_id"],
                                                     "proposals": len(a.get("learnings") or []), "issue": issue["html_url"], "day": day},
               f"process.proposal_filed:{a['incident']}")
        log("process proposal filed", sig, issue["html_url"])


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


def committee_weekly_step(cur, now: datetime | None = None):
    """週 1 回、製品ごとに知見の委員会の定例のジョブを積む（ADR 0024）。固定 Issue に積み、まとめと起票は dispatcher が行う"""
    if not COMMITTEE_WEEKLY or DRY_RUN:
        return
    now = (now or datetime.now(timezone.utc)).astimezone(design.JST)
    try:
        with open(KNOWLEDGE_DEFS, encoding="utf-8") as f:
            weekly = ((yaml.safe_load(f) or {}).get("committee") or {}).get("weekly") or {}
    except OSError:
        return
    if not design_review_due(now, {"weekday": weekly.get("weekday", "sunday"), "hour": weekly.get("hour", 3)}):
        return
    week = design.week_of(now)
    for repo in REPOS:
        key = f"supervisor.committee_weekly:{repo}:{week}"
        cur.execute("select 1 from events where dedupe_key=%s", (key,))
        if cur.fetchone() or not knowledge_root_exists(repo):
            continue
        try:
            issue = committee.weekly_issue(gh, repo, weekly.get("issue_label", committee.WEEKLY_LABEL))
        except Exception as e:  # noqa: BLE001  次の周回でやり直す
            log("committee weekly issue failed", repo, repr(e))
            continue
        corr = f"github:{repo}#{issue}"
        inp = committee.weekly_input(cur, repo, now)
        try:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, model_tier, kind) values (%s,%s,%s,'supervisor','tier-judge','committee_weekly') returning id",
                        (corr, repo, issue))
        except psycopg.errors.UniqueViolation:
            continue
        jid = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                    (corr, jid, Jsonb({"by": "supervisor", "kind": "committee_weekly", "week": week, "max_issues": weekly.get("max_issues", 4), "input": inp})))
        record(cur, "supervisor.committee_weekly_enqueued", corr, {"repo": repo, "week": week, "issue": issue, "job_id": jid}, key)
        log("committee weekly enqueued", repo, week, "job", jid)


def ephemeral_step(cur):
    """PR ごとの使い捨て環境（ADR 0043 の O7）。立っている環境の PR が閉じたか、EPHEMERAL_TTL_HOURS を過ぎたら壊す依頼を書く。
    PR が閉じたかは events の記録で見る（PR の見張りの小さなポーリングが github.pr_closed を残す。ADR 0050）。GitHub は見ない"""
    if DRY_RUN:
        return
    for repo, pr, reason in ephemeral.due_down(cur):
        ok, m = ephemeral.request_down(cur, repo, pr, "supervisor", "ephemeral_step", reason)
        log("ephemeral down", repo, pr, reason, m)


def incident_patrol_step(cur):
    """工場の事故の日次の見回り（ADR 0046）。definitions/incident.yaml の committee.patrol.daily の時を過ぎたら 1 日 1 回、決定論の集計で兆しを残し、
    medium 以上は台帳に開く（dispatcher/factory_incident.py）。LLM は使わない。日報（DAILY_REPORT_HOUR）より前の時に置く"""
    if DRY_RUN:
        return
    try:
        got = factory_incident.daily_step(cur)
    except Exception as e:  # noqa: BLE001  見回りの失敗で周回を止めない
        log("incident patrol failed", repr(e)[:300])
        return
    if got:
        log("incident patrol", got["day"], "signs", len(got["signs"]), "opened", got["opened"])


def incident_review_step(cur, now: datetime | None = None):
    """工場の事故の週次の見回り（ADR 0046 の順番 3・4）。committee.patrol.weekly の曜日と時（JST）を過ぎたら週 1 回、その週の記録を
    制御プレーンだけで組み立ててジョブ（kind=incident_review、governance の枠）に積む。予算の枠を使い切っているときは積まず、次の周回で見る。
    ラインの一時停止中も流す（見回りはラインを止めても動く）。結果で残った Engine の改善 Issue の案は、ここで起票する（周回ごとに 2 件まで）"""
    if DRY_RUN:
        return
    now = now or datetime.now(timezone.utc)
    try:
        if not over_budget(cur):
            got = factory_incident.enqueue_weekly(cur, now, REPOS)
            if got:
                if drain.active(cur):
                    drain.allow(cur, [got["corr"]], "supervisor", f"事故の週次の見回り（{got['week']}）")
                record(cur, "supervisor.incident_review_enqueued", got["corr"], got, f"supervisor.incident_review:{got['week']}")
                log("incident review enqueued", got["week"], "job", got["job_id"])
        filed = factory_incident.file_issues(cur, gh, ENGINE_REPO, PROCESS_LABEL)
        if filed:
            log("incident issues filed", filed)
        filed = desk_investigate.file_issues(cur, gh, ENGINE_REPO, version.engine_version(ENV))   # 窓口の一次調査の改善 Issue と引き継ぎ（ADR 0048）
        if filed:
            log("investigation issues filed", filed)
    except Exception as e:  # noqa: BLE001  見回りの失敗で周回を止めない
        log("incident review step failed", repr(e)[:300])


def nightly_explore_step(cur, now: datetime | None = None):
    """夜に 1 回、製品ごとに QA 役の探索のジョブを積む（ADR 0028 の nightly_explore）。判断は持たず、時刻・予算・一時停止を見るだけ。
    積めなかった理由は 1 晩 1 回だけ supervisor.nightly_explore_skipped に残す（朝の日報が「昨夜は回らなかった」と出す）"""
    if not NIGHTLY_EXPLORE or DRY_RUN:
        return
    try:
        with open(QA_DEFS, encoding="utf-8") as f:
            defs = {**qa_line.load_defs(), "explore": (yaml.safe_load(f) or {}).get("explore") or qa_line.DEFS["explore"]}
    except OSError:
        defs = qa_line.load_defs()
    xd = defs["explore"]
    now = (now or datetime.now(timezone.utc)).astimezone(design.JST)
    lo, hi = (int(h) for h in xd.get("hours") or [1, 5])
    if not lo <= now.hour < hi:
        return
    night = qa_line.night_of(now)
    for repo in NIGHTLY_EXPLORE:
        key = f"supervisor.nightly_explore:{repo}:{night}"
        cur.execute("select 1 from events where dedupe_key=%s", (key,))
        if cur.fetchone():
            continue
        why = None
        ob = over_budget(cur)
        if ob:
            why = {"reason": "over_budget", **ob}
        elif drain.active(cur) and not xd.get("through_pause"):
            why = {"reason": "paused"}
        if why:
            record(cur, "supervisor.nightly_explore_skipped", None, {"repo": repo, "night": night, **why}, f"supervisor.nightly_explore_skipped:{repo}:{night}:{why['reason']}")
            continue
        try:
            got = qa_line.enqueue_explore(cur, gh, repo, night, defs=defs)
        except psycopg.errors.UniqueViolation:
            continue   # 同じ固定 Issue の QA のジョブが待ち・実行中。次の周回で
        except Exception as e:  # noqa: BLE001  次の周回でやり直す
            log("nightly explore enqueue failed", repo, repr(e)[:200])
            continue
        if drain.active(cur):
            drain.allow(cur, [got["corr"]], "supervisor", f"夜間の探索（{night} の夜）")
        record(cur, "supervisor.nightly_explore_enqueued", got["corr"], {"repo": repo, "night": night, "charter": got["charter"], "issue": got["issue"],
                                                                          "job_id": got["job_id"], "release_id": got["release_id"], "through_pause": drain.active(cur)}, key)
        log("nightly explore enqueued", repo, night, got["charter"], "job", got["job_id"])


def qa_gate_step(cur, now: datetime | None = None):
    """出荷の QA の関門（ADR 0049）。判断は持たず、qa_gate.step に記録を見て次の手を打たせるだけ。
    予算の枠を使い切っていれば新しく積まない（夜間の探索と同じ）。ラインの一時停止中も関門のジョブは流す（出荷は一時停止と別に動くので）"""
    if not QA_GATE or DRY_RUN:
        return
    import qa_gate
    defs = _qa_defs()
    may = not over_budget(cur)

    def allow(got):
        if drain.active(cur):
            drain.allow(cur, [got["corr"]], "supervisor", "出荷の QA の関門")

    for repo in QA_GATE:
        try:
            out = qa_gate.step(cur, gh, repo, defs=defs, now=now, may_enqueue=may, on_enqueued=allow, log=log)
        except Exception as e:  # noqa: BLE001  見回りの失敗で周回を止めない
            log("qa gate step failed", repo, repr(e)[:300])
            continue
        if out["opened"] or out["enqueued"] or out["decided"]:
            log("qa gate", repo, "opened", out["opened"], "enqueued", [(x["which"], x["job_id"]) for x in out["enqueued"]],
                "decided", (out["decided"] or {}).get("release_id"), (out["decided"] or {}).get("candidates"))


def kpi_weekly_step(cur, now: datetime | None = None):
    """週次の KPI の Report ライン（ADR 0043 の O2・O3）。週に 1 回、出荷の列車の製品ごとに staging で KPI を測る依頼を書き、
    測れた結果を前回と比べて Report・Opportunity の下書き（受信トレイ）・見込みの誤差（kpi.forecast_scored）にする。決定的でトークンを使わない"""
    if DRY_RUN or not KPI_WEEKLY:
        return
    try:
        report_kpi.request_step(cur, KPI_WEEKLY, now, log=log)
        report_kpi.report_step(cur, log=log)
    except Exception as e:  # noqa: BLE001  報告の失敗でラインを止めない。次の周回でやり直す
        log("kpi weekly step error", repr(e)[:300])


def opportunity_ticket_step(cur):
    """受信トレイで「起票する」と答えた改善の提案を、プランナー宛ての Issue にする（受信トレイの入口は GitHub のトークンを持たない）"""
    if DRY_RUN:
        return
    try:
        opportunity.file_requested(cur, gh, log=log)
    except Exception as e:  # noqa: BLE001  起票の失敗でラインを止めない。次の周回でやり直す
        log("opportunity ticket step error", repr(e)[:300])


def _qa_defs() -> dict:
    """definitions/qa.yaml の explore・synthetic・gate（なければ qa_line の写し）"""
    try:
        with open(QA_DEFS, encoding="utf-8") as f:
            full = yaml.safe_load(f) or {}
    except OSError:
        full = {}
    return {**qa_line.load_defs(), **{k: full[k] for k in ("explore", "synthetic", "gate") if full.get(k)}}


def synthetic_users_step(cur, now: datetime | None = None):
    """週に 1 回、製品ごとに合成の利用者のジョブを積む（ADR 0043 の O4）。判断は持たず、曜日・時刻・予算・一時停止を見るだけ。
    決めた曜日の時刻（hours の 1 つめ）を過ぎたら、その週（ISO 週、JST）のうちに 1 回積む（ADR 0050 の決定 3）"""
    if not SYNTHETIC_USERS or DRY_RUN:
        return
    defs = _qa_defs()
    sd = defs["synthetic"]
    now = (now or datetime.now(timezone.utc)).astimezone(design.JST)
    lo = int((sd.get("hours") or [5, 7])[0])
    if (now.weekday(), now.hour) < (int(sd.get("weekday", 6)), lo):
        return   # その週の曜日・時刻を過ぎたら、週のうちに 1 回（ADR 0050 の決定 3。Mac が眠っていても起きた周回で取り戻す）
    week = qa_line.week_of(now)
    for repo in SYNTHETIC_USERS:
        key = f"supervisor.synthetic_users:{repo}:{week}"
        cur.execute("select 1 from events where dedupe_key=%s", (key,))
        if cur.fetchone():
            continue
        if over_budget(cur) or (drain.active(cur) and not sd.get("through_pause")):
            continue   # 同じ時間帯の後の周回で積めることもある
        try:
            got = qa_line.enqueue_synthetic(cur, gh, repo, defs=defs, week=week,
                                            friction=qa_line.friction_from(ENV.get(sd.get("friction_env") or "SYNTH_FRICTION"), defs))
        except psycopg.errors.UniqueViolation:
            continue   # 同じ固定 Issue の QA のジョブが待ち・実行中。次の周回で
        except Exception as e:  # noqa: BLE001  次の周回でやり直す
            log("synthetic users enqueue failed", repo, repr(e)[:200])
            continue
        if drain.active(cur):
            drain.allow(cur, [got["corr"]], "supervisor", f"合成の利用者（{week}）")
        record(cur, "supervisor.synthetic_users_enqueued", got["corr"], {"repo": repo, "week": week, "issue": got["issue"], "job_id": got["job_id"],
                                                                          "release_id": got["release_id"], "planted": sorted(got["friction"])}, key)
        log("synthetic users enqueued", repo, week, "job", got["job_id"], "planted", sorted(got["friction"]))


def quality_learn_step(cur, now: datetime | None = None):
    """週に 1 回、品質の Learn を回す（ADR 0043 の O9）。集計・重点・シナリオは決定論。基準の変更案は知見の Issue にして委員会の列に入れる"""
    if not QUALITY_LEARN or DRY_RUN:
        return
    try:
        with open(DEFECT_DEFS, encoding="utf-8") as f:
            ld = {**quality_learn.DEFS, **((yaml.safe_load(f) or {}).get("learn") or {})}
    except OSError:
        ld = quality_learn.DEFS
    now = now or datetime.now(timezone.utc)
    for repo in REPOS:
        try:
            quality_learn.step(cur, repo, now, ld, log=log)
            if KNOWLEDGE_ISSUES and knowledge_root_exists(repo) and not committee.turn(cur, repo):
                quality_learn.file_proposals(cur, gh, repo, now, log=log)
        except Exception as e:  # noqa: BLE001  集計の失敗でラインを止めない
            log("quality learn step error", repo, repr(e)[:200])


def plan_outcome_step(cur):
    """結果の出た工場の PR を、最新の Plan と突き合わせて plan.outcome に残す（ADR 0036 の 4a。決定的）"""
    try:
        plan_outcome.step(cur, gh, log=log)
    except Exception as e:  # noqa: BLE001  突き合わせの失敗でラインを止めない
        log("plan outcome step error", repr(e)[:200])


def workflow_shadow_step(cur):
    """ワークフローの宣言と実績を突き合わせ、守らせていたら止めた・飛ばしたところを workflow.shadow に残す（ADR 0038 の順番 1。止めない）"""
    try:
        workflow_shadow.step(cur, log=log)
    except Exception as e:  # noqa: BLE001  記録の失敗でラインを止めない
        log("workflow shadow step error", repr(e)[:200])


def ticket_sync_step(cur):
    """着手中なのに Issue が閉じたチケットを台帳から外す（ticket_sync.py、2026-11-10 オーナー）。
    Issue が閉じたかは PR の見張りが残す github.issue_closed を読む（ADR 0050）。GitHub は見ない"""
    if DRY_RUN:
        return
    try:
        ticket_sync.apply(cur, log=log)
    except Exception as e:  # noqa: BLE001  台帳の片付けの失敗でラインを止めない
        log("ticket sync step error", repr(e)[:200])


def patrols_enabled() -> list[str]:
    """いま有効な見回りの id（ADR 0050 の決定 1）。heartbeat に書き、番人の見回りの見回りがこの一覧だけを見る。
    環境変数や定義で止めたものは載せない（止めたものに停止を開かない）"""
    if DRY_RUN:
        return []
    incident = (factory_incident.load_defs().get("committee") or {}).get("patrol") or {}
    return patrol_watch.enabled_ids({
        "nightly_explore": bool(NIGHTLY_EXPLORE),
        "synthetic_users": bool(SYNTHETIC_USERS),
        "design_review": DESIGN_REVIEW and bool(QUALITY_REPOS) and os.path.exists(DESIGN_REVIEW_DEFS),
        "committee_weekly": COMMITTEE_WEEKLY and os.path.exists(KNOWLEDGE_DEFS),
        "incident_daily": (incident.get("daily") or {}).get("enabled") is not False,
        "incident_weekly": bool(REPOS) and (incident.get("weekly") or {}).get("enabled") is not False,
        "daily_report": bool(DAILY_REPORT) and os.path.exists(DAILY_REPORT),
        "notify_report": True,
        "retrospective": True,
        "rule_transition": LEARN_RULE_TRANSITIONS and bool(REPOS),
        "quality_learn": QUALITY_LEARN and bool(REPOS),
        "kpi_weekly": bool(KPI_WEEKLY),
    })


def tick():
    with db() as conn, conn.cursor() as cur:
        try:
            patrols = patrols_enabled()
        except Exception as e:  # noqa: BLE001  宣言の失敗で heartbeat を止めない（宣言がなければ見回りの見回りは何も見ない）
            log("patrols declaration failed", repr(e)[:200])
            patrols = []
        cur.execute("insert into events(name, payload) values ('supervisor.heartbeat', %s) returning id",
                    (Jsonb({"repos": REPOS, "engine_version": version.engine_version(ENV), "patrols": patrols}),))   # 更新役が新しい版で回ったかを見る（ADR 0031）
        hb = cur.fetchone()["id"]
        rules, manifests = load_rules(), {}
        open_prs = merge_step(cur, rules, manifests)
        plan_outcome_step(cur)
        workflow_shadow_step(cur)
        # 同時に流しているチケットの数を heartbeat に残す（操業コンソールの流量の表示が読む。コンソールは GitHub を見ないため）
        cur.execute("update events set payload = payload || %s where id = %s", (Jsonb({"tickets_inflight": inflight_count(cur, open_prs)}), hb))
        stage_step(cur, manifests)
        judge_step(cur, manifests)
        enqueue_step(cur, open_prs)
        incident_patrol_step(cur)
        ticket_sync_step(cur)
        daily_report_step(cur)
        notify_report_step(cur)
        changelog_step(cur)
        release_train_step(cur)
        qa_gate_step(cur)
        ephemeral_step(cur)
        nightly_explore_step(cur)
        kpi_weekly_step(cur)
        opportunity_ticket_step(cur)


        synthetic_users_step(cur)
        learn_step(cur)
        rule_transition_step(cur)
        case_review_step(cur)
        knowledge_issue_step(cur)
        process_issue_step(cur)
        design_review_step(cur)
        committee_weekly_step(cur)
        incident_review_step(cur)
        quality_learn_step(cur)


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
