"""依頼者ごとの好み（ADR 0040 の決定 6）。制御プレーンの requester_preferences から、この回の依頼者・業務に効くものを選ぶ。

- 渡すのは active で confirmed のものだけ（知見と同じく、推し量りの段階では作業を変えない）
- 業務を書いた好みは、この回の業務かその祖先に当たるときだけ。書いていない好みはその依頼者の作業すべてに効く
- 渡した記録は requester_preference.used に id（P-NNNN）だけ残す。中身は events に書かない
"""
from __future__ import annotations

LIMIT = 20


def pid(n: int) -> str:
    return f"P-{int(n):04d}"


def _ancestors(node: str) -> set[str]:
    parts = node.split(".")
    return {".".join(parts[:i]) for i in range(1, len(parts) + 1)}


def applies(pref_work: list[str] | None, job_work: list[str]) -> bool:
    """好みの業務がこの回の業務（かその祖先）に当たるか。好みが業務を持たなければ当たる"""
    if not pref_work:
        return True
    near: set[str] = set()
    for w in job_work:
        near |= _ancestors(w)
    return bool(set(pref_work) & near)


def for_job(cur, product: str, requester: str | None, work: list[str]) -> list[dict]:
    """この回に渡す好み。依頼者が分からなければ渡さない"""
    if not requester or not product:
        return []
    # 表がまだない DB（007 を当てる前）でも相談を止めない。失敗は savepoint で戻し、好みなしで進める
    cur.execute("savepoint requester_prefs")
    try:
        cur.execute("select id, statement, work, axes, verified_at from requester_preferences "
                    "where product=%s and requester=%s and status='active' and confidence='confirmed' "
                    "order by verified_at desc, id desc limit %s", (product, requester, LIMIT * 3))
        rows = cur.fetchall()
        cur.execute("release savepoint requester_prefs")
    except Exception:  # noqa: BLE001
        cur.execute("rollback to savepoint requester_prefs")
        return []
    out = []
    for r in rows:
        if applies(r.get("work") or [], work):
            out.append({"id": pid(r["id"]), "statement": r["statement"], "work": list(r.get("work") or []),
                        "verified_at": str(r.get("verified_at") or "")})
        if len(out) >= LIMIT:
            break
    return out

