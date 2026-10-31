#!/usr/bin/env python3
"""建設状況の日次レポート（govern の経路。LLM は使わない）。

definitions/roadmap.yaml の項目を取得元から測ってロードマップの進捗を出し、前日の動きと オーナー に頼みたいことを並べる。
- ダッシュボード: 工場状況とは別の固定 Issue（ラベル `factory:construction`）の本文を毎回書き換える
- 日次レポート: 同じ Issue に 1 日 1 回コメントを足す
- 推移: 毎回のスナップショットを events の `report.daily`（JST の日付で 1 件）に残し、そこから折れ線を描く

使い方:
  status_report.py --dry-run                 # 書かずに Markdown を標準出力へ
  status_report.py --publish                 # Issue を書き換え、コメントを足し、スナップショットを記録する
  status_report.py --publish --force         # 今日の分が記録済みでも出し直す

環境変数:
  GH_TOKEN（なければ gh auth token）、DATABASE_URL（なければ制御プレーンの項目は「不明」）、
  BUILD_WORK_DIR（建設作業フォルダ 50_build-work/。なければそこから測る項目は「不明」）、
  STATUS_REPO（既定 guccyon/factory-engine）、REPORT_REPOS（既定 guccyon/rally,guccyon/factory-engine）、ROADMAP（既定は同じリポジトリの definitions/roadmap.yaml）

Issue や PR の本文は件数を数えるだけで、シェルにもプロンプトにも渡さない。
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "quality"))
import ingest as quality_ingest  # noqa: E402  内部品質の取り込み（health/quality/ingest.py、ADR 0019）

ENV = os.environ
# 内部品質の定期診断を取り込む製品（製品の CI に quality.yml があるもの）。既定は工場の製品すべて（quality.yml がなければ取り込むものがないだけ）
QUALITY_REPOS = [r.strip() for r in (ENV.get("QUALITY_REPOS") or ENV.get("FACTORY_REPOS", "")).split(",") if r.strip()]
JST = timezone(timedelta(hours=9))   # slim イメージに tzdata がないので固定のオフセット
HERE = Path(__file__).resolve().parent
ROADMAP = Path(ENV.get("ROADMAP") or HERE.parent.parent / "definitions" / "roadmap.yaml")
STATUS_REPO = ENV.get("STATUS_REPO", "guccyon/factory-engine")
REPOS = [r.strip() for r in ENV.get("REPORT_REPOS", "guccyon/rally,guccyon/factory-engine").split(",") if r.strip()]
BUILD_WORK_DIR = ENV.get("BUILD_WORK_DIR", "")
DATABASE_URL = ENV.get("DATABASE_URL", "")
LABEL = "factory:construction"
COST_BUDGET_USD = float(ENV.get("DAILY_COST_BUDGET_USD", "100"))   # 1 日の費用の目安（API 単価換算）。Max 5x の範囲に収まるペース（2026-10-29 オーナー）
START_DATE = ENV.get("CONSTRUCTION_START", "2026-10-25")   # 建設の初日（日ごとの系列の起点）
FACTORY_BRANCH_RE = re.compile(r"^factory/issue-\d+-job-\d+")
SNAPSHOT_MARK = "factory-snapshot:v1"   # Issue の本文の末尾に埋める JSON（base64）。claude.ai のページはこれから描く
APPROVAL_LABEL = "factory:needs-approval"
# マージに必ず人の承認が要るリポジトリ（マージ基準。工場そのものの変更）。ラベルが無くても、下書きでない開いた PR はすべて承認待ちとして載せる
HUMAN_MERGE_REPOS = [r.strip() for r in ENV.get("HUMAN_MERGE_REPOS", "guccyon/factory-engine").split(",") if r.strip()]
TREND_DAYS = 14


class Unknown(Exception):
    """取得元に届かず測れなかった"""


# ------------------------------------------------------------------ 取得元
def gh_token() -> str:
    t = ENV.get("GH_TOKEN") or ENV.get("GITHUB_TOKEN")
    if t:
        return t
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


TOKEN = gh_token()


def gh(method: str, path: str, body: dict | None = None):
    headers = {"Accept": "application/vnd.github+json", "Content-Type": "application/json", "User-Agent": "factory-status-report"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(f"https://api.github.com/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        return json.loads(data) if data else None


CLOSING_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#\d+", re.IGNORECASE)


def merged_prs(repo: str, pages: int = 3) -> list[dict]:
    """閉じた PR を更新の新しい順に取り、マージされたものだけ返す。search API は使わない（リポジトリ単位のエンドポイントだけで済ませる）"""
    out = []
    for page in range(1, pages + 1):
        batch = gh("GET", f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}")
        out += [p for p in batch if p.get("merged_at")]
        if len(batch) < 100:
            break
    return out


def labeled_open_prs(repo: str, label: str) -> list[dict]:
    items = gh("GET", f"repos/{repo}/issues?state=open&per_page=100&labels={urllib.parse.quote(label)}")
    return [i for i in items if "pull_request" in i]


def open_prs(repo: str) -> list[dict]:
    return gh("GET", f"repos/{repo}/pulls?state=open&per_page=100")


def approval_prs(now: datetime) -> list[dict]:
    """承認待ちの PR を古い順に返す。ラベル factory:needs-approval（supervisor の判定）と、HUMAN_MERGE_REPOS の下書きでない開いた PR"""
    seen: dict[tuple, dict] = {}

    def add(repo: str, number: int, title: str, url: str, created: str, why: str):
        key = (repo, number)
        if key not in seen:
            days = (now - parse_ts(created)).total_seconds() / 86400 if created else None
            seen[key] = {"repo": repo, "number": number, "title": title, "url": url, "why": why,
                         "days": round(days, 1) if days is not None else None}

    for r in REPOS:
        for i in labeled_open_prs(r, APPROVAL_LABEL):
            add(r, i["number"], i["title"], i["html_url"], i.get("created_at", ""), "label")
    for r in HUMAN_MERGE_REPOS:
        for p in open_prs(r):
            if not p.get("draft"):
                add(r, p["number"], p["title"], p["html_url"], p.get("created_at", ""), "repo")
    return sorted(seen.values(), key=lambda x: -(x["days"] or 0))


def yaml_files(repo: str, path: str) -> list[dict]:
    entries = gh("GET", f"repos/{repo}/contents/{urllib.parse.quote(path)}")
    out = []
    for e in entries:
        if e["type"] == "file" and e["name"].endswith((".yaml", ".yml")):
            raw = base64.b64decode(gh("GET", f"repos/{repo}/contents/{urllib.parse.quote(e['path'])}")["content"])
            out.append({"name": e["name"], "data": yaml.safe_load(raw) or {}})
    return out


def db_conn():
    if not DATABASE_URL:
        return None
    try:
        import psycopg
        return psycopg.connect(DATABASE_URL, autocommit=True)
    except Exception as e:  # noqa: BLE001  DB に届かなくてもレポートは出す（該当項目は不明）
        print("[status-report] DB に接続できない:", repr(e), file=sys.stderr)
        return None


def db_value(conn, sql: str, args: tuple = ()):
    if conn is None:
        raise Unknown("制御プレーンに届かない")
    with conn.cursor() as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
        return row[0] if row else None


def build_work() -> Path:
    if not BUILD_WORK_DIR or not Path(BUILD_WORK_DIR).is_dir() or not any(Path(BUILD_WORK_DIR).iterdir()):
        raise Unknown("建設作業フォルダが渡されていない")   # compose は未設定のとき空のディレクトリをマウントする
    return Path(BUILD_WORK_DIR)


# ------------------------------------------------------------------ 項目の測定
def measure(m: dict, conn) -> dict:
    """{progress: 0〜1, value: 表示用, unknown: bool}"""
    try:
        if "manual" in m:
            v = m["manual"]
            p = 1.0 if v == "done" else 0.0 if v == "todo" else float(v)
            return {"progress": p, "value": {1.0: "済", 0.0: "未"}.get(p, f"{round(p * 100)}%")}
        if "github_merged_prs" in m:
            s = m["github_merged_prs"]
            prs = merged_prs(s["repo"])
            n = sum(1 for p in prs if not s.get("closing") or CLOSING_RE.search(p.get("body") or ""))
            return {"progress": min(n / s["target"], 1.0), "value": f"{n} / {s['target']}", "n": n}
        if "github_files" in m:
            s = m["github_files"]
            files = yaml_files(s["repo"], s["path"])
            where = s.get("where") or {}
            n = sum(1 for f in files if all(f["data"].get(k) == v for k, v in where.items()))
            return {"progress": min(n / s["target"], 1.0), "value": f"{n} / {s['target']}", "n": n}
        if "build_work_files" in m:
            s = m["build_work_files"]
            n = len(glob.glob(str(build_work() / s["glob"])))
            return {"progress": min(n / s["target"], 1.0), "value": f"{n} / {s['target']}", "n": n}
        if "pairwise" in m:
            s = m["pairwise"]
            files = sorted(glob.glob(str(build_work() / s["glob"])))
            if not files:
                return {"progress": 0.0, "value": "未計測"}
            d = json.loads(Path(files[-1]).read_text())
            n, acc = int(d.get("n") or 0), float(d.get("pairwise_accuracy") or 0)
            p = min(n / s["target_n"], 1.0) * (1.0 if acc >= s["threshold"] else 0.5)
            return {"progress": p, "value": f"{round(acc * 100)}%（N={n} / {s['target_n']}）", "n": n}
        if "db" in m:
            s = m["db"]
            n = float(db_value(conn, s["sql"]) or 0)
            return {"progress": min(n / s["target"], 1.0), "value": f"{n:g} / {s['target']}", "n": n}
        raise Unknown(f"measure の種類を知らない: {sorted(m)}")
    except Unknown as e:
        return {"progress": 0.0, "value": f"不明（{e}）", "unknown": True}
    except Exception as e:  # noqa: BLE001  1 項目の失敗でレポートを止めない
        return {"progress": 0.0, "value": f"不明（{type(e).__name__}）", "unknown": True}


def roadmap_progress(conn) -> dict:
    spec = yaml.safe_load(ROADMAP.read_text())
    phases, wsum, acc = [], 0.0, 0.0
    for ph in spec["phases"]:
        items = [{"id": it["id"], "ja": it["ja"], **measure(it["measure"], conn)} for it in ph["items"]]
        p = sum(i["progress"] for i in items) / len(items) if items else 0.0
        phases.append({"id": ph["id"], "ja": ph["ja"], "weight": ph["weight_weeks"], "progress": round(p, 3), "items": items})
        wsum += ph["weight_weeks"]
        acc += ph["weight_weeks"] * p
    return {"overall": round(acc / wsum, 3) if wsum else 0.0, "phases": phases}


# ------------------------------------------------------------------ 前日の動き・頼みたいこと
def activity(conn, since: datetime, until: datetime) -> dict:
    out: dict = {"since": since.isoformat(), "until": until.isoformat()}
    lo, hi = (t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") for t in (since, until))   # GitHub の merged_at は UTC
    try:
        out["merged"] = {r: [{"number": p["number"], "title": p["title"], "url": p["html_url"], "factory": is_factory_pr(p)}
                             for p in merged_prs(r, pages=1) if lo <= p["merged_at"] < hi] for r in REPOS}
    except Exception as e:  # noqa: BLE001
        out["merged_error"] = type(e).__name__
    if conn is None:
        out["db"] = "unknown"
        return out
    with conn.cursor() as cur:
        cur.execute("""select coalesce(payload->>'state','?'), count(*), coalesce(sum((payload->>'cost_usd')::numeric),0)
                         from events where name='job.finished' and occurred_at >= %s and occurred_at < %s group by 1""", (since, until))
        out["jobs"] = {s: {"count": n, "cost_usd": float(c)} for s, n, c in cur.fetchall()}
        cur.execute("select count(*) from events where name='job.finished' and occurred_at >= %s and occurred_at < %s and (payload->>'usage_limited')::boolean", (since, until))
        out["usage_limited"] = cur.fetchone()[0]
        cur.execute("select key, category, what from stoppages where started_at >= %s and started_at < %s order by started_at", (since, until))
        out["stoppages_opened"] = [{"key": k, "category": c, "what": w} for k, c, w in cur.fetchall()]
        cur.execute("select key, category, what, started_at from stoppages where ended_at is null order by started_at")
        out["stoppages_open"] = [{"key": k, "category": c, "what": w, "since": s.astimezone(JST).strftime("%m-%d %H:%M")} for k, c, w, s in cur.fetchall()]
        cur.execute("select count(*) from events where name='gate.evaluated' and occurred_at >= %s and occurred_at < %s", (since, until))
        out["inspections"] = cur.fetchone()[0]
    return out


def is_factory_pr(p: dict) -> bool:
    """工場のワーカーが作った PR（ブランチ factory/issue-<n>-job-<id>）。それ以外は建設者の PR"""
    return bool(FACTORY_BRANCH_RE.match(p.get("head", {}).get("ref", "")))


def series(conn, start: str, until: datetime) -> list[dict]:
    """START_DATE から until の前日までの JST の日ごとの値。毎回取得元から作り直す（状態を持たない）"""
    days, d = [], datetime.fromisoformat(start).date()
    last = until.astimezone(JST).date()
    while d < last:
        days.append(d.isoformat())
        d += timedelta(days=1)
    rows = {k: {"date": k, "merged_factory": 0, "merged_builder": 0} for k in days}
    for r in REPOS:
        for p in merged_prs(r):
            k = parse_ts(p["merged_at"]).astimezone(JST).date().isoformat()
            if k in rows:
                rows[k]["merged_factory" if is_factory_pr(p) else "merged_builder"] += 1
    if conn is not None:
        sql = {
            "jobs_ok": "select (occurred_at at time zone 'Asia/Tokyo')::date, count(*) from events where name='job.finished' and payload->>'state'='succeeded' group by 1",
            "jobs_ng": "select (occurred_at at time zone 'Asia/Tokyo')::date, count(*) from events where name='job.finished' and payload->>'state'<>'succeeded' group by 1",
            "cost_usd": "select (occurred_at at time zone 'Asia/Tokyo')::date, coalesce(sum((payload->>'cost_usd')::numeric),0) from events where name='job.finished' group by 1",
            "inspections": "select (occurred_at at time zone 'Asia/Tokyo')::date, count(*) from events where name='gate.evaluated' group by 1",
            "stoppages": "select (started_at at time zone 'Asia/Tokyo')::date, count(*) from stoppages group by 1",
            "stoppage_hours": "select (started_at at time zone 'Asia/Tokyo')::date, sum(extract(epoch from coalesce(ended_at, now()) - started_at))/3600 from stoppages group by 1",
        }
        with conn.cursor() as cur:
            for key, q in sql.items():
                cur.execute(q)
                for day, v in cur.fetchall():
                    if day.isoformat() in rows:
                        rows[day.isoformat()][key] = round(float(v or 0), 2)
    return [rows[k] for k in days]


def parse_ts(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def merge_gate(conn, until: datetime) -> dict | None:
    """Gen1 限定: 工場の PR が自動マージされなかった理由を観点ごとに数え、日ごとの出入り（入: 工場が出した PR、出: 自動マージ・人のマージ・閉じた）を出す。
    自動マージの基準づくりの材料。理由は supervisor.merge_decided の最新の判定から機械的に決める（LLM は使わない）"""
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("""
          with prs as (select payload->>'pr_url' url, min(created_at) opened_at from artifacts where type='PR' and payload->>'pr_url' is not null group by 1),
               dec as (select distinct on (payload->>'pr_url') payload->>'pr_url' url, payload from events where name='supervisor.merge_decided' order by payload->>'pr_url', id desc)
          select prs.url, prs.opened_at, dec.payload,
                 (select min(occurred_at) from events e where e.name='supervisor.merged' and e.payload->>'pr_url'=prs.url) auto_at,
                 (select min(coalesce((e.payload->>'merged_at')::timestamptz, e.occurred_at)) from events e where e.name='pr.merged' and e.payload->>'pr_url'=prs.url) merged_at,
                 (select min(coalesce((e.payload->>'closed_at')::timestamptz, e.occurred_at)) from events e where e.name='pr.closed_unmerged' and e.payload->>'pr_url'=prs.url) closed_at,
                 (select count(distinct e.payload->>'head_sha') from events e where e.name='supervisor.merge_decided' and e.payload->>'pr_url'=prs.url) heads
            from prs left join dec on dec.url = prs.url order by prs.opened_at""")
        rows = cur.fetchall()
    jst_day = lambda t: t.astimezone(JST).date().isoformat() if t else None
    yday = (until.astimezone(JST).date() - timedelta(days=1)).isoformat()
    reasons: dict = {}
    flow: dict = {}
    prs = []
    for url, opened_at, d, auto_at, merged_at, closed_at, heads in rows:
        flow.setdefault(jst_day(opened_at), {}).setdefault("opened", 0)
        flow[jst_day(opened_at)]["opened"] += 1
        if auto_at:
            outcome, out_at = "auto", auto_at
        elif merged_at:
            outcome, out_at = "human", merged_at
        elif closed_at:
            outcome, out_at = "closed", closed_at
        else:
            outcome, out_at = "open", None
        if out_at:
            flow.setdefault(jst_day(out_at), {}).setdefault(outcome, 0)
            flow[jst_day(out_at)][outcome] += 1
        if outcome == "auto":
            continue
        # 理由（観点）: 表が承認を求めた変更種別 > 表は auto だが工場の権限（phase）で止めた > 判定まで来なかった
        if d is None:
            keys = ["未判定（CI・衝突・途中終了で clean にならなかった）"]
        elif d.get("table") != "auto":
            keys = [r.split("=")[0] for r in d.get("reasons", []) if not r.endswith("=auto")] or ["表が承認を求めた"]
        else:
            keys = [f"工場の権限（{d.get('phase', '?')} の phase_grants が {d.get('factory_grant')}）"]
            if heads > 1:
                keys.append("表は auto だが、判定のあとに直した（head が変わった）")
        if outcome == "closed":
            keys = keys + ["人が閉じた（マージしなかった）"]
        out_day = jst_day(out_at)
        for k in keys:
            r = reasons.setdefault(k, {"total": 0, "yday": 0, "open": 0})
            r["total"] += 1
            r["yday"] += 1 if out_day == yday or (outcome == "open" and jst_day(opened_at) == yday) else 0
            r["open"] += 1 if outcome == "open" else 0
        prs.append({"url": url, "outcome": outcome, "reasons": keys, "table": (d or {}).get("table"),
                    "change_types": (d or {}).get("change_types", []), "opened": jst_day(opened_at), "out": out_day})
    # 表が auto で人がそのままマージした件数（自動マージを解放してよい根拠。manifest の release_conditions）
    agree = sum(1 for p in prs if p["table"] == "auto" and p["outcome"] == "human" and len(p["reasons"]) == 1)
    days = sorted(k for k in flow if k and k < until.astimezone(JST).date().isoformat())
    return {"reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1]["total"])),
            "flow": [{"date": k, **flow[k]} for k in days], "prs": prs[-60:], "table_auto_human_merged": agree,
            "total": len(rows), "auto": sum(1 for r in rows if r[3])}


def needs(progress: dict, now: datetime | None = None) -> dict:
    out: dict = {}
    try:
        out["approval_prs"] = approval_prs(now or datetime.now(timezone.utc))
    except Exception as e:  # noqa: BLE001
        out["approval_prs_error"] = type(e).__name__
    for ph in progress["phases"]:
        for it in ph["items"]:
            if it["id"] == "golden_set":
                try:
                    files = yaml_files("guccyon/rally", ".factory-config/evals/golden")
                    out["golden_unconfirmed"] = sorted(f["name"].split("-")[0] for f in files if f["data"].get("scored_by") != "confirmed")
                except Exception as e:  # noqa: BLE001
                    out["golden_unconfirmed_error"] = type(e).__name__
    return out


# ------------------------------------------------------------------ 描画
def pct(x: float) -> str:
    return f"{round(x * 100)}%"


def bar(x: float, width: int = 10) -> str:
    n = round(x * width)
    return "█" * n + "░" * (width - n)


def fmt_needs(nd: dict) -> list[str]:
    lines = []
    for p in nd.get("approval_prs", []):
        age = f"（{p['days']:g} 日）" if p.get("days") is not None else ""
        lines.append(f"- 承認待ちの PR: [{p['repo']}#{p['number']}]({p['url']}) {p['title']}{age}")
    if nd.get("golden_unconfirmed"):
        lines.append(f"- Golden Set の確認: {', '.join(nd['golden_unconfirmed'])}")
    for k in ("approval_prs_error", "golden_unconfirmed_error"):
        if k in nd:
            lines.append(f"- （{k.removesuffix('_error')} を取得できなかった: {nd[k]}）")
    return lines or ["- なし"]


def fmt_activity(a: dict) -> list[str]:
    lines = []
    merged = a.get("merged", {})
    total = sum(len(v) for v in merged.values())
    lines.append(f"- マージした PR: {total} 本" + ("" if "merged_error" not in a else f"（取得できなかった: {a['merged_error']}）"))
    for r, prs in merged.items():
        for p in prs[:10]:
            lines.append(f"  - [{r}#{p['number']}]({p['url']}) {p['title']}")
        if len(prs) > 10:
            lines.append(f"  - ほか {len(prs) - 10} 本")
    if a.get("db") == "unknown":
        lines.append("- ジョブ・停止・費用: 不明（制御プレーンに届かない）")
        return lines
    jobs = a.get("jobs", {})
    cost = sum(v["cost_usd"] for v in jobs.values())
    lines.append(f"- ジョブ: 成功 {jobs.get('succeeded', {}).get('count', 0)} / 失敗 {jobs.get('failed', {}).get('count', 0)}"
                 f"（使用量上限 {a.get('usage_limited', 0)}）、検査 {a.get('inspections', 0)} 件、費用 ${cost:.2f}（API 単価換算）")
    opened = a.get("stoppages_opened", [])
    lines.append(f"- 新しい停止: {len(opened)} 件" + "".join(f"\n  - `{s['category']}` {s['what']}" for s in opened))
    still = a.get("stoppages_open", [])
    lines.append(f"- 開いたままの停止: {len(still)} 件" + "".join(f"\n  - `{s['category']}` {s['what']}（{s['since']} から）" for s in still))
    return lines


def ingest_quality(conn, day: str):
    """描く前に、製品の CI の診断とマージの難易度を取り込む。失敗してもレポートは出す"""
    try:
        defs = quality_ingest.load_definitions()
        n = quality_ingest.ingest_snapshots(conn, QUALITY_REPOS, day, gh, TOKEN, defs, log=lambda *a: print(*a, file=sys.stderr))
        m = quality_ingest.ingest_difficulty(conn, gh, defs, log=lambda *a: print(*a, file=sys.stderr))
        print(f"[status-report] 内部品質: 診断 {n} 件、難易度 {m} 件を取り込んだ")
    except Exception as e:  # noqa: BLE001
        print("[status-report] 内部品質を取り込めない:", repr(e), file=sys.stderr)


def quality_trend(conn, days: int = 30) -> dict:
    """製品ごとの内部品質の診断（quality.snapshot、ADR 0019）の推移。日付の古い順"""
    if conn is None:
        return {}
    out: dict[str, list[dict]] = {}
    with conn.cursor() as cur:
        cur.execute("""select payload from events where name='quality.snapshot' and occurred_at > now() - make_interval(days => %s)
                       order by payload->>'day'""", (days,))
        for (p,) in cur.fetchall():
            out.setdefault(p.get("repo"), []).append({"day": p.get("day"), "score": p.get("score"), "metrics": p.get("metrics") or {},
                                                      "hotspots": (p.get("hotspots") or [])[:3]})
    return out


QUALITY_LABELS = {"complex_function_ratio": "複雑すぎる関数", "long_function_ratio": "長すぎる関数", "duplication_ratio": "重複",
                  "tests_per_kloc": "テスト / 1000 行", "lint_warnings_per_kloc": "Linter の警告 / 1000 行", "layer_violations": "層の違反"}


def fmt_quality(qt: dict) -> list[str]:
    if not qt:
        return ["（内部品質の診断はまだありません）"]
    lines = []
    for repo, hist in qt.items():
        last = hist[-1]
        prev = hist[-2] if len(hist) > 1 else None
        d = "" if not prev or prev["score"] is None or last["score"] is None else f"（前回 {'+' if last['score'] >= prev['score'] else ''}{last['score'] - prev['score']}）"
        lines.append(f"**{repo}** スコア {last['score']}{d}（{last['day']}）")
        lines.append("- " + "、".join(f"{ja} {last['metrics'][k]}" for k, ja in QUALITY_LABELS.items() if last["metrics"].get(k) is not None))
        if last["hotspots"]:
            lines.append("- よく変わり複雑なファイル: " + "、".join(f"`{h['file']}`（{h['changes']} 回 × {h['max_ccn']}）" for h in last["hotspots"]))
        if len(hist) >= 2:
            xs = ", ".join(f'"{(h["day"] or "")[5:]}"' for h in hist)
            ys = ", ".join(str(h["score"] if h["score"] is not None else 0) for h in hist)
            lines += ["", "```mermaid", "xychart-beta", f'  title "{repo} の内部品質のスコア"', f"  x-axis [{xs}]", '  y-axis "点" 0 --> 100', f"  line [{ys}]", "```"]
        lines.append("")
    return lines


LEVELS_JA = {"easy": "易", "medium": "中", "hard": "難"}


def change_cost(conn, weeks: int = 6) -> list[dict]:
    """難易度ごと・ISO 週ごとの変更のコストの中央値（quality.yaml の change_cost、ADR 0019）。難易度（pr.difficulty）のあるマージだけ"""
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute("""
          select to_char(date_trunc('week', (e.payload->>'merged_at')::timestamptz at time zone 'Asia/Tokyo'), 'IYYY-"W"IW') as week,
                 d.payload->>'level' as level,
                 extract(epoch from ((e.payload->>'merged_at')::timestamptz - t.created_at)) / 3600 as lead_h,
                 (select coalesce(sum((j.result->>'cost_usd')::float), 0) from jobs j where j.correlation_id = e.correlation_id) as cost,
                 (select count(*) from jobs j where j.correlation_id = e.correlation_id) as jobs,
                 (select count(*) from events s where s.name = 'sendback.filed' and s.payload->>'origin_pr' = e.payload->>'pr_url') as sendbacks
            from events e
            join events d on d.dedupe_key = 'pr.difficulty:' || (e.payload->>'pr_url')
            left join tickets t on t.source = 'github' and 'github:' || t.source_ref = e.correlation_id
           where e.name = 'pr.merged'
             and (e.payload->>'merged_at')::timestamptz > now() - make_interval(weeks => %s)""", (weeks,))
        rows = cur.fetchall()
    groups: dict[tuple, list] = {}
    for week, level, lead_h, cost, jobs, sendbacks in rows:
        groups.setdefault((week, level), []).append((lead_h, cost, jobs, sendbacks))
    def med(xs):
        xs = sorted(x for x in xs if x is not None)
        return xs[len(xs) // 2] if xs else None
    return [{"week": w, "level": lv, "n": len(v), "lead_time_hours": med([x[0] for x in v]), "cost_usd": med([x[1] for x in v]),
             "jobs": med([x[2] for x in v]), "sendbacks": med([x[3] for x in v])} for (w, lv), v in sorted(groups.items())]


def fmt_change_cost(cc: list[dict]) -> list[str]:
    if not cc:
        return ["（難易度の付いたマージはまだありません）"]
    lines = ["| 週 | 難易度 | 件数 | リードタイム（時間） | 費用（USD） | ジョブ | 差し戻し |", "|---|---|---|---|---|---|---|"]
    for r in cc:
        lt = "-" if r["lead_time_hours"] is None else f"{r['lead_time_hours']:.1f}"
        lines.append(f"| {r['week']} | {LEVELS_JA.get(r['level'], r['level'])} | {r['n']}{'（参考）' if r['n'] < 3 else ''} | {lt} | "
                     f"{(r['cost_usd'] or 0):.2f} | {r['jobs']} | {r['sendbacks']} |")
    return lines + ["", "中央値。同じ難易度で週を追って増えていれば、作りが変えにくくなっている兆し。"]


def fmt_trend(history: list[dict]) -> list[str]:
    if len(history) < 2:
        return ["（推移は 2 日分たまってから出ます）"]
    xs = ", ".join(f'"{h["date"][5:]}"' for h in history)
    prog = ", ".join(str(round(h["overall"] * 100, 1)) for h in history)
    cost = ", ".join(f'{h.get("cost_usd") or 0:.2f}' for h in history)
    return ["```mermaid", "xychart-beta", '  title "全体の進捗（%）"', f"  x-axis [{xs}]", '  y-axis "%" 0 --> 100', f"  line [{prog}]", "```",
            "", "```mermaid", "xychart-beta", '  title "1 日の費用（USD、API 単価換算）"', f"  x-axis [{xs}]", f"  bar [{cost}]", "```"]


def render_dashboard(snap: dict, history: list[dict]) -> str:
    pr = snap["progress"]
    lines = [f"# 建設状況（{snap['date']} 時点・毎朝更新）", "",
             f"**全体 {pct(pr['overall'])}** `{bar(pr['overall'], 20)}`", "",
             "割合は `definitions/roadmap.yaml` から機械的に出しています（フェーズは期間の目安で重み付け）。不明の項目は 0 で数えます。", "",
             "## オーナー に頼みたいこと", *fmt_needs(snap["needs"]), "",
             "## フェーズ", "", "| フェーズ | 進捗 | |", "|---|---|---|"]
    for ph in pr["phases"]:
        lines.append(f"| {ph['id']} {ph['ja']} | {pct(ph['progress'])} | `{bar(ph['progress'])}` |")
    lines += ["", "## 項目の実測", ""]
    for ph in pr["phases"]:
        if ph["progress"] == 0 and all(i["progress"] == 0 and not i.get("unknown") for i in ph["items"]):
            continue
        lines.append(f"<details{' open' if 0 < ph['progress'] < 1 else ''}><summary>{ph['id']} {ph['ja']}（{pct(ph['progress'])}）</summary>\n")
        for it in ph["items"]:
            mark = "✅" if it["progress"] >= 1 else "❔" if it.get("unknown") else "🔸" if it["progress"] > 0 else "⬜"
            lines.append(f"- {mark} {it['ja']}: {it['value']}")
        lines.append("\n</details>\n")
    lines += ["## 推移", "", *fmt_trend(history), "", "## 内部品質（定期診断）", "", *fmt_quality(snap.get("quality") or {}),
              "### 変更のコスト（難易度別・週ごと）", "", *fmt_change_cost(snap.get("change_cost") or []), "", "## 前日（JST）", *fmt_activity(snap["activity"]), "",
              "<sub>health/daily/status_report.py が生成（LLM 不使用）。日次の記録はこの Issue のコメント。</sub>"]
    return "\n".join(lines)


def render_daily(snap: dict, prev: dict | None) -> str:
    pr = snap["progress"]
    head = f"## {snap['date']} の建設状況: 全体 {pct(pr['overall'])}"
    if prev:
        d = pr["overall"] - prev["overall"]
        head += f"（前回 {prev['date']} から {'+' if d >= 0 else ''}{round(d * 100, 1)} pt）"
    lines = [head, ""]
    if prev:
        before = {p["id"]: p["progress"] for p in prev.get("phases", [])}
        moved = [f"{p['id']} {pct(before.get(p['id'], 0))} → {pct(p['progress'])}" for p in pr["phases"] if abs(p["progress"] - before.get(p["id"], 0)) >= 0.005]
        lines.append("- 動いたフェーズ: " + ("、".join(moved) if moved else "なし"))
    for repo, hist in (snap.get("quality") or {}).items():
        lines.append(f"- 内部品質: {fmt_quality({repo: hist[-2:] if len(hist) > 1 else hist})[0]}")
    lines += ["", "**頼みたいこと**", *fmt_needs(snap["needs"]), "", "**前日（JST）**", *fmt_activity(snap["activity"])]
    return "\n".join(lines)


# ------------------------------------------------------------------ 記録と公開
def load_history(conn) -> list[dict]:
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute("""select payload from events where name='report.daily' order by occurred_at desc limit %s""", (TREND_DAYS,))
        return [r[0] for r in cur.fetchall()][::-1]


def already_reported(conn, date: str) -> bool:
    return conn is not None and db_value(conn, "select count(*) from events where dedupe_key=%s", (f"report.daily:{date}",)) > 0


def record_snapshot(conn, snap: dict, force: bool):
    if conn is None:
        return
    from psycopg.types.json import Jsonb
    summary = {"date": snap["date"], "overall": snap["progress"]["overall"],
               "phases": [{"id": p["id"], "progress": p["progress"]} for p in snap["progress"]["phases"]],
               "cost_usd": sum(v["cost_usd"] for v in snap["activity"].get("jobs", {}).values())}
    with conn.cursor() as cur:
        if force:
            cur.execute("delete from events where dedupe_key=%s", (f"report.daily:{snap['date']}",))
        cur.execute("insert into events(name, payload, dedupe_key) values ('report.daily',%s,%s) on conflict (dedupe_key) do nothing",
                    (Jsonb(summary), f"report.daily:{snap['date']}"))


def embed(snap: dict) -> str:
    """本文の末尾に、見えない形でスナップショットを埋める（Issue 本文の上限 65536 字に収める。系列は日数ぶんだけ）"""
    raw = base64.b64encode(json.dumps(snap, ensure_ascii=False, separators=(",", ":")).encode()).decode()
    return f"\n\n<!-- {SNAPSHOT_MARK} {raw} -->\n"


def status_issue() -> int:
    found = gh("GET", f"repos/{STATUS_REPO}/issues?state=open&labels={urllib.parse.quote(LABEL)}&per_page=1")
    if found:
        return found[0]["number"]
    try:
        gh("POST", f"repos/{STATUS_REPO}/labels", {"name": LABEL, "color": "0e8a16", "description": "建設状況の日次レポート（health/daily）"})
    except urllib.error.HTTPError as e:
        if e.code != 422:   # 既にある
            raise
    return gh("POST", f"repos/{STATUS_REPO}/issues", {"title": "建設状況（毎朝更新）", "labels": [LABEL], "body": "準備中"})["number"]


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--publish", action="store_true")
    ap.add_argument("--force", action="store_true", help="今日の分が記録済みでも出し直す")
    ap.add_argument("--json", help="スナップショットの JSON をこのパスにも書く（ページの描画の確認用）")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    date = now.astimezone(JST).strftime("%Y-%m-%d")
    conn = db_conn()
    if args.publish and not args.force and already_reported(conn, date):
        print(f"[status-report] {date} は記録済み")
        return 0

    # 「前日」は JST の 0〜24 時で区切る（毎朝の配信で前日分を丸ごと見せる）
    today0 = now.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    if conn is not None and args.publish:
        ingest_quality(conn, date)
    progress = roadmap_progress(conn)
    snap = {"date": date, "generated_at": now.isoformat(), "progress": progress,
            "activity": activity(conn, today0 - timedelta(days=1), today0), "needs": needs(progress),
            "series": series(conn, START_DATE, now), "merge_gate": merge_gate(conn, now), "db": conn is not None, "build_work": bool(BUILD_WORK_DIR),
            "cost_budget_usd": COST_BUDGET_USD, "quality": quality_trend(conn), "change_cost": change_cost(conn)}
    history = load_history(conn)
    prev = next((h for h in reversed(history) if h["date"] != date), None)
    today = {"date": date, "overall": progress["overall"],
             "phases": [{"id": p["id"], "progress": p["progress"]} for p in progress["phases"]],
             "cost_usd": sum(v["cost_usd"] for v in snap["activity"].get("jobs", {}).values())}
    trend = [h for h in history if h["date"] != date] + [today]
    snap["progress_history"] = trend

    dashboard, daily = render_dashboard(snap, trend), render_daily(snap, prev)
    if args.json:
        Path(args.json).write_text(json.dumps(snap, ensure_ascii=False, indent=1))
    if args.dry_run:
        print(dashboard, "\n\n---\n", daily, sep="\n")
        return 0
    n = status_issue()
    gh("PATCH", f"repos/{STATUS_REPO}/issues/{n}", {"body": dashboard + embed(snap)})
    gh("POST", f"repos/{STATUS_REPO}/issues/{n}/comments", {"body": daily})
    record_snapshot(conn, snap, args.force)
    print(f"[status-report] {date} を {STATUS_REPO}#{n} に出した（全体 {pct(progress['overall'])}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
