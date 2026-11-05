#!/usr/bin/env python3
"""PR ごとの診断（依存・静的解析・秘密情報）の結果をまとめる（ADR 0028 の ci_diagnose）。

製品の CI（`templates/ci/diagnose.yml` を写した `.github/workflows/diagnose.yml`）が 3 つの道具を走らせ、
その出力をこのスクリプトが 1 つの JSON（成果物 diagnose）と Job Summary の Markdown にまとめる。LLM を使わない。

  - 依存: osv-scanner の JSON を base と head の 2 つ受け取り、head にだけある脆弱性を「この PR が持ち込んだ」とする
  - 静的解析: semgrep の JSON（--baseline-commit で base との差分だけにしたもの）
  - 秘密情報: gitleaks の JSON（PR のコミットの範囲だけを --redact で走らせたもの）

shadow で動かす前提なので、道具の出力がない・壊れているときはその診断を error にして先へ進み、終了コードは常に 0。
秘密情報の値（Secret・Match）は出力に入れない（公開リポジトリの Job Summary に残るため）。

findings の各要素は Defect（ADR 0028 の 5）の found_in=diagnose に写せる形にしてある。取り込みは次の段。

使い方:
  core-gates/diagnose.py summarize [--deps-head osv-head.json] [--deps-base osv-base.json]
      [--sast semgrep.json] [--secrets gitleaks.json] [--root-head .] [--root-base /tmp/base]
      [--repo owner/name] [--pr N] [--base SHA] [--head SHA] [--out diagnose.json]
  Markdown は標準出力に出す（CI では $GITHUB_STEP_SUMMARY に足す）

依存: python3 の標準ライブラリだけ
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCHEMA_VERSION = 1
SEVERITIES = ["critical", "high", "medium", "low", "unknown"]
SEMGREP_SEVERITY = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}
LABELS = {"deps": "依存", "sast": "静的解析", "secrets": "秘密情報"}
SUMMARY_LIMIT = 30  # Job Summary に並べる件数の上限（全件は JSON に）


def _load_json(path: str | None):
    """読めれば (中身, None)、読めなければ (None, 理由)。"""
    if not path:
        return None, "道具の出力が渡されていない"
    p = Path(path)
    if not p.exists():
        return None, f"{p.name} がない（道具が走らなかった）"
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return None, f"{p.name} が空"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        return None, f"{p.name} を JSON として読めない（{e.msg}）"


def _relative(path: str, root: str | None) -> str:
    if root:
        try:
            return os.path.relpath(os.path.realpath(path), os.path.realpath(root))
        except ValueError:
            pass
    return path


def cvss_severity(score) -> str:
    """osv-scanner の max_severity（CVSS の基本値の文字列）を段階にする。"""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "unknown"
    if s >= 9.0:
        return "critical"
    if s >= 7.0:
        return "high"
    if s >= 4.0:
        return "medium"
    if s > 0:
        return "low"
    return "unknown"


def _vuln_id(ids: list[str]) -> str:
    # GHSA を優先し、なければ辞書順で最初（同じ脆弱性の別名で base と head がずれないように）
    ghsa = sorted(i for i in ids if i.startswith("GHSA-"))
    return ghsa[0] if ghsa else sorted(ids)[0]


def parse_osv(doc, root: str | None) -> list[dict]:
    out: list[dict] = []
    for result in (doc or {}).get("results") or []:
        source = _relative((result.get("source") or {}).get("path", ""), root)
        for pkg in result.get("packages") or []:
            p = pkg.get("package") or {}
            groups = pkg.get("groups") or [
                {"ids": [v.get("id")], "max_severity": ""} for v in pkg.get("vulnerabilities") or [] if v.get("id")
            ]
            for g in groups:
                ids = [i for i in g.get("ids") or [] if i]
                if not ids:
                    continue
                out.append(
                    {
                        "perspective": "dependency",
                        "rule": _vuln_id(ids),
                        "aliases": sorted(set(ids) | set(g.get("aliases") or [])),
                        "severity": cvss_severity(g.get("max_severity")),
                        "location": source,
                        "package": f"{p.get('ecosystem', '')}:{p.get('name', '')}",
                        "version": p.get("version", ""),
                    }
                )
    return out


def _dep_key(f: dict) -> tuple:
    # 版は鍵に入れない（版を上げてもまだ脆弱なものは「持ち込み」ではない）
    return (f["location"], f["package"], f["rule"])


def diff_deps(head: list[dict], base: list[dict]) -> list[dict]:
    seen = {_dep_key(f) for f in base}
    return [f for f in head if _dep_key(f) not in seen]


def parse_semgrep(doc) -> list[dict]:
    out = []
    for r in (doc or {}).get("results") or []:
        extra = r.get("extra") or {}
        out.append(
            {
                "perspective": "sast",
                "rule": r.get("check_id", ""),
                "severity": SEMGREP_SEVERITY.get(str(extra.get("severity", "")).upper(), "unknown"),
                "location": f"{r.get('path', '')}:{(r.get('start') or {}).get('line', '')}",
                "message": " ".join(str(extra.get("message", "")).split())[:300],
            }
        )
    return out


def parse_gitleaks(doc) -> list[dict]:
    out = []
    for r in doc or []:
        # Secret・Match は捨てる（--redact でも値の一部が残る道具の版があるため）
        out.append(
            {
                "perspective": "secret",
                "rule": r.get("RuleID", ""),
                "severity": "high",
                "location": f"{r.get('File', '')}:{r.get('StartLine', '')}",
                "commit": str(r.get("Commit", ""))[:12],
                "message": r.get("Description", ""),
            }
        )
    return out


def _section(findings: list[dict] | None, error: str | None, **extra) -> dict:
    if error is not None:
        return {"status": "error", "error": error, "findings": [], **extra}
    return {"status": "found" if findings else "clean", "findings": findings, **extra}


def summarize(args) -> dict:
    sections: dict[str, dict] = {}

    head_doc, head_err = _load_json(args.deps_head)
    base_doc, base_err = _load_json(args.deps_base)
    if head_err:
        sections["deps"] = _section(None, head_err)
    else:
        head = parse_osv(head_doc, args.root_head)
        if base_err:
            # base が測れなければ head の全件を出す（持ち込みかは分からないと書く）
            sections["deps"] = _section(head, None, total=len(head), compared_with_base=False, note=f"base を比べられなかった: {base_err}")
        else:
            base = parse_osv(base_doc, args.root_base)
            sections["deps"] = _section(diff_deps(head, base), None, total=len(head), compared_with_base=True)

    sast_doc, sast_err = _load_json(args.sast)
    sast_errors = len((sast_doc or {}).get("errors") or []) if not sast_err else 0
    sections["sast"] = _section(None if sast_err else parse_semgrep(sast_doc), sast_err, tool_errors=sast_errors)

    sec_doc, sec_err = _load_json(args.secrets)
    if not sec_err and not isinstance(sec_doc, list):
        sec_err = "gitleaks の出力が配列でない"
    sections["secrets"] = _section(None if sec_err else parse_gitleaks(sec_doc), sec_err)

    for s in sections.values():
        s["findings"].sort(key=lambda f: (SEVERITIES.index(f["severity"]), f["location"], f["rule"]))

    return {
        "version": SCHEMA_VERSION,
        "mode": "shadow",
        "repo": args.repo,
        "pr": args.pr,
        "base": args.base,
        "head": args.head,
        "diagnoses": sections,
        "counts": {k: len(v["findings"]) for k, v in sections.items()},
    }


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict) -> str:
    lines = ["## PR の診断（shadow）", "", "失敗しても PR は止めない。結果は成果物 `diagnose` の `diagnose.json` にも残る。", ""]
    lines += ["| 診断 | 状態 | この PR が持ち込んだもの |", "|---|---|---|"]
    status_ja = {"clean": "なし", "found": "あり", "error": "測れなかった"}
    for key, s in report["diagnoses"].items():
        note = s.get("error") or s.get("note") or ""
        count = "-" if s["status"] == "error" else str(len(s["findings"]))
        lines.append(f"| {LABELS[key]} | {status_ja[s['status']]}{'（' + _cell(note) + '）' if note else ''} | {count} |")
    for key, s in report["diagnoses"].items():
        if not s["findings"]:
            continue
        lines += ["", f"### {LABELS[key]}", "", "| 重さ | 場所 | 規則 | 内容 |", "|---|---|---|---|"]
        for f in s["findings"][:SUMMARY_LIMIT]:
            what = f.get("message") or f"{f.get('package', '')} {f.get('version', '')}".strip()
            lines.append(f"| {f['severity']} | `{_cell(f['location'])}` | `{_cell(f['rule'])}` | {_cell(what)} |")
        if len(s["findings"]) > SUMMARY_LIMIT:
            lines.append(f"\nほか {len(s['findings']) - SUMMARY_LIMIT} 件は diagnose.json に。")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summarize")
    s.add_argument("--deps-head")
    s.add_argument("--deps-base")
    s.add_argument("--sast")
    s.add_argument("--secrets")
    s.add_argument("--root-head", default=".")
    s.add_argument("--root-base")
    s.add_argument("--repo", default="")
    s.add_argument("--pr", default="")
    s.add_argument("--base", default="")
    s.add_argument("--head", default="")
    s.add_argument("--out", default="diagnose.json")
    args = ap.parse_args(argv)

    report = summarize(args)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
