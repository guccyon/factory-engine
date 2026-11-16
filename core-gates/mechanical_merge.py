"""続きのジョブの衝突を、エージェントを使わずに機械的に解く（ADR 0016・0017 の衝突の解消。工程の改善案 #504・#524、2026-11-16 オーナー）。決定的（LLM を使わない）。

衝突したファイルがすべて、製品が宣言した「機械的に解いてよいパス」（`.factory-config/continuation.yaml` の `conflict.mechanical.paths`。
宣言がなければ CHANGELOG.md だけ）に当たるときに、両方の変更を残して解く（`git merge-file --union`）。

- CHANGELOG.md: 今までどおり、両方の行を残す（追記どうしの衝突。2026-11-02 に 1 日 345 回・$1,420 の解消が走った）
- それ以外（知見の K-*.yaml の solutions の末尾、道具の README の節の末尾など）: 衝突した箇所が「両方が同じ場所に書き足しただけ」
  （共通の祖先の側が空）のときだけ解く。どちらかが既存の行を書き換えていれば解かない（エージェントに回す）
- YAML（.yaml・.yml）は、解いた結果が読めて、最上位の形（対応表か列か）が変わらないときだけ解く
- 1 つでも解けないファイルがあれば、何も書き換えずに終わる（中途半端に解かない）

11/3〜11/10 の 1 週間に、知見の YAML と道具の README の衝突の解消が約 150 件・約 $27（費用の 2 割強）走っていた。
どれも、委員会の議決から同時に起きたチケットが同じファイルの同じ末尾に 1 項目ずつ書き足したもの。

宣言の書き方（製品の `.factory-config/continuation.yaml`）:

    conflict:
      mechanical:
        paths: ["CHANGELOG.md", "changelog.d/", ".factory-config/knowledge/K-*.yaml", "tools/*/README.md"]

末尾が / のものはディレクトリの下のすべて。* を含むものはパターン（fnmatch。* は / もまたぐ）。

使い方: python3 mechanical_merge.py --repo <製品のディレクトリ> <衝突したファイル>...
終了コード: 0 = すべて解いて git add した（標準出力に解いたファイルを空白区切りで出す）、
           3 = 宣言したパスでないファイルがある（何もしない）、1 = 宣言したパスだが安全に解けない（何もしない）
"""
from __future__ import annotations

import fnmatch
import subprocess
import sys
import tempfile
from pathlib import Path

DECLARATION = ".factory-config/continuation.yaml"
DEFAULT_PATHS = ["CHANGELOG.md"]
LEGACY_UNION = {"CHANGELOG.md"}   # 書き足しの判定をせず両方の行を残す（今までの扱い）


def declared_paths(repo: Path) -> list[str]:
    p = repo / DECLARATION
    if not p.exists():
        return list(DEFAULT_PATHS)
    try:
        import yaml
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        paths = ((d.get("conflict") or {}).get("mechanical") or {}).get("paths")
    except Exception:  # noqa: BLE001  読めなければ今までの既定
        return list(DEFAULT_PATHS)
    return [str(x) for x in paths] if isinstance(paths, list) and paths else list(DEFAULT_PATHS)


def matches(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if pat.endswith("/") and path.startswith(pat):
            return True
        if any(c in pat for c in "*?[") and fnmatch.fnmatchcase(path, pat):
            return True
        if path == pat:
            return True
    return False


def _stage(repo: Path, n: int, path: str) -> str:
    r = subprocess.run(["git", "show", f":{n}:{path}"], cwd=repo, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _merge_file(ours: str, base: str, theirs: str, *opts: str) -> str:
    with tempfile.TemporaryDirectory() as d:
        o, b, t = Path(d, "ours"), Path(d, "base"), Path(d, "theirs")
        o.write_text(ours, encoding="utf-8")
        b.write_text(base, encoding="utf-8")
        t.write_text(theirs, encoding="utf-8")
        r = subprocess.run(["git", "merge-file", "-p", *opts, "-L", "ours", "-L", "base", "-L", "theirs", str(o), str(b), str(t)],
                           capture_output=True, text=True)
        if r.returncode < 0:
            raise RuntimeError(r.stderr)
        return r.stdout


def appends_only(ours: str, base: str, theirs: str) -> bool:
    """衝突した箇所がすべて、共通の祖先の側が空（両方が同じ場所に書き足しただけ）か"""
    out = _merge_file(ours, base, theirs, "--diff3")
    state, base_lines, hunks = None, [], 0
    for line in out.splitlines():
        if line.startswith("<<<<<<< "):
            state, base_lines = "ours", []
            hunks += 1
        elif line.startswith("||||||| ") and state == "ours":
            state = "base"
        elif line == "=======" and state in ("ours", "base"):
            if state == "base" and any(l.strip() for l in base_lines):
                return False
            state = "theirs"
        elif line.startswith(">>>>>>> ") and state == "theirs":
            state = None
        elif state == "base":
            base_lines.append(line)
    return hunks > 0 and state is None


def yaml_ok(merged: str, ours: str) -> bool:
    import yaml
    try:
        m, o = yaml.safe_load(merged), yaml.safe_load(ours)
    except yaml.YAMLError:
        return False
    return type(m) is type(o)


def resolve(repo: Path, path: str) -> str | None:
    """1 つのファイルを解いた中身。安全に解けなければ None"""
    base, ours, theirs = _stage(repo, 1, path), _stage(repo, 2, path), _stage(repo, 3, path)
    if not ours or not theirs:
        return None   # 片方で消した・足した（追加と追加の衝突など）は機械的に解かない
    if path not in LEGACY_UNION and not appends_only(ours, base, theirs):
        return None
    merged = _merge_file(ours, base, theirs, "--union")
    if path.endswith((".yaml", ".yml")) and not yaml_ok(merged, ours):
        return None
    return merged


def main(argv: list[str]) -> int:
    args = list(argv)
    repo = Path(".")
    if "--repo" in args:
        i = args.index("--repo")
        repo = Path(args[i + 1])
        del args[i:i + 2]
    files = [f for f in args if f]
    if not files:
        return 3
    patterns = declared_paths(repo)
    if not all(matches(f, patterns) for f in files):
        return 3
    out = {}
    for f in files:
        merged = resolve(repo, f)
        if merged is None:
            print(f"mechanical_merge: {f} は書き足しどうしの衝突でないので、機械的に解かない", file=sys.stderr)
            return 1
        out[f] = merged
    for f, text in out.items():
        (repo / f).write_text(text, encoding="utf-8")
        subprocess.run(["git", "add", "--", f], cwd=repo, check=True)
    print(" ".join(files))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
