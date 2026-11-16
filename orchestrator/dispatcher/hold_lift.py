"""S1 の差し止めを、直しが本番に出たのを QA 役が確かめ直して解く（ADR 0062）。

supervisor の周回ごとに step を呼ぶ。差し止めの Incident（演習でないもの）ごとに:
  1. Defect の Issue が閉じて ADR 0053 の緊急便に載った（major.rereleased）
  2. その便の版（かその後の版）が、本番を立てる製品では本番に、そうでなければ staging に出ている
  3. QA 役が staging で再現の手順をたどり直し（QA のジョブの mode: recheck）、直ったと答えた（確信度 CONFIDENCE_MIN 以上）
差し止めの全部の Incident が直ったら incident.qa_lift で解く。直っていない・確かめられないときは解かずに知らせる。

events:
  qa.recheck_enqueued {incident_id, repo, issue, job_id, attempt, release_id}
  qa.rechecked        {incident_id, repo, issue, job_id, attempt, fixed: yes|no|unconfirmed, confidence, saw}
  qa.recheck_gave_up  {incident_id, repo, issue, fixed, why}   # 直っていない・積み直しても確かめられない。人の解除を待つ

LLM は使わない（判断は QA 役のジョブの答え）。失敗しても supervisor の周回は止めない（呼び出し側で握る）。
"""
from __future__ import annotations

import os

import incident
import major_defect
import ship
import stoppage
import train
from psycopg.types.json import Jsonb

ENABLED = os.environ.get("HOLD_AUTO_LIFT", "1") != "0"
CONFIDENCE_MIN = 0.7
ATTEMPTS = 2           # unconfirmed は 1 回だけ積み直す（ADR 0049 の関門の attempts と同じ）
BODY_MAX = 6000        # QA 役に渡す Defect の Issue の本文の上限（再現・期待・実際の節が入る長さ）
FIXED = ("yes", "no", "unconfirmed")


def _ev(cur, name: str, payload: dict, dedupe: str | None = None) -> bool:
    cur.execute("insert into events(name, payload, dedupe_key) values (%s,%s,%s) on conflict (dedupe_key) do nothing returning id",
                (name, Jsonb(payload), dedupe))
    return cur.fetchone() is not None


def _rank(cur, repo: str) -> dict[str, int]:
    cur.execute("select payload->>'release_id' as rid from events where name='release.created' and payload->>'repo'=%s order by id", (repo,))
    return {r["rid"]: i for i, r in enumerate(cur.fetchall())}


def fix_live(cur, inc: dict) -> str | None:
    """条件 1・2: 直しを載せた緊急便の版（かその後の版）が出ていれば、その環境に出ている版の id。まだなら None"""
    rr = major_defect.rereleased(cur, inc["incident_id"])
    if not rr:
        return None
    t = train.train_of(cur, rr["train_id"])
    if not t:
        return None
    repo = inc["repo"]
    rels = [r["release_id"] for r in t["releases"] if r["repo"] == repo]
    if not rels:
        return None
    env = "staging" if ship.prod_held(cur, repo) else "prod"
    live = (ship.status(cur, repo)["environments"].get(env) or {}).get("release_id")
    rank = _rank(cur, repo)
    if not live or rank.get(live, -1) < max(rank.get(r, 10**9) for r in rels):
        return None
    return live


def rechecks(cur, iid: str) -> list[dict]:
    cur.execute("select e.payload, j.state, j.result from events e left join jobs j on j.id = (e.payload->>'job_id')::bigint "
                "where e.name='qa.recheck_enqueued' and e.payload->>'incident_id'=%s order by e.id", (iid,))
    return [{**r["payload"], "state": r["state"], "result": r["result"]} for r in cur.fetchall()]


def answer_of(result: dict | None, num: int | None) -> dict | None:
    """確かめ直しのジョブの結果から、その Issue の答え {fixed, confidence, saw}。形が崩れていれば unconfirmed"""
    checks = ((result or {}).get("recheck") or {}).get("checks") or []
    for c in checks:
        if isinstance(c, dict) and c.get("issue") == num:
            fixed = c.get("fixed") if c.get("fixed") in FIXED else "unconfirmed"
            conf = c.get("confidence") if isinstance(c.get("confidence"), (int, float)) and not isinstance(c.get("confidence"), bool) else 0
            if fixed == "yes" and conf < CONFIDENCE_MIN:
                fixed = "unconfirmed"
            return {"fixed": fixed, "confidence": conf, "saw": str(c.get("saw") or "")[:500]}
    return None


def defect_input(gh, repo: str, num: int) -> dict:
    it = gh("GET", f"repos/{repo}/issues/{num}") or {}
    return {"issue": num, "title": str(it.get("title") or "")[:200], "body": str(it.get("body") or "")[:BODY_MAX]}


def enqueue(cur, gh, inc: dict, num: int, attempt: int, live: str) -> int | None:
    import psycopg
    import qa_line
    repo = inc["repo"]
    try:
        got = qa_line.enqueue(cur, gh, repo, by="qa_lift", priority="p0", title=f"QA: 直しの確かめ直し #{num}",
                              extra={"mode": "recheck", "defects": [defect_input(gh, repo, num)], "incident_id": inc["incident_id"]},
                              **({"unit": f"recheck:{num}:{attempt}"} if qa_line.unit_ready(cur) else {}))
    except psycopg.errors.UniqueViolation:
        return None   # 同じ固定 Issue で別の QA が動いている。次の周回で積む
    _ev(cur, "qa.recheck_enqueued", {"incident_id": inc["incident_id"], "repo": repo, "issue": num, "job_id": got["job_id"],
                                     "attempt": attempt, "release_id": live}, f"qa.recheck_enqueued:{inc['incident_id']}:{attempt}")
    return got["job_id"]


def _comment(gh, repo: str, num: int, body: str):
    try:
        gh("POST", f"repos/{repo}/issues/{num}/comments", {"body": body})
    except Exception:  # noqa: BLE001, S110  記録は events にある
        pass


def gave_up(cur, iid: str) -> dict | None:
    cur.execute("select payload from events where name='qa.recheck_gave_up' and payload->>'incident_id'=%s limit 1", (iid,))
    r = cur.fetchone()
    return r["payload"] if r else None


def check_incident(cur, gh, inc: dict, log=None) -> dict:
    """1 件の Incident を進める。返り値 {state: waiting | checking | fixed | not_fixed | unconfirmed, ...}"""
    iid, repo, num = inc["incident_id"], inc["repo"], major_defect.issue_number(inc.get("issue"))
    if num is None:
        return {"state": "waiting", "why": "Defect の Issue がない"}
    g = gave_up(cur, iid)
    if g:
        return {"state": "not_fixed" if g.get("fixed") == "no" else "unconfirmed", "issue": num}
    live = fix_live(cur, inc)
    if not live:
        return {"state": "waiting", "why": "直しがまだ出ていない"}
    rs = rechecks(cur, iid)
    last = rs[-1] if rs else None
    if last and last["state"] in ("queued", "running"):
        return {"state": "checking", "job_id": last["job_id"]}
    if last:
        cur.execute("select 1 from events where name='qa.rechecked' and payload->>'job_id'=%s", (str(last["job_id"]),))
        ans = (answer_of(last["result"], num) if last["state"] == "succeeded" else None) or {"fixed": "unconfirmed", "confidence": 0,
                                                                                            "saw": f"ジョブが {last['state']} で終わった"}
        if cur.fetchone() is None:
            _ev(cur, "qa.rechecked", {"incident_id": iid, "repo": repo, "issue": num, "job_id": last["job_id"], "attempt": last["attempt"], **ans},
                f"qa.rechecked:{last['job_id']}")
        if ans["fixed"] == "yes":
            return {"state": "fixed", "issue": num, "job_id": last["job_id"], "release_id": last.get("release_id"), **ans}
        if ans["fixed"] == "no" or len(rs) >= ATTEMPTS:
            _ev(cur, "qa.recheck_gave_up", {"incident_id": iid, "repo": repo, "issue": num, "fixed": ans["fixed"], "why": ans["saw"]},
                f"qa.recheck_gave_up:{iid}")
            if ans["fixed"] == "no":
                _comment(gh, repo, num, f"QA 役が直しを確かめ直しました（ジョブ {last['job_id']}、バージョン {(last.get('release_id') or '-').split('@')[-1]}）。"
                                        f"**直っていませんでした。** ラインの差し止めは解いていません（ADR 0062）。\n\n> {ans['saw']}")
                stoppage.post_notice(f"**差し止めを解けません**: QA 役が確かめ直したところ、{inc.get('issue')} は直っていませんでした。"
                                     "直し直すか、人が確かめて解除してください。", cur)
            else:
                stoppage.post_notice(f"**差し止めを解けません**: QA 役が {inc.get('issue')} の直しを確かめられませんでした（{ans['saw'][:200]}）。"
                                     "人が確かめて解除してください（操業コンソールの「差し止めを解除」）。", cur)
            return {"state": "not_fixed" if ans["fixed"] == "no" else "unconfirmed", "issue": num}
    job = enqueue(cur, gh, inc, num, len(rs) + 1, live)
    if log:
        log("hold lift recheck", iid, job)
    return {"state": "checking", "job_id": job}


def step(cur, gh, log=None) -> dict | None:
    """差し止め中なら 1 周進める。解いたら {lifted: True, ...}"""
    if not ENABLED:
        return None
    st = incident.hold_state(cur)
    if not st:
        return None
    incs = st.get("incidents") or []
    if not incs or any(i.get("drill") for i in incs):
        return None   # 演習は人が終える（drill-end と人の解除）
    opened = {i["incident_id"]: i for i in incident.open_incidents(cur)}
    states = {}
    for i in incs:
        inc = opened.get(i.get("incident_id"))
        if not inc:   # 人が先に閉じた Incident は直ったものとして扱う
            states[i.get("incident_id")] = {"state": "fixed", "issue": major_defect.issue_number(i.get("issue")), "closed_by_human": True}
            continue
        states[inc["incident_id"]] = check_incident(cur, gh, inc, log)
    if not all(s["state"] == "fixed" for s in states.values()):
        return {"lifted": False, "states": states}
    checks = [{"incident_id": k, **{x: v.get(x) for x in ("issue", "job_id", "release_id", "fixed", "confidence", "saw") if v.get(x) is not None}}
              for k, v in states.items()]
    nums = "・".join(f"#{c['issue']}" for c in checks if c.get("issue"))
    reason = f"直しが出たのを QA 役が staging で確かめ直した（{nums}）"
    if not incident.qa_lift(cur, list(states), reason, checks):
        return {"lifted": False, "states": states}
    for c in checks:
        inc = opened.get(c["incident_id"]) or {}
        if c.get("job_id") and c.get("issue"):
            _comment(gh, inc.get("repo") or "", c["issue"],
                     f"QA 役が直しを確かめ直しました（ジョブ {c['job_id']}、バージョン {(c.get('release_id') or '-').split('@')[-1]}、確信度 {c.get('confidence')}）。"
                     f"**直っていたので、ラインの差し止めを解きました**（ADR 0062）。\n\n> {c.get('saw') or ''}")
    stoppage.post_notice(f"**QA 役が差し止めを解きました**: {reason}。ほかのチケットも流れ始めます（ADR 0062）。", cur)
    if log:
        log("hold lifted by qa", reason)
    return {"lifted": True, "states": states, "reason": reason}
