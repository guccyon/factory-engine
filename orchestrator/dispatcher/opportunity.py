"""改善の提案（Opportunity。definitions/artifacts.yaml）を残して受信トレイに出す（ADR 0043 の O4。O2 の Report ラインも同じ口を使う想定）。

- artifacts の Opportunity（title・problem・expected_effect は必須。expected_effect は指標・方向・幅）と events の opportunity.proposed に残す
- 受信トレイに「見て、拾うなら起票する」の依頼（task の kind opportunity、プロダクトオーナーの出番）を 1 件積む。起票はしない（人かプランナーが拾う）
- 同じ出どころ（source）・同じ目印（key）の提案は 1 回だけ。LLM は使わない
- 動作確認でわざと入れた詰まりに当たった提案（extra の planted_hit）は、受信トレイの題と案内でそれと分かるようにする
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

import inbox

DIRECTIONS = ("up", "down")
THEMES = {1: "利用者の体験", 2: "信頼性と性能"}   # definitions/learning_taxonomy.yaml の themes のうち、ここから出すもの


def propose(cur, repo: str, source: str, key: str, *, title: str, problem: str, expected_effect: dict, theme: int,
            evidence: list | None = None, correlation_id: str | None = None, job_id: int | None = None, extra: dict | None = None) -> int | None:
    """提案を 1 件残す。返り値は Opportunity の id（同じ source と key がもうあれば None）"""
    eff = expected_effect or {}
    if not title or not problem or not eff.get("metric") or eff.get("direction") not in DIRECTIONS or not eff.get("range"):
        raise ValueError("Opportunity は title・problem・expected_effect（metric・direction: up|down・range）が要る")
    dk = f"opportunity.proposed:{source}:{repo}:{key}"[:400]
    cur.execute("select 1 from events where dedupe_key=%s", (dk,))
    if cur.fetchone():
        return None
    payload = {"repo": repo, "source": source, "key": key, "theme": theme, "title": str(title)[:200], "problem": str(problem)[:2000],
               "expected_effect": {k: eff.get(k) for k in ("metric", "direction", "range")}, "evidence": list(evidence or [])[:20],
               **(extra or {})}
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Opportunity',%s,%s,'learn',%s) returning id",
                (correlation_id, job_id, Jsonb(payload)))
    oid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('opportunity.proposed',%s,%s,%s,%s)",
                (correlation_id, job_id, Jsonb({**payload, "opportunity_id": oid}), dk))
    planted = bool(payload.get("planted_hit"))
    inbox.request(cur, f"opportunity:{oid}", "opportunity", "po", f"改善の提案{'（動作確認の仕込みに当たった）' if planted else ''}: {payload['title']}",
                  action=("動作確認のためにわざと入れた詰まりに当たった提案で、製品の問題の根拠にはならない。確かめたら見たとして閉じる" if planted else
                          "提案を読み、拾うなら製品の Issue に起票する（工場は起票しない）。拾わないなら見たとして閉じる"),
                  correlation_id=correlation_id, by=source)
    return oid


def recent(cur, repo: str | None = None, source: str | None = None, limit: int = 20) -> list[dict]:
    cur.execute("select payload, occurred_at from events where name='opportunity.proposed' and (%s::text is null or payload->>'repo'=%s) "
                "and (%s::text is null or payload->>'source'=%s) order by id desc limit %s", (repo, repo, source, source, limit))
    return [{**r["payload"], "at": r["occurred_at"]} for r in cur.fetchall()]
