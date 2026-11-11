"""宣言した共有ファイルの直書きを止める（ADR 0051 の 3）。決定的（LLM を使わない）。製品の CI で PR ごとに動かす。

`.factory-config/aggregates.yaml` で断片で更新すると宣言したファイル（generate 以外）を、工場の PR が直接書き換えていたら落とす。
取りまとめの PR（変更が、宣言したファイルの更新と断片の削除だけのもの）は通す。宣言がない製品は何もしない
（CHANGELOG.md の直書きは今までどおり検査のルーブリック DOC-002 で見る）。

人の PR は対象にしない（節の見出しを足すなど、ファイルの形を直すのは人がする）。工場の PR はブランチ名（factory/）で見分ける。

使い方: python3 aggregate_guard.py <製品のディレクトリ> --base <基準の commit> [--branch <PR のブランチ>] [--all]
終了コード: 0 = 問題なし、1 = 直書きがある、2 = 宣言の形が合わない
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import aggregate_compile as ac

FACTORY_PREFIX = "factory/"


def changes(repo: Path, base: str) -> list[tuple[str, str]]:
    """基準からの変更（状態, パス）。リネームは新しいパスで数える"""
    out = subprocess.run(["git", "diff", "--name-status", "--no-renames", f"{base}...HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    return [(l.split("\t", 1)[0][0], l.split("\t", 1)[1]) for l in out.splitlines() if "\t" in l]


def is_compile_pr(decl: list[dict], ch: list[tuple[str, str]]) -> bool:
    """取りまとめの PR か: 変更が宣言したファイルの更新と、断片の置き場の断片の削除だけで、断片を 1 つ以上消している"""
    paths = {a["path"] for a in decl}
    dirs = tuple(a["fragments"].rstrip("/") + "/" for a in decl if a.get("fragments"))
    removed = [p for s, p in ch if s == "D" and dirs and p.startswith(dirs)]
    return bool(removed) and all(p in paths or p in removed for s, p in ch)


def violations(decl: list[dict], ch: list[tuple[str, str]]) -> list[str]:
    if is_compile_pr(decl, ch):
        return []
    direct = {a["path"]: a for a in decl if a["compile"] != "generate"}
    return [f"{p} を直接書き換えている。足す内容は {direct[p]['fragments'].rstrip('/')}/<Issue 番号>.md に書く（ADR 0051）"
            for s, p in ch if p in direct and s != "D"]


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    opt = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i] in ("--base", "--branch")}
    repo = Path(args[0] if args else ".")
    if "--all" not in argv and not opt.get("--branch", "").startswith(FACTORY_PREFIX):
        print("aggregate-guard: 工場の PR ではないので見ない")
        return 0
    if not (repo / ac.DECLARATION).exists():
        print("aggregate-guard: 宣言（.factory-config/aggregates.yaml）がない")
        return 0
    try:
        decl = ac.load(repo)
    except ac.Invalid as e:
        print(f"aggregate-guard: {e}", file=sys.stderr)
        return 2
    bad = violations(decl, changes(repo, opt.get("--base", "origin/main")))
    for b in bad:
        print(f"::error::{b}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
