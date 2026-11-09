"""改善の提案（Opportunity。definitions/artifacts.yaml）を残して受信トレイに出す（ADR 0043 の O4。O2 の Report ラインも同じ口を使う想定）。

- artifacts の Opportunity（title・problem・expected_effect は必須。expected_effect は指標・方向・幅）と events の opportunity.proposed に残す
- 受信トレイに「見て、拾うなら起票する」の依頼（task の kind opportunity、プロダクトオーナーの出番）を 1 件積む。起票はしない（人かプランナーが拾う）
- 同じ出どころ（source）・同じ目印（key）の提案は 1 回だけ。LLM は使わない
- 合成の利用者の仕込み（SYNTH_FRICTION）に当たった提案（extra の planted_hit）は、除けを本物の提案と分ける（仕込みの回で一度出た key が、
  仕込みなしの回の本物の同じ迷いを止めないように）。受信トレイでも「仕込みに当たった」と分かる見出しと頼みごとにする
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

import inbox

DIRECTIONS = ("up", "down")
THEMES = {1: "利用者の体験", 2: "信頼性と性能"}   # definitions/learning_taxonomy.yaml の themes のうち、ここから出すもの


def propose(cur, repo: str, source: str, key: str, *, title: str, problem: str, expected_effect: dict, theme: int,
            evidence: list | None = None, correlation_id: str | None = None, job_id: int | None = None, extra: dict | None = None) -> int | None:
    """提案を 1 件残す。返り値は Opportunity の id（同じ source と key がもうあれば None。仕込みに当たったものとそうでないものは別に数える）"""
    eff = expected_effect or {}
    if not title or not problem or not eff.get("metric") or eff.get("direction") not in DIRECTIONS or not eff.get("range"):
        raise ValueError("Opportunity は title・problem・expected_effect（metric・direction: up|down・range）が要る")
    planted = (extra or {}).get("planted_hit")
    dk = _dedupe_key(cur, source, repo, key, planted)
    if dk is None:
        return None
    payload = {"repo": repo, "source": source, "key": key, "theme": theme, "title": str(title)[:200], "problem": str(problem)[:2000],
               "expected_effect": {k: eff.get(k) for k in ("metric", "direction", "range")}, "evidence": list(evidence or [])[:20],
               **(extra or {})}
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Opportunity',%s,%s,'learn',%s) returning id",
                (correlation_id, job_id, Jsonb(payload)))
    oid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('opportunity.proposed',%s,%s,%s,%s)",
                (correlation_id, job_id, Jsonb({**payload, "opportunity_id": oid}), dk))
    if planted:
        inbox.request(cur, f"opportunity:{oid}", "opportunity", "po", f"改善の提案（仕込みに当たった: {planted}）: {payload['title']}",
                      action=(f"わざと仕込んだ詰まり（{planted}。工場の SYNTH_FRICTION、製品のコードには入っていない）に当たった提案。"
                              "合成の利用者が仕込みに気づけた印で、製品の不具合ではない。見たとして閉じる（起票しない）"),
                      correlation_id=correlation_id, by=source)
    else:
        inbox.request(cur, f"opportunity:{oid}", "opportunity", "po", f"改善の提案: {payload['title']}",
                      action="提案を読み、拾うなら製品の Issue に起票する（工場は起票しない）。拾わないなら見たとして閉じる",
                      correlation_id=correlation_id, by=source)
    return oid


def _dedupe_key(cur, source: str, repo: str, key: str, planted: str | None) -> str | None:
    """除けの key。もう出した提案なら None。仕込みに当たった提案は planted: の下で別に数える（本物の提案を止めない）。
    この分け方の前に仕込みの提案が本物の key を使っていた分（payload に planted_hit がある）は、仕込みの除けに数え、本物は :real の key で出す"""
    base = f"opportunity.proposed:{source}:{repo}:{key}"
    if planted:
        cur.execute("select 1 from events where dedupe_key=%s and payload->>'planted_hit' is not null", (base[:400],))
        if cur.fetchone():
            return None   # 分ける前に出した仕込みの提案と同じ
        cands = [f"opportunity.proposed:{source}:{repo}:planted:{key}"[:400]]
    else:
        cands = [base[:400], (base[:395] + ":real")]
    for dk in cands:
        cur.execute("select payload->>'planted_hit' as hit from events where dedupe_key=%s", (dk,))
        row = cur.fetchone()
        if not row:
            return dk
        if planted or not row["hit"]:
            return None   # 同じ提案がもうある
    return None


def recent(cur, repo: str | None = None, source: str | None = None, limit: int = 20) -> list[dict]:
    cur.execute("select payload, occurred_at from events where name='opportunity.proposed' and (%s::text is null or payload->>'repo'=%s) "
                "and (%s::text is null or payload->>'source'=%s) order by id desc limit %s", (repo, repo, source, source, limit))
    return [{**r["payload"], "at": r["occurred_at"]} for r in cur.fetchall()]
