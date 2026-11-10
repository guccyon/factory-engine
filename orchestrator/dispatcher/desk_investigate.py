"""窓口の依頼を工場の中での一次調査に流す（ADR 0048 の順番 4）。dispatcher の側。

  相談役が route（path: investigate、調べる問いと手がかり）を返す → route()（読むだけなので確認を飛ばして running）
  → enqueue()（1 日の費用の上限を見て、調査役のジョブ kind investigate を委員会と同じ governance の枠に積む）
  → core-gates/investigate.py が結果（報告・直す先・Engine の改善 Issue・引き継ぎ資料）を返す
  → record()（結果を成果物 InvestigationResult に残し、報告を会話に提出する。受取確認は desk_line.deliver のまま）

依頼者は種類を選ばない。調査にするかは相談役が依頼の中身から決める（ADR 0048 の決定 1）。
Engine の改善 Issue の起票・引き継ぎ資料の採番と送り出し・事故の台帳への案件は順番 5（ここでは結果を残すまで）。
値は definitions/investigate.yaml の limits と desk.yaml の loop の写し（dispatcher のコンテナには definitions/ がない。食い違いはテストで見る）。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

import desk
import usage

KIND = "investigate"
PER_CASE_TURNS = 40
PER_CASE_USD = 3.0
PER_DAY_USD = float(os.environ.get("INVESTIGATE_PER_DAY_USD", "10"))   # 工場の 1 日の予算の内数（調査委員会の分は数えない）
FOLLOW_UPS = 2               # 同じ会話の続きの調査（合わせて 1 + 2 回）
MODEL_TIER = "tier-judge"
RESULT_TYPE = "InvestigationResult"
OUTCOME_JA = {"identified": "原因が分かりました。", "outside": "原因は工場の外にありました。",
              "unidentified": "工場の中の記録だけでは、原因を特定できませんでした。"}
FIX_JA = {"engine": "工場の仕組みの直しが要ります。工場の提供元に改善の依頼として渡します。",
          "operation": "運用の値（流量・予算など）を変えると直りそうです。",
          "product": "製品の側の直しが要ります。窓口の依頼として出せます。"}


def _event(cur, name: str, cid: int, payload: dict, dedupe_key: str | None = None, job_id: int | None = None) -> bool:
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values (%s,%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (name, desk.corr(cid), job_id, Jsonb({"conversation_id": cid, **payload}), dedupe_key))
    return cur.fetchone() is not None


def rounds(cur, cid: int) -> int:
    """この会話で調査に振り分けた回数"""
    cur.execute("select count(*) as n from events where correlation_id=%s and name='desk.routed' and payload->>'path'='investigate'", (desk.corr(cid),))
    return cur.fetchone()["n"]


def route(cur, cid: int, job: dict, res: dict, round_: int) -> None:
    """相談役の振り分け（path: investigate）を記録して、確認を飛ばして動かす（調査は読むだけ。definitions/operations.yaml の read の段と同じ扱い）"""
    r = res.get("route") or {}
    q = (r.get("question") or "").strip()
    if not q:
        desk.factory_message(cur, cid, "factory", "調査に振り分けようとしましたが、調べる問いが決まりませんでした。何が困っているかを一言足してください。",
                             kind="error", state="waiting_requester", payload={"job_id": job["id"], "route": r})
        return
    if rounds(cur, cid) >= 1 + FOLLOW_UPS:
        desk.factory_message(cur, cid, "factory",
                             f"この会話での調査は {1 + FOLLOW_UPS} 回までです。続きは工場の提供元が工場の外から調べるほうが早いので、"
                             "提供元に回します（運用主に知らせます）。", kind="limit", state="waiting_requester", payload={"job_id": job["id"]})
        return
    payload = {"conversation_id": cid, "path": "investigate", "question": q, "refs": r.get("refs") or [], "window_days": r.get("window_days") or 0,
               "why": r.get("why") or "", "tier": "read", "round": round_, "job_id": job["id"], "confidence": res.get("confidence"),
               "request": res.get("request") or {}, "workflow": {"path": "investigate", "steps": ["execute", "respond"]}}
    desk.advance(cur, cid, "routed", "desk.routed", payload, path="investigate")
    lines = ["振り分けました: 工場の中で調べます（工場の記録・ログ・設定を読むだけで、何も変えません）。", f"- 調べること: {q}"]
    if r.get("why"):
        lines.append(f"- 理由: {r['why']}")
    lines += [f"- 費用の上限: 1 回 ${PER_CASE_USD:.0f} まで。上限に当たったら、そこまで分かったことを出します", "", "すぐに始めます。"]
    desk.factory_message(cur, cid, "planner", "\n".join(lines), kind="route", payload={"round": round_})
    desk.advance(cur, cid, "running", "desk.confirmed_run", {"conversation_id": cid, "round": round_, "skipped": "read"})


def spent_today(cur, now: datetime) -> float:
    """今の予算の日（7:00 JST 区切り）に終わった調査のジョブの費用"""
    day = usage.budget_day(now)
    start = datetime(day.year, day.month, day.day, usage.DAY_START_HOUR, tzinfo=usage.JST)
    cur.execute("select coalesce(sum(nullif(result->>'cost_usd','')::numeric), 0) as usd from jobs "
                "where kind=%s and finished_at >= %s", (KIND, start))
    return float(cur.fetchone()["usd"] or 0)


def spent_on(cur, cid: int) -> float:
    """この会話の調査に使った費用。会話の費用（cost_usd）には足すが、相談役の上限（desk_line.MAX_USD）には数えない（調査は 1 回ごとの上限で守る）"""
    cur.execute("select coalesce(sum(nullif(result->>'cost_usd','')::numeric), 0) as usd from jobs where kind=%s and correlation_id=%s",
                (KIND, desk.corr(cid)))
    return float(cur.fetchone()["usd"] or 0)


def _route_event(cur, cid: int) -> dict | None:
    cur.execute("select payload from events where correlation_id=%s and name='desk.routed' order by id desc limit 1", (desk.corr(cid),))
    r = cur.fetchone()
    return r["payload"] if r else None


def investigate_input(c: dict, rt: dict, engine_ref: str | None) -> dict:
    """調査役に渡す依頼（依頼者の言葉はデータ）。deny は Engine の改善 Issue に出てはいけない名前（製品・依頼者）"""
    now = datetime.now(timezone.utc)
    days = int(rt.get("window_days") or 0)
    product = c["product"]
    return {"conversation_id": c["id"], "question": rt["question"], "product": product, "engine_ref": engine_ref,
            "window": [(now - timedelta(days=max(days, 1))).isoformat(), now.isoformat()],
            "refs": rt.get("refs") or [], "notes": rt.get("why") or "",
            "limits": {"turns": PER_CASE_TURNS, "usd": PER_CASE_USD},
            "deny": sorted({product, product.split("/")[-1], product.split("/")[0], c.get("opened_by") or ""} - {""})}


def enqueue(cur, repos: list[str], now: datetime | None = None, engine_ref: str | None = None) -> list[int]:
    """running の調査の会話に、その回の調査役のジョブを 1 本だけ積む。1 日の上限に達していたら積まずに会話に知らせる（次の日に積む）"""
    now = now or datetime.now(timezone.utc)
    cur.execute("select * from desk_conversations where state='running' and path='investigate' order by updated_at")
    out = []
    spent = None
    for c in cur.fetchall():
        cid = c["id"]
        rt = _route_event(cur, cid)
        if not rt or rt.get("path") != "investigate":
            continue
        n = rt.get("round") or 1
        cur.execute("select 1 from events where correlation_id=%s and name='investigate.enqueued' and (payload->>'round')::int=%s", (desk.corr(cid), n))
        if cur.fetchone():
            continue
        if c["product"] not in repos:
            if _event(cur, "investigate.skipped", cid, {"round": n, "reason": "not_a_factory_repo"}, f"investigate.skipped:{cid}:{n}"):
                desk.factory_message(cur, cid, "factory", f"{c['product']} はこの工場が受け持つ製品ではないので、調べられません。", kind="error")
            continue
        spent = spent_today(cur, now) if spent is None else spent
        if spent >= PER_DAY_USD:
            day = usage.budget_day(now).isoformat()
            if _event(cur, "investigate.deferred", cid, {"round": n, "spent_usd": round(spent, 2), "per_day_usd": PER_DAY_USD, "day": day},
                      f"investigate.deferred:{cid}:{n}:{day}"):
                desk.factory_message(cur, cid, "factory", f"今日の調査の費用が上限（${PER_DAY_USD:.0f}）に達したので、"
                                     f"次の日（{usage.DAY_START_HOUR}:00 から）に始めます。", kind="progress")
            continue
        inp = investigate_input(c, rt, engine_ref)
        try:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind) "
                        "values (%s,%s,%s,1,'desk',%s,%s) returning id", (desk.corr(cid), c["product"], -cid, MODEL_TIER, KIND))
        except psycopg.errors.UniqueViolation:
            continue
        jid = cur.fetchone()["id"]
        _event(cur, "job.enqueued", cid, {"by": "desk", "kind": KIND, "repo": c["product"], "attempt": 1, "input": inp}, job_id=jid)
        _event(cur, "investigate.enqueued", cid, {"round": n, "job_id": jid}, f"investigate.enqueued:{cid}:{n}", job_id=jid)
        spent += PER_CASE_USD   # 同じ周回で積みすぎない（終わるまで費用は分からないので上限で見込む）
        out.append(jid)
    return out


def report_text(res: dict) -> str:
    lines = [OUTCOME_JA.get(res.get("outcome"), OUTCOME_JA["unidentified"])]
    if (res.get("report") or "").strip():
        lines += ["", res["report"].strip()]
    fix = res.get("fix_target")
    if fix in FIX_JA:
        lines += ["", FIX_JA[fix]]
        if fix == "operation" and res.get("operation_proposal"):
            lines.append(f"- 提案: {res['operation_proposal']}")
        if fix == "product" and res.get("product_request"):
            lines.append(f"- 依頼の下書き: {res['product_request']}")
    if res.get("outcome") == "unidentified" or res.get("handoff"):
        lines += ["", "工場の中で分からなかったところは、調べた事実と仮説を残しました。工場の提供元が、工場の外から続きを調べられます。"]
    if (res.get("money_loss") or {}).get("suspected"):
        lines += ["", "費用が余分にかかった疑いがあります。工場の事故として確かめます。"]
    return "\n".join(lines)


def record(cur, job: dict, result: dict | None, state: str, err: str | None) -> None:
    """調査役の結果を残し、報告を提出する。失敗も提出する（依頼者が続きか差し戻しを選べる）"""
    import desk_line
    cid = -int(job["issue_number"])
    res = result or {}
    desk_line._add_cost(cur, cid, res.get("cost_usd"))
    cur.execute("select * from desk_conversations where id=%s", (cid,))
    c = cur.fetchone()
    rt = _route_event(cur, cid) or {}
    n = rt.get("round") or 1
    if not c or c["state"] != "running":
        return   # 取り下げられた・終わった会話には提出しない（結果は jobs に残る）
    if state == "cancelled":
        return
    if state != "succeeded" or not res.get("ok"):
        _event(cur, "investigate.failed", cid, {"round": n, "error": (err or str(res.get("exit")))[:300]}, job_id=job["id"])
        desk_line.deliver(cur, cid, n, {"ok": False, "summary": f"調査のジョブが途中で失敗しました（ジョブ {job['id']}）。"
                                                                 "「続きを頼む」でもう一度調べます。"})
        return
    keep = {k: res.get(k) for k in ("outcome", "report", "confidence", "fix_target", "engine_issue", "issue_errors", "issue_held",
                                     "operation_proposal", "product_request", "money_loss", "handoff", "handoff_errors",
                                     "cost_usd", "num_turns", "model")}
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values (%s,%s,%s,'investigate',%s) returning id",
                (RESULT_TYPE, desk.corr(cid), job["id"], Jsonb({**keep, "conversation_id": cid, "round": n, "product": c["product"]})))
    aid = cur.fetchone()["id"]
    _event(cur, "investigate.finished", cid, {"round": n, "artifact_id": aid, "outcome": keep["outcome"], "fix_target": keep["fix_target"],
                                              "issue_held": bool(keep["issue_held"]), "has_handoff": bool(keep["handoff"]),
                                              "money_loss": bool((keep["money_loss"] or {}).get("suspected")), "cost_usd": keep["cost_usd"]},
           job_id=job["id"])
    stop = (keep.get("handoff") or {}).get("stop_reason")
    out = {"ok": keep["outcome"] != "unidentified", "summary": report_text(res)}
    if stop == "budget":
        out["cap_hit"] = "費用・ターン"
        out["remaining"] = "まだ見ていない所: " + "、".join((keep["handoff"] or {}).get("not_looked_at") or ["（記録なし）"])
    desk_line.deliver(cur, cid, n, out)
