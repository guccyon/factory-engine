"""プランナーの判定を残し、経路どおりに配る（ADR 0025 のプランナーの節、ADR 0026 の着手判定）。

  Issue に factory:plan → plan のジョブ（core-gates/plan.py）→ ここで:
    ready:    Plan を残し、経路（役の並び）の最初の役のラベルを付ける（design_system → factory:design-system、
              ui_design → factory:design、implement → factory:planned）。以後は今の線（デザインの線、supervisor の投入）が配る。
              デザインシステムの仕事の後にほかの役が続く経路は、デザインシステムの仕事を別のチケットに切り（plan.split）、
              このチケットは factory:waiting-steps で待つ。そのチケットが閉じたら advance_steps が次の役のラベルを付ける
    ask:      理由と選択肢つきの問いを Issue のコメントに書き、factory:needs-info。返信が付いたら poll_answers が判定し直しを積む
    escalate: factory:plan-human を付けて人に回す
    split:    大きすぎる（ADR 0036 の 2）。タスクのまとまりを子のチケットとして起票し（窓口と同じ起票。依存のないものから factory:plan）、
              このチケットは factory:waiting-steps で待つ。子がすべて閉じたら advance_steps がこのチケットを閉じる

判断の根拠（3 層・とらなかった経路・入力の版・当てた guard と印）は Plan と plan.decided に残し、経路ごとの結果と突き合わせて
後で見返す（factoryctl plan show）。状態は制御プレーンの artifacts（Plan）と events（plan.*）に置く（ADR 0020）。
定義は definitions/planner.yaml。
"""
from __future__ import annotations

import re

from psycopg.types.json import Jsonb

import desk_line
import plan_outcome

# definitions/planner.yaml の labels・roles・asking と同じ値（dispatcher のコンテナには definitions/ がないので写す。食い違いはテストで見る）
TRIGGER_LABEL, NEEDS_INFO_LABEL, ESCALATE_LABEL = "factory:plan", "factory:needs-info", "factory:plan-human"
ROLE_LABELS = {"design_system": "factory:design-system", "ui_design": "factory:design", "implement": "factory:planned"}
ROLE_JA = {"design_system": "デザインシステムのデザイナー", "ui_design": "プロダクトデザイナー", "implement": "実装役"}
MAX_ROUNDS = 2
MARKER = "<!-- factory:plan-questions -->"
RUNNING_LABEL, QUEUED_LABEL = "factory:running", "factory:queued"
WAITING_LABEL = "factory:waiting-steps"   # 経路の前の役（別のチケット）が終わるのを待っている
GUARD_JA = {"no_design_system": "デザインシステムがなく、進め方の指定がない", "low_confidence": "プランナーの確信度が低い",
            "rounds_exhausted": "問いの往復を使い切った", "no_questions": "着手できないが問いが出なかった", "empty_steps": "経路が組めなかった",
            "oversized": "大きすぎる（切る基準に当たる）"}
ADVISORY_JA = {"small_change_with_designer": "小さな改修だがデザイナーを通す", "screen_without_designer": "新しい画面だがデザイナーを通さない",
               "design_after_implement": "実装の後にデザインの役がある", "no_size": "大きさの見積もりがない",
               "split_after_design": "大きいので、画面が決まってから実装の手前で切る"}
SIZE_TABLE_DAYS = 60     # プランナーに渡す実績の表の期間（ADR 0036 の 1）
LAYER_JA = {"db": "DB", "contract": "契約", "backend": "backend", "web": "Web", "mobile": "モバイル", "docs": "文書", "other": "その他"}
OVERSIZE_JA = {"layers": "触る層が多い", "files": "変わる見込みのファイルが多い", "turns": "見込みのターンが多い"}


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
               "evidence": p.get("evidence") or [], "expected_files": p.get("expected_files") or [], "interpretation": p.get("interpretation"), "confidence": p.get("confidence"),
               "agent_ready": p.get("ready"), "versions": result.get("versions") or {},
               "size": p.get("size"), "oversize": result.get("oversize") or [], "tasks": result.get("tasks") or [], "phase": result.get("phase"),
               "model": result.get("model"), "cost_usd": result.get("cost_usd"), "num_turns": result.get("num_turns")}
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Plan',%s,%s,'plan',%s) returning id",
                (corr, job_id, Jsonb(payload)))
    pid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('plan.decided',%s,%s,%s)",
                (corr, job_id, Jsonb({"plan_id": pid, "repo": repo, "issue": issue, "round": payload["round"], "decision": payload["decision"],
                                      "steps": payload["steps"], "efforts": [s.get("effort") for s in payload["step_details"] if isinstance(s, dict)],
                                      "design_scale": payload["design_scale"], "guards": payload["guards"], "advisories": payload["advisories"],
                                      "alternatives": len(payload["alternatives"]), "confidence": payload["confidence"],
                                      "size": payload["size"], "oversize": payload["oversize"], "tasks": len(payload["tasks"]), "phase": payload["phase"],
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


def attach_sizes(cur, repo: str, inp: dict) -> dict:
    """プランナーの入力に、この製品の実績の表（plan_outcome）を足す（ADR 0036 の 1）"""
    try:
        table = plan_outcome.size_table(plan_outcome.outcomes(cur, repo, SIZE_TABLE_DAYS))
    except Exception:  # noqa: BLE001  表がなくても判定はできる
        table = []
    return {**(inp or {}), "size_table": table}


PRE_IMPLEMENT_MARK = "split_after_design"


def wants_pre_implement(cur, corr: str) -> bool:
    """最後に着手できると決めた判定が「画面が決まってから切る」の印を付けていて、まだ実装の手前の判定をしていない（ADR 0036 の 3）"""
    cur.execute("select payload from artifacts where type='Plan' and correlation_id=%s and payload->>'decision' in ('ready','split') "
                "order by id desc limit 1", (corr,))
    r = cur.fetchone()
    p = (r or {}).get("payload") or {}
    return PRE_IMPLEMENT_MARK in (p.get("advisories") or []) and p.get("phase") != "implement"


def enqueue_pre_implement(cur, repo: str, issue: int, corr: str, why: str, extra: dict | None = None) -> int | None:
    """実装の手前の判定を積む（phase=implement）。画面が決まった後と、途中終了を 2 回したとき（ADR 0036 の 3・6）"""
    jid = enqueue(cur, repo, issue, corr, "tier-small", {"round": 1, "phase": "implement", **(extra or {})})
    if jid:
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('plan.pre_implement',%s,%s,%s)",
                    (corr, jid, Jsonb({"repo": repo, "issue": issue, "why": why, **(extra or {})})))
    return jid


def split_tasks(cur, gh, repo: str, issue: int, corr: str, plan_id: int, title: str, tasks: list[dict], p: dict) -> list[dict]:
    """大きなチケットを、タスクのまとまり（依存つき）として子のチケットに切る（ADR 0036 の 2）。起票は窓口と同じ（desk_line.file_task）。
    途中で失敗しても、起票した子は plan.task_filed で覚えているので、判定し直しても同じ子を 2 度作らない"""
    cur.execute("select payload from events where name='plan.task_filed' and correlation_id=%s and payload->>'plan_id'=%s", (corr, str(plan_id)))
    numbers = {e["payload"]["key"]: int(e["payload"]["number"]) for e in cur.fetchall()}
    parent = gh("GET", f"repos/{repo}/issues/{issue}") or {}
    parent_deps = [int(x) for m in desk_line.DEP_RE.finditer(parent.get("body") or "") for x in re.findall(r"#(\d+)", m.group(1))]   # 親の依存（足りない部品の仕事など）は子にも付ける
    prio = [l["name"] for l in parent.get("labels") or [] if isinstance(l, dict) and (l.get("name") or "").startswith("factory:priority:")]
    filed = []
    for t in tasks:
        deps = parent_deps + [numbers[k] for k in t.get("depends_on") or [] if k in numbers]
        if t["key"] not in numbers:
            kind = desk_line.KIND_JA.get(t.get("kind"), t.get("kind"))
            body = "\n".join([f"#{issue}（{title}）が大きいので、プランナーが切ったタスクのまとまりの {t['key']}（{kind}）です。{t.get('summary') or ''}",
                              "", f"もとのチケットの本文（画面の設計 UXSpec・受け入れ条件があればそれも）を正とし、このタスクではその範囲のうち下の「やること」だけを行います。"]
                             + desk_line.task_sections(t, deps)
                             + ["", f"<sub>Plan {plan_id}・手間の見込み {desk_line.EFFORT_JA.get(t.get('effort'), t.get('effort'))}</sub>",
                                f"<!-- factory:plan-split parent={issue} plan={plan_id} task={t['key']} -->"])
            it = desk_line.file_task(cur, gh, repo, f"{t['title']}（#{issue} の {t['key']}）", body, deps, prio)
            numbers[t["key"]] = it["number"]
            cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('plan.task_filed',%s,%s,%s) on conflict (dedupe_key) do nothing",
                        (corr, Jsonb({"plan_id": plan_id, "repo": repo, "issue": issue, "key": t["key"], "number": it["number"], "label": it["label"]}),
                         f"plan.task_filed:{plan_id}:{t['key']}"))
        filed.append({"key": t["key"], "number": numbers[t["key"]], "title": t["title"], "kind": t.get("kind"), "depends_on": deps})
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('plan.split',%s,%s,%s) on conflict (dedupe_key) do nothing",
                (corr, Jsonb({"plan_id": plan_id, "repo": repo, "issue": issue, "kind": "tasks", "children": [f["number"] for f in filed],
                              "child": None, "steps": p.get("steps_roles") or ["implement"], "oversize": p.get("oversize") or []}), f"plan.split:{plan_id}"))
    return filed


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
    if decision == "split" and gh and result.get("tasks"):
        filed = split_tasks(cur, gh, repo, issue, corr, plan_id, title, result["tasks"], {**p, "steps_roles": steps, "oversize": result.get("oversize")})
        swap_label(repo, issue, [RUNNING_LABEL], [WAITING_LABEL])
        sz = p.get("size") or {}
        why = "、".join(OVERSIZE_JA.get(h, h) for h in result.get("oversize") or [])
        body = [f"プランナー（ジョブ {job_id}）: 1 回の実装には大きいので（{why}。触る層 {'・'.join(LAYER_JA.get(l, l) for l in sz.get('layers') or [])}、"
                f"見込み {sz.get('turns_band') or '-'}、変わる見込みのファイル {len(p.get('expected_files') or [])}）、{len(filed)} 件のタスクに切りました。"
                "依存のないものからプランナーの着手判定に回ります。すべて閉じたら、このチケットを閉じます。", ""]
        body += [f"- #{f['number']} {f['title']}" + (f"（{' '.join(f'#{d}' for d in f['depends_on'])} の後）" if f["depends_on"] else "") for f in filed]
        body += ["", p.get("summary") or "", "", f"見積もりの根拠: {sz.get('basis') or '-'}", "", f"<sub>Plan {plan_id}</sub>"]
        comment(repo, issue, "\n".join(body))
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
            if not r or not (r["payload"].get("child") or r["payload"].get("children")):
                continue
            sp = r["payload"]
            if sp.get("kind") == "tasks":   # タスクのまとまりに切った（ADR 0036）: 子がすべて閉じたら、このチケットを閉じる
                n += close_split_parent(cur, gh, comment, repo, issue, corr, sp)
                continue
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


def close_split_parent(cur, gh, comment, repo: str, issue: int, corr: str, sp: dict) -> int:
    """切ったタスクのまとまり（ADR 0036）の子がすべて閉じたら、もとのチケットを閉じる。1 度だけ"""
    children = [int(c) for c in sp.get("children") or []]
    if not children or any((gh("GET", f"repos/{repo}/issues/{c}") or {}).get("state") != "closed" for c in children):
        return 0
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('plan.advanced',%s,%s,%s) on conflict (dedupe_key) do nothing returning id",
                (corr, Jsonb({"plan_id": sp["plan_id"], "repo": repo, "issue": issue, "after": children, "role": "done"}), f"plan.advanced:{sp['plan_id']}"))
    if cur.fetchone() is None:
        return 0
    comment(repo, issue, f"切ったタスク（{' '.join(f'#{c}' for c in children)}）がすべて閉じたので、このチケットを閉じます。")
    gh("PATCH", f"repos/{repo}/issues/{issue}", {"state": "closed", "state_reason": "completed"})
    cur.execute("update tickets set state='closed', updated_at=now() where source='github' and source_ref=%s and state<>'closed'", (f"{repo}#{issue}",))
    return 1
