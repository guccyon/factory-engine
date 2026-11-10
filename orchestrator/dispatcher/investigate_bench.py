"""工場の中での一次調査の評価セットを回して採点する（ADR 0048 の決定 8・順番 6）。

評価セットは eval-corpus/investigate/cases.yaml（過去に外から調べて原因が分かった件）。dispatcher のコンテナには eval-corpus がないので、
factoryctl investigation bench が標準入力で受け取る。
  enqueue(): 1 件ごとに調査役のジョブ（kind investigate、governance の枠）を積む。窓口の会話は作らない（correlation_id は invbench:<回>:<件>）。
             1 日の上限の勘定には入れない（人が回すもの）。調査役は直す前の Engine の版（case.engine_ref）の定義と文書を読む
  record():  結果を採点して investigate.bench_scored に残す。回の全件がそろったら investigate.bench_finished（特定率）
  trial():   最新の回の特定率が合格線（definitions/investigate.yaml の eval.pass_rate）に届くまでは True（窓口の報告に「試行中」と添える）
採点は決定論（LLM の審判は使わない）: 結果と直す先が合うこと、must_mention の各組から 1 語以上が報告・改善 Issue・仮説に出ること、
止めた理由の指定がある件はそのどれかであること。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import yaml
from psycopg.types.json import Jsonb

PREFIX = "invbench:"
PASS_RATE = 0.5               # definitions/investigate.yaml の eval.pass_rate の写し（食い違いはテストで見る）
ISSUE_BASE = 1_000_000        # 窓口の会話（-会話の id）と重ならない、評価のジョブの issue_number（-(ISSUE_BASE + 通し番号)）
REF = re.compile(r"^([0-9a-f]{7,40}|main)$")


def load_cases(text: str) -> list[dict]:
    d = yaml.safe_load(text) or {}
    out = []
    for c in d.get("cases") or []:
        if not c.get("id") or not c.get("question") or not isinstance(c.get("expected"), dict):
            raise ValueError(f"評価の件の形が足りない: {c.get('id')}")
        if c.get("engine_ref") and not REF.match(str(c["engine_ref"])):
            raise ValueError(f"engine_ref はコミットのハッシュか main: {c['id']}")
        out.append({**c, "product": c.get("product") or d.get("product")})
    return out


def _text(res: dict) -> str:
    parts = [res.get("report") or "", str(res.get("operation_proposal") or ""), str(res.get("product_request") or "")]
    iss = res.get("engine_issue") or {}
    parts += [iss.get("title") or "", iss.get("body") or ""]
    for h in (res.get("handoff") or {}).get("hypotheses") or []:
        parts.append(str(h.get("claim") or "") if isinstance(h, dict) else str(h))
    return "\n".join(parts).lower()


def score(case: dict, res: dict | None) -> dict:
    """1 件の採点。reasons は外れた理由（空なら合格）"""
    exp = case["expected"]
    res = res or {}
    reasons = []
    if not res.get("ok", True) or not res:
        reasons.append("調査役のジョブが結果を返さなかった")
    if res.get("outcome") != exp.get("outcome"):
        reasons.append(f"結果が {res.get('outcome')}（正解は {exp.get('outcome')}）")
    fixes = exp.get("fix_target")
    if fixes:
        fixes = fixes if isinstance(fixes, list) else [fixes]
        if (res.get("fix_target") or "none") not in fixes:
            reasons.append(f"直す先が {res.get('fix_target')}（正解は {'・'.join(fixes)}）")
    stops = exp.get("stop_reason")
    if stops:
        got = (res.get("handoff") or {}).get("stop_reason")
        if got not in (stops if isinstance(stops, list) else [stops]):
            reasons.append(f"止めた理由が {got}（正解は {'・'.join(stops if isinstance(stops, list) else [stops])}）")
    t = _text(res)
    for group in exp.get("must_mention") or []:
        if not any(str(w).lower() in t for w in group):
            reasons.append(f"原因の要素「{'・'.join(map(str, group))}」に触れていない")
    return {"pass": not reasons, "reasons": reasons}


def _event(cur, name: str, corr: str | None, payload: dict, dedupe_key: str | None = None, job_id: int | None = None) -> bool:
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values (%s,%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (name, corr, job_id, Jsonb(payload), dedupe_key))
    return cur.fetchone() is not None


def case_input(case: dict, run: str) -> dict:
    product = case["product"]
    return {"question": case["question"], "product": product, "engine_ref": case.get("engine_ref") or "main",
            "window": list(case.get("window") or []), "refs": case.get("refs") or [], "notes": "評価セットの再生（ADR 0048 の決定 8）",
            "limits": {"turns": 40, "usd": 3.0}, "bench": {"run": run, "case": case["id"]},
            "deny": sorted({product, product.split("/")[-1], product.split("/")[0]})}


def enqueue(cur, cases: list[dict], run: str | None = None, only: list[str] | None = None, model_tier: str = "tier-judge") -> dict:
    """評価の回を始める。返り値は {run, jobs: {件: job_id}}"""
    run = run or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    picked = [c for c in cases if not only or c["id"] in only]
    if not picked:
        raise ValueError("回す件がない")
    cur.execute("select count(*) as n from events where name='investigate.bench_enqueued'")
    seq = int(cur.fetchone()["n"])
    jobs = {}
    for i, c in enumerate(picked):
        corr = f"{PREFIX}{run}:{c['id']}"
        cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind) "
                    "values (%s,%s,%s,1,'bench',%s,'investigate') returning id", (corr, c["product"], -(ISSUE_BASE + seq + i), model_tier))
        jid = cur.fetchone()["id"]
        _event(cur, "job.enqueued", corr, {"by": "bench", "kind": "investigate", "repo": c["product"], "attempt": 1, "input": case_input(c, run)}, job_id=jid)
        _event(cur, "investigate.bench_enqueued", corr, {"run": run, "case": c["id"], "job_id": jid, "expected": c["expected"],
                                                         "of": len(picked)}, f"investigate.bench_enqueued:{run}:{c['id']}", job_id=jid)
        jobs[c["id"]] = jid
    return {"run": run, "jobs": jobs}


def record(cur, job: dict, result: dict | None, state: str, err: str | None) -> dict | None:
    corr = job["correlation_id"]
    cur.execute("select payload from events where name='investigate.bench_enqueued' and correlation_id=%s", (corr,))
    r = cur.fetchone()
    if not r:
        return None
    p = r["payload"]
    res = (result or {}) if state == "succeeded" else {"ok": False}
    s = score({"id": p["case"], "expected": p["expected"]}, res)
    _event(cur, "investigate.bench_scored", corr, {"run": p["run"], "case": p["case"], "pass": s["pass"], "reasons": s["reasons"],
                                                   "outcome": res.get("outcome"), "fix_target": res.get("fix_target"),
                                                   "stop_reason": (res.get("handoff") or {}).get("stop_reason"),
                                                   "cost_usd": res.get("cost_usd"), "num_turns": res.get("num_turns"), "error": (err or "")[:200]},
           f"investigate.bench_scored:{p['run']}:{p['case']}", job_id=job["id"])
    st = status(cur, p["run"])
    if st["done"] == st["of"]:
        _event(cur, "investigate.bench_finished", f"{PREFIX}{p['run']}", {"run": p["run"], "of": st["of"], "passed": st["passed"],
                                                                         "rate": st["rate"], "pass_rate": PASS_RATE, "cost_usd": st["cost_usd"]},
               f"investigate.bench_finished:{p['run']}")
    return s


def status(cur, run: str | None = None) -> dict:
    """回の進み（run を省くと最新の回）"""
    if not run:
        cur.execute("select payload->>'run' as run from events where name='investigate.bench_enqueued' order by id desc limit 1")
        r = cur.fetchone()
        if not r:
            return {"run": None, "of": 0, "done": 0, "passed": 0, "rate": None, "cost_usd": 0.0, "cases": []}
        run = r["run"]
    cur.execute("select e.payload->>'case' as case, (e.payload->>'of')::int as of, s.payload as scored from events e "
                "left join events s on s.name='investigate.bench_scored' and s.payload->>'run' = e.payload->>'run' and s.payload->>'case' = e.payload->>'case' "
                "where e.name='investigate.bench_enqueued' and e.payload->>'run'=%s order by e.id", (run,))
    rows = cur.fetchall()
    of = rows[0]["of"] if rows else 0
    done = [r for r in rows if r["scored"]]
    passed = sum(1 for r in done if r["scored"]["pass"])
    cost = sum(float(r["scored"].get("cost_usd") or 0) for r in done)
    return {"run": run, "of": of, "done": len(done), "passed": passed, "rate": round(passed / len(done), 3) if done else None,
            "cost_usd": round(cost, 2), "cases": [{"case": r["case"], **(r["scored"] or {"pass": None})} for r in rows]}


def trial(cur) -> dict | None:
    """試行中なら、最新の終わった回の特定率（回していなければ rate None）を返す。合格していれば None"""
    cur.execute("select payload from events where name='investigate.bench_finished' order by id desc limit 1")
    r = cur.fetchone()
    if r and float(r["payload"].get("rate") or 0) >= PASS_RATE:
        return None
    return {"rate": r["payload"].get("rate") if r else None, "pass_rate": PASS_RATE}
