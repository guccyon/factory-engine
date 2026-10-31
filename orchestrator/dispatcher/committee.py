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
