"""共有ファイルの取りまとめ（ADR 0051 の 2）。決定的（LLM を使わない）。ADR 0027 の CHANGELOG の取りまとめを一般化したもの。

製品は `.factory-config/aggregates.yaml` に、断片で更新するファイル（取りまとめるファイル）を宣言する。宣言がなければ
CHANGELOG.md だけ（今までの changelog.d/ の決まり）を取りまとめる。

    aggregates:
      - path: CHANGELOG.md
        fragments: changelog.d/
        compile: changelog          # 種類ごとに [Unreleased] に移す（changelog_compile.py）
      - path: apps/backend/README.md
        fragments: apps/backend/readme.d/
        compile: append_section     # 断片の「## 見出し」ごとに、同じ見出しの節の末尾に足す。見出しのない行は default_section か末尾
        default_section: 機能
      - path: tools/x/README.md
        fragments: tools/x/list.d/
        compile: sorted_list        # 印（<!-- aggregate:start --> 〜 <!-- aggregate:end -->）の間の行に足し、重複を消して並べる
      - path: apps/admin-web/src/api/schema.ts
        compile: generate           # 断片は使わない。取りまとめのときに正本から作り直す
        command: npm --prefix apps/admin-web run gen:api

断片は 1 PR 1 ファイル（`<Issue 番号>.md`）。取りまとめた断片は消す。断片の置き場の README.md は断片として扱わない。

使い方:
  python3 aggregate_compile.py <製品のディレクトリ>                 # 宣言したファイルをすべて取りまとめる
  python3 aggregate_compile.py <製品のディレクトリ> --view <path>   # ファイルとまだ取りまとめていない断片を合わせた中身を出す（書き換えない）
  python3 aggregate_compile.py <製品のディレクトリ> --list          # 宣言（と既定）を JSON で出す
終了コード: 0 = 1 つ以上取りまとめた、3 = 取りまとめるものがない、2 = 宣言かファイルの形が合わない
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import changelog_compile

DECLARATION = ".factory-config/aggregates.yaml"
KINDS = {"changelog", "append_section", "sorted_list", "generate"}
DEFAULT = [{"path": "CHANGELOG.md", "fragments": "changelog.d/", "compile": "changelog"}]
SKIP = {"README.md"}
START, END = "<!-- aggregate:start -->", "<!-- aggregate:end -->"


class Invalid(ValueError):
    pass


def load(repo: Path) -> list[dict]:
    """宣言を読む。なければ CHANGELOG だけ（今までの決まり）。形が合わなければ Invalid"""
    p = repo / DECLARATION
    if not p.exists():
        return [dict(d) for d in DEFAULT]
    import yaml
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out = []
    for a in data.get("aggregates") or []:
        if not isinstance(a, dict) or not a.get("path") or a.get("compile") not in KINDS:
            raise Invalid(f"宣言の形が合わない: {a!r}")
        if a["compile"] != "generate" and not a.get("fragments"):
            raise Invalid(f"{a['path']}: fragments（断片の置き場）がない")
        if a["compile"] == "generate" and not a.get("command"):
            raise Invalid(f"{a['path']}: generate には command が要る")
        for k in ("path", "fragments"):
            if a.get(k) and (Path(a[k]).is_absolute() or ".." in Path(a[k]).parts):
                raise Invalid(f"{a[k]}: 製品の中の相対パスで書く")
        out.append(a)
    return out


def fragments(repo: Path, a: dict) -> list[Path]:
    d = repo / (a.get("fragments") or "")
    if not a.get("fragments") or not d.is_dir():
        return []
    return sorted((p for p in d.glob("*.md") if p.name not in SKIP), key=changelog_compile.fragment_key)


def _sections(text: str) -> dict[str, list[str]]:
    """断片を「見出し → 行」に分ける。見出しの前の行は '' に入れる"""
    out: dict[str, list[str]] = {}
    cur = ""
    for line in text.splitlines():
        h = re.match(r"^#{2,4}\s+(.+?)\s*$", line)
        if h:
            cur = h.group(1)
            continue
        if line.strip():
            out.setdefault(cur, []).append(line.rstrip())
    return out


def _heading(line: str) -> str | None:
    m = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
    return m.group(1) if m else None


def append_section(target: str, frags: list[str], default_section: str | None = None) -> str:
    """断片の見出しごとに、同じ見出しの節の末尾（次の同じか上の見出しの前）に行を足す。古い断片から順に足す"""
    lines = target.splitlines()
    for text in frags:
        for head, new in _sections(text).items():
            head = head or default_section or ""
            idx = next((i for i, l in enumerate(lines) if _heading(l) == head), None) if head else None
            if idx is None:
                if head:
                    lines += ["", f"## {head}", ""]
                lines += new
                continue
            level = len(re.match(r"^(#+)", lines[idx]).group(1))
            end = next((j for j in range(idx + 1, len(lines)) if (m := re.match(r"^(#+)\s", lines[j])) and len(m.group(1)) <= level), len(lines))
            while end > idx + 1 and not lines[end - 1].strip():
                end -= 1
            lines[end:end] = new
    return "\n".join(lines) + ("\n" if target.endswith("\n") or not target else "")


def sorted_list(target: str, frags: list[str]) -> str:
    """印の間の行に断片の行を足し、重複を消して並べる。印がなければ Invalid"""
    lines = target.splitlines()
    try:
        s = next(i for i, l in enumerate(lines) if l.strip() == START)
        e = next(i for i in range(s + 1, len(lines)) if lines[i].strip() == END)
    except StopIteration:
        raise Invalid(f"{START} と {END} の印がない")
    items = [l for l in lines[s + 1:e] if l.strip()]
    for text in frags:
        items += [l.rstrip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    merged = sorted(dict.fromkeys(items))
    return "\n".join(lines[:s + 1] + merged + lines[e:]) + ("\n" if target.endswith("\n") else "")


def view(repo: Path, a: dict) -> str:
    """ファイルとまだ取りまとめていない断片を合わせた中身（書き換えない）。製品の検査が一覧を照らすときに使う"""
    target = (repo / a["path"]).read_text(encoding="utf-8") if (repo / a["path"]).exists() else ""
    frags = [p.read_text(encoding="utf-8") for p in fragments(repo, a)]
    if not frags or a["compile"] == "generate":
        return target
    if a["compile"] == "changelog":
        entries: dict[str, list[str]] = {}
        for t in reversed(frags):
            for k, ls in changelog_compile.parse_fragment(t).items():
                entries.setdefault(k, []).extend(ls)
        try:
            return changelog_compile.merge(target, entries)
        except ValueError:
            raise Invalid("CHANGELOG.md に [Unreleased] がない")
    if a["compile"] == "append_section":
        return append_section(target, frags, a.get("default_section"))
    return sorted_list(target, frags)


def compile_one(repo: Path, a: dict) -> bool:
    """1 つのファイルを取りまとめる。取りまとめたら True"""
    if a["compile"] == "generate":
        r = subprocess.run(a["command"], shell=True, cwd=repo, capture_output=True, text=True, timeout=600)  # noqa: S602  製品の宣言（CI と同じ信頼の範囲）
        if r.returncode != 0:
            raise Invalid(f"{a['path']}: 生成に失敗した（{r.returncode}）: {r.stderr[-500:]}")
        changed = subprocess.run(["git", "status", "--porcelain", "--", a["path"]], cwd=repo, capture_output=True, text=True).stdout.strip()
        return bool(changed)
    frags = fragments(repo, a)
    if not frags:
        return False
    out = view(repo, a)
    p = repo / a["path"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(out, encoding="utf-8")
    for f in frags:
        f.unlink()
    print(f"aggregate: {a['path']} に断片 {len(frags)} 件")
    return True


def compile_repo(repo: Path) -> int:
    try:
        decl = load(repo)
    except Invalid as e:
        print(f"aggregate: {e}", file=sys.stderr)
        return 2
    done, bad = 0, 0
    for a in decl:
        try:
            done += compile_one(repo, a)
        except Invalid as e:
            print(f"aggregate: {e}", file=sys.stderr)
            bad += 1
    return 0 if done else 2 if bad else 3


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    repo = Path(args[0] if args else ".")
    if "--list" in argv:
        print(json.dumps(load(repo), ensure_ascii=False))
        return 0
    if "--view" in argv:
        path = argv[argv.index("--view") + 1]
        a = next((x for x in load(repo) if x["path"] == path), None)
        if not a:
            print(f"aggregate: {path} は宣言されていない", file=sys.stderr)
            return 2
        sys.stdout.write(view(repo, a))
        return 0
    return compile_repo(repo)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
