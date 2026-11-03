"""CHANGELOG の断片の取りまとめ（ADR 0027 の 1）。決定的（LLM を使わない）。

製品の PR は CHANGELOG.md を直接書き換えず、`changelog.d/<Issue 番号>.md` に `[Unreleased]` に入れる行を書く。
このスクリプトは断片を CHANGELOG.md の `## [Unreleased]` の種類（`### 追加` など）ごとに移し、移した断片を消す。

断片の形:
    ### 追加
    - 1 行目の項目
    - 2 行目の項目（続きの行は字下げして書く）

見出しのない断片の行は `### 変更` に入れる。同じ種類の中では、新しい断片（Issue 番号の大きいもの）を上にする
（CHANGELOG.md の既存の並びと同じく新しいものが上）。種類の見出しがなければ KIND_ORDER の順で作る。

使い方: python3 changelog_compile.py <製品のリポジトリのディレクトリ>
終了コード: 0 = 取りまとめた、3 = 断片がない、2 = CHANGELOG.md に `## [Unreleased]` がない
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

FRAGMENT_DIR = "changelog.d"
SKIP = {"README.md"}
KIND_ORDER = ["追加", "変更", "修正", "削除", "非推奨", "セキュリティ"]
DEFAULT_KIND = "変更"
UNRELEASED = "## [Unreleased]"


def fragment_key(p: Path):
    m = re.match(r"(\d+)", p.stem)
    return (0, int(m.group(1)), p.name) if m else (1, 0, p.name)


def fragments(repo: Path) -> list[Path]:
    d = repo / FRAGMENT_DIR
    if not d.is_dir():
        return []
    return sorted((p for p in d.glob("*.md") if p.name not in SKIP), key=fragment_key)


def parse_fragment(text: str) -> dict[str, list[str]]:
    """種類 → 項目の行（続きの行を含む、そのままの行）"""
    out: dict[str, list[str]] = {}
    kind = DEFAULT_KIND
    for line in text.splitlines():
        h = re.match(r"^#{2,4}\s+(.+?)\s*$", line)
        if h:
            kind = h.group(1)
            continue
        if not line.strip():
            continue
        out.setdefault(kind, []).append(line.rstrip())
    return out


def merge(changelog: str, entries: dict[str, list[str]]) -> str:
    lines = changelog.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == UNRELEASED)
    except StopIteration:
        raise ValueError("no [Unreleased]")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    section = lines[start + 1:end]
    # 種類ごとに分ける（見出しの前の行は head に残す）
    head, kinds, order = [], {}, []
    cur = None
    for l in section:
        h = re.match(r"^###\s+(.+?)\s*$", l)
        if h:
            cur = h.group(1)
            kinds[cur] = []
            order.append(cur)
        elif cur is None:
            head.append(l)
        else:
            kinds[cur].append(l)
    for k in entries:
        if k not in kinds:
            kinds[k] = []
            order.append(k)
    order.sort(key=lambda k: KIND_ORDER.index(k) if k in KIND_ORDER else len(KIND_ORDER))
    out_section = [""] + ([l for l in head if l.strip()] + [""] if any(l.strip() for l in head) else [])
    for k in order:
        body = [l for l in kinds[k] if l.strip()]
        new = entries.get(k, [])
        if not body and not new:
            continue
        out_section += [f"### {k}"] + new + body + [""]
    return "\n".join(lines[:start + 1] + out_section + lines[end:]) + ("\n" if changelog.endswith("\n") else "")


def compile_repo(repo: Path) -> int:
    frags = fragments(repo)
    if not frags:
        return 3
    entries: dict[str, list[str]] = {}
    for p in reversed(frags):   # 新しい断片を上に
        for k, ls in parse_fragment(p.read_text(encoding="utf-8")).items():
            entries.setdefault(k, []).extend(ls)
    cl = repo / "CHANGELOG.md"
    try:
        cl.write_text(merge(cl.read_text(encoding="utf-8"), entries), encoding="utf-8")
    except ValueError:
        return 2
    for p in frags:
        p.unlink()
    print(f"changelog: {len(frags)} fragments compiled")
    return 0


if __name__ == "__main__":
    sys.exit(compile_repo(Path(sys.argv[1] if len(sys.argv) > 1 else ".")))
