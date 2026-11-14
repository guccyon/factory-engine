"""メジャー不具合の即時修正と再リリース（ADR 0053）。

メジャー不具合 = 利用者が先に進めない・業務が止まるレベルの不具合。QA 役の重要度 S1（止める）と S2（流れが exit まで通らない）がこれに当たる
（ADR 0053 の 1 の改訂。ラインの差し止めは今までどおり S1 だけ）。
工場は人の承認を待たずに、修正 → 再リリースまでを自分で進める。

- 起票: メジャーの Defect の Issue は factory:fix-proposed（人が流す 1 タップ）ではなく factory:plan にし、p0 を付ける（incident.from_qa から呼ぶ）。
  プランナーは着手判定だけをし、方針が要ればスパイクのレーン（System Architect）が原因と直し方を決め、同じ原因の不具合を束ねる（ADR 0055）
- 再リリース: メジャーの Incident の Defect の Issue が閉じ（修正の PR がマージされ）、まだ Release に入っていないマージ済みの PR があれば、
  supervisor が緊急便（train.emergency）を出す。Incident ごとに 1 回（major.rereleased）
- 本番の承認: その緊急便が S1 の修正を載せていて、本番を立てる段階の製品の Release が載っていれば、staging に出た後に supervisor が承認する
  （QA の関門の報告は待たない。release.approved の qa_skipped に理由が残る）。S2 だけの緊急便は staging まで出し、本番は QA の関門の報告と
  承認者の判断を待つ（ADR 0053 の追記 2026-11-14。緊急便はその時点の main をまるごと載せるので、S2 まで自動で本番に出すと
  ほかの変更も関門と承認を通らずに本番に出続ける）
- 本番で見つかったメジャー不具合の revert（ADR 0053 の決定 3）は本番を立てる製品が出てから実装する（rally は development）

LLM は使わない。失敗しても supervisor の周回は止めない（呼び出し側で握る）。
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

import incident
import ship
import train

MAJOR_SEVERITIES = ("s1", "s2")
AUTO_APPROVE_SEVERITIES = ("s1",)   # 工場が本番まで自分で承認する緊急便の重要度（ADR 0053 の追記 2026-11-14）
FIX_PROPOSED, PLAN = "factory:fix-proposed", "factory:plan"
REREL = "major.rereleased"
APPROVE_VIA = "メジャー不具合の即時再リリース（ADR 0053）"


def is_major(severity: str | None) -> bool:
    return severity in MAJOR_SEVERITIES


def issue_number(url: str | None) -> int | None:
    num = (url or "").rstrip("/").rsplit("/", 1)[-1].lstrip("#")
    return int(num) if num.isdigit() else None


def plan_fix(gh, repo: str, num: int) -> None:
    """メジャー不具合の Issue を人の 1 タップなしで流す（fix-proposed を外し、p0 とプランナーの着手判定を付ける）"""
    gh("POST", f"repos/{repo}/issues/{num}/labels", {"labels": [PLAN, incident.HOLD_PRIORITY_LABEL]})
    try:
        gh("DELETE", f"repos/{repo}/issues/{num}/labels/{FIX_PROPOSED.replace(':', '%3A')}")
    except Exception:  # noqa: BLE001, S110  付いていなければ 404
        pass


def rereleased(cur, iid: str) -> dict | None:
    cur.execute("select payload from events where name=%s and payload->>'incident_id'=%s limit 1", (REREL, iid))
    r = cur.fetchone()
    return r["payload"] if r else None


def issue_closed(gh, repo: str, num: int) -> bool:
    return (gh("GET", f"repos/{repo}/issues/{num}") or {}).get("state") == "closed"


def mark(cur, inc: dict, repo: str, tid: str) -> None:
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values (%s,%s,%s,%s) on conflict (dedupe_key) do nothing",
                (REREL, tid, Jsonb({"incident_id": inc["incident_id"], "repo": repo, "issue": inc.get("issue"), "train_id": tid}),
                 f"{REREL}:{inc['incident_id']}"))


def rerelease_step(cur, gh, log=None) -> list[str]:
    """メジャーの Incident のうち、Defect の Issue が閉じたものを緊急便で出す。返り値は出した便の id"""
    out, shipped = [], {}
    for inc in incident.open_incidents(cur):
        if not is_major(inc.get("severity")) or inc.get("drill") or rereleased(cur, inc["incident_id"]):
            continue
        repo, num = inc["repo"], issue_number(inc.get("issue"))
        if num is None or not issue_closed(gh, repo, num):
            continue
        if repo in shipped:   # 同じ原因をまとめて直した PR で閉じたもの: この周回の便に載っている
            mark(cur, inc, repo, shipped[repo])
            continue
        ok, msg = train.emergency(cur, repo, "supervisor", f"メジャー不具合の修正: {inc.get('issue')}（{inc.get('title') or ''}）")
        if not ok:
            if log:
                log("major rerelease waiting", inc["incident_id"], msg)
            continue
        cur.execute("select payload->>'train_id' as tid from events where name='release.train' and payload->>'kind'='emergency' "
                    "and payload->'repos' ? %s order by id desc limit 1", (repo,))
        tid = cur.fetchone()["tid"]
        mark(cur, inc, repo, tid)
        shipped[repo] = tid
        out.append(tid)
        if log:
            log("major rerelease", inc["incident_id"], msg)
    return out


def carries_auto_approved(cur, tid: str) -> bool:
    """その緊急便が、工場が本番まで承認する重要度（S1）の Incident の修正を載せているか"""
    cur.execute("select 1 from events r join events o on o.name='incident.opened' and o.payload->>'incident_id' = r.payload->>'incident_id' "
                "where r.name=%s and r.payload->>'train_id'=%s and o.payload->>'severity' = any(%s) limit 1",
                (REREL, tid, list(AUTO_APPROVE_SEVERITIES)))
    return cur.fetchone() is not None


def approve_step(cur, log=None) -> list[str]:
    """メジャー不具合の緊急便のうち、本番の承認が要り、まだ承認していないものを承認する（staging に出る前は次の周回で）"""
    cur.execute("select r.payload->>'train_id' as tid from events r where r.name=%s and not exists "
                "(select 1 from events a where a.name='release.train_approved' and a.payload->>'train_id' = r.payload->>'train_id')", (REREL,))
    out = []
    for row in cur.fetchall():
        t = train.train_of(cur, row["tid"])
        if not t or not t["approval"].get("required") or not carries_auto_approved(cur, row["tid"]):
            continue
        staged = all(ship.status(cur, r["repo"])["environments"].get("staging", {}).get("release_id") == r["release_id"] for r in t["releases"])
        if not staged:
            continue
        ok, msg = train.approve(cur, row["tid"], "supervisor", APPROVE_VIA, skip_qa=APPROVE_VIA)
        if log:
            log("major approve", row["tid"], ok, msg)
        if ok:
            out.append(row["tid"])
    return out
