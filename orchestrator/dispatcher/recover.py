"""時間切れ・途中終了で失敗した実装のチケットを、工場が自分で立て直す（ADR 0036 の 6 の続き。2026-11-14）。

これまでは失敗したチケットは停止 job:<id> を開いて人の rerun を待った。p0 の修正の束（rally #1436 の t2 #1438）も、
打ち切りで落ちたまま人が気づくまで止まった。人の手は承認と審美眼に絞る（オーナー 2026-11-14）ので、
直し方の決まっている失敗は工場が次の一手を打つ。決まっていない失敗（未分類・使用量上限・錠）は今までどおり人へ。

次の一手（チケットごとに AUTO_MAX 回まで。超えたら停止を残して人へ）:
- 途中終了（incomplete）で、続きがまだ INCOMPLETE_CONTINUE_MAX 回以内 → ドラフト PR のブランチから続ける（rerun --continue）
- 時間切れ（timeout）の 1 回目 → そのまま積み直す（ワーカーが止まった・遅かった、の一過性を見込む）
- それ以外（続きも途中終了、時間切れの 2 回目） → プランナーに残りを見積もらせ、大きければ切らせる（plan resplit）

AUTO_RECOVER=0 で止める（supervisor は何もしない）。
"""
from __future__ import annotations

import os

from psycopg.types.json import Jsonb

ENABLED = os.environ.get("AUTO_RECOVER", "1") != "0"
AUTO_MAX = int(os.environ.get("AUTO_RECOVER_MAX", "2"))   # チケットごとの自動の立て直しの上限
WINDOW_HOURS = 24                                          # これより前に落ちたものは拾わない（人がもう見ている見込み）


def decide(error: str, incomplete_n: int, timeout_n: int, auto_n: int, *, has_pr: bool, continue_max: int) -> str | None:
    """次の一手。'continue'（続きから）・'rerun'（そのまま積み直す）・'resplit'（プランナーに切らせる）・None（人へ）"""
    if auto_n >= AUTO_MAX:
        return None
    if error.startswith("incomplete"):
        return "continue" if has_pr and incomplete_n <= continue_max else "resplit"
    if error == "timeout":
        return "rerun" if timeout_n <= 1 else "resplit"
    return None


def candidates(cur) -> list[dict]:
    """直近に落ちた実装のジョブのうち、同じチケットでその後のジョブがなく、チケットがまだ開いているもの"""
    cur.execute("""select j.id, j.correlation_id, j.repo, j.issue_number, j.error, j.result from jobs j
                   where j.kind = 'implement' and j.state = 'failed' and (j.error = 'timeout' or j.error like 'incomplete%%')
                     and j.finished_at > now() - make_interval(hours => %s)
                     and not exists (select 1 from jobs n where n.correlation_id = j.correlation_id and n.id > j.id)
                     and not exists (select 1 from events e where e.name = 'job.auto_recovered' and (e.payload->>'previous')::bigint = j.id)
                     and exists (select 1 from tickets t where t.source = 'github' and 'github:' || t.source_ref = j.correlation_id
                                 and not t.held and t.state <> 'closed')
                   order by j.id""", (WINDOW_HOURS,))
    return list(cur.fetchall())


def counts(cur, corr: str) -> tuple[int, int, int]:
    """(途中終了の回数, 時間切れの回数, 自動で立て直した回数)"""
    cur.execute("""select count(*) filter (where coalesce((result->>'incomplete')::boolean, false) or error like 'incomplete%%') as inc,
                          count(*) filter (where error = 'timeout') as tmo
                   from jobs where correlation_id = %s and kind = 'implement'""", (corr,))
    r = cur.fetchone()
    cur.execute("select count(*) as n from events where name = 'job.auto_recovered' and correlation_id = %s", (corr,))
    return int(r["inc"]), int(r["tmo"]), int(cur.fetchone()["n"])


def step(cur, log=print) -> list[dict]:
    """supervisor の周期ごとに呼ぶ。打った手の一覧を返す"""
    if not ENABLED:
        return []
    import planner
    import rerun
    import stoppage
    done = []
    for j in candidates(cur):
        corr = j["correlation_id"]
        inc, tmo, auto = counts(cur, corr)
        has_pr = bool((j["result"] or {}).get("pr_number"))
        action = decide(j["error"] or "", inc, tmo, auto, has_pr=has_pr, continue_max=rerun.INCOMPLETE_CONTINUE_MAX)
        if not action:
            continue
        if action == "resplit":
            cur.execute("select result->>'pr_url' as pr from jobs where correlation_id=%s and kind='implement' and result->>'pr_url' is not null "
                        "order by id desc limit 1", (corr,))
            pr = (cur.fetchone() or {}).get("pr")
            new_id = planner.enqueue_pre_implement(cur, j["repo"], j["issue_number"], corr, "auto_recover", {"continuing_pr": pr} if pr else {})
            msg = f"plan job {new_id}" if new_id else "an active job already exists"
        else:
            new_id, msg = rerun.rerun(cur, j["id"], continue_draft=(action == "continue"), requested_by="supervisor:auto_recover")
        if not new_id:
            log("[recover] skip", j["id"], msg)
            continue
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.auto_recovered', %s, %s, %s)",
                    (corr, new_id, Jsonb({"previous": j["id"], "error": j["error"], "action": action, "attempt": auto + 1, "max": AUTO_MAX})))
        ja = {"continue": "ドラフト PR の続きから積み直した", "rerun": "そのまま積み直した", "resplit": "残りをプランナーに見積もらせた"}[action]
        stoppage.close_stoppage(cur, f"job:{j['id']}", remedy=f"工場が自動で立て直した（{ja}。ジョブ {new_id}、{auto + 1}/{AUTO_MAX} 回目）",
                                closed_by="supervisor", quiet=True)
        log("[recover]", j["id"], "->", new_id, action)
        done.append({"previous": j["id"], "new": new_id, "action": action})
    return done
