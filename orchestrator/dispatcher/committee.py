"""知見の委員会（ADR 0024）の制御プレーン側: 審査を積む条件、議決の記録、議決に従った PR への作用、担保の Issue。

- 審査（4 役の票と議決）はワーカーの core-gates/committee_review.py。議決とそれで active にした知見は PR のブランチに積まれる
- ここは結果を受けて、events の committee.decided と artifacts の CommitteeDecision に残し、PR にまとめを書く
  - sendback: 理由を書いて PR を閉じる（不採用。票は記録に残る）
  - human: 人の承認に回す（factory:needs-approval）。人のマージが採用、閉じれば不採用
  - adopt / enforce: 採用。enforce の担保の案は、委員会が実装せずに Issue にして工場のラインで作る
- supervisor は、変更種別 knowledge の PR を自動でマージする前に、委員会の採用（approved）を見る
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

ROLE_JA = {"consistency": "整合", "evidence": "根拠", "enforcement": "担保", "dissent": "反対"}
VOTE_JA = {"adopt": "採る", "enforce": "採る（決定論で担保）", "human": "人に回す", "sendback": "差し戻す"}
OUTCOME_JA = {"adopt": "採用", "enforce": "採用（担保の Issue を起票）", "human": "人の判断へ", "sendback": "不採用（差し戻し）"}
APPROVAL_LABEL = "factory:needs-approval"


def decided(cur, pr_url: str) -> dict | None:
    cur.execute("select payload from events where name='committee.decided' and payload->>'pr_url'=%s order by id desc limit 1", (pr_url,))
    r = cur.fetchone()
    return r["payload"] if r else None


def approved(cur, pr_url: str) -> bool:
    d = decided(cur, pr_url)
    return bool(d and d.get("outcome") in ("adopt", "enforce"))


def summary(result: dict) -> str:
    out = result.get("outcome", "")
    lines = [f"## 知見の委員会の議決: {OUTCOME_JA.get(out, out)}", ""]
    if result.get("decision"):
        lines.append(f"議決 `{result['decision']}` をこの PR に積みました（`.factory-config/knowledge/committee/{result['decision']}.yaml`）。")
    if result.get("reason") == "knowledge_check":
        lines.append("決定論の検査（knowledge_check）が通らないため、審査せずに差し戻します。")
    lines.append("")
    for v in result.get("votes") or []:
        reasons = " / ".join(v.get("reasons") or []) or "（理由なし）"
        pii = "（個人情報あり）" if v.get("personal_data") else ""
        lines.append(f"- {ROLE_JA.get(v.get('role'), v.get('role'))}: {VOTE_JA.get(v.get('vote'), v.get('vote'))}{pii} — {reasons}")
    if out == "human":
        lines += ["", "意見が割れたか、人の判断が要る作用先です。マージすれば採用、閉じれば不採用になります。"]
    if out == "sendback":
        lines += ["", "この変更セットは採りません。票と理由は議決の記録（制御プレーン）に残ります。"]
    return "\n".join(lines) + "\n\n---\n_知見の委員会（ADR 0024）_"


def enforce_issue(e: dict, result: dict) -> tuple[str, str]:
    title = f"担保: {e.get('knowledge')} を {e.get('solution_kind')} で決定論にする"
    body = f"""知見の委員会（ADR 0024）が、知見 `{e.get('knowledge')}` を決定論で担保すると決めました（議決 `{result.get('decision')}`、{result.get('pr_url')}）。
委員会は実装しないので、工場のラインで作ってください。

## 担保の案
- 強さ: {e.get('level')}
- 解決策の種類: {e.get('solution_kind')}
- 案: {e.get('proposal')}

## やること
1. 案のとおりに担保を作る（lint・テスト・型・スキーマ・CI・設定・overlay のルールのいずれか）。既存の検査に足せるならそれを使う
2. 同じ PR で、知見 `{e.get('knowledge')}` の `enforcement` に、強さと `solutions`（解決策の種類・この Issue・時期・議決 `{result.get('decision')}`）を足す。知見は消さない
3. `python3 /work/engine/core-gates/knowledge_check.py --repo . --index` で `INDEX.md` を作り直し、検査が通ることを確かめる
"""
    return title, body


def record(cur, repo: str, pr_number: int, corr: str, job_id: int, result: dict, gh, comment) -> bool:
    """議決を残して、PR に作用する。同じ PR の議決は 1 度だけ"""
    payload = {"pr_url": result.get("pr_url"), "outcome": result.get("outcome"), "decision": result.get("decision"),
               "votes": result.get("votes") or [], "enforce": result.get("enforce") or [], "added": result.get("added") or [],
               "pushed_sha": result.get("pushed_sha") or None, "reason": result.get("reason"), "cost_usd": result.get("cost_usd")}
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('committee.decided',%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (corr, job_id, Jsonb(payload), f"committee.decided:{result.get('pr_url')}"))
    if cur.fetchone() is None:
        return False
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('CommitteeDecision',%s,%s,'committee',%s)",
                (corr, job_id, Jsonb(payload)))
    comment(repo, pr_number, summary(result))
    out = result.get("outcome")
    if out == "sendback":
        gh("PATCH", f"repos/{repo}/pulls/{pr_number}", {"state": "closed"})
    elif out == "human":
        gh("POST", f"repos/{repo}/issues/{pr_number}/labels", {"labels": [APPROVAL_LABEL]})
    elif out == "enforce":
        for e in result.get("enforce") or []:
            key = f"committee.enforce_filed:{result.get('decision')}:{e.get('knowledge')}:{e.get('solution_kind')}"
            cur.execute("select 1 from events where dedupe_key=%s", (key,))
            if cur.fetchone():
                continue
            title, body = enforce_issue(e, result)
            issue = gh("POST", f"repos/{repo}/issues", {"title": title, "body": body, "labels": ["factory:planned", "factory:learn"]})
            cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('committee.enforce_filed',%s,%s,%s,%s)",
                        (corr, job_id, Jsonb({"decision": result.get("decision"), "knowledge": e.get("knowledge"),
                                              "solution_kind": e.get("solution_kind"), "issue": issue.get("html_url")}), key))
    return True


# ------------------------------------------------------------------ 週 1 の定例
WEEKLY_LABEL = "factory:knowledge-committee"
WEEKLY_TITLE = "知見の委員会の定例（毎週更新）"


def weekly_issue(gh, repo: str, label: str = WEEKLY_LABEL) -> int:
    got = gh("GET", f"repos/{repo}/issues?state=open&labels={label}&per_page=5") or []
    if got:
        return got[0]["number"]
    body = "知見の委員会（factory-engine の ADR 0024）が週 1 回、知見の置き場を見直したまとめを書きます。閉じないでください。"
    return gh("POST", f"repos/{repo}/issues", {"title": WEEKLY_TITLE, "body": body, "labels": [label]})["number"]


def weekly_input(cur, repo: str, now) -> dict:
    """その週（7 日）の議決と起票（委員会の定例の材料）。制御プレーンの記録だけで組み立てる"""
    from datetime import timedelta
    since = now - timedelta(days=7)
    cur.execute("""select payload from events where name='committee.decided' and occurred_at >= %s
                     and payload->>'pr_url' like %s order by id""", (since, f"https://github.com/{repo}/pull/%"))
    decisions = [{k: r["payload"].get(k) for k in ("pr_url", "outcome", "decision", "added")} | {
        "votes": [{"role": v.get("role"), "vote": v.get("vote")} for v in r["payload"].get("votes") or []]} for r in cur.fetchall()]
    cur.execute("select payload from events where name='knowledge.case_filed' and occurred_at >= %s and payload->>'issue' like %s order by id",
                (since, f"https://github.com/{repo}/issues/%"))
    filed = [{k: r["payload"].get(k) for k in ("incident", "themes", "issue")} for r in cur.fetchall()]
    cur.execute("select payload from events where name='committee.enforce_filed' and occurred_at >= %s order by id", (since,))
    enforce = [r["payload"] for r in cur.fetchall()]
    return {"decisions": decisions, "case_filed": filed, "enforce_filed": enforce}


def _items(xs: list[dict]) -> str:
    return "\n".join(f"- {', '.join(x.get('ids') or [])}: {x.get('why', '')}（案: {x.get('proposal', '')}）" for x in xs) or "- なし"


def weekly_markdown(week: str, result: dict) -> str:
    f, r = result.get("facts") or {}, result.get("report") or {}
    st = r.get("structure") or {}
    stale = "\n".join(f"- {s['id']}（{s.get('confidence')}、期限 {s.get('review_by')}）" for s in f.get("stale") or []) or "- なし"
    return f"""## {week} の定例

{r.get('summary', '')}

- 知見 {f.get('items', 0)} 件（状態 {f.get('by_status', {})}、active の確かさ {f.get('by_confidence', {})}）、事例 {f.get('cases', 0)} 件、議決 {f.get('decisions', 0)} 件

### 期限切れ（要再確認）
{stale}

### 矛盾・重複の候補
{_items(r.get('contradictions') or [])}

### 確かさを上げる候補
{_items(r.get('promote') or [])}

### 廃止の候補
{_items(r.get('retire') or [])}

### 決定論で担保する候補
{_items(r.get('enforce') or [])}

### 置き場の構造
{('見直す: ' + st.get('proposal', '')) if st.get('needed') else '今は変えない'}

---
_知見の委員会（ADR 0024）。委員会は実装しないので、見直しと担保は Issue にして工場のラインで作る_"""


def weekly_issues(week: str, result: dict) -> list[tuple[str, str, str]]:
    """(dedupe の key, 題, 本文)。見直しは 1 本の変更セットにまとめる"""
    f, r = result.get("facts") or {}, result.get("report") or {}
    out = []
    stale = [s["id"] for s in f.get("stale") or []]
    review = {"contradictions": r.get("contradictions") or [], "promote": r.get("promote") or [], "retire": r.get("retire") or []}
    if stale or any(review.values()):
        body = f"""知見の委員会の週 1 の定例（{week}）で決めた見直しを、1 本の変更セットの PR にしてください（ADR 0024）。

## 期限切れ（要再確認）
{chr(10).join('- ' + s for s in stale) or '- なし'}
根拠をたどり直し、まだ正しければ `verified_at` と `review_by` を更新（recheck）、崩れていれば retire にする。

## 矛盾・重複（amend・supersede・retire）
{_items(review['contradictions'])}

## 確かさを上げる（hypothesis → confirmed。evidence に独立した事例を 2 件以上）
{_items(review['promote'])}

## 廃止（retire）
{_items(review['retire'])}

## やること
- `.factory-config/knowledge/README.md` の決まりに従い、ファイルは消さず状態だけ変える。置き換えは supersedes と superseded_by を両方に書く
- `python3 /work/engine/core-gates/knowledge_check.py --repo . --index` で INDEX.md を作り直し、検査が通ることを確かめる
- PR の本文に、知見ごとの扱いと理由を並べる。`.factory-config/knowledge/` の外は変えない
"""
        out.append((f"review:{week}", f"知見の見直し（{week} の定例）", body))
    for e in r.get("enforce") or []:
        ids = ", ".join(e.get("ids") or [])
        body = f"""知見の委員会の週 1 の定例（{week}）で、知見 {ids} を決定論で担保すると決めました（ADR 0024）。委員会は実装しないので、工場のラインで作ってください。

- 解決策の種類: {e.get('solution_kind')}
- 理由: {e.get('why')}
- 案: {e.get('proposal')}

同じ PR で、知見の `enforcement` に強さ（deterministic）と `solutions`（解決策の種類・この Issue・時期）を足す。知見は消さない。
`python3 /work/engine/core-gates/knowledge_check.py --repo . --index` で INDEX.md を作り直し、検査が通ることを確かめる。
"""
        out.append((f"enforce:{week}:{ids}:{e.get('solution_kind')}", f"担保: {ids} を {e.get('solution_kind')} で決定論にする", body))
    st = r.get("structure") or {}
    if st.get("needed"):
        out.append((f"structure:{week}", "知見の置き場の構造を見直す", f"""知見の委員会（置き場の構造のオーナー）が週 1 の定例（{week}）で、置き場のディレクトリの分け方を変えると決めました（ADR 0024）。

案: {st.get('proposal')}

`core-gates/knowledge_check.py` が新しい構造を読めるかを先に確かめ、読めなければ Engine の変更が要ることをこの Issue に書いて止める。
"""))
    return out


def weekly_record(cur, repo: str, issue: int, corr: str, job_id: int, result: dict, week: str, gh, comment, max_issues: int = 4) -> bool:
    payload = {"repo": repo, "week": week, "facts": result.get("facts") or {}, "report": result.get("report") or {},
               "model": result.get("model"), "cost_usd": result.get("cost_usd")}
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('committee.weekly_reviewed',%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (corr, job_id, Jsonb(payload), f"committee.weekly:{repo}:{week}"))
    if cur.fetchone() is None:
        return False
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('CommitteeWeekly',%s,%s,'committee',%s)",
                (corr, job_id, Jsonb(payload)))
    comment(repo, issue, weekly_markdown(week, result))
    for key, title, body in weekly_issues(week, result)[:max_issues]:
        dk = f"committee.weekly_filed:{repo}:{key}"
        cur.execute("select 1 from events where dedupe_key=%s", (dk,))
        if cur.fetchone():
            continue
        got = gh("POST", f"repos/{repo}/issues", {"title": title, "body": body, "labels": ["factory:planned", "factory:learn"]})
        cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('committee.weekly_filed',%s,%s,%s,%s)",
                    (corr, job_id, Jsonb({"repo": repo, "week": week, "key": key, "issue": got.get("html_url")}), dk))
    return True
