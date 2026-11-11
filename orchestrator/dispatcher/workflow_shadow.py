"""ワークフローの宣言を、守らせずに記録だけで突き合わせる（ADR 0038 の順番 1）。決定的（LLM を使わない）。

プランナーが Plan の workflow に宣言した上限・飛ばすステップ・着手のしかた（start）と、実際の動きを比べ、
「宣言どおりに守らせていたら、どこで止め、何を飛ばし、何を待たせたか」を events の workflow.shadow に残す。
ここ自体はラインの動きを変えない。守らせるのは workflow_guard.py（順番 2。最初は衝突の解消の回数）。
宣言のないチケット（プランナーを通らずに入ったもの）は既定値の上限で数える（ADR 0038 の決定 7）。

- 台帳（ledger）: チケットごとの実績。実装のターン（衝突の解消の続きを除く。ADR 0036 と同じ数え方）と費用、チケット全体の費用、
  続きのジョブの理由ごとの回数（衝突・CI 落ち・検査の指摘・途中終了）、QA のやり直し、judge のやり直し（同じ head を裁き直した数）
- cap_hit: 実績が宣言の上限を超えた（守らせていれば、超えた分は動かなかった）。当たったときの動き（action）も残す
- would_skip: 宣言で飛ばしたステップ（inspect・qa）のジョブが動いた
- would_wait: 宣言で start: after にしたのに、前のチケットが閉じる前に投入した（supervisor の enqueue_step が残す）

上限の種類と当たったときの動きは definitions/workflow.yaml の caps.kinds の写し（dispatcher のコンテナには definitions/ がない。食い違いはテストで見る）。
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

ON_CAP = {
    "implement.turns": "replan", "implement.cost_usd": "human", "ticket.cost_usd": "human",
    "retries.conflict": "serialize_then_human", "retries.ci_fix": "human", "retries.review_fix": "human",
    "retries.continue": "replan", "retries.qa_rerun": "human", "retries.judge_rounds": "human",
    "quick.turns": "offer_analysis", "quick.cost_usd": "offer_analysis", "analysis.cost_usd": "human",
}
CAP_JA = {
    "implement.turns": "実装のターン", "implement.cost_usd": "実装の費用", "ticket.cost_usd": "チケット全体の費用",
    "retries.conflict": "衝突の解消", "retries.ci_fix": "CI 落ちの直し", "retries.review_fix": "検査の指摘の直し",
    "retries.continue": "途中終了の続き", "retries.qa_rerun": "QA のやり直し", "retries.judge_rounds": "judge のやり直し",
    "quick.turns": "小さな LLM の仕事のターン", "quick.cost_usd": "小さな LLM の仕事の費用", "analysis.cost_usd": "分析役の報告の費用",
}
ACTION_JA = {"replan": "プランナーに戻して切る", "human": "人へ", "serialize_then_human": "並べて待ち、それでも衝突したら人へ",
             "offer_analysis": "決まったところまで返し、分析役の報告を提案する"}
# 帯ごとの既定値（definitions/workflow.yaml の caps.defaults の写し）。宣言のないチケット（プランナーを通らずに factory:planned で入った
# 差し戻しの直し・Defect・Learn の改善など。2026-11-09 の実績で実装のチケットの 9 割）は any と M で数え、守らせる（ADR 0038 の決定 7）
DEFAULT_CAPS = {
    "S": {"implement.turns": 30, "implement.cost_usd": 3, "ticket.cost_usd": 6},
    "M": {"implement.turns": 60, "implement.cost_usd": 8, "ticket.cost_usd": 15},
    "L": {"implement.turns": 90, "implement.cost_usd": 15, "ticket.cost_usd": 25},
    "XL": {"implement.turns": 120, "implement.cost_usd": 25, "ticket.cost_usd": 30},
    "any": {"retries.conflict": 3, "retries.ci_fix": 2, "retries.review_fix": 2, "retries.continue": 1, "retries.qa_rerun": 1,
            "retries.judge_rounds": 1, "quick.turns": 8, "quick.cost_usd": 0.5, "analysis.cost_usd": 5},
}
DEFAULT_BAND = "M"
CONTINUE_CAP = {"conflict": "retries.conflict", "ci_failed": "retries.ci_fix", "review_fix": "retries.review_fix", "incomplete": "retries.continue"}
SKIPPABLE_JOBS = {"inspect": "inspect", "qa": "qa"}   # ジョブの kind → ステップ。宣言で飛ばしたのに動いたら would_skip
STEP_DAYS = 30
STEP_LIMIT = 200


def _cost(r: dict | None) -> float:
    try:
        return float((r or {}).get("cost_usd") or 0)
    except (TypeError, ValueError):
        return 0.0


def latest_workflow(cur, corr: str) -> dict | None:
    """最後に着手と決めた（ready・split）判定の Plan の workflow。宣言のない Plan（ADR 0038 の前）は None"""
    cur.execute("select id, created_at, payload from artifacts where type='Plan' and correlation_id=%s and payload ? 'workflow' "
                "and payload->>'decision' in ('ready','split') order by id desc limit 1", (corr,))
    r = cur.fetchone()
    if not r or not isinstance((r["payload"] or {}).get("workflow"), dict):
        return None
    return {"plan_id": r["id"], "at": r["created_at"], **r["payload"]["workflow"]}


def caps_of(cur, corr: str) -> tuple[dict, dict | None]:
    """そのチケットの上限と宣言。宣言がなければ既定値（any と M）で、宣言は None"""
    wf = latest_workflow(cur, corr)
    if wf and isinstance(wf.get("caps"), dict):
        return {**DEFAULT_CAPS["any"], **DEFAULT_CAPS[DEFAULT_BAND], **wf["caps"]}, wf
    return {**DEFAULT_CAPS["any"], **DEFAULT_CAPS[DEFAULT_BAND]}, wf


def ledger(cur, corr: str) -> dict:
    """チケットの台帳（実績）。上限の種類と同じ鍵で返す"""
    led = {k: 0 for k in ON_CAP if not k.startswith(("quick.", "analysis."))}
    cur.execute("select payload->>'reason' as r, count(*) as n from events where name='continue.enqueued' and correlation_id=%s group by 1", (corr,))
    for r in cur.fetchall():
        if r["r"] in CONTINUE_CAP:
            led[CONTINUE_CAP[r["r"]]] = int(r["n"])
    cur.execute("""select j.id, j.kind, j.result,
                          exists (select 1 from events c where c.name='continue.enqueued' and c.job_id=j.id and c.payload->>'reason'='conflict') as conflict
                     from jobs j where j.correlation_id=%s and j.state in ('succeeded','failed')""", (corr,))
    kinds: dict[str, int] = {}
    total = impl_cost = 0.0
    impl_turns = 0
    for j in cur.fetchall():
        c = _cost(j["result"])
        total += c
        kinds[j["kind"]] = kinds.get(j["kind"], 0) + 1
        if j["kind"] == "implement":
            impl_cost += c
            if not j["conflict"]:
                impl_turns += int((j["result"] or {}).get("num_turns") or 0)
    led["implement.turns"] = impl_turns
    led["implement.cost_usd"] = round(impl_cost, 4)
    led["ticket.cost_usd"] = round(total, 4)
    led["retries.qa_rerun"] = max(0, kinds.get("qa", 0) - 1)
    led["retries.judge_rounds"] = judge_rounds(cur, corr)
    return led


def judge_rounds(cur, corr: str) -> int:
    """judge のやり直し = 同じ PR の同じ head を 2 回以上裁いた数。judge は head ごとに 1 回積まれる（supervisor.judge_step）ので、
    続きのジョブで head が変わるたびの judge は新しい審査であってやり直しではない（検査の指摘の直しは retries.review_fix が数える）。
    head の記録がないジョブ（手で積んだものなど）は、その 1 件だけの審査として数える"""
    cur.execute("""select count(*) - count(distinct k) as n
                     from (select coalesce(j.issue_number::text || '@' || (e.payload->>'head_sha'), 'job:' || j.id) as k
                             from jobs j left join events e on e.job_id=j.id and e.name='job.enqueued'
                            where j.correlation_id=%s and j.kind='judge' and j.state in ('succeeded','failed')) t""", (corr,))
    return int(cur.fetchone()["n"] or 0)


def _record(cur, corr: str, kind: str, key: str, payload: dict) -> bool:
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('workflow.shadow',%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (corr, Jsonb({"kind": kind, **payload}), f"workflow.shadow:{key}"))
    return cur.fetchone() is not None


def _ticket(corr: str) -> tuple[str, int | None]:
    repo, _, n = corr.removeprefix("github:").partition("#")
    return repo, int(n) if n.isdigit() else None


def evaluate(cur, corr: str) -> list[dict]:
    """1 件のチケットの宣言と実績を比べ、新しく当たったものを残す。返り値は今回残したもの"""
    caps, wf = caps_of(cur, corr)
    repo, issue = _ticket(corr)
    out = []
    led = ledger(cur, corr)
    pid = wf["plan_id"] if wf else None
    for cap, actual in led.items():
        limit = caps.get(cap)
        if not isinstance(limit, (int, float)) or actual <= limit:
            continue
        p = {"repo": repo, "issue": issue, "plan_id": pid, "cap": cap, "declared": limit, "actual": actual,
             "action": ON_CAP.get(cap), "declared_by_planner": bool(wf) and cap in (wf.get("caps_declared") or {}), "default": wf is None}
        if _record(cur, corr, "cap_hit", f"cap:{corr}:{pid or 'default'}:{cap}", p):
            out.append(p)
    if not wf:
        return out
    skipped = {s.get("step") for s in wf.get("steps") or [] if isinstance(s, dict) and s.get("run") is False}
    cur.execute("select id, kind from jobs where correlation_id=%s and kind = any(%s) and enqueued_at >= %s order by id",
                (corr, list(SKIPPABLE_JOBS), wf["at"]))
    for j in cur.fetchall():
        step = SKIPPABLE_JOBS[j["kind"]]
        if step not in skipped:
            continue
        p = {"repo": repo, "issue": issue, "plan_id": wf["plan_id"], "step": step, "job_id": j["id"]}
        if _record(cur, corr, "would_skip", f"skip:{corr}:{wf['plan_id']}:{step}", p):
            out.append(p)
    return out


def would_wait(cur, corr: str, open_after: list[int]) -> bool:
    """宣言で start: after にしたのに、前のチケット（open_after。まだ開いている番号）が閉じる前に投入した（supervisor が投入の直前に呼ぶ）"""
    wf = latest_workflow(cur, corr)
    if not wf or not open_after:
        return False
    repo, issue = _ticket(corr)
    st = wf.get("start") or {}
    return _record(cur, corr, "would_wait", f"wait:{corr}:{wf['plan_id']}",
                   {"repo": repo, "issue": issue, "plan_id": wf["plan_id"], "after": open_after, "files": st.get("files") or [], "why": st.get("why") or ""})


def declared_start(cur, corr: str) -> dict:
    """プランナーの着手の宣言（{mode: now|after, after: [番号], files, why}）。宣言がなければ {}"""
    wf = latest_workflow(cur, corr)
    st = (wf or {}).get("start")
    return st if isinstance(st, dict) else {}


def declared_after(cur, corr: str) -> list[int]:
    wf = latest_workflow(cur, corr)
    st = (wf or {}).get("start") or {}
    return [int(x) for x in st.get("after") or []] if st.get("mode") == "after" else []


def step(cur, days: int = STEP_DAYS, limit: int = STEP_LIMIT, log=lambda *a: None) -> int:
    """最近実装のジョブが動いたチケットを突き合わせる（supervisor の周回）。宣言のないチケットは既定値で数える"""
    cur.execute("""select distinct j.correlation_id as corr from jobs j
                    where j.finished_at > now() - interval '2 days' and j.kind='implement'
                      and j.enqueued_at > now() - make_interval(days => %s)
                    limit %s""", (days, limit))
    n = 0
    for r in cur.fetchall():
        for p in evaluate(cur, r["corr"]):
            n += 1
            log("workflow shadow", r["corr"], p.get("cap") or p.get("step"))
    return n


def shadows(cur, corr: str) -> list[dict]:
    cur.execute("select occurred_at, payload from events where name='workflow.shadow' and correlation_id=%s order by id", (corr,))
    return [{"at": r["occurred_at"], **r["payload"]} for r in cur.fetchall()]


def cap_hits(cur, repo: str, days: int = 30, limit: int = 30) -> list[dict]:
    """この製品で上限に当たった過去の事例（プランナーの入力。ADR 0038 の 4b）"""
    cur.execute("select payload from events where name='workflow.shadow' and payload->>'kind'='cap_hit' and payload->>'repo'=%s "
                "and occurred_at > now() - make_interval(days => %s) order by id desc limit %s", (repo, days, limit))
    return [r["payload"] for r in cur.fetchall()]


def summary(cur, frm, to) -> dict:
    """日報の材料: 窓の中で、守らせていたら止めた・飛ばした・待たせた件数と、止めた後に使った費用の見込み"""
    cur.execute("select payload from events where name='workflow.shadow' and occurred_at >= %s and occurred_at < %s", (frm, to))
    rows = [r["payload"] for r in cur.fetchall()]
    caps: dict[str, int] = {}
    for p in rows:
        if p.get("kind") == "cap_hit":
            caps[p.get("cap")] = caps.get(p.get("cap"), 0) + 1
    cur.execute("select count(*) as n, count(*) filter (where payload->'workflow_marks' is not null and jsonb_array_length(payload->'workflow_marks') > 0) as marked "
                "from artifacts where type='Plan' and payload ? 'workflow' and created_at >= %s and created_at < %s", (frm, to))
    pl = cur.fetchone()
    cur.execute("select payload->>'kind' as k, count(*) as n from events where name='workflow.enforced' and occurred_at >= %s and occurred_at < %s group by 1",
                (frm, to))
    enforced = {r["k"]: int(r["n"]) for r in cur.fetchall()}
    return {"plans": int(pl["n"]), "plans_marked": int(pl["marked"]), "cap_hit": caps, "enforced": enforced,
            "would_skip": sum(1 for p in rows if p.get("kind") == "would_skip"),
            "would_wait": sum(1 for p in rows if p.get("kind") == "would_wait")}


def report_lines(s: dict) -> list[str]:
    if not s.get("plans") and not s.get("cap_hit") and not s.get("would_skip") and not s.get("would_wait") and not s.get("enforced"):
        return []
    lines = [f"ワークフローの宣言（ADR 0038）: 宣言 {s['plans']} 件（決まりで直した {s['plans_marked']} 件）"]
    e = s.get("enforced") or {}
    if e:
        lines.append(f"- 守らせた（衝突の解消の上限）: 待たせた {e.get('conflict_wait', 0)} 件、待ちの後に 1 回解いた {e.get('conflict_retry', 0)} 件、"
                     f"人に回した {e.get('conflict_human', 0)} 件")
    for cap, n in sorted(s.get("cap_hit", {}).items()):
        lines.append(f"- 上限を超えた（記録だけ）: {CAP_JA.get(cap, cap)} {n} 件（守らせれば{ACTION_JA.get(ON_CAP.get(cap), '')}）")
    if s.get("would_skip"):
        lines.append(f"- 守らせていたら飛ばした: {s['would_skip']} 件")
    if s.get("would_wait"):
        lines.append(f"- 守らせていたら待たせた（start: after）: {s['would_wait']} 件")
    return lines
