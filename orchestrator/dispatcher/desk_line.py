"""窓口の依頼の壁打ちと、タスクのまとまりの投入（ADR 0030 の順番 2）。dispatcher の側。

  依頼を開く・返事をする（desk_api）→ ここで相談役のジョブ（desk_consult、窓口の枠）を積む
  → core-gates/desk_consult.py が 問い か 下書き を返す → ここで会話に書く（問い: waiting_requester、下書き: drafted）
  → 依頼者が下書きを 1 回確認する（desk.confirm_draft。confirmed）
  → ここで製品の Issue として起票する（依存の順。依存のないタスクは factory:plan、あるタスクは factory:desk-waiting）
  → 依存が閉じたタスクを factory:plan に替える（advance_waiting）。以後は今のライン（プランナーの着手判定 → 経路どおりに配る）

相談役は読むだけ。起票・ラベル・優先度・会話への書き込みはここで行う。依頼者の言葉はデータとして相談役に渡し、命令として扱わない。
起票したチケットは、入口は今のラインと同じ GitHub の Issue（tickets.source は github）で、もとの会話を desk_conversation_id に持つ
（依頼 → 確定 → チケット → PR → 出荷と追える。依頼者は自分の依頼から作ったチケットの優先度・保留を変えられる）。
優先度は依頼者の指定（priority_set_by は desk:<名前>）。プランナーは並べ替えず、見込み（前に並ぶ件数・押し出されるチケット・費用）を見せるだけ。
定義は definitions/desk.yaml（dispatcher のコンテナには definitions/ がないので、使う値をここに写す。食い違いはテストで見る）。
"""
from __future__ import annotations

import os
import re
import statistics
import urllib.parse

import psycopg
from psycopg.types.json import Jsonb

import desk
import inbox
import operations
import ops
import requester_prefs

KIND = "desk_consult"
WAITING_LABEL, TRIGGER_LABEL = "factory:desk-waiting", "factory:plan"
MAX_ROUNDS = 3
MAX_USD = float(os.environ.get("DESK_CONSULT_MAX_USD", "2"))   # 相談 1 件の費用の上限（ADR 0030 の 6。仮置き）
KIND_JA = {"requirements": "要件", "design": "画面", "contract": "契約", "implement": "実装", "answer": "成果物"}
EFFORT_JA = {"light": "軽い", "standard": "ふつう", "deep": "重い"}
CONSULT_WORK = ["desk.converse", "plan.route"]   # 相談役の業務（definitions/work.yaml の from_agent_steps の desk.consult）
MARKER = "<!-- factory:desk conversation={cid} draft={draft} task={key} -->"
DEP_RE = re.compile(r"(?:依存|depends\s+on)\s*[:：]\s*((?:#\d+[\s,、]*)+)", re.IGNORECASE)   # supervisor.DEP_RE と同じ


def _event(cur, name: str, corr: str | None, payload: dict, dedupe_key: str | None = None, job_id: int | None = None) -> bool:
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values (%s,%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (name, corr, job_id, Jsonb(payload), dedupe_key))
    return cur.fetchone() is not None


# ------------------------------------------------------------------ 相談役のジョブを積む
def pending(cur) -> list[dict]:
    """相談役に回す会話: 依頼で、open（依頼者が書いた後）で、相談役のジョブが動いておらず、最後の依頼者の発言が最後に積んだジョブより新しい"""
    cur.execute("""
      select c.*, m.last_msg from desk_conversations c
        join lateral (select max(e.id) as last_msg from events e where e.correlation_id = 'desk:' || c.id
                       and e.name = 'desk.message' and e.payload->>'role' = 'requester') m on true
       where c.kind = 'request' and c.state = 'open'
         and not exists (select 1 from jobs j where j.correlation_id = 'desk:' || c.id and j.state in ('queued','running'))
         and m.last_msg > coalesce((select max(e.id) from events e where e.correlation_id = 'desk:' || c.id
                                     and e.name = 'job.enqueued'), 0)
       order by c.updated_at""")
    return cur.fetchall()


def messages(cur, cid: int) -> list[dict]:
    cur.execute("select id, occurred_at, payload from events where correlation_id=%s and name in ('desk.message','desk.answered') order by id",
                (desk.corr(cid),))
    return [{"id": e["id"], "at": e["occurred_at"].isoformat(), **{k: v for k, v in e["payload"].items() if k != "conversation_id"}}
            for e in cur.fetchall()]


def questions_asked(msgs: list[dict]) -> int:
    return sum(1 for m in msgs if m.get("role") == "planner" and m.get("kind") == "question")


def consult_input(cur, c: dict) -> dict:
    """相談役に渡す材料（制御プレーンの記録だけで組み立てる）。依頼者の言葉はデータとして渡す"""
    msgs = messages(cur, c["id"])
    d = desk.latest_draft(cur, c["id"])
    cur.execute("select source_ref, title, state, priority, waiting_on from tickets where state in ('planned','active') "
                "order by priority desc, created_at limit 80")
    tickets = [{"ref": t["source_ref"], "title": t["title"], "state": t["state"], "level": ops.level_of(t["priority"]),
                "waiting_on": t["waiting_on"]} for t in cur.fetchall()]
    # 最近閉じたチケット（済んだ仕事を依存や「待ち」に挙げないため。2026-11-06 相談 1 で、閉じた #455 を待つ下書きが出た）
    cur.execute("select source_ref, title, updated_at from tickets where state='closed' and repo=%s and updated_at > now() - interval '30 days' "
                "order by updated_at desc limit 60", (c["product"],))
    closed = [{"ref": t["source_ref"], "title": t["title"], "closed_at": t["updated_at"].date().isoformat()} for t in cur.fetchall()]
    return {"conversation_id": c["id"], "product": c["product"], "wanted": c["wanted"] or {}, "recently_closed": closed,
            "messages": [{k: m.get(k) for k in ("author", "role", "kind", "body", "at", "options")} for m in msgs if m.get("kind") != "ack"],
            "operations": operations.catalog(),   # 振り分け（ADR 0039 の決定 2）。決まった操作で済む依頼は operate に
            "questions_asked": questions_asked(msgs), "force_draft": False, "tickets": tickets,
            "previous_draft": ({k: d["payload"].get(k) for k in ("request", "tasks", "unresolved", "overlaps")} if d else None),
            # 依頼者ごとの好み（ADR 0040 の決定 6）。窓口と振り分けの業務に効く、依頼者が自分で言ったものだけ
            "requester": c.get("opened_by"),
            "requester_preferences": requester_prefs.for_job(cur, c["product"], c.get("opened_by"), CONSULT_WORK)}


def enqueue(cur, model_tier: str, repos: list[str]) -> list[int]:
    """相談役のジョブを積む。製品が工場の対象でない・費用の上限に達した会話は積まずに、会話に知らせる"""
    out = []
    for c in pending(cur):
        corr = desk.corr(c["id"])
        if c["product"] not in repos:
            if _event(cur, "desk.consult_skipped", corr, {"conversation_id": c["id"], "reason": "not_a_factory_repo"},
                      f"desk.consult_skipped:{c['id']}:{c['last_msg']}"):
                desk.factory_message(cur, c["id"], "factory", f"{c['product']} はこの工場が受け持つ製品ではありません。運用主に確かめてください。",
                                     kind="error", state="waiting_requester")
            continue
        if float(c["cost_usd"] or 0) >= MAX_USD:
            if _event(cur, "desk.consult_skipped", corr, {"conversation_id": c["id"], "reason": "cost_limit", "cost_usd": float(c["cost_usd"])},
                      f"desk.consult_skipped:{c['id']}:{c['last_msg']}"):
                has_draft = desk.latest_draft(cur, c["id"]) is not None
                desk.factory_message(cur, c["id"], "factory",
                                     f"この相談の費用が上限（${MAX_USD:.2f}）に達したので、これ以上は相談役を動かしません。"
                                     + ("最新の下書きで確認するか、" if has_draft else "") + "閉じて、依頼を絞って開き直してください。",
                                     kind="limit", state="drafted" if has_draft else "waiting_requester")
            continue
        inp = consult_input(cur, c)
        try:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind) "
                        "values (%s,%s,%s,1,'desk',%s,%s) returning id", (corr, c["product"], -c["id"], model_tier, KIND))
        except psycopg.errors.UniqueViolation:
            continue
        jid = cur.fetchone()["id"]
        _event(cur, "job.enqueued", corr, {"by": "desk", "kind": KIND, "repo": c["product"], "conversation_id": c["id"], "attempt": 1,
                                           "input": inp}, job_id=jid)
        out.append(jid)
    return out


# ------------------------------------------------------------------ 相談役の結果を会話に書く
def _add_cost(cur, cid: int, cost) -> None:
    try:
        v = float(cost or 0)
    except (TypeError, ValueError):
        v = 0.0
    if v:
        cur.execute("update desk_conversations set cost_usd = cost_usd + %s where id=%s", (v, cid))


def question_text(q: dict, round_: int, max_rounds: int) -> str:
    lines = [f"質問（{round_}/{max_rounds}）: {q.get('question')}"]
    if q.get("why"):
        lines.append(f"（{q['why']}）")
    rec = q.get("recommended", -1)
    for j, o in enumerate(q.get("options") or []):
        lines.append(f"  {chr(ord('a') + j)}. {o}{'（推奨）' if j == rec else ''}")
    lines.append("返事は言葉でも、選択肢の記号でもかまいません。")
    return "\n".join(lines)


def draft_text(draft_id: int, d: dict) -> str:
    r, est = d.get("request") or {}, d.get("estimate") or {}
    lines = [f"下書き {draft_id}（{'確定' if d.get('status') == 'clarified' else '一部未確定'}）",
             f"- 何を: {r.get('what') or ''}", f"- 何のために: {r.get('why') or ''}",
             f"- いつまでに: {r.get('by_when') or ''}", f"- どの精度で: {r.get('precision') or ''}", "", "タスク（依存の順）:"]
    for t in d.get("tasks") or []:
        dep = f" ← {', '.join(t.get('depends_on') or [])}" if t.get("depends_on") else ""
        lines.append(f"- {t['key']} [{KIND_JA.get(t.get('kind'), t.get('kind'))}・{EFFORT_JA.get(t.get('effort'), t.get('effort'))}] {t.get('title')}{dep}")
    if d.get("overlaps"):
        lines += ["", "既存のチケットとの重なり:"] + [f"- {o['ref']}: {o.get('how') or ''}" for o in d["overlaps"]]
    if d.get("unresolved"):
        lines += ["", "未確定:"] + [f"- {u}" for u in d["unresolved"]]
    if est:
        lines += ["", f"見込み（優先度 {est.get('level')}）: 前に並ぶチケット {est.get('ahead')} 件"
                      + (f"、直近 7 日の出来高は 1 日 {est['per_day']} 件で、着手まで約 {est['start_in_days']} 日" if est.get("per_day") else "")
                      + (f"。費用は 1 件あたり約 ${est['cost_per_task_usd']:.2f}、合計約 ${est['cost_usd']:.2f}" if est.get("cost_per_task_usd") else "")]
        if est.get("pushed_back"):
            lines.append("後ろに回るチケット: " + "、".join(f"{p['ref']}（{p['level']}）" for p in est["pushed_back"]))
    lines += ["", f"よければ確認してください（confirm、draft_id={draft_id}。優先度も決め直せます）。直したいところは返事で言ってもらえれば出し直します。"]
    return "\n".join(lines)


def estimate(cur, n_tasks: int, level: str) -> dict:
    """見込み: 前に並ぶチケット、押し出されるチケット、出来高からの着手までの日数、費用。記録だけから決定論で出す（判断はしない）"""
    level = level if level in ops.LEVELS else "p2"
    pv = ops.LEVELS[level][0]
    cur.execute("select count(*) as n from tickets where state in ('planned','active') and not held and priority >= %s", (pv,))
    ahead = cur.fetchone()["n"]
    cur.execute("select source_ref, title, priority from tickets where state='planned' and not held and priority < %s "
                "order by priority desc, created_at limit 5", (pv,))
    pushed = [{"ref": t["source_ref"], "title": t["title"], "level": ops.level_of(t["priority"])} for t in cur.fetchall()]
    cur.execute("select count(*) as n from events where name='pr.merged' and occurred_at > now() - interval '7 days'")
    per_day = round(cur.fetchone()["n"] / 7, 1)
    cur.execute("select correlation_id, sum(coalesce((result->>'cost_usd')::numeric, 0)) as usd from jobs "
                "where correlation_id like 'github:%%' and finished_at > now() - interval '14 days' group by correlation_id "
                "having bool_or(kind='implement' and state='succeeded')")
    costs = [float(r["usd"]) for r in cur.fetchall() if r["usd"]]
    per_task = round(statistics.median(costs), 2) if costs else None
    return {"level": level, "ahead": ahead, "pushed_back": pushed, "per_day": per_day or None,
            "start_in_days": round(ahead / per_day, 1) if per_day else None,
            "cost_per_task_usd": per_task, "cost_usd": round(per_task * n_tasks, 2) if per_task else None}


def record(cur, job: dict, result: dict | None, state: str, err: str | None) -> None:
    """相談役のジョブの結果を会話に書く。問いは waiting_requester、下書きは drafted、失敗は知らせて依頼者の言い足しを待つ"""
    cid = -int(job["issue_number"])
    corr = desk.corr(cid)
    res = result or {}
    _add_cost(cur, cid, res.get("cost_usd"))
    if state != "succeeded" or not res.get("ok"):
        if state == "cancelled":
            return
        desk.factory_message(cur, cid, "factory", f"相談役のジョブ {job['id']} が失敗しました（{err or res.get('exit')}）。"
                             "運用主が再実行するか、言い足しをもらえればもう一度動かします。", kind="error", state="waiting_requester",
                             payload={"job_id": job["id"]})
        return
    if res.get("outcome") == "question":
        q = res.get("question") or {}
        desk.factory_message(cur, cid, "planner", question_text(q, res.get("round") or 1, res.get("max_rounds") or MAX_ROUNDS),
                             kind="question", state="waiting_requester",
                             payload={"job_id": job["id"], "round": res.get("round"), "question": q.get("question"), "why": q.get("why"),
                                      "options": q.get("options") or [], "recommended": q.get("recommended", -1),
                                      "summary": res.get("summary") or ""})
        return
    if res.get("outcome") == "route":
        route(cur, cid, job, res)
        return
    if res.get("outcome") != "draft":
        desk.factory_message(cur, cid, "factory", "下書きを作れませんでした（" + "、".join((res.get("errors") or ["形の誤り"])[:5])
                             + "）。依頼を絞るか言い足してもらえれば、もう一度切ります。", kind="error", state="waiting_requester",
                             payload={"job_id": job["id"], "errors": res.get("errors") or []})
        return
    cur.execute("select * from desk_conversations where id=%s", (cid,))
    c = cur.fetchone()
    est = estimate(cur, len(res.get("tasks") or []), (c["wanted"] or {}).get("level") or "p2")
    payload = {k: res.get(k) for k in ("status", "request", "unresolved", "tasks", "overlaps", "fixes", "summary", "evidence",
                                       "interpretation", "confidence", "versions", "questions_asked", "model", "cost_usd")}
    payload.update({"product": c["product"], "estimate": est, "job_id": job["id"]})
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values (%s,%s,%s,'desk',%s) returning id",
                (desk.DRAFT_TYPE, corr, job["id"], Jsonb(payload)))
    draft_id = cur.fetchone()["id"]
    req = res.get("request") or {}
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Request',%s,%s,'desk',%s)",
                (corr, job["id"], Jsonb({"requester": c["opened_by"], "raw_text": c["title"], "status": res.get("status"), "draft_id": draft_id,
                                        "rounds": res.get("questions_asked"), **{k: req.get(k) for k in ("what", "why", "by_when", "precision")}})))
    _event(cur, "desk.drafted", corr, {"conversation_id": cid, "draft_id": draft_id, "status": res.get("status"), "tasks": len(payload["tasks"] or []),
                                       "unresolved": len(payload["unresolved"] or []), "questions_asked": res.get("questions_asked"),
                                       "overlaps": len(payload["overlaps"] or []), "cost_usd": res.get("cost_usd")}, job_id=job["id"])
    desk.factory_message(cur, cid, "planner", draft_text(draft_id, payload), kind="draft", state="drafted",
                         payload={"job_id": job["id"], "draft_id": draft_id, "status": res.get("status"),
                                  "tasks": [{k: t.get(k) for k in ("key", "title", "kind", "effort", "depends_on")} for t in payload["tasks"] or []],
                                  "unresolved": payload["unresolved"], "overlaps": payload["overlaps"], "estimate": est})


# ------------------------------------------------------------------ 確認した下書きを起票する
def task_sections(t: dict, deps: list[int]) -> list[str]:
    """タスク 1 件の本文の節（先に読むもの・やること・やらないこと・触るファイル・受け入れ条件・関係・依存）。
    窓口の下書きと、プランナーが切ったタスクのまとまり（ADR 0036）が同じ形で書く。依存の行は advance_waiting が読む"""
    def items(xs):
        return [f"- {x}" for x in xs or [] if isinstance(x, str) and x.strip()]
    lines = []
    for head, key in (("先に読むもの", "read_first"), ("やること", "do"), ("やらないこと", "dont"), ("触るファイルの見込み", "expected_files"),
                      ("受け入れ条件", "acceptance")):
        if items(t.get(key)):
            lines += ["", f"## {head}"] + items(t.get(key))
    if t.get("related"):
        lines += ["", "## 関係する Issue", "- " + " ".join(t["related"])]
    if deps:
        lines += ["", "依存: " + " ".join(f"#{n}" for n in deps)]
    return lines


def file_task(cur, gh, repo: str, title: str, body: str, deps: list[int], labels: list[str]) -> dict:
    """タスク 1 件を製品の Issue に起票し、チケット（依存と待ち）を作る。依存があれば待ちのラベル、なければプランナーの入口。
    返り値は {number, url, label, ticket}。窓口の起票とプランナーの切り分け（ADR 0036）が使う"""
    label = WAITING_LABEL if deps else TRIGGER_LABEL
    it = gh("POST", f"repos/{repo}/issues", {"title": title[:200], "body": body, "labels": [label] + list(labels)}) or {}
    if not it.get("number"):
        raise RuntimeError(f"Issue を作れなかった（{title[:40]}）")
    tk = ops.upsert_ticket(cur, "github", f"{repo}#{it['number']}", repo, title=title[:200], url=it.get("html_url"),
                           state="active", depends_on=[f"{repo}#{x}" for x in deps], waiting_on=[f"{repo}#{x}" for x in deps])
    return {"number": int(it["number"]), "url": it.get("html_url"), "label": label, "ticket": tk}


def issue_body(cid: int, draft_id: int, req: dict, t: dict, deps: list[int], who: str) -> str:
    lines = [f"タスク（{KIND_JA.get(t.get('kind'), t.get('kind'))}）。窓口の依頼（相談 {cid}、依頼者 {who}）から切ったタスクのまとまりの {t['key']}。"
             f"{t.get('summary') or ''}", "", "## 依頼（確定した Request）",
             f"- 何を: {req.get('what') or ''}", f"- 何のために: {req.get('why') or ''}",
             f"- いつまでに: {req.get('by_when') or ''}", f"- どの精度で: {req.get('precision') or ''}"]
    lines += task_sections(t, deps)
    lines += ["", f"<sub>窓口の下書き {draft_id}・手間の見込み {EFFORT_JA.get(t.get('effort'), t.get('effort'))}</sub>",
              MARKER.format(cid=cid, draft=draft_id, key=t["key"])]
    return "\n".join(lines)


def confirmed(cur) -> list[dict]:
    """確認されたが、まだ起票し終えていない会話（確認の記録と、その下書き）"""
    cur.execute("""
      select c.*, e.payload as confirm from desk_conversations c
        join lateral (select payload from events e where e.correlation_id = 'desk:' || c.id and e.name = 'desk.request_confirmed'
                       order by id desc limit 1) e on true
       where c.kind = 'request' and c.state = 'confirmed'
         and not exists (select 1 from events t where t.correlation_id = 'desk:' || c.id and t.name = 'desk.tickets_created'
                          and t.payload->>'draft_id' = e.payload->>'draft_id')
       order by c.updated_at""")
    return cur.fetchall()


def file_confirmed(cur, gh, repos: list[str]) -> int:
    """確認された下書きを、製品の Issue として依存の順に起票し、チケットに会話と依頼者の優先度を付ける。
    途中で失敗しても、起票済みのタスクは desk.ticket_filed で覚えているので、次の周回で残りから続ける"""
    n = 0
    for c in confirmed(cur):
        cid, conf = c["id"], c["confirm"]
        corr, draft_id, level = desk.corr(cid), int(conf["draft_id"]), conf.get("level") or "p2"
        cur.execute("select payload from artifacts where id=%s and type=%s and correlation_id=%s", (draft_id, desk.DRAFT_TYPE, corr))
        row = cur.fetchone()
        if not row or c["product"] not in repos:
            if _event(cur, "desk.file_failed", corr, {"conversation_id": cid, "draft_id": draft_id, "reason": "no_draft" if not row else "not_a_factory_repo"},
                      f"desk.file_failed:{draft_id}"):
                desk.factory_message(cur, cid, "factory", "下書きからチケットを作れませんでした（下書きがない・工場が受け持つ製品ではない）。運用主に確かめてください。",
                                     kind="error")
            continue
        d, repo, by = row["payload"], c["product"], f"desk:{c['opened_by']}"
        numbers: dict[str, int] = {}
        cur.execute("select payload from events where name='desk.ticket_filed' and correlation_id=%s and payload->>'draft_id'=%s",
                    (corr, str(draft_id)))
        for e in cur.fetchall():
            numbers[e["payload"]["key"]] = int(e["payload"]["number"])
        filed = []
        try:
            for t in d.get("tasks") or []:
                deps = [numbers[k] for k in t.get("depends_on") or [] if k in numbers]
                if t["key"] not in numbers:
                    it = file_task(cur, gh, repo, t["title"], issue_body(cid, draft_id, d.get("request") or {}, t, deps, c["opened_by"]), deps,
                                   [f"{ops.PRIORITY_LABEL_PREFIX}{level}"])
                    numbers[t["key"]] = it["number"]
                    _event(cur, "desk.ticket_filed", corr, {"conversation_id": cid, "draft_id": draft_id, "key": t["key"], "number": it["number"],
                                                            "url": it["url"], "label": it["label"]}, f"desk.ticket_filed:{draft_id}:{t['key']}")
                    tk = it["ticket"]
                    cur.execute("update tickets set desk_conversation_id=%s where id=%s", (cid, tk["id"]))
                    ops.set_priority(cur, tk["id"], ops.LEVELS[level][0], by, f"窓口の依頼（相談 {cid}）")
                filed.append({"key": t["key"], "ref": f"{repo}#{numbers[t['key']]}", "number": numbers[t["key"]], "title": t["title"],
                              "depends_on": [f"#{x}" for x in deps]})
        except Exception as e:  # noqa: BLE001  起票済みの分は覚えている。次の周回で続ける
            if _event(cur, "desk.file_failed", corr, {"conversation_id": cid, "draft_id": draft_id, "error": repr(e)[:300], "filed": len(numbers)},
                      f"desk.file_failed:{draft_id}:{len(numbers)}"):
                desk.factory_message(cur, cid, "factory", f"チケットの起票が途中で止まりました（{len(numbers)} 件まで）。次の周回で続けます。", kind="error")
            continue
        cur.execute("insert into events(name, correlation_id, payload) values ('desk.tickets_created',%s,%s)",
                    (corr, Jsonb({"conversation_id": cid, "draft_id": draft_id, "level": level, "by": c["opened_by"], "tickets": filed})))
        lines = [f"下書き {draft_id} から {len(filed)} 件のチケットを作りました（優先度 {level}）。依存のないものからプランナーの着手判定に回ります。"]
        lines += [f"- {f['ref']} {f['title']}" + (f"（{' '.join(f['depends_on'])} の後）" if f["depends_on"] else "") for f in filed]
        lines.append("進み具合はチケットの状態（tickets）で見られます。")
        desk.factory_message(cur, cid, "factory", "\n".join(lines), kind="tickets", payload={"draft_id": draft_id, "tickets": filed})
        # チケット化は起票した時点で製品のラインに渡し、会話はここで終える（ADR 0039 の決定 1）
        _event(cur, "desk.handed_off", corr, {"conversation_id": cid, "draft_id": draft_id, "tickets": [f["ref"] for f in filed]})
        desk.finish(cur, cid, "handed_off", "factory", path="ticket")
        n += 1
    return n


def advance_waiting(cur, gh, swap_label, comment, repos: list[str]) -> int:
    """factory:desk-waiting のタスクで、依存（本文の「依存: #12 #13」）がすべて閉じたものをプランナーの着手判定に回す"""
    n = 0
    for repo in repos:
        for it in gh("GET", f"repos/{repo}/issues?state=open&labels={urllib.parse.quote(WAITING_LABEL)}&per_page=50") or []:
            if "pull_request" in it:
                continue
            deps = [int(x) for m in DEP_RE.finditer(it.get("body") or "") for x in re.findall(r"#(\d+)", m.group(1))]
            if any((gh("GET", f"repos/{repo}/issues/{d}") or {}).get("state") != "closed" for d in deps):
                continue
            corr = f"github:{repo}#{it['number']}"
            if not _event(cur, "desk.task_released", corr, {"repo": repo, "issue": it["number"], "after": deps},
                          f"desk.task_released:{repo}#{it['number']}"):
                continue
            swap_label(repo, it["number"], [WAITING_LABEL], [TRIGGER_LABEL])
            comment(repo, it["number"], f"依存（{' '.join(f'#{d}' for d in deps)}）が閉じたので、プランナーの着手判定に回しました。")
            cur.execute("update tickets set waiting_on='{}', updated_at=now() where source='github' and source_ref=%s", (f"{repo}#{it['number']}",))
            n += 1
    return n


# ------------------------------------------------------------------ GitHub の Issue の依頼を窓口の会話に持ち込む（factoryctl desk from-issue）
def open_from_issue(cur, gh, who: dict, repo: str, number: int) -> dict:
    """clarify で確定した Issue（factory:clarified）などを、窓口の依頼として開き直す。確定した Request があれば添える。
    以後は窓口の会話の往復と下書き・確認で進む（もとの Issue にはリンクのコメントだけ書く）"""
    it = gh("GET", f"repos/{repo}/issues/{number}") or {}
    if not it.get("title"):
        raise desk.DeskError(404, f"{repo}#{number} が読めない")
    cur.execute("select payload from artifacts where type='Request' and correlation_id=%s and produced_by='clarify' order by id desc limit 1",
                (f"github:{repo}#{number}",))
    r = (cur.fetchone() or {}).get("payload") or {}
    body = [it["title"], "", (it.get("body") or "").strip()[:4000], "", f"（{repo}#{number} から持ち込んだ依頼）"]
    if r.get("status") == "clarified":
        body += ["", "窓口（clarify）で確定した内容:", f"- 何を: {r.get('what') or ''}", f"- 何のために: {r.get('why') or ''}",
                 f"- いつまでに: {r.get('by_when') or ''}", f"- どの精度で: {r.get('precision') or ''}"]
    c = desk.open_conversation(cur, who, "request", repo, "\n".join(body)[: desk.MAX_BODY], source_ref=it.get("html_url") or None)
    _event(cur, "desk.imported", desk.corr(c["id"]), {"conversation_id": c["id"], "repo": repo, "issue": number, "by": who["name"],
                                                      "clarified": r.get("status") == "clarified"})
    return c


# ------------------------------------------------------------------ 単発の依頼のループ（ADR 0039 の順番 2。operate の 1 本）
#   受付（desk.open_conversation がすぐ返す）→ 振り分け（相談役が route を返す → route()）
#   → 確認（send の段は受信トレイの件 desk_confirm。read の段は飛ばす）→ 実行（run_operations。dispatcher が操作を 1 回動かす）
#   → 提出（会話に結果を書き、受信トレイの件 desk_receipt で受取確認を待つ）→ 依頼者の答え（answer()）
MAX_FOLLOW_UPS = 5     # 続きの上限（決定 5。仮置き）。超えたら新しい会話にしてもらう
CONFIRM_KIND, RECEIPT_KIND = "desk_confirm", "desk_receipt"
CONFIRM_OPTIONS = [{"id": "run", "label": "実行する"}, {"id": "cancel", "label": "やめる"}]
RECEIPT_OPTIONS = [{"id": "accept", "label": "受け取った"}, {"id": "more", "label": "続きを頼む", "note_required": True},
                   {"id": "return", "label": "差し戻す（頼んだことと違う）", "note_required": True}]
TIER_JA = {"read": "読むだけ（確認なしで動かす）", "send": "工場の外に出す（依頼者の確認 1 回）", "change": "状態を変える（承認ボタン）"}


def _round(cur, cid: int) -> int:
    """振り分けの回（続き・差し戻しで振り分け直すたびに 1 つ増える）。受信トレイの件の key と、実行の重複よけに使う"""
    cur.execute("select count(*) as n from events where correlation_id=%s and name='desk.routed'", (desk.corr(cid),))
    return cur.fetchone()["n"]


def last_route(cur, cid: int) -> dict | None:
    cur.execute("select id, payload from events where correlation_id=%s and name='desk.routed' order by id desc limit 1", (desk.corr(cid),))
    r = cur.fetchone()
    return {"event_id": r["id"], **r["payload"]} if r else None


def route(cur, cid: int, job: dict, res: dict) -> None:
    """相談役の振り分けを検査して記録する（決定 2）。宣言にない操作・引数なら振り分けず、依頼者に言い足しを頼む。
    振り分けの結果は確認を飛ばすときも必ず会話に書く"""
    import notify
    r = res.get("route") or {}
    cur.execute("select state from desk_conversations where id=%s", (cid,))
    c = cur.fetchone()
    if not c or c["state"] not in ("open", "waiting_requester"):
        return   # 相談役が動いている間に取り下げられた・終わった会話は振り分けない
    cfg = notify.cached(cur)
    params, errors = operations.check(r.get("op"), r.get("params"), destinations=sorted(cfg.destinations), role="requester")
    if errors:
        desk.factory_message(cur, cid, "factory", "決まった操作に振り分けようとしましたが、宣言と合いませんでした（" + "、".join(errors[:5])
                             + "）。依頼の言葉を足してもらえれば、もう一度振り分けます。", kind="error", state="waiting_requester",
                             payload={"job_id": job["id"], "errors": errors, "route": r})
        return
    op, tier = r["op"], operations.tier(r["op"])
    pv = operations.preview(op, params, cfg)
    n = _round(cur, cid) + 1
    payload = {"conversation_id": cid, "path": "operate", "op": op, "params": params, "tier": tier, "why": r.get("why") or "",
               "preview": pv, "round": n, "job_id": job["id"], "confidence": res.get("confidence"),
               "request": res.get("request") or {}, "workflow": {"path": "operate", "steps": ["confirm", "execute", "respond"]}}
    desk.advance(cur, cid, "routed", "desk.routed", payload, path="operate")
    lines = [f"振り分けました: 決まった操作（{op}。{operations.OPS[op]['ja']}）で済みます。", f"- やること: {pv['text']}",
             f"- 確認の段: {TIER_JA.get(tier, tier)}"]
    if r.get("why"):
        lines.append(f"- 理由: {r['why']}")
    if tier == "read":
        desk.factory_message(cur, cid, "planner", "\n".join(lines + ["", "すぐに動かします。"]), kind="route", payload={"round": n})
        desk.advance(cur, cid, "running", "desk.confirmed_run", {"conversation_id": cid, "round": n, "skipped": "read"})
        return
    key = f"{CONFIRM_KIND}:{cid}:{n}"
    lines += ["", "よければ「実行する」、やめるなら「やめる」で答えてください（窓口の confirm、受信トレイのどちらからでも）。"]
    desk.factory_message(cur, cid, "planner", "\n".join(lines), kind="route", payload={"round": n, "inbox_key": f"task:{key}"})
    desk.advance(cur, cid, "awaiting_confirm", "desk.confirm_requested", {"conversation_id": cid, "round": n, "inbox_key": f"task:{key}"})
    inbox.request(cur, key, CONFIRM_KIND, "requester", f"窓口の依頼 {cid}: {pv['text']}"[:300],
                  action="実行してよいか答える（実行する・やめる）", correlation_id=desk.corr(cid), by="desk")


def running_operations(cur) -> list[dict]:
    cur.execute("select * from desk_conversations where state='running' and path='operate' order by updated_at")
    return cur.fetchall()


def run_operations(cur, config=None, poster=None) -> int:
    """確認の済んだ操作を 1 回ずつ動かして提出する（決定 2 の 4・5）。同じ回は 2 度動かさない（落ちても送り直さない。送れたかは結果に書く）"""
    n = 0
    for c in running_operations(cur):
        cid, corr = c["id"], desk.corr(c["id"])
        rt = last_route(cur, cid)
        if not rt or rt.get("op") not in operations.OPS:
            continue
        if not _event(cur, "op.started", corr, {"conversation_id": cid, "op": rt["op"], "params": rt["params"], "round": rt["round"]},
                      f"op.started:{cid}:{rt['round']}"):
            continue
        try:
            out = operations.run(cur, rt["op"], rt["params"], config=config, poster=poster)
        except Exception as e:  # noqa: BLE001  操作の失敗も結果として提出する（依頼者が差し戻せる）
            out = {"ok": False, "summary": f"操作が途中で失敗しました（{repr(e)[:200]}）", "detail": {}}
        _event(cur, "op.executed", corr, {"conversation_id": cid, "op": rt["op"], "params": rt["params"], "round": rt["round"],
                                         "ok": out["ok"], "summary": out["summary"]})
        deliver(cur, cid, rt["round"], out)
        n += 1
    return n


def deliver(cur, cid: int, round_: int, out: dict) -> None:
    """提出（決定 5）。会話に結果と所見の要約を書き、受取確認の件を受信トレイに積む。返事がなくても閉じない"""
    key = f"{RECEIPT_KIND}:{cid}:{round_}"
    head = "できました。" if out["ok"] else "やってみましたが、思ったとおりにはいきませんでした。"
    desk.factory_message(cur, cid, "factory", "\n".join([head + out["summary"], "",
                                                         "受け取ったら「受け取った」、追加があれば「続きを頼む」、頼んだことと違えば「差し戻す」で答えてください"
                                                         "（窓口の返事に answer を付けるか、受信トレイから）。"]),
                         kind="delivered", payload={"round": round_, "ok": out["ok"], "inbox_key": f"task:{key}"})
    desk.advance(cur, cid, "delivered", "desk.delivered", {"conversation_id": cid, "round": round_, "ok": out["ok"], "summary": out["summary"],
                                                          "inbox_key": f"task:{key}"})
    inbox.request(cur, key, RECEIPT_KIND, "requester", f"窓口の依頼 {cid} を提出: {out['summary']}"[:300],
                  action="受け取ったか答える（受け取った・続きを頼む・差し戻す）", correlation_id=desk.corr(cid), by="desk")


KEY_RE = re.compile(rf"^({CONFIRM_KIND}|{RECEIPT_KIND}):(\d+):(\d+)$")


def answer(cur, key: str, option: str, by: str, via: str, note: str = "") -> tuple[bool, str]:
    """受信トレイの件（desk_confirm・desk_receipt）への答え。inbox.answer の作用から呼ばれる（どの入口から来た答えもここを通る）"""
    m = KEY_RE.match(key or "")
    if not m:
        return False, f"窓口の件ではない: {key}"
    kind, cid, round_ = m.group(1), int(m.group(2)), int(m.group(3))
    cur.execute("select * from desk_conversations where id=%s for update", (cid,))
    c = cur.fetchone()
    if not c:
        return False, "そのような相談はない"
    corr = desk.corr(cid)
    if kind == CONFIRM_KIND:
        if c["state"] != "awaiting_confirm" or _round(cur, cid) != round_:
            return False, f"いまは確認を受けられない（状態 {c['state']}）"
        inbox.resolve(cur, key, by, option)
        if option == "run":
            desk.advance(cur, cid, "running", "desk.confirmed_run", {"conversation_id": cid, "round": round_, "by": by, "via": via})
            return True, "実行に進めた（次の周回で動かす）"
        _event(cur, "op.rejected", corr, {"conversation_id": cid, "round": round_, "by": by, "via": via, "note": note})
        desk.finish(cur, cid, "withdrawn", by, note=note or "確認でやめた")
        return True, "やめた（依頼は取り下げとして閉じた）"
    if c["state"] != "delivered" or _round(cur, cid) != round_:
        return False, f"いまは受取確認を受けられない（状態 {c['state']}）"
    if option == "more" and (c["follow_ups"] or 0) >= MAX_FOLLOW_UPS:
        return False, f"続きは {MAX_FOLLOW_UPS} 回まで。新しい相談として開いてください"
    inbox.resolve(cur, key, by, option)
    _event(cur, "desk.received", corr, {"conversation_id": cid, "round": round_, "answer": option, "by": by, "via": via, "note": note})
    if option == "accept":
        desk.finish(cur, cid, "accepted", by, note=note)
        return True, "受け取った（依頼を完了にした）"
    # 続き・差し戻しは同じ会話で振り分けから（決定 5）。依頼者の一言を発言として残し、相談役に回す
    if option == "more":
        cur.execute("update desk_conversations set follow_ups = follow_ups + 1 where id=%s", (cid,))
    desk.advance(cur, cid, "open", "desk.rerouted", {"conversation_id": cid, "round": round_, "answer": option})
    _event(cur, "desk.message", corr, {"conversation_id": cid, "author": c["opened_by"], "role": "requester",
                                       "kind": "follow_up" if option == "more" else "returned", "body": note})
    return True, "続きとして振り分け直す" if option == "more" else "差し戻しとして振り分け直す"
