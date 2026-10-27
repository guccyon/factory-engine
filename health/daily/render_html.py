#!/usr/bin/env python3
"""建設状況のスナップショットから claude.ai のページ（HTML）を作る（LLM は使わない）。

スナップショットは status_report.py が Mac で作り、固定 Issue（factory:construction）の本文の末尾に埋めている。
このスクリプトはそれを読み、テンプレート（page.html）にデータを差し込むだけ。毎朝のクラウドのセッションは
これを走らせて出来たファイルを公開する。

使い方:
  render_html.py --out construction.html                 # Issue から読む（GH_TOKEN か gh の認証）
  render_html.py --snapshot snap.json --out construction.html
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
STATUS_REPO = os.environ.get("STATUS_REPO", "guccyon/factory-engine")
LABEL = "factory:construction"
MARK = re.compile(r"<!-- factory-snapshot:v1 ([A-Za-z0-9+/=]+) -->")


def token() -> str:
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if t:
        return t
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def from_issue() -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "factory-status-page"}
    if t := token():
        headers["Authorization"] = f"Bearer {t}"
    url = f"https://api.github.com/repos/{STATUS_REPO}/issues?state=open&per_page=1&labels={urllib.parse.quote(LABEL)}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        issues = json.loads(r.read())
    if not issues:
        sys.exit(f"{STATUS_REPO} に {LABEL} の Issue がない（Mac の supervisor がまだ一度も出していない）")
    m = MARK.search(issues[0].get("body") or "")
    if not m:
        sys.exit("Issue の本文にスナップショットがない")
    snap = json.loads(base64.b64decode(m.group(1)))
    snap["issue_url"] = issues[0]["html_url"]
    return snap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", help="スナップショットの JSON（省略時は Issue から読む）")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    snap = json.loads(Path(args.snapshot).read_text()) if args.snapshot else from_issue()
    data = json.dumps(snap, ensure_ascii=False).replace("</", "<\\/")   # script の中で閉じタグにならないように
    html = (HERE / "page.html").read_text().replace("/*__SNAPSHOT__*/null", data)
    Path(args.out).write_text(html)
    print(f"[status-page] {snap['date']} のページを {args.out} に書いた")
    return 0


if __name__ == "__main__":
    sys.exit(main())
