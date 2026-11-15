"""実装後の画面（exit_capture）への「直してほしい（Issue にする）」（2026-11-15 オーナー）。

- 受信トレイで画面に「直してほしい」と答えると、違和感の印（taste.flagged。教師信号）を残したうえで、直す頼みを記録する（request）
- 受信トレイの入口は GitHub のトークンを持たないので、Issue は supervisor が次の周期に作る（file_requested）。
  宛先は画面の PR と同じリポジトリのプランナー（factory:plan）。要否と経路はプランナーが決める
- 確認の件は閉じない（印と同じ）。マージも止めない。同じ画面・同じコメントの頼みは 1 回だけ。LLM は使わない
"""
from __future__ import annotations

import hashlib

from psycopg.types.json import Jsonb

TICKET_LABEL = "factory:plan"   # プランナーの入口（planner.py の TRIGGER_LABEL。opportunity.py と同じ）


def _get(cur, aid: int) -> dict | None:
    cur.execute("select correlation_id, payload from artifacts where id=%s and type='HumanChoice'", (aid,))
    return cur.fetchone()


def request(cur, aid: int, note: str, by: str, option_id: str | None = None, viewpoint: str | None = None) -> tuple[bool, str]:
    """直す頼みを記録する。印（taste.flagged）も同じ答えで残す"""
    import choice
    if not (note or "").strip():
        return False, "直してほしい点をコメントに書く"
    r = _get(cur, aid)
    if not r or r["payload"].get("kind") != "exit_capture":
        return False, "直す頼みは実装後の画面だけ"
    sub = r["payload"].get("subject") or {}
    if not sub.get("repo"):
        return False, "画面のリポジトリが分からない"
    ok, msg = choice.flag(cur, aid, note, by, option_id, viewpoint)
    if not ok:
        return ok, msg
    o = next((x for x in r["payload"].get("options") or [] if x["id"] == option_id), {})
    digest = hashlib.sha1(f"{option_id}|{note.strip()}".encode()).hexdigest()[:12]
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('capture.fix_requested',%s,%s,%s) "
                "on conflict do nothing returning id",
                (r["correlation_id"], Jsonb({"artifact_id": aid, "repo": sub["repo"], "pr_url": sub.get("pr_url") or "", "app": sub.get("app") or "",
                                             "ticket": sub.get("ticket") or "", "option": option_id, "option_label": o.get("label"),
                                             "artifact_url": sub.get("artifact_url") or "", "note": note.strip()[:500], "by": str(by)[:80]}),
                 f"capture.fix_requested:{aid}:{digest}"))
    if not cur.fetchone():
        return False, "同じ画面に同じコメントで、もう直す頼みを出してある"
    return True, f"直す頼みを記録した。次の周期に {sub['repo']} にプランナー宛ての Issue（{TICKET_LABEL}）を作る。確認の件は開いたまま"


def title(q: dict) -> str:
    where = f"{q.get('app') or '画面'}" + (f"「{q['option_label']}」" if q.get("option_label") else "")
    head = (q.get("note") or "").splitlines()[0][:60]
    return f"{where}を直す: {head}"[:200]


def ticket_body(q: dict) -> str:
    lines = ["## 背景", "", "実装後の画面の確認で、オーナーが「直してほしい」と答えました。", "",
             "## 直してほしい点", "", str(q.get("note") or ""), "", "## 画面", ""]
    if q.get("option_label"):
        lines.append(f"- 画面: {q['option_label']}（{q.get('option') or ''}）")
    if q.get("app"):
        lines.append(f"- アプリ: {q['app']}")
    if q.get("pr_url"):
        lines.append(f"- 出どころの PR: {q['pr_url']}")
    if q.get("ticket"):
        lines.append(f"- 出どころのチケット: #{q['ticket']}")
    if q.get("artifact_url"):
        lines.append(f"- 前・後・差分の画像（CI の成果物）: {q['artifact_url']}")
    lines += ["", "## プランナーへ", "", "要否と経路を決めてください。小さな変更 1 本で済むなら、それで構いません。",
              "", f"<!-- factory:capture-fix:{q.get('artifact_id')} -->"]
    return "\n".join(lines)


def file_requested(cur, gh, log=None) -> list[str]:
    """直す頼みのうち、まだ Issue にしていないものを Issue にする（supervisor の周期。GitHub のトークンを持つ側）"""
    cur.execute("""select r.id, r.payload from events r where r.name='capture.fix_requested'
                     and not exists (select 1 from events f where f.name='capture.fix_filed' and (f.payload->>'request_id')::bigint = r.id)
                   order by r.id limit 5""")
    out = []
    for r in cur.fetchall():
        q = r["payload"]
        try:
            issue = gh("POST", f"repos/{q['repo']}/issues", {"title": title(q), "body": ticket_body(q), "labels": [TICKET_LABEL]})
        except Exception as e:  # noqa: BLE001  次の周期にもう一度試す
            if log:
                log("capture fix ticket failed", r["id"], repr(e)[:200])
            continue
        cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('capture.fix_filed',%s,%s,%s) on conflict do nothing",
                    (f"github:{q['repo']}#{issue['number']}",
                     Jsonb({"request_id": r["id"], "artifact_id": q.get("artifact_id"), "repo": q["repo"], "issue": issue["html_url"], "by": q.get("by")}),
                     f"capture.fix_filed:{r['id']}"))
        if log:
            log("capture fix ticket filed", r["id"], issue["html_url"])
        out.append(issue["html_url"])
    return out
