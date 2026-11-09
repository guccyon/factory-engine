"""品質の Learn（ADR 0043 の O9 quality_learn）。Defect（defect.recorded。P5a の defect_metrics）と Incident（O5）を入力に、週に 1 回、

1. 発見工程の分布と出やすい場所（観点・ストーリー）を集計して quality.learned に残す
2. QA の重点（厚く流すストーリーと観点）を qa.focus_updated に、回帰のシナリオ（前に見つかった Defect の再現の手順）を qa.scenario_added に足す。
   QA のジョブを積むとき qa_line.learned_focus が入力に足す（QA 役の入力の更新。工程は変えない。人の承認は要らない）
3. 基準の変更案（quality_policy の値）を quality.proposal_drafted にし、知見の変更セットの Issue（factory:learn）にして委員会（ADR 0024）の列に入れる。
   委員会は案を知見として採るかを決めるだけで、品質方針の値を変えるのは人（ADR 0043 の O9「案として委員会に出すだけ」）

LLM は使わない。数値は definitions/defects.yaml の learn（dispatcher のイメージには definitions/ を入れないので DEFS に写す。テストが食い違いを見る）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

JST = timezone(timedelta(hours=9))
AFTER_MERGE = ("qa", "diagnose", "prod")   # definitions/defects.yaml の stages の before_merge: false
INCIDENT_EVENT = "incident.recorded"       # O5（severity_budget）の Incident。まだなければ 0 件として扱う
STORY_KEY = re.compile(r"^[\w.-]+/[\w.-]+:([A-Za-z0-9_-]+)#")
DEFS = {"weekday": 0, "hour": 7, "window_days": 30, "focus_stories_max": 3, "focus_viewpoints_max": 2,
        "scenario_severities": ["s1", "s2", "s3"], "scenarios_per_week": 1, "proposal_min_after_merge": 2, "proposals_per_week": 1}
LABELS = ["factory:planned", "factory:learn"]
KNOWLEDGE_ROOT = ".factory-config/knowledge"


def week_of(now: datetime) -> str:
    y, w, _ = now.astimezone(JST).isocalendar()
    return f"{y}-W{w:02d}"


def story_of(key: str | None) -> str | None:
    m = STORY_KEY.match(key or "")
    return m.group(1) if m and m.group(1) != "explore" else None


def facts(cur, repo: str, now: datetime, window_days: int) -> dict:
    """直近 window_days 日の Defect と Incident の集計（制御プレーンの記録だけ）"""
    since = now - timedelta(days=window_days)
    cur.execute("select id, payload from events where name='defect.recorded' and occurred_at >= %s and payload->>'repo'=%s order by id",
                (since, repo))
    rows = [{"event_id": r["id"], **r["payload"]} for r in cur.fetchall()]
    cur.execute("select count(*) as n from events where name=%s and occurred_at >= %s and payload->>'repo'=%s", (INCIDENT_EVENT, since, repo))
    incidents = cur.fetchone()["n"]
    by_found: dict[str, int] = {}
    by_vp: dict[str, int] = {}
    by_vp_after: dict[str, int] = {}
    by_story: dict[str, int] = {}
    for r in rows:
        f, vp = r.get("found_in"), r.get("viewpoint") or "unknown"
        by_found[f] = by_found.get(f, 0) + 1
        by_vp[vp] = by_vp.get(vp, 0) + 1
        if f in AFTER_MERGE:
            by_vp_after[vp] = by_vp_after.get(vp, 0) + 1
        s = story_of(r.get("key"))
        if s:
            by_story[s] = by_story.get(s, 0) + 1
    top = lambda d: sorted(d.items(), key=lambda x: (-x[1], x[0]))  # noqa: E731
    before = sum(n for f, n in by_found.items() if f not in AFTER_MERGE)
    return {"window_days": window_days, "defects": len(rows), "incidents": incidents, "by_found_in": dict(top(by_found)),
            "hotspots": [{"viewpoint": k, "n": n} for k, n in top(by_vp)[:5]],
            "after_merge_viewpoints": [{"viewpoint": k, "n": n} for k, n in top(by_vp_after)],
            "stories": [{"story_id": k, "n": n} for k, n in top(by_story)],
            "escape_prevention": round(before / len(rows), 3) if rows else None, "_rows": rows}


def focus(fx: dict, ld: dict) -> dict:
    vps = fx["after_merge_viewpoints"] or fx["hotspots"]
    return {"stories": [s["story_id"] for s in fx["stories"][: int(ld["focus_stories_max"])]],
            "viewpoints": [v["viewpoint"] for v in vps if v["viewpoint"] != "unknown"][: int(ld["focus_viewpoints_max"])]}


def scenario_candidates(fx: dict, ld: dict) -> list[dict]:
    """回帰のシナリオの種。QA（夜間の探索を含む）で見つかり、重さが線の内で、再現の手順があるもの。同じ題が多い順・重い順"""
    sev = list(ld["scenario_severities"])
    groups: dict[str, dict] = {}
    for r in fx["_rows"]:
        if r.get("found_in") != "qa" or r.get("severity") not in sev or not r.get("reproduce") or not r.get("title"):
            continue
        g = groups.setdefault(str(r["title"])[:90], {"title": str(r["title"])[:200], "n": 0, "row": r})
        g["n"] += 1
    return [{"title": g["title"], "reproduce": g["row"]["reproduce"], "viewpoint": g["row"].get("viewpoint"), "severity": g["row"].get("severity"),
             "story_id": story_of(g["row"].get("key")), "from_defect": g["row"].get("key"), "defect_event": g["row"]["event_id"],
             "issue": g["row"].get("issue"), "n": g["n"]}
            for g in sorted(groups.values(), key=lambda g: (-g["n"], sev.index(g["row"]["severity"]), g["title"]))]


def proposal(fx: dict, ld: dict) -> dict | None:
    """基準の変更案の種。マージの後（QA・診断・本番）に同じ観点の Defect が線より多く出ていれば、その観点をマージの手前で止める案"""
    after = [v for v in fx["after_merge_viewpoints"] if v["viewpoint"] != "unknown"]
    if not after or after[0]["n"] < int(ld["proposal_min_after_merge"]):
        return None
    vp, n = after[0]["viewpoint"], after[0]["n"]
    found = {k: v for k, v in fx["by_found_in"].items()}
    return {"viewpoint": vp, "after_merge": n, "by_found_in": found, "escape_prevention": fx["escape_prevention"], "incidents": fx["incidents"],
            "statement": (f"観点 {vp} の Defect がマージの後（QA・診断・本番）で直近 {fx['window_days']} 日に {n} 件見つかっており、"
                          "マージの手前（PR の検査・CI）で止まっていない"),
            "change": (f"品質方針（.factory-config/quality_policy.yaml）に、観点 {vp} をマージの手前で確かめる約束を足す"
                       "（例: PR の検査のルーブリックに入れる、S の条件・エラーバジェットの欄で扱いを決める）"),
            "expected": f"観点 {vp} の Defect のうちマージの手前で見つかる割合が上がり、流出防止率が上がる"}


def _record(cur, name: str, payload: dict, key: str) -> bool:
    cur.execute("insert into events(name, payload, dedupe_key) values (%s,%s,%s) on conflict (dedupe_key) do nothing returning id",
                (name, Jsonb(payload), key))
    return cur.fetchone() is not None


def step(cur, repo: str, now: datetime, ld: dict | None = None, *, force: bool = False, log=None) -> dict | None:
    """週に 1 回（ld の weekday・hour を過ぎたら）回す。返り値はその週に新しく残したもの（回さなければ None）"""
    ld = {**DEFS, **(ld or {})}
    j = now.astimezone(JST)
    if not force and (j.weekday() != int(ld["weekday"]) or j.hour < int(ld["hour"])):
        return None
    week = week_of(now)
    fx = facts(cur, repo, now, int(ld["window_days"]))
    public = {k: v for k, v in fx.items() if not k.startswith("_")}
    fc = focus(fx, ld)
    prop = proposal(fx, ld)
    if not _record(cur, "quality.learned", {"repo": repo, "week": week, **public, "focus": fc, "proposal": bool(prop)},
                   f"quality.learned:{repo}:{week}"):
        return None
    out = {"week": week, "facts": public, "focus": None, "scenarios": [], "proposal": None}
    if fc["stories"] or fc["viewpoints"]:
        _record(cur, "qa.focus_updated", {"repo": repo, "week": week, **fc, "basis": "defect.recorded"}, f"qa.focus_updated:{repo}:{week}")
        out["focus"] = fc
    n = 0
    for c in scenario_candidates(fx, ld):
        if n >= int(ld["scenarios_per_week"]):
            break
        sid = f"R-{week}-{n + 1}"
        if _record(cur, "qa.scenario_added", {"repo": repo, "week": week, "id": sid, **c, "expected": "この再現の手順で、前に見つかった食い違いが起きない"},
                   f"qa.scenario_added:{repo}:{c['title'][:120]}"):
            out["scenarios"].append(sid)
            n += 1
    if prop and _record(cur, "quality.proposal_drafted", {"repo": repo, "week": week, **prop}, f"quality.proposal_drafted:{repo}:{week}"):
        out["proposal"] = prop["viewpoint"]
    if log:
        log("quality learned", repo, week, "focus", fc, "scenarios", out["scenarios"], "proposal", out["proposal"])
    return out


def proposal_issue(repo: str, p: dict, event_id: int) -> tuple[str, str]:
    title = f"知見: 品質の基準の変更案を足す（{p['week']}・観点 {p['viewpoint']}）"
    found = "、".join(f"{k} {v}" for k, v in (p.get("by_found_in") or {}).items()) or "なし"
    body = f"""品質の Learn（factory-engine の ADR 0043 の O9）が、Defect の記録から品質の基準の変更案を出しました。
知見の置き場（`{KNOWLEDGE_ROOT}/`、ADR 0024）に案として足し、知見の委員会の審査にかけてください。**品質方針そのもの（`.factory-config/quality_policy.yaml`）はこの PR で変えません**（値を変えるのは人の承認）。

## 根拠（制御プレーンの記録）
- 集計: events `quality.proposal_drafted`（id {event_id}）、週 {p['week']}
- 発見工程の分布: {found}
- 流出防止率: {p.get('escape_prevention')}　Incident: {p.get('incidents', 0)} 件

## 案
- 事実: {p['statement']}
- 変更の案: {p['change']}
- 見込み: {p['expected']}

## やること（1 本の PR）
1. `{KNOWLEDGE_ROOT}/README.md` を読み、置き場の決まりに従う
2. 知見 `{KNOWLEDGE_ROOT}/K-NNNN.yaml` を 1 つ足す（番号は空いている次の番号）。`theme: 5`、`kind: hypothesis_test`、`acts_on: process`（文脈には渡さず、人に回す提案）、
   `status: draft`、`confidence: hypothesis`、`verified_at` は今日、`review_by` は 30 日以内、`evidence` に `event: "quality.proposal_drafted:{event_id}"`、
   `scope` は `work: [biz.quality_policy, ops.qa]`。statement に上の事実と変更の案と見込みを書く
3. 既存の知見（`K-*.yaml`）を全部読み、重なる・矛盾するものの扱い（keep・amend・supersede・retire・recheck）を決める。ファイルは消さない
4. `python3 /work/engine/core-gates/knowledge_check.py --repo . --index` で `INDEX.md` を作り直し、`python3 /work/engine/core-gates/knowledge_check.py --repo .` が通ることを確かめる
5. PR の本文に、足した知見と、既存の知見ごとの扱いと理由を並べる

## 守ること
- 個人情報は書かない。`{KNOWLEDGE_ROOT}/` の外のファイルは変えない（品質方針の値も変えない）
"""
    return title, body


def file_proposals(cur, gh, repo: str, now: datetime, log=None) -> list[str]:
    """まだ Issue にしていない基準の変更案を、知見の変更セットの Issue にして委員会の列（knowledge.case_filed）に入れる。呼ぶ側が委員会の順番を見る"""
    cur.execute("""select d.id, d.payload from events d where d.name='quality.proposal_drafted' and d.payload->>'repo'=%s
                     and not exists (select 1 from events f where f.name='quality.proposal_filed' and f.payload->>'drafted_event' = d.id::text)
                   order by d.id limit 1""", (repo,))
    r = cur.fetchone()
    if not r:
        return []
    p = r["payload"]
    title, body = proposal_issue(repo, p, r["id"])
    issue = gh("POST", f"repos/{repo}/issues", {"title": title, "body": body, "labels": list(LABELS)})
    corr = f"github:{repo}#{issue['number']}"
    day = f"{now.astimezone(JST):%Y-%m-%d}"
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('knowledge.case_filed',%s,%s,%s)",
                (corr, Jsonb({"incident": f"quality:{repo}:{p['week']}", "analysis_event": r["id"], "themes": [5], "learnings": 1,
                              "issue": issue["html_url"], "day": day, "source": "quality_learn"}), f"knowledge.case_filed:quality:{repo}:{p['week']}"))
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('quality.proposal_filed',%s,%s,%s)",
                (corr, Jsonb({"repo": repo, "week": p["week"], "viewpoint": p["viewpoint"], "drafted_event": str(r["id"]), "issue": issue["html_url"]}),
                 f"quality.proposal_filed:{r['id']}"))
    if log:
        log("quality proposal filed", repo, p["week"], issue["html_url"])
    return [issue["html_url"]]
