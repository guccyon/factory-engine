"""改善の提案（Opportunity。definitions/artifacts.yaml）を残して受信トレイに出す（ADR 0043 の O4。O2 の Report ラインも同じ口を使う想定）。

- artifacts の Opportunity（title・problem・expected_effect は必須。expected_effect は指標・方向・幅）と events の opportunity.proposed に残す
- 受信トレイに「見て、拾うなら起票する」の依頼（task の kind opportunity、プロダクトオーナーの出番）を 1 件積む。提案を出す側は起票しない
- 受信トレイで「起票する」と答えたら頼みだけを記録し（request_ticket。受信トレイの入口は GitHub のトークンを持たない）、
  supervisor が次の周期にプランナー宛ての Issue（factory:plan）を作る（file_requested）。要否と経路はプランナーが決める（2026-11-10 オーナー）
- 同じ出どころ（source）・同じ目印（key）の提案は 1 回だけ。LLM は使わない
- 動作確認でわざと入れた詰まりに当たった提案（extra の planted_hit）は、受信トレイの題と案内でそれと分かるようにする
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

import inbox

DIRECTIONS = ("up", "down")
TICKET_LABEL = "factory:plan"   # プランナーの入口（planner.py の TRIGGER_LABEL）
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
                          "提案を読み、拾うなら「起票する」でプランナーに回す。拾わないなら見たとして閉じる"),
                  correlation_id=correlation_id, by=source)
    return oid


def recent(cur, repo: str | None = None, source: str | None = None, limit: int = 20) -> list[dict]:
    cur.execute("select payload, occurred_at from events where name='opportunity.proposed' and (%s::text is null or payload->>'repo'=%s) "
                "and (%s::text is null or payload->>'source'=%s) order by id desc limit %s", (repo, repo, source, source, limit))
    return [{**r["payload"], "at": r["occurred_at"]} for r in cur.fetchall()]


def get(cur, oid: int) -> dict | None:
    cur.execute("select payload from artifacts where type='Opportunity' and id=%s", (oid,))
    r = cur.fetchone()
    return r["payload"] if r else None


def _oid(key: str) -> int | None:
    head, _, rest = (key or "").partition(":")
    return int(rest) if head == "opportunity" and rest.isdigit() else None


def can_file(cur, key: str) -> bool:
    """受信トレイで「起票する」を出せる提案か（仕込みに当たった提案は製品の問題の根拠にならないので出さない）"""
    oid = _oid(key)
    p = get(cur, oid) if oid else None
    return bool(p and not p.get("planted_hit"))


def request_ticket(cur, key: str, by: str, note: str = "") -> tuple[bool, str]:
    """受信トレイの「起票する」。頼みを記録するだけで、Issue は supervisor が次の周期に作る（file_requested）"""
    oid = _oid(key)
    p = get(cur, oid) if oid else None
    if not p:
        return False, "起票する提案が見つからない"
    if p.get("planted_hit"):
        return False, "動作確認の仕込みに当たった提案は起票しない。見たとして閉じる"
    cur.execute("insert into events(name, payload, dedupe_key) values ('opportunity.ticket_requested',%s,%s) on conflict do nothing returning id",
                (Jsonb({"opportunity_id": oid, "repo": p["repo"], "title": p["title"], "by": str(by)[:80], "note": str(note or "")[:500]}),
                 f"opportunity.ticket_requested:{oid}"))
    if not cur.fetchone():
        return False, "この提案はもう起票を頼んである"
    return True, f"提案を起票に回す。次の周期に {p['repo']} にプランナー宛ての Issue（{TICKET_LABEL}）を作り、要否と経路はプランナーが決める"


def ticket_body(oid: int, p: dict, note: str = "") -> str:
    eff = p.get("expected_effect") or {}
    lines = ["## 背景", "", f"工場の改善の提案（{p.get('source')}）を、受信トレイでプロダクトオーナーが起票に回しました。", "",
             "## 問題", "", str(p.get("problem") or ""), "",
             "## 期待する効果", "", f"- 指標: `{eff.get('metric')}`", f"- 向き: {'上げる' if eff.get('direction') == 'up' else '下げる'}",
             f"- 幅: {eff.get('range')}"]
    if note.strip():
        lines += ["", "## 起票した人の一言", "", note.strip()]
    ev = [str(e) for e in p.get("evidence") or []]
    if ev:
        lines += ["", "## 根拠", ""] + [f"- {e}" for e in ev[:10]]
    lines += ["", "## プランナーへ", "",
              "要否と経路を決めてください。小さな変更 1 本で済むなら、それで構いません。",
              "変更が製品の KPI を動かすと見込めるなら、Requirements の `kpi_forecast` に見込みを書いてください"
              + (f"（この提案の指標は `{eff.get('metric')}`）。" if eff.get("metric") else "。"),
              "", f"<!-- factory:opportunity:{oid} -->"]
    return "\n".join(lines)


def file_requested(cur, gh, log=None) -> list[str]:
    """「起票する」と頼まれ、まだ Issue にしていない提案を、プランナー宛ての Issue にする（supervisor の周期。GitHub のトークンを持つ側）"""
    cur.execute("""select r.payload from events r where r.name='opportunity.ticket_requested'
                     and not exists (select 1 from events f where f.name='opportunity.ticket_filed' and f.payload->>'opportunity_id' = r.payload->>'opportunity_id')
                   order by r.id limit 5""")
    out = []
    for r in cur.fetchall():
        q = r["payload"]
        p = get(cur, int(q["opportunity_id"])) or {}
        try:
            issue = gh("POST", f"repos/{q['repo']}/issues", {"title": str(p.get("title") or q["title"])[:200],
                                                             "body": ticket_body(int(q["opportunity_id"]), p, q.get("note") or ""),
                                                             "labels": [TICKET_LABEL]})
        except Exception as e:  # noqa: BLE001  次の周期にもう一度試す
            if log:
                log("opportunity ticket failed", q["opportunity_id"], repr(e)[:200])
            continue
        corr = f"github:{q['repo']}#{issue['number']}"
        cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('opportunity.ticket_filed',%s,%s,%s) on conflict do nothing",
                    (corr, Jsonb({"opportunity_id": q["opportunity_id"], "repo": q["repo"], "issue": issue["html_url"], "by": q.get("by")}),
                     f"opportunity.ticket_filed:{q['opportunity_id']}"))
        if log:
            log("opportunity ticket filed", q["opportunity_id"], issue["html_url"])
        out.append(issue["html_url"])
    return out
