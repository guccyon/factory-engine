"""窓口の依頼に LLM の実行役が応える（ADR 0037 の決定 5 の quick と analysis、ADR 0039 の決定 2・2a）。dispatcher の側。

  quick:    その場で答える（文面・要約・表・見立て・既にある記録からの数）。目安 15 分
  analysis: 集計の問いを書いて動かし、報告書にする（所見 → 数 → 解釈 → 打てる手 → 限界 → 次に調べること）。振り分けで提出の時刻を示す
            （ADR 0039 の決定 3）。数は、実行役が出した問いを dispatcher が読むだけの接続で動かし直して突き合わせる（ADR 0037 の決定 6）

  相談役が route（path: quick、やってほしいことの 1 文と成果物の形）を返す → route()（読むだけなので確認を飛ばして running）
  → enqueue()（実行役のジョブ kind desk_task を窓口の枠に積む）
  → core-gates/desk_task.py が、依頼を自分で解釈して答え・文面・表・見立てを作って返す
  → record()（結果を成果物 DeskTaskResult に残し、会話に提出する。受取確認は desk_line.deliver のまま）

実行役は読むだけ。読めるのは、製品の specs/・docs/ の写し、工場の記録の要約（回答役と同じ材料）、
dispatcher の中継の道具（inv_sql: 工場の記録のビュー、inv_github: 製品の Issue・PR を GET だけ）。
外に出す・状態を変える作用は持たない（ADR 0037 の決定 4。結果は依頼者にだけ返す）。依頼の本文はデータとして渡す。
上限（ターン・費用）に当たったら、決まったところまでを提出し、続きの選択肢を添える（ADR 0039 の決定 4）。
値は definitions/desk.yaml の task の写し（dispatcher のコンテナには definitions/ がない。食い違いはテストで見る）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

import desk
import requester_prefs

KIND = "desk_task"
MODEL_TIER = "tier-small"   # 工程 desk.task の既定（quick）
TIERS = {"quick": "tier-small", "analysis": "tier-judge"}   # analysis は集計の問いを組み、数を解釈するので強いモデル
RESULT_TYPE = "DeskTaskResult"
# 対応の種類ごとの上限（ADR 0037 の決定 5 の目安。仮置き。運用で直す）。turns はエージェントのターン、usd は 1 回の目安、max_usd は claude を止める天井
LIMITS = {"quick": {"turns": 16, "usd": 0.5, "max_usd": 1.5}, "analysis": {"turns": 40, "usd": 3.0, "max_usd": 5.0}}
PATHS = tuple(LIMITS)
FORMS = {"answer": "会話の返事", "text": "文面の下書き", "table": "表", "summary": "要約", "estimate": "見立て", "report": "報告書"}
ETA_SEC = {"quick": desk.LOOP["targets"]["deliver"]["quick"], "analysis": desk.LOOP["targets"]["deliver"]["analysis"]}
MAX_QUERIES = 12          # 突き合わせで動かし直す問いの数の上限
KEEP_ROWS = 50            # 成果物に残す行の数（問いごと）
TASK_WORK = ["desk.converse"]   # 実行役の業務（依頼者の好みの選び方。回答役と同じ）
MAX_FOLLOW_UPS = desk.LOOP["max_follow_ups"]


def _event(cur, name: str, cid: int, payload: dict, dedupe_key: str | None = None, job_id: int | None = None) -> bool:
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values (%s,%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (name, desk.corr(cid), job_id, Jsonb({"conversation_id": cid, **payload}), dedupe_key))
    return cur.fetchone() is not None


def _route_event(cur, cid: int) -> dict | None:
    cur.execute("select payload from events where correlation_id=%s and name='desk.routed' order by id desc limit 1", (desk.corr(cid),))
    r = cur.fetchone()
    return r["payload"] if r else None


def rounds(cur, cid: int) -> int:
    cur.execute("select count(*) as n from events where correlation_id=%s and name='desk.routed' and payload->>'path' = any(%s)",
                (desk.corr(cid), list(PATHS)))
    return cur.fetchone()["n"]


def route(cur, cid: int, job: dict, res: dict, round_: int, now: datetime | None = None) -> None:
    """相談役の振り分け（path: quick・analysis）を記録して、確認を飛ばして動かす（読むだけで、結果は依頼者にだけ返す。ADR 0037 の決定 4）"""
    r = res.get("route") or {}
    path = r.get("path")
    t = r.get("task") if isinstance(r.get("task"), dict) else {}
    what = (t.get("what") or "").strip()
    if path not in PATHS or not what:
        desk.factory_message(cur, cid, "factory", "その場で答える仕事に振り分けようとしましたが、やることが決まりませんでした。何がほしいかを一言足してください。",
                             kind="error", state="waiting_requester", payload={"job_id": job["id"], "route": r})
        return
    if rounds(cur, cid) >= 1 + MAX_FOLLOW_UPS:
        desk.factory_message(cur, cid, "factory", f"この会話での続きは {MAX_FOLLOW_UPS} 回までです。続きは新しい依頼として開いてください。",
                             kind="limit", state="waiting_requester", payload={"job_id": job["id"]})
        return
    form = t.get("form") if t.get("form") in FORMS else ("report" if path == "analysis" else "answer")
    lim = LIMITS[path]
    by = now or datetime.now(timezone.utc)
    by = by + timedelta(seconds=ETA_SEC[path])
    payload = {"conversation_id": cid, "path": path, "task": {"what": what, "form": form}, "why": r.get("why") or "", "tier": "read",
               "round": round_, "job_id": job["id"], "confidence": res.get("confidence"), "request": res.get("request") or {},
               "workflow": {"path": path, "steps": ["execute", "respond"], "limits": lim},
               **({"deliver_by": by.isoformat()} if path == "analysis" else {})}
    desk.advance(cur, cid, "routed", "desk.routed", payload, path=path)
    if path == "analysis":
        lines = ["振り分けました: 工場が集計して報告書にします（工場の記録・製品の GitHub・仕様を読むだけで、何も変えず、外にも送りません）。",
                 f"- 調べること: {what}", "- 返すもの: 報告書（所見・数と出した問い・解釈・打てる手・分からないこと）"]
    else:
        lines = ["振り分けました: 工場がその場で答えます（記録と仕様を読むだけで、何も変えず、外にも送りません）。",
                 f"- やること: {what}", f"- 返すもの: {FORMS[form]}"]
    if r.get("why"):
        lines.append(f"- 理由: {r['why']}")
    lines += [f"- 提出の目安: {desk.clock(by)} ごろ（費用は 1 回 ${lim['max_usd']:.0f} まで）" if path == "analysis"
              else "- 目安: 15 分以内。大きすぎたら、そこまでを出して続け方を相談します", "", "すぐに始めます。"]
    desk.factory_message(cur, cid, "planner", "\n".join(lines), kind="route", payload={"round": round_})
    desk.advance(cur, cid, "running", "desk.confirmed_run", {"conversation_id": cid, "round": round_, "skipped": "read"})


def spent_on(cur, cid: int) -> float:
    """この会話の実行役に使った費用。会話の費用（cost_usd）には足すが、相談役の上限（desk_line.MAX_USD）には数えない（1 回ごとの上限で守る）"""
    cur.execute("select coalesce(sum(nullif(result->>'cost_usd','')::numeric), 0) as usd from jobs where kind=%s and correlation_id=%s",
                (KIND, desk.corr(cid)))
    return float(cur.fetchone()["usd"] or 0)


def task_input(cur, c: dict, rt: dict) -> dict:
    """実行役に渡す依頼（依頼者の言葉はデータ）。工場の記録の要約は回答役と同じ材料（参照の id つき）"""
    import desk_answer
    from desk_line import messages
    msgs = messages(cur, c["id"])
    path = rt.get("path") or "quick"
    return {"conversation_id": c["id"], "product": c["product"], "requester": c.get("opened_by"), "path": path,
            "task": rt.get("task") or {}, "request": rt.get("request") or {}, "why": rt.get("why") or "", "round": rt.get("round") or 1,
            "messages": [{k: m.get(k) for k in ("author", "role", "kind", "body", "at")} for m in msgs if m.get("kind") not in ("ack", "route")],
            "records": desk_answer.records(cur, c["product"], desk_answer.referenced(msgs, c["product"])),
            "limits": LIMITS.get(path, LIMITS["quick"]), "tier": TIERS.get(path, MODEL_TIER),
            "requester_preferences": requester_prefs.for_job(cur, c["product"], c.get("opened_by"), TASK_WORK)}


def enqueue(cur, repos: list[str]) -> list[int]:
    """running の quick の会話に、その回の実行役のジョブを 1 本だけ積む（窓口の枠。ラインの一時停止中も動く）"""
    cur.execute("select * from desk_conversations where state='running' and path = any(%s) order by updated_at", (list(PATHS),))
    out = []
    for c in cur.fetchall():
        cid = c["id"]
        rt = _route_event(cur, cid)
        if not rt or rt.get("path") not in PATHS:
            continue
        n = rt.get("round") or 1
        cur.execute("select 1 from events where correlation_id=%s and name='desk_task.enqueued' and (payload->>'round')::int=%s", (desk.corr(cid), n))
        if cur.fetchone():
            continue
        if c["product"] not in repos:
            if _event(cur, "desk_task.skipped", cid, {"round": n, "reason": "not_a_factory_repo"}, f"desk_task.skipped:{cid}:{n}"):
                desk.factory_message(cur, cid, "factory", f"{c['product']} はこの工場が受け持つ製品ではないので、答えられません。", kind="error")
            continue
        inp = task_input(cur, c, rt)
        try:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind) "
                        "values (%s,%s,%s,1,'desk',%s,%s) returning id", (desk.corr(cid), c["product"], -cid, TIERS.get(rt["path"], MODEL_TIER), KIND))
        except psycopg.errors.UniqueViolation:
            continue
        jid = cur.fetchone()["id"]
        _event(cur, "job.enqueued", cid, {"by": "desk", "kind": KIND, "repo": c["product"], "attempt": 1, "input": inp}, job_id=jid)
        _event(cur, "desk_task.enqueued", cid, {"round": n, "job_id": jid, "path": rt["path"]}, f"desk_task.enqueued:{cid}:{n}", job_id=jid)
        out.append(jid)
    return out


NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers(v) -> set[str]:
    """値から数を取り出して正規化する（12・12.0・"$12.00" を同じに見る）"""
    out = set()
    for m in NUM.findall(str(v).replace(",", "")):
        try:
            f = float(m)
        except ValueError:
            continue
        out.add(f"{round(f, 2):g}")
    return out


def verify(queries: list, query=None) -> list[dict]:
    """実行役が出した集計の問いを、読むだけの接続で動かし直し、報告に使った数（used）が結果にあるかを見る（ADR 0037 の決定 6 の突き合わせ）。
    query は investigate_db.query と同じ形（{columns, rows, truncated}）。繋げない・流せないときは checked=false で残す（数の作り話の疑いとは分ける）"""
    import investigate_db
    query = query or investigate_db.query
    out = []
    for q in (queries or [])[:MAX_QUERIES]:
        if not isinstance(q, dict) or not isinstance(q.get("sql"), str):
            continue
        item = {"id": str(q.get("id") or len(out) + 1)[:40], "purpose": str(q.get("purpose") or "")[:200], "sql": q["sql"][:4000],
                "used": str(q.get("used") or "")[:300]}
        try:
            r = query(q["sql"])
        except Exception as e:  # noqa: BLE001  突き合わせられなかったことを残す（提出は止めない）
            out.append({**item, "checked": False, "error": f"{type(e).__name__}: {str(e)[:200]}"})
            continue
        seen = set()
        for row in r.get("rows") or []:
            for v in row:
                seen |= _numbers(v)
        want = _numbers(item["used"])
        missing = sorted(want - seen)
        out.append({**item, "checked": True, "columns": r.get("columns") or [], "rows": [[str(v) for v in row] for row in (r.get("rows") or [])[:KEEP_ROWS]],
                    "row_count": len(r.get("rows") or []), "truncated": bool(r.get("truncated")), "matched": not missing, "missing": missing})
    return out


def verify_text(checks: list[dict]) -> list[str]:
    if not checks:
        return []
    ok = [c for c in checks if c.get("checked") and c.get("matched")]
    bad = [c for c in checks if c.get("checked") and not c.get("matched")]
    skipped = [c for c in checks if not c.get("checked")]
    lines = ["", f"数の突き合わせ: 報告に使った問い {len(checks)} 本を工場の側で動かし直しました（合った {len(ok)}・ずれた {len(bad)}・動かせなかった {len(skipped)}）。"]
    for c in bad:
        lines.append(f"- ⚠ 問い {c['id']}（{c['purpose']}）: 報告の数 {', '.join(c['missing'])} が結果に見当たりません。この数は確かめが要ります")
    for c in skipped:
        lines.append(f"- 問い {c['id']}（{c['purpose']}）は動かし直せませんでした（{c.get('error')}）")
    return lines


def deliver_text(res: dict) -> str:
    """提出の本文。冒頭に「こう受け取った」（ADR 0039 の決定 2a）、続けて成果物、根拠、分からないこと"""
    lines = [""]
    if (res.get("interpretation") or "").strip():
        lines += [f"こう受け取りました: {res['interpretation'].strip()}", ""]
    lines.append((res.get("answer") or "").strip())
    ev = res.get("evidence") or []
    if ev:
        lines += ["", "根拠:"] + [f"- {e.get('ref')}: {e.get('what') or ''}" for e in ev]
    unknown = res.get("unknowns") or []
    if unknown:
        lines += ["", "分からないこと・推測で書いたこと:"] + [f"- {u}" for u in unknown]
    if res.get("next"):
        lines += ["", f"次にできること: {res['next']}"]
    lines += verify_text(res.get("checks") or [])
    return "\n".join(lines)


def record(cur, job: dict, result: dict | None, state: str, err: str | None, query=None) -> None:
    """実行役の結果を残し、提出する。失敗も提出する（依頼者が続きか差し戻しを選べる）"""
    import desk_line
    cid = -int(job["issue_number"])
    res = result or {}
    desk_line._add_cost(cur, cid, res.get("cost_usd"))
    cur.execute("select * from desk_conversations where id=%s", (cid,))
    c = cur.fetchone()
    rt = _route_event(cur, cid) or {}
    n = rt.get("round") or 1
    if not c or c["state"] != "running" or state == "cancelled":
        return   # 取り下げられた・終わった会話には提出しない（結果は jobs に残る）
    if state != "succeeded" or not res.get("ok") or not (res.get("answer") or "").strip():
        _event(cur, "desk_task.failed", cid, {"round": n, "error": (err or str(res.get("exit") or res.get("errors") or ""))[:300]}, job_id=job["id"])
        desk_line.deliver(cur, cid, n, {"ok": False, "summary": f"答えを作る途中で止まりました（ジョブ {job['id']}）。"
                                                                 "「続きを頼む」でもう一度やります。"})
        return
    desk_line.remember_stated(cur, cid, job, res)
    if rt.get("path") == "analysis":
        res = {**res, "checks": verify(res.get("queries") or [], query=query)}
    keep = {k: res.get(k) for k in ("interpretation", "answer", "evidence", "unknowns", "next", "confidence", "cap_hit", "process",
                                     "queries", "checks", "cost_usd", "num_turns", "model", "versions")}
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values (%s,%s,%s,'desk',%s) returning id",
                (RESULT_TYPE, desk.corr(cid), job["id"], Jsonb({**keep, "conversation_id": cid, "round": n, "path": rt.get("path"),
                                                                "product": c["product"]})))
    aid = cur.fetchone()["id"]
    checks = res.get("checks") or []
    _event(cur, "desk_task.answered", cid, {"round": n, "artifact_id": aid, "path": rt.get("path"), "cap_hit": bool(res.get("cap_hit")),
                                        "evidence": len(res.get("evidence") or []), "cost_usd": res.get("cost_usd"), "num_turns": res.get("num_turns"),
                                        **({"queries": len(checks), "mismatched": sum(1 for c in checks if c.get("checked") and not c.get("matched")),
                                            "unchecked": sum(1 for c in checks if not c.get("checked"))} if rt.get("path") == "analysis" else {})},
           job_id=job["id"])
    out = {"ok": True, "summary": deliver_text(res)}
    if res.get("cap_hit"):
        out["cap_hit"] = "ターン"
        out["remaining"] = (res.get("next") or "残りは時間をかけた分析（analysis）で続けられます")
    desk_line.deliver(cur, cid, n, out)
