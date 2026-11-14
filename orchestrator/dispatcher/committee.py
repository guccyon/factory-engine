"""知見の委員会（ADR 0024）の制御プレーン側: 審査を積む条件、議決の記録、議決に従った PR への作用、担保の Issue。

- 審査（4 役の票と議決）はワーカーの core-gates/committee_review.py。議決とそれで active にした知見は PR のブランチに積まれる
- ここは結果を受けて、events の committee.decided と artifacts の CommitteeDecision に残し、PR にまとめを書く
  - sendback: 理由を書いて PR を閉じる（不採用。票は記録に残る）
  - revise: 1〜2 役が直し方を示して差し戻した。差し戻しの理由を指摘として続きのジョブ（review_fix）に渡し、直したらかけ直す。
    同じ PR の 2 度目の revise は human にする（2026-11-04 オーナー: 知見の PR は委員会の可決でマージし、割れたときだけ人へ）
  - human: 人の承認に回す（factory:needs-approval）。人のマージが採用、閉じれば不採用
  - adopt / enforce: 採用。enforce の担保の案は、委員会が実装せずに Issue にして工場のラインで作る
- supervisor は、変更種別 knowledge の PR を自動でマージする前に、委員会の採用（approved）を見る
- 知見は製品ごとに 1 件ずつ直列に流す（queue・turn）。並列に書くとどの PR も同じ番号（K-0001・C-0001）を取って全部が衝突した
  （2026-11-04 オーナー: 振り返りの対象をキューに積み、委員会が 1 つずつ取り出して評価し、番号がぶつからないように蓄積する）
"""
from __future__ import annotations

import continuation
import judgment
import stoppage
from psycopg.types.json import Jsonb

# 知見の Issue を起票してから PR ができるまで、順番を持っていてよい時間。過ぎたら詰まったとみなし、次に譲る
QUEUE_WRITE_GRACE = "2 days"

ROLE_JA = {"consistency": "整合", "evidence": "根拠", "enforcement": "担保", "dissent": "反対"}
VOTE_JA = {"adopt": "採る", "enforce": "採る（決定論で担保）", "human": "人に回す", "sendback": "差し戻す"}
OUTCOME_JA = {"adopt": "採用", "enforce": "採用（担保の Issue を起票）", "human": "人の判断へ", "sendback": "不採用（差し戻し）",
              "revise": "直してかけ直す"}
APPROVAL_LABEL = "factory:needs-approval"


def decided(cur, pr_url: str) -> dict | None:
    """最新の議決。factoryctl committee reopen（committee.reopened）より前の議決は数えない"""
    cur.execute("""select payload from events d where name='committee.decided' and payload->>'pr_url'=%s
                     and not exists (select 1 from events o where o.name='committee.reopened' and o.payload->>'pr_url'=%s and o.id > d.id)
                   order by id desc limit 1""", (pr_url, pr_url))
    r = cur.fetchone()
    return r["payload"] if r else None


def queue(cur, repo: str) -> list[dict]:
    """製品の知見のキュー（起票の古い順）。済んだ（PR がマージされた・閉じた）ものは除く。
    要素は {corr, pr_url（まだ PR がなければ None）, blocked（人を待っている・書き手が詰まった）}"""
    cur.execute("""select f.correlation_id as corr, f.occurred_at > now() - %s::interval as fresh,
                          (select a.payload->>'pr_url' from artifacts a where a.type='PR' and a.correlation_id=f.correlation_id
                            and a.payload->>'pr_url' is not null order by a.id desc limit 1) as pr_url
                     from events f
                    where f.name='knowledge.case_filed' and f.correlation_id like %s
                      and not exists (select 1 from events d where d.name in ('pr.merged','pr.closed_unmerged') and d.correlation_id=f.correlation_id)
                    order by f.id""", (QUEUE_WRITE_GRACE, f"github:{repo}#%"))
    out = []
    for r in cur.fetchall():
        url = r["pr_url"]
        if url:
            d = decided(cur, url)
            # 人を待つもの（supervisor.awaiting_decision と同じ見方）。衝突・CI 落ちの停止（ci:）は工場が直す対象なので待ちに数えない
            prefixes = ("sendback:", "hold:") if judgment.decisions_settled(cur, url) else ("judge:", "sendback:", "hold:")
            blocked = bool(d and d.get("outcome") == "human") or any(stoppage.open_keys(cur, f"{k}{url}") for k in prefixes)
        else:
            blocked = not r["fresh"]
        out.append({"corr": r["corr"], "pr_url": url, "blocked": blocked})
    return out


def turn(cur, repo: str) -> str | None:
    """いま流してよい知見（キューの先頭の、人を待っていないもの）の correlation_id。人を待つものは次に譲る（待ちでキューを止めない）"""
    return next((q["corr"] for q in queue(cur, repo) if not q["blocked"]), None)


def rounds(cur, pr_url: str) -> int:
    """委員会にかけた回数の区切り（直してかけ直した回数 + かけ直しの指示の回数）。積むときの dedupe に使う"""
    cur.execute("select count(*) as n from events where name in ('committee.revise','committee.reopened') and payload->>'pr_url'=%s", (pr_url,))
    return cur.fetchone()["n"]


def revised(cur, pr_url: str) -> bool:
    cur.execute("select 1 from events where name='committee.revise' and payload->>'pr_url'=%s limit 1", (pr_url,))
    return cur.fetchone() is not None


def revised_head_moved(cur, pr_url: str, sha: str) -> bool:
    """最後の区切りが revise なら、その後に head が変わった（直しが push された）か。reopen なら常に True"""
    cur.execute("""select name, payload->>'head_sha' as h from events where name in ('committee.revise','committee.reopened')
                    and payload->>'pr_url'=%s order by id desc limit 1""", (pr_url,))
    r = cur.fetchone()
    return not r or r["name"] == "committee.reopened" or r["h"] != sha


def revise_signal(result: dict) -> dict:
    """差し戻した役の理由を、続きのジョブ（review_fix）の指摘の形にする"""
    defects = [{"rubric_id": f"committee:{v.get('role')}", "severity": "sendback", "location": ".factory-config/knowledge/",
                "evidence": r, "remediation_hint": "知見の委員会の差し戻しの理由に沿って、知見（K-*.yaml）と事例記録を直す。"
                                                  "knowledge_check を通し、INDEX.md を作り直す"}
               for v in result.get("votes") or [] if v.get("vote") == "sendback" for r in v.get("reasons") or []]
    return {"defects": defects, "source": "committee"}


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


ENFORCE_PREFIX = "担保: "
LEARN_LABEL = "factory:learn"


def open_issue_titled(gh, repo: str, title: str) -> int | None:
    """同じ題の開いている Learn の Issue の番号。担保の題は「担保: <知見> を <解決策の種類> で決定論にする」で、議決の担保と週の定例の担保で同じ形"""
    try:
        got = gh("GET", f"repos/{repo}/issues?state=open&labels={LEARN_LABEL}&per_page=100")
    except Exception:  # noqa: BLE001  見られなければ今までどおり起票する（起票し損ねるより、重複の方が後で見つけやすい）
        return None
    for i in got if isinstance(got, list) else []:
        if i.get("title") == title and "pull_request" not in i:
            return i.get("number")
    return None


def join_open(cur, repo: str, title: str, body: str, corr: str, job_id, key: str, gh, comment) -> bool:
    """同じ知見 × 同じ解決策の種類の担保がもう開いていれば、新しく起票せず、その Issue に議決と案をコメントで足す（ADR 0044 の決定 8 の 1）。
    2026-11-08 rally #1052（議決 D-0033）が PR #1116 で進んでいる間に、週の定例が同じ担保を #1122 で起票し、2 本が同時に流れた。
    知見のファイルに記録がなくても、開いている担保の Issue があれば進行中と読む"""
    n = open_issue_titled(gh, repo, title)
    if n is None:
        return False
    comment(repo, n, "知見の委員会が同じ担保をもう一度決めました。新しい Issue は起票せず、ここに足します（同じ目的の仕事を同時に流さない。ADR 0044）。\n\n" + body)
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('committee.enforce_joined',%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing", (corr, job_id, Jsonb({"repo": repo, "title": title, "issue": n}), key))
    return True


def filed_before(cur, repo: str, e: dict) -> bool:
    """同じ知見 × 同じ解決策の種類の担保を、前にも起票したか（開いていれば join_open が拾うので、ここに来るのは閉じた後）。
    2026-11-08〜09 rally: 担保の PR は知見の enforcement を書き足すので委員会にかかり、委員会がまた同じ担保を決めて次の Issue を起票した。
    PR がマージされて元の Issue が閉じると、同じ題の担保が繰り返し起票された（K-0004 の ci_check だけで 8 本）。一度作った担保は二度起票しない。
    作り直しが要るときは、週 1 の定例の見直しか人が起票する"""
    cur.execute("select 1 from events where name='committee.enforce_filed' and payload->>'knowledge'=%s and payload->>'solution_kind'=%s "
                "and payload->>'issue' like %s limit 1", (e.get("knowledge"), e.get("solution_kind"), f"%/{repo}/issues/%"))
    return cur.fetchone() is not None


def enforce_issue(e: dict, result: dict) -> tuple[str, str]:
    title = f"{ENFORCE_PREFIX}{e.get('knowledge')} を {e.get('solution_kind')} で決定論にする"
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
    """議決を残して、PR に作用する。同じ PR の議決は 1 度だけ（revise はかけ直しの区切りごと）"""
    if result.get("outcome") == "revise":
        if revised(cur, result.get("pr_url")):
            result = {**result, "outcome": "human", "reason": "revised_once"}   # 直してもまだ割れた: 人へ
        else:
            return revise(cur, repo, pr_number, corr, job_id, result, gh, comment)
    payload = {"pr_url": result.get("pr_url"), "outcome": result.get("outcome"), "decision": result.get("decision"),
               "votes": result.get("votes") or [], "enforce": result.get("enforce") or [], "added": result.get("added") or [],
               "pushed_sha": result.get("pushed_sha") or None, "reason": result.get("reason"), "cost_usd": result.get("cost_usd")}
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('committee.decided',%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (corr, job_id, Jsonb(payload), f"committee.decided:{result.get('pr_url')}:{rounds(cur, result.get('pr_url'))}"))
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
            if join_open(cur, repo, title, body, corr, job_id, key, gh, comment):
                continue
            if filed_before(cur, repo, e):
                cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('committee.enforce_skipped',%s,%s,%s,%s) "
                            "on conflict (dedupe_key) do nothing", (corr, job_id, Jsonb({"repo": repo, "title": title, "pr_url": result.get("pr_url")}), key))
                continue
            issue = gh("POST", f"repos/{repo}/issues", {"title": title, "body": body, "labels": ["factory:planned", "factory:learn"]})
            cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('committee.enforce_filed',%s,%s,%s,%s)",
                        (corr, job_id, Jsonb({"decision": result.get("decision"), "knowledge": e.get("knowledge"),
                                              "solution_kind": e.get("solution_kind"), "issue": issue.get("html_url")}), key))
    return True


def revise(cur, repo: str, pr_number: int, corr: str, job_id: int, result: dict, gh, comment) -> bool:
    """1〜2 役の差し戻し: 理由を指摘として続きのジョブに渡す。直して head が変われば supervisor がかけ直す"""
    pr_url = result.get("pr_url")
    pr = gh("GET", f"repos/{repo}/pulls/{pr_number}")
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('committee.revise',%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id",
                (corr, job_id, Jsonb({"pr_url": pr_url, "head_sha": pr["head"]["sha"], "votes": result.get("votes") or [],
                                      "cost_usd": result.get("cost_usd")}), f"committee.revise:{pr_url}"))
    if cur.fetchone() is None:
        return False
    comment(repo, pr_number, summary(result).replace("\n\n---", "\n\n差し戻した役の理由に沿って工場が直し、委員会にかけ直します。"
                                                   "かけ直しでまだ割れたら人の判断に回します。\n\n---", 1))
    jid = continuation.enqueue(cur, repo=repo, pr_number=pr_number, pr_url=pr_url, branch=pr["head"]["ref"], head_sha=pr["head"]["sha"],
                               reason="review_fix", by="committee", signal=revise_signal(result))
    return jid is not None


# ------------------------------------------------------------------ 週 1 の定例
WEEKLY_LABEL = "factory:knowledge-committee"
WEEKLY_TITLE = "知見の委員会の定例（毎週更新）"


def weekly_issue(gh, repo: str, label: str = WEEKLY_LABEL) -> int:
    got = gh("GET", f"repos/{repo}/issues?state=open&labels={label}&per_page=5") or []
    if got:
        return got[0]["number"]
    body = "知見の委員会（factory-engine の ADR 0024）が週 1 回、知見の置き場を見直したまとめを書きます。閉じないでください。"
    return gh("POST", f"repos/{repo}/issues", {"title": WEEKLY_TITLE, "body": body, "labels": [label]})["number"]


UNUSED_DAYS = 60   # definitions/knowledge.yaml の invalidation「60 日使われていない」


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
    # 効き目の信号（ADR 0040 の決定 8）: 真因と同じ知見が、その回に届かなかった・届いたのに差し戻された
    cur.execute("""select name, payload from events where name in ('knowledge.missed','knowledge.delivered_failed') and occurred_at >= %s
                     and payload->>'repo'=%s order by id""", (since, repo))
    reach = [{"signal": "missed" if r["name"] == "knowledge.missed" else "delivered_failed",
              **{k: r["payload"].get(k) for k in ("id", "incident", "reason", "scope", "work", "role")}} for r in cur.fetchall()]
    # 形骸化の信号（ADR 0024 の失効のきっかけ「60 日使われていない」）: 直近 60 日に渡した知見の id と最後に渡した日
    cur.execute("""select id, max(occurred_at) as last from (select jsonb_array_elements_text(payload->'ids') as id, occurred_at from events
                     where name='knowledge.used' and occurred_at >= %s and payload->>'repo'=%s) u group by id""", (now - timedelta(days=UNUSED_DAYS), repo))
    used = {r["id"]: r["last"].date().isoformat() for r in cur.fetchall()}
    return {"decisions": decisions, "case_filed": filed, "enforce_filed": enforce, "reach": reach, "used": used}


def _items(xs: list[dict]) -> str:
    return "\n".join(f"- {', '.join(x.get('ids') or [])}: {x.get('why', '')}（案: {x.get('proposal', '')}）" for x in xs) or "- なし"


def weekly_markdown(week: str, result: dict) -> str:
    f, r = result.get("facts") or {}, result.get("report") or {}
    st = r.get("structure") or {}
    stale = "\n".join(f"- {s['id']}（{s.get('confidence')}、期限 {s.get('review_by')}）" for s in f.get("stale") or []) or "- なし"
    return f"""## {week} の定例

{r.get('summary', '')}

- 知見 {f.get('items', 0)} 件（状態 {f.get('by_status', {})}、active の確かさ {f.get('by_confidence', {})}）、事例 {f.get('cases', 0)} 件、議決 {f.get('decisions', 0)} 件
- 効き目（ADR 0040 の決定 8）: 真因と同じ知見が届かなかった {(f.get('reach') or {}).get('missed', 0)} 件（うち効く範囲の当たり外れ {(f.get('reach') or {}).get('missed_scope', 0)} 件）、届いたのに差し戻された {(f.get('reach') or {}).get('delivered_failed', 0)} 件

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

### 効く範囲を広げる候補（届かなかった知見）
{_items(r.get('widen') or [])}

### 置き場の構造
{('見直す: ' + st.get('proposal', '')) if st.get('needed') else '今は変えない'}

---
_知見の委員会（ADR 0024）。委員会は実装しないので、見直しと担保は Issue にして工場のラインで作る_"""


def weekly_issues(week: str, result: dict) -> list[tuple[str, str, str]]:
    """(dedupe の key, 題, 本文)。見直しは 1 本の変更セットにまとめる"""
    f, r = result.get("facts") or {}, result.get("report") or {}
    out = []
    stale = [s["id"] for s in f.get("stale") or []]
    unused = [u["id"] for u in f.get("unused") or []]
    review = {"contradictions": r.get("contradictions") or [], "promote": r.get("promote") or [], "retire": r.get("retire") or [],
              "widen": r.get("widen") or []}
    if stale or unused or any(review.values()):
        body = f"""知見の委員会の週 1 の定例（{week}）で決めた見直しを、1 本の変更セットの PR にしてください（ADR 0024）。

## 期限切れ（要再確認）
{chr(10).join('- ' + s for s in stale) or '- なし'}
根拠をたどり直し、まだ正しければ `verified_at` と `review_by` を更新（recheck）、崩れていれば retire にする。

## 使われない知見（{UNUSED_DAYS} 日。形骸化の候補）
{chr(10).join('- ' + u for u in unused) or '- なし'}
エージェントに渡せる知見なのに {UNUSED_DAYS} 日渡っていない。scope が狭すぎるなら広げ（widen と同じ直し方）、役目を終えていれば retire にする。理由を PR の本文に書く。

## 矛盾・重複（amend・supersede・retire）
{_items(review['contradictions'])}

## 確かさを上げる（hypothesis → confirmed。evidence に独立した事例を 2 件以上）
{_items(review['promote'])}

## 廃止（retire）
{_items(review['retire'])}

## 効く範囲を広げる（scope。ADR 0040 の決定 8）
{_items(review['widen'])}
真因と同じだったのにその回に届かなかった知見。scope の業務を親のノードに上げる・成果物を足す・状況の軸の条件を外すなど、案のとおりに直す。広げすぎない（届かなかった回の業務と状況が入るところまで）

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
        out.append((f"enforce:{week}:{ids}:{e.get('solution_kind')}", f"{ENFORCE_PREFIX}{ids} を {e.get('solution_kind')} で決定論にする", body))
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
        if title.startswith(ENFORCE_PREFIX) and join_open(cur, repo, title, body, corr, job_id, dk, gh, comment):
            continue
        got = gh("POST", f"repos/{repo}/issues", {"title": title, "body": body, "labels": ["factory:planned", "factory:learn"]})
        cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('committee.weekly_filed',%s,%s,%s,%s)",
                    (corr, job_id, Jsonb({"repo": repo, "week": week, "key": key, "issue": got.get("html_url")}), dk))
    return True
