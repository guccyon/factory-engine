"""内部品質の診断と変更の難易度を制御プレーンに取り込む（definitions/quality.yaml、ADR 0017）。

日次レポート（health/daily/status_report.py）が描く前に 1 回呼ぶ:
- 製品の CI（.github/workflows/quality.yml）が main を測った成果物 quality-snapshot を、製品ごとに 1 日 1 件 quality.snapshot に写す。
  前回より悪くなった指標は quality.regressed に残す（教師信号 quality_regression の元）
- マージ済みの工場の PR（pr.merged）のうち難易度のないものに、差分から難易度を決めて pr.difficulty を残す（変更のコストの層）

ここは測らない（測るのは snapshot.py）。数値はワーカーに渡さない。LLM を使わない。
conn は psycopg の接続（autocommit、行はタプル）。gh は (method, path) → JSON の関数。
"""
from __future__ import annotations

import io
import json
import os
import re
import urllib.request
import zipfile

import yaml

WORKFLOW = "quality.yml"
ARTIFACT = "quality-snapshot"
MAX_BYTES = 1_000_000
KEEP = ("sha", "score", "metrics", "counts", "distributions", "lines", "measured_languages", "hotspots", "tool_versions")
PR_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)$")


def load_definitions(path: str | None = None) -> dict:
    path = path or os.environ.get("QUALITY_DEFINITIONS") or os.path.join(os.path.dirname(__file__), "..", "..", "definitions", "quality.yaml")
    if not os.path.exists(path):
        path = "/definitions/quality.yaml"
    return yaml.safe_load(open(path, encoding="utf-8"))


# ------------------------------------------------------------------ 診断の取り込み
def _download(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "factory-status-report"})
    req.add_unredirected_header("Authorization", f"Bearer {token}")   # 成果物は署名付きの URL に転送されるので、転送先には付けない
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("artifact too large")
    return data


def latest_snapshot(repo: str, gh, token: str) -> tuple[dict, dict] | None:
    """main の直近の成功した実行の成果物を (snapshot, run) で返す。なければ None"""
    runs = gh("GET", f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?branch=main&status=success&per_page=1") or {}
    if not runs.get("workflow_runs"):
        return None
    run = runs["workflow_runs"][0]
    arts = gh("GET", f"repos/{repo}/actions/runs/{run['id']}/artifacts") or {}
    art = next((a for a in arts.get("artifacts", []) if a.get("name") == ARTIFACT and not a.get("expired")), None)
    if not art:
        return None
    z = zipfile.ZipFile(io.BytesIO(_download(art["archive_download_url"], token)))
    name = next((n for n in z.namelist() if n.endswith(".json")), None)
    if not name:
        return None
    return json.loads(z.read(name)), {"id": run["id"], "created_at": run.get("created_at"), "head_sha": run.get("head_sha")}


def to_payload(repo: str, day: str, snap: dict, run: dict) -> dict:
    """成果物から決まった項目だけを取る（成果物は製品の CI が書いたもの。ほかの項目は持ち込まない）"""
    p = {k: snap.get(k) for k in KEEP}
    p["hotspots"] = [{"file": str(h.get("file"))[:300], "changes": h.get("changes"), "max_ccn": h.get("max_ccn"), "score": h.get("score")}
                     for h in (snap.get("hotspots") or [])[:20]]
    p.update({"repo": repo, "day": day, "run_id": run["id"], "run_created_at": run["created_at"]})
    if run.get("head_sha") and p.get("sha") and run["head_sha"] != p["sha"]:
        p["sha_mismatch"] = True
    return p


def regressions(prev: dict | None, cur: dict, defs: dict) -> list[dict]:
    """前回の診断より悪くなった指標（スコアに入る指標と、下限の規則の数）。原因の PR は from_sha..to_sha の間にマージされたもの"""
    if not prev or prev.get("sha") == cur.get("sha"):
        return []
    out = []
    for k in defs["score"]["components"]:
        b, a = (prev.get("metrics") or {}).get(k), (cur.get("metrics") or {}).get(k)
        if not isinstance(b, (int, float)) or not isinstance(a, (int, float)):
            continue
        worse = a > b if defs["metrics"][k]["better"] == "lower" else a < b
        if worse and abs(a - b) > 1e-9:
            out.append({"metric": k, "before": b, "after": a, "from_sha": prev.get("sha"), "to_sha": cur.get("sha")})
    for k in defs["floor"]["rules"]:
        b, a = (prev.get("counts") or {}).get(k), (cur.get("counts") or {}).get(k)
        if isinstance(b, int) and isinstance(a, int) and a > b:
            out.append({"metric": k, "before": b, "after": a, "from_sha": prev.get("sha"), "to_sha": cur.get("sha"), "floor": True})
    return out


def _insert(cur, name: str, payload: dict, dedupe: str, corr: str | None = None) -> bool:
    from psycopg.types.json import Jsonb
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values (%s,%s,%s,%s) on conflict (dedupe_key) do nothing returning id",
                (name, corr, Jsonb(payload), dedupe))
    return cur.fetchone() is not None


def ingest_snapshots(conn, repos: list[str], day: str, gh, token: str, defs: dict, log=print) -> int:
    n = 0
    with conn.cursor() as cur:
        for repo in repos:
            cur.execute("select 1 from events where dedupe_key=%s", (f"quality.snapshot:{repo}:{day}",))
            if cur.fetchone():
                continue
            try:
                got = latest_snapshot(repo, gh, token)
            except Exception as e:  # noqa: BLE001  取れなければ翌日にやり直す。レポートは止めない
                log("[quality] 診断を取れない", repo, repr(e))
                continue
            if not got:
                continue
            payload = to_payload(repo, day, *got)
            cur.execute("select payload from events where name='quality.snapshot' and payload->>'repo'=%s and payload->>'day' < %s "
                        "order by payload->>'day' desc limit 1", (repo, day))
            row = cur.fetchone()
            if not _insert(cur, "quality.snapshot", payload, f"quality.snapshot:{repo}:{day}"):
                continue
            n += 1
            for r in regressions(row[0] if row else None, payload, defs):
                _insert(cur, "quality.regressed", {"repo": repo, "day": day, **r}, f"quality.regressed:{repo}:{day}:{r['metric']}")
    return n


# ------------------------------------------------------------------ 変更の難易度
def difficulty(files: list[dict], defs: dict) -> dict:
    """PR の差分から難易度を決める（quality.yaml の change_cost.difficulty）。files は GitHub の pulls/{n}/files"""
    d = defs["change_cost"]["difficulty"]
    ex = re.compile("|".join(d["exclude_paths"]))
    test = re.compile("|".join(d["test_paths"]))
    pts = d["points"]
    paths = [f.get("filename", "") for f in files]
    areas = sorted({"/".join(p.split("/")[:2]) for p in paths if not ex.search(p)})
    contract = any(re.search(r, p) for p in paths for r in pts["contract_diff"]["paths"])
    schema = any(re.search(r, p) for p in paths for r in pts["schema_diff"]["paths"])
    new_files = sum(1 for p, f in zip(paths, files) if f.get("status") == "added" and not ex.search(p) and not test.search(p))
    lines = sum((f.get("additions") or 0) + (f.get("deletions") or 0) for f in files)
    points = max(0, len(areas) - 1) + (pts["contract_diff"]["points"] if contract else 0) + (pts["schema_diff"]["points"] if schema else 0)
    points += pts["new_files"]["points"] if new_files >= 3 else 0
    points += max([p for limit, p in pts["size"]["points"] if lines > limit] or [0])
    level = next(k for k, (lo, hi) in d["levels"].items() if lo <= points <= hi)
    return {"files": len(files), "lines": lines, "areas": areas, "contract_diff": contract, "schema_diff": schema,
            "new_files": new_files, "points": points, "level": level}


def pr_files(gh, repo: str, number: int) -> list[dict]:
    files, page = [], 1
    while page <= 10:
        got = gh("GET", f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}") or []
        files += got
        if len(got) < 100:
            break
        page += 1
    return files


def ingest_difficulty(conn, gh, defs: dict, limit: int = 30, log=print) -> int:
    """難易度のないマージ済みの工場の PR に pr.difficulty を残す（新しい順に limit 本まで。残りは翌日）"""
    n = 0
    with conn.cursor() as cur:
        cur.execute("""select e.correlation_id, e.payload->>'pr_url' from events e
                        where e.name='pr.merged' and not exists (select 1 from events d where d.dedupe_key = 'pr.difficulty:' || (e.payload->>'pr_url'))
                        order by e.id desc limit %s""", (limit,))
        for corr, url in cur.fetchall():
            m = PR_URL.match(url or "")
            if not m:
                continue
            try:
                change = difficulty(pr_files(gh, m.group(1), int(m.group(2))), defs)
            except Exception as e:  # noqa: BLE001
                log("[quality] 難易度を決められない", url, repr(e))
                continue
            n += _insert(cur, "pr.difficulty", {"pr_url": url, **change}, f"pr.difficulty:{url}", corr)
    return n
