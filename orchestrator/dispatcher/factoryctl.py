#!/usr/bin/env python3
"""工場の手動操作: 一覧・停止・再実行（ADR 0003）。

  docker compose exec dispatcher factoryctl list [--all]
  docker compose exec dispatcher factoryctl stop <job_id>
  docker compose exec dispatcher factoryctl rerun <job_id>
  docker compose exec dispatcher factoryctl enqueue <repo> <issue> [--tier tier-small]
  docker compose exec dispatcher factoryctl events <job_id>
"""
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def main(argv):
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)
    cur = conn.cursor()
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
        cur.execute("select repo, issue_number, attempt from jobs where id=%s", (job_id,))
        j = cur.fetchone()
        if not j:
            print("no such job"); return 1
        try:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier) values (%s,%s,%s,%s,'rerun',%s) returning id",
                        (f"github:{j['repo']}#{j['issue_number']}", j["repo"], j["issue_number"], j["attempt"] + 1, os.environ.get("MODEL_TIER", "tier-large")))
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
    else:
        print(__doc__); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
