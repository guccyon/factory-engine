#!/usr/bin/env python3
"""工場の手動操作: 一覧・停止・再実行（ADR 0003）。

  docker compose exec dispatcher factoryctl list [--all]
  docker compose exec dispatcher factoryctl stop <job_id>
  docker compose exec dispatcher factoryctl rerun <job_id>
  docker compose exec dispatcher factoryctl enqueue <repo> <issue> [--tier tier-small]
  docker compose exec dispatcher factoryctl events <job_id>

操業停止（ADR 0005）。番人が見えない停止（builder の端末の許可待ちなど）は手で記録する:
  factoryctl stoppage open <分類> <何が止まったか> [--key K] [--waiting-for ..] [--cause ..] [--prevention ..] [--at ISO] [--by builder] [--quiet]
  factoryctl stoppage close <key> [--remedy ..] [--prevention ..] [--at ISO] [--quiet]   # --quiet は通知しない（過去分の取り込み）
  factoryctl stoppage list [--open]
  factoryctl report stoppages [--from YYYY-MM-DD] [--to YYYY-MM-DD]   # Markdown。既定は直近 7 日。週次レポートの stoppages.md はこの出力
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import stoppage


def opt(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default


def report_stoppages(cur, argv):
    today = datetime.now(timezone.utc).date()
    to = opt(argv, "--to", (today + timedelta(days=1)).isoformat())
    frm = opt(argv, "--from", (today - timedelta(days=6)).isoformat())
    # 期間に重なる停止（開いたままのものを含む）
    cur.execute("select *, coalesce(ended_at, now()) - started_at as dur from stoppages "
                "where started_at < %s::date and coalesce(ended_at, now()) >= %s::date order by started_at", (to, frm))
    rows = cur.fetchall()
    out = [f"# 操業停止の記録（{frm} 〜 {to}、{os.environ.get('REPORT_TZ', 'Asia/Tokyo')}、制御プレーンから生成）", "",
           "生成: `factoryctl report stoppages --from --to`。記録は events の stoppage.opened / closed（ADR 0005）", ""]
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["category"], []).append(r)
    out += ["## 内訳", "", "| 分類 | 件数 | 合計時間 | 開いたまま |", "|---|---|---|---|"]
    for cat in stoppage.CATEGORIES:
        rs = by.get(cat, [])
        if rs:
            mins = sum(int(r["dur"].total_seconds() // 60) for r in rs)
            out.append(f"| {cat} | {len(rs)} | {mins // 60} 時間 {mins % 60} 分 | {sum(1 for r in rs if r['ended_at'] is None)} |")
    out += ["", "## 一覧", "", "| key | 開始 | 終了 | 分類 | 検知 | 何が止まったか | 原因 | 手当て | 再発を防ぐ仕組み |", "|---|---|---|---|---|---|---|---|---|"]

    def cell(v):
        return (v or "").replace("|", "／").replace("\n", " ")
    for r in rows:
        end = f"{r['ended_at']:%m-%d %H:%M}" if r["ended_at"] else "**開いたまま**"
        out.append(f"| {r['key']} | {r['started_at']:%m-%d %H:%M} | {end} | {r['category']} | {r['detected_by']} | {cell(r['what'])} | "
                   f"{cell(r['cause'])} | {cell(r['remedy'])} | {cell(r['prevention'])} |")
    print("\n".join(out))


def main(argv):
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)
    cur = conn.cursor()
    cur.execute("select set_config('timezone', %s, false)", (os.environ.get("REPORT_TZ", "Asia/Tokyo"),))   # 表示は建設者の時刻で
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        where = "" if "--all" in argv else "where state in ('queued','running') or finished_at > now() - interval '1 day'"
        cur.execute(f"select id, state, repo, issue_number, attempt, enqueued_at, started_at, finished_at, error, result->>'pr_url' as pr_url from jobs {where} order by id desc limit 50")
        for r in cur.fetchall():
            print(f"{r['id']:>4} {r['state']:<9} {r['repo']}#{r['issue_number']} a{r['attempt']} {r['enqueued_at']:%m-%d %H:%M} {r['error'] or ''} {r['pr_url'] or ''}")
    elif cmd == "stop":
        job_id = int(argv[2])
        cur.execute("update jobs set cancel_requested=true, state=case when state='queued' then 'cancelled' else state end, finished_at=case when state='queued' then now() else finished_at end where id=%s and state in ('queued','running') returning state", (job_id,))
        r = cur.fetchone()
        cur.execute("insert into events(name, job_id, payload) values ('job.cancel_requested', %s, %s)", (job_id, Jsonb({"by": "factoryctl"})))
        print("cancel requested" if r else "job is not active")
    elif cmd == "rerun":
        job_id = int(argv[2])
        cur.execute("select repo, issue_number, attempt, kind from jobs where id=%s", (job_id,))
        j = cur.fetchone()
        if not j:
            print("no such job"); return 1
        try:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind) values (%s,%s,%s,%s,'rerun',%s,%s) returning id",
                        (f"github:{j['repo']}#{j['issue_number']}", j["repo"], j["issue_number"], j["attempt"] + 1, os.environ.get("MODEL_TIER", "tier-large"), j["kind"]))
        except psycopg.errors.UniqueViolation:
            print("an active job already exists for this issue"); return 1
        new_id = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued', %s, %s, %s)",
                    (f"github:{j['repo']}#{j['issue_number']}", new_id, Jsonb({"by": "rerun", "attempt": j["attempt"] + 1, "previous": job_id})))
        print(f"enqueued job {new_id} (attempt {j['attempt'] + 1})")
    elif cmd == "enqueue":   # 手動投入（試験運転や、ラベルを使わない再投入）。--tier tier-small でモデル tier を変えられる（ペア比較の材料）
        repo, issue = argv[2], int(argv[3])
        tier = argv[argv.index("--tier") + 1] if "--tier" in argv else os.environ.get("MODEL_TIER", "tier-large")
        try:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, model_tier) values (%s,%s,%s,'factoryctl',%s) returning id",
                        (f"github:{repo}#{issue}", repo, issue, tier))
        except psycopg.errors.UniqueViolation:
            print("an active job already exists for this issue"); return 1
        new_id = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued', %s, %s, %s)",
                    (f"github:{repo}#{issue}", new_id, Jsonb({"by": "factoryctl"})))
        print(f"enqueued job {new_id}")
    elif cmd == "events":
        job_id = int(argv[2])
        cur.execute("select name, occurred_at, payload from events where job_id=%s order by id", (job_id,))
        for r in cur.fetchall():
            print(f"{r['occurred_at']:%H:%M:%S} {r['name']:<22} {json.dumps(r['payload'], ensure_ascii=False)}")
    elif cmd == "stoppage":
        sub = argv[2] if len(argv) > 2 else "list"
        if sub == "open":
            category, what = argv[3], argv[4]
            key = opt(argv, "--key", f"manual:{datetime.now(timezone.utc):%Y%m%dT%H%M%S}")
            ok = stoppage.open_stoppage(cur, key, category, what, waiting_for=opt(argv, "--waiting-for", ""), cause=opt(argv, "--cause", ""),
                                        prevention=opt(argv, "--prevention", ""), started_at=opt(argv, "--at"), detected_by=opt(argv, "--by", "builder"), quiet="--quiet" in argv)
            print(f"opened {key}" if ok else f"{key} already exists")
        elif sub == "close":
            key = argv[3]
            ok = stoppage.close_stoppage(cur, key, remedy=opt(argv, "--remedy", ""), prevention=opt(argv, "--prevention", ""),
                                         ended_at=opt(argv, "--at"), closed_by="builder", quiet="--quiet" in argv)
            print(f"closed {key}" if ok else f"{key} is not open")
        else:
            where = "where ended_at is null" if "--open" in argv else ""
            cur.execute(f"select * from stoppages {where} order by started_at")
            for r in cur.fetchall():
                end = f"{r['ended_at']:%m-%d %H:%M}" if r["ended_at"] else "open"
                print(f"{r['key']:<26} {r['category']:<11} {r['started_at']:%m-%d %H:%M} → {end:<11} {r['what']}")
    elif cmd == "report" and len(argv) > 2 and argv[2] == "stoppages":
        report_stoppages(cur, argv)
    else:
        print(__doc__); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
