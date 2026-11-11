"""出荷のレーンの QA の関門（ADR 0049）。staging に配備した版に、判定（E2E、mode: verdict）と探索（推論、mode: explore）を同時に積み、
両方の結果を 1 つの報告にまとめて承認に添える。関門は合否を決めない。ブロッカーの候補を挙げ、ブロッカーにするかは承認者が決める。

流れ（supervisor の qa_gate_step が周回ごとに step を呼ぶ。判断は持たず、記録を見て次の手を打つだけ）:
  release.deployed（staging・ok）→ open: qa.gate_opened。前の版の開いた関門は qa.gate_superseded
  → fill: 判定は固定 Issue factory:qa、探索は factory:qa-explore に積む（qa.gate_job。同じ Issue に動くジョブがあれば次の周回で）
  → 確かめられなかったジョブ（失敗・結果なし・判定が blocked だけ）は 1 回だけ積み直す
  → 両方の結果がそろうか、開いてから definitions/qa.yaml の gate.hoursたったら decide: qa.gate_decided（報告つき）と、判定の固定 Issue へのまとめ
  → 本番を立てる段階の製品は、受信トレイの「本番待ち」に報告を添える（inbox._release_item）。承認者は「本番に出す」か「見送る」（qa.gate_blocked）

記録（events）:
  qa.gate_opened      {release_id, repo, env}
  qa.gate_job         {release_id, repo, which: verdict|explore, job_id, attempt, charter?}
  qa.gate_superseded  {release_id, repo, by_release}
  qa.gate_decided     {release_id, repo, candidates, verdict: {...}, explore: {...}, report: [{label, text}], timed_out}
  qa.gate_blocked     {release_id, repo, by, via, note}
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

import qa_line

WHICH = ("verdict", "explore")
RECORD_GRACE = timedelta(minutes=10)   # ジョブが終わってから dispatcher が結果を記録するまでの猶予
TERMINAL = ("succeeded", "failed", "cancelled")
SEV_JA = {"s1": "S1", "s2": "S2", "s3": "S3", "s4": "S4"}


def _ev(cur, name: str, payload: dict, dedupe: str | None = None, now: datetime | None = None) -> bool:
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key, occurred_at) values (%s,%s,%s,%s,coalesce(%s::timestamptz, now())) "
                "on conflict (dedupe_key) do nothing returning id",
                (name, f"qa_gate:{payload['release_id']}", Jsonb(payload), dedupe, now))
    return cur.fetchone() is not None


# ------------------------------------------------------------------ 読む
def gate(cur, release_id: str) -> dict | None:
    """その版の関門の状態。{release_id, repo, opened_at, state: open|decided|superseded, jobs: {which: [qa.gate_job...]}, decided, blocked}"""
    cur.execute("select name, payload, occurred_at from events where correlation_id=%s and name like 'qa.gate_%%' order by id",
                (f"qa_gate:{release_id}",))
    rows = cur.fetchall()
    if not rows or rows[0]["name"] != "qa.gate_opened":
        return None
    g = {"release_id": release_id, "repo": rows[0]["payload"]["repo"], "opened_at": rows[0]["occurred_at"], "state": "open",
         "jobs": {w: [] for w in WHICH}, "decided": None, "blocked": None}
    for r in rows[1:]:
        p = r["payload"]
        if r["name"] == "qa.gate_job":
            g["jobs"][p["which"]].append(p)
        elif r["name"] == "qa.gate_decided":
            g["state"], g["decided"] = "decided", {**p, "at": r["occurred_at"]}
        elif r["name"] == "qa.gate_superseded" and g["state"] == "open":
            g["state"] = "superseded"
        elif r["name"] == "qa.gate_blocked":
            g["blocked"] = {**p, "at": r["occurred_at"]}
    return g


def open_gates(cur, repo: str) -> list[str]:
    cur.execute("select payload->>'release_id' as rid from events o where name='qa.gate_opened' and payload->>'repo'=%s and not exists "
                "(select 1 from events e where e.correlation_id=o.correlation_id and e.name in ('qa.gate_decided','qa.gate_superseded')) order by id",
                (repo,))
    return [r["rid"] for r in cur.fetchall()]


def recent(cur, repo: str | None = None, limit: int = 5) -> list[dict]:
    cur.execute("select payload->>'release_id' as rid from events where name='qa.gate_opened' and (%s::text is null or payload->>'repo'=%s) "
                "order by id desc limit %s", (repo, repo, limit))
    return [g for g in (gate(cur, r["rid"]) for r in cur.fetchall()) if g]


def to_open(cur, repo: str) -> str | None:
    """staging に最後に配備した版が、いま staging にいて、まだ関門を開いていなければその版。
    シードの入れ直し・再起動は release.deployed を残さないので開かない。Rollback で戻った版は最後の配備ではないので開かない"""
    import ship
    cur.execute("select payload->>'release_id' as rid from events where name='release.deployed' and payload->>'repo'=%s "
                "and payload->>'env'='staging' and (payload->>'ok')::boolean order by id desc limit 1", (repo,))
    r = cur.fetchone()
    if not r:
        return None
    if (ship.status(cur, repo)["environments"].get("staging") or {}).get("release_id") != r["rid"]:
        return None
    cur.execute("select 1 from events where name='qa.gate_opened' and payload->>'release_id'=%s", (r["rid"],))
    return None if cur.fetchone() else r["rid"]


# ------------------------------------------------------------------ 結果の読み方
def _job(cur, job_id: int) -> dict | None:
    cur.execute("select id, state, finished_at, error from jobs where id=%s", (job_id,))
    return cur.fetchone()


def verdict_outcome(cur, job_id: int, now: datetime) -> dict | None:
    """判定のジョブの結果。まだ終わっていなければ None。
    {status: done|unconfirmed, candidates: [...], notes: [...], stories: {decision: n}, why?}"""
    j = _job(cur, job_id)
    if not j or j["state"] not in TERMINAL:
        return None
    cur.execute("select id, payload from artifacts where type='QAVerdict' and job_id=%s order by id", (job_id,))
    vs = cur.fetchall()
    if j["state"] != "succeeded" or (not vs and j["finished_at"] and now - j["finished_at"] > RECORD_GRACE):
        return {"status": "unconfirmed", "why": f"ジョブ {job_id} は {j['state']}" + (f"（{str(j['error'])[:120]}）" if j.get("error") else "")
                if j["state"] != "succeeded" else f"ジョブ {job_id} の判定が記録されていない", "candidates": [], "notes": [], "stories": {}}
    if not vs:
        return None   # 終わったが dispatcher がまだ記録していない
    cur.execute("select payload from events where job_id=%s and name in ('qa.defect_filed','qa.defect_seen')", (job_id,))
    issues = {}
    for r in cur.fetchall():
        p = r["payload"]
        issues.setdefault((p.get("verdict_id"), p.get("title")), p.get("issue"))
    candidates, notes, stories = [], [], {}
    for a in vs:
        v = a["payload"]
        stories[v["decision"]] = stories.get(v["decision"], 0) + 1
        if v["decision"] == "blocked":
            notes.append({"what": "確かめられなかった", "story_id": v["story_id"], "title": str(v.get("interpretation") or "")[:120]})
        for d in v.get("defects") or [] if v["decision"] == "hold" else []:
            item = {"from": "判定", "story_id": v["story_id"], "title": str(d.get("title") or "")[:120], "severity": d.get("severity"),
                    "viewpoint": d.get("viewpoint"), "issue": issues.get((a["id"], d.get("title")))}
            if d.get("known_gap"):
                notes.append({**item, "what": "known_gap"})
            else:
                candidates.append(item)
    only_blocked = bool(vs) and stories.get("blocked", 0) == len(vs)
    return {"status": "unconfirmed" if only_blocked else "done", "why": "全ストーリーが blocked" if only_blocked else None,
            "candidates": candidates, "notes": notes, "stories": stories}


def explore_outcome(cur, job_id: int, now: datetime, blocker_severities=("s1", "s2")) -> dict | None:
    j = _job(cur, job_id)
    if not j or j["state"] not in TERMINAL:
        return None
    cur.execute("select payload from events where name='qa.explore' and job_id=%s order by id desc limit 1", (job_id,))
    r = cur.fetchone()
    if j["state"] != "succeeded" or (not r and j["finished_at"] and now - j["finished_at"] > RECORD_GRACE):
        return {"status": "unconfirmed", "why": f"ジョブ {job_id} は {j['state']}" if j["state"] != "succeeded" else f"ジョブ {job_id} の探索が記録されていない",
                "candidates": [], "notes": [], "tried": 0}
    if not r:
        return None
    p = r["payload"]
    candidates, notes = [], []
    for d in p.get("defects") or []:
        item = {"from": "探索", "story_id": d.get("story_id"), "title": str(d.get("title") or "")[:120], "severity": d.get("severity"),
                "viewpoint": d.get("viewpoint"), "issue": d.get("issue")}
        if d.get("severity") in blocker_severities and not d.get("known_gap"):
            candidates.append(item)
        else:
            notes.append({**item, "what": "known_gap" if d.get("known_gap") else "参考"})
    return {"status": "done", "why": None, "candidates": candidates, "notes": notes, "tried": p.get("tried") or 0,
            "charter": p.get("charter"), "flags": len(p.get("flags") or [])}




# ------------------------------------------------------------------ 動かす
def next_charter(cur, repo: str, defs: dict) -> str:
    """その製品で最後に使ったチャーターの次（夜間の探索と同じ 5 種を回す）"""
    cs = [c["id"] if isinstance(c, dict) else c for c in defs["explore"]["charters"]]
    cur.execute("select payload->>'charter' as c from events where name='qa.explore' and payload->>'repo'=%s order by id desc limit 1", (repo,))
    r = cur.fetchone()
    last = r["c"] if r else None
    return cs[(cs.index(last) + 1) % len(cs)] if last in cs else cs[0]


def open_gate(cur, repo: str, release_id: str, now: datetime | None = None) -> bool:
    """関門を開く。前の版の開いた関門は superseded で閉じる（承認の対象は staging にいまある版だけ）"""
    for old in open_gates(cur, repo):
        if old != release_id:
            _ev(cur, "qa.gate_superseded", {"release_id": old, "repo": repo, "by_release": release_id}, f"qa.gate_superseded:{old}", now)
    return _ev(cur, "qa.gate_opened", {"release_id": release_id, "repo": repo, "env": "staging"}, f"qa.gate_opened:{release_id}", now)


def enqueue_job(cur, gh, repo: str, release_id: str, which: str, attempt: int, defs: dict, now: datetime) -> dict:
    """関門のジョブを 1 つ積む。同じ固定 Issue に動いているジョブがあれば psycopg の UniqueViolation（次の周回で）"""
    if which == "verdict":
        got = qa_line.enqueue(cur, gh, repo, by="qa_gate", priority=(defs.get("gate") or qa_line.DEFS["gate"])["priority"], defs=defs,
                              extra={"gate": release_id})
        extra = {}
    else:
        charter = next_charter(cur, repo, defs)
        got = qa_line.enqueue(cur, gh, repo, by="qa_gate", priority=(defs.get("gate") or qa_line.DEFS["gate"])["priority"], defs=defs,
                              issue_label=defs["act"]["explore_issue_label"],
                              extra={"mode": "explore", "charter": charter, "night": qa_line.night_of(now), "unattended": False, "gate": release_id})
        extra = {"charter": charter}
    _ev(cur, "qa.gate_job", {"release_id": release_id, "repo": repo, "which": which, "job_id": got["job_id"], "attempt": attempt, **extra},
        f"qa.gate_job:{release_id}:{which}:{attempt}", now)
    return got


def step(cur, gh, repo: str, *, defs: dict | None = None, now: datetime | None = None, may_enqueue: bool = True,
         on_enqueued=None, log=print) -> dict:
    """周回ごとに 1 回。開く・積む・積み直す・まとめる。返り値 {opened, enqueued: [..], decided}"""
    import psycopg
    defs = defs or qa_line.load_defs()
    now = now or datetime.now(timezone.utc)
    gd = {**qa_line.DEFS["gate"], **(defs.get("gate") or {})}   # hours（結果を待つ時間）・attempts（積み直しを含む回数）・priority・blocker_severities
    outcome = {"verdict": verdict_outcome,
               "explore": lambda c, j, n: explore_outcome(c, j, n, tuple(gd["blocker_severities"]))}
    out = {"opened": None, "enqueued": [], "decided": None}
    rid = to_open(cur, repo)
    if rid and may_enqueue and open_gate(cur, repo, rid, now):
        out["opened"] = rid
    for rid in open_gates(cur, repo):
        g = gate(cur, rid)
        results = {}
        for which in WHICH:
            jobs = g["jobs"][which]
            res = outcome[which](cur, jobs[-1]["job_id"], now) if jobs else None
            if res and res["status"] == "unconfirmed" and len(jobs) < int(gd["attempts"]) and may_enqueue:
                jobs, res = jobs + [None], None   # 積み直す（下で）
            if (not jobs or jobs[-1] is None) and may_enqueue:
                try:
                    got = enqueue_job(cur, gh, repo, rid, which, len([j for j in jobs if j]) + 1, defs, now)
                    out["enqueued"].append({"which": which, **got})
                    if on_enqueued:
                        on_enqueued(got)
                except psycopg.errors.UniqueViolation:
                    pass   # 同じ固定 Issue の QA のジョブが待ち・実行中。次の周回で
                except Exception as e:  # noqa: BLE001  次の周回でやり直す
                    log("qa gate enqueue failed", repo, rid, which, repr(e)[:200])
            results[which] = res
        timed_out = now - g["opened_at"] > timedelta(hours=float(gd["hours"]))
        if all(results.get(w) for w in WHICH) or timed_out:
            out["decided"] = decide(cur, gh, repo, rid, results, timed_out, now)
    return out


# ------------------------------------------------------------------ まとめる
def _line(c: dict) -> str:
    sev = SEV_JA.get(c.get("severity"), c.get("severity") or "-")
    where = f"（{c['story_id']}）" if c.get("story_id") else ""
    return f"[{c['from']}・{sev}] {c['title']}{where}" + (f" {c['issue']}" if c.get("issue") else "")


def report(results: dict, timed_out: bool) -> list[dict]:
    """受信トレイの detail（{label, text}）の形の報告"""
    cands = [c for w in WHICH for c in (results.get(w) or {}).get("candidates") or []]
    v, x = results.get("verdict"), results.get("explore")
    rows = [{"label": "ブロッカーの候補", "text": f"{len(cands)} 件" + ("。ブロッカーにするかは承認者が決める" if cands else "（見つからなかった）")}]
    rows += [{"label": f"候補 {i + 1}", "text": _line(c)} for i, c in enumerate(cands[:8])]
    if v:
        st = "・".join(f"{qa_line.DECISION_JA.get(k, k)} {n}" for k, n in sorted(v.get("stories", {}).items()))
        rows.append({"label": "判定（E2E）", "text": (st or "-") if v["status"] == "done" else f"確かめられなかった: {v.get('why')}"})
    else:
        rows.append({"label": "判定（E2E）", "text": "結果がそろわなかった（時間切れ）" if timed_out else "まだ"})
    if x:
        rows.append({"label": "探索（推論）", "text": f"チャーター {x.get('charter')}・試したこと {x.get('tried')} 件・見つけたもの "
                     f"{len(x['candidates']) + len(x['notes'])} 件" if x["status"] == "done" else f"確かめられなかった: {x.get('why')}"})
    else:
        rows.append({"label": "探索（推論）", "text": "結果がそろわなかった（時間切れ）" if timed_out else "まだ"})
    notes = [n for w in WHICH for n in (results.get(w) or {}).get("notes") or []]
    if notes:
        rows.append({"label": "参考", "text": "；".join((f"[{n['what']}] " + (n.get("title") or n.get("story_id") or ""))[:100] for n in notes[:6])
                     + (f" ほか {len(notes) - 6} 件" if len(notes) > 6 else "")})
    return rows


def decide(cur, gh, repo: str, release_id: str, results: dict, timed_out: bool, now: datetime | None = None) -> dict:
    cands = [c for w in WHICH for c in (results.get(w) or {}).get("candidates") or []]
    rep = report(results, timed_out)
    payload = {"release_id": release_id, "repo": repo, "candidates": len(cands), "blockers": cands,
               "verdict": results.get("verdict"), "explore": results.get("explore"), "report": rep, "timed_out": timed_out}
    if not _ev(cur, "qa.gate_decided", payload, f"qa.gate_decided:{release_id}", now):
        return payload
    try:   # 判定の固定 Issue にもまとめを書く（本番を立てない製品はここと記録だけが見える先）
        n = qa_line.find_issue(gh, repo, qa_line.DEFS["act"]["issue_label"])
        if n:
            body = [f"## 出荷の QA の関門（版 {release_id}、factory-engine の ADR 0049）", ""]
            body += [f"- **{r['label']}**: {r['text']}" for r in rep]
            body += ["", "関門は合否を決めません。ブロッカーにするかは承認者が決めます（本番を立てない段階の製品では記録だけ）。"]
            gh("POST", f"repos/{repo}/issues/{n}/comments", {"body": "\n".join(body)})
    except Exception:  # noqa: BLE001  まとめは events に残っている
        pass
    return payload


def block(cur, release_id: str, by: str, via: str, note: str) -> tuple[bool, str]:
    """承認者が候補をブロッカーにして、この版を本番に出さないと決めた（受信トレイの「見送る」）"""
    g = gate(cur, release_id)
    if not g:
        return False, "この版には QA の関門がない"
    if not (note or "").strip():
        return False, "どれをブロッカーにしたか、一言が要る"
    _ev(cur, "qa.gate_blocked", {"release_id": release_id, "repo": g["repo"], "by": by, "via": via, "note": note.strip()[:500]},
        f"qa.gate_blocked:{release_id}")
    return True, f"{release_id} を本番に出さないことにした（修正は次の便で）"


def approval_check(cur, release_id: str, skip_qa: str | None) -> tuple[bool, str, dict]:
    """本番に出す承認の前の条件（ADR 0049 の決定 4）。関門が開いたまま（報告の前）なら --skip-qa の理由が要る。
    返り値 (通すか, 理由, release.approved に足すもの)"""
    g = gate(cur, release_id)
    if g and g["state"] == "decided":
        return True, "", {"qa_gate": {"candidates": g["decided"].get("candidates"), "blocked": bool(g["blocked"])}}
    if g and g["state"] == "open":
        if not (skip_qa or "").strip():
            return False, "QA の関門の報告がまだ（判定と探索の結果を待っている）。待たずに出すときは --skip-qa --reason .. で理由を付ける", {}
        return True, "", {"qa_skipped": skip_qa.strip()[:300]}
    return True, "", ({"qa_skipped": skip_qa.strip()[:300]} if (skip_qa or "").strip() else {"qa_gate": None})
