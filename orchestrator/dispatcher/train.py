"""定期便（ADR 0043 の O6 release_train）。Gen1 の最小の形で、回路の確認までにとどめる。

便は「承認の単位」と「Release のまとめ方」だけを変える。staging への出し方（ship.py）は今のまま。
- 定期便: supervisor が 1 日 1 便（JST の RELEASE_TRAIN_HOUR 時を過ぎて）、製品ごとにまだ Release に入っていないマージ済みの PR を Release に切り、
  まとめて 1 つの便（release.train）に載せて staging に出す
- 緊急便: S1 の修正だけは便を待たずに出す。S1 の Defect の Issue が閉じたら supervisor が出す（ADR 0053、major_defect.py）。
  人も factoryctl release train <repo> --emergency --reason .. で出せる
- 直しだけの便（mode: hotfix、ADR 0064）: 本番を立てた製品の S1 の緊急便は、main をまるごと載せず、いま本番に出ている版のコミットに
  直しの PR のコミットだけを cherry-pick した版（patch を 1 つ上げる）で出す。ぶつかったら main をまるごと載せる便（mode: fallback）に
  切り替え、本番は自動で承認しない（auto_approve: false）
- 承認: 本番に出す承認は便ごとに 1 回（factoryctl release train-approve <train_id>）。便に載った Release をまとめて承認する。
  本番を立てない段階（development）の製品だけの便は承認が要らない（approval.required が偽）

便は Release の束（releases の配列）として持つ。Gen1.5 で製品が複数のリポジトリ（backend・mobile・IaC）に分かれたとき、
1 便に複数のリポジトリの Release を載せる形をここで先に持っておく（ADR 0043 の持ち越しの節）。

記録（events）:
  release.train           {train_id, kind: regular|emergency, releases: [{release_id, repo, prs}], approval: {unit, required, pending}, by, reason,
                           mode?: hotfix|fallback, auto_approve?: false, fallback_of?}
  release.train_approved  {train_id, by, via, approved: [release_id], skipped: [{release_id, why}]}
  release.train_dismissed {train_id, by, reason}   承認せずに見送った便（中身が後の便で本番に出たなど）。承認待ちから外す
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


def record_train(cur, kind: str, releases: list[dict], by: str, reason: str | None = None, now: datetime | None = None,
                 extra: dict | None = None) -> str:
    """便を残す。releases は [{release_id, repo, prs}]。extra は直しだけの便の mode など（ADR 0064）"""
    if kind not in KINDS:
        raise ValueError(kind)
    now = now or datetime.now(timezone.utc)
    tid = train_id(kind, now)
    base, n = tid, 1
    while train_of(cur, tid):   # 同じ秒に 2 便目（直しだけの便を切り替えたときなど）
        n += 1
        tid = f"{base}-{n}"
    required = [r["release_id"] for r in releases if not ship.prod_held(cur, r["repo"])]
    # 時刻は便の時刻（now）にそろえる。ran_today がこの時刻で「今日の便」を見るので、DB の時計とずれると翌日の便を出し損なう
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key, occurred_at) values ('release.train',%s,%s,%s,%s)",
                (tid, Jsonb({**(extra or {}), "train_id": tid, "kind": kind, "releases": releases, "repos": sorted({r["repo"] for r in releases}),
                             "approval": {"unit": "train", "required": bool(required), "pending": required},
                             "by": by, "reason": reason}), f"release.train:{tid}", now))
    return tid


def cut(cur, repos: list[str], emergency: bool = False, allow_empty: bool = False, extra: dict | None = None) -> list[dict]:
    """製品ごとに、まだ Release に入っていないマージ済みの PR を Release に切り、staging への出荷を依頼する。
    PR がない・出荷が処理中か承認待ちの製品は載せない（緊急便は承認待ちを追い越す）。
    allow_empty は直しだけの便がぶつかったときの切り替え用（直しの PR は先の Release に入ったが、main の先頭を出す）"""
    out = []
    for repo in repos:
        if (ship.unreleased(cur, repo) == 0 and not allow_empty) or ship.busy(cur, repo, emergency=emergency):
            continue
        rid, prs = ship.create_release(cur, repo, ship.next_version(cur, repo), extra=extra)
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


def emergency(cur, repo: str, by: str, reason: str, now: datetime | None = None, *, allow_empty: bool = False,
              extra: dict | None = None) -> tuple[bool, str]:
    """緊急便（S1 の修正）。定期便の時刻と 1 日 1 便を待たずに出す。理由は必須（何の S1 を直すか）。
    extra は便に足す項目（ぶつかったときの切り替えの mode: fallback・auto_approve: false。ADR 0064 の決定 2）"""
    if not (reason or "").strip():
        return False, "緊急便は理由（どの S1 を直すか）が要る"
    if ship.unreleased(cur, repo) == 0 and not allow_empty:
        return False, "まだ Release に入っていないマージ済みの PR がない（修正の PR がマージされてから出す）"
    if ship.busy(cur, repo, emergency=True):
        return False, "出荷が処理中（factoryctl release status で見る）"
    rels = cut(cur, [repo], emergency=True, allow_empty=allow_empty,
               extra={k: v for k, v in (extra or {}).items() if k in ("mode", "fallback_of")})
    if not rels:
        return False, "Release を切れなかった"
    tid = record_train(cur, "emergency", rels, by, reason=reason.strip()[:300], now=now, extra=extra)
    return True, f"緊急便 {tid} を出した（{rels[0]['release_id']}、PR {rels[0]['prs']} 件）"


def hotfix(cur, repo: str, base: dict, picks: list[dict], by: str, reason: str, now: datetime | None = None) -> tuple[bool, str, str | None]:
    """直しだけの便（ADR 0064 の決定 1）。base はいま本番に出ている版（ship.prod_base）、picks は直しの PR（[{pr_url, merge_commit}]、マージの順）。
    Release には直しの PR だけを入れ、版は本番の版の patch を 1 つ上げる。main のほかの変更は次の定期便で QA の関門を通って出る。
    cherry-pick は shipper が行い、ぶつかったら release.ship_failed（hotfix_conflict）を残す（切り替えは major_defect.fallback_step）"""
    if not (reason or "").strip():
        return False, "緊急便は理由（どの S1 を直すか）が要る", None
    picks = [p for p in picks if p.get("merge_commit")]
    if not picks:
        return False, "載せる直しの PR（マージのコミット）がない", None
    if ship.busy(cur, repo, emergency=True):
        return False, "出荷が処理中（factoryctl release status で見る）", None
    version = ship.patch_version(cur, repo, base["version"])
    if not version:
        return False, f"本番の版 {base['version']} から直しの版を作れない（数字 3 つの版でない）", None
    rid, prs = ship.create_release(cur, repo, version, prs=[p["pr_url"] for p in picks],
                                   extra={"mode": "hotfix", "base_release": base["release_id"], "base_commit": base["commit"],
                                          "picks": [p["merge_commit"] for p in picks]})
    if not rid:
        return False, f"{repo}@{version} はもうある", None
    ship.request_ship(cur, rid, "supervisor", "hotfix")
    tid = record_train(cur, "emergency", [{"release_id": rid, "repo": repo, "prs": len(prs)}], by, reason=reason.strip()[:300], now=now,
                       extra={"mode": "hotfix", "base_release": base["release_id"]})
    return True, f"直しだけの便 {tid} を出した（{rid}、{base['release_id']} に直しの PR {len(prs)} 件）", tid


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
    cur.execute("select 1 from events where name='release.train_dismissed' and payload->>'train_id'=%s", (tid,))
    if cur.fetchone():
        return False, "この便は見送った"
    approved, skipped = [], []
    for r in t["releases"]:
        ok, m = ship.request_approve(cur, r["release_id"], by, f"{via}（便 {tid}）", skip_qa=skip_qa)
        (approved.append(r["release_id"]) if ok else skipped.append({"release_id": r["release_id"], "why": m}))
    if not approved:
        return False, "承認できる Release がない: " + "；".join(f"{s['release_id']} {s['why']}" for s in skipped)
    cur.execute("insert into events(name, correlation_id, payload) values ('release.train_approved',%s,%s)",
                (tid, Jsonb({"train_id": tid, "by": by, "via": via, "approved": approved, "skipped": skipped})))
    return True, f"便 {tid} を承認した（{len(approved)} 件を prod へ）" + (f"。{len(skipped)} 件は承認しなかった" if skipped else "")


def dismiss(cur, tid: str, by: str, reason: str) -> tuple[bool, str]:
    """承認待ちの便を承認せずに見送る（11/16: 緊急便が続けて出て、後の便が先の便の中身ごと本番に出たため、先の 3 便が承認待ちのまま残った）。
    Release と環境には触らない。記録を残して承認待ちから外すだけ"""
    if not reason.strip():
        return False, "見送る理由（--reason）が要る"
    if not train_of(cur, tid):
        return False, f"{tid} という便はない"
    cur.execute("select name from events where name = any(%s) and payload->>'train_id'=%s limit 1",
                (["release.train_approved", "release.train_dismissed"], tid))
    r = cur.fetchone()
    if r:
        return False, "この便はもう承認した" if r["name"] == "release.train_approved" else "この便はもう見送った"
    cur.execute("insert into events(name, correlation_id, payload) values ('release.train_dismissed',%s,%s)",
                (tid, Jsonb({"train_id": tid, "by": by, "reason": reason})))
    return True, f"便 {tid} を見送った（承認待ちから外した）"


def recent(cur, limit: int = 10) -> list[dict]:
    """最近の便。承認した便には approved（承認の記録）、見送った便には dismissed（見送りの記録）を付ける"""
    cur.execute("select t.payload, t.occurred_at, a.payload as approved, d.payload as dismissed from events t left join lateral "
                "(select payload from events a where a.name='release.train_approved' and a.payload->>'train_id'=t.payload->>'train_id' limit 1) a on true "
                "left join lateral (select payload from events d where d.name='release.train_dismissed' and d.payload->>'train_id'=t.payload->>'train_id' limit 1) d on true "
                "where t.name='release.train' order by t.id desc limit %s", (limit,))
    return [{**r["payload"], "at": r["occurred_at"], "approved": r["approved"], "dismissed": r["dismissed"]} for r in cur.fetchall()]
