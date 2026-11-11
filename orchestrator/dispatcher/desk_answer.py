"""窓口の問い合わせに答える Answer ライン（ADR 0030 の順番 4、ADR 0043 案の O1 の最小形）。dispatcher の側。

  問い合わせを開く・返事をする（desk_api）→ ここで回答役のジョブ（desk_answer、窓口の枠）を積む
  → core-gates/desk_answer.py が 問い返し（1 回まで）か 回答 を返す → ここで会話に書く（問い返し: waiting_requester、回答: answered）
  → 依頼者が続けて聞けば（answered → open）、もう一度回答役を動かす

回答役は読むだけで、工場を変える操作は持たない。ここも会話に書く以外のことはしない。
材料は制御プレーンの記録（チケット・ジョブ・停止・ラインの宣言・日報）だけで組み立て、記録ごとに参照の id（ticket:… job:… など）を付けて渡す。
答えの根拠はこの id か、ワーカーが写した文書のパスで返させ、知らない参照は落とす（core-gates/desk_answer.py）。
立場は計画（planner）と工場長（factory_head）の 2 つだけ（ADR 0030 の W4・順番 4。ほかは P5a・P5b の記録ができてから足す）。
"""
from __future__ import annotations

import json
import os
import re

import psycopg

import desk
import line
import ops
from desk_line import MAX_USD, _add_cost, _event, messages, question_text

KIND = "desk_answer"
MAX_ROUNDS = 1           # 問い返しは 1 回まで（ADR 0043 案の O1）。使い切ったら、分かる範囲で答えて分からないことを書く
STANCES = {"planner": "計画", "factory_head": "工場長"}
REF_IN_TEXT = re.compile(r"(?:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(?P<n>\d{1,6})\b")
MAX_REFERENCED = 10
MAX_REPORT_CHARS = int(os.environ.get("DESK_ANSWER_REPORT_CHARS", "20000"))


def pending(cur) -> list[dict]:
    """回答役に回す会話: 問い合わせで、open（依頼者が書いた後）で、ジョブが動いておらず、最後の依頼者の発言が最後に積んだジョブより新しい"""
    cur.execute("""
      select c.*, m.last_msg from desk_conversations c
        join lateral (select max(e.id) as last_msg from events e where e.correlation_id = 'desk:' || c.id
                       and e.name = 'desk.message' and e.payload->>'role' = 'requester') m on true
       where c.kind = 'inquiry' and c.state = 'open'
         and not exists (select 1 from jobs j where j.correlation_id = 'desk:' || c.id and j.state in ('queued','running'))
         and m.last_msg > coalesce((select max(e.id) from events e where e.correlation_id = 'desk:' || c.id
                                     and e.name = 'job.enqueued'), 0)
       order by c.updated_at""")
    return cur.fetchall()


def questions_asked(msgs: list[dict]) -> int:
    return sum(1 for m in msgs if m.get("role") == "answer" and m.get("kind") == "question")


def _iso(t) -> str | None:
    return t.isoformat() if t else None


def referenced(msgs: list[dict], product: str) -> list[str]:
    """依頼者の言葉に出てくるチケットの参照（#123・owner/repo#123）。製品の外のリポジトリは見ない"""
    out: list[str] = []
    for m in msgs:
        if m.get("role") != "requester":
            continue
        for x in REF_IN_TEXT.finditer(m.get("body") or ""):
            repo = x.group("repo") or product
            ref = f"{repo}#{int(x.group('n'))}"
            if repo == product and ref not in out:
                out.append(ref)
    return out[:MAX_REFERENCED]


def ticket_record(t: dict) -> dict:
    return {"id": f"ticket:{t['source_ref']}", "ref": t["source_ref"], "title": t["title"], "state": t["state"],
            "level": ops.level_of(t["priority"]), "held": t["held"], "waiting_on": t["waiting_on"],
            "updated_at": _iso(t["updated_at"])}


def ticket_detail(cur, ref: str) -> dict:
    """1 件のチケットの経緯（チケット・ジョブ・出来事・開いている停止）。「#xxx はなぜ止まっている」の材料"""
    corr = f"github:{ref}"
    cur.execute("select * from tickets where source='github' and source_ref=%s", (ref,))
    t = cur.fetchone()
    cur.execute("select id, kind, state, attempt, enqueued_at, finished_at, error, result->>'cost_usd' as cost_usd from jobs "
                "where correlation_id=%s order by id desc limit 15", (corr,))
    jobs = [{"id": f"job:{j['id']}", "kind": j["kind"], "state": j["state"], "attempt": j["attempt"], "enqueued_at": _iso(j["enqueued_at"]),
             "finished_at": _iso(j["finished_at"]), "error": (j["error"] or "")[:200], "cost_usd": j["cost_usd"]} for j in cur.fetchall()]
    cur.execute("select id, name, occurred_at, payload from events where correlation_id=%s order by id desc limit 30", (corr,))
    evs = []
    for e in cur.fetchall():
        p = e["payload"] or {}
        brief = {k: p[k] for k in ("state", "error", "reason", "route", "label", "level", "verdict", "pr_url", "what", "waiting_for", "key")
                 if k in p and isinstance(p[k], (str, int, float, bool))}
        evs.append({"id": f"event:{e['id']}", "name": e["name"], "at": _iso(e["occurred_at"]), **brief})
    cur.execute("select key, category, what, waiting_for, started_at from stoppages where ended_at is null and correlation_id=%s", (corr,))
    stops = [{"id": f"stoppage:{s['key']}", "category": s["category"], "what": s["what"], "waiting_for": s["waiting_for"],
              "started_at": _iso(s["started_at"])} for s in cur.fetchall()]
    return {"ref": ref, "ticket": ticket_record(t) if t else None, "jobs": jobs, "events": list(reversed(evs)), "open_stoppages": stops}


def roadmap_progress(cur) -> dict | None:
    """工場の建設のロードマップの各項目の現在値（最新の建設日報 report.daily が measure を評価した値）。#321:
    定義（factory/roadmap.yaml）だけでは「実測値がない」と答え、項目の数も数え違えたので、数と値はここで渡す。日報は 1 日 1 回なので as_of を添える"""
    cur.execute("select id, occurred_at, payload from events where name='report.daily' order by occurred_at desc limit 1")
    r = cur.fetchone()
    if not r:
        return None
    p = r["payload"] or {}
    phases = [{"id": ph.get("id"), "ja": ph.get("ja"), "progress": ph.get("progress"), "item_count": ph.get("item_count"),
               "items": ph.get("items")} for ph in p.get("phases") or []]
    return {"id": f"roadmap:{p.get('date') or r['id']}", "as_of": _iso(r["occurred_at"]), "date": p.get("date"),
            "overall": p.get("overall"), "phases": phases,
            "note": "項目の数は item_count、各項目の現在値は items[].value（日報が measure を評価した値）。items が null の日報は項目の値を残していない"}


def records(cur, product: str, refs: list[str]) -> dict:
    """回答役に渡す工場の記録。どれも参照の id を持つ（答えの根拠に書かせる）"""
    cur.execute("select * from tickets where repo=%s and state in ('planned','active') order by priority desc, created_at limit 120", (product,))
    flowing = [ticket_record(t) for t in cur.fetchall()]
    cur.execute("select * from tickets where repo=%s and state='closed' and updated_at > now() - interval '30 days' "
                "order by updated_at desc limit 60", (product,))
    closed = [ticket_record(t) for t in cur.fetchall()]
    cur.execute("select key, category, what, waiting_for, started_at, correlation_id from stoppages where ended_at is null order by started_at")
    open_stops = [{"id": f"stoppage:{s['key']}", "category": s["category"], "what": s["what"], "waiting_for": s["waiting_for"],
                   "started_at": _iso(s["started_at"]), "correlation_id": s["correlation_id"]} for s in cur.fetchall()]
    cur.execute("select key, category, what, started_at, ended_at, remedy from stoppages where ended_at > now() - interval '7 days' "
                "order by ended_at desc limit 20")
    closed_stops = [{"id": f"stoppage:{s['key']}", "category": s["category"], "what": s["what"], "started_at": _iso(s["started_at"]),
                     "ended_at": _iso(s["ended_at"]), "remedy": s["remedy"]} for s in cur.fetchall()]
    cur.execute("""select date_trunc('day', finished_at) as day, count(*) filter (where state='succeeded') as ok,
                          count(*) filter (where state='failed') as failed,
                          round(sum(coalesce((result->>'cost_usd')::numeric, 0)), 2) as usd
                     from jobs where finished_at > now() - interval '7 days' group by 1 order by 1""")
    jobs_by_day = [{"day": r["day"].date().isoformat(), "succeeded": r["ok"], "failed": r["failed"], "cost_usd": float(r["usd"] or 0)}
                   for r in cur.fetchall()]
    cur.execute("select date_trunc('day', occurred_at) as day, count(*) as n from events where name='pr.merged' "
                "and occurred_at > now() - interval '7 days' group by 1 order by 1")
    merged_by_day = [{"day": r["day"].date().isoformat(), "merged": r["n"]} for r in cur.fetchall()]
    cur.execute("select id, occurred_at, payload from events where name='report.built' order by id desc limit 1")
    rep = cur.fetchone()
    report = None
    if rep:
        body = json.dumps((rep["payload"] or {}).get("report") or {}, ensure_ascii=False)
        report = {"id": f"report:{rep['id']}", "kind": rep["payload"].get("kind"), "start": rep["payload"].get("start"),
                  "built_at": _iso(rep["occurred_at"]), "report_json": body[:MAX_REPORT_CHARS],
                  "truncated": len(body) > MAX_REPORT_CHARS}
    return {"line": {"id": "line:status", **line.status(cur)}, "roadmap": roadmap_progress(cur), "tickets": flowing, "recently_closed": closed,
            "open_stoppages": open_stops, "recent_stoppages": closed_stops,
            "activity_7d": {"id": "activity:7d", "jobs_by_day": jobs_by_day, "merged_by_day": merged_by_day},
            "latest_report": report, "referenced": [ticket_detail(cur, r) for r in refs]}


def answer_input(cur, c: dict) -> dict:
    """回答役に渡す材料（制御プレーンの記録だけで組み立てる）。依頼者の言葉はデータとして渡す"""
    msgs = messages(cur, c["id"])
    asked = questions_asked(msgs)
    return {"conversation_id": c["id"], "product": c["product"], "requester": c.get("opened_by"),
            "messages": [{k: m.get(k) for k in ("author", "role", "kind", "body", "at", "options")} for m in msgs if m.get("kind") != "ack"],
            "questions_asked": asked, "max_rounds": MAX_ROUNDS, "force_answer": asked >= MAX_ROUNDS,
            "stances": STANCES, "records": records(cur, c["product"], referenced(msgs, c["product"]))}


def enqueue(cur, model_tier: str, repos: list[str]) -> list[int]:
    """回答役のジョブを積む。製品が工場の対象でない・費用の上限に達した会話は積まずに、会話に知らせる"""
    out = []
    for c in pending(cur):
        corr = desk.corr(c["id"])
        if c["product"] not in repos:
            if _event(cur, "desk.answer_skipped", corr, {"conversation_id": c["id"], "reason": "not_a_factory_repo"},
                      f"desk.answer_skipped:{c['id']}:{c['last_msg']}"):
                desk.factory_message(cur, c["id"], "factory", f"{c['product']} はこの工場が受け持つ製品ではありません。運用主に確かめてください。",
                                     kind="error", state="waiting_requester")
            continue
        if float(c["cost_usd"] or 0) >= MAX_USD:
            if _event(cur, "desk.answer_skipped", corr, {"conversation_id": c["id"], "reason": "cost_limit", "cost_usd": float(c["cost_usd"])},
                      f"desk.answer_skipped:{c['id']}:{c['last_msg']}"):
                desk.factory_message(cur, c["id"], "factory",
                                     f"この問い合わせの費用が上限（${MAX_USD:.2f}）に達したので、これ以上は回答役を動かしません。"
                                     "続きは新しい問い合わせとして開いてください。", kind="limit", state="waiting_requester")
            continue
        inp = answer_input(cur, c)
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


def answer_text(res: dict) -> str:
    stance = STANCES.get(res.get("stance"), res.get("stance") or "")
    lines = [f"回答（{stance}の立場から）", "", (res.get("answer") or "").strip()]
    ev = res.get("evidence") or []
    if ev:
        lines += ["", "根拠:"] + [f"- {e.get('ref')}: {e.get('what') or ''}" for e in ev]
    unknown = res.get("unknowns") or []
    lines += ["", "分からないこと:"] + ([f"- {u}" for u in unknown] if unknown else ["- なし"])
    if res.get("switch_to_request"):
        lines += ["", "工場に何かをしてほしい話になっているので、進めるなら依頼（kind=request）として開いてください。窓口の相談役が切り方を一緒に決めます。"]
    lines += ["", "続けて聞きたいことがあれば、この会話に返事を書いてください。"]
    return "\n".join(lines)


def record(cur, job: dict, result: dict | None, state: str, err: str | None) -> None:
    """回答役のジョブの結果を会話に書く。問い返しは waiting_requester、回答は answered（desk.answered）。失敗は知らせて言い足しを待つ"""
    cid = -int(job["issue_number"])
    res = result or {}
    _add_cost(cur, cid, res.get("cost_usd"))
    if state != "succeeded" or not res.get("ok"):
        if state == "cancelled":
            return
        desk.factory_message(cur, cid, "factory", f"回答役のジョブ {job['id']} が失敗しました（{err or res.get('exit')}）。"
                             "運用主が再実行するか、言い足しをもらえればもう一度動かします。", kind="error", state="waiting_requester",
                             payload={"job_id": job["id"]})
        return
    if res.get("outcome") == "question":
        q = res.get("question") or {}
        desk.factory_message(cur, cid, "answer", question_text(q, res.get("round") or 1, res.get("max_rounds") or MAX_ROUNDS),
                             kind="question", state="waiting_requester",
                             payload={"job_id": job["id"], "round": res.get("round"), "question": q.get("question"), "why": q.get("why"),
                                      "options": q.get("options") or [], "recommended": q.get("recommended", -1)})
        return
    if res.get("outcome") != "answer":
        desk.factory_message(cur, cid, "factory", "回答を作れませんでした（" + "、".join((res.get("errors") or ["形の誤り"])[:5])
                             + "）。聞き方を変えるか言い足してもらえれば、もう一度動かします。", kind="error", state="waiting_requester",
                             payload={"job_id": job["id"], "errors": res.get("errors") or []})
        return
    desk.factory_message(cur, cid, "answer", answer_text(res), kind="answer", state="answered",
                         payload={"job_id": job["id"], "stance": res.get("stance"), "evidence": res.get("evidence") or [],
                                  "unknowns": res.get("unknowns") or [], "switch_to_request": bool(res.get("switch_to_request")),
                                  "dropped_refs": res.get("dropped_refs") or [], "confidence": res.get("confidence"),
                                  "questions_asked": res.get("questions_asked"), "model": res.get("model"), "cost_usd": res.get("cost_usd"),
                                  "versions": res.get("versions")})
