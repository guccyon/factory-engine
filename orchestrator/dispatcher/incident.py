"""Incident と、QA 役によるラインの差し止め（ADR 0028 の決定 5、ADR 0043 の O5 severity_budget）。形は definitions/quality_policy.yaml と
definitions/events.yaml。

- Incident: 利用者に影響が出たもの。Gen1 は本番を立てないので（ADR 0012 の追記）、staging を本番の代わりにし、QA 役（判定・夜間の探索）が
  起票した Defect（known_gap を除く）を、その重要度の Incident としても残す（artifacts の Incident と events の incident.opened）。
  同じ Defect の Issue に当たったものは 1 件（見かけるたびに数えない）
  本番を立てる段階（launch・operating）の製品では、本番の見張り（prod_watch.py）と本番への出荷の失敗（ship.py）も S2 の Incident を開く
  （detected_by が prod_watch / shipper、env が prod。ADR 0057）
- 差し止め: 重要度が HOLD_SEVERITIES（S1）の Incident が開いたら、QA 役がラインを差し止める（events の line.held_by_qa）。
  ラインの一時停止（drain。工場の更新のための札）とは別の札で、解除は人だけ（line.hold_released。factoryctl line release か操業コンソール）。
  差し止めの間、dispatcher はラインのジョブのうち、S1 の Defect の Issue と QA の固定 Issue のジョブ、p0 のジョブだけを取り出す
  （S1 の修正と確かめ直しは流す）。実行中のジョブは最後まで流す。委員会と窓口の枠は止めない
- エラーバジェット: 製品の .factory-config/quality_policy.yaml の error_budget（期間と重要度ごとの許容件数）と、期間の中の Incident の数を
  突き合わせる。超えたら incident.budget_exceeded を期間と重要度ごとに 1 回残して知らせる。演習（drill）の Incident は数えない
- 演習: factoryctl incident drill が staging に仕込み（ship.yaml の touch.drills）、incident.drill_started を残す。終わるまでに開いた
  その製品の Incident は drill: true になる

LLM は使わない。記録の失敗で呼び出し元（QA の起票）を止めない（safe_ の付いた口）。
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import yaml
from psycopg.types.json import Jsonb

import line
import ops
import stoppage

SEVERITIES = ("s1", "s2", "s3", "s4")
HOLD_SEVERITIES = ("s1",)          # definitions/quality_policy.yaml の severities で hold: true のもの（S1 は止める）
HOLD, RELEASED = "line.held_by_qa", "line.hold_released"
HOLD_PRIORITY_LABEL = ops.PRIORITY_LABEL_PREFIX + "p0"   # S1 の Defect の Issue に付ける（どの機能開発より優先。ADR 0028）
POLICY_PATH = ".factory-config/quality_policy.yaml"
# 差し止めを解除できないもの（人だけが解除する。QA 役と工場の常駐が自分の差し止めを外さない）
NOT_HUMAN = ("qa", "supervisor", "dispatcher", "watcher", "shipper")
JST = timezone(timedelta(hours=9))


# ------------------------------------------------------------------ Incident
def incident_id(repo: str, key: str) -> str:
    return f"{repo}:{key}"[:300]


def drill_active(cur, repo: str) -> dict | None:
    """その製品で演習の最中なら incident.drill_started の中身（＋ id）。終わっていれば None"""
    cur.execute("select id, name, payload from events where name in ('incident.drill_started','incident.drill_ended') "
                "and payload->>'repo'=%s order by id desc limit 1", (repo,))
    r = cur.fetchone()
    return {**r["payload"], "event_id": r["id"]} if r and r["name"] == "incident.drill_started" else None


def open_incident(cur, repo: str, key: str, severity: str, title: str, *, detected_by: str = "qa", env: str | None = None,
                  release_id: str | None = None, issue: str | None = None, impact: str = "", correlation_id: str | None = None,
                  job_id: int | None = None) -> dict | None:
    """Incident を 1 件開く。同じ key（Defect の Issue）は 1 回だけ。返り値は新しく開いたときの {incident_id, severity, drill, ...}、既にあれば None"""
    if severity not in SEVERITIES:
        raise ValueError(f"severity は {'・'.join(SEVERITIES)} のどれか: {severity}")
    iid = incident_id(repo, key)
    # 演習の印は、演習が起こす重要度（S1）だけに付ける。演習の最中に見つかったほかの重要度は本物の不具合として数える
    drill = drill_active(cur, repo) if severity in HOLD_SEVERITIES else None
    now = datetime.now(timezone.utc)
    payload = {"incident_id": iid, "repo": repo, "severity": severity, "title": str(title or "")[:200], "detected_at": now.isoformat(),
               "detected_by": detected_by, "env": env, "release_id": release_id, "issue": issue, "related_pr_ids": [],
               "related_releases": [release_id] if release_id else [], "impact": str(impact or "")[:500],
               "drill": bool(drill), "drill_id": drill.get("drill") if drill else None}
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('incident.opened',%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (correlation_id, job_id, Jsonb(payload), f"incident.opened:{iid}"))
    if cur.fetchone() is None:
        return None
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Incident',%s,%s,'operate',%s)",
                (correlation_id or f"incident:{iid}", job_id, Jsonb(payload)))
    return payload


def close_incident(cur, iid: str, by: str, note: str = "") -> bool:
    """Incident を閉じる（直った・演習を終えた）。開いていなければ False"""
    if not any(i["incident_id"] == iid for i in open_incidents(cur)):
        return False
    cur.execute("insert into events(name, payload, dedupe_key) values ('incident.closed',%s,%s) on conflict (dedupe_key) do nothing",
                (Jsonb({"incident_id": iid, "by": by, "note": note}), f"incident.closed:{iid}"))
    return True


def open_incidents(cur, repo: str | None = None) -> list[dict]:
    cur.execute("select o.payload, o.occurred_at from events o where o.name='incident.opened' and (%s::text is null or o.payload->>'repo'=%s) "
                "and not exists (select 1 from events c where c.name='incident.closed' and c.payload->>'incident_id' = o.payload->>'incident_id') "
                "order by o.id desc", (repo, repo))
    return [{**r["payload"], "at": r["occurred_at"]} for r in cur.fetchall()]


def recent(cur, repo: str | None = None, limit: int = 20) -> list[dict]:
    """最近の Incident（閉じたものも）。factoryctl incident list"""
    cur.execute("select o.payload, o.occurred_at, (select c.payload from events c where c.name='incident.closed' "
                "and c.payload->>'incident_id' = o.payload->>'incident_id' limit 1) as closed "
                "from events o where o.name='incident.opened' and (%s::text is null or o.payload->>'repo'=%s) order by o.id desc limit %s",
                (repo, repo, limit))
    return [{**r["payload"], "at": r["occurred_at"], "closed": r["closed"]} for r in cur.fetchall()]


# ------------------------------------------------------------------ 差し止め
def hold_state(cur) -> dict | None:
    """差し止め中なら最後の line.held_by_qa の中身に since・minutes を足したもの。そうでなければ None"""
    cur.execute("select name, payload, occurred_at, extract(epoch from now() - occurred_at)::int / 60 as minutes from events "
                "where name in (%s, %s) order by id desc limit 1", (HOLD, RELEASED))
    r = cur.fetchone()
    if not r or r["name"] != HOLD:
        return None
    cur.execute("select occurred_at from events where name=%s and id > coalesce((select max(id) from events where name=%s), 0) "
                "order by id limit 1", (HOLD, RELEASED))
    first = cur.fetchone()
    return {**r["payload"], "since": (first or r)["occurred_at"], "minutes": r["minutes"]}


def held(cur) -> bool:
    return hold_state(cur) is not None


def stoppage_key(st: dict) -> str:
    return f"qa_hold:{st['since']:%Y%m%dT%H%M%S}"


def hold(cur, inc: dict, *, qa_corr: str | None, defect_corr: str | None, by: str = "qa") -> bool:
    """S1 の Incident でラインを差し止める。すでに差し止め中なら Incident と流してよい Issue を足す。返り値は新しく差し止めたか"""
    prev = hold_state(cur)
    incidents = list((prev or {}).get("incidents") or [])
    corrs = set((prev or {}).get("corrs") or [])
    if inc["incident_id"] in [i.get("incident_id") for i in incidents] and prev:
        return False
    incidents.append({k: inc.get(k) for k in ("incident_id", "repo", "severity", "title", "issue", "drill")})
    corrs |= {c for c in (qa_corr, defect_corr) if c}
    cur.execute("insert into events(name, payload) values (%s, %s)",
                (HOLD, Jsonb({"by": by, "incidents": incidents, "corrs": sorted(corrs), "min_priority": ops.PREEMPT_AT,
                              "reason": f"{inc['severity'].upper()}: {inc.get('title')}"})))
    st = hold_state(cur)
    what = (f"QA 役が {inc['severity'].upper()} の Incident でラインを差し止めた{'（演習）' if inc.get('drill') else ''}: {inc.get('title')}"
            + (f"（{inc['issue']}）" if inc.get("issue") else ""))
    if prev:
        stoppage.post_notice(f"**差し止めに Incident を足しました**: {what}", cur)
    else:
        stoppage.open_stoppage(cur, stoppage_key(st), "human", what, detected_by="qa", started_at=st["since"],
                               waiting_for="手動の解除（操業コンソールの「差し止めを解除」か factoryctl line release --reason ...）。"
                                           "S1 の修正（p0）と QA の確かめ直しは差し止めの間も流れる")
    return prev is None


def release(cur, by: str, reason: str) -> bool:
    """人が差し止めを解除する。差し止めていなければ False。QA 役と工場の常駐は解除できない（ValueError）"""
    who = str(by or "").split(":")[0].strip()
    if not by or who in NOT_HUMAN:
        raise ValueError("差し止めの解除は人だけ（ADR 0043 の O5）")
    if not str(reason or "").strip():
        raise ValueError("解除の理由が要る（直した・演習を終えた など）")
    st = hold_state(cur)
    if not st:
        return False
    cur.execute("insert into events(name, payload) values (%s, %s)",
                (RELEASED, Jsonb({"by": by, "reason": reason, "incidents": [i.get("incident_id") for i in st.get("incidents") or []],
                                  "held_minutes": st["minutes"]})))
    stoppage.close_stoppage(cur, stoppage_key(st), remedy=f"人が差し止めを解除した（{by}: {reason}）", closed_by=by, woken_by="human")
    return True


def claim(cur) -> dict | None:
    """差し止めの間に取り出してよいラインのジョブ（S1 の Defect の Issue・QA の固定 Issue・p0）を 1 つ"""
    st = hold_state(cur) or {}
    return line.claim_line(cur, list(st.get("corrs") or []), st.get("min_priority", ops.PREEMPT_AT))


def hold_status(cur) -> dict | None:
    """操業コンソールと factoryctl の表示"""
    st = hold_state(cur)
    if not st:
        return None
    return {"since": st["since"].isoformat(), "minutes": st["minutes"], "reason": st.get("reason") or "",
            "incidents": st.get("incidents") or [], "allowed": st.get("corrs") or []}


# ------------------------------------------------------------------ エラーバジェット
def parse_policy(text: str) -> dict:
    doc = yaml.safe_load(text) or {}
    return doc if isinstance(doc, dict) else {}


def fetch_policy(gh, repo: str) -> dict:
    """製品の既定ブランチの quality_policy.yaml。読めなければ空"""
    try:
        c = gh("GET", f"repos/{repo}/contents/{POLICY_PATH}")
        return parse_policy(base64.b64decode(c["content"]).decode())
    except Exception:  # noqa: BLE001
        return {}


def budget(cur, repo: str, policy: dict, now: datetime | None = None) -> dict | None:
    """期間の中の Incident（演習を除く）の数と許容件数。policy に error_budget がなければ None。
    期間は JST の暦日で now を含む period_days 日（今日を含めて遡る）"""
    eb = (policy or {}).get("error_budget") or {}
    allowed = eb.get("allowed") or {}
    days = int(eb.get("period_days") or 0)
    if not days or not allowed:
        return None
    now = now or datetime.now(timezone.utc)
    start = datetime.combine(now.astimezone(JST).date() - timedelta(days=days - 1), datetime.min.time(), JST)
    cur.execute("select payload->>'severity' as s, count(*) as n from events where name='incident.opened' and payload->>'repo'=%s "
                "and occurred_at >= %s and coalesce((payload->>'drill')::boolean, false) = false group by 1", (repo, start))
    counts = {r["s"]: r["n"] for r in cur.fetchall()}
    rows = {s: {"count": int(counts.get(s, 0)), "allowed": allowed.get(s)} for s in SEVERITIES}
    over = [s for s, v in rows.items() if isinstance(v["allowed"], int) and v["count"] > v["allowed"]]
    return {"repo": repo, "period_days": days, "since": start.isoformat(), "severities": rows, "over": over,
            "provisional": bool(policy.get("provisional")), "on_exceed": eb.get("on_exceed")}


def check_budget(cur, repo: str, policy: dict, now: datetime | None = None) -> list[str]:
    """超えた重要度ごとに incident.budget_exceeded を期間に 1 回残し、知らせる。返り値は新しく超えた重要度"""
    b = budget(cur, repo, policy, now)
    if not b:
        return []
    out = []
    for s in b["over"]:
        v = b["severities"][s]
        cur.execute("insert into events(name, payload, dedupe_key) values ('incident.budget_exceeded',%s,%s) on conflict (dedupe_key) do nothing returning id",
                    (Jsonb({"repo": repo, "severity": s, "count": v["count"], "allowed": v["allowed"], "period_days": b["period_days"],
                            "since": b["since"]}), f"incident.budget_exceeded:{repo}:{s}:{b['since'][:10]}"))
        if cur.fetchone():
            out.append(s)
            stoppage.post_notice(f"**エラーバジェットを超えました**（{repo}・{s.upper()}）: {b['period_days']} 日で {v['count']} 件"
                                 f"（許容 {v['allowed']} 件{'・仮置きの値' if b['provisional'] else ''}）。{b.get('on_exceed') or ''}", cur)
    return out


# ------------------------------------------------------------------ QA 役から
def from_qa(cur, gh, comment, repo: str, *, key: str, severity: str, title: str, issue_url: str | None, env: str | None,
            release_id: str | None, qa_corr: str, job_id: int | None, policy: dict | None = None, log=None) -> dict | None:
    """QA 役が起票した Defect を Incident にし、メジャー（S1・S2）なら直しに流し（ADR 0053）、S1 ならラインを差し止めて Issue に p0 を付け、エラーバジェットを確かめる。
    失敗しても呼び出し元（起票とまとめ）は止めない。返り値は新しく開いた Incident（なければ None）"""
    try:
        inc = open_incident(cur, repo, key, severity, title, env=env, release_id=release_id, issue=issue_url,
                            correlation_id=qa_corr, job_id=job_id, impact="QA 役が staging で利用者として見つけた（Gen1 は staging を本番の代わりにする）")
        if not inc:
            return None
        num = (issue_url or "").rstrip("/").rsplit("/", 1)[-1].lstrip("#")
        defect_corr = f"github:{repo}#{num}" if num.isdigit() else None
        import major_defect
        planned = False
        if num.isdigit() and major_defect.is_major(severity) and not inc["drill"]:
            # メジャー不具合（S1・S2）は人の 1 タップを待たずに流す。方針が要ればスパイクで決める（ADR 0053・0055）。演習は直すコードがないので流さない
            try:
                major_defect.plan_fix(gh, repo, int(num))
                planned = True
            except Exception as e:  # noqa: BLE001
                if log:
                    log("major label failed", issue_url, repr(e))
        flow = ("メジャー不具合（先に進めない・業務が止まる）なので、工場が承認を待たずに直し方を決めて直し、修正がマージされたら緊急便で出し直します"
                "（ADR 0053。同じ原因の不具合があればまとめて直します）。" if planned else "")
        if severity in HOLD_SEVERITIES:
            hold(cur, inc, qa_corr=qa_corr, defect_corr=defect_corr)
            if num.isdigit():
                try:
                    gh("POST", f"repos/{repo}/issues/{num}/labels", {"labels": [HOLD_PRIORITY_LABEL]})
                except Exception as e:  # noqa: BLE001
                    if log:
                        log("s1 label failed", issue_url, repr(e))
                comment(repo, int(num), f"重要度 {severity.upper()} の Incident（`{inc['incident_id']}`）として、QA 役がラインを差し止めました"
                                        f"（factory-engine の ADR 0043 の O5）。{'**これは演習です**（staging に仕込んだもの。直すコードはありません）。' if inc['drill'] else ''}\n\n"
                                        "差し止めの間も、この Issue（p0）と QA の確かめ直しは流れます。ほかのチケットは止まっています。" + flow
                                        + "解除は手動で行います（操業コンソールの「差し止めを解除」か `factoryctl line release --reason ...`）。")
        elif planned:
            comment(repo, int(num), f"重要度 {severity.upper()} の不具合です。" + flow)
        if policy is None:
            policy = fetch_policy(gh, repo)
        check_budget(cur, repo, policy)
        return inc
    except Exception as e:  # noqa: BLE001
        if log:
            log("incident from qa failed", repo, key, repr(e))
        return None
