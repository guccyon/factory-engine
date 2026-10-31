"""プランナーの判定を残し、経路どおりに配る（ADR 0025 のプランナーの節、ADR 0026 の着手判定）。

  Issue に factory:plan → plan のジョブ（core-gates/plan.py）→ ここで:
    ready:    Plan を残し、経路のラベルを付ける（implement → factory:planned、ui_design → factory:design、
              design_system → factory:design-system）。以後は今の線（supervisor の投入、デザインの線）がラベルどおりに配る
    ask:      理由と選択肢つきの問いを Issue のコメントに書き、factory:needs-info。返信が付いたら poll_answers が判定し直しを積む
    escalate: factory:plan-human を付けて人に回す

状態（判定・問い・返事）は制御プレーンの artifacts（Plan）と events（plan.*）に置く（ADR 0020）。
定義は definitions/planner.yaml。
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

# definitions/planner.yaml の labels・routes・asking と同じ値（dispatcher のコンテナには definitions/ がないので写す。食い違いはテストで見る）
TRIGGER_LABEL, NEEDS_INFO_LABEL, ESCALATE_LABEL = "factory:plan", "factory:needs-info", "factory:plan-human"
ROUTE_LABELS = {"implement": "factory:planned", "ui_design": "factory:design", "design_system": "factory:design-system"}
ROUTE_JA = {"implement": "実装役へ直行", "ui_design": "画面のデザイナー", "design_system": "デザインシステムのデザイナー"}
MAX_ROUNDS = 2
MARKER = "<!-- factory:plan-questions -->"
RUNNING_LABEL, QUEUED_LABEL = "factory:running", "factory:queued"
ROUTE_NOTE = {"design_system": "デザインシステムのデザイナー役ができるまでは、このラベルのまま待ちます。"}
NOTE_JA = {"small_change_direct": "小さな改修なのでデザイナーを通さない", "system_scale": "デザインシステムそのものの変更",
           "screen_needs_design": "新しい画面・作り直しなのでデザインを通す", "no_design_system": "デザインシステムがなく、進め方の指定がない",
           "low_confidence": "プランナーの確信度が低い", "rounds_exhausted": "問いの往復を使い切った", "no_questions": "着手できないが問いが出なかった"}


def record(cur, repo: str, issue: int, corr: str, job_id: int, result: dict) -> int:
    p = result.get("plan") or {}
    payload = {"repo": repo, "issue": issue, "round": int(result.get("round") or 1), "decision": result.get("decision"),
               "route": result.get("route"), "design_scale": p.get("design_scale"), "notes": result.get("notes") or [],
               "summary": p.get("summary"), "questions": p.get("questions") or [], "facts": result.get("facts") or {},
               "evidence": p.get("evidence") or [], "interpretation": p.get("interpretation"), "confidence": p.get("confidence"),
               "model": result.get("model"), "cost_usd": result.get("cost_usd")}
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Plan',%s,%s,'plan',%s) returning id",
                (corr, job_id, Jsonb(payload)))
    pid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('plan.decided',%s,%s,%s)",
                (corr, job_id, Jsonb({"plan_id": pid, "repo": repo, "issue": issue, "round": payload["round"], "decision": payload["decision"],
                                      "route": payload["route"], "design_scale": payload["design_scale"], "notes": payload["notes"],
                                      "confidence": payload["confidence"], "cost_usd": payload["cost_usd"]})))
    return pid


def questions_comment(plan_id: int, round_: int, max_rounds: int, summary: str, questions: list[dict], notes: list[str]) -> str:
    lines = [MARKER, f"**プランナーからの質問（{round_}/{max_rounds}）**", "", "このチケットは、次の点が決まると着手できます。"
             "返事はこの Issue にコメントで書いてください（選択肢の番号か、言葉で）。", ""]
    for i, q in enumerate(questions, 1):
        rec = q.get("recommended") if isinstance(q.get("recommended"), int) else -1
        lines.append(f"{i}. {q.get('question')}（{q.get('why')}）")
        for j, o in enumerate(q.get("options") or []):
            lines.append(f"   - {chr(ord('a') + j)}. {o}{'（推奨）' if j == rec else ''}")
    if summary:
        lines += ["", f"読み取った内容: {summary}"]
    if notes:
        lines += ["", "当てはめた決まり: " + "、".join(NOTE_JA.get(n, n) for n in notes)]
    lines += ["", f"<sub>Plan {plan_id}</sub>"]
    return "\n".join(lines)


def act(cur, repo: str, issue: int, corr: str, job_id: int, plan_id: int, result: dict, swap_label, comment):
    decision, route = result.get("decision"), result.get("route")
    p, notes = result.get("plan") or {}, result.get("notes") or []
    if decision == "ready" and route in ROUTE_LABELS:
        swap_label(repo, issue, [RUNNING_LABEL], [ROUTE_LABELS[route]])
        why = "（" + "、".join(NOTE_JA.get(n, n) for n in notes) + "）" if notes else ""
        comment(repo, issue, f"プランナー（ジョブ {job_id}）: 着手できます。経路は **{ROUTE_JA[route]}**{why}。"
                             f"{ROUTE_NOTE.get(route, '')}\n\n{p.get('summary') or ''}\n\n<sub>Plan {plan_id}</sub>")
        return
    if decision == "ask":
        round_ = int(result.get("round") or 1)
        comment(repo, issue, questions_comment(plan_id, round_, MAX_ROUNDS, p.get("summary") or "", p.get("questions") or [], notes))
        swap_label(repo, issue, [RUNNING_LABEL], [NEEDS_INFO_LABEL])
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('plan.asked',%s,%s,%s)",
                    (corr, job_id, Jsonb({"plan_id": plan_id, "repo": repo, "issue": issue, "round": round_, "questions": len(p.get("questions") or [])})))
        return
    swap_label(repo, issue, [RUNNING_LABEL], [ESCALATE_LABEL])
    comment(repo, issue, f"プランナー（ジョブ {job_id}）: 着手の判定を人にお願いします（{'、'.join(NOTE_JA.get(n, n) for n in notes) or '判定できなかった'}）。"
                         f"決まったら、経路のラベル（{'・'.join(ROUTE_LABELS.values())}）を付けるか、本文を直して `{TRIGGER_LABEL}` を付け直してください。"
                         f"\n\n{p.get('summary') or ''}\n\n<sub>Plan {plan_id}</sub>")


def answered(comments: list[dict]) -> bool:
    """最後の問いのコメントより後に、工場の印のないコメントがあれば返事があったとみなす"""
    last_q = max((i for i, c in enumerate(comments) if MARKER in (c.get("body") or "")), default=None)
    if last_q is None:
        return False
    return any("<!-- factory" not in (c.get("body") or "") and (c.get("body") or "").strip() for c in comments[last_q + 1:])


def last_round(cur, corr: str) -> int:
    cur.execute("select payload->>'round' as r from artifacts where type='Plan' and correlation_id=%s order by id desc limit 1", (corr,))
    r = cur.fetchone()
    return int(r["r"]) if r and r["r"] else 0


def enqueue(cur, repo: str, issue: int, corr: str, model_tier: str, inp: dict) -> int | None:
    import psycopg
    try:
        cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind) "
                    "values (%s,%s,%s,1,'planner',%s,'plan') returning id", (corr, repo, issue, model_tier))
    except psycopg.errors.UniqueViolation:
        return None
    jid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                (corr, jid, Jsonb({"by": "planner", "kind": "plan", "repo": repo, "issue": issue, "attempt": 1, "input": inp})))
    return jid


def poll_answers(cur, gh, swap_label, repos: list[str], model_tier: str) -> int:
    """factory:needs-info のチケットで返事が付いたものを、round を 1 つ進めて判定し直す"""
    import urllib.parse
    n = 0
    for repo in repos:
        for it in gh("GET", f"repos/{repo}/issues?state=open&labels={urllib.parse.quote(NEEDS_INFO_LABEL)}&per_page=50") or []:
            if "pull_request" in it:
                continue
            issue = it["number"]
            comments = gh("GET", f"repos/{repo}/issues/{issue}/comments?per_page=100") or []
            if not answered(comments):
                continue
            corr = f"github:{repo}#{issue}"
            round_ = last_round(cur, corr) + 1
            jid = enqueue(cur, repo, issue, corr, model_tier, {"round": round_})
            if not jid:
                continue
            cur.execute("insert into events(name, correlation_id, job_id, payload) values ('plan.answered',%s,%s,%s)",
                        (corr, jid, Jsonb({"repo": repo, "issue": issue, "round": round_})))
            swap_label(repo, issue, [NEEDS_INFO_LABEL], [QUEUED_LABEL])
            n += 1
    return n
