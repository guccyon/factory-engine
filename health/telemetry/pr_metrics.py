#!/usr/bin/env python3
"""PR 単位の結果テレメトリ（P0 Week 2 の最小実装）。

definitions/metrics.yaml の式をそのまま実装する。取得元は GitHub（gh CLI）、git log、
Claude Code のセッションログ（~/.claude/projects/）の 3 つ。
状態は持たない（毎回計算し直す）。P0a で成果物ストアができたら、出力の JSON をそこに流す。

使い方:
  pr_metrics.py --repo guccyon/rally --git-dir ~/src/github.com/guccyon/rally/main \
                --limit 20 --out telemetry.json [--as-of 2026-10-25T00:00:00Z]

依存: python3（標準ライブラリのみ）、gh（認証済み）、git
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# 初期値（開始定義 第 3 節 Week 2 で承認を求める。P0 Exit で実測により改訂）
REVERT_WINDOW_DAYS = 7      # マージ後 N 日以内の revert / hotfix
CHURN_WINDOW_DAYS = 21      # 新規ファイルの大幅書き換えを見る期間
CHURN_RATIO = 0.50          # 変更行 / 作成時行数 がこれを超えたら churn
REVIEW_MARKER = re.compile(r"レビュー|review", re.IGNORECASE)   # レビュー往復を示すコメントの印
# churn の対象外（追記で育つ文書・知見ファイル。コードの手戻りを測る指標なので外す）
CHURN_EXCLUDE = re.compile(r"^(\.claude/memories/|docs/|CHANGELOG\.md$|README(\.\w+)?\.md$)")


def sh(*args: str, cwd: str | None = None) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, cwd=cwd).stdout


def gh_json(path: str):
    return json.loads(sh("gh", "api", "--paginate", path))


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def hours(a: datetime, b: datetime) -> float:
    return round((b - a).total_seconds() / 3600, 2)


# ---------------------------------------------------------------- GitHub
def merged_prs(repo: str, base: str, limit: int) -> list[dict]:
    prs = gh_json(f"repos/{repo}/pulls?state=closed&base={base}&per_page=100&sort=updated&direction=desc")
    prs = [p for p in prs if p.get("merged_at")]
    prs.sort(key=lambda p: p["merged_at"], reverse=True)
    return prs[:limit]


def closing_issues(repo: str, number: int) -> list[dict]:
    out = sh("gh", "pr", "view", str(number), "-R", repo, "--json", "closingIssuesReferences,body,title")
    d = json.loads(out)
    issues = [{"number": i["number"], "source": "closing_reference"} for i in d["closingIssuesReferences"]]
    # 本文・タイトルの #N 参照（closing reference がないときの補助。帰属ルールは metrics.yaml）
    seen = {i["number"] for i in issues}
    for m in re.finditer(r"(?<![\w/])#(\d+)", (d["title"] or "") + "\n" + (d["body"] or "")):
        n = int(m.group(1))
        if n != number and n not in seen:
            issues.append({"number": n, "source": "text_reference"})
            seen.add(n)
    return issues


def resolve_issue(repo: str, number: int) -> dict | None:
    """#N がこのリポジトリの Issue なら created_at を返す。存在しない（他リポジトリの番号）か PR なら None。"""
    try:
        d = json.loads(sh("gh", "api", f"repos/{repo}/issues/{number}"))
    except subprocess.CalledProcessError:
        return {"kind": "unresolved"}
    if "pull_request" in d:   # #N が PR だった（統合ブランチの段階 PR など）
        return {"kind": "pull_request"}
    return {"kind": "issue", "created_at": d["created_at"]}


def review_loops(repo: str, number: int, author: str, merged_at: datetime) -> dict:
    """差し戻し → 修正の往復回数。
    往復 1 回 = レビューの印のあるコメント（または CHANGES_REQUESTED のレビュー）の後に、マージ前のコミットがあること。
    レビューは別エージェントが同じアカウントで書くので、著者で区別しない。"""
    comments = gh_json(f"repos/{repo}/issues/{number}/comments?per_page=100")
    reviews = gh_json(f"repos/{repo}/pulls/{number}/reviews?per_page=100")
    commits = gh_json(f"repos/{repo}/pulls/{number}/commits?per_page=100")
    events = []
    for c in comments:
        if REVIEW_MARKER.search(c.get("body") or ""):
            events.append(parse_ts(c["created_at"]))
    for r in reviews:
        if r.get("state") in ("CHANGES_REQUESTED", "COMMENTED") and r.get("submitted_at"):
            events.append(parse_ts(r["submitted_at"]))
    commit_times = sorted(parse_ts(c["commit"]["committer"]["date"]) for c in commits)
    loops = 0
    for t in sorted(events):
        if any(t < ct <= merged_at for ct in commit_times):
            loops += 1
    return {"value": loops, "review_events": len(events), "commits": len(commits),
            "method": "comment_marker+changes_requested", "missing": False}


def pr_files(repo: str, number: int) -> list[dict]:
    return gh_json(f"repos/{repo}/pulls/{number}/files?per_page=100")


# ---------------------------------------------------------------- git
def commits_after(git_dir: str, since: datetime, until: datetime, paths: list[str] | None = None) -> list[dict]:
    args = ["git", "log", "origin/main", "--first-parent", "--format=%H%x1f%cI%x1f%s", "--numstat",
            f"--since={since.isoformat()}", f"--until={until.isoformat()}"]
    if paths:
        args += ["--"] + paths
    out = sh(*args, cwd=git_dir)
    commits, cur = [], None
    for line in out.splitlines():
        if "\x1f" in line:
            h, d, s = line.split("\x1f")
            cur = {"sha": h, "date": d, "subject": s, "files": {}}
            commits.append(cur)
        elif line.strip() and cur is not None:
            parts = line.split("\t")
            if len(parts) == 3:
                a, dl, f = parts
                cur["files"][f] = (int(a) if a.isdigit() else 0) + (int(dl) if dl.isdigit() else 0)
    return commits


def revert_hotfix(git_dir: str, pr: dict, files: list[str], merged_at: datetime, as_of: datetime) -> dict:
    window_end = merged_at + timedelta(days=REVERT_WINDOW_DAYS)
    censored = as_of < window_end
    later = commits_after(git_dir, merged_at + timedelta(seconds=1), min(window_end, as_of))
    revert = [c for c in later if re.search(r"revert", c["subject"], re.I)
              and (f"#{pr['number']}" in c["subject"] or pr["merge_commit_sha"][:7] in c["subject"])]
    touched = set(files)
    hotfix = [c for c in later if re.match(r"(fix|hotfix)\b", c["subject"], re.I)
              and touched & set(c["files"]) and c["sha"] != pr["merge_commit_sha"]]
    return {"revert": bool(revert), "hotfix": bool(hotfix),
            "revert_commits": [c["sha"][:7] for c in revert], "hotfix_commits": [c["sha"][:7] for c in hotfix],
            "window_days": REVERT_WINDOW_DAYS, "censored": censored,
            "observed_days": round((min(window_end, as_of) - merged_at).total_seconds() / 86400, 1)}


def churn(git_dir: str, pr: dict, files: list[dict], merged_at: datetime, as_of: datetime) -> dict:
    added = {f["filename"]: f["additions"] for f in files
             if f["status"] == "added" and f["additions"] > 0 and not CHURN_EXCLUDE.search(f["filename"])}
    window_end = merged_at + timedelta(days=CHURN_WINDOW_DAYS)
    censored = as_of < window_end
    if not added:
        return {"value": None, "reason": "no_new_files", "new_files": 0, "censored": censored}
    later = commits_after(git_dir, merged_at + timedelta(seconds=1), min(window_end, as_of), list(added))
    changed = defaultdict(int)
    for c in later:
        if c["sha"] == pr["merge_commit_sha"]:
            continue
        for f, n in c["files"].items():
            changed[f] += n
    churned = [f for f, base in added.items() if changed.get(f, 0) / base > CHURN_RATIO]
    value = None if (censored and not churned) else bool(churned)   # 期間内に超えていれば確定、未経過で未超過は「未確定」
    return {"value": value, "new_files": len(added), "churned_files": churned,
            "window_days": CHURN_WINDOW_DAYS, "ratio": CHURN_RATIO, "censored": censored}


# ---------------------------------------------------------------- Claude Code session logs
def session_token_index(repo: str) -> dict[int, list[dict]]:
    """pr-link を持つセッションの modelUsage を PR に配る。1 セッションが複数 PR に紐付くときは均等割り（近似）。"""
    by_pr: dict[int, list[dict]] = defaultdict(list)
    for path in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        linked, cost = set(), None
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if '"pr-link"' not in line and '"cost-state"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") == "pr-link" and d.get("prRepository") == repo:
                        linked.add(int(d["prNumber"]))
                    elif d.get("type") == "cost-state":
                        cost = d   # 最後の cost-state が累計
        except OSError:
            continue
        if linked and cost and cost.get("modelUsage"):
            share = 1 / len(linked)
            for n in linked:
                by_pr[n].append({"session": os.path.basename(path)[:8], "share": share,
                                 "linked_prs": sorted(linked), "modelUsage": cost["modelUsage"],
                                 "totalCostUSD": cost.get("totalCostUSD")})
    return by_pr


def control_plane_token_index(db: str | None, repo: str) -> dict[int, dict]:
    """制御プレーン（orchestrator）の events から、工場のワーカーが作った PR の usage を引く。
    db は "docker:<container>"（手元の Mac に psql がないとき）か psql の DSN。"""
    if not db:
        return {}
    sql = ("select a.payload->>'pr_url', e.payload from artifacts a join events e on e.job_id=a.job_id and e.name='session.cost_recorded' "
           f"where a.type='PR' and a.payload->>'repo'='{repo}'")
    if db.startswith("docker:"):
        out = sh("docker", "exec", "-i", db.split(":", 1)[1], "psql", "-U", "factory", "-d", "factory", "-At", "-F", "\t", "-c", sql)
    else:
        out = sh("psql", db, "-At", "-F", "\t", "-c", sql)
    idx = {}
    for line in out.splitlines():
        url, payload = line.split("\t", 1)
        n = int(url.rstrip("/").rsplit("/", 1)[1])
        p = json.loads(payload)
        u = p.get("usage") or {}
        idx[n] = {"missing": False, "attribution": "exact", "source": "control_plane",
                  "by_model": {p.get("model") or "unknown": {"inputTokens": u.get("input_tokens", 0), "outputTokens": u.get("output_tokens", 0),
                                                              "cacheReadInputTokens": u.get("cache_read_input_tokens", 0), "cacheCreationInputTokens": u.get("cache_creation_input_tokens", 0),
                                                              "costUSD": p.get("cost_usd")}},
                  "cost_usd_estimate": p.get("cost_usd"), "sessions": ["factory-worker"]}
    return idx


def tokens_for(index: dict[int, list[dict]], number: int, cp: dict[int, dict] | None = None) -> dict:
    if cp and number in cp:   # 工場が作った PR は制御プレーンの記録が正（均等割りの近似をしない）
        return cp[number]
    parts = index.get(number)
    if not parts:
        return {"missing": True, "reason": "no_session_with_pr_link"}
    by_model: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    cost = 0.0
    for p in parts:
        for model, u in p["modelUsage"].items():
            for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"):
                by_model[model][k] += u.get(k, 0) * p["share"]
            by_model[model]["costUSD"] += u.get("costUSD", 0) * p["share"]
        cost += (p.get("totalCostUSD") or 0) * p["share"]
    return {"missing": False, "by_model": {m: {k: round(v) if k != "costUSD" else round(v, 4) for k, v in u.items()} for m, u in by_model.items()},
            "cost_usd_estimate": round(cost, 4), "sessions": [p["session"] for p in parts],
            "attribution": "even_split" if any(p["share"] < 1 for p in parts) else "exact"}


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--git-dir", required=True)
    ap.add_argument("--base", default="main")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--out", required=True)
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--human-override", default="", help="人間が自動判定を覆した PR 番号（カンマ区切り。P0 は承認ログがないので手入力）")
    ap.add_argument("--events-db", default=None, help="制御プレーンの DB（docker:factory-postgres-1 か psql の DSN）。工場が作った PR の usage をここから取る")
    a = ap.parse_args()
    as_of = parse_ts(a.as_of) if a.as_of else datetime.now(timezone.utc)
    overrides = {int(x) for x in a.human_override.split(",") if x.strip()}

    sh("git", "fetch", "-q", "origin", a.base, cwd=a.git_dir)
    index = session_token_index(a.repo)
    cp_index = control_plane_token_index(a.events_db, a.repo)
    rows = []
    for pr in merged_prs(a.repo, a.base, a.limit):
        n = pr["number"]
        merged_at = parse_ts(pr["merged_at"])
        created_at = parse_ts(pr["created_at"])
        issues = []
        for i in closing_issues(a.repo, n):
            r = resolve_issue(a.repo, i["number"])
            if r["kind"] == "unresolved":      # dependabot の本文などに出る他リポジトリの番号は捨てる
                continue
            issues.append({**i, "kind": r["kind"], **({"created_at": r["created_at"]} if r["kind"] == "issue" else {})})
        issue_times = [i["created_at"] for i in issues if i["kind"] == "issue"]
        if issue_times:
            start, start_src = parse_ts(min(issue_times)), "earliest_linked_issue"
        else:
            start, start_src = created_at, "pr_created_fallback"
        files = pr_files(a.repo, n)
        rows.append({
            "pr": n, "title": pr["title"], "merged_at": pr["merged_at"], "merge_commit": pr["merge_commit_sha"][:7],
            "issues": issues,
            "lead_time": {"hours": hours(start, merged_at), "start": start.isoformat(), "start_source": start_src,
                          "pr_open_hours": hours(created_at, merged_at)},
            "review_loops": review_loops(a.repo, n, pr["user"]["login"], merged_at),
            "tokens": tokens_for(index, n, cp_index),
            "revert_hotfix": revert_hotfix(a.git_dir, pr, [f["filename"] for f in files], merged_at, as_of),
            "churn": churn(a.git_dir, pr, files, merged_at, as_of),
            "incident": {"missing": True, "reason": "no_monitoring_in_p0"},
            "human_override": {"value": n in overrides, "source": "manual(pr-merge-criteria 判断の記録)"},
            "gate_results": {"missing": True, "reason": "from_p1"},
            "size": {"files": len(files), "additions": pr.get("additions"), "deletions": pr.get("deletions")},
        })
        print(f"PR #{n} done", file=sys.stderr)

    def rate(pred):
        return round(sum(1 for r in rows if pred(r)) / max(len(rows), 1), 2)
    summary = {
        "as_of": as_of.isoformat(), "repo": a.repo, "base": a.base, "count": len(rows),
        "thresholds": {"revert_window_days": REVERT_WINDOW_DAYS, "churn_window_days": CHURN_WINDOW_DAYS, "churn_ratio": CHURN_RATIO},
        "missing_rates": {
            "lead_time_issue_attribution": rate(lambda r: r["lead_time"]["start_source"] != "earliest_linked_issue"),
            "tokens": rate(lambda r: r["tokens"]["missing"]),
            "revert_hotfix_censored": rate(lambda r: r["revert_hotfix"]["censored"]),
            "churn_undetermined": rate(lambda r: r["churn"]["value"] is None),
            "incident": 1.0, "gate_results": 1.0,
        },
    }
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "prs": rows}, fh, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
