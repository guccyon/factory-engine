"""出荷の QA の関門で流すストーリーを選ぶ（ADR 0060）。決定的（LLM を使わない）。記録と製品のファイルの一覧を読んで決めるだけ。

ストーリー 1 本ずつに、最初に当たったものを理由にする:
  never    まだ 1 回も確かめていない                                   → 流す
  changed  最後に確かめたあとに出た版の PR が、そのストーリーに関係する → 流す
  stale    最後に確かめてから gate.stale_hours を超えた                → 流す
  fresh    どれでもない                                                 → 流さない

「確かめた」は、そのストーリーの判定（QAVerdict）が pass か hold で出たこと。出どころは問わない。
PR がストーリーに関係するかは、(1) その PR のチケットの Plan の stories の宣言、(2) 宣言がなければ PR が変えたファイルのパス、で決める。
迷ったら広く取る（どれにも当たらないファイル・読めなかった PR は全部のストーリー）。狭く取りすぎた分は stale が拾う。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

ALL = "all"
IGNORED_PREFIXES = ("docs/", "changelog.d/")         # 利用者の流れに触らないパス
IGNORED_NAMES = ("CHANGELOG.md",)
STORY_FILE = re.compile(r"^specs/stories/([A-Za-z0-9_-]+)\.yaml$")
APP_DIR = re.compile(r"^apps/([A-Za-z0-9_.-]+)/")
PR_URL = re.compile(r"/pull/(\d+)$")
PR_FILES_MAX = 300   # これより多い PR は広い変更として全部のストーリーにする
RUN_ORDER = {"changed": 0, "never": 0, "stale": 1}


def _norm(s: str) -> str:
    return s.lower().replace("-", "_")


def last_verified(cur, repo: str) -> dict[str, datetime]:
    """ストーリーごとの、最後に確かめたジョブの始まった時刻（pass か hold の判定）。その時刻より後に出た版は、まだ確かめていない"""
    cur.execute("""select a.payload->>'story_id' as s, max(coalesce(j.started_at, a.created_at)) as at
                     from artifacts a join jobs j on j.id = a.job_id
                    where a.type='QAVerdict' and j.repo=%s and a.payload->>'decision' in ('pass','hold')
                    group by 1""", (repo,))
    return {r["s"]: r["at"] for r in cur.fetchall() if r["s"]}


def prs_since(cur, repo: str, since: datetime) -> list[str]:
    """その時刻より後に作った版に載った PR（古い順、重複なし）"""
    cur.execute("select payload->'prs' as prs from events where name='release.created' and payload->>'repo'=%s and occurred_at > %s order by id",
                (repo, since))
    out: list[str] = []
    for r in cur.fetchall():
        for u in r["prs"] or []:
            if u not in out:
                out.append(u)
    return out


def declared(cur, pr_url: str) -> list[str] | None:
    """その PR のチケットの最新の Plan が宣言したストーリー（増やす・変える・守る）。宣言がなければ None"""
    cur.execute("select correlation_id from jobs where result->>'pr_url'=%s and correlation_id is not null order by id limit 1", (pr_url,))
    r = cur.fetchone()
    if not r:
        return None
    cur.execute("select payload->'stories' as st from artifacts where type='Plan' and correlation_id=%s and payload->>'decision' in ('ready','split') "
                "and jsonb_typeof(payload->'stories')='array' and jsonb_array_length(payload->'stories') > 0 order by id desc limit 1",
                (r["correlation_id"],))
    p = cur.fetchone()
    if not p:
        return None
    ids = [str(x.get("id") if isinstance(x, dict) else x) for x in p["st"] or []]
    return [i for i in ids if i and i != "None"] or None


def by_files(files: list[str], docs: list[dict]) -> set[str] | str:
    """PR が変えたファイルのパスから、関係するストーリー。どれにも当たらないファイルがあれば ALL"""
    contexts = {_norm(c): c for d in docs for c in (d.get("touches") or {}).get("contexts") or []}
    out: set[str] = set()
    for f in files:
        if f.endswith(".md") or f in IGNORED_NAMES or f.startswith(IGNORED_PREFIXES):
            continue
        m = STORY_FILE.match(f)
        if m:
            out.add(m.group(1))
            continue
        segs = {_norm(re.sub(r"\.[A-Za-z0-9]+$", "", s)) for s in f.split("/")}
        hit = {contexts[c] for c in contexts if c in segs}
        if hit:
            out |= {d["id"] for d in docs if hit & set((d.get("touches") or {}).get("contexts") or [])}
            continue
        a = APP_DIR.match(f)
        got = {d["id"] for d in docs if a and a.group(1) in ((d.get("touches") or {}).get("apps") or [])}
        if not got:
            return ALL   # 配備・共通の土台・依存の更新など。どのストーリーにも効きうる
        out |= got
    return out


def related(cur, gh, repo: str, pr_url: str, docs: list[dict]) -> tuple[set[str] | str, str]:
    """PR 1 つが関係するストーリーと、決め方（declared | files | all）"""
    ids = declared(cur, pr_url)
    if ids is not None:
        return set(ids), "declared"
    m = PR_URL.search(pr_url)
    if not m:
        return ALL, "all"
    files: list[str] = []
    try:
        for page in range(1, PR_FILES_MAX // 100 + 1):
            got = gh("GET", f"repos/{repo}/pulls/{m.group(1)}/files?per_page=100&page={page}")
            if not isinstance(got, list):
                return ALL, "all"
            files += [x["filename"] for x in got if isinstance(x, dict) and x.get("filename")]
            if len(got) < 100:
                break
        else:
            return ALL, "all"   # ファイルが多すぎる PR は広い変更として扱う
    except Exception:  # noqa: BLE001  読めなければ広く取る
        return ALL, "all"
    if not files:
        return ALL, "all"
    got = by_files(files, docs)
    return got, ("all" if got == ALL else "files")


def select(cur, gh, repo: str, docs: list[dict], stale_hours: float, now: datetime) -> dict[str, dict]:
    """ストーリーごとの選び方。{story: {run, why: never|changed|stale|fresh, prs?: [番号], last?: ISO}}"""
    seen = last_verified(cur, repo)
    cache: dict[str, tuple] = {}
    out: dict[str, dict] = {}
    for d in docs:
        sid, last = d["id"], seen.get(d["id"])
        if last is None:
            out[sid] = {"run": True, "why": "never"}
            continue
        hits = []
        for u in prs_since(cur, repo, last):
            if u not in cache:
                cache[u] = related(cur, gh, repo, u, docs)
            ids, _ = cache[u]
            if ids == ALL or sid in ids:
                m = PR_URL.search(u)
                hits.append(int(m.group(1)) if m else u)
        if hits:
            out[sid] = {"run": True, "why": "changed", "prs": hits[:10], "last": last.isoformat()}
        elif now - last > timedelta(hours=float(stale_hours)):
            out[sid] = {"run": True, "why": "stale", "last": last.isoformat()}
        else:
            out[sid] = {"run": False, "why": "fresh", "last": last.isoformat()}
    return out


def ordered(ids: list[str], selection: dict[str, dict]) -> list[str]:
    """流すものだけを、変更に関係するもの → しばらく確かめていないものの順に（同じ理由の中は元の並び）"""
    run = [i for i in ids if (selection.get(i) or {"run": True}).get("run")]
    return sorted(run, key=lambda i: RUN_ORDER.get((selection.get(i) or {}).get("why"), 0))


WHY_JA = {"never": "まだ確かめていない", "changed": "変更に関係する", "stale": "しばらく確かめていない", "fresh": "変更に関係なし"}


def line(sid: str, sel: dict, tz) -> str:
    """報告と Issue の本文の 1 行"""
    last = datetime.fromisoformat(sel["last"]).astimezone(tz).strftime("%m/%d %H:%M") if sel.get("last") else None
    if sel["why"] == "changed":
        return f"{sid}（{WHY_JA['changed']}: " + "・".join(f"#{n}" for n in sel.get("prs") or []) + "）"
    if sel["why"] == "fresh":
        return f"{sid}（{WHY_JA['fresh']}。{last} に確認済み）"
    return f"{sid}（{WHY_JA[sel['why']]}" + (f"。前回 {last}" if last else "") + "）"
