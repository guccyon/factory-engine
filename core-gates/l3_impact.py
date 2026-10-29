#!/usr/bin/env python3
"""L3 の影響範囲の実測（gates.yaml の L3「拡張性・結合度」、P2）。

PR の base と head の差分から、変更が及んだファイルの数とコンテキストの数を数える。
決定的（LLM を使わない）。同じ差分と同じ設定なら、何度走らせても同じ結果と同じ digest を返す（L3 の Exit 条件「同じ PR に同じ判定」）。

コンテキストの割り当て:
  - 語彙は製品の specs/context-map.yaml のコンテキスト id、specs/glossary.yaml の用語 id（その用語のコンテキスト）、
    製品の .factory-config/l3.yaml の keywords（語 → コンテキスト）
  - l3.yaml の paths（glob → コンテキスト、または shared）に当たるファイルはそれに従う。名前から読めないファイルの例外に使う
  - ファイルのパスを語に分け（/ _ - . と camelCase の区切り）、左から最長一致で語彙を当てる
  - ディレクトリの名前で当たれば、そのコンテキストの持ち物とみる（domain/src/entitlement/live_lesson_quota.rs は entitlement だけ）。
    ディレクトリで当たらなければファイル名で当てる
  - l3.yaml の weak の語（例: 生徒・コーチのように至る所に出る語）は、ファイル名の中で、ほかの語が当たらなかったときだけ使う
  - 複数形は落として比べる（bookings と booking、coaches と coach は同じ語）
  - マイグレーション（*.sql）は head の本文に出る表の名前も同じように当てる
  - どれにも当たらないファイルは「共有」（shared）として数え、コンテキストの数には入れない
  - テスト（l3.yaml の tests の glob）は数えるが、コンテキストの数は本体のファイルだけで出す（テストのコンテキストは test_contexts に別に出す）

発火条件（gates.yaml の L3.trigger）: 契約の差分（specs/contracts/）、スキーマの差分（マイグレーション）、新しいモジュール（l3.yaml の module_roots の下に新しいディレクトリ）。
定期健診では --force で、変更シナリオの実測では --scenario で、発火条件を見ずに測る。

判定（P2 は shadow。PR は落とさない）:
  - --scenario cs-NNN を付けると、specs/change-scenarios.yaml の expected_contexts と実測を比べる。予想になかったコンテキストがあれば flag
  - シナリオがなければ、コンテキストの数が l3.yaml の max_contexts を超えたら flag

使い方:
  core-gates/l3_impact.py --base origin/main [--head HEAD] [--repo .] [--scenario cs-001] [--force] [--config l3.yaml] [--specs-ref origin/main] [--json out.json] [--summary summary.md]
  終了コード: 0 = pass（または発火しない）、1 = flag、2 = 読めなかった

依存: python3、git。YAML は PyYAML があればそれ、なければ ruby で読む
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys

SHARED = "shared"
DEFAULT_CONFIG = {
    "keywords": {},
    "weak": [],
    "ignore": [],
    "paths": {},
    "tests": ["*/tests/*", "*/test/*", "*_test.*", "*.test.*", "*.spec.*"],
    "module_roots": [],
    "max_contexts": 3,
}
SQL_TABLE = re.compile(
    r"\b(?:create\s+table(?:\s+if\s+not\s+exists)?|alter\s+table(?:\s+if\s+exists)?|drop\s+table(?:\s+if\s+exists)?"
    r"|insert\s+into|update|delete\s+from|on)\s+(?:only\s+)?\"?([a-z_][a-z0-9_]*)\"?",
    re.IGNORECASE,
)


def parse_yaml(text: str) -> dict:
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except ImportError:
        r = subprocess.run(["ruby", "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.safe_load(STDIN.read, aliases: true))"],
                           input=text, capture_output=True, text=True, check=True)
        return json.loads(r.stdout) or {}


def git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def git_show(repo: str, ref: str, path: str) -> str | None:
    r = subprocess.run(["git", "-C", repo, "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def singular(word: str) -> str:
    """複数形を落とす（bookings と booking、coaches と coach を同じ語にする）。語彙とパスの両方に同じ規則をかけるので、多少粗くてよい"""
    if len(word) <= 3 or not word.endswith("s") or word.endswith("ss"):
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "xes", "sses")):
        return word[:-2]
    return word[:-1]


def tokens(text: str) -> list[str]:
    """パスや表の名前を小文字の語に分ける。camelCase も分け、複数形の s を落とす"""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return [singular(t) for t in re.split(r"[^a-zA-Z0-9]+", text.lower()) if t]


class Vocabulary:
    def __init__(self, context_ids: list[str], terms: dict[str, list[str]], keywords: dict[str, list[str]], weak: list[str]):
        self.phrases: dict[tuple, frozenset] = {}
        for cid in context_ids:
            self._add(cid, [cid])
        for word, cids in {**terms, **keywords}.items():
            self._add(word, cids)
        self.weak = {tuple(tokens(w)) for w in weak}
        self.longest = max((len(p) for p in self.phrases), default=1)

    def _add(self, word: str, cids: list[str]):
        key = tuple(tokens(word))
        if key:
            self.phrases[key] = self.phrases.get(key, frozenset()) | frozenset(cids)

    def match(self, toks: list[str]) -> tuple[set, set]:
        """(強い語で当たったコンテキスト, weak の語で当たったコンテキスト)"""
        strong, weak = set(), set()
        i = 0
        while i < len(toks):
            for n in range(min(self.longest, len(toks) - i), 0, -1):
                key = tuple(toks[i:i + n])
                if key in self.phrases:
                    (weak if key in self.weak else strong).update(self.phrases[key])
                    i += n
                    break
            else:
                i += 1
        return strong, weak


def load_config(repo: str, ref: str, config_path: str | None = None) -> tuple[Vocabulary, dict, dict]:
    cmap = parse_yaml(git_show(repo, ref, "specs/context-map.yaml") or "")
    context_ids = [c["id"] for c in cmap.get("contexts", [])]
    if not context_ids:
        raise ValueError("specs/context-map.yaml にコンテキストがない")
    glossary = parse_yaml(git_show(repo, ref, "specs/glossary.yaml") or "")
    terms = {}
    for t in glossary.get("terms", []):
        cids = t.get("contexts") or ([t["context"]] if t.get("context") else [])
        if cids:
            terms[t["id"]] = cids
    text = open(config_path).read() if config_path else git_show(repo, ref, ".factory-config/l3.yaml")
    config = {**DEFAULT_CONFIG, **parse_yaml(text or "")}
    keywords = {w: (c if isinstance(c, list) else [c]) for w, c in (config.get("keywords") or {}).items()}
    unknown = sorted({c for cs in [*terms.values(), *keywords.values()] for c in cs} - set(context_ids))
    if unknown:
        raise ValueError(f"context-map にないコンテキスト: {', '.join(unknown)}")
    scenarios = parse_yaml(git_show(repo, ref, "specs/change-scenarios.yaml") or "")
    return Vocabulary(context_ids, terms, keywords, config.get("weak") or []), config, scenarios


def changed_files(repo: str, base: str, head: str) -> list[tuple[str, str]]:
    """(状態, パス)。base と head の分岐点からの差分。名前の変更は新しいパスで数える"""
    out = git(repo, "diff", "--name-status", "-M", f"{base}...{head}")
    files = []
    for line in out.splitlines():
        parts = line.split("\t")
        files.append((parts[0][0], parts[-1]))
    return sorted(set(files), key=lambda f: f[1])


def classify(repo: str, head: str, path: str, status: str, vocab: Vocabulary, paths: dict) -> list[str]:
    for glob_, cids in paths.items():
        if fnmatch.fnmatch(path, glob_):
            return sorted(cids if isinstance(cids, list) else [cids])
    # ディレクトリで当たればその持ち物。ディレクトリの名前（student-app など）では weak の語を使わない
    directory, _, name = path.rpartition("/")
    owner, _ = vocab.match(tokens(directory))
    if owner:
        return sorted(owner)
    strong, weak = vocab.match(tokens(name.rsplit(".", 1)[0]))
    if path.endswith(".sql") and status != "D":
        body = git_show(repo, head, path) or ""
        body = re.sub(r"--[^\n]*", "", body)
        for table in SQL_TABLE.findall(body):
            s, w = vocab.match(tokens(table))
            strong |= s
            weak |= w
    found = strong or weak
    return sorted(found) if found else [SHARED]


def count(by_file: dict) -> dict:
    by_context: dict[str, int] = {}
    for cids in by_file.values():
        for c in cids:
            by_context[c] = by_context.get(c, 0) + 1
    return dict(sorted(by_context.items()))


def new_modules(repo: str, base: str, head: str, roots: list[str], files: list[tuple[str, str]]) -> list[str]:
    mods = set()
    for status, path in files:
        if status != "A":
            continue
        for root in roots:
            root = root.rstrip("/") + "/"
            if not path.startswith(root):
                continue
            rest = path[len(root):]
            if "/" not in rest:
                continue
            mod = root + rest.split("/", 1)[0]
            if git_show(repo, base, mod) is None:
                mods.add(mod)
    return sorted(mods)


def measure(repo: str, base: str, head: str, scenario_id: str | None = None, force: bool = False,
            config_path: str | None = None, specs_ref: str | None = None) -> dict:
    vocab, config, scenarios = load_config(repo, specs_ref or head, config_path)
    ignore = config.get("ignore") or []
    files = [f for f in changed_files(repo, base, head) if not any(fnmatch.fnmatch(f[1], g) for g in ignore)]

    reasons = []
    if any(p.startswith("specs/contracts/") for _, p in files):
        reasons.append("contract_diff")
    if any(p.endswith(".sql") and "/migrations/" in p for _, p in files):
        reasons.append("schema_diff")
    mods = new_modules(repo, base, head, config.get("module_roots") or [], files)
    if mods:
        reasons.append("new_module")

    if scenario_id:
        reasons.append("scenario")
    paths, tests = config.get("paths") or {}, config.get("tests") or []
    by_file = {p: classify(repo, head, p, s, vocab, paths) for s, p in files}
    is_test = {p: any(fnmatch.fnmatch(p, g) for g in tests) for p in by_file}
    by_context = count({p: c for p, c in by_file.items() if not is_test[p]})
    test_by_context = count({p: c for p, c in by_file.items() if is_test[p]})
    contexts = sorted(c for c in by_context if c != SHARED)

    result = {
        "gate": "L3",
        "base": git(repo, "merge-base", base, head).strip(),
        "head": git(repo, "rev-parse", head).strip(),
        "fires": bool(reasons) or force,
        "trigger": reasons if reasons else (["forced"] if force else []),
        "new_modules": mods,
        "files": len(files),
        "test_files": sum(is_test.values()),
        "contexts": contexts,
        "context_count": len(contexts),
        "files_by_context": by_context,
        "test_contexts": sorted(c for c in test_by_context if c != SHARED),
        "test_files_by_context": test_by_context,
        "by_file": by_file,
    }

    verdict, notes = "pass", []
    if scenario_id:
        sc = next((s for s in scenarios.get("scenarios", []) if s.get("id") == scenario_id), None)
        if sc is None:
            raise ValueError(f"change-scenarios.yaml に {scenario_id} がない")
        expected = sorted(sc.get("expected_contexts") or [])
        result["scenario"] = {
            "id": scenario_id,
            "expected": expected,
            "unexpected": sorted(set(contexts) - set(expected)),
            "not_touched": sorted(set(expected) - set(contexts)),
        }
        if result["scenario"]["unexpected"]:
            verdict = "flag"
            notes.append("予想になかったコンテキストに及んだ: " + ", ".join(result["scenario"]["unexpected"]))
    elif len(contexts) > int(config.get("max_contexts", 3)):
        verdict = "flag"
        notes.append(f"コンテキストの数 {len(contexts)} が上限 {config.get('max_contexts')} を超えた")
    if not result["fires"]:
        verdict, notes = "skip", ["発火条件（契約・スキーマの差分、新しいモジュール）に当たらない"]
    result["verdict"] = verdict
    result["notes"] = notes
    result["digest"] = digest(result)
    return result


def digest(result: dict) -> str:
    """判定に効く部分だけの指紋。同じ差分と設定なら同じ値になる"""
    keys = ("base", "head", "fires", "trigger", "files", "test_files", "contexts", "files_by_context",
            "test_files_by_context", "by_file", "scenario", "verdict")
    body = json.dumps({k: result.get(k) for k in keys}, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()[:16]


def summary_md(r: dict) -> str:
    lines = [f"## L3 影響範囲（shadow）: {r['verdict']}", ""]
    lines.append(f"- 発火: {'はい（' + ', '.join(r['trigger']) + '）' if r['fires'] else 'いいえ'}")
    lines.append(f"- ファイル {r['files']}（うちテスト {r['test_files']}）、コンテキスト {r['context_count']}（{', '.join(r['contexts']) or 'なし'}）")
    if r["test_contexts"]:
        lines.append(f"- テストが及んだコンテキスト: {', '.join(r['test_contexts'])}")
    if "scenario" in r:
        s = r["scenario"]
        lines.append(f"- シナリオ {s['id']}: 予想 {', '.join(s['expected'])} / 予想外 {', '.join(s['unexpected']) or 'なし'} / 及ばなかった {', '.join(s['not_touched']) or 'なし'}")
    for n in r["notes"]:
        lines.append(f"- {n}")
    lines.append(f"- digest: `{r['digest']}`")
    lines += ["", "| コンテキスト | 本体のファイル | テストのファイル |", "|---|---|---|"]
    for c in sorted(set(r["files_by_context"]) | set(r["test_files_by_context"])):
        lines.append(f"| {c} | {r['files_by_context'].get(c, 0)} | {r['test_files_by_context'].get(c, 0)} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--scenario")
    ap.add_argument("--force", action="store_true", help="発火条件を見ずに測る（定期健診）")
    ap.add_argument("--config", help="製品の .factory-config/l3.yaml の代わりに読むファイル（試すとき）")
    ap.add_argument("--specs-ref", help="コンテキスト・用語・シナリオ・設定を読む版（既定は head。過去の PR を今の語彙で測り直すとき）")
    ap.add_argument("--json")
    ap.add_argument("--summary")
    a = ap.parse_args()
    try:
        r = measure(a.repo, a.base, a.head, a.scenario, a.force, a.config, a.specs_ref)
    except (subprocess.CalledProcessError, ValueError, KeyError) as e:
        print(f"L3: 読めなかった: {e}", file=sys.stderr)
        return 2
    if a.json:
        with open(a.json, "w") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
    md = summary_md(r)
    if a.summary:
        with open(a.summary, "a") as f:
            f.write(md)
    print(md)
    return 1 if r["verdict"] == "flag" else 0


if __name__ == "__main__":
    sys.exit(main())
