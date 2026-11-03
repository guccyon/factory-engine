"""PR の差分を取る（検査・judge・本文の直しの共通）。

`gh pr diff` は GitHub の差分の API を使うので、変更ファイルが 300 を超える PR では HTTP 406（too_large）で必ず失敗する。
その場合はファイルごとの差分（pulls/<n>/files、3000 ファイルまで）から組み立てる。依存の置き場や生成物に見えるファイル
（node_modules/ など）は後ろに回し、件数を冒頭に書く。検査官がそれを欠陥として見られるようにするため
（2026-11-03 rally #678: tools/design-catalog/node_modules を含む 435 ファイルの PR で検査が毎回落ち、PR が止まった）。
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Callable

VENDORED = re.compile(r"(^|/)(node_modules|vendor|dist|build|target|\.dart_tool|\.next|coverage)/")
TOO_LARGE = ("too_large", "maximum number of files", "HTTP 406")


def assemble(files: list[dict]) -> str:
    """ファイルごとの差分（API の files の要素）から unified diff の形に組み立てる。依存の置き場・生成物は後ろに回す"""
    vendored = [f for f in files if VENDORED.search(f.get("filename", ""))]
    own = [f for f in files if not VENDORED.search(f.get("filename", ""))]
    head = [f"[差分が大きく（{len(files)} ファイル）、GitHub の差分の API で取れなかったので、ファイルごとの差分から組み立てた]"]
    if vendored:
        dirs = sorted({VENDORED.search(f["filename"]).group(0).strip("/") for f in vendored})
        head.append(f"[依存の置き場や生成物に見えるファイルが {len(vendored)} 件ある（{', '.join(dirs)}）。"
                    "ふつうはリポジトリに入れない。差分の後ろにまとめた]")
    out = ["\n".join(head), ""]
    for f in own + vendored:
        name = f.get("filename", "?")
        out.append(f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}  ({f.get('status', '')}, +{f.get('additions', 0)} -{f.get('deletions', 0)})")
        out.append(f.get("patch") or "（差分の本文なし: バイナリか、1 ファイルの差分が大きすぎる）")
    return "\n".join(out) + "\n"


def fetch(repo: str, number: int | str, sh: Callable[..., str]) -> str:
    """PR の差分。sh は呼び出し元の gh の呼び方（一時的な失敗の繰り返しを含む）"""
    r = subprocess.run(["gh", "pr", "diff", str(number), "-R", repo], capture_output=True, text=True)
    if r.returncode == 0:
        return r.stdout
    if not any(m in (r.stderr or "") for m in TOO_LARGE):
        return sh("gh", "pr", "diff", str(number), "-R", repo)   # 一時的な失敗は今までどおり繰り返す
    lines = sh("gh", "api", f"repos/{repo}/pulls/{number}/files", "--paginate",
               "--jq", ".[] | {filename, status, additions, deletions, patch}")
    return assemble([json.loads(l) for l in lines.splitlines() if l.strip()])
