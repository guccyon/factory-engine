#!/usr/bin/env python3
"""知見の変更セットの番号を、既定ブランチと重ならないように付け直す（ADR 0024、2026-11-04）。LLM を使わない。

知見の PR を並列に書くと、どの PR も既定ブランチを見て「空いている次の番号」を取るので、K-0001・C-0001・D-0001 が
ぶつかって全部の PR が衝突した（rally #570・#571・#677・#694・#824）。知見は 1 件ずつ直列に流すが、すでにぶつかった PR と、
直列の外で入ってきたものは、衝突の解消の前にこのスクリプトで番号を付け直す。

やること:
  1. このブランチが足した（既定ブランチとの共通の祖先から見て A の）K-NNNN.yaml・cases/C-NNNN.md・committee/D-NNNN.yaml のうち、
     既定ブランチにも同じ番号があるものを選ぶ
  2. 既定ブランチとこのブランチの両方で使っている番号の最大の次から、古い番号の順に新しい番号を振る
  3. ファイルを git mv し、このブランチが足したファイルの中の参照（K-0001 など）を 1 回の置き換えで直す
     （既定ブランチのファイルには触らない。ぶつかった番号は、このブランチの中では自分の知見を指しているので）
     既存の知見を直す変更セット（amend）もあるので、このブランチが書き換えた既存のファイルは、このブランチが足した行だけを直す
     （rally #677 で、K-0001 の evidence に足した C-0003 が付け直されず、別の PR の事例を指した）
  4. --commit なら 1 つのコミットにする。INDEX.md は既定ブランチを取り込んだ後に knowledge_check.py --index で作り直す

使い方:
  core-gates/knowledge_renumber.py --base origin/main [--repo .] [--commit]
  出力: {"renamed": {"K-0001": "K-0003", ...}}（JSON 1 行）。終了コード: 0 = 済み（付け直しがなくても 0）、2 = git が使えなかった
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = ".factory-config/knowledge"
KINDS = {  # 接頭辞 → 置き場（ROOT からの相対）と拡張子
    "K": ("", ".yaml"),
    "C": ("cases/", ".md"),
    "D": ("committee/", ".yaml"),
}
NAME_RE = re.compile(r"^(?:(cases|committee)/)?([KCD])-(\d{4,})\.(yaml|md)$")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def ids_of(paths: list[str]) -> dict[str, str]:
    """置き場のパスの一覧から {id: パス}。置き場の決まり（K は直下、C は cases/、D は committee/）に合うものだけ"""
    out = {}
    for p in paths:
        if not p.startswith(ROOT + "/"):
            continue
        m = NAME_RE.match(p[len(ROOT) + 1:])
        if not m:
            continue
        sub, prefix, num, ext = m.groups()
        want_sub, want_ext = KINDS[prefix]
        if (sub + "/" if sub else "") == want_sub and "." + ext == want_ext:
            out[f"{prefix}-{num}"] = p
    return out


def plan(base_ids: set[str], added: dict[str, str]) -> dict[str, str]:
    """ぶつかった番号 → 新しい番号。番号の桁は 4 桁（足りなければ伸ばす）"""
    mapping: dict[str, str] = {}
    for prefix in KINDS:
        used = [int(i.split("-")[1]) for i in base_ids | set(added) if i.startswith(prefix + "-")]
        nxt = max(used, default=0) + 1
        for old in sorted(i for i in added if i.startswith(prefix + "-") and i in base_ids):
            mapping[old] = f"{prefix}-{nxt:04d}"
            nxt += 1
    return mapping


def rewrite(text: str, mapping: dict[str, str]) -> str:
    if not mapping:
        return text
    pat = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)) + r")(?!\d)")
    return pat.sub(lambda m: mapping[m.group(1)], text)


def rewrite_added_lines(before: str, after: str, mapping: dict[str, str]) -> str:
    """after のうち before になかった行（このブランチが足した・書き換えた行）だけを置き換える"""
    a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
    out = []
    for op, _, _, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        chunk = b[j1:j2]
        out.extend(chunk if op == "equal" else [rewrite(line, mapping) for line in chunk])
    return "".join(out)


def renumber(repo: Path, base: str, commit: bool = False) -> dict[str, str]:
    mb = git(repo, "merge-base", base, "HEAD").strip()
    base_ids = set(ids_of(git(repo, "ls-tree", "-r", "--name-only", base, "--", ROOT).split()))
    added_paths = git(repo, "diff", "--name-only", "--diff-filter=A", mb, "HEAD", "--", ROOT).split()
    added = ids_of(added_paths)
    mapping = plan(base_ids, added)
    if not mapping:
        return {}
    moved = {}
    for old, new in mapping.items():
        src = added[old]
        moved[src] = src.replace(f"{old}.", f"{new}.")
        git(repo, "mv", src, moved[src])
    for p in added_paths:
        if p.endswith("/INDEX.md"):
            continue   # 取り込みの後に作り直す
        p = moved.get(p, p)
        f = repo / p
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8")
        new = rewrite(text, mapping)
        if new != text:
            f.write_text(new, encoding="utf-8")
            git(repo, "add", p)
    for p in git(repo, "diff", "--name-only", "--diff-filter=M", mb, "HEAD", "--", ROOT).split():
        if p.endswith("/INDEX.md"):
            continue
        f = repo / p
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8")
        new = rewrite_added_lines(git(repo, "show", f"{mb}:{p}"), text, mapping)
        if new != text:
            f.write_text(new, encoding="utf-8")
            git(repo, "add", p)
    if commit:
        pairs = "、".join(f"{o} → {n}" for o, n in mapping.items())
        git(repo, "commit", "-q", "-m", f"知見の番号を既定ブランチと重ならないように付け直す（{pairs}）\n\ncore-gates/knowledge_renumber.py で機械的に付け直した")
    return mapping


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", required=True)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args(argv)
    try:
        mapping = renumber(Path(a.repo), a.base, a.commit)
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"git が使えなかった: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"renamed": mapping}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
