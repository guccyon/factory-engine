#!/usr/bin/env python3
"""overlay のルールの検査（ADR 0013、P3 の Learn ループ）。

ルール変更 PR（change_type factory_rule）が工場自身のパイプラインを通るための、決定的な部分を受け持つ。
LLM を使わないので、同じルールと同じ差分に同じ結果を返す。

2 つの使い方:
  1. ルールの検査（既定）: 製品の .factory-config/overlay/rules/*.yaml を読み、
     - 形の検査: definitions/learning.yaml の rule.required、状態・段階・機密区分の値、eval ケースの正例・負例が 1 件以上ずつ、id の重複
     - eval ケースの実行: check.kind が diff_regex のルールは、eval ケースの差分に当てて expect（fire / silent）どおりかを見る。
       retired 以外の全ルールを毎回走らせるので、ほかのルールの退行もここで出る
     - rubric・episodic の段のルールは LLM が要るので、ここでは形だけを見て「工場で判定」と出す（判定は工場の inspect ジョブ）
  2. 発火（--base / --head）: PR の差分に shadow と adopted の diff_regex ルールを当て、当たった箇所を出す（gate.evaluated の gate=overlay）

eval ケースの差分は製品の .factory-config/evals/ からの相対パス（file:rules/OV-001/positive-1.diff など）。CI で外に取りに行かない。

使い方:
  core-gates/rule_eval.py [--repo .] [--json out.json] [--summary summary.md]
  core-gates/rule_eval.py --base origin/main [--head HEAD] [--repo .] [--json out.json]
  終了コード: 0 = 緑（発火の使い方では常に 0）、1 = 形か eval ケースが通らない、2 = 読めなかった

依存: python3、git（発火の使い方だけ）。YAML は PyYAML があればそれ、なければ ruby で読む
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

RULES_DIR = ".factory-config/overlay/rules"
EVALS_DIR = ".factory-config/evals"

# definitions/learning.yaml の rule と lifecycle に合わせる（製品の CI は Engine の固定した版のこのファイルだけを取るので、値はここに持つ）
REQUIRED = ["id", "title", "state", "knowledge_stage", "confidentiality", "lessons", "signature", "check", "eval_cases"]
STATES = ["candidate", "shadow", "adopted", "retired"]
STAGES = ["episodic", "rubric", "crystallized"]
CONFIDENTIALITY = ["product", "client", "public"]
CHECK_KINDS = {"episodic": ["episode"], "rubric": ["rubric"], "crystallized": ["diff_regex"]}
ID_RE = re.compile(r"^OV-\d{3,}$")
FIRING_STATES = ("shadow", "adopted")


def load_yaml(path: Path):
    try:
        import yaml  # type: ignore
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except ImportError:
        out = subprocess.run(["ruby", "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.load_file(ARGV[0]))", str(path)],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out)


def load_rules(repo: Path) -> tuple[list[dict], list[str]]:
    rules, errors = [], []
    d = repo / RULES_DIR
    if not d.is_dir():
        return [], []
    for p in sorted(d.glob("*.yaml")):
        try:
            r = load_yaml(p)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{p.name}: 読めない（{e}）")
            continue
        if not isinstance(r, dict):
            errors.append(f"{p.name}: ルールの形ではない")
            continue
        r["_file"] = p.name
        rules.append(r)
    return rules, errors


def digest(rule: dict) -> str:
    """ルールの中身（判定に効く部分）の digest。工場が LLM で判定した結果を、どの版のルールに対するものかに結びつける"""
    body = {k: rule.get(k) for k in ("id", "title", "knowledge_stage", "check", "eval_cases", "scope")}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def validate(rule: dict) -> list[str]:
    errs = [f"{k} がない" for k in REQUIRED if rule.get(k) in (None, "", [])]
    rid = rule.get("id") or ""
    if rid and not ID_RE.match(str(rid)):
        errs.append(f"id {rid} が OV-NNN の形ではない")
    if rid and rule.get("_file") and rule["_file"] != f"{rid}.yaml":
        errs.append(f"ファイル名が {rid}.yaml ではない")
    if rule.get("state") and rule["state"] not in STATES:
        errs.append(f"state {rule['state']} は {STATES} のどれでもない")
    stage = rule.get("knowledge_stage")
    if stage and stage not in STAGES:
        errs.append(f"knowledge_stage {stage} は {STAGES} のどれでもない")
    if rule.get("confidentiality") and rule["confidentiality"] not in CONFIDENTIALITY:
        errs.append(f"confidentiality {rule['confidentiality']} は {CONFIDENTIALITY} のどれでもない")
    check = rule.get("check") or {}
    if check and stage in CHECK_KINDS and check.get("kind") not in CHECK_KINDS[stage]:
        errs.append(f"段階 {stage} の check.kind は {CHECK_KINDS[stage]} のどれか（{check.get('kind')}）")
    if check.get("kind") == "diff_regex":
        try:
            re.compile(check.get("pattern") or "")
        except re.error as e:
            errs.append(f"check.pattern が正規表現として読めない（{e}）")
        if not check.get("pattern"):
            errs.append("check.pattern がない")
    cases = rule.get("eval_cases") or []
    for pol, expect in (("positive", "fire"), ("negative", "silent")):
        if not any(c.get("polarity") == pol for c in cases):
            errs.append(f"eval ケースの {pol} がない（正例・負例を 1 件以上ずつ同梱する）")
    for c in cases:
        if c.get("polarity") not in ("positive", "negative"):
            errs.append(f"eval ケース {c.get('id')}: polarity が positive / negative ではない")
        if c.get("expect") not in ("fire", "silent"):
            errs.append(f"eval ケース {c.get('id')}: expect が fire / silent ではない")
        if not str(c.get("diff_ref") or "").startswith("file:"):
            errs.append(f"eval ケース {c.get('id')}: diff_ref は file:（.factory-config/evals/ からの相対パス）")
    return errs


def added_lines(diff: str) -> list[tuple[str, int, str]]:
    """unified diff の追加行を (パス, head 側の行番号, 本文) で返す"""
    out, path, line, prev = [], None, 0, ""
    for raw in diff.splitlines():
        header = raw.startswith("+++ ") and prev.startswith("--- ")   # 追加行の「++ x」をファイルの見出しと取り違えない
        prev = raw
        if header:
            p = raw[4:].strip()
            path = None if p == "/dev/null" else re.sub(r"^b/", "", p)
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            line = int(m.group(1)) if m else 0
        elif path is None:
            continue
        elif raw.startswith("+"):
            out.append((path, line, raw[1:]))
            line += 1
        elif not raw.startswith("-") and not raw.startswith("\\"):
            line += 1
    return out


def in_scope(path: str, globs: list[str] | None) -> bool:
    return not globs or any(fnmatch.fnmatch(path, g) for g in globs)


def fire(rule: dict, diff: str) -> list[dict]:
    """diff_regex のルールを差分に当てる。当たった追加行の一覧（空なら発火しない）"""
    check = rule.get("check") or {}
    if check.get("kind") != "diff_regex":
        return []
    flags = re.IGNORECASE if "i" in (check.get("flags") or "") else 0
    pat = re.compile(check["pattern"], flags)
    globs = check.get("paths") or (rule.get("scope") or {}).get("paths")
    return [{"path": p, "line": n, "text": t.strip()[:200]} for p, n, t in added_lines(diff) if in_scope(p, globs) and pat.search(t)]


def read_case(repo: Path, ref: str) -> str:
    rel = ref.split(":", 1)[1]
    p = (repo / EVALS_DIR / rel).resolve()
    if not str(p).startswith(str((repo / EVALS_DIR).resolve())):
        raise ValueError(f"{ref} が evals/ の外を指している")
    return p.read_text(encoding="utf-8")


def evaluate(repo: Path) -> dict:
    rules, load_errors = load_rules(repo)
    seen: dict[str, str] = {}
    results = []
    for r in rules:
        errs = validate(r)
        rid = r.get("id")
        if rid in seen:
            errs.append(f"id {rid} が {seen[rid]} と重なっている")
        seen.setdefault(rid, r["_file"])
        cases = []
        deterministic = (r.get("check") or {}).get("kind") == "diff_regex"
        if r.get("state") != "retired" and not errs:
            for c in r.get("eval_cases") or []:
                if not deterministic:
                    cases.append({"id": c.get("id"), "expect": c.get("expect"), "result": "factory"})   # LLM の判定は工場の inspect ジョブ
                    continue
                try:
                    hits = fire(r, read_case(repo, c["diff_ref"]))
                except (OSError, ValueError) as e:
                    errs.append(f"eval ケース {c.get('id')}: 差分を読めない（{e}）")
                    continue
                got = "fire" if hits else "silent"
                cases.append({"id": c.get("id"), "expect": c.get("expect"), "result": got, "ok": got == c.get("expect"), "hits": hits[:5]})
        failed = [c["id"] for c in cases if c.get("ok") is False]
        results.append({"id": rid, "file": r["_file"], "state": r.get("state"), "knowledge_stage": r.get("knowledge_stage"),
                        "confidentiality": r.get("confidentiality"), "digest": digest(r), "errors": errs, "cases": cases,
                        "failed_cases": failed, "ok": not errs and not failed})
    ok = not load_errors and all(x["ok"] for x in results)
    return {"ok": ok, "load_errors": load_errors, "rules": results,
            "counts": {s: sum(1 for x in results if x["state"] == s) for s in STATES}}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def fire_on_pr(repo: Path, base: str, head: str) -> dict:
    """PR の差分に shadow と adopted の決定的なルールを当てる（発火の記録。PR は落とさない）"""
    rules, _ = load_rules(repo)
    diff = git(repo, "diff", "--no-color", "-U0", f"{base}...{head}")
    fires = []
    for r in rules:
        if r.get("state") in FIRING_STATES and not validate(r):
            hits = fire(r, diff)
            if hits:
                fires.append({"id": r["id"], "state": r["state"], "severity": r.get("severity", "should"), "digest": digest(r), "hits": hits[:20]})
    return {"base": base, "head": head, "gate": "overlay", "mode": "shadow", "fires": fires}


def summary_md(rep: dict) -> str:
    if "fires" in rep:
        lines = ["## overlay のルールの発火（shadow）", ""]
        if not rep["fires"]:
            return "\n".join(lines + ["- 発火なし"])
        for f in rep["fires"]:
            lines.append(f"- {f['id']}（{f['state']}）: " + "、".join(f"{h['path']}:{h['line']}" for h in f["hits"][:5]))
        return "\n".join(lines)
    c = rep["counts"]
    lines = ["## overlay のルールの検査", "", f"- 結果: {'緑' if rep['ok'] else '通らない'}",
             f"- ルール: candidate {c['candidate']} / shadow {c['shadow']} / adopted {c['adopted']} / retired {c['retired']}", ""]
    lines += [f"- 読めない: {e}" for e in rep["load_errors"]]
    if rep["rules"]:
        lines += ["| ルール | 状態 | 段階 | 機密区分 | eval ケース | 結果 |", "|---|---|---|---|---|---|"]
        for r in rep["rules"]:
            det = [x for x in r["cases"] if "ok" in x]
            cases = f"{sum(1 for x in det if x['ok'])}/{len(det)}" if det else ("工場で判定" if r["cases"] else "-")
            res = "OK" if r["ok"] else "；".join(r["errors"] + [f"{x} が期待と違う" for x in r["failed_cases"]])
            lines.append(f"| {r['id']} | {r['state']} | {r['knowledge_stage']} | {r['confidentiality']} | {cases} | {res} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--json")
    ap.add_argument("--summary")
    a = ap.parse_args()
    repo = Path(a.repo)
    try:
        rep = fire_on_pr(repo, a.base, a.head) if a.base else evaluate(repo)
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"読めなかった: {e}", file=sys.stderr)
        return 2
    if a.json:
        Path(a.json).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    md = summary_md(rep)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    print(md)
    return 0 if a.base or rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
