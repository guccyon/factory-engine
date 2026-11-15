"""オンボーディングの棚卸しと解析のジョブ（S1・S2。ADR 0045 の追記 2026-11-15。Gen1 のプロトタイプ）。

- 事業主が答え（onboarding_intake.py）でリポを渡したら、工場が全リポを読み取りで取り、技術・ビルド・テスト・出し方（S1）と、
  API・画面・リポ間の依存・DB・用語の候補（S2）を起こす。先方のリポには書かない（ジョブのトークンは読み取りだけ）
- 答えが変わったら、解析に効く問い（ANALYSIS_QUESTIONS と解析が足した問い）なら解析をやり直し、それ以外なら判定だけを測り直す
  （取り込みのループの最初の形。ADR 0045 の 9）。続けて答えたときに何度も流さないよう、最後の答えから DEBOUNCE_SEC 待ってから積む
- 結果: 解析の要約は events の onboarding.analyzed、起こした文書（アーキテクチャの要約・API の契約の下書き・画面の一覧・用語の候補）は
  オブジェクトストアの onboarding/<製品>/<ジョブ>/。判定（onboarding.assessed）は、前の判定に、答えから出した粒度（p-*）と、
  解析が出した粒度（o-*）と満たした条件を重ねて記録する
- 工場の中でビルドしてテストを流す（S4、o-env・o-tests）はまだ行わない。建設者が factoryctl readiness record で残す
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

import onboarding
import onboarding_intake as intake
import readiness

KIND = "onboard"
CORR_PREFIX = "onboarding:"
ISSUE_BASE = 1_000_000_000          # issue_number は -(ISSUE_BASE + きっかけの events の id)。窓口の会話（-会話の id）と重ならない
DEBOUNCE_SEC = int(os.environ.get("ONBOARD_DEBOUNCE_SEC", "600"))
PER_DAY = int(os.environ.get("ONBOARD_PER_DAY", "4"))   # 1 製品が 1 日に流す解析の上限（答えるたびに流して費用が膨らまないように）
ANALYSIS_QUESTIONS = ("repos", "web_scope", "auth_flow", "staging", "ship_existing", "design", "docs", "review_rules")
BUILT_ITEMS = ("o-stack", "o-api", "o-screens", "o-rules", "o-glossary")
# 人の確かめが要る粒度は、解析だけでは上げない（o-screens・o-glossary の 3 は事業主、o-rules の 3 は社内の開発者）
CAP_WITHOUT_HUMAN = {"o-screens": 2, "o-glossary": 2, "o-rules": 2}
JOB_MET = ("repos-readable", "inventory-filled", "analysis-matched", "drafts-exist")
DOC_NAMES = {"architecture_md": "architecture.md", "api_yaml": "api.yaml", "screens_md": "screens.md", "glossary_yaml": "glossary.yaml"}
MAX_ENDPOINTS, MAX_SCREENS = 300, 200


def corr(product: str) -> str:
    return CORR_PREFIX + product


def _ev(cur, name: str, product: str, payload: dict, job_id: int | None = None) -> int:
    cur.execute("insert into events(name, correlation_id, job_id, payload) values (%s, %s, %s, %s) returning id",
                (name, f"product:{product}", job_id, Jsonb({"product": product, **payload})))
    return cur.fetchone()["id"]


def _last(cur, name: str, product: str) -> dict | None:
    cur.execute("select id, occurred_at, payload from events where name=%s and correlation_id=%s order by id desc limit 1",
                (name, f"product:{product}"))
    return cur.fetchone()


# ------------------------------------------------------------------ 積む
def _changed_since(st: dict, since_id: int) -> set[str]:
    out = set()
    for qid, a in st["answers"].items():
        if (a.get("event_id") or 0) > since_id:
            out.add(qid)
    for r in st["repos"]:
        if (r.get("event_id") or 0) > since_id:
            out.add("repos")
    return out


def _runs(cur, product: str) -> list[dict]:
    cur.execute("select j.id, j.state, j.enqueued_at, (e.payload->'input'->>'trigger_id')::bigint as trigger_id from jobs j "
                "left join events e on e.job_id=j.id and e.name='job.enqueued' where j.kind=%s and j.correlation_id=%s order by j.id desc",
                (KIND, corr(product)))
    return cur.fetchall()


def plan(cur, defs_i: dict, product: str, now: datetime) -> tuple[str, dict]:
    """その製品で次にすること。("run", 入力の材料) か ("assess", {}) か ("wait"|"none", {理由})"""
    st = intake.state(cur, defs_i, product)
    comp = intake.composition(st)
    if not comp["repos"]:
        return "none", {"reason": "リポがまだ渡されていない"}
    runs = _runs(cur, product)
    if any(r["state"] in ("queued", "running") for r in runs):
        return "wait", {"reason": "解析が流れている"}
    done_trigger = max([r["trigger_id"] or 0 for r in runs] or [0])
    req = _last(cur, "onboarding.run_requested", product)
    req_id = req["id"] if req else 0
    trigger = max(st["last_intake_id"], req_id)
    if trigger <= done_trigger:
        reassessed = _last(cur, "onboarding.reassessed", product)
        if st["last_intake_id"] > ((reassessed or {}).get("payload") or {}).get("intake_id", 0):
            return "assess", {}
        return "none", {"reason": "変わっていない"}
    changed = _changed_since(st, done_trigger)
    analysis_q = {q for q in changed if q in ANALYSIS_QUESTIONS or q.startswith(defs_i.get("analysis_question_prefix", "a-"))}
    if done_trigger and not analysis_q and req_id <= done_trigger:   # 解析に効かない答えだけ（通知の宛先など）
        reassessed = _last(cur, "onboarding.reassessed", product)
        if st["last_intake_id"] > ((reassessed or {}).get("payload") or {}).get("intake_id", 0):
            return "assess", {}
        return "none", {"reason": "変わっていない"}
    cur.execute("select max(occurred_at) as at from events where id=%s", (trigger,))
    at = cur.fetchone()["at"]
    if req_id < trigger and at and (now - at).total_seconds() < DEBOUNCE_SEC:   # 続けて答えているあいだは待つ（頼まれたときはすぐ）
        return "wait", {"reason": "答えが続いている"}
    today = [r for r in runs if r["enqueued_at"] and r["enqueued_at"] > now - timedelta(days=1)]
    if len(today) >= PER_DAY:
        return "wait", {"reason": f"この 1 日の解析が上限（{PER_DAY} 回）"}
    return "run", {"state": st, "composition": comp, "trigger_id": trigger}


def job_input(cur, defs_i: dict, st: dict, comp: dict, trigger_id: int, store_get=None) -> dict:
    """解析のジョブに渡す材料（制御プレーンの記録と、文のファイルの本文だけ）"""
    answers = {}
    for qid, a in st["answers"].items():
        if qid == "repos" or a.get("file") or a.get("stale"):
            continue
        answers[qid] = {"value": "（無い・分からない）" if a.get("skip") else a.get("value"), "note": a.get("note")}
    qs = {q["id"]: q for q in st["questions"]}
    files, budget = [], defs_i["limits"]["file_text_to_job"]
    for f in reversed(st["files"]):   # 新しいものから
        item = {"question": f["question"], "name": f["name"], "bytes": f["bytes"]}
        if f.get("text") and store_get and budget > 0:
            try:
                body = store_get(f["key"]).decode("utf-8", "replace")[:budget]
                item["text"] = body
                budget -= len(body)
            except Exception:  # noqa: BLE001  本文が読めなくても名前だけ渡す
                pass
        files.append(item)
    prev = (onboarding.latest(cur, st["product"]) or [None])[0]
    return {"product": st["product"], "trigger_id": trigger_id, "intake_id": st["last_intake_id"], "composition": comp,
            "answers": {k: {**v, "question": qs.get(k, {}).get("ja", k)} for k, v in answers.items()},
            "files": files, "previous_levels": (prev or {}).get("levels") or {},
            "previous_questions": [q for q in ((st.get("analysis") or {}).get("questions") or [])][:20]}


def enqueue(cur, model_tier: str, store_get=None, now: datetime | None = None) -> list[int]:
    """答えが変わった製品の解析を積む。解析に効かない変更は判定だけ測り直す。返り値は積んだジョブの id"""
    now = now or datetime.now(timezone.utc)
    try:
        defs_i = intake.load()
    except FileNotFoundError:
        return []
    out = []
    for product in intake.products(cur):
        what, info = plan(cur, defs_i, product, now)
        if what == "assess":
            assess(cur, readiness.load(), defs_i, product, by="onboarding（答えから）")
            continue
        if what != "run":
            continue
        comp, trigger = info["composition"], info["trigger_id"]
        inp = job_input(cur, defs_i, info["state"], comp, trigger, store_get)
        repo = comp["main"] or comp["repos"][0]["repo"]
        cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind) "
                    "values (%s,%s,%s,1,'onboarding',%s,%s) returning id", (corr(product), repo, -(ISSUE_BASE + trigger), model_tier, KIND))
        jid = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued', %s, %s, %s)",
                    (corr(product), jid, Jsonb({"by": "onboarding", "kind": KIND, "repo": repo, "attempt": 1, "input": inp})))
        _ev(cur, "onboarding.analysis_enqueued", product, {"trigger_id": trigger, "repos": [r["repo"] for r in comp["repos"]]}, jid)
        out.append(jid)
    return out


def job_repos(cur, job_id: int) -> list[str]:
    """解析のジョブが読むリポ（ジョブのトークンの範囲）"""
    cur.execute("select payload from events where name='job.enqueued' and job_id=%s order by id limit 1", (job_id,))
    r = cur.fetchone()
    comp = (((r or {}).get("payload") or {}).get("input") or {}).get("composition") or {}
    return [x["repo"] for x in comp.get("repos") or [] if intake.REPO.match(str(x.get("repo") or ""))]


# ------------------------------------------------------------------ 結果
def record(cur, job: dict, result: dict, state: str, err: str | None, put=None) -> int | None:
    """解析の結果を残し、判定を測り直す。返り値は onboarding.analyzed の id"""
    product = (job.get("correlation_id") or "")[len(CORR_PREFIX):]
    cur.execute("select payload from events where name='job.enqueued' and job_id=%s order by id limit 1", (job["id"],))
    inp = ((cur.fetchone() or {}).get("payload") or {}).get("input") or {}
    if state != "succeeded" or not isinstance(result.get("analysis"), dict):
        _ev(cur, "onboarding.analysis_failed", product, {"state": state, "error": (err or "")[:300], "trigger_id": inp.get("trigger_id")}, job["id"])
        return None
    a = result["analysis"]
    docs = {}
    for k, name in DOC_NAMES.items():
        body = (a.get("docs") or {}).get(k)
        if body and put:
            key = f"onboarding/{product}/{job['id']}/{name}"
            put(key, str(body).encode()[:400_000], "text/plain; charset=utf-8")
            docs[name] = key
    qs = [{"id": f"a-{job['id']}-{i + 1}", "question": str(q.get("question") or "")[:300], "why": str(q.get("why") or "")[:300],
           "concern": str(q.get("concern") or "")[:60]} for i, q in enumerate(a.get("questions") or []) if q.get("question")][:15]
    api = a.get("api") or {}
    payload = {"job_id": job["id"], "trigger_id": inp.get("trigger_id"), "intake_id": inp.get("intake_id"),
               "summary": str(a.get("summary") or "")[:2000], "repos": (a.get("repos") or [])[:20], "services": (a.get("services") or [])[:40],
               "api": {"source": api.get("source"), "endpoints_total": api.get("endpoints_total"), "matched_ratio": api.get("matched_ratio"),
                       "note": api.get("note"), "endpoints": (api.get("endpoints") or [])[:MAX_ENDPOINTS]},
               "screens": (a.get("screens") or [])[:MAX_SCREENS], "db": a.get("db"), "tests": (a.get("tests") or [])[:20],
               "design": a.get("design"), "network": a.get("network"), "glossary": (a.get("glossary") or [])[:80],
               "levels": {k: v for k, v in (a.get("levels") or {}).items() if k in BUILT_ITEMS},
               "met": [m for m in a.get("met") or [] if m in JOB_MET], "questions": qs, "docs": docs,
               "counts": {"repos": len(a.get("repos") or []), "endpoints": api.get("endpoints_total") or len(api.get("endpoints") or []),
                          "screens": len(a.get("screens") or []), "services": len(a.get("services") or [])},
               "confidence": a.get("confidence"), "cost_usd": result.get("cost_usd"), "model": result.get("model")}
    aid = _ev(cur, intake.ANALYZED, product, payload, job["id"])
    assess(cur, readiness.load(), intake.load(), product, by=f"onboard ジョブ {job['id']}")
    return aid


def _lv(v) -> int:
    return v.get("level", 0) if isinstance(v, dict) else int(v or 0)


def assess(cur, defs_r: dict, defs_i: dict, product: str, by: str) -> int | None:
    """前の判定に、答えから出した粒度と、いちばん新しい解析の粒度と条件を重ねて記録する"""
    prev = (onboarding.latest(cur, product) or [None])[0] or {}
    st = intake.state(cur, defs_i, product)
    an = st.get("analysis") or {}
    comp = intake.composition(st)
    items = {it["id"] for it in defs_r["items"]}
    levels: dict[str, dict] = {k: {"level": _lv(v), **((prev.get("evidence") or {}).get(k) or {})} for k, v in (prev.get("levels") or {}).items()
                               if k in items}
    for k, lv in intake.derive_levels(defs_i, st).items():
        if k in items and lv > levels.get(k, {}).get("level", 0):
            levels[k] = {"level": lv, "tool": "intake", "note": "事業主の答えから"}
    confirmed = (st["answers"].get("reading_confirmed") or {}).get("value") == "yes" and not st["answers"]["reading_confirmed"].get("stale")
    for k, v in (an.get("levels") or {}).items():
        lv = _lv(v)
        cap = 3 if confirmed and k in ("o-screens", "o-glossary") else CAP_WITHOUT_HUMAN.get(k, 3)
        lv = max(0, min(lv, cap))
        levels[k] = {"level": lv, "tool": "onboard", "note": (str(v.get("note"))[:200] if isinstance(v, dict) and v.get("note") else f"解析 {an.get('job_id')}")}
    met = set(prev.get("met") or []) | {m for m in an.get("met") or [] if m in JOB_MET}
    if comp["main"]:
        met.add("takeover-written")   # 工場の引き受け（担当のリポと扱い）が答えの repos に書かれた
    if confirmed and an:
        met.add("reading-confirmed")
    stack = prev.get("stack") or {}
    if an.get("repos"):
        stack = {"draft": True, "repos": [{"repo": r.get("repo"), "role": r.get("role"), "languages": r.get("languages"), "frameworks": r.get("frameworks"),
                                           "tests": [r["test"]] if r.get("test") else [], "note": r.get("note")} for r in an["repos"] if r.get("repo")],
                 "services": an.get("services") or []}
    sources = [s for s in prev.get("sources") or [] if s.get("service") not in ("GitHub", "書き出したファイル")]
    if comp["repos"]:
        writes = [r["repo"] for r in comp["repos"] if r["access"] == "write"]
        sources.append({"service": "GitHub", "use": "リポジトリ", "kind": "account",
                        "scope": f"読み取り {len(comp['repos']) - len(writes)} 本・書く {len(writes)} 本", "state": "connected"})
    if st["files"]:
        sources.append({"service": "書き出したファイル", "use": "資料", "kind": "export", "state": "export",
                        "note": f"{len(st['files'])} 件（原本とずれることがある）"})
    doc = {"product": product, "commit": "", "by": by, "note": f"答え {st['last_intake_id']}・解析 {an.get('job_id') or 'なし'}",
           "levels": levels, "met": sorted(met), "stack": stack, "sources": sources}
    rid, probs = onboarding.record(cur, defs_r, doc, by)
    if probs:
        _ev(cur, "onboarding.assess_failed", product, {"problems": probs[:10]})
        return None
    _ev(cur, "onboarding.reassessed", product, {"intake_id": st["last_intake_id"], "assessed_id": rid, "analysis_id": an.get("id")})
    return rid


def docs(cur, product: str) -> dict:
    an = intake.latest_analysis(cur, product) or {}
    return an.get("docs") or {}


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)
