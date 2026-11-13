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

着手できる判定（ready・split）では、型つきの上流の成果物（P4 の typed_upstream）として、要件（Requirements）と
このタスクが増やす・変える・守るユーザーストーリーを artifacts（Requirements）に残し、Issue の本文の節に書く。後の役は本文を読む。
従う ADR と新しく残す判断は artifacts（ADR。status proposed）に、契約の差分は artifacts（ContractDiff）に残し、同じ節に書く。

判断の根拠（3 層・とらなかった経路・入力の版・当てた guard と印）は Plan と plan.decided に残し、経路ごとの結果と突き合わせて
後で見返す（factoryctl plan show）。状態は制御プレーンの artifacts（Plan）と events（plan.*）に置く（ADR 0020）。
定義は definitions/planner.yaml。
"""
from __future__ import annotations

import re

from psycopg.types.json import Jsonb

import desk_line
import plan_outcome
import serialize
import workflow_shadow

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
               "split_after_design": "大きいので、画面が決まってから実装の手前で切る", "no_requirements": "要件（受け入れ条件）がない",
               "no_stories": "画面を含むのにユーザーストーリーの宣言がない", "unknown_story": "製品にないストーリーの id を落とした", "unknown_kpi": "製品にない KPI の見込みを落とした",
               "story_exists": "増やすストーリーの id がもうあるので、変えるにした", "unknown_adr": "製品にない ADR の番号を落とした",
               "unknown_contract": "製品にない契約の置き場の差分を落とした", "breaking_contract": "互換を壊す契約の差分がある",
               "no_contract_diff": "契約の層を触るのに契約の差分がない"}
# definitions/planner.yaml の upstream と同じ値
REQ_MARK, REQ_MARK_END = "<!-- factory-requirements:v1 -->", "<!-- /factory-requirements -->"
STORY_ACTION_JA = {"add": "増やす", "change": "変える", "keep": "守る"}
STORIES_DIR = "specs/stories/"
ADR_DIR, CONTRACTS_DIR = "docs/adr/", "specs/contracts/"
CONTRACT_KIND_JA = {"add": "足す", "change": "変える", "remove": "消す"}
COMPAT_JA = {"none": "互換に影響なし", "additive": "足すだけ（互換を保つ）", "breaking": "互換を壊す"}
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
               "requirements": result.get("requirements"), "stories": result.get("stories") or [],
               "adrs": adrs_of(result), "contract_changes": contracts_of(result),
               "model": result.get("model"), "cost_usd": result.get("cost_usd"), "num_turns": result.get("num_turns")}
    if isinstance(result.get("workflow"), dict):   # ワークフローの宣言（ADR 0038）。決まりで直した後の形と、直した印
        payload["workflow"], payload["workflow_marks"] = result["workflow"], result.get("workflow_marks") or []
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Plan',%s,%s,'plan',%s) returning id",
                (corr, job_id, Jsonb(payload)))
    pid = cur.fetchone()["id"]
    req = result.get("requirements")
    if isinstance(req, dict) and req.get("acceptance_criteria"):   # 型つきの上流の成果物（artifacts.yaml の Requirements）
        cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Requirements',%s,%s,'plan',%s)",
                    (corr, job_id, Jsonb({"repo": repo, "issue": issue, "plan_id": pid, **req, "stories": payload["stories"],
                                          "adrs_follow": payload["adrs"]["follow"]})))
    for a in payload["adrs"]["new"]:   # 新しく残す判断（artifacts.yaml の ADR）。ファイルは実装役が同じ PR で書く
        cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('ADR',%s,%s,'plan',%s)",
                    (corr, job_id, Jsonb({"repo": repo, "issue": issue, "plan_id": pid, **a})))
    for c in payload["contract_changes"]:   # 契約の差分（artifacts.yaml の ContractDiff）
        cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('ContractDiff',%s,%s,'plan',%s)",
                    (corr, job_id, Jsonb({"repo": repo, "issue": issue, "plan_id": pid, **c})))
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('plan.decided',%s,%s,%s)",
                (corr, job_id, Jsonb({"plan_id": pid, "repo": repo, "issue": issue, "round": payload["round"], "decision": payload["decision"],
                                      "steps": payload["steps"], "efforts": [s.get("effort") for s in payload["step_details"] if isinstance(s, dict)],
                                      "design_scale": payload["design_scale"], "guards": payload["guards"], "advisories": payload["advisories"],
                                      "alternatives": len(payload["alternatives"]), "confidence": payload["confidence"],
                                      "size": payload["size"], "oversize": payload["oversize"], "tasks": len(payload["tasks"]), "phase": payload["phase"],
                                      "requirements": bool(isinstance(req, dict) and req.get("acceptance_criteria")),
                                      "stories": [{"action": x.get("action"), "id": x.get("id")} for x in payload["stories"] if isinstance(x, dict)],
                                      "adrs": {"follow": [x.get("id") for x in payload["adrs"]["follow"]], "new": len(payload["adrs"]["new"])},
                                      "contracts": [{"contract": c.get("contract"), "kind": c.get("kind"), "compat_impact": c.get("compat_impact")}
                                                    for c in payload["contract_changes"]],
                                      "versions": payload["versions"], "model": payload["model"], "cost_usd": payload["cost_usd"],
                                      **({"workflow": workflow_summary(payload["workflow"]), "workflow_marks": payload["workflow_marks"]} if "workflow" in payload else {})})))
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


def adrs_of(result: dict) -> dict:
    a = result.get("adrs") if isinstance(result.get("adrs"), dict) else {}
    return {"follow": [x for x in a.get("follow") or [] if isinstance(x, dict)], "new": [x for x in a.get("new") or [] if isinstance(x, dict)]}


def contracts_of(result: dict) -> list[dict]:
    return [c for c in result.get("contract_changes") or [] if isinstance(c, dict)]


def decisions_lines(adrs: dict, contracts: list[dict]) -> list[str]:
    """節の「判断（ADR）」と「契約の差分」"""
    lines = []
    if adrs["follow"] or adrs["new"]:
        lines.append("判断（ADR）:")
        lines += [f"- 従う ADR {a.get('id')}: {a.get('why') or ''}" for a in adrs["follow"]]
        for a in adrs["new"]:
            lines.append(f"- 新しく残す: **{a.get('title')}**")
            lines += [f"  - {k}: {a[f]}" for f, k in (("context", "文脈"), ("decision", "決定"), ("consequences", "結果")) if a.get(f)]
            if a.get("alternatives"):
                lines.append("  - とらなかった案: " + "／".join(a["alternatives"]))
        if adrs["new"]:
            lines += ["", f"実装役へ: 新しく残す判断は、この変更と同じ PR で `{ADR_DIR}<次の番号>-<名前>.md` に書く（状態は「提案（工場）」、"
                      "既存の ADR の見出し 文脈・決定・結果 に合わせる）。"]
        lines.append("")
    if contracts:
        lines.append("契約の差分:")
        for c in contracts:
            ops = f"（{'、'.join(f'`{o}`' for o in c.get('operations') or [])}）" if c.get("operations") else ""
            lines.append(f"- `{c.get('contract')}` を{CONTRACT_KIND_JA.get(c.get('kind'), c.get('kind'))}{ops}・"
                         f"{COMPAT_JA.get(c.get('compat_impact'), c.get('compat_impact'))}: {c.get('summary') or ''}")
        lines += ["", "実装役へ: 契約のファイルを先に直し、実装はそれに合わせる。"]
        if any(c.get("compat_impact") == "breaking" for c in contracts):
            lines[-1] += "互換を壊す差分は、足す（expand）→ 移す → 消す（contract）に分け、消す工程は人の承認を待つ（製品の ADR 0004）。"
        lines.append("")
    return lines


def requirements_section(plan_id: int, req: dict | None, stories: list[dict], adrs: dict | None = None, contracts: list[dict] | None = None) -> str:
    """Issue の本文に書く「要件とユーザーストーリー」の節（後の役が本文から読む）。従う ADR・新しい判断・契約の差分も同じ節に書く"""
    lines = [REQ_MARK, "## 要件とユーザーストーリー（プランナー）", ""]
    if req:
        lines += ["受け入れ条件:"] + [f"- {a}" for a in req.get("acceptance_criteria") or []]
        if req.get("non_goals"):
            lines += ["", "やらないこと:"] + [f"- {a}" for a in req["non_goals"]]
        if req.get("success_definition"):
            lines += ["", f"できあがりの定義: {req['success_definition']}"]
        kf = req.get("kpi_forecast")
        if isinstance(kf, dict) and kf.get("kpi"):   # KPI の見込み（ADR 0043 の O3）。週次の Report が実測と突き合わせる
            lines += ["", f"KPI の見込み: `{kf['kpi']}` が {kf.get('change'):+g} 動く（{kf.get('why') or '理由なし'}）"]
        lines.append("")
    if stories:
        lines.append("ユーザーストーリー:")
        for st in stories:
            head = f"- {STORY_ACTION_JA.get(st.get('action'), st.get('action'))} `{st.get('id')}`"
            if st.get("action") == "add" and (st.get("title") or st.get("actor")):
                head += f"（新規: {st.get('title') or ''}{'・' + st['actor'] if st.get('actor') else ''}）"
            lines.append(f"{head}: {st.get('why') or ''}")
        lines += ["", f"実装役へ: 増やす・変えるストーリーは、この変更と同じ PR で `{STORIES_DIR}<id>.yaml` に書く（既存のストーリーと actors.yaml の書き方に合わせる）。"
                  "守るストーリーの流れは壊さない（QA 役が回帰で流す）。", ""]
    lines += decisions_lines(adrs or {"follow": [], "new": []}, contracts or [])
    lines += [f"<sub>Plan {plan_id}</sub>", REQ_MARK_END]
    return "\n".join(lines)


def replace_requirements(body: str, section: str) -> str:
    pat = re.compile(re.escape(REQ_MARK) + r".*?" + re.escape(REQ_MARK_END), re.S)
    if pat.search(body):
        return pat.sub(lambda _: section, body, count=1)
    return (body.rstrip() + "\n\n" if body.strip() else "") + section + "\n"


def write_requirements(gh, repo: str, issue: int, plan_id: int, result: dict) -> bool:
    """要件とストーリーを Issue の本文の節に書く（前の判定の節は置き換える）。出すものがなければ書かない"""
    req, stories, adrs, contracts = result.get("requirements"), result.get("stories") or [], adrs_of(result), contracts_of(result)
    if not gh or not ((isinstance(req, dict) and req.get("acceptance_criteria")) or stories or adrs["follow"] or adrs["new"] or contracts):
        return False
    it = gh("GET", f"repos/{repo}/issues/{issue}") or {}
    gh("PATCH", f"repos/{repo}/issues/{issue}",
       {"body": replace_requirements(it.get("body") or "", requirements_section(plan_id, req, stories, adrs, contracts))})
    return True


def upstream_note(result: dict) -> str:
    st = result.get("stories") or []
    by = "、".join(f"{STORY_ACTION_JA.get(x.get('action'), x.get('action'))} `{x.get('id')}`" for x in st) or "なし"
    adrs, contracts = adrs_of(result), contracts_of(result)
    extra = ""
    if adrs["follow"] or adrs["new"]:
        extra += f"、ADR: 従う {len(adrs['follow'])}・新しく残す {len(adrs['new'])}"
    if contracts:
        extra += f"、契約の差分 {len(contracts)}"
    return f"要件とユーザーストーリーを本文の「要件とユーザーストーリー（プランナー）」の節に書きました（ストーリー: {by}{extra}）。"


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


INFLIGHT_LIMIT = 30


def open_defects(cur, repo: str, corr: str) -> list[dict]:
    """その製品で開いている QA の不具合（Incident。演習と、修正を出し直したものを除く）。このチケット自身も除く"""
    import incident
    cur.execute("select payload->>'incident_id' as iid from events where name='major.rereleased'")
    done = {r["iid"] for r in cur.fetchall()}
    rows = []
    for i in incident.open_incidents(cur, repo):
        num = str(i.get("issue") or "").rstrip("/").rsplit("/", 1)[-1]
        if i.get("drill") or i["incident_id"] in done or not num.isdigit() or f"github:{repo}#{num}" == corr:
            continue
        rows.append({"issue": int(num), "severity": i.get("severity"), "title": i.get("title")})
    return rows[:30]


def attach_context(cur, repo: str, corr: str, inp: dict) -> dict:
    """プランナーの入力に、流れている・待っているタスクと触る（見込みの）ファイル、よく衝突するファイル、上限に当たった過去の事例を足す
    （ADR 0038 の 4b。着手のしかた start を決める材料と、知見の蓄積）。制御プレーンの記録だけで組み立てる（GitHub を呼ばない）"""
    out = dict(inp or {})
    try:
        cur.execute("select source_ref, title, state from tickets where source='github' and repo=%s and state in ('planned','active') "
                    "order by updated_at desc limit %s", (repo, INFLIGHT_LIMIT))
        rows = []
        for t in cur.fetchall():
            c = f"github:{t['source_ref']}"
            if c == corr:
                continue
            _, _, n = t["source_ref"].partition("#")
            rows.append({"issue": int(n) if n.isdigit() else n, "title": t["title"], "state": t["state"],
                         "files": sorted(serialize.expected_files(cur, c, None))})
        out["inflight"] = rows
        out["hot_files"] = serialize.hot_files(cur)
        out["cap_hits"] = workflow_shadow.cap_hits(cur, repo)
        cur.execute("select title from tickets where source='github' and 'github:' || source_ref = %s order by id desc limit 1", (corr,))
        t = cur.fetchone()
        out["similar"] = plan_outcome.similar(cur, repo, corr, (t or {}).get("title") or "")   # 似た過去のチケットの実績（見積もりの材料）
        out["open_defects"] = open_defects(cur, repo, corr)   # 開いている QA の不具合（同じ原因をまとめて直す材料。ADR 0053 の 6）
    except Exception:  # noqa: BLE001  材料がなくても判定はできる
        out.setdefault("inflight", [])
    return out


def workflow_summary(wf: dict) -> dict:
    """plan.decided に載せる宣言の要約（飛ばすステップ・厚くするステップ・上限・着手のしかた・渡し先）"""
    steps = [s for s in wf.get("steps") or [] if isinstance(s, dict)]
    return {"path": wf.get("path"), "skip": [s["step"] for s in steps if s.get("run") is False],
            "deep": [s["step"] for s in steps if s.get("run") is True and s.get("effort") == "deep"],
            "caps": wf.get("caps") or {}, "start": (wf.get("start") or {}).get("mode"), "after": (wf.get("start") or {}).get("after") or [],
            "handoff": wf.get("handoff") or {}}


def workflow_lines(wf: dict | None, marks: list[str] | None = None) -> list[str]:
    """Issue のコメントと factoryctl plan show に出す、ワークフローの宣言"""
    if not isinstance(wf, dict):
        return []
    s = workflow_summary(wf)
    caps = wf.get("caps") or {}
    lines = ["", "ワークフローの宣言（ADR 0038。衝突の解消・費用の上限・着手・飛ばしは指示役が守らせる。ほかの上限は記録だけ）:"]
    for st in wf.get("steps") or []:
        run = {True: "動かす", False: "飛ばす", "auto": "条件で動く"}.get(st.get("run"), str(st.get("run")))
        lines.append(f"- {st.get('step')}: {run}（{st.get('effort') or '-'}）{('。' + st['why']) if st.get('why') else ''}")
    lines.append("- 上限: " + "、".join(f"{workflow_shadow.CAP_JA.get(k, k)} {v}" for k, v in caps.items() if not k.startswith(("quick.", "analysis."))))
    if s["start"] == "after":
        lines.append(f"- 着手: {' '.join(f'#{n}' for n in s['after'])} の後（{(wf.get('start') or {}).get('why') or ''}）")
    lines.append(f"- 渡し先: 出荷 {s['handoff'].get('ship')}、Learn {s['handoff'].get('learn')}")
    if marks:
        lines.append("- 決まりで直したところ: " + "、".join(marks))
    return lines


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
                              "", "もとのチケットの本文（要件とユーザーストーリー・画面の設計 UXSpec・受け入れ条件があればそれも）を正とし、このタスクではその範囲のうち下の「やること」だけを行います。"]
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
    wrote = decision in ("ready", "split") and bool(steps) and write_requirements(gh, repo, issue, plan_id, result)   # 子を切る前に書く（子は親の本文を正として読む）
    note = [""] + [upstream_note(result)] if wrote else []
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
        body += note
        if marks:
            body += ["", "見返すための印: " + _ja(marks)]
        body += workflow_lines(result.get("workflow"), result.get("workflow_marks"))
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
        body += ["", p.get("summary") or "", "", f"見積もりの根拠: {sz.get('basis') or '-'}"] + note + ["", f"<sub>Plan {plan_id}</sub>"]
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
