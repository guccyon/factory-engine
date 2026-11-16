"""振り返り（ADR 0061）の制御プレーン側: スプリントの区切りと、N の自動調整。

知見の委員会は、工場が消化したチケット N 件ごとの振り返り（スプリント）として開く。ここは決定論の部分だけを持つ。
- 数え始め（retro.started）: 製品ごとに、初めて見たときから数える。それより前に閉じたチケットは振り返らない
- 数えるチケット（retro.ticket_classified）: tickets で閉じたもののうち、工場の内部の Issue（definitions/knowledge.yaml の
  retro.exclude_labels）でないもの。ラベルは閉じたあとに 1 回だけ GitHub に聞き、結果を events に残す（聞き直さない）
- スプリントを閉じる（retro.sprint_closed）: 数えたチケットのうちまだどのスプリントにも入っていないものが N 件たまったら、
  古い順に N 件で閉じる。N 件たまらなくても、最後に閉じてから time_floor.days たち、min_tickets 件以上あれば閉じる
- N の自動調整（retro.size_adjusted）: 振り返りの結果の数（知見ごとの根拠のチケット数・議題の被覆率・差し戻しの理由・知見 0 件の連続）から、
  次の N を step ずつ動かす（min〜max）。決まりを変えるのは定義の PR

振り返りそのもの（ロールの委員とリーダーの議論、議事録、知見の変更セット）はワーカーの仕事で、ここでは扱わない。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from statistics import median

import yaml
from psycopg.types.json import Jsonb

CLASSIFY_PER_TICK = 30
DEFS_PATH = os.environ.get("KNOWLEDGE_DEFINITIONS", "/definitions/knowledge.yaml")


def load_defs(path: str | None = None) -> dict:
    """definitions/knowledge.yaml の retro"""
    with open(path or DEFS_PATH, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("retro") or {}   # 1 周で GitHub にラベルを聞くチケットの数の上限（GET は ETag で条件付き）


def corr(repo: str) -> str:
    return f"retro:{repo}"


def _record(cur, name: str, repo: str, payload: dict, key: str | None = None) -> bool:
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values (%s,%s,%s,%s) on conflict (dedupe_key) do nothing returning id",
                (name, corr(repo), Jsonb(payload), key))
    return cur.fetchone() is not None


# ------------------------------------------------------------------ 数え始め
def started_at(cur, repo: str, now: datetime | None = None) -> datetime:
    """製品の数え始め。なければ今を数え始めにする（それより前に閉じたチケットは振り返らない）"""
    cur.execute("select (payload->>'at')::timestamptz as at from events where name='retro.started' and dedupe_key=%s", (f"retro.started:{repo}",))
    r = cur.fetchone()
    if r:
        return r["at"]
    at = now or datetime.now(timezone.utc)
    _record(cur, "retro.started", repo, {"repo": repo, "at": at.isoformat()}, f"retro.started:{repo}")
    return at


# ------------------------------------------------------------------ 数えるチケット
def classify(cur, repo: str, gh, exclude: list[str], since: datetime, limit: int = CLASSIFY_PER_TICK) -> int:
    """数え始めより後に閉じたチケットで、まだ分けていないものを、数える・数えないに分けて events に残す。分けた件数を返す"""
    cur.execute("""select t.source_ref as ref, t.updated_at as closed_at from tickets t
                    where t.source='github' and t.repo=%s and t.state='closed' and t.updated_at >= %s
                      and not exists (select 1 from events e where e.dedupe_key = 'retro.ticket:' || t.source_ref)
                    order by t.updated_at limit %s""", (repo, since, limit))
    done = 0
    for r in cur.fetchall():
        m = re.match(r"(.+)#(\d+)$", r["ref"])
        if not m:
            continue
        try:
            issue = gh("GET", f"repos/{m.group(1)}/issues/{m.group(2)}") or {}
        except Exception:  # noqa: BLE001  聞けなければ次の周回で
            continue
        labels = sorted(l.get("name", "") for l in issue.get("labels") or [] if isinstance(l, dict))
        skip = sorted(set(labels) & set(exclude))
        _record(cur, "retro.ticket_classified", repo,
                {"repo": repo, "ref": r["ref"], "closed_at": r["closed_at"].isoformat(), "counted": not skip, "excluded_by": skip,
                 "title": issue.get("title") or ""},
                f"retro.ticket:{r['ref']}")
        done += 1
    return done


def pending(cur, repo: str) -> list[dict]:
    """数えたチケットのうち、まだどのスプリントにも入っていないもの（閉じた古い順）"""
    cur.execute("""select c.payload->>'ref' as ref, (c.payload->>'closed_at')::timestamptz as closed_at, c.payload->>'title' as title
                     from events c
                    where c.name='retro.ticket_classified' and c.payload->>'repo'=%s and (c.payload->>'counted')::boolean
                      and not exists (select 1 from events s where s.name='retro.sprint_closed' and s.payload->>'repo'=%s
                                        and s.payload->'tickets' ? (c.payload->>'ref'))
                    order by (c.payload->>'closed_at')::timestamptz, c.id""", (repo, repo))
    return list(cur.fetchall())


# ------------------------------------------------------------------ スプリント
def last_sprint(cur, repo: str) -> dict | None:
    cur.execute("select payload, occurred_at from events where name='retro.sprint_closed' and payload->>'repo'=%s order by id desc limit 1", (repo,))
    r = cur.fetchone()
    return {**r["payload"], "closed_at": r["occurred_at"]} if r else None


def size(cur, repo: str, defs: dict) -> int:
    """次のスプリントの N。調整の記録がなければ初期値。範囲の外なら範囲に収める（定義を変えたとき）"""
    s = defs.get("size") or {}
    cur.execute("select (payload->>'next')::int as n from events where name='retro.size_adjusted' and payload->>'repo'=%s order by id desc limit 1", (repo,))
    r = cur.fetchone()
    n = r["n"] if r else int(s.get("initial", 12))
    return max(int(s.get("min", 1)), min(int(s.get("max", n)), n))


def maybe_close(cur, repo: str, defs: dict, now: datetime | None = None) -> dict | None:
    """N 件たまったか、時間の下限に当たったらスプリントを閉じる。閉じたらその記録を返す"""
    now = now or datetime.now(timezone.utc)
    items = pending(cur, repo)
    if not items:
        return None
    n = size(cur, repo, defs)
    floor = defs.get("time_floor") or {}
    last = last_sprint(cur, repo)
    since = last["closed_at"] if last else started_at(cur, repo, now)
    if len(items) >= n:
        reason, take = "size", items[:n]
    elif len(items) >= int(floor.get("min_tickets", 3)) and now - since >= timedelta(days=int(floor.get("days", 14))):
        reason, take = "time", items
    else:
        return None
    seq = (last or {}).get("sprint", 0) + 1
    sprint = {"repo": repo, "sprint": seq, "size": n, "reason": reason, "tickets": [t["ref"] for t in take],
              "titles": {t["ref"]: t["title"] for t in take},
              "from": take[0]["closed_at"].isoformat(), "to": take[-1]["closed_at"].isoformat()}
    if not _record(cur, "retro.sprint_closed", repo, sprint, f"retro.sprint:{repo}:{seq}"):
        return None
    return sprint


# ------------------------------------------------------------------ N の自動調整
def metrics(tickets: list[str], knowledge: list[dict], sendback_reasons: list[str] | None = None) -> dict:
    """振り返りの結果から、調整に使う数を決定論で出す。knowledge は [{id, evidence_tickets: [...]}]"""
    per = [len(set(k.get("evidence_tickets") or [])) for k in knowledge]
    covered = {t for k in knowledge for t in (k.get("evidence_tickets") or []) if t in set(tickets)}
    reasons = sendback_reasons or []
    broad = [r for r in reasons if re.search(r"広すぎ|抽象|根拠.*(たど|辿)れ|一般論", r)]
    return {"tickets": len(tickets), "knowledge": len(knowledge),
            "median_evidence": (lambda x: int(x) if float(x).is_integer() else x)(median(per)) if per else 0,
            "coverage": round(len(covered) / len(tickets), 3) if tickets else 0.0,
            "sendbacks": len(reasons), "broad_sendback_ratio": round(len(broad) / len(reasons), 3) if reasons else 0.0}


def adjust(n: int, m: dict, defs: dict, empty_streak: int = 0) -> tuple[int, str]:
    """次の N と理由。合図が両方に出たら動かさない（どちらとも言えない）"""
    s, a = defs.get("size") or {}, defs.get("adjust") or {}
    step, lo, hi = int(s.get("step", 2)), int(s.get("min", 1)), int(s.get("max", 99))
    up, down = [], []
    if m["knowledge"] == 0:
        if empty_streak + 1 >= int(a.get("empty_streak", 2)):
            up.append(f"知見 0 件が {empty_streak + 1} 回続いた")
    else:
        if m["median_evidence"] <= a.get("local_median_evidence", 1):
            up.append(f"知見 1 件あたりの根拠のチケットが中央値 {m['median_evidence']} 件（局所的）")
        if m["coverage"] < a.get("abstract_coverage", 0.3):
            down.append(f"議題のチケットのうち知見の根拠になったのが {round(m['coverage'] * 100)}%（抽象的）")
    if m["sendbacks"] and m["broad_sendback_ratio"] >= a.get("abstract_sendback_ratio", 0.5):
        down.append(f"レビューの差し戻しの {round(m['broad_sendback_ratio'] * 100)}% が「広すぎる・根拠がたどれない」")
    if up and down:
        return n, "合図が両方に出たので動かさない: " + " / ".join(up + down)
    if up:
        return min(hi, n + step), " / ".join(up)
    if down:
        return max(lo, n - step), " / ".join(down)
    return n, "ちょうど良い（合図なし）"


def empty_streak(cur, repo: str) -> int:
    """直近の調整の記録で、知見 0 件が何回続いているか"""
    cur.execute("select (payload->'metrics'->>'knowledge')::int as k from events where name='retro.size_adjusted' and payload->>'repo'=%s order by id desc limit 10", (repo,))
    streak = 0
    for r in cur.fetchall():
        if r["k"]:
            break
        streak += 1
    return streak


def record_adjust(cur, repo: str, sprint: int, m: dict, defs: dict) -> dict:
    """振り返りの結果を受けて次の N を決め、retro.size_adjusted に残す（スプリントごとに 1 回）"""
    cur_n = size(cur, repo, defs)
    nxt, why = adjust(cur_n, m, defs, empty_streak(cur, repo))
    p = {"repo": repo, "sprint": sprint, "prev": cur_n, "next": nxt, "reason": why, "metrics": m}
    _record(cur, "retro.size_adjusted", repo, p, f"retro.size:{repo}:{sprint}")
    return p


# ------------------------------------------------------------------ 議題（ワーカーに渡す材料）
# ジョブの種類 → ロール（振り返りの委員）。工場の内部の仕事（審査・分析・取りまとめ）はロールにしない
ROLES = {"plan": "プランナー", "implement": "実装", "inspect": "検査", "judge": "judge（出口の裁き）", "qa": "QA",
         "ui_design": "デザイナー", "ui_design_review": "デザイン検査", "design_system": "デザインシステム", "spike": "System Architect",
         "clarify": "要求の聞き取り", "desk_consult": "窓口の相談役", "desk_task": "窓口の実行役"}
TIMELINE_MAX = 40          # チケット 1 件あたりの出来事の数の上限
TEXT_MAX = 300             # 出来事の中身の 1 項目の長さの上限
KEEP_KEYS = ("reason", "verdict", "outcome", "judgment", "summary", "severity", "title", "error", "kind", "category", "what", "root_causes",
             "pr_url", "merged", "turns", "cost_usd", "incomplete", "continues", "route_changed", "needs_judgment", "ids", "state")


def _short(v):
    s = v if isinstance(v, str) else __import__("json").dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= TEXT_MAX else s[:TEXT_MAX] + "…"


def ticket_material(cur, ref: str) -> dict:
    """チケット 1 件の材料（制御プレーンの記録だけ）。ジョブ（ロール・状態・費用）と出来事の流れ"""
    corr_id = f"github:{ref}"
    cur.execute("""select j.id, j.kind, j.state, j.attempt, j.enqueued_at, j.started_at, j.finished_at,
                          coalesce((j.result->>'cost_usd')::numeric, 0) as cost
                     from jobs j where j.correlation_id=%s order by j.id""", (corr_id,))
    jobs = [{"kind": r["kind"], "role": ROLES.get(r["kind"]), "state": r["state"], "attempt": r["attempt"],
             "minutes": round((r["finished_at"] - r["started_at"]).total_seconds() / 60, 1) if r["finished_at"] and r["started_at"] else None,
             "cost_usd": float(r["cost"])} for r in cur.fetchall()]
    cur.execute("""select name, occurred_at, payload from events where correlation_id=%s
                     and name not in ('job.enqueued','job.started','supervisor.heartbeat') order by id limit %s""", (corr_id, TIMELINE_MAX))
    timeline = [{"at": r["occurred_at"].isoformat(), "name": r["name"],
                 **{k: _short(v) for k, v in (r["payload"] or {}).items() if k in KEEP_KEYS and v not in (None, "", [], {})}}
                for r in cur.fetchall()]
    cur.execute("select min(occurred_at) as first from events where correlation_id=%s", (corr_id,))
    first = (cur.fetchone() or {}).get("first")
    cur.execute("""select payload from events where name='case.analyzed' and payload->>'ticket'=%s order by id""", (corr_id,))
    cases = [{k: r["payload"].get(k) for k in ("incident", "kind", "summary", "root_causes", "learnings")} for r in cur.fetchall()]
    return {"ref": ref, "jobs": jobs, "timeline": timeline, "cases": cases, "first_seen": first.isoformat() if first else None,
            "roles": sorted({j["role"] for j in jobs if j["role"]}),
            "cost_usd": round(sum(j["cost_usd"] for j in jobs), 2),
            "sendbacks": sum(1 for t in timeline if t["name"] in ("sendback.filed", "committee.revise") or t.get("verdict") in ("adopt", "replace")),
            "stoppages": sum(1 for t in timeline if t["name"].startswith("stoppage."))}


def committee_roles(materials: list[dict], max_roles: int) -> list[dict]:
    """議題のチケットに関わったロール（関わったチケットの数の多い順に max_roles まで）"""
    count: dict[str, list[str]] = {}
    for m in materials:
        for r in m["roles"]:
            count.setdefault(r, []).append(m["ref"])
    ranked = sorted(count.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return [{"role": r, "tickets": ts} for r, ts in ranked[:max_roles]]


def build_input(cur, sprint: dict, defs: dict) -> dict:
    """振り返りの議題。対象のチケットの材料、期間の運用（障害）と事故（事故調査委員会）、前回の振り返りの打ち手"""
    repo = sprint["repo"]
    materials = [{**ticket_material(cur, ref), "title": (sprint.get("titles") or {}).get(ref, "")} for ref in sprint["tickets"]]
    since, until = sprint["from"], sprint["to"]
    # 運用: 製品の障害（ADR 0043 の O5・0053・0057）。期間に開いた・閉じたもの
    cur.execute("""select name, occurred_at, payload from events where name in ('incident.opened','incident.closed','prod.down','prod.up','major.rereleased')
                     and occurred_at between %s::timestamptz - interval '1 day' and %s::timestamptz + interval '1 day'
                     and (payload->>'repo'=%s or payload->>'incident_id' like %s) order by id limit 30""", (since, until, repo, f"{repo}:%"))
    ops = [{"name": r["name"], "at": r["occurred_at"].isoformat(), **{k: _short(v) for k, v in (r["payload"] or {}).items()
                                                                     if k in ("incident_id", "severity", "error", "related_pr_ids", "down_seconds")}}
           for r in cur.fetchall()]
    # 事故（ADR 0046）: 期間に開いた・見回りで見つけた兆し。お金の損の再発防止を開発のやり方に落とす材料
    cur.execute("""select name, occurred_at, payload from events where name in ('fincident.opened','fincident.sign','fincident.reviewed')
                     and occurred_at between %s::timestamptz - interval '1 day' and %s::timestamptz + interval '1 day' order by id limit 20""", (since, until))
    incidents = [{"name": r["name"], "at": r["occurred_at"].isoformat(), **{k: _short(v) for k, v in (r["payload"] or {}).items()
                                                                           if k in ("id", "title", "resource", "severity", "summary", "viewpoints")}}
                 for r in cur.fetchall()]
    # 前回の振り返りの打ち手（効き目の確認）
    cur.execute("select payload from events where name='retro.held' and payload->>'repo'=%s order by id desc limit 1", (repo,))
    prev = (cur.fetchone() or {}).get("payload") or {}
    previous = {"sprint": prev.get("sprint"), "knowledge": [{k: x.get(k) for k in ("statement", "destination", "perspective")} for x in prev.get("knowledge") or []],
                "actions": prev.get("actions") or []} if prev else None
    # 置き場の棚卸しの材料（週 1 の定例から移した。ADR 0061 の決定 10）
    week = weekly_like_input(cur, repo, since)
    return {"repo": repo, "sprint": sprint["sprint"], "size": sprint["size"], "reason": sprint["reason"], "from": since, "to": until,
            "tickets": materials, "roles": committee_roles(materials, int((defs.get("committee") or {}).get("max_roles", 6))),
            "operations": ops, "incidents": incidents, "previous": previous, "inventory": week,
            "prompts": defs.get("prompts") or {}, "domain_destinations": defs.get("domain_destinations") or {}}


def weekly_like_input(cur, repo: str, since) -> dict:
    """棚卸しの材料: スプリントの期間の効き目の信号（届かなかった・届いたのに差し戻された）と、直近 60 日に渡した知見。ADR 0024 の定例と同じ見方"""
    cur.execute("""select name, payload from events where name in ('knowledge.missed','knowledge.delivered_failed') and occurred_at >= %s::timestamptz
                     and payload->>'repo'=%s order by id limit 50""", (since, repo))
    reach = [{"signal": "missed" if r["name"] == "knowledge.missed" else "delivered_failed",
              **{k: r["payload"].get(k) for k in ("id", "incident", "reason", "scope", "work", "role")}} for r in cur.fetchall()]
    cur.execute("""select id, max(occurred_at) as last from (select jsonb_array_elements_text(payload->'ids') as id, occurred_at from events
                     where name='knowledge.used' and occurred_at >= now() - interval '60 days' and payload->>'repo'=%s) u group by id""", (repo,))
    return {"reach": reach, "used": {r["id"]: r["last"].date().isoformat() for r in cur.fetchall()}}


# ------------------------------------------------------------------ 固定 Issue（議事録のまとめを書く所）
ISSUE_LABEL = "factory:knowledge-committee"   # 週 1 の定例の固定 Issue をそのまま使う（題だけ替える）
ISSUE_TITLE = "知見の委員会の振り返り（スプリントごとに更新）"
DOCS_LABEL = "factory:domain-docs"            # 製品の docs/ にドメインの知識を足す Issue。次のスプリントには数えない（knowledge.yaml の exclude_labels）


def retro_issue(gh, repo: str) -> int:
    got = gh("GET", f"repos/{repo}/issues?state=open&labels={ISSUE_LABEL}&per_page=5") or []
    if got:
        if got[0].get("title") != ISSUE_TITLE:
            gh("PATCH", f"repos/{repo}/issues/{got[0]['number']}", {"title": ISSUE_TITLE})
        return got[0]["number"]
    body = ("知見の委員会（factory-engine の ADR 0061）が、工場の消化したチケット N 件ごとに振り返りを開き、その議事録のまとめをここに書きます。"
            "詳しい議事録は操業コンソールの委員会のページにあります。閉じないでください。")
    return gh("POST", f"repos/{repo}/issues", {"title": ISSUE_TITLE, "body": body, "labels": [ISSUE_LABEL]})["number"]


# ------------------------------------------------------------------ 議事録と起票
def minutes(sprint: dict, result: dict) -> dict:
    """議事録（retro.held と artifacts の RetroMinutes の中身）"""
    lead = result.get("lead") or {}
    return {"repo": sprint["repo"], "sprint": sprint["sprint"], "size": sprint["size"], "reason": sprint["reason"],
            "tickets": sprint["tickets"], "titles": sprint.get("titles") or {}, "from": sprint.get("from"), "to": sprint.get("to"),
            "roles": sorted({o.get("role") for o in result.get("opinions") or [] if o.get("role")}),
            "failed_roles": result.get("failed_roles") or [], "opinions": result.get("opinions") or [],
            "summary": lead.get("summary") or "", "knowledge": lead.get("knowledge") or [], "rejected": lead.get("rejected") or [],
            "actions": lead.get("actions") or [], "previous_effect": lead.get("previous_effect") or "",
            "inventory": lead.get("inventory") or {}, "docs_structure": lead.get("docs_structure") or {},
            "facts": {k: (result.get("facts") or {}).get(k) for k in ("items", "cases", "decisions", "files_in_root", "structure_signal", "stale", "unused")},
            "cost_usd": result.get("cost_usd"), "model": result.get("model")}


def _ticket_list(m: dict) -> str:
    return "\n".join(f"- {t} {m['titles'].get(t, '')}" for t in m["tickets"])


def _k_line(k: dict) -> str:
    ev = ", ".join(k.get("evidence_tickets") or [])
    return (f"- {k.get('statement')}\n  - 観点: {k.get('perspective')}／立場: {', '.join(k.get('from_roles') or [])}／根拠: {ev}"
            f"／作用: {k.get('acts_on')}／業務: {k.get('work')}" + (f"／影響する知見: {', '.join(k['affects'])}" if k.get("affects") else ""))


def markdown(m: dict, filed: dict) -> str:
    ops = "\n".join(f"- **{o.get('role')}**（{o.get('perspective')}、{', '.join(o.get('tickets') or [])}）: {o.get('observation')} → {o.get('proposal')}"
                    for o in m["opinions"]) or "- なし"
    kn = "\n".join(_k_line(k) + f"（置き場: {'製品の docs/' if k.get('destination') == 'product_docs' else '事業部の知見'}）" for k in m["knowledge"]) or "- なし"
    rej = "\n".join(f"- {r.get('statement')}（{', '.join(r.get('from_roles') or [])}）: {r.get('why')}" for r in m["rejected"]) or "- なし"
    act = "\n".join(f"- [{a.get('target')}] {a.get('what')}: {a.get('why')}" for a in m["actions"]) or "- なし"
    fl = "\n".join(f"- {k}: {v}" for k, v in filed.items()) or "- なし"
    missing = f"\n欠けた委員: {', '.join(f['role'] for f in m['failed_roles'])}" if m["failed_roles"] else ""
    return f"""## 振り返り S-{m['sprint']}（チケット {len(m['tickets'])} 件）

{m['summary']}

出席: {', '.join(m['roles']) or 'なし'}、事業部のリーダー{missing}

### 対象のチケット
{_ticket_list(m)}

### 立場ごとの意見
{ops}

### 得られた知見
{kn}

### 採らなかった候補
{rej}

### 打ち手
{act}

### 前回の打ち手の効き目
{m['previous_effect'] or 'なし'}

### 起票
{fl}

---
_知見の委員会の振り返り（ADR 0061）。知見は変更セットの PR にして知見のレビューにかけ、通れば事業部に展開します_"""


def _items(xs: list[dict]) -> str:
    return "\n".join(f"- {', '.join(x.get('ids') or [])}: {x.get('why', '')}（案: {x.get('proposal', '')}）" for x in xs) or "- なし"


def knowledge_issue(m: dict) -> tuple[str, str] | None:
    """事業部の知見の変更セット（新しい知見＋棚卸しの見直し）。何もなければ None"""
    ks = [k for k in m["knowledge"] if k.get("destination") != "product_docs"]
    inv = m["inventory"] or {}
    stale = [s.get("id") for s in (m["facts"].get("stale") or [])]
    unused = [u.get("id") for u in (m["facts"].get("unused") or [])]
    review = {k: inv.get(k) or [] for k in ("contradictions", "promote", "retire", "widen")}
    if not ks and not stale and not unused and not any(review.values()) and not (inv.get("structure") or {}).get("needed"):
        return None
    new = "\n".join(_k_line(k) + f"\n  - 種類: {k.get('kind')}／テーマ: {k.get('theme')}／一般に言える理由: {k.get('why_universal')}" for k in ks) or "- なし"
    st = inv.get("structure") or {}
    body = f"""知見の委員会の振り返り S-{m['sprint']}（ADR 0061）で抽出した知見と、置き場の見直しを、1 本の変更セットの PR にしてください（ADR 0024 の決定 4）。
PR は知見のレビュー（整合・根拠・担保・反対）にかかります。

## 新しい知見
{new}

- 1 件 1 ファイル（K-NNNN.yaml、番号は既定ブランチの空いている次）。`decided_by` は書かず draft で足す（レビューが active にする）
- 確かさ: 根拠のチケットが独立して 2 件以上なら confirmed、1 件なら hypothesis。`evidence` に根拠のチケット（kind: issue）を並べる
- `scope`: 業務（work）を必ず書く。ドメインならではの開発の知見（テーマ 3）は事業部の開発者が全員知っておくものなので、業務は根に近く、ロールは絞らない
- 影響する既存の知見があれば、扱い（amend・supersede・retire・recheck・keep）と理由を PR の本文に並べる

## 期限切れ（要再確認）
{chr(10).join('- ' + s for s in stale) or '- なし'}

## 使われない知見（形骸化の候補）
{chr(10).join('- ' + u for u in unused) or '- なし'}

## 矛盾・重複
{_items(review['contradictions'])}

## 確かさを上げる
{_items(review['promote'])}

## 廃止
{_items(review['retire'])}

## 効く範囲を広げる
{_items(review['widen'])}

## 置き場の構造
{('見直す: ' + st.get('proposal', '')) if st.get('needed') else '今は変えない'}

## やること
- `.factory-config/knowledge/README.md` の決まりに従う。ファイルは消さず状態だけ変える
- `python3 /work/engine/core-gates/knowledge_check.py --repo . --index` で INDEX.md を作り直し、検査が通ることを確かめる
- `.factory-config/knowledge/` の外は変えない

対象のチケット:
{_ticket_list(m)}
"""
    return f"知見の変更セット（振り返り S-{m['sprint']}）", body


def docs_issue(m: dict) -> tuple[str, str] | None:
    """ドメインの知識を製品の docs/ に足す（工場に依存しない書き方で）。何もなければ None"""
    ks = [k for k in m["knowledge"] if k.get("destination") == "product_docs"]
    ds = m["docs_structure"] or {}
    if not ks and not ds.get("needed"):
        return None
    lines = "\n".join(f"- {k.get('statement')}（根拠のチケット: {', '.join(k.get('evidence_tickets') or [])}）" for k in ks) or "- なし"
    return f"ドメインの知識を docs/ に足す（振り返り S-{m['sprint']}）", f"""振り返り S-{m['sprint']} で、このプロダクトのドメインについて分かったことを、製品のドメインの知識として `docs/` に足してください（ADR 0061 の決定 7）。

## 足す知識
{lines}

## 構造の見直し
{('見直す: ' + ds.get('proposal', '')) if ds.get('needed') else '今は変えない（既存の構造に足す）'}

## 決まり
- 工場に依存しない書き方にする。工場の語（知見の id、ロール名、振り返り）を書かない。別の開発組織が読んでも分かる形で
- `docs/` の既存の構造に合わせて足す。なければ `docs/domain/` から始める
- 業務ルールや仕様（`specs/`）を新しく決める・変えるものは書かない。そう読めるものは PR の本文に挙げて止め、事業主の判断を仰ぐ
- 個人情報は書かない
"""


def action_issues(m: dict, repo: str, engine_repo: str) -> list[tuple[str, str, str, list[str]]]:
    """打ち手を (リポジトリ, 題, 本文, ラベル) にする。engine と process は工場のリポジトリへ（実装とマージは人の承認）"""
    out = []
    for a in m["actions"][:4]:
        tgt = a.get("target")
        body = f"""知見の委員会の振り返り S-{m['sprint']}（{repo}、ADR 0061）で決めた打ち手です。

- 何を: {a.get('what')}
- なぜ: {a.get('why')}
- 根拠のチケット: {', '.join(a.get('tickets') or [])}
"""
        if tgt in ("engine", "process"):
            note = "役割への指示・ラインの設定の変更なので、実装とマージは人の承認です（ADR 0023 の承認の線）。" if tgt == "process" else "工場の改善です。直すのは建設者です。"
            out.append((engine_repo, f"振り返りの打ち手: {a.get('what')}"[:120], body + "\n" + note, ["factory:process-proposal"]))
        elif tgt == "product":
            out.append((repo, f"振り返りの打ち手: {a.get('what')}"[:120], body, ["factory:planned"]))
    return out


def record(cur, repo: str, issue: int, corr_id: str, job_id: int, sprint: dict, result: dict, defs: dict, gh, comment,
           engine_repo: str) -> bool:
    """議事録を残し、固定 Issue にまとめを書き、知見の変更セット・docs/・打ち手を起票し、次の N を決める。スプリントごとに 1 回"""
    m = minutes(sprint, result)
    key = f"retro.held:{repo}:{sprint['sprint']}"
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('retro.held',%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (corr(repo), job_id, Jsonb(m), key))
    if cur.fetchone() is None:
        return False
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('RetroMinutes',%s,%s,'committee',%s)",
                (corr(repo), job_id, Jsonb(m)))
    filed: dict[str, str] = {}
    todo = []
    if (k := knowledge_issue(m)):
        todo.append(("knowledge", repo, k[0], k[1], ["factory:planned", "factory:learn"]))
    if (d := docs_issue(m)):
        todo.append(("docs", repo, d[0], d[1], ["factory:planned", DOCS_LABEL]))
    todo += [(f"action:{i}", r, t, b, ls) for i, (r, t, b, ls) in enumerate(action_issues(m, repo, engine_repo))]
    for what, r, title, body, labels in todo:
        dk = f"retro.filed:{repo}:{sprint['sprint']}:{what}"
        cur.execute("select 1 from events where dedupe_key=%s", (dk,))
        if cur.fetchone():
            continue
        try:
            got = gh("POST", f"repos/{r}/issues", {"title": title, "body": body, "labels": labels})
        except Exception:  # noqa: BLE001  起票できなくても議事録は残る。次の振り返りの棚卸しで拾える
            continue
        url = got.get("html_url") or ""
        filed[title] = url
        cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('retro.filed',%s,%s,%s,%s)",
                    (corr(repo), job_id, Jsonb({"repo": repo, "sprint": sprint["sprint"], "what": what, "issue": url, "target_repo": r}), dk))
        if what == "knowledge":
            # 知見の変更セットは知見のキュー（committee.queue）に入れる。委員会の枠で流し、1 件ずつ、番号がぶつからないように（ADR 0024 の追記）。
            # 品質の Learn（quality_learn）と同じく knowledge.case_filed に source を付けて残す
            themes = sorted({k.get("theme") for k in m["knowledge"] if k.get("destination") != "product_docs" and k.get("theme")})
            cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('knowledge.case_filed',%s,%s,%s,%s) "
                        "on conflict (dedupe_key) do nothing",
                        (f"github:{r}#{got.get('number')}", job_id,
                         Jsonb({"incident": f"retro:{repo}:{sprint['sprint']}", "analysis_event": None, "themes": themes,
                                "learnings": len(m["knowledge"]), "issue": url, "day": datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d"),
                                "source": "retro"}),
                         f"knowledge.case_filed:retro:{repo}:{sprint['sprint']}"))
    if filed:
        cur.execute("update events set payload = payload || %s where dedupe_key=%s", (Jsonb({"filed": filed}), key))
        cur.execute("update artifacts set payload = payload || %s where type='RetroMinutes' and job_id=%s", (Jsonb({"filed": filed}), job_id))
    try:
        comment(repo, issue, markdown(m, filed))
    except Exception:  # noqa: BLE001  まとめは events に残る
        pass
    record_adjust(cur, repo, sprint["sprint"], metrics(sprint["tickets"], m["knowledge"]), defs)
    return True
