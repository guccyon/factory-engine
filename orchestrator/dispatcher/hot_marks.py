"""衝突しやすいファイルの印（ADR 0038 の追記、2026-11-11 オーナー）。決定的（LLM を使わない）。Learn ループの日次の振り返り（learn_step）が回す。

製品ごとに、直近にマージした PR（RECENT_PRS 件）でファイルごとの変更回数を数え、上位（TOP_FILES 件）のうち、
同じ期間に衝突の解消で MIN_CONFLICTS 回以上衝突したファイルに「衝突しやすいファイル」の印を付ける（hotfile.marked）。
印を付けてから UNMARK_AFTER_DAYS 日以上たち、直近 UNMARK_AFTER_DAYS 日に衝突がなければ印を外す（hotfile.unmarked）。
外した後は、新しい衝突が起きるまで付け直さない。

印はプランナーの入力（inflight.md の hot_files）に入り、プランナーが着手するか（start: now / after）を決める。
指示役はプランナーの宣言（start: after）を守らせる。宣言のないチケットだけ、印のファイルに触る流れているチケットの後ろに並べる（serialize）。

PR の変更ファイルは GitHub から引き、pr.files に残して 2 度引かない。値は仮置き（definitions/workflow.yaml の hot_files）。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

import process_learn

RECENT_PRS = 50
TOP_FILES = 20
MIN_CONFLICTS = 3
UNMARK_AFTER_DAYS = 7
EXEMPT = {"CHANGELOG.md"}   # 断片（changelog.d/）で衝突しない作りにした。印を付けない


def recent_merged(gh, repo: str, n: int = RECENT_PRS) -> list[dict]:
    """直近にマージした PR（新しい順）。{number, merged_at}"""
    out: list[dict] = []
    page = 1
    while len(out) < n and page <= 5:
        prs = gh("GET", f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}") or []
        if not prs:
            break
        out += [{"number": p["number"], "merged_at": p["merged_at"]} for p in prs if p.get("merged_at")]
        page += 1
    return sorted(out, key=lambda p: p["merged_at"], reverse=True)[:n]


def pr_files(cur, gh, repo: str, number: int) -> list[str]:
    """PR の変更ファイル。pr.files に残したものを使い、なければ GitHub から引いて残す"""
    cur.execute("select payload->'files' as f from events where dedupe_key=%s", (f"pr.files:{repo}#{number}",))
    r = cur.fetchone()
    if r:
        return list(r["f"] or [])
    files: list[str] = []
    for page in range(1, 6):
        got = gh("GET", f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}") or []
        files += [f["filename"] for f in got]
        if len(got) < 100:
            break
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('pr.files',null,%s,%s) on conflict (dedupe_key) do nothing",
                (Jsonb({"repo": repo, "number": number, "files": files}), f"pr.files:{repo}#{number}"))
    return files


def conflicts_by_file(cur, repo: str, frm: datetime, to: datetime) -> dict[str, dict]:
    """その製品の衝突の解消（ファイルごと）。{パス: {conflicts, last}}"""
    out: dict[str, dict] = {}
    for s in process_learn.conflict_fix(cur, frm, to):
        if s["key"] in (process_learn.UNKNOWN, process_learn.NO_CONFLICT) or not (s["ticket"] or "").startswith(f"github:{repo}#"):
            continue
        g = out.setdefault(s["key"], {"conflicts": 0, "last": s["at"]})
        g["conflicts"] += 1
        g["last"] = max(g["last"], s["at"])
    return out


def candidates(changes: Counter, conflicts: dict[str, dict]) -> dict[str, dict]:
    """変更回数の上位のうち、衝突が MIN_CONFLICTS 回以上のファイル"""
    top = [f for f, _ in sorted(changes.items(), key=lambda kv: (-kv[1], kv[0])) if f not in EXEMPT][:TOP_FILES]
    return {f: {"changes": changes[f], "rank": top.index(f) + 1, "conflicts": conflicts[f]["conflicts"]}
            for f in top if conflicts.get(f, {}).get("conflicts", 0) >= MIN_CONFLICTS}


def latest(cur, repo: str | None = None) -> dict[str, dict]:
    """ファイルごとの最後の印の記録（付けた・外した）。{パス: {name, at, ...}}"""
    cur.execute("""select distinct on (payload->>'repo', payload->>'file') name, occurred_at, payload from events
                    where name in ('hotfile.marked','hotfile.unmarked') and (%s::text is null or payload->>'repo'=%s)
                    order by payload->>'repo', payload->>'file', id desc""", (repo, repo))
    return {r["payload"]["file"]: {**r["payload"], "name": r["name"], "at": r["occurred_at"]} for r in cur.fetchall()}


def active(cur, repo: str | None = None) -> dict[str, dict]:
    """今付いている印。{パス: 印の記録}。repo を省くと全製品（パスが重なれば新しい方）"""
    return {f: m for f, m in latest(cur, repo).items() if m["name"] == "hotfile.marked"}


def step(cur, gh, repos: list[str], now: datetime | None = None, log=lambda *a: None) -> dict:
    """製品ごとに印を付け直す。返り値は {marked: [...], unmarked: [...]}"""
    now = now or datetime.now(timezone.utc)
    res = {"marked": [], "unmarked": []}
    day = f"{now:%Y-%m-%d}"
    for repo in repos:
        prs = recent_merged(gh, repo)
        if not prs:
            continue
        changes: Counter = Counter()
        for p in prs:
            changes.update(set(pr_files(cur, gh, repo, p["number"])))
        frm = datetime.fromisoformat(prs[-1]["merged_at"].replace("Z", "+00:00"))
        conf = conflicts_by_file(cur, repo, frm, now)
        cand = candidates(changes, conf)
        last = latest(cur, repo)
        marks = {f: m for f, m in last.items() if m["name"] == "hotfile.marked"}
        for f, c in cand.items():
            if f in marks:
                continue
            if f in last and conf[f]["last"] <= last[f]["at"]:
                continue   # 外した後に新しい衝突がなければ付け直さない（期間で外した印が翌日に戻らないように）
            payload = {"repo": repo, "file": f, **c, "prs": len(prs), "since": prs[-1]["merged_at"],
                       "why": f"直近 {len(prs)} 件の PR で {c['changes']} 回変わり（{c['rank']} 位）、その間に {c['conflicts']} 回衝突した"}
            cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('hotfile.marked',null,%s,%s) on conflict (dedupe_key) do nothing",
                        (Jsonb(payload), f"hotfile.marked:{repo}:{f}:{day}"))
            res["marked"].append(f"{repo}:{f}")
            log("hot file marked", repo, f, c)
        recent = conflicts_by_file(cur, repo, now - timedelta(days=UNMARK_AFTER_DAYS), now)
        for f, m in marks.items():
            if recent.get(f, {}).get("conflicts", 0) > 0 or now - m["at"] < timedelta(days=UNMARK_AFTER_DAYS):
                continue
            payload = {"repo": repo, "file": f, "why": f"{UNMARK_AFTER_DAYS} 日間衝突しなかった",
                       "marked_at": m["at"].isoformat() if hasattr(m["at"], "isoformat") else m["at"]}
            cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('hotfile.unmarked',null,%s,%s) on conflict (dedupe_key) do nothing",
                        (Jsonb(payload), f"hotfile.unmarked:{repo}:{f}:{day}"))
            res["unmarked"].append(f"{repo}:{f}")
            log("hot file unmarked", repo, f)
    return res
