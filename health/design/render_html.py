#!/usr/bin/env python3
"""設計の診断（ADR 0022）のスナップショットから claude.ai のページ（HTML）を作る（LLM は使わない）。

スナップショットは dispatcher（orchestrator/dispatcher/design.py）が、製品の固定 Issue（factory:design-review）の
本文の末尾に埋めている。このスクリプトはそれを読み、テンプレート（page.html）にデータを差し込むだけ。
週 1 回のクラウドの予約がこれを走らせて出来たファイルを同じ URL に公開し直す。

使い方:
  render_html.py --repo guccyon/rally --out design.html     # Issue から読む（GH_TOKEN か gh の認証）
  render_html.py --snapshot snap.json --out design.html
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABEL = "factory:design-review"
MARK = re.compile(r"<!-- factory-design:v1 ([A-Za-z0-9+/=]+) -->")


def token() -> str:
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if t:
        return t
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def parse(body: str) -> dict:
    m = MARK.search(body or "")
    if not m:
        raise ValueError("Issue の本文にスナップショットがない")
    return json.loads(base64.b64decode(m.group(1)))


def from_issue(repo: str) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "factory-design-page"}
    if t := token():
        headers["Authorization"] = f"Bearer {t}"
    url = f"https://api.github.com/repos/{repo}/issues?state=open&per_page=1&labels={urllib.parse.quote(LABEL)}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        issues = json.loads(r.read())
    if not issues:
        sys.exit(f"{repo} に {LABEL} の Issue がない（工場がまだ一度も診断していない）")
    snap = parse(issues[0].get("body") or "")
    snap["issue_url"] = issues[0]["html_url"]
    return snap


def viewpoints() -> dict:
    """観点の名前（definitions/design_review.yaml）。読めなければページの側の既定の名前を使う"""
    try:
        import yaml
        defs = yaml.safe_load((HERE.parent.parent / "definitions" / "design_review.yaml").read_text(encoding="utf-8"))
        return {k: v["ja"] for k, v in defs["viewpoints"].items()}
    except (ImportError, OSError, KeyError, TypeError):
        return {}


def render(snap: dict) -> str:
    snap = {**snap, "viewpoints": snap.get("viewpoints") or viewpoints()}
    data = json.dumps(snap, ensure_ascii=False).replace("</", "<\\/")   # script の中で閉じタグにならないように
    return (HERE / "page.html").read_text(encoding="utf-8").replace("/*__SNAPSHOT__*/null", data)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("DESIGN_REPO", "guccyon/rally"))
    ap.add_argument("--snapshot", help="スナップショットの JSON（省略時は Issue から読む）")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    snap = json.loads(Path(args.snapshot).read_text(encoding="utf-8")) if args.snapshot else from_issue(args.repo)
    Path(args.out).write_text(render(snap), encoding="utf-8")
    print(f"[design-page] {snap.get('repo')} {snap.get('week')} のページを {args.out} に書いた")
    return 0


if __name__ == "__main__":
    sys.exit(main())
