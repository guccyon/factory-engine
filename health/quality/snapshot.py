#!/usr/bin/env python3
"""内部品質の定期診断（definitions/quality.yaml、ADR 0017）。

製品のチェックアウトを測って、指標・スコア・ホットスポットを JSON に出す（measure）。
PR の base と head の結果を比べて、下限割れを Markdown で出す（floor。shadow なので終了コードは常に 0）。
LLM を使わない。同じ版のコードと同じ入力に同じ数値を返す。

使い方:
  snapshot.py measure --root . --config .factory-config/quality.yaml --definitions quality.yaml \
      [--clippy clippy.json] [--extra layer_violations=0] [--repo o/r] [--no-history] --out snapshot.json
  snapshot.py floor --definitions quality.yaml --base base.json --head head.json

依存: python3、PyYAML、lizard（複雑さと関数の長さ。なければその指標は null）、git（ホットスポット）
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import yaml

try:
    import lizard
except ImportError:  # 複雑さと長さは測らない（null）
    lizard = None

# lizard が読める言語（Gen1）。読めない言語は行数・重複・テストの数だけ測る
LIZARD_LANGS = {"rust", "typescript", "javascript", "python", "go", "kotlin", "swift", "java"}

TEST_FN = {
    "rust": re.compile(r"^\s*#\[(?:tokio::|sqlx::|actix_rt::)?test\b"),
    "typescript": re.compile(r"^\s*(?:test|it)(?:\.each\([^)]*\))?\s*\("),
    "javascript": re.compile(r"^\s*(?:test|it)(?:\.each\([^)]*\))?\s*\("),
    "dart": re.compile(r"^\s*(?:test|testWidgets)\s*\("),
    "python": re.compile(r"^\s*def test_"),
}
COMMENT = re.compile(r"^(//|/\*|\*|#(?!\[)|--)")
TRIVIAL = re.compile(r"^[\s{}()\[\];,.:]*$")
IMPORT = re.compile(r"^(use |import |export \* from|part |library |mod \w+;)")


def expand(root: str, patterns: list[str]) -> list[str]:
    out = set()
    for p in patterns or []:
        out.update(os.path.relpath(f, root) for f in glob.glob(os.path.join(root, p), recursive=True) if os.path.isfile(f))
    return sorted(out)


def code_lines(text: str) -> list[tuple[int, str]]:
    """空行・コメントだけの行を除き、前後の空白を落とした (行番号, 行)"""
    out = []
    for i, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if s and not COMMENT.match(s):
            out.append((i, re.sub(r"\s+", " ", s)))
    return out


def inline_test_start(text: str, marker: str | None) -> int | None:
    """製品コードのファイルの中のテスト（Rust の #[cfg(test)] など）が始まる行。そこから後ろはテストのコードとして扱う"""
    if not marker:
        return None
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith(marker):
            return i
    return None


def percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def duplicated_lines(files: dict[str, list[tuple[int, str]]], window: int) -> tuple[int, int]:
    """window 行の窓が 2 か所以上に出たら、その窓の行を重複に数える。import と記号だけの行は見ない"""
    seen: dict[str, list[tuple[str, int]]] = defaultdict(list)
    norm: dict[str, list[str]] = {}
    for path, lines in files.items():
        ls = [s for _, s in lines if not TRIVIAL.match(s) and not IMPORT.match(s)]
        norm[path] = ls
        for i in range(len(ls) - window + 1):
            h = hashlib.sha1("\n".join(ls[i:i + window]).encode()).hexdigest()
            seen[h].append((path, i))
    dup: dict[str, set[int]] = defaultdict(set)
    for places in seen.values():
        if len(places) > 1:
            for path, i in places:
                dup[path].update(range(i, i + window))
    total = sum(len(v) for v in norm.values())
    return sum(len(v) for v in dup.values()), total


def change_counts(root: str, days: int, now: datetime) -> Counter:
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        out = subprocess.run(["git", "-C", root, "log", f"--since={since}", "--name-only", "--format="],
                             check=True, capture_output=True, text=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Counter()
    return Counter(l for l in out.splitlines() if l.strip())


def clippy_warnings(path: str | None) -> int | None:
    """cargo clippy --message-format=json の出力から、警告（lint の code があるもの）を位置で重複を除いて数える"""
    if not path:
        return None
    keys = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = m.get("message") if m.get("reason") == "compiler-message" else None
            if not msg or msg.get("level") != "warning" or not (msg.get("code") or {}).get("code"):
                continue
            span = next((s for s in msg.get("spans", []) if s.get("is_primary")), {})
            keys.add((msg["code"]["code"], span.get("file_name"), span.get("line_start")))
    return len(keys)


def measure(root: str, config: dict, defs: dict, clippy: str | None, extra: dict, history: bool, now: datetime) -> dict:
    m = defs["metrics"]
    cx_limit, len_limit = m["complex_function_ratio"]["complexity_limit"], m["long_function_ratio"]["length_limit"]
    window = m["duplication_ratio"]["window_lines"]
    exclude = re.compile("|".join(config.get("exclude", [])) or r"(?!)")

    prod: dict[str, list[tuple[int, str]]] = {}
    funcs: list[dict] = []
    tests = 0
    measurable_langs, file_max_ccn = [], {}
    for lang, spec in (config.get("languages") or {}).items():
        test_re = TEST_FN.get(lang)
        src = [f for f in expand(root, spec.get("src")) if not exclude.search(f)]
        tst = [f for f in expand(root, spec.get("tests")) if not exclude.search(f) and f not in src]
        use_lizard = lizard is not None and lang in LIZARD_LANGS
        if use_lizard:
            measurable_langs.append(lang)
        for f in src + tst:
            text = open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
            cut = None if f in tst else inline_test_start(text, spec.get("inline_test_marker"))
            if test_re:
                tests += sum(1 for i, l in enumerate(text.splitlines(), 1) if test_re.match(l) and (f in tst or (cut and i >= cut)))
            if f in tst:
                continue
            prod[f] = [(i, s) for i, s in code_lines(text) if cut is None or i < cut]
            if use_lizard:
                info = lizard.analyze_file.analyze_source_code(f, text)
                fs = [fn for fn in info.function_list if cut is None or fn.start_line < cut]
                for fn in fs:
                    funcs.append({"file": f, "name": fn.name, "ccn": fn.cyclomatic_complexity, "nloc": fn.nloc})
                if fs:
                    file_max_ccn[f] = max(fn.cyclomatic_complexity for fn in fs)

    dup, norm_total = duplicated_lines(prod, window)
    lines = sum(len(v) for v in prod.values())
    kloc = lines / 1000 if lines else None
    ccn = [f["ccn"] for f in funcs]
    nloc = [f["nloc"] for f in funcs]
    lint = clippy_warnings(clippy)
    complex_fns = sum(1 for c in ccn if c > cx_limit) if funcs else None

    metrics = {
        "layer_violations": extra.get("layer_violations"),
        "lint_warnings_per_kloc": round(lint / kloc, 2) if lint is not None and kloc else None,
        "complex_function_ratio": round(complex_fns / len(funcs), 4) if funcs else None,
        "long_function_ratio": round(sum(1 for n in nloc if n > len_limit) / len(funcs), 4) if funcs else None,
        "duplication_ratio": round(dup / norm_total, 4) if norm_total else None,
        "tests_per_kloc": round(tests / kloc, 2) if kloc else None,
        "mutation_score": extra.get("mutation_score"),
    }
    hotspots = []
    if history:
        changes = change_counts(root, defs["hotspot"]["window_days"], now)
        hotspots = sorted(({"file": f, "changes": changes[f], "max_ccn": c, "score": changes[f] * c}
                           for f, c in file_max_ccn.items() if changes[f]), key=lambda h: (-h["score"], h["file"]))
        hotspots = hotspots[:defs["hotspot"]["top"]]
    return {
        "metrics": metrics,
        "score": score(metrics, defs),
        "counts": {"functions": len(funcs), "complex_functions": complex_fns, "tests": tests, "lint_warnings": lint,
                   "duplicated_lines": dup, "files": len(prod)},
        "distributions": {"complexity": dist(ccn), "function_length": dist(nloc)},
        "lines": lines,
        "measured_languages": sorted(measurable_langs),
        "hotspots": hotspots,
        "tool_versions": {"lizard": getattr(lizard, "version", None) if lizard else None, "python": sys.version.split()[0]},
    }


def dist(xs: list[float]) -> dict:
    return {"p50": percentile(xs, 0.5), "p90": percentile(xs, 0.9), "max": max(xs) if xs else None}


def score(metrics: dict, defs: dict) -> int | None:
    num = den = 0.0
    for k, c in defs["score"]["components"].items():
        v = metrics.get(k)
        if v is None:
            continue
        good, bad = c["good"], c["bad"]
        sub = 100 * min(1.0, max(0.0, (bad - v) / (bad - good)))   # higher の指標は good > bad なので同じ式で向きが逆になる
        num += c["weight"] * sub
        den += c["weight"]
    return round(num / den) if den else None


def floor(defs: dict, base: dict, head: dict) -> tuple[list[dict], str]:
    """下限の規則ごとに base と head を比べる。Gen1 は complex_functions だけ"""
    results = []
    for rid, rule in defs["floor"]["rules"].items():
        b, h = base["counts"].get(rid), head["counts"].get(rid)
        broken = b is not None and h is not None and h > b
        results.append({"rule": rid, "ja": rule["ja"], "base": b, "head": h, "broken": broken})
    lines = [f"### 内部品質の下限（{defs['floor']['mode']}。差し戻さない）", "", "| 規則 | base | head | 結果 |", "|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['ja']} | {r['base']} | {r['head']} | {'下限割れ' if r['broken'] else 'OK' if r['head'] is not None else '測れない'} |")
    lines += ["", f"スコア（参考）: base {base.get('score')} → head {head.get('score')}"]
    return results, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    me = sub.add_parser("measure")
    me.add_argument("--root", default=".")
    me.add_argument("--config", required=True)
    me.add_argument("--definitions", required=True)
    me.add_argument("--clippy")
    me.add_argument("--extra", action="append", default=[], help="key=value（CI が数えた値。数値）")
    me.add_argument("--repo")
    me.add_argument("--no-history", action="store_true")
    me.add_argument("--out")
    fl = sub.add_parser("floor")
    fl.add_argument("--definitions", required=True)
    fl.add_argument("--base", required=True)
    fl.add_argument("--head", required=True)
    fl.add_argument("--out")
    a = ap.parse_args(argv)
    defs = yaml.safe_load(open(a.definitions, encoding="utf-8"))
    if a.cmd == "measure":
        config = yaml.safe_load(open(a.config, encoding="utf-8")) or {}
        extra = {}
        for kv in a.extra:
            k, _, v = kv.partition("=")
            extra[k] = float(v) if "." in v else int(v)
        now = datetime.now(timezone.utc)
        snap = measure(a.root, config, defs, a.clippy, extra, not a.no_history, now)
        sha = subprocess.run(["git", "-C", a.root, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() or None
        snap = {"repo": a.repo, "sha": sha, "day": now.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d"), **snap}
        out = json.dumps(snap, ensure_ascii=False, indent=2)
    else:
        results, md = floor(defs, json.load(open(a.base)), json.load(open(a.head)))
        print(md)
        out = json.dumps({"mode": defs["floor"]["mode"], "results": results}, ensure_ascii=False, indent=2)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(out + "\n")
    elif a.cmd == "measure":
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
