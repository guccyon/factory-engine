"""操業コンソールの委員会のページ（/committees）の中身。読むだけ（記録は書かない）。

工場の委員会は 2 つ。開いた記録は events に残っているので、それを開催の一覧と議事録に組み直す。
- 知見の委員会（ADR 0024）: 知見の PR ごとの審査（committee.decided・差し戻して直させた committee.revise）と、週 1 の定例（committee.weekly_reviewed）
- 事故の調査委員会（ADR 0046）: 週次の見回り・調査・記録（fincident.reviewed）

議事録は {h, text?, items?} の節の並びで返す。画面は節を並べるだけで、委員会ごとの組み立てはここに閉じる。
"""
from __future__ import annotations

import re

import committee

COMMITTEES = {"knowledge": "知見の委員会", "incident": "事故の調査委員会"}
# event の name → (委員会, 会の種類)
KINDS = {
    "committee.decided": ("knowledge", "審査"),
    "committee.revise": ("knowledge", "審査（差し戻し）"),
    "committee.weekly_reviewed": ("knowledge", "定例"),
    "fincident.reviewed": ("incident", "週次の見回り"),
}
NAMES = list(KINDS)
LIMIT = 50

REPORT_JA = [("contradictions", "矛盾・重複の候補"), ("promote", "確かさを上げる候補"), ("retire", "廃止の候補"),
             ("enforce", "決定論で担保する候補"), ("widen", "効く範囲を広げる候補（届かなかった知見）")]


def _pr(url: str | None) -> str:
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", url or "")
    return f"{m.group(1)}#{m.group(2)}" if m else (url or "")


def _repo(name: str, p: dict) -> str:
    if name == "committee.weekly_reviewed":
        return p.get("repo") or ""
    if name.startswith("committee."):
        return _pr(p.get("pr_url")).split("#")[0]
    return ""


def _title(name: str, p: dict) -> str:
    if name in ("committee.decided", "committee.revise"):
        return f"知見の審査 {_pr(p.get('pr_url'))}"
    if name == "committee.weekly_reviewed":
        return f"{p.get('week', '')} の定例"
    return f"{p.get('week', '')} の見回り"


def _outcome(name: str, p: dict) -> str:
    if name == "committee.decided":
        return committee.OUTCOME_JA.get(p.get("outcome"), p.get("outcome") or "")
    if name == "committee.revise":
        return committee.OUTCOME_JA["revise"]
    if name == "committee.weekly_reviewed":
        rep = p.get("report") or {}
        return f"見直しの候補 {sum(len(rep.get(k) or []) for k, _ in REPORT_JA)} 件"
    return f"所見 {p.get('findings', 0)} 件（事故 {p.get('incidents', 0)} 件）"


def _row(r: dict) -> dict:
    name, p = r["name"], r["payload"] or {}
    com, kind = KINDS[name]
    return {"id": r["id"], "committee": com, "committee_ja": COMMITTEES[com], "kind": kind, "title": _title(name, p),
            "repo": _repo(name, p), "outcome": _outcome(name, p), "at": r["occurred_at"], "cost_usd": p.get("cost_usd"),
            "level": "bad" if p.get("outcome") == "human" or p.get("incidents") else ""}


def list_meetings(cur, committee_id: str | None = None, limit: int = LIMIT) -> dict:
    names = [n for n in NAMES if not committee_id or KINDS[n][0] == committee_id]
    cur.execute("select id, name, occurred_at, payload from events where name = any(%s) order by id desc limit %s", (names, limit))
    rows = [_row(r) for r in cur.fetchall()]
    return {"committees": COMMITTEES, "meetings": rows}


def _counts(d) -> str:
    return "・".join(f"{k} {v}" for k, v in (d or {}).items()) or "なし"


def _items(xs) -> list[str]:
    out = []
    for x in xs or []:
        if isinstance(x, dict):
            ids = ", ".join(x.get("ids") or []) or x.get("id") or x.get("name") or ""
            body = x.get("why") or x.get("question") or x.get("title") or ""
            prop = f"（案: {x['proposal']}）" if x.get("proposal") else ""
            out.append(f"{ids}: {body}{prop}" if ids and body else (ids or body) + prop)
        else:
            out.append(str(x))
    return out


def _review(cur, r: dict) -> list[dict]:
    p = r["payload"] or {}
    secs = [{"h": "議決", "items": [x for x in [f"議決の記録: {p['decision']}" if p.get("decision") else "",
                                   "決定論の検査（knowledge_check）が通らないため審査せずに差し戻した" if p.get("reason") == "knowledge_check" else "",
                                   "直してかけ直してもまだ割れたので人に回した" if p.get("reason") == "revised_once" else ""] if x]}]
    secs.append({"h": "票", "items": [f"{committee.ROLE_JA.get(v.get('role'), v.get('role'))}: {committee.VOTE_JA.get(v.get('vote'), v.get('vote'))}"
                                      f"{'（個人情報あり）' if v.get('personal_data') else ''} — {' / '.join(v.get('reasons') or []) or '（理由なし）'}"
                                      for v in p.get("votes") or []]})
    if p.get("added"):
        secs.append({"h": "採った知見", "items": _items(p["added"])})
    if p.get("decision"):
        cur.execute("select payload from events where name='committee.enforce_filed' and payload->>'decision'=%s order by id", (p["decision"],))
        filed = [x["payload"] for x in cur.fetchall()]
        if filed:
            secs.append({"h": "担保の Issue", "items": [f"{f.get('knowledge')}（{f.get('solution_kind')}）: {f.get('issue')}" for f in filed]})
    if r["name"] == "committee.decided" and p.get("outcome") == "human":
        secs.append({"h": "次に", "text": "人の判断に回した。PR をマージすれば採用、閉じれば不採用"})
    if r["name"] == "committee.revise":
        secs.append({"h": "次に", "text": "差し戻した役の理由に沿って工場が直し、委員会にかけ直す。かけ直しでまだ割れたら人の判断に回す"})
    return secs


def _weekly(cur, r: dict) -> list[dict]:
    p = r["payload"] or {}
    f, rep = p.get("facts") or {}, p.get("report") or {}
    reach = f.get("reach") or {}
    secs = [{"h": "まとめ", "text": rep.get("summary") or ""},
            {"h": "数", "items": [f"知見 {f.get('items', 0)} 件（状態 {_counts(f.get('by_status'))}、active の確かさ {_counts(f.get('by_confidence'))}）",
                                  f"事例 {f.get('cases', 0)} 件、議決 {f.get('decisions', 0)} 件",
                                  f"届かなかった知見 {reach.get('missed', 0)} 件、届いたのに差し戻された {reach.get('delivered_failed', 0)} 件"]},
            {"h": "期限切れ（要再確認）", "items": [f"{s.get('id')}（{s.get('confidence')}、期限 {s.get('review_by')}）" for s in f.get("stale") or []]}]
    secs += [{"h": h, "items": _items(rep.get(k))} for k, h in REPORT_JA]
    st = rep.get("structure") or {}
    secs.append({"h": "置き場の構造", "text": ("見直す: " + st.get("proposal", "")) if st.get("needed") else "今は変えない"})
    cur.execute("select payload from events where name='committee.weekly_filed' and payload->>'repo'=%s and payload->>'week'=%s order by id",
                (p.get("repo"), p.get("week")))
    filed = [x["payload"] for x in cur.fetchall()]
    secs.append({"h": "起票した Issue", "items": [x.get("issue") or x.get("key") for x in filed]})
    return secs


def _incident(cur, r: dict) -> list[dict]:
    p = r["payload"] or {}
    secs = [{"h": "まとめ", "text": p.get("summary") or ""},
            {"h": "見た観点", "items": _items(p.get("viewpoints"))},
            {"h": "所見", "items": [f"所見 {p.get('findings', 0)} 件、うち事故 {p.get('incidents', 0)} 件",
                                    *(f"台帳に開いた: {c}" for c in p.get("opened") or []), *(f"台帳に足した: {c}" for c in p.get("updated") or [])]},
            {"h": "事故記録（送り出し口へ）", "items": list(p.get("outboxed") or [])}]
    if p.get("issues_proposed"):
        cur.execute("select payload from events where name='fincident.issue_proposed' and payload->>'week'=%s order by id", (p.get("week"),))
        secs.append({"h": "Issue の案", "items": [x["payload"].get("title") or x["payload"].get("key") for x in cur.fetchall()]})
    if p.get("rejected"):
        secs.append({"h": "通らなかった下書き", "items": [f"{x.get('id') or ''} {x.get('title') or ''}: {' / '.join(map(str, (x.get('check') or []) + (x.get('review') or [])))}"
                                                     for x in p["rejected"]]})
    secs.append({"h": "新しい観点の候補", "items": [f"{v.get('resource')} × {v.get('mechanism')}: {v.get('question')}（{v.get('why')}）"
                                                for v in p.get("new_viewpoints") or [] if isinstance(v, dict)]})
    secs.append({"h": "専用のチェックの見直し", "items": [f"{c.get('name')}: {'残す' if c.get('keep') else '外す'} — {c.get('why')}" for c in p.get("check_reviews") or []]})
    return secs


def detail(cur, mid: int) -> dict | None:
    cur.execute("select id, name, occurred_at, correlation_id, job_id, payload from events where id=%s and name = any(%s)", (mid, NAMES))
    r = cur.fetchone()
    if not r:
        return None
    p = r["payload"] or {}
    if r["name"] in ("committee.decided", "committee.revise"):
        secs, link = _review(cur, r), p.get("pr_url")
    elif r["name"] == "committee.weekly_reviewed":
        secs, link = _weekly(cur, r), None
    else:
        secs, link = _incident(cur, r), None
    # 中身のない節は出さない（議事録を短く）
    secs = [s for s in secs if s.get("text") or s.get("items")]
    return {**_row(r), "job_id": r["job_id"], "link": link, "model": p.get("model"), "sections": secs}


def get(cur, path: str) -> tuple[int, dict] | None:
    """/api/committees（?committee=knowledge|incident）と /api/committees/<id>"""
    from urllib.parse import parse_qs, urlsplit
    u = urlsplit(path)
    if u.path == "/api/committees":
        c = (parse_qs(u.query).get("committee") or [None])[0]
        return 200, list_meetings(cur, c if c in COMMITTEES else None)
    m = re.fullmatch(r"/api/committees/(\d+)", u.path)
    if m:
        d = detail(cur, int(m.group(1)))
        return (200, d) if d else (404, {"error": "not found"})
    return None
