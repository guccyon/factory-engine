"""週 1 回の設計の診断（ADR 0022）の記録と公開。

supervisor が製品ごとの固定 Issue（ラベル factory:design-review）に design_review のジョブを積み、
ワーカー（design_review.sh）が所見を返す。dispatcher はこのモジュールで、所見を events（design.reviewed）と
artifacts（DesignReview）に残し、固定 Issue の本文を書き換えて週ごとのコメントを足す。
本文の末尾には、ページ（health/design/render_html.py）が読むスナップショット（所見と最新の数値の診断）を埋める。
LLM は使わない。所見はワーカーのプロンプトに入れない（人が読むためのもの）。
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

LABEL = "factory:design-review"
TITLE = "設計の診断（毎週更新）"
MARK = "factory-design:v1"
JST = timezone(timedelta(hours=9))
SEVERITY_JA = {"high": "重", "mid": "中", "low": "小"}
QUALITY_KEYS = ("score", "metrics", "counts", "distributions", "hotspots", "sha", "day")


def week_of(now: datetime | None = None) -> str:
    """JST の ISO 週（例: 2026-W44）"""
    y, w, _ = (now or datetime.now(JST)).astimezone(JST).isocalendar()
    return f"{y}-W{w:02d}"


def find_issue(gh, repo: str) -> int | None:
    got = gh("GET", f"repos/{repo}/issues?state=open&labels={LABEL}&per_page=5") or []
    return got[0]["number"] if got else None


def ensure_issue(gh, repo: str) -> int:
    n = find_issue(gh, repo)
    if n:
        return n
    body = "工場が週 1 回、このリポジトリのドメインモデリングと設計を読んで所見を書きます（factory-engine の ADR 0022）。閉じないでください。"
    return gh("POST", f"repos/{repo}/issues", {"title": TITLE, "body": body, "labels": [LABEL]})["number"]


def previous_findings(cur, repo: str) -> list[dict]:
    """前回の所見の題と重さ（次の診断に渡し、直ったか・残っているかを見させる）"""
    cur.execute("select payload from events where name='design.reviewed' and payload->>'repo'=%s order by id desc limit 1", (repo,))
    r = cur.fetchone()
    return [{"title": f.get("title"), "severity": f.get("severity")} for f in ((r or {}).get("payload") or {}).get("findings") or []]


def latest_quality(cur, repo: str) -> dict | None:
    cur.execute("select payload from events where name='quality.snapshot' and payload->>'repo'=%s order by id desc limit 1", (repo,))
    r = cur.fetchone()
    if not r:
        return None
    p = r["payload"]
    return {k: p.get(k) for k in QUALITY_KEYS}


def quality_trend(cur, repo: str, weeks: int = 12) -> list[dict]:
    cur.execute("select payload->>'day' as day, (payload->>'score')::float as score from events where name='quality.snapshot' and payload->>'repo'=%s "
                "and payload->>'score' is not null order by payload->>'day' desc limit %s", (repo, weeks * 7))
    return [{"day": r["day"], "score": r["score"]} for r in reversed(cur.fetchall())]


def markdown(repo: str, week: str, rep: dict, sha: str, job_id: int, quality: dict | None) -> str:
    lines = [f"# {TITLE}", "", f"{week}・`{repo}` の `{(sha or '')[:7]}` を読んだ所見（job {job_id}）。工場が週 1 回書き換えます。", "",
             "## 要約", "", rep.get("summary") or "", ""]
    if quality and quality.get("score") is not None:
        c = quality.get("counts") or {}
        lines += ["## 数値の診断（直近）", "",
                  f"スコア {quality['score']}（{quality.get('day')}）。複雑すぎる関数 {c.get('complex_functions', '—')} 本 / 関数 {c.get('functions', '—')} 本。", ""]
    lines += ["## よくできているところ", ""] + [f"- {s}" for s in rep.get("strengths") or []] + [""]
    lines += ["## 所見", ""]
    for i, f in enumerate(rep.get("findings") or [], 1):
        lines += [f"### {i}. [{SEVERITY_JA.get(f.get('severity'), f.get('severity'))}] {f.get('title')}", "", f.get("body") or "", ""]
        if f.get("evidence"):
            lines += ["根拠: " + "、".join(f"`{e}`" for e in f["evidence"][:6]), ""]
        if f.get("fix"):
            lines += [f"直し方: {f['fix']}", ""]
    return "\n".join(lines)


def snapshot(repo: str, week: str, rep: dict, sha: str, job_id: int, quality: dict | None, trend: list[dict]) -> str:
    data = {"repo": repo, "week": week, "sha": sha, "job_id": job_id, "generated_at": datetime.now(timezone.utc).isoformat(),
            "review": rep, "quality": quality, "quality_trend": trend}
    return f"<!-- {MARK} {base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode()} -->"


def record(cur, repo: str, issue: int, corr: str, job_id: int, result: dict, gh, comment) -> bool:
    """所見を残して固定 Issue に書く。同じ週の 2 回目は記録だけ更新しない（dedupe）"""
    rep = result.get("report") or {}
    week, sha = week_of(), result.get("sha") or ""
    payload = {"repo": repo, "week": week, "sha": sha, "summary": rep.get("summary"), "strengths": rep.get("strengths") or [],
               "findings": rep.get("findings") or [], "checked": rep.get("checked_viewpoints") or [],
               "model": result.get("model"), "cost_usd": result.get("cost_usd")}
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('design.reviewed',%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (corr, job_id, Jsonb(payload), f"design.reviewed:{repo}:{week}"))
    if cur.fetchone() is None:
        return False
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('DesignReview',%s,%s,'govern',%s)",
                (corr, job_id, Jsonb(payload)))
    quality = latest_quality(cur, repo)
    body = markdown(repo, week, rep, sha, job_id, quality) + "\n\n" + snapshot(repo, week, rep, sha, job_id, quality, quality_trend(cur, repo))
    gh("PATCH", f"repos/{repo}/issues/{issue}", {"body": body})
    counts = {k: sum(1 for f in payload["findings"] if f.get("severity") == k) for k in SEVERITY_JA}
    comment(repo, issue, f"{week} の設計の診断（job {job_id}）: 所見 {len(payload['findings'])} 件"
                         f"（重 {counts['high']}・中 {counts['mid']}・小 {counts['low']}）。\n\n{rep.get('summary') or ''}")
    return True
