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

import re
from datetime import datetime, timedelta, timezone
from statistics import median

from psycopg.types.json import Jsonb

CLASSIFY_PER_TICK = 30   # 1 周で GitHub にラベルを聞くチケットの数の上限（GET は ETag で条件付き）


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
