"""窓口の依頼を工場の中での一次調査に流す（ADR 0048 の順番 4）。dispatcher の側。

  相談役が route（path: investigate、調べる問いと手がかり）を返す → route()（読むだけなので確認を飛ばして running）
  → enqueue()（1 日の費用の上限を見て、調査役のジョブ kind investigate を委員会と同じ governance の枠に積む）
  → core-gates/investigate.py が結果（報告・直す先・Engine の改善 Issue・引き継ぎ資料）を返す
  → record()（結果を成果物 InvestigationResult に残し、報告を会話に提出する。受取確認は desk_line.deliver のまま）

依頼者は種類を選ばない。調査にするかは相談役が依頼の中身から決める（ADR 0048 の決定 1）。
提出（順番 5）: record() が結果を見て、決まった道で次を残す（調査役は何も変えない）
  - Engine の改善 Issue の案（investigate.issue_proposed）。調査役の検査を通らなかった・ここの検査に掛かったものは起票せず保留（investigate.issue_held と建設者の受信トレイ）
  - 引き継ぎ資料（INV-YYYY-NNNN、investigate.handoff）。本体は工場の中（events）に置き、提供元の口には要約と id だけ
  - お金の損の疑いは事故の台帳に案件を開く（ADR 0046）
  起票は supervisor の周回（file_issues）。Engine の改善 Issue はラベル factory:investigation、引き継ぎは factory:investigation-handoff。
値は definitions/investigate.yaml の limits と desk.yaml の loop の写し（dispatcher のコンテナには definitions/ がない。食い違いはテストで見る）。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

import desk
import factory_incident
import inbox
import investigate_bench
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
ISSUE_LABEL = "factory:investigation"
HANDOFF_LABEL = "factory:investigation-handoff"
HANDOFF_ALWAYS = ("sensitive", "asked")   # 特定できても外に回す止め方（ADR 0048 の決定 6 の 4・5）
STOP_JA = {"budget": "上限に当たった、または仮説が残らない・確信度が低いまま", "needs_code": "確かめるにはコードを読むしかない",
           "needs_host": "試しの実行やホストの中を見る必要がある", "sensitive": "秘密情報・権限・決済・DB の破壊的な変更に関わりそう",
           "asked": "依頼者が外でも調べてと言った"}
# 起票の前の 2 段目の検査（1 段目は調査役のジョブの core-gates/investigate_check.py。dispatcher には core-gates がないので、漏れやすい形だけを写す）
LEAK = [("URL", re.compile(r"https?://")), ("番号の参照", re.compile(r"#\d+\b")), ("メールアドレス", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
        ("記録の参照", re.compile(r"\b(job|ticket|event|stoppage|judgment|desk|pr|issue):\S+"))]   # 製品・依頼者の名前は deny で見る
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
                "where kind=%s and finished_at >= %s and correlation_id not like %s", (KIND, start, investigate_bench.PREFIX + "%"))   # 評価セットの再生は数えない
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
            "deny": deny_for(c)}


def deny_for(c: dict) -> list[str]:
    """Engine の改善 Issue と引き継ぎの要約に出てはいけない名前（製品のリポジトリ名・製品名・持ち主・依頼者）"""
    product = c["product"]
    return sorted({product, product.split("/")[-1], product.split("/")[0], c.get("opened_by") or ""} - {""})


def leaks(text: str, deny: list[str]) -> list[str]:
    """起票の前の 2 段目の検査。引っかかった種類を返す（中身は返さない。ログや受信トレイに名前を写さないため）"""
    low = (text or "").lower()
    out = [name for name, rx in LEAK if rx.search(text or "")]
    if any(d and d.lower() in low for d in deny):
        out.append("製品・人の名前")
    return out


def _next_inv_id(cur, now: datetime) -> str:
    prefix = f"INV-{now.astimezone(usage.JST):%Y}-"
    cur.execute("select count(*) as n from events where name='investigate.handoff' and payload->>'id' like %s", (prefix + "%",))
    return f"{prefix}{int(cur.fetchone()['n']) + 1:04d}"


def needs_handoff(res: dict) -> bool:
    """外（二次）に引き継ぐか（ADR 0048 の決定 6）。特定できなかった、または特定できても秘密情報に関わる・依頼者が外でもと言った"""
    stop = (res.get("handoff") or {}).get("stop_reason")
    return res.get("outcome") == "unidentified" or stop in HANDOFF_ALWAYS


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
    sub = submit(cur, c, n, job["id"], aid, keep)
    stop = (keep.get("handoff") or {}).get("stop_reason")
    tr = investigate_bench.trial(cur)
    note = ("\n\n（この調査は試行中です。過去の件で原因に着いた割合が "
            + (f"{tr['rate']:.0%}" if tr["rate"] is not None else "まだ測っていない状態")
            + f"で、目安の {tr['pass_rate']:.0%} に届いていません。結果は確かめてから使ってください）") if tr else ""
    out = {"ok": keep["outcome"] != "unidentified",
           "summary": report_text(res) + (f"\n\n引き継ぎ資料: {sub['handoff']}" if sub.get("handoff") else "") + note}
    if stop == "budget":
        out["cap_hit"] = "費用・ターン"
        out["remaining"] = "まだ見ていない所: " + "、".join((keep["handoff"] or {}).get("not_looked_at") or ["（記録なし）"])
    desk_line.deliver(cur, cid, n, out)


# ------------------------------------------------------------------ 提出（ADR 0048 の順番 5）
def submit(cur, c: dict, round_: int, job_id: int, artifact_id: int, res: dict, now: datetime | None = None) -> dict:
    """結果を見て、Engine の改善 Issue の案・引き継ぎ資料・事故の台帳の案件を決まった道で残す。起票はしない（supervisor の file_issues）"""
    now = now or datetime.now(timezone.utc)
    cid = c["id"]
    deny = deny_for(c)
    out: dict = {}
    base = {"conversation_id": cid, "round": round_, "artifact_id": artifact_id, "job_id": job_id}
    issue = res.get("engine_issue") if res.get("fix_target") == "engine" else None
    if issue and issue.get("title"):
        text = f"{issue['title']}\n{issue.get('body') or ''}"
        errs = list(res.get("issue_errors") or []) if res.get("issue_held") else leaks(text, deny)
        key = f"inv:{cid}:{round_}"
        if res.get("issue_held") or errs:   # 漏れるより遅れるほうを選ぶ（決定 5 の 3）。建設者が直して起票する
            if _event(cur, "investigate.issue_held", cid, {**base, "key": key, "errors": errs[:10]}, f"investigate.issue_held:{key}", job_id=job_id):
                inbox.request(cur, f"investigate_issue_held:{cid}:{round_}", "investigate_issue_held", "builder",
                              f"一次調査の改善 Issue の起票を保留（顧客の情報が残った: {'、'.join(errs[:3]) or '検査'}）。成果物 {artifact_id} を直して起票する",
                              action="成果物の engine_issue を直し、ラベル factory:investigation で起票する", correlation_id=desk.corr(cid), by="investigate")
            out["issue"] = "held"
        elif _event(cur, "investigate.issue_proposed", cid, {**base, "key": key, "title": issue["title"][:200], "body": issue.get("body") or ""},
                    f"investigate.issue_proposed:{key}", job_id=job_id):
            out["issue"] = "proposed"
    if needs_handoff(res):
        h = res.get("handoff") or {}
        cur.execute("select payload->>'id' as id from events where name='investigate.handoff' and dedupe_key=%s", (f"investigate.handoff:{cid}:{round_}",))
        got = cur.fetchone()
        hid = got["id"] if got else _next_inv_id(cur, now)
        dossier = {**h, "id": hid}
        _event(cur, "investigate.handoff", cid, {**base, "id": hid, "stop_reason": h.get("stop_reason"), "dossier": dossier,
                                                 "errors": list(res.get("handoff_errors") or [])[:10]}, f"investigate.handoff:{cid}:{round_}", job_id=job_id)
        out["handoff"] = hid
    if (res.get("money_loss") or {}).get("suspected"):
        try:
            case = factory_incident.open_case(cur, "investigate", f"一次調査でお金の損の疑い（窓口の会話 {cid}）", "llm_tokens", "medium",
                                              {**base, "note": str((res.get("money_loss") or {}).get("note") or "")[:500]},
                                              note="窓口の一次調査（ADR 0048）が見つけた。資源と仕組みは調査委員会が確かめる",
                                              dedupe=f"fincident.opened:inv:{cid}:{round_}", now=now)
        except (ValueError, OSError) as e:   # 台帳の定義が読めないときも提出は止めない
            case = None
            _event(cur, "investigate.case_failed", cid, {**base, "error": repr(e)[:200]}, job_id=job_id)
        if case:
            out["case"] = case
    return out


def handoff_summary(p: dict, engine_ref: str | None) -> str:
    """提供元の口（Issue）に書く引き継ぎの要約。決まったひな形だけで組み立て、調査役の文は入れない（顧客の情報を出さない）"""
    d = p.get("dossier") or {}
    stop = p.get("stop_reason") or "budget"
    rows = [f"工場の中の一次調査（ADR 0048）で特定できなかった件を、工場の外の調査に引き継ぎます。",
            "", f"- 引き継ぎ資料: {p['id']}", f"- 止めた理由: {stop}（{STOP_JA.get(stop, '')}）",
            f"- Engine の版: {engine_ref or 'unknown'}",
            f"- 見た事実: {len(d.get('facts') or [])} 件、仮説: {len(d.get('hypotheses') or [])} 件、打ち消した仮説: {len(d.get('ruled_out') or [])} 件、"
            f"まだ見ていない所: {len(d.get('not_looked_at') or [])} 件",
            "", f"資料の本体はその工場の中にあります（`factoryctl investigation show {p['id']}`）。一次の事実をやり直さず、資料の id から始めてください。"]
    if p.get("errors"):
        rows.append(f"資料の形の検査で {len(p['errors'])} 件の不足があります（本体に記録）。")
    return "\n".join(rows)


def pending(cur, name: str, done: str, limit: int) -> list[dict]:
    cur.execute(f"select p.payload, p.correlation_id from events p where p.name=%s and not exists "
                f"(select 1 from events f where f.name=%s and f.dedupe_key = %s || (p.payload->>'conversation_id') || ':' || (p.payload->>'round')) "
                f"order by p.id limit %s", (name, done, f"{done}:", limit))
    return [r["payload"] for r in cur.fetchall()]


def file_issues(cur, gh, engine_repo: str, engine_ref: str | None = None, limit: int = 2) -> list[str]:
    """案を Engine の Issue にする（supervisor の周回）。改善 Issue は同じ題が開いていれば足さず、そこに事実が増えたことを書く（ADR 0044）"""
    filed = []
    todo = pending(cur, "investigate.issue_proposed", "investigate.issue_filed", limit)
    if todo:
        open_ = {i.get("title", ""): i for i in gh("GET", f"repos/{engine_repo}/issues?state=open&labels={ISSUE_LABEL}&per_page=100") or []}
        for p in todo:
            hit = open_.get(p["title"])
            if hit:
                gh("POST", f"repos/{engine_repo}/issues/{hit['number']}/comments",
                   {"body": "別の工場の中の一次調査（ADR 0048）でも同じ症状を見ました。直すときに合わせて確かめてください。"})
                url = hit.get("html_url")
            else:
                body = p["body"] + "\n\n---\n工場の中の一次調査（ADR 0048）が起票。直すのは建設者。"
                url = (gh("POST", f"repos/{engine_repo}/issues", {"title": p["title"], "body": body, "labels": [ISSUE_LABEL]}) or {}).get("html_url")
            _event(cur, "investigate.issue_filed", p["conversation_id"], {"round": p["round"], "key": p["key"], "issue": url, "duplicate": bool(hit)},
                   f"investigate.issue_filed:{p['conversation_id']}:{p['round']}")
            filed.append(url)
    for p in pending(cur, "investigate.handoff", "investigate.handoff_filed", limit):
        url = (gh("POST", f"repos/{engine_repo}/issues", {"title": f"一次調査の引き継ぎ {p['id']}", "body": handoff_summary(p, engine_ref),
                                                         "labels": [HANDOFF_LABEL]}) or {}).get("html_url")
        _event(cur, "investigate.handoff_filed", p["conversation_id"], {"round": p["round"], "id": p["id"], "issue": url},
               f"investigate.handoff_filed:{p['conversation_id']}:{p['round']}")
        filed.append(url)
    return filed


def handoffs(cur, limit: int = 50) -> list[dict]:
    cur.execute("select h.payload, h.occurred_at, f.payload->>'issue' as issue from events h left join events f on f.name='investigate.handoff_filed' "
                "and f.payload->>'id' = h.payload->>'id' where h.name='investigate.handoff' order by h.id desc limit %s", (limit,))
    return [{**r["payload"], "at": r["occurred_at"].isoformat(), "issue": r["issue"]} for r in cur.fetchall()]


def get_handoff(cur, hid: str) -> dict | None:
    """引き継ぎ資料の本体（二次の調査はここから始める）。依頼者の問いは言葉のまま入っている（工場の中だけで見る）"""
    return next((h for h in handoffs(cur, 1000) if h["id"] == hid), None)
