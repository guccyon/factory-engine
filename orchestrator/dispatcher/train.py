"""定期便（ADR 0043 の O6 release_train）。Gen1 の最小の形で、回路の確認までにとどめる。

便は「承認の単位」と「Release のまとめ方」だけを変える。staging への出し方（ship.py）は今のまま。
- 定期便: supervisor が 1 日 1 便（JST の RELEASE_TRAIN_HOUR 時を過ぎて）、製品ごとにまだ Release に入っていないマージ済みの PR を Release に切り、
  まとめて 1 つの便（release.train）に載せて staging に出す
- 緊急便: S1 の修正だけは便を待たずに出す。S1 の Defect の Issue が閉じたら supervisor が出す（ADR 0053、major_defect.py）。
  人も factoryctl release train <repo> --emergency --reason .. で出せる
- 承認: 本番に出す承認は便ごとに 1 回（factoryctl release train-approve <train_id>）。便に載った Release をまとめて承認する。
  本番を立てない段階（development）の製品だけの便は承認が要らない（approval.required が偽）

便は Release の束（releases の配列）として持つ。Gen1.5 で製品が複数のリポジトリ（backend・mobile・IaC）に分かれたとき、
1 便に複数のリポジトリの Release を載せる形をここで先に持っておく（ADR 0043 の持ち越しの節）。

記録（events）:
  release.train           {train_id, kind: regular|emergency, releases: [{release_id, repo, prs}], approval: {unit, required, pending}, by, reason}
  release.train_approved  {train_id, by, via, approved: [release_id], skipped: [{release_id, why}]}
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import ship
from psycopg.types.json import Jsonb

JST = timezone(timedelta(hours=9))
KINDS = ("regular", "emergency")


def train_id(kind: str, at: datetime) -> str:
    at = at.astimezone(JST)
    return f"train:{at:%Y-%m-%d}:{kind}:{at:%H%M%S}"


def ran_today(cur, repo: str, now: datetime) -> bool:
    """その製品が今日（JST）の定期便にもう載ったか"""
    day = now.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    cur.execute("select 1 from events where name='release.train' and payload->>'kind'='regular' and occurred_at >= %s "
                "and exists (select 1 from jsonb_array_elements(payload->'releases') r where r->>'repo' = %s) limit 1", (day, repo))
    return cur.fetchone() is not None


def record_train(cur, kind: str, releases: list[dict], by: str, reason: str | None = None, now: datetime | None = None) -> str:
    """便を残す。releases は [{release_id, repo, prs}]"""
    if kind not in KINDS:
        raise ValueError(kind)
    now = now or datetime.now(timezone.utc)
    tid = train_id(kind, now)
    required = [r["release_id"] for r in releases if not ship.prod_held(cur, r["repo"])]
    # 時刻は便の時刻（now）にそろえる。ran_today がこの時刻で「今日の便」を見るので、DB の時計とずれると翌日の便を出し損なう
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key, occurred_at) values ('release.train',%s,%s,%s,%s)",
                (tid, Jsonb({"train_id": tid, "kind": kind, "releases": releases, "repos": sorted({r["repo"] for r in releases}),
                             "approval": {"unit": "train", "required": bool(required), "pending": required},
                             "by": by, "reason": reason}), f"release.train:{tid}", now))
    return tid


def cut(cur, repos: list[str]) -> list[dict]:
    """製品ごとに、まだ Release に入っていないマージ済みの PR を Release に切り、staging への出荷を依頼する。
    PR がない・出荷が処理中か承認待ちの製品は載せない"""
    out = []
    for repo in repos:
        if ship.unreleased(cur, repo) == 0 or ship.busy(cur, repo):
            continue
        rid, prs = ship.create_release(cur, repo, ship.next_version(cur, repo))
        if not rid:
            continue
        ship.request_ship(cur, rid, "supervisor", "release_train")
        out.append({"release_id": rid, "repo": repo, "prs": len(prs)})
    return out


def regular(cur, repos: list[str], now: datetime | None = None) -> tuple[str | None, list[dict]]:
    """定期便。今日の便にまだ載っていない製品だけを見る。載せる Release がなければ便を出さない"""
    now = now or datetime.now(timezone.utc)
    rels = cut(cur, [r for r in repos if not ran_today(cur, r, now)])
    if not rels:
        return None, []
    return record_train(cur, "regular", rels, "supervisor", now=now), rels


def emergency(cur, repo: str, by: str, reason: str, now: datetime | None = None) -> tuple[bool, str]:
    """緊急便（S1 の修正）。定期便の時刻と 1 日 1 便を待たずに出す。理由は必須（何の S1 を直すか）"""
    if not (reason or "").strip():
        return False, "緊急便は理由（どの S1 を直すか）が要る"
    if ship.unreleased(cur, repo) == 0:
        return False, "まだ Release に入っていないマージ済みの PR がない（修正の PR がマージされてから出す）"
    if ship.busy(cur, repo):
        return False, "出荷が処理中か承認待ち（factoryctl release status で見る）"
    rels = cut(cur, [repo])
    if not rels:
        return False, "Release を切れなかった"
    tid = record_train(cur, "emergency", rels, by, reason=reason.strip()[:300], now=now)
    return True, f"緊急便 {tid} を出した（{rels[0]['release_id']}、PR {rels[0]['prs']} 件）"


def train_of(cur, tid: str) -> dict | None:
    cur.execute("select payload from events where name='release.train' and payload->>'train_id'=%s", (tid,))
    r = cur.fetchone()
    return r["payload"] if r else None


def approve(cur, tid: str, by: str, via: str, skip_qa: str | None = None) -> tuple[bool, str]:
    """便ごとに 1 回の承認。便に載った Release のうち、本番に出す段階の製品のものをまとめて承認する。
    QA の関門（ADR 0049）の報告の前の Release は skip_qa（理由）がなければ承認しない（skipped に理由が残る）"""
    t = train_of(cur, tid)
    if not t:
        return False, f"{tid} という便はない"
    cur.execute("select 1 from events where name='release.train_approved' and payload->>'train_id'=%s", (tid,))
    if cur.fetchone():
        return False, "この便はもう承認した"
    approved, skipped = [], []
    for r in t["releases"]:
        ok, m = ship.request_approve(cur, r["release_id"], by, f"{via}（便 {tid}）", skip_qa=skip_qa)
        (approved.append(r["release_id"]) if ok else skipped.append({"release_id": r["release_id"], "why": m}))
    if not approved:
        return False, "承認できる Release がない: " + "；".join(f"{s['release_id']} {s['why']}" for s in skipped)
    cur.execute("insert into events(name, correlation_id, payload) values ('release.train_approved',%s,%s)",
                (tid, Jsonb({"train_id": tid, "by": by, "via": via, "approved": approved, "skipped": skipped})))
    return True, f"便 {tid} を承認した（{len(approved)} 件を prod へ）" + (f"。{len(skipped)} 件は承認しなかった" if skipped else "")


def recent(cur, limit: int = 10) -> list[dict]:
    """最近の便。承認した便には approved（承認の記録）を付ける"""
    cur.execute("select t.payload, t.occurred_at, a.payload as approved from events t left join lateral "
                "(select payload from events a where a.name='release.train_approved' and a.payload->>'train_id'=t.payload->>'train_id' limit 1) a on true "
                "where t.name='release.train' order by t.id desc limit %s", (limit,))
    return [{**r["payload"], "at": r["occurred_at"], "approved": r["approved"]} for r in cur.fetchall()]
