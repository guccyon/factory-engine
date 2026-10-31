#!/usr/bin/env python3
"""知見の変更セットの決定論の検査（ADR 0024）。

製品の .factory-config/knowledge/ を読み、委員会に持ち込む前に機械で確かめられることを見る。LLM を使わない。
  - 形: 必須項目、値（テーマ・種類・確かさ・状態・機密区分・担保）、id とファイル名の一致、id の重複
  - 期限: review_by が確かさの既定の日数（仮説 30 日・確認済み 90 日）を超えていない。settled は期限なしでよいが、担保（enforcement.solutions）か規範（evidence の spec）が要る
  - 参照: supersedes と superseded_by が両側にある、置き換えられた知見が active のまま残っていない、evidence の case が cases/ にある
  - 個人情報: 知見・事例記録・議決の全文に、メール・電話番号・利用者 ID の形（UUID）・カード番号・IP アドレス・秘密情報の形がない
  - 一覧: INDEX.md が中身から作った一覧と一致する（--index で作り直す）

製品の CI は Engine の固定した版のこのファイルだけを取って動かすので、値はここに持つ（正本は definitions/knowledge.yaml）。

使い方:
  core-gates/knowledge_check.py [--repo .] [--json out.json] [--today YYYY-MM-DD]
  core-gates/knowledge_check.py --index [--repo .]        # INDEX.md を作り直す
  core-gates/knowledge_check.py --inventory [--repo .]    # 期限切れ（要再確認）の一覧を JSON で出す（日次の棚卸し）
  終了コード: 0 = 緑、1 = 通らない、2 = 読めなかった

依存: python3。YAML は PyYAML があればそれ、なければ ruby で読む
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = ".factory-config/knowledge"
REQUIRED = ["id", "theme", "kind", "statement", "scope", "confidence", "evidence", "status", "verified_at", "confidentiality"]
THEMES = list(range(1, 9))
KINDS = ["fact", "term_usage", "practice", "causal", "preference", "hypothesis_test"]
CONFIDENCE_DAYS = {"hypothesis": 30, "confirmed": 90, "settled": None}
STATUSES = ["draft", "active", "stale", "superseded", "retired"]
EVIDENCE_KINDS = ["case", "pr", "issue", "session", "event", "spec", "restricted"]
LEVELS = ["context", "checklist", "deterministic"]
SOLUTION_KINDS = ["lint", "test", "type", "schema", "ci_check", "config", "overlay_rule"]
CONFIDENTIALITY = ["product", "client", "public"]
ID_RE = re.compile(r"^K-\d{4,}$")
CASE_RE = re.compile(r"^C-\d{4,}$")
DECISION_RE = re.compile(r"^D-\d{4,}$")
RESTRICTED_RE = re.compile(r"^R-\d{4,}$")

# 個人情報と秘密情報の形。知見は抽象化して書く（「生徒 A さん」ではなく「予約の確定待ちの生徒」）
PII = {
    "メールアドレス": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "電話番号": re.compile(r"(?<!\d)(?:\+81[- ]?|0)\d{1,4}[- ]\d{1,4}[- ]\d{3,4}(?!\d)|(?<!\d)0[789]0\d{8}(?!\d)"),
    "利用者 ID の形（UUID）": re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    "カード番号": re.compile(r"(?<!\d)(?:\d{4}[- ]){3}\d{4}(?!\d)"),
    "IP アドレス": re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
    "秘密情報": re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-ant-[A-Za-z0-9_\-]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
PII_ALLOWED_IPS = {"127.0.0.1", "0.0.0.0"}


def load_yaml(path: Path):
    try:
        import yaml  # type: ignore
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except ImportError:
        out = subprocess.run(["ruby", "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.load_file(ARGV[0], permitted_classes: [Date]))", str(path)],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out)


def as_date(v) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        return None


def load(repo: Path) -> tuple[list[dict], set[str], set[str], list[str]]:
    d = repo / ROOT
    items, errors = [], []
    if not d.is_dir():
        return [], set(), set(), []
    for p in sorted(d.glob("K-*.yaml")):
        try:
            k = load_yaml(p)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{p.name}: 読めない（{e}）")
            continue
        if not isinstance(k, dict):
            errors.append(f"{p.name}: 知見の形ではない")
            continue
        k["_file"] = p.name
        items.append(k)
    cases = {p.stem for p in (d / "cases").glob("C-*.md")} if (d / "cases").is_dir() else set()
    decisions = {p.stem for p in (d / "committee").glob("D-*.yaml")} if (d / "committee").is_dir() else set()
    return items, cases, decisions, errors


def check_item(k: dict, cases: set[str], decisions: set[str]) -> list[str]:
    f, e = k["_file"], []
    for key in REQUIRED:
        if k.get(key) in (None, "", [], {}):
            e.append(f"{f}: {key} がない")
    kid = str(k.get("id", ""))
    if not ID_RE.match(kid):
        e.append(f"{f}: id は K-NNNN（{kid}）")
    elif f != f"{kid}.yaml":
        e.append(f"{f}: ファイル名と id が違う（{kid}）")
    if k.get("theme") not in THEMES:
        e.append(f"{f}: theme は 1〜8（{k.get('theme')}）")
    if k.get("kind") not in KINDS:
        e.append(f"{f}: kind は {KINDS} のどれか（{k.get('kind')}）")
    conf, status = k.get("confidence"), k.get("status")
    if conf not in CONFIDENCE_DAYS:
        e.append(f"{f}: confidence は {list(CONFIDENCE_DAYS)} のどれか（{conf}）")
    if status not in STATUSES:
        e.append(f"{f}: status は {STATUSES} のどれか（{status}）")
    if k.get("confidentiality") not in CONFIDENTIALITY:
        e.append(f"{f}: confidentiality は {CONFIDENTIALITY} のどれか")
    if status != "draft" and not k.get("decided_by"):
        e.append(f"{f}: draft 以外は decided_by（委員会の議決）が要る")
    dec = str(k.get("decided_by") or "")
    if dec.startswith("committee:") and dec.split(":", 1)[1] not in decisions:
        e.append(f"{f}: decided_by の議決 {dec} が committee/ にない")
    scope = k.get("scope") or {}
    if isinstance(scope, dict) and set(scope) - {"contexts", "roles", "paths"}:
        e.append(f"{f}: scope のキーは contexts・roles・paths だけ（{sorted(set(scope) - {'contexts', 'roles', 'paths'})}）")
    for ev in k.get("evidence") or []:
        if not isinstance(ev, dict) or len(ev) != 1:
            e.append(f"{f}: evidence は 1 項目に 1 つの参照（例: - case: C-0001）")
            continue
        (ek, ev_v), = ev.items()
        if ek not in EVIDENCE_KINDS:
            e.append(f"{f}: evidence の種類は {EVIDENCE_KINDS} のどれか（{ek}）")
        elif ek == "case" and str(ev_v) not in cases:
            e.append(f"{f}: 根拠の事例記録 {ev_v} が cases/ にない")
        elif ek == "case" and not CASE_RE.match(str(ev_v)):
            e.append(f"{f}: case は C-NNNN（{ev_v}）")
        elif ek == "restricted" and not RESTRICTED_RE.match(str(ev_v)):
            e.append(f"{f}: restricted は不透明な id（R-NNNN）だけを書く（{ev_v}）")
    verified, review = as_date(k.get("verified_at")), as_date(k.get("review_by"))
    if verified is None and k.get("verified_at"):
        e.append(f"{f}: verified_at は YYYY-MM-DD")
    days = CONFIDENCE_DAYS.get(conf) if conf in CONFIDENCE_DAYS else None
    if conf in ("hypothesis", "confirmed"):
        if review is None:
            e.append(f"{f}: {conf} は review_by（再確認の期限）が要る")
        elif verified and review > verified + timedelta(days=days):
            e.append(f"{f}: review_by が {conf} の期限（確認から {days} 日）を超えている")
    enf = k.get("enforcement") or {}
    if enf:
        if enf.get("level") not in LEVELS:
            e.append(f"{f}: enforcement.level は {LEVELS} のどれか")
        for s in enf.get("solutions") or []:
            if not isinstance(s, dict) or s.get("kind") not in SOLUTION_KINDS or not s.get("what"):
                e.append(f"{f}: enforcement.solutions の各項目は kind（{SOLUTION_KINDS}）と what が要る")
    if conf == "settled":
        has_solution = enf.get("level") == "deterministic" and enf.get("solutions")
        has_norm = any(isinstance(ev, dict) and "spec" in ev for ev in k.get("evidence") or [])
        if not (has_solution or has_norm):
            e.append(f"{f}: settled は決定論の担保（enforcement.solutions）か規範（evidence の spec）が要る")
    return e


def check_links(items: list[dict]) -> list[str]:
    by = {str(k.get("id")): k for k in items}
    e, seen = [], set()
    for k in items:
        kid = str(k.get("id"))
        if kid in seen:
            e.append(f"{k['_file']}: id {kid} が重複している")
        seen.add(kid)
        for old in k.get("supersedes") or []:
            o = by.get(str(old))
            if o is None:
                e.append(f"{k['_file']}: supersedes の {old} がない")
                continue
            if kid not in [str(x) for x in (o.get("superseded_by") or [])]:
                e.append(f"{o['_file']}: superseded_by に {kid} がない（{kid} が置き換えた）")
            if o.get("status") != "superseded" and k.get("status") == "active":
                e.append(f"{o['_file']}: {kid} に置き換えられたのに status が {o.get('status')}（superseded にする）")
        for new in k.get("superseded_by") or []:
            n = by.get(str(new))
            if n is None or kid not in [str(x) for x in (n.get("supersedes") or [])]:
                e.append(f"{k['_file']}: superseded_by の {new} の supersedes に {kid} がない")
        if k.get("status") == "superseded" and not k.get("superseded_by"):
            e.append(f"{k['_file']}: superseded は superseded_by が要る")
        for r in k.get("related") or []:
            if str(r) not in by:
                e.append(f"{k['_file']}: related の {r} がない")
    return e


def check_pii(repo: Path) -> list[str]:
    d, e = repo / ROOT, []
    if not d.is_dir():
        return []
    for p in sorted(d.rglob("*")):
        if not p.is_file() or p.suffix not in (".yaml", ".yml", ".md", ".txt", ".json"):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            for label, r in PII.items():
                for m in r.finditer(line):
                    if label == "IP アドレス" and m.group(0) in PII_ALLOWED_IPS:
                        continue
                    e.append(f"{p.relative_to(repo)}:{n}: 個人情報か秘密情報の形（{label}）。抽象化して書き直す。利用者の情報がどうしても要るなら制限付きの保管先に置き、restricted:R-NNNN で指す")
    return e


def index_md(items: list[dict], today: date) -> str:
    rows = sorted(items, key=lambda k: (k.get("theme") or 0, str(k.get("id"))))
    out = ["# 知見の一覧（自動生成。手で編集しない）", "", "core-gates/knowledge_check.py --index で作り直す。期限は review_by。", "",
           "| id | テーマ | 種類 | 確かさ | 状態 | 担保 | 確認日 | 期限 | 主張 |", "|---|---|---|---|---|---|---|---|---|"]
    for k in rows:
        review = as_date(k.get("review_by"))
        due = f"{review}{'（過ぎた）' if review and review < today and k.get('status') == 'active' else ''}" if review else "—"
        stmt = " ".join(str(k.get("statement") or "").split())[:80]
        out.append(f"| {k.get('id')} | {k.get('theme')} | {k.get('kind')} | {k.get('confidence')} | {k.get('status')} | "
                   f"{(k.get('enforcement') or {}).get('level', 'context')} | {k.get('verified_at')} | {due} | {stmt} |")
    if not rows:
        out.append("| — | | | | | | | | まだない |")
    return "\n".join(out) + "\n"


def inventory(items: list[dict], today: date) -> list[dict]:
    """期限を過ぎた active の知見（要再確認）"""
    out = []
    for k in items:
        review = as_date(k.get("review_by"))
        if k.get("status") == "active" and review and review < today:
            out.append({"id": k.get("id"), "theme": k.get("theme"), "confidence": k.get("confidence"), "review_by": str(review)})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--json")
    ap.add_argument("--today")
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--inventory", action="store_true")
    a = ap.parse_args(argv)
    repo = Path(a.repo)
    today = date.fromisoformat(a.today) if a.today else date.today()
    try:
        items, cases, decisions, errors = load(repo)
    except Exception as e:  # noqa: BLE001
        print(f"読めなかった: {e}", file=sys.stderr)
        return 2
    if a.index:
        (repo / ROOT).mkdir(parents=True, exist_ok=True)
        (repo / ROOT / "INDEX.md").write_text(index_md(items, today), encoding="utf-8")
        return 0
    if a.inventory:
        print(json.dumps(inventory(items, today), ensure_ascii=False))
        return 0
    for k in items:
        errors += check_item(k, cases, decisions)
    errors += check_links(items)
    errors += check_pii(repo)
    idx = repo / ROOT / "INDEX.md"
    if (repo / ROOT).is_dir() and items:
        # 期限の「（過ぎた）」は日付で変わるので、比べるときは外す
        strip = lambda s: re.sub(r"（過ぎた）", "", s)  # noqa: E731
        if not idx.is_file() or strip(idx.read_text(encoding="utf-8")) != strip(index_md(items, today)):
            errors.append(f"{ROOT}/INDEX.md が中身と合わない（core-gates/knowledge_check.py --index で作り直す）")
    result = {"ok": not errors, "items": len(items), "cases": len(cases), "decisions": len(decisions), "errors": errors,
              "stale": inventory(items, today)}
    if a.json:
        Path(a.json).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    for line in errors:
        print(f"::error::{line}")
    print(f"knowledge: {len(items)} 件、事例 {len(cases)} 件、議決 {len(decisions)} 件、エラー {len(errors)} 件、要再確認 {len(result['stale'])} 件")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
