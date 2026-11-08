"""失敗したジョブの積み直し（rerun）。factoryctl rerun と操業コンソールの「やり直す」ボタンの両方から使う（2026-11-08）。

- 続きのジョブ（衝突・CI 落ち・指摘・途中終了）は、続ける先（工場のブランチと PR）と合図の中身を引き継いで続きとして積み直す。
  引き継がないと最初からの実装になり、別のブランチで PR をもう 1 本作ってしまう
- 入力を受け取る種類のジョブは前の入力（PR・指摘・検査する案など）を引き継ぐ
"""
from __future__ import annotations

import os

import psycopg
from psycopg.types.json import Jsonb

import continuation

# 入力を job.enqueued の input で受け取る種類（dispatcher が標準入力で渡す）。積み直すときは前の入力を引き継ぐ。
# 引き継がないと入力が空のまま走り、本文の追記なら pr_url が無くて落ちる
PIPED_KINDS = ("judge", "body_fix", "design_review", "case_review", "committee_weekly", "ui_design", "ui_design_review", "plan", "design_system", "desk_consult", "qa")
INCOMPLETE_CONTINUE_MAX = 1   # 途中終了の続きは 1 回まで（ADR 0036 の 6）


def carried_input(cur, job_id: int, kind: str) -> dict:
    """入力を受け取る種類のジョブを積み直すときは、前の入力（PR・指摘・検査する案など）を引き継ぐ"""
    if kind not in PIPED_KINDS:
        return {}
    cur.execute("select payload->'input' as inp from events where name='job.enqueued' and job_id=%s order by id limit 1", (job_id,))
    inp = (cur.fetchone() or {}).get("inp")
    return {"input": inp} if inp else {}


def incomplete_count(cur, corr: str) -> int:
    """そのチケットの実装役のジョブが途中終了した回数"""
    cur.execute("select count(*) as n from jobs where correlation_id=%s and kind='implement' and coalesce((result->>'incomplete')::boolean, false)", (corr,))
    return int(cur.fetchone()["n"])


def rerun(cur, job_id: int, *, continue_draft: bool = False, force: bool = False, requested_by: str = "factoryctl") -> tuple[int | None, str]:
    """ジョブを積み直す。(新しいジョブの id, 知らせる文)。積めなかったときは id が None で、文が理由"""
    cur.execute("select correlation_id, repo, issue_number, attempt, kind, ticket_id, priority, result from jobs where id=%s", (job_id,))
    j = cur.fetchone()
    if not j:
        return None, "no such job"
    corr = j["correlation_id"] if j["kind"] == "desk_consult" else f"github:{j['repo']}#{j['issue_number']}"   # 窓口の相談役は会話（desk:<id>）に積む
    carried = continuation.carry_over(cur, job_id) if j["kind"] == "implement" else {}
    cont = carried.get("continue")
    if continue_draft and not cont:   # 途中終了のドラフト PR のブランチから続ける（最初からやり直さない）
        res = j["result"] or {}
        if j["kind"] != "implement" or not res.get("pr_number"):
            return None, "--continue needs an implement job that left a PR"
        cont = {"branch": f"factory/issue-{j['issue_number']}-job-{job_id}", "pr_number": int(res["pr_number"]), "reason": "incomplete"}
        carried = {"continue": cont}
    if cont and cont.get("reason") == "incomplete" and not force:
        n_inc = incomplete_count(cur, corr)
        if n_inc > INCOMPLETE_CONTINUE_MAX:   # 続きも途中終了した（ADR 0036 の 6）。同じ大きさのまま積み直さない
            return None, (f"途中終了が {n_inc} 回あります（続きは {INCOMPLETE_CONTINUE_MAX} 回まで。ADR 0036 の 6）。"
                          f"残りをプランナーに切らせるなら factoryctl plan resplit {j['repo']}#{j['issue_number']}、それでも続けるなら --force")
    try:
        cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind, ticket_id, priority) "
                    "values (%s,%s,%s,%s,'rerun',%s,%s,%s,%s) returning id",
                    (corr, j["repo"], j["issue_number"], j["attempt"] + 1, os.environ.get("MODEL_TIER", "tier-large"),
                     j["kind"], j["ticket_id"], j["priority"]))
    except psycopg.errors.UniqueViolation:
        return None, "an active job already exists for this issue"
    new_id = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued', %s, %s, %s)",
                (corr, new_id, Jsonb({"by": "rerun", "requested_by": requested_by, "attempt": j["attempt"] + 1, "previous": job_id, **carried, **carried_input(cur, job_id, j["kind"])})))
    return new_id, f"enqueued job {new_id} (attempt {j['attempt'] + 1}{', continue ' + cont['branch'] + ' (' + cont['reason'] + ')' if cont else ''})"
