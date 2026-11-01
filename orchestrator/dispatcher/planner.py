"""プランナーの判定を残し、経路どおりに配る（ADR 0025 のプランナーの節、ADR 0026 の着手判定）。

  Issue に factory:plan → plan のジョブ（core-gates/plan.py）→ ここで:
    ready:    Plan を残し、経路（役の並び）の最初の役のラベルを付ける（design_system → factory:design-system、
              ui_design → factory:design、implement → factory:planned）。以後は今の線（デザインの線、supervisor の投入）が配る。
              デザインシステムの仕事の後にほかの役が続く経路は、デザインシステムの仕事を別のチケットに切り（plan.split）、
              このチケットは factory:waiting-steps で待つ。そのチケットが閉じたら advance_steps が次の役のラベルを付ける
    ask:      理由と選択肢つきの問いを Issue のコメントに書き、factory:needs-info。返信が付いたら poll_answers が判定し直しを積む
    escalate: factory:plan-human を付けて人に回す

判断の根拠（3 層・とらなかった経路・入力の版・当てた guard と印）は Plan と plan.decided に残し、経路ごとの結果と突き合わせて
後で見返す（factoryctl plan show）。状態は制御プレーンの artifacts（Plan）と events（plan.*）に置く（ADR 0020）。
定義は definitions/planner.yaml。
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

# definitions/planner.yaml の labels・roles・asking と同じ値（dispatcher のコンテナには definitions/ がないので写す。食い違いはテストで見る）
TRIGGER_LABEL, NEEDS_INFO_LABEL, ESCALATE_LABEL = "factory:plan", "factory:needs-info", "factory:plan-human"
ROLE_LABELS = {"design_system": "factory:design-system", "ui_design": "factory:design", "implement": "factory:planned"}
ROLE_JA = {"design_system": "デザインシステムのデザイナー", "ui_design": "プロダクトデザイナー", "implement": "実装役"}
MAX_ROUNDS = 2
MARKER = "<!-- factory:plan-questions -->"
RUNNING_LABEL, QUEUED_LABEL = "factory:running", "factory:queued"
WAITING_LABEL = "factory:waiting-steps"   # 経路の前の役（別のチケット）が終わるのを待っている
GUARD_JA = {"no_design_system": "デザインシステムがなく、進め方の指定がない", "low_confidence": "プランナーの確信度が低い",
            "rounds_exhausted": "問いの往復を使い切った", "no_questions": "着手できないが問いが出なかった", "empty_steps": "経路が組めなかった"}
ADVISORY_JA = {"small_change_with_designer": "小さな改修だがデザイナーを通す", "screen_without_designer": "新しい画面だがデザイナーを通さない",
               "design_after_implement": "実装の後にデザインの役がある"}


def _ja(ids: list[str]) -> str:
    return "、".join(GUARD_JA.get(i) or ADVISORY_JA.get(i) or i for i in ids)


def path_ja(steps: list[str]) -> str:
    return " → ".join(ROLE_JA.get(r, r) for r in steps) or "（なし）"


def record(cur, repo: str, issue: int, corr: str, job_id: int, result: dict) -> int:
    """判定 1 回分を残す。後で経路ごとの結果と突き合わせて見返せるよう、根拠と入力の版をそのまま持つ"""
    p = result.get("plan") or {}
    payload = {"repo": repo, "issue": issue, "round": int(result.get("round") or 1), "decision": result.get("decision"),
               "steps": result.get("steps") or [], "step_details": p.get("steps") or [], "alternatives": p.get("alternatives") or [],
               "design_scale": p.get("design_scale"), "design_direction_given": p.get("design_direction_given"),
               "guards": result.get("guards") or [], "advisories": result.get("advisories") or [],
               "summary": p.get("summary"), "questions": p.get("questions") or [], "facts": result.get("facts") or {},
               "evidence": p.get("evidence") or [], "interpretation": p.get("interpretation"), "confidence": p.get("confidence"),
               "agent_ready": p.get("ready"), "versions": result.get("versions") or {},
               "model": result.get("model"), "cost_usd": result.get("cost_usd"), "num_turns": result.get("num_turns")}
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Plan',%s,%s,'plan',%s) returning id",
                (corr, job_id, Jsonb(payload)))
    pid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('plan.decided',%s,%s,%s)",
                (corr, job_id, Jsonb({"plan_id": pid, "repo": repo, "issue": issue, "round": payload["round"], "decision": payload["decision"],
                                      "steps": payload["steps"], "efforts": [s.get("effort") for s in payload["step_details"] if isinstance(s, dict)],
                                      "design_scale": payload["design_scale"], "guards": payload["guards"], "advisories": payload["advisories"],
                                      "alternatives": len(payload["alternatives"]), "confidence": payload["confidence"],
                                      "versions": payload["versions"], "model": payload["model"], "cost_usd": payload["cost_usd"]})))
    return pid


def rationale(p: dict, steps: list[str]) -> list[str]:
    """Issue に書く根拠（役ごとの理由と、とらなかった経路）"""
    lines = []
    for st in p.get("steps") or []:
        if isinstance(st, dict) and st.get("role") in steps:
            lines.append(f"- {ROLE_JA.get(st['role'], st['role'])}（{st.get('effort') or '-'}）: {(st.get('focus') or '').rstrip('。')}。{st.get('why') or ''}")
    alts = [a for a in p.get("alternatives") or [] if isinstance(a, dict)]
    if alts:
        lines += ["", "とらなかった経路:"] + [f"- {path_ja(a.get('steps') or [])}: {a.get('why_not') or ''}" for a in alts]
    return lines


def questions_comment(plan_id: int, round_: int, max_rounds: int, summary: str, questions: list[dict], guards: list[str]) -> str:
    lines = [MARKER, f"**プランナーからの質問（{round_}/{max_rounds}）**", "", "このチケットは、次の点が決まると着手できます。"
             "返事はこの Issue にコメントで書いてください（選択肢の番号か、言葉で）。", ""]
    for i, q in enumerate(questions, 1):
        rec = q.get("recommended") if isinstance(q.get("recommended"), int) else -1
        lines.append(f"{i}. {q.get('question')}（{q.get('why')}）")
        for j, o in enumerate(q.get("options") or []):
            lines.append(f"   - {chr(ord('a') + j)}. {o}{'（推奨）' if j == rec else ''}")
    if summary:
        lines += ["", f"読み取った内容: {summary}"]
    if guards:
        lines += ["", "当てはめた決まり: " + _ja(guards)]
    lines += ["", f"<sub>Plan {plan_id}</sub>"]
    return "\n".join(lines)


def split_design_system(cur, gh, repo: str, issue: int, corr: str, plan_id: int, title: str, steps: list[str], p: dict) -> int:
    """デザインシステムの仕事を別のチケットに切る（そのチケットの PR が閉じると、このチケットは経路の次の役へ進む）"""
    focus = next((st.get("focus") for st in p.get("steps") or [] if isinstance(st, dict) and st.get("role") == "design_system"), "") or ""
    body = "\n".join([f"#{issue} の経路（{path_ja(steps)}）の最初の役として、プランナーが切ったデザインシステムの仕事です。", "",
                       f"やること: {focus}", "", f"このチケットが閉じると、#{issue} は {ROLE_JA[steps[1]]} に進みます。", "",
                       f"<sub>Plan {plan_id}</sub>"])
    child = (gh("POST", f"repos/{repo}/issues", {"title": f"デザインシステム: {title}"[:200], "body": body, "labels": [ROLE_LABELS["design_system"]]}) or {}).get("number")
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('plan.split',%s,%s,%s) on conflict (dedupe_key) do nothing",
                (corr, Jsonb({"plan_id": plan_id, "repo": repo, "issue": issue, "child": child, "steps": steps}), f"plan.split:{plan_id}"))
    return child


def act(cur, repo: str, issue: int, corr: str, job_id: int, plan_id: int, result: dict, swap_label, comment, gh=None, title: str = ""):
    decision, steps = result.get("decision"), [r for r in result.get("steps") or [] if r in ROLE_LABELS]
    p, guards, marks = result.get("plan") or {}, result.get("guards") or [], result.get("advisories") or []
    if decision == "ready" and steps:
        if steps[0] == "design_system" and len(steps) > 1 and gh:   # デザインシステムの仕事は別のチケットにして、このチケットは待つ
            child = split_design_system(cur, gh, repo, issue, corr, plan_id, title, steps, p)
            swap_label(repo, issue, [RUNNING_LABEL], [WAITING_LABEL])
            lead = f"デザインシステムの仕事を #{child} に切りました。#{child} が閉じたら {ROLE_JA[steps[1]]} に進みます。"
        else:
            swap_label(repo, issue, [RUNNING_LABEL], [ROLE_LABELS[steps[0]]])
            lead = ""
        body = [f"プランナー（ジョブ {job_id}）: 着手できます。経路は **{path_ja(steps)}** です。{lead}", "",
                p.get("summary") or "", ""] + rationale(p, steps)
        if marks:
            body += ["", "見返すための印: " + _ja(marks)]
        comment(repo, issue, "\n".join(body + ["", f"<sub>Plan {plan_id}</sub>"]))
        return
    if decision == "ask":
        round_ = int(result.get("round") or 1)
        comment(repo, issue, questions_comment(plan_id, round_, MAX_ROUNDS, p.get("summary") or "", p.get("questions") or [], guards))
        swap_label(repo, issue, [RUNNING_LABEL], [NEEDS_INFO_LABEL])
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('plan.asked',%s,%s,%s)",
                    (corr, job_id, Jsonb({"plan_id": plan_id, "repo": repo, "issue": issue, "round": round_, "questions": len(p.get("questions") or [])})))
        return
    swap_label(repo, issue, [RUNNING_LABEL], [ESCALATE_LABEL])
    comment(repo, issue, f"プランナー（ジョブ {job_id}）: 着手の判定を人にお願いします（{_ja(guards) or '判定できなかった'}）。"
                         f"プランナーの見立ては {path_ja(steps)} です。決まったら、最初の役のラベル（{'・'.join(ROLE_LABELS.values())}）を付けるか、"
                         f"本文を直して `{TRIGGER_LABEL}` を付け直してください。"
                         f"\n\n{p.get('summary') or ''}\n\n<sub>Plan {plan_id}</sub>")


def plans(cur, corr: str) -> list[dict]:
    """1 件のチケットの判定の履歴（古い順）。factoryctl plan show が読む"""
    cur.execute("select id, job_id, created_at, payload from artifacts where type='Plan' and correlation_id=%s order by id", (corr,))
    return [{"id": r["id"], "job_id": r["job_id"], "created_at": r["created_at"], **r["payload"]} for r in cur.fetchall()]


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


def advance_steps(cur, gh, swap_label, comment, repos: list[str]) -> int:
    """factory:waiting-steps のチケットで、前の役のチケットが閉じたものを経路の次の役へ進める（dispatcher の周回）"""
    import urllib.parse
    n = 0
    for repo in repos:
        for it in gh("GET", f"repos/{repo}/issues?state=open&labels={urllib.parse.quote(WAITING_LABEL)}&per_page=50") or []:
            if "pull_request" in it:
                continue
            issue, corr = it["number"], f"github:{repo}#{it['number']}"
            cur.execute("select payload from events where name='plan.split' and correlation_id=%s order by id desc limit 1", (corr,))
            r = cur.fetchone()
            if not r or not r["payload"].get("child"):
                continue
            sp = r["payload"]
            if (gh("GET", f"repos/{repo}/issues/{sp['child']}") or {}).get("state") != "closed":
                continue
            nxt = sp["steps"][1]
            cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('plan.advanced',%s,%s,%s) on conflict (dedupe_key) do nothing returning id",
                        (corr, Jsonb({"plan_id": sp["plan_id"], "repo": repo, "issue": issue, "after": sp["child"], "role": nxt}), f"plan.advanced:{sp['plan_id']}"))
            if cur.fetchone() is None:
                continue
            swap_label(repo, issue, [WAITING_LABEL], [ROLE_LABELS[nxt]])
            comment(repo, issue, f"#{sp['child']}（デザインシステム）が閉じたので、経路の次の役（{ROLE_JA[nxt]}）に進めました。")
            n += 1
    return n
