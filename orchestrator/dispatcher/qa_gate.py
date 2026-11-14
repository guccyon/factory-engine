"""出荷のレーンの QA の関門（ADR 0049）。staging に配備した版に、判定（E2E、mode: verdict）と探索（推論、mode: explore）を同時に積み、
両方の結果を 1 つの報告にまとめて承認に添える。関門は合否を決めない。ブロッカーの候補を挙げ、ブロッカーにするかは承認者が決める。

流れ（supervisor の qa_gate_step が周回ごとに step を呼ぶ。判断は持たず、記録を見て次の手を打つだけ）:
  release.deployed（staging・ok）→ open: qa.gate_opened。前の版の開いた関門は qa.gate_superseded
  → fill: 判定は固定 Issue factory:qa、探索は factory:qa-explore に積む（qa.gate_job。同じ Issue に動くジョブがあれば次の周回で）
  → 時刻をまたぐストーリー（specs/stories の time があるもの）は判定から外し、判定と探索が両方終わってから
    「時刻の判定」（which: verdict_time）として同じ判定の固定 Issue に積む（ADR 0055 の決定 3。時計を合わせる間、環境を 1 つのジョブだけが使う）。
    どれを外すかは開いた最初の周回で 1 回だけ決めて qa.gate_plan に残す
  → 確かめられなかったジョブ（失敗・結果なし・判定が blocked だけ）は 1 回だけ積み直す
  → 両方の結果がそろうか、開いてから definitions/qa.yaml の gate.hoursたったら decide: qa.gate_decided（報告つき）と、判定の固定 Issue へのまとめ
  → 本番を立てる段階の製品は、受信トレイの「本番待ち」に報告を添える（inbox._release_item）。承認者は「本番に出す」か「見送る」（qa.gate_blocked）

版ごとのチケットとシナリオごとのジョブ（ADR 0054）:
  計画（qa.gate_plan）を残すとき、版ごとの Issue（factory:qa-release、題「QA: <版>」）を 1 つ作り、シナリオごとの単位（units）を決める。
  判定はストーリー 1 本につき 1 ジョブ、探索はチャーター 1 つにつき 1 ジョブ、時刻をまたぐストーリーも 1 本につき 1 ジョブを、
  単位（jobs.unit）を付けてその Issue に積む。判定は同時に gate.parallel 本まで。時刻をまたぐストーリーは、ほかの単位が全部終わってから 1 本ずつ流す。
  確かめられなかった単位だけを 1 回積み直す。そろったら報告をその Issue に書いて閉じる。版のチケットの費用の上限（ジョブの上限の合計 × 係数）に
  当たったら、残りを積まずにそろった分で報告を作る（人への停止は開かない）。
  単位の列がない DB（009_job_unit.sql の前）・ストーリーを数えられなかった版・この形の前に計画を残した版は、今までの形（固定 Issue）で動く。

記録（events）:
  qa.gate_opened      {release_id, repo, env}
  qa.gate_plan        {release_id, repo, time_stories, other_stories?, error?, issue?, corr?, cap_usd?, units?: [{unit, which, story? | charter?}], legacy_why?}
  qa.gate_job         {release_id, repo, which: verdict|explore|verdict_time, job_id, attempt, charter?, stories?, unit?, story?}
  qa.gate_superseded  {release_id, repo, by_release}
  qa.gate_decided     {release_id, repo, candidates, verdict: {...}, explore: {...}, verdict_time?: {...}, report: [{label, text}], timed_out}
  qa.gate_blocked     {release_id, repo, by, via, note}
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import yaml
from psycopg.types.json import Jsonb

import qa_line
import workflow_guard

WHICH = ("verdict", "explore")   # 並行に流すもの
TIME = "verdict_time"             # 時刻をまたぐストーリーの判定。WHICH が終わってから 1 つだけで流す（ADR 0055）
ALL = WHICH + (TIME,)
LABEL_JA = {"verdict": "判定（E2E）", "explore": "探索（推論）", TIME: "判定（時刻をまたぐ）"}
STORIES_DIR = "specs/stories"
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
    """その版の関門の状態。{release_id, repo, opened_at, state: open|decided|superseded, jobs: {which: [qa.gate_job...]}, plan, decided, blocked}"""
    cur.execute("select name, payload, occurred_at from events where correlation_id=%s and name like 'qa.gate_%%' order by id",
                (f"qa_gate:{release_id}",))
    rows = cur.fetchall()
    if not rows or rows[0]["name"] != "qa.gate_opened":
        return None
    g = {"release_id": release_id, "repo": rows[0]["payload"]["repo"], "opened_at": rows[0]["occurred_at"], "state": "open", "opened": rows[0]["payload"],
         "jobs": {w: [] for w in ALL}, "units": {}, "plan": None, "decided": None, "blocked": None}
    for r in rows[1:]:
        p = r["payload"]
        if r["name"] == "qa.gate_job":
            g["jobs"][p["which"]].append(p)
            if p.get("unit"):
                g["units"].setdefault(p["unit"], []).append(p)
        elif r["name"] == "qa.gate_plan":
            g["plan"] = p
        elif r["name"] == "qa.gate_decided":
            g["state"], g["decided"] = "decided", {**p, "at": r["occurred_at"]}
        elif r["name"] == "qa.gate_superseded" and g["state"] == "open":
            g["state"] = "superseded"
        elif r["name"] == "qa.gate_blocked":
            g["blocked"] = {**p, "at": r["occurred_at"]}
    return g


def open_gates(cur, repo: str) -> list[str]:
    cur.execute("select payload->>'release_id' as rid from events o where name='qa.gate_opened' and payload->>'repo'=%s and not exists "
                "(select 1 from events e where e.correlation_id=o.correlation_id and e.name in ('qa.gate_decided','qa.gate_superseded')) "
                "and o.payload->>'ticket' is null order by id", (repo,))   # QA だけのチケットの関門（ADR 0056）は ticket_step が進める
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




# ------------------------------------------------------------------ 時刻をまたぐストーリーを分ける（ADR 0055 の決定 3）
def time_stories(gh, repo: str) -> tuple[list[str], list[str]]:
    """製品の specs/stories/ の active なユーザーストーリーを、time があるもの・ないものに分ける（id の並び）。読めなければ例外"""
    listing = gh("GET", f"repos/{repo}/contents/{STORIES_DIR}") or []
    if not isinstance(listing, list):
        raise RuntimeError(f"{STORIES_DIR} の一覧が読めない")
    with_time, without = [], []
    for f in sorted((x for x in listing if isinstance(x, dict) and x.get("type") == "file"), key=lambda x: x.get("name") or ""):
        if not str(f.get("name") or "").endswith(".yaml") or f["name"] == "actors.yaml":
            continue
        got = gh("GET", f"repos/{repo}/contents/{f['path']}") or {}
        doc = yaml.safe_load(base64.b64decode(got.get("content") or "").decode("utf-8")) if isinstance(got, dict) else None
        if not isinstance(doc, dict) or doc.get("type") != "UserStory" or doc.get("status", "active") != "active" or not doc.get("id"):
            continue
        (with_time if doc.get("time") else without).append(str(doc["id"]))
    return with_time, without


def plan(cur, gh, g: dict, now: datetime, log=print, defs: dict | None = None, gd: dict | None = None) -> dict:
    """その版の関門で、時刻をまたぐストーリーをどれにするか。最初の周回で 1 回だけ決めて qa.gate_plan に残す。
    読めなかったときは分けない（time_stories が空、other_stories は None＝判定は今までどおり全部）。
    ストーリーを数えられ、単位の列があれば、版ごとの Issue とシナリオごとの単位も決める（ADR 0054。plan_units）"""
    if g.get("plan") is not None:
        return g["plan"]
    rid, repo = g["release_id"], g["repo"]
    try:
        with_time, without = time_stories(gh, repo)
        p = {"release_id": rid, "repo": repo, "time_stories": with_time, "other_stories": without}
    except Exception as e:  # noqa: BLE001  分けられないだけで、関門は今までどおり流す
        log("qa gate plan failed", repo, rid, repr(e)[:200])
        p = {"release_id": rid, "repo": repo, "time_stories": [], "other_stories": None, "error": repr(e)[:200]}
    if defs is not None and gd is not None:
        p.update(plan_units(cur, gh, p, defs, gd))
    _ev(cur, "qa.gate_plan", p, f"qa.gate_plan:{rid}", now)
    g["plan"] = p
    return p


# ------------------------------------------------------------------ 動かす
def next_charter(cur, repo: str, defs: dict) -> str:
    """その製品で最後に使ったチャーターの次（夜間の探索と同じ 5 種を回す）"""
    cs = [c["id"] if isinstance(c, dict) else c for c in defs["explore"]["charters"]]
    cur.execute("select payload->>'charter' as c from events where name='qa.explore' and payload->>'repo'=%s order by id desc limit 1", (repo,))
    r = cur.fetchone()
    last = r["c"] if r else None
    return cs[(cs.index(last) + 1) % len(cs)] if last in cs else cs[0]


def open_gate(cur, repo: str, release_id: str, now: datetime | None = None, gh=None) -> bool:
    """関門を開く。前の版の開いた関門は superseded で閉じる（承認の対象は staging にいまある版だけ）。版ごとの Issue があれば閉じる"""
    for old in open_gates(cur, repo):
        if old != release_id:
            pl = (gate(cur, old) or {}).get("plan") or {}
            if _ev(cur, "qa.gate_superseded", {"release_id": old, "repo": repo, "by_release": release_id}, f"qa.gate_superseded:{old}", now) \
                    and gh and pl.get("issue"):
                close_issue(gh, repo, pl["issue"], f"次の版 {release_id} が staging に出たので、この版の QA は打ち切りました。")
    return _ev(cur, "qa.gate_opened", {"release_id": release_id, "repo": repo, "env": "staging"}, f"qa.gate_opened:{release_id}", now)


# ------------------------------------------------------------------ 版ごとのチケットとシナリオごとのジョブ（ADR 0054）
UNIT_COST = {"verdict": "story_cost_usd", "explore": "explore_cost_usd", TIME: "time_story_cost_usd"}


def close_issue(gh, repo: str, issue: int, body: str | None = None):
    """版ごとの Issue に書いて閉じる。失敗しても関門は進める（記録は events にある）"""
    try:
        if body:
            gh("POST", f"repos/{repo}/issues/{issue}/comments", {"body": body})
        gh("PATCH", f"repos/{repo}/issues/{issue}", {"state": "closed"})
    except Exception:  # noqa: BLE001
        pass


def ticket_cap(units: list[dict], gd: dict) -> float:
    """版のチケットの費用の上限 = ジョブの上限の合計 × 係数（積み直しの分）"""
    return round(sum(float(gd[UNIT_COST[u["which"]]]) for u in units) * float(gd["ticket_cap_factor"]), 2)


def plan_units(cur, gh, p: dict, defs: dict, gd: dict) -> dict:
    """計画に足すもの。シナリオごとの単位と版ごとの Issue（issue・corr・cap_usd・units）。積めないときは理由だけ（legacy_why。今までの形で動く）"""
    rid, repo = p["release_id"], p["repo"]
    stories, timed = list(p.get("other_stories") or []), list(p.get("time_stories") or [])
    if not qa_line.unit_ready(cur):
        return {"legacy_why": "jobs に単位の列がない（009_job_unit.sql の前）"}
    if not stories and not timed:
        return {"legacy_why": "active なストーリーを数えられなかった"}
    charter = next_charter(cur, repo, defs)
    units = [{"unit": f"verdict:{s}", "which": "verdict", "story": s} for s in stories]
    units.append({"unit": f"explore:{charter}", "which": "explore", "charter": charter})
    units += [{"unit": f"{TIME}:{s}", "which": TIME, "story": s} for s in timed]   # 時刻をまたぐストーリーは最後に 1 本ずつ（ADR 0055 の決定 3）
    cap = ticket_cap(units, gd)
    try:
        body = (f"工場の QA 役が、staging に出た版 {rid} をシナリオごとに確かめます（factory-engine の ADR 0049・0054）。\n\n"
                f"- 判定: ストーリー {len(stories) + len(timed)} 本（1 本につき 1 ジョブ"
                + (f"。うち時刻をまたぐもの {len(timed)} 本は最後に流す" if timed else "") + "）\n"
                f"- 探索: チャーター {charter}\n- 費用の上限: ${cap:.2f}\n\n"
                "結果がそろったら、ここに報告を書いて閉じます。見つけた Defect は別の Issue（factory:qa-defect）に起票します。")
        issue = gh("POST", f"repos/{repo}/issues", {"title": f"QA: {rid.split('@')[-1]}", "body": body, "labels": [gd["release_issue_label"]]})["number"]
    except Exception as e:  # noqa: BLE001
        return {"legacy_why": f"版ごとの Issue を作れなかった: {repr(e)[:120]}"}
    return {"issue": issue, "corr": f"github:{repo}#{issue}", "cap_usd": cap, "units": units}


def enqueue_unit(cur, gh, repo: str, pl: dict, u: dict, attempt: int, defs: dict, gd: dict, now: datetime) -> dict:
    """計画の単位を 1 つ、版ごとの Issue に積む。ジョブごとの上限（ターン・費用）は入力で渡す（ワーカーの core-gates/qa.py が守らせる）"""
    rid = pl["release_id"]
    common = dict(by="qa_gate", priority=gd["priority"], defs=defs, issue=pl["issue"], unit=u["unit"], title=pl.get("title") or f"QA: {rid}")
    if u["which"] == "explore":
        got = qa_line.enqueue(cur, gh, repo, **common,
                              extra={"mode": "explore", "charter": u["charter"], "night": qa_line.night_of(now), "unattended": False, "gate": rid,
                                     "max_budget_usd": float(gd["explore_cost_usd"])})
        extra = {"charter": u["charter"]}
    else:
        turns = gd["time_story_max_turns"] if u["which"] == TIME else gd["story_max_turns"]
        got = qa_line.enqueue(cur, gh, repo, stories=[u["story"]], **common, exclusive_env=(u["which"] == TIME),
                              extra={"gate": rid, "max_turns": int(turns), "max_budget_usd": float(gd[UNIT_COST[u["which"]]])})
        extra = {"story": u["story"], "stories": [u["story"]]}
    _ev(cur, "qa.gate_job", {"release_id": rid, "repo": repo, "which": u["which"], "unit": u["unit"], "job_id": got["job_id"], "attempt": attempt, **extra},
        f"qa.gate_job:{rid}:{u['unit']}:{attempt}", now)
    return got


def merge_verdicts(units: list[dict], res: dict, which: str) -> dict | None:
    """ストーリーごとの判定の結果を、今までの判定 1 本の形にまとめる。missing は結果のそろわなかったストーリー"""
    vs = [(u, res.get(u["unit"])) for u in units if u["which"] == which]
    if not vs:
        return None
    out = {"status": "unconfirmed", "why": None, "candidates": [], "notes": [], "stories": {}, "missing": []}
    for u, r in vs:
        if not r or r["status"] != "done":
            out["missing"].append({"story_id": u["story"], "why": (r or {}).get("why") or "結果がそろわなかった（時間切れ）"})
        if not r:
            continue
        if r["status"] == "done":
            out["status"] = "done"
        out["candidates"] += r.get("candidates") or []
        out["notes"] += r.get("notes") or []
        for k, n in (r.get("stories") or {}).items():
            out["stories"][k] = out["stories"].get(k, 0) + n
    if out["status"] != "done":
        out["why"] = "どのストーリーも確かめられなかった"
    return out


def step_units(cur, gh, repo: str, g: dict, defs: dict, gd: dict, now: datetime, may_enqueue: bool, on_enqueued, log, outcome: dict) -> tuple[list, dict | None]:
    """シナリオごとの単位のある関門の 1 周。返り値（積んだもの, そろったときの結果 {verdict, explore, verdict_time?, capped, timed_out} か None）"""
    import psycopg
    pl, enq, res = g["plan"], [], {}
    left = workflow_guard.cost_left(cur, pl["corr"])["ticket.cost_usd"]
    capped = left["cap"] > 0 and left["left"] <= 0   # 版のチケットの費用の上限。残りを積まず、そろった分で報告を作る
    last = {u["unit"]: (g["units"].get(u["unit"]) or [None])[-1] for u in pl["units"]}
    done = {u["unit"]: (outcome[u["which"]](cur, last[u["unit"]]["job_id"], now) if last[u["unit"]] else None) for u in pl["units"]}
    running = {w: sum(1 for u in pl["units"] if u["which"] == w and last[u["unit"]] and done[u["unit"]] is None) for w in ALL}
    limit = {"verdict": int(gd["parallel"]), "explore": len(pl["units"]), TIME: 1}   # 判定は端末の数まで。時刻をまたぐものは 1 本ずつ
    for u in pl["units"]:   # 並びは 判定 → 探索 → 時刻をまたぐもの
        k, w, tried, r = u["unit"], u["which"], len(g["units"].get(u["unit"]) or []), done[u["unit"]]
        want = tried == 0 or (bool(r) and r["status"] == "unconfirmed" and tried < int(gd["attempts"]))
        alone = w != TIME or all(res.get(o["unit"]) for o in pl["units"] if o["which"] != TIME)   # 時計を合わせる間、環境を 1 つのジョブだけが使う
        if want and capped:
            r = r or {"status": "unconfirmed", "why": "費用の上限で流せなかった", "candidates": [], "notes": [], "stories": {}, "tried": 0}
        elif want and may_enqueue and alone and running[w] < limit[w]:
            r = None
            try:
                got = enqueue_unit(cur, gh, repo, pl, u, tried + 1, defs, gd, now)
                enq.append({"which": w, "unit": k, **got})
                running[w] += 1
                if on_enqueued:
                    on_enqueued(got)
            except psycopg.errors.UniqueViolation:
                pass   # 同じ単位のジョブが待ち・実行中（手で積み直したなど）。次の周回で
            except Exception as e:  # noqa: BLE001  次の周回でやり直す
                log("qa gate enqueue failed", repo, pl["release_id"], k, repr(e)[:200])
        elif want:
            r = None   # 順番待ち（同時に流す数・ほかの単位の終わり・予算の枠）
        res[k] = r
    timed_out = now - g["opened_at"] > timedelta(hours=float(gd["hours"]))
    if not timed_out and any(v is None for v in res.values()):
        return enq, None
    out = {"verdict": merge_verdicts(pl["units"], res, "verdict"), "capped": capped, "timed_out": timed_out,
           "explore": next((res.get(u["unit"]) for u in pl["units"] if u["which"] == "explore"), None)}
    if any(u["which"] == TIME for u in pl["units"]):
        out[TIME] = merge_verdicts(pl["units"], res, TIME)
    if out["verdict"] is None:   # 全部が時刻をまたぐ
        out["verdict"] = {"status": "done", "why": None, "candidates": [], "notes": [], "stories": {}}
    return enq, out


def enqueue_job(cur, gh, repo: str, release_id: str, which: str, attempt: int, defs: dict, now: datetime,
                stories: list[str] | None = None) -> dict:
    """関門のジョブを 1 つ積む。stories は判定で流すストーリー（なければ全部）。同じ固定 Issue に動いているジョブがあれば
    psycopg の UniqueViolation（次の周回で）"""
    if which in ("verdict", TIME):
        got = qa_line.enqueue(cur, gh, repo, by="qa_gate", priority=(defs.get("gate") or qa_line.DEFS["gate"])["priority"], defs=defs,
                              stories=stories, extra={"gate": release_id}, exclusive_env=(which == TIME))
        extra = {"stories": list(stories)} if stories else {}
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
    outcome = {"verdict": verdict_outcome, TIME: verdict_outcome,
               "explore": lambda c, j, n: explore_outcome(c, j, n, tuple(gd["blocker_severities"]))}
    out = {"opened": None, "enqueued": [], "decided": None}
    rid = to_open(cur, repo)
    if rid and may_enqueue and open_gate(cur, repo, rid, now, gh):
        out["opened"] = rid
    for rid in open_gates(cur, repo):
        g = gate(cur, rid)
        pl = plan(cur, gh, g, now, log, defs, gd) if may_enqueue else (g.get("plan") or {"time_stories": [], "other_stories": None})
        if pl.get("units"):   # 版ごとのチケットとシナリオごとのジョブ（ADR 0054）
            enq, results = step_units(cur, gh, repo, g, defs, gd, now, may_enqueue, on_enqueued, log, outcome)
            out["enqueued"] += enq
            if results:
                out["decided"] = decide(cur, gh, repo, rid, results, results.pop("timed_out"), now, plan=pl)
            continue
        time_ids, other_ids = list(pl.get("time_stories") or []), pl.get("other_stories")
        results = {}

        def run(which, stories=None):
            jobs = g["jobs"][which]
            res = outcome[which](cur, jobs[-1]["job_id"], now) if jobs else None
            if res and res["status"] == "unconfirmed" and len(jobs) < int(gd["attempts"]) and may_enqueue:
                jobs, res = jobs + [None], None   # 積み直す（下で）
            if (not jobs or jobs[-1] is None) and may_enqueue:
                try:
                    got = enqueue_job(cur, gh, repo, rid, which, len([j for j in jobs if j]) + 1, defs, now, stories=stories)
                    out["enqueued"].append({"which": which, **got})
                    if on_enqueued:
                        on_enqueued(got)
                except psycopg.errors.UniqueViolation:
                    pass   # 同じ固定 Issue の QA のジョブが待ち・実行中。次の周回で
                except Exception as e:  # noqa: BLE001  次の周回でやり直す
                    log("qa gate enqueue failed", repo, rid, which, repr(e)[:200])
            return res

        for which in WHICH:
            if which == "verdict" and time_ids and not other_ids:
                results[which] = {"status": "done", "why": None, "candidates": [], "notes": [], "stories": {}}   # 全部が時刻をまたぐ
                continue
            results[which] = run(which, other_ids if which == "verdict" and time_ids else None)
        need = WHICH
        if time_ids:   # 時刻をまたぐストーリーは、判定と探索が終わってから 1 つだけで流す（ADR 0055 の決定 3）
            need = ALL
            results[TIME] = run(TIME, time_ids) if all(results.get(w) for w in WHICH) else None
        timed_out = now - g["opened_at"] > timedelta(hours=float(gd["hours"]))
        if all(results.get(w) for w in need) or timed_out:
            out["decided"] = decide(cur, gh, repo, rid, results, timed_out, now)
    out["tickets"] = ticket_step(cur, gh, repo, defs=defs, gd=gd, now=now, may_enqueue=may_enqueue, on_enqueued=on_enqueued, log=log, outcome=outcome)
    return out


# ------------------------------------------------------------------ まとめる
def _line(c: dict) -> str:
    sev = SEV_JA.get(c.get("severity"), c.get("severity") or "-")
    where = f"（{c['story_id']}）" if c.get("story_id") else ""
    return f"[{c['from']}・{sev}] {c['title']}{where}" + (f" {c['issue']}" if c.get("issue") else "")


def report(results: dict, timed_out: bool) -> list[dict]:
    """受信トレイの detail（{label, text}）の形の報告"""
    cands = [c for w in ALL for c in (results.get(w) or {}).get("candidates") or []]
    v, x = results.get("verdict"), results.get("explore")
    rows = [{"label": "ブロッカーの候補", "text": f"{len(cands)} 件" + ("。ブロッカーにするかは承認者が決める" if cands else "（見つからなかった）")}]
    rows += [{"label": f"候補 {i + 1}", "text": _line(c)} for i, c in enumerate(cands[:8])]
    if v:
        st = "・".join(f"{qa_line.DECISION_JA.get(k, k)} {n}" for k, n in sorted(v.get("stories", {}).items()))
        rows.append({"label": "判定（E2E）", "text": (st or "-") if v["status"] == "done" else f"確かめられなかった: {v.get('why')}"})
    else:
        rows.append({"label": "判定（E2E）", "text": "結果がそろわなかった（時間切れ）" if timed_out else "まだ"})
    missing = [m for w in ("verdict", TIME) for m in (results.get(w) or {}).get("missing") or []]
    if missing:   # シナリオごとのジョブ（ADR 0054）: 結果のそろわなかったストーリーを名前で書く
        rows.append({"label": "そろわなかったシナリオ", "text": "；".join(f"{m['story_id']}（{str(m['why'])[:60]}）" for m in missing[:8])
                     + (f" ほか {len(missing) - 8} 件" if len(missing) > 8 else "")})
    if results.get("capped"):
        rows.append({"label": "費用の上限", "text": "版のチケットの費用の上限に当たったので、残りのシナリオは流していない"})
    if x:
        rows.append({"label": "探索（推論）", "text": f"チャーター {x.get('charter')}・試したこと {x.get('tried')} 件・見つけたもの "
                     f"{len(x['candidates']) + len(x['notes'])} 件" if x["status"] == "done" else f"確かめられなかった: {x.get('why')}"})
    else:
        rows.append({"label": "探索（推論）", "text": "結果がそろわなかった（時間切れ）" if timed_out else "まだ"})
    if TIME in results:   # 時刻をまたぐストーリーを分けた版だけ（ADR 0055）
        t = results[TIME]
        if t:
            st = "・".join(f"{qa_line.DECISION_JA.get(k, k)} {n}" for k, n in sorted(t.get("stories", {}).items()))
            rows.append({"label": LABEL_JA[TIME], "text": (st or "-") if t["status"] == "done" else f"確かめられなかった: {t.get('why')}"})
        else:
            rows.append({"label": LABEL_JA[TIME], "text": "結果がそろわなかった（時間切れ）" if timed_out else "まだ"})
    notes = [n for w in ALL for n in (results.get(w) or {}).get("notes") or []]
    if notes:
        rows.append({"label": "参考", "text": "；".join((f"[{n['what']}] " + (n.get("title") or n.get("story_id") or ""))[:100] for n in notes[:6])
                     + (f" ほか {len(notes) - 6} 件" if len(notes) > 6 else "")})
    return rows


def decide(cur, gh, repo: str, release_id: str, results: dict, timed_out: bool, now: datetime | None = None, plan: dict | None = None) -> dict:
    cands = [c for w in ALL for c in (results.get(w) or {}).get("candidates") or []]
    rep = report(results, timed_out)
    payload = {"release_id": release_id, "repo": repo, "candidates": len(cands), "blockers": cands,
               "verdict": results.get("verdict"), "explore": results.get("explore"), "report": rep, "timed_out": timed_out,
               **({"capped": True} if results.get("capped") else {}), **({"issue": plan["issue"]} if plan else {})}
    if TIME in results:
        payload[TIME] = results[TIME]
    if not _ev(cur, "qa.gate_decided", payload, f"qa.gate_decided:{release_id}", now):
        return payload
    body = [f"## 出荷の QA の関門（版 {release_id}、factory-engine の ADR 0049）", ""]
    body += [f"- **{r['label']}**: {r['text']}" for r in rep]
    body += ["", "関門は合否を決めません。ブロッカーにするかは承認者が決めます（本番を立てない段階の製品では記録だけ）。"]
    if plan:   # 版ごとの Issue に報告を書いて閉じる（ADR 0054 の決定 2）。チケットの費用の集計もここで締まる
        close_issue(gh, repo, plan["issue"], "\n".join(body))
        return payload
    try:   # 判定の固定 Issue にもまとめを書く（本番を立てない製品はここと記録だけが見える先）
        n = qa_line.find_issue(gh, repo, qa_line.DEFS["act"]["issue_label"])
        if n:
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
        return False, "どれをブロッカーにしたか、コメントが要る"
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


# ------------------------------------------------------------------ QA だけのチケット（ADR 0056）
# プランナーが道 verify（stage → qa → respond → learn）で着手できると決めたチケットに、dispatcher が factory:qa-ticket を付ける。
# 指示役（supervisor）は周回ごとに、宣言（Plan の workflow）を読んで次の手を打つだけ。判断は持たない（ADR 0004）。
#   stage: 宣言が動かす（既定）で、マージ済みで版にしていない変更があれば、版を切って staging に出す。出した版の関門（ADR 0049）の結果を使う
#   qa: 新しい版を出さなかった（か、出した版の関門が使えなかった）ら、いまの staging に、このチケットを入れ物にした関門を開き、
#       scope のストーリー 1 本につき 1 ジョブ（ADR 0054。時刻をまたぐものは最後に 1 本ずつ、ADR 0055）、deep なら探索 1 つを積む
#   respond: 結果をチケットにコメントして閉じる。承認・本番にはつなげない
# 記録の鍵は qa_gate:ticket:<repo>#<番号>（release_id の欄にこの鍵を入れる。payload の ticket で版の関門と見分ける）
#   qa.gate_opened {release_id: 鍵, repo, env, ticket, staged_release?, on_release?, decl}
TICKET_LABEL = "factory:qa-ticket"


def ticket_key(repo: str, n: int) -> str:
    return f"ticket:{repo}#{n}"


def verify_decl(cur, corr: str) -> dict | None:
    """最後の着手できる判定が道 verify なら、その宣言（stage・stories・explore・cap_usd）。dispatcher の planner.verify_scope と同じ読み方"""
    cur.execute("select payload from artifacts where type='Plan' and correlation_id=%s and payload->>'decision'='ready' order by id desc limit 1", (corr,))
    r = cur.fetchone()
    wf = ((r or {}).get("payload") or {}).get("workflow") or {}
    if wf.get("path") != "verify":
        return None
    steps = {s.get("step"): s for s in wf.get("steps") or [] if isinstance(s, dict)}
    qa = steps.get("qa") or {}
    return {"stage": (steps.get("stage") or {}).get("run", True) is True, "stories": [str(x) for x in qa.get("scope") or []],
            "explore": qa.get("effort") == "deep", "cap_usd": (wf.get("caps_declared") or {}).get("ticket.cost_usd")}


def _staging_release(cur, repo: str) -> str | None:
    import ship
    return (ship.status(cur, repo)["environments"].get("staging") or {}).get("release_id")


def _comment(gh, repo: str, n: int, body: str):
    try:
        gh("POST", f"repos/{repo}/issues/{n}/comments", {"body": body})
    except Exception:  # noqa: BLE001  記録は events にある
        pass


def stage_release(cur, repo: str, n: int, log=print) -> str | None:
    """マージ済みで版にしていない変更があれば、版を切って staging への出荷を依頼する（factoryctl env up と同じ）。出した版の id"""
    import ship
    if not ship.unreleased(cur, repo):
        return None
    try:
        rid, _prs = ship.create_release(cur, repo, ship.next_version(cur, repo))
        if not rid:
            return None
        ok, m = ship.request_ship(cur, rid, f"qa_ticket:{repo}#{n}", "QA だけのチケット（ADR 0056）")
        return rid if ok else None
    except Exception as e:  # noqa: BLE001  出せなければ今の staging で流す
        log("qa ticket stage failed", repo, n, repr(e)[:200])
        return None


def open_ticket(cur, gh, repo: str, n: int, decl: dict, now: datetime, log=print) -> bool:
    key = ticket_key(repo, n)
    staged = stage_release(cur, repo, n, log) if decl["stage"] else None
    on = _staging_release(cur, repo)
    if not _ev(cur, "qa.gate_opened", {"release_id": key, "repo": repo, "env": "staging", "ticket": n, "staged_release": staged,
                                       "on_release": on, "decl": decl}, f"qa.gate_opened:{key}", now):
        return False
    stories = "、".join(decl["stories"]) if decl["stories"] else "全部"
    if staged:
        _comment(gh, repo, n, f"工場の QA 役: マージ済みの変更を版 {staged.split('@')[-1]} にして staging に出します。"
                              f"出た版の QA（出荷の関門）の結果から、このチケットのストーリー（{stories}）の結果を抜き出して返します（ADR 0056）。")
    else:
        _comment(gh, repo, n, f"工場の QA 役: いま staging にある版 {(on or '-').split('@')[-1]} で、ストーリー（{stories}）を流します"
                              + ("。探索（推論）も 1 つ流します" if decl["explore"] else "") + "（ADR 0056）。")
    return True


def ticket_plan(cur, gh, g: dict, n: int, decl: dict, defs: dict, gd: dict, now: datetime, log=print, note: str | None = None) -> dict:
    """このチケットで流す単位。最初の周回で 1 回だけ決めて qa.gate_plan に残す。チケットの上限は ジョブの上限の合計 × 係数 と宣言の小さいほう"""
    if g.get("plan") is not None:
        return g["plan"]
    key, repo = g["release_id"], g["repo"]
    p = {"release_id": key, "repo": repo, "ticket": n, "issue": n, "corr": f"github:{repo}#{n}", "title": f"QA だけ: #{n}",
         "time_stories": [], "other_stories": None}
    if note:
        p["note"] = note
    try:
        with_time, without = time_stories(gh, repo)
        want = set(decl["stories"])
        if want:
            p["unknown"] = sorted(want - set(with_time) - set(without))
            with_time, without = [s for s in with_time if s in want], [s for s in without if s in want]
        p["time_stories"], p["other_stories"] = with_time, without
    except Exception as e:  # noqa: BLE001  数えられなければ宣言のまま 1 ジョブで流す
        p["error"] = repr(e)[:200]
        p["other_stories"] = list(decl["stories"]) or None
    units = [{"unit": f"verdict:{s}", "which": "verdict", "story": s} for s in p["other_stories"] or []]
    if decl["explore"]:
        c = next_charter(cur, repo, defs)
        units.append({"unit": f"explore:{c}", "which": "explore", "charter": c})
    units += [{"unit": f"{TIME}:{s}", "which": TIME, "story": s} for s in p["time_stories"]]
    if units and qa_line.unit_ready(cur) and not p.get("error"):
        cap = ticket_cap(units, gd)
        if isinstance(decl.get("cap_usd"), (int, float)) and decl["cap_usd"] > 0:
            cap = min(cap, float(decl["cap_usd"]))
        p.update({"units": units, "cap_usd": cap})
    else:
        p["legacy_why"] = "jobs に単位の列がない（009_job_unit.sql の前）" if units else "流すストーリーがない"
        p["cap_usd"] = round(float(gd["story_cost_usd"]) * max(1, len(units)) * float(gd["ticket_cap_factor"]), 2)
    _ev(cur, "qa.gate_plan", p, f"qa.gate_plan:{key}", now)
    g["plan"] = p
    return p


def staged_results(cur, rg: dict, decl: dict, now: datetime, outcome: dict) -> dict:
    """出した版の関門の結果から、このチケットのストーリーの分を抜き出す。版の関門が単位の形でなければ、まとめをそのまま使う"""
    pl = rg.get("plan") or {}
    if not pl.get("units"):
        d = rg["decided"] or {}
        return {"verdict": d.get("verdict"), "explore": d.get("explore"), **({TIME: d[TIME]} if d.get(TIME) else {}), "whole": True}
    want = set(decl["stories"])
    units = [u for u in pl["units"] if u["which"] != "explore" and (not want or u["story"] in want)]
    if decl["explore"]:
        units += [u for u in pl["units"] if u["which"] == "explore"]
    res = {}
    for u in units:
        jobs = rg["units"].get(u["unit"]) or []
        res[u["unit"]] = outcome[u["which"]](cur, jobs[-1]["job_id"], now) if jobs else None
    out = {"verdict": merge_verdicts(units, res, "verdict"), "explore": next((res.get(u["unit"]) for u in units if u["which"] == "explore"), None)}
    if any(u["which"] == TIME for u in units):
        out[TIME] = merge_verdicts(units, res, TIME)
    if out["verdict"] is None:
        out["verdict"] = {"status": "done", "why": None, "candidates": [], "notes": [], "stories": {}}
    return out


def legacy_ticket(cur, gh, repo: str, g: dict, pl: dict, defs: dict, gd: dict, now: datetime, may_enqueue: bool, on_enqueued, log) -> tuple[list, dict | None]:
    """単位の列がない DB では、流すストーリーを 1 ジョブでこのチケットに積む（同じ Issue に動けるジョブは 1 つ）"""
    import psycopg
    jobs, enq = g["jobs"]["verdict"], []
    res = verdict_outcome(cur, jobs[-1]["job_id"], now) if jobs else None
    retry = bool(res) and res["status"] == "unconfirmed" and len(jobs) < int(gd["attempts"])
    if (not jobs or retry) and may_enqueue:
        stories = list(pl.get("other_stories") or []) + list(pl.get("time_stories") or [])   # 空なら全ストーリー
        try:
            got = qa_line.enqueue(cur, gh, repo, stories=stories or None, by="qa_ticket", priority=gd["priority"], defs=defs, issue=pl["issue"],
                                  title=pl.get("title"), extra={"gate": g["release_id"]}, exclusive_env=bool(pl.get("time_stories")))
            _ev(cur, "qa.gate_job", {"release_id": g["release_id"], "repo": repo, "which": "verdict", "job_id": got["job_id"], "attempt": len(jobs) + 1,
                                     "stories": stories}, f"qa.gate_job:{g['release_id']}:verdict:{len(jobs) + 1}", now)
            enq.append({"which": "verdict", **got})
            if on_enqueued:
                on_enqueued(got)
            res = None
        except psycopg.errors.UniqueViolation:
            res = None
        except Exception as e:  # noqa: BLE001
            log("qa ticket enqueue failed", repo, g["release_id"], repr(e)[:200])
            res = None
    elif retry:
        res = None
    timed_out = now - g["opened_at"] > timedelta(hours=float(gd["hours"]))
    if res is None and not timed_out:
        return enq, None
    return enq, {"verdict": res, "explore": None, "timed_out": timed_out, "capped": False}


def ticket_decide(cur, gh, repo: str, g: dict, results: dict, timed_out: bool, now: datetime, via: str | None = None) -> dict:
    """チケットに報告を書いて閉じる。承認・受信トレイにはつなげない"""
    n, key = g["opened"]["ticket"], g["release_id"]
    on = _staging_release(cur, repo)
    began = g["opened"].get("staged_release") or g["opened"].get("on_release")
    rep = report(results, timed_out)
    if (g.get("plan") or {}).get("unknown"):
        rep.append({"label": "製品にないストーリー", "text": "、".join(g["plan"]["unknown"]) + "（流していない）"})
    if (g.get("plan") or {}).get("note"):
        rep.append({"label": "メモ", "text": g["plan"]["note"]})
    if results.get("whole"):
        rep.append({"label": "注", "text": "出した版の QA がストーリーごとの形でなかったので、版全体の結果をそのまま載せた"})
    if began and on and began != on and not via:
        rep.append({"label": "注", "text": f"流している間に staging の版が {began.split('@')[-1]} から {on.split('@')[-1]} に替わった"})
    cands = [c for w in ALL for c in (results.get(w) or {}).get("candidates") or []]
    payload = {"release_id": key, "repo": repo, "ticket": n, "candidates": len(cands), "blockers": cands, "verdict": results.get("verdict"),
               "explore": results.get("explore"), "report": rep, "timed_out": timed_out, "on_release": began, **({"via_release": via} if via else {})}
    if TIME in results:
        payload[TIME] = results[TIME]
    if not _ev(cur, "qa.gate_decided", payload, f"qa.gate_decided:{key}", now):
        return payload
    rep[0] = {"label": "見つけた不具合（S1・S2 の候補）", "text": f"{len(cands)} 件" + ("" if cands else "（見つからなかった）")}
    body = [f"## QA だけのチケットの結果（staging の版 {(began or '-').split('@')[-1]}、factory-engine の ADR 0056）", ""]
    body += [f"- **{r['label']}**: {r['text']}" for r in rep]
    if via:
        body += ["", f"結果は、出した版の QA（出荷の関門）から抜き出しました。"]
    body += ["", "見つけた不具合は別のチケット（factory:qa-defect）に起票しています。本番への出荷には何もしていません。"]
    close_issue(gh, repo, n, "\n".join(body))
    return payload


def ticket_step(cur, gh, repo: str, *, defs: dict, gd: dict, now: datetime, may_enqueue: bool, on_enqueued, log, outcome: dict) -> list[dict]:
    """QA だけのチケットを 1 周進める。返り値は動いたこと（開いた・積んだ・閉じた）"""
    import urllib.parse
    try:
        issues = gh("GET", f"repos/{repo}/issues?state=open&labels={urllib.parse.quote(TICKET_LABEL)}&per_page=50") or []
    except Exception as e:  # noqa: BLE001
        log("qa ticket list failed", repo, repr(e)[:200])
        return []
    out = []
    for it in issues:
        n = it.get("number")
        if not n or it.get("pull_request"):
            continue
        key = ticket_key(repo, n)
        g = gate(cur, key)
        if g is None:
            decl = verify_decl(cur, f"github:{repo}#{n}")
            if not decl:
                continue   # 道 verify の計画がない（人が手でラベルを付けたなど）。何もしない
            if may_enqueue and open_ticket(cur, gh, repo, n, decl, now, log):
                out.append({"ticket": n, "opened": True})
            continue
        if g["state"] != "open":
            continue
        decl, staged = g["opened"].get("decl") or {}, g["opened"].get("staged_release")
        note = None
        if staged and g.get("plan") is None:   # 出した版の関門の結果を使う
            rg = gate(cur, staged)
            if rg and rg["state"] == "decided":
                out.append({"ticket": n, "decided": ticket_decide(cur, gh, repo, g, staged_results(cur, rg, decl, now, outcome), False, now, via=staged)})
                continue
            cur.execute("select 1 from events where name='release.ship_failed' and payload->>'release_id'=%s limit 1", (staged,))
            failed = cur.fetchone() is not None
            waited = now - g["opened_at"] > timedelta(hours=2 * float(gd["hours"]))
            if not (failed or (rg and rg["state"] == "superseded") or waited):
                continue   # 出荷と版の関門を待つ
            note = (f"版 {staged.split('@')[-1]} を staging に出せなかった" if failed else
                    f"版 {staged.split('@')[-1]} の QA が使えなかった（" + ("次の版に置き換わった" if rg and rg["state"] == "superseded" else "時間切れ") + "）") \
                + "ので、いまの staging で流した"
        pl = ticket_plan(cur, gh, g, n, decl, defs, gd, now, log, note) if may_enqueue or g.get("plan") else None
        if not pl:
            continue
        if pl.get("units"):
            enq, results = step_units(cur, gh, repo, g, defs, gd, now, may_enqueue, on_enqueued, log, outcome)
        else:
            enq, results = legacy_ticket(cur, gh, repo, g, pl, defs, gd, now, may_enqueue, on_enqueued, log)
        if enq:
            out.append({"ticket": n, "enqueued": [x["job_id"] for x in enq]})
        if results:
            timed_out = results.pop("timed_out")
            out.append({"ticket": n, "decided": ticket_decide(cur, gh, repo, g, results, timed_out, now)})
    return out
