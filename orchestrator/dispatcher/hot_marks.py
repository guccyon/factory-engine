"""衝突しやすいファイルの印（ADR 0038 の追記、2026-11-11 オーナー）。決定的（LLM を使わない）。Learn ループの日次の振り返り（learn_step）が回す。

製品ごとに、直近にマージした PR（RECENT_PRS 件）でファイルごとの変更回数を数え、上位（TOP_FILES 件）のうち、
同じ期間に衝突の解消で MIN_CONFLICTS 回以上衝突したファイルに「衝突しやすいファイル」の印を付ける（hotfile.marked）。
印を付けてから UNMARK_AFTER_DAYS 日以上たち、直近 UNMARK_AFTER_DAYS 日に衝突がなければ印を外す（hotfile.unmarked）。
外した後は、新しい衝突が起きるまで付け直さない。

印はプランナーの入力（inflight.md の hot_files）に入り、プランナーが着手するか（start: now / after）を決める。
指示役はプランナーの宣言（start: after）を守らせる。宣言のないチケットだけ、印のファイルに触る流れているチケットの後ろに並べる（serialize）。

PR の変更ファイルは GitHub から引き、pr.files に残して 2 度引かない。値は仮置き（definitions/workflow.yaml の hot_files）。

ADR 0051 の 1（2026-11-12 オーナー）: 同じ集計でファイルの形（追記型・生成物・登録の箇所・中身）を見分け、印に持たせる。
衝突が閾値に届かなくても、変更回数の上位で同時に開いていた PR が MIN_CONCURRENT 本以上ある追記型・生成物は
「取りまとめの候補」（hotfile.aggregate_candidate）にする。断片にためて取りまとめる（ADR 0051 の 2）対象を、衝突の前に見つける。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

import base64
import os

import process_learn

RECENT_PRS = 50
TOP_FILES = 20
MIN_CONFLICTS = 3
UNMARK_AFTER_DAYS = 7
EXEMPT = {"CHANGELOG.md"}   # 断片（changelog.d/）で衝突しない作りにした。印を付けない
MIN_CONCURRENT = 3          # 取りまとめの候補: 同時に開いていた PR の数（ADR 0051 の 1）
APPEND_RATIO = 0.8          # 差分の行のうち追加がこの割合以上なら追記型
CODE_EXT = {".ts", ".tsx", ".js", ".mjs", ".cjs", ".rs", ".py", ".dart", ".go", ".kt", ".swift", ".java"}
GENERATED_MARKS = ("generated", "do not edit", "生成物", "自動生成")
SHAPE_JA = {"append": "追記型", "generated": "生成物", "registry": "登録の箇所", "content": "中身", "unknown": "不明"}


def recent_merged(gh, repo: str, n: int = RECENT_PRS) -> list[dict]:
    """直近にマージした PR（新しい順）。{number, merged_at, created_at}"""
    out: list[dict] = []
    page = 1
    while len(out) < n and page <= 5:
        prs = gh("GET", f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}") or []
        if not prs:
            break
        out += [{"number": p["number"], "merged_at": p["merged_at"], "created_at": p.get("created_at")} for p in prs if p.get("merged_at")]
        page += 1
    return sorted(out, key=lambda p: p["merged_at"], reverse=True)[:n]


def pr_file_stats(cur, gh, repo: str, number: int) -> dict[str, list[int] | None]:
    """PR の変更ファイルと差分の行数。{パス: [追加, 削除] か None}。pr.files に残したものを使い、なければ GitHub から引いて残す。
    行数を持たない古い記録（ADR 0051 の前）は、ファイル名だけ（行数は None）"""
    cur.execute("select payload from events where dedupe_key=%s", (f"pr.files:{repo}#{number}",))
    r = cur.fetchone()
    if r:
        stats = (r["payload"] or {}).get("stats") or {}
        return {f: stats.get(f) for f in (r["payload"] or {}).get("files") or []}
    got_all: list[dict] = []
    for page in range(1, 6):
        got = gh("GET", f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}") or []
        got_all += got
        if len(got) < 100:
            break
    files = [f["filename"] for f in got_all]
    stats = {f["filename"]: [int(f.get("additions") or 0), int(f.get("deletions") or 0)] for f in got_all if "additions" in f}
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('pr.files',null,%s,%s) on conflict (dedupe_key) do nothing",
                (Jsonb({"repo": repo, "number": number, "files": files, "stats": stats}), f"pr.files:{repo}#{number}"))
    return {f: stats.get(f) for f in files}


def pr_files(cur, gh, repo: str, number: int) -> list[str]:
    """PR の変更ファイル（pr_file_stats のファイル名だけ）"""
    return list(pr_file_stats(cur, gh, repo, number))


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


def _ts(v) -> datetime | None:
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if isinstance(v, str) and v else v if isinstance(v, datetime) else None


def concurrency(intervals: list[tuple[datetime, datetime]]) -> int:
    """同時に開いていた PR の数の最大（開いた〜マージの区間の重なり）"""
    ev = sorted([(a, 1) for a, b in intervals] + [(b, -1) for a, b in intervals], key=lambda x: (x[0], x[1]))
    cur = best = 0
    for _, d in ev:
        cur += d
        best = max(best, cur)
    return best


def is_generated(gh, repo: str, path: str) -> bool:
    """先頭（600 字）に生成物の印があるか。読めなければ False"""
    try:
        c = gh("GET", f"repos/{repo}/contents/{path}") or {}
        head = base64.b64decode(c.get("content") or "").decode("utf-8", "replace")[:600].lower()
    except Exception:  # noqa: BLE001  形の見分けで見回りを止めない
        return False
    return any(m in head for m in GENERATED_MARKS)


def shape(path: str, added: int, deleted: int, generated: bool) -> str:
    """ファイルの形（ADR 0051 の 1）。生成物 → コードは登録の箇所 → 追記の割合で追記型 → それ以外は中身"""
    if generated:
        return "generated"
    if os.path.splitext(path)[1] in CODE_EXT:
        return "registry"
    if added + deleted == 0:
        return "unknown"
    return "append" if added / (added + deleted) >= APPEND_RATIO else "content"


def profile(per_pr: list[tuple[dict, dict]]) -> dict[str, dict]:
    """ファイルごとの追加・削除の行数と、同時に開いていた PR の数。per_pr は [(PR, {パス: [追加, 削除] か None})]"""
    out: dict[str, dict] = {}
    spans: dict[str, list] = {}
    for p, stats in per_pr:
        a, b = _ts(p.get("created_at")), _ts(p.get("merged_at"))
        for f, st in stats.items():
            g = out.setdefault(f, {"added": 0, "deleted": 0})
            if st:
                g["added"] += st[0]
                g["deleted"] += st[1]
            if a and b:
                spans.setdefault(f, []).append((a, b))
    for f, g in out.items():
        g["concurrent"] = concurrency(spans.get(f, []))
    return out


def aggregate_candidates(changes: Counter, prof: dict[str, dict], shapes: dict[str, str], marked: set[str]) -> dict[str, dict]:
    """取りまとめの候補: 変更回数の上位で、同時に開いていた PR が MIN_CONCURRENT 本以上ある追記型・生成物（印のあるものも含む）"""
    top = [f for f, _ in sorted(changes.items(), key=lambda kv: (-kv[1], kv[0])) if f not in EXEMPT][:TOP_FILES]
    return {f: {"changes": changes[f], "rank": top.index(f) + 1, "concurrent": prof[f]["concurrent"], "shape": shapes[f]}
            for f in top if shapes.get(f) in ("append", "generated") and (prof[f]["concurrent"] >= MIN_CONCURRENT or f in marked)}


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
    res = {"marked": [], "unmarked": [], "candidates": []}
    day = f"{now:%Y-%m-%d}"
    for repo in repos:
        prs = recent_merged(gh, repo)
        if not prs:
            continue
        changes: Counter = Counter()
        per_pr = []
        for p in prs:
            st = pr_file_stats(cur, gh, repo, p["number"])
            per_pr.append((p, st))
            changes.update(set(st))
        prof = profile(per_pr)
        top = [f for f, _ in sorted(changes.items(), key=lambda kv: (-kv[1], kv[0])) if f not in EXEMPT][:TOP_FILES]
        shapes = {f: shape(f, prof[f]["added"], prof[f]["deleted"], is_generated(gh, repo, f)) for f in top}
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
                       "shape": shapes.get(f, "unknown"), "concurrent": prof[f]["concurrent"],
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
        # 取りまとめの候補（ADR 0051 の 1）。日ごとに 1 回記録し、レポートは直近の日の記録を読む
        for f, c in aggregate_candidates(changes, prof, shapes, set(marks) | set(cand)).items():
            payload = {"repo": repo, "file": f, **c, "shape_ja": SHAPE_JA[c["shape"]], "prs": len(prs), "since": prs[-1]["merged_at"],
                       "conflicts": conf.get(f, {}).get("conflicts", 0),
                       "why": f"直近 {len(prs)} 件の PR で {c['changes']} 回変わり（{c['rank']} 位）、同時に {c['concurrent']} 本開いていた{SHAPE_JA[c['shape']]}"}
            cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('hotfile.aggregate_candidate',null,%s,%s) "
                        "on conflict (dedupe_key) do nothing", (Jsonb(payload), f"hotfile.aggregate_candidate:{repo}:{f}:{day}"))
            res["candidates"].append(f"{repo}:{f}")
    return res


def aggregate_candidates_latest(cur, repo: str | None = None) -> list[dict]:
    """直近の日の取りまとめの候補（製品ごと）。レポートと改善チケットの入力"""
    cur.execute("""select payload from events e where name='hotfile.aggregate_candidate' and (%s::text is null or payload->>'repo'=%s)
                    and occurred_at >= (select max(occurred_at) - interval '1 hour' from events x where x.name='hotfile.aggregate_candidate'
                                         and x.payload->>'repo'=e.payload->>'repo')
                    order by payload->>'repo', (payload->>'rank')::int""", (repo, repo))
    return [r["payload"] for r in cur.fetchall()]
