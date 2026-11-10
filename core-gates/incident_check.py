#!/usr/bin/env python3
"""事故記録（FIR）の決定論の検査（ADR 0046）。

incidents/ の FIR-YYYY-NNNN.yaml を読み、査読の前に機械で確かめられることを見る。LLM を使わない。
  - 形: 必須項目、値（状態・資源・仕組み・帯・誰が気づいたか・一般の歯止め）、id とファイル名の一致、id の重複。帯 low は FIR にしない
  - 公開: draft 以外は published（YYYY-MM）が要る。revised は revised_at も
  - 専用のチェック: dedicated_checks の各項目に name と review_by が要る（ADR 0046 の 5 の 3）
  - 影響: impact は比と帯のキーだけ（生の金額・トークン数は書かない）
  - 製品の情報: local_ref が空、全文に URL・Issue や PR の番号・コミットのハッシュ・メール・ホスト名・IP・個人のパス・
    生の金額・日付（日まで）・秘密情報の形がない、--deny で渡した名前（製品・リポジトリ・事業主）が出てこない
  - 一覧: INDEX.md が中身から作った一覧と一致する（--index で作り直す）
  - 専用のチェックの棚卸し（definitions/incident.yaml の dedicated_checks）: 各項目に name・where・added・review_by・why、
    where のファイルがあり印の文字列がそこにある、fir の記録がある、FIR の dedicated_checks と name・review_by がそろう

値はここに持つ（正本は definitions/incident.yaml。食い違いは core-gates/tests/test_incident_check.py で見る）。

使い方:
  core-gates/incident_check.py [--root incidents] [--deny 名前 ...] [--deny-file names.txt] [--json out.json]
  core-gates/incident_check.py --index [--root incidents]
  （<root>/../definitions/incident.yaml があれば専用のチェックの棚卸しも見る。--defs で変えられる）
  終了コード: 0 = 緑、1 = 通らない、2 = 読めなかった

依存: python3 と PyYAML
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = "incidents"
REQUIRED = ["id", "title", "status", "resource", "mechanism_class", "severity", "affects", "symptom", "mechanism", "impact", "mitigation"]
STATUSES = ["draft", "published", "revised", "withdrawn"]
RESOURCES = ["llm_tokens", "external_api", "cloud_metered", "plan_quota"]
MECHANISMS = ["unbounded_repeat", "self_feedback", "contention", "double_pay", "leak"]
SEVERITIES = ["critical", "high", "medium", "low"]
IMPACT_KEYS = ["cost_vs_daily_budget", "share_of_spend", "wasted_vs_median", "noticed_by"]
NOTICED_BY = ["factory", "builder", "owner"]
GENERIC_GUARDS = ["daily_budget", "ticket_ceilings", "same_purpose_once"]
MITIGATION_KEYS = ["fixed_in", "root_fix", "generic_guards", "dedicated_checks", "workaround"]
ID_RE = re.compile(r"^FIR-(\d{4})-(\d{4,})$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# 製品の情報と秘密情報の形（ADR 0046 の 8）。ADR の番号（ADR 0027）と Engine の版の日付（2026.11.03）は残してよい
DEIDENT = {
    "URL": re.compile(r"https?://\S+"),
    "Issue・PR の番号": re.compile(r"(?<![\w-])#\d+\b|\b[\w.-]+/[\w.-]+#\d+\b|(?:Issue|PR|ジョブ|job)\s*#?\d{2,}", re.IGNORECASE),
    "コミットのハッシュ": re.compile(r"(?<![\w.])[0-9a-f]{7,40}(?![\w.])"),
    "メールアドレス": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "ホスト名": re.compile(r"\b[\w-]+\.(?:local|lan|internal|home)\b", re.IGNORECASE),
    "IP アドレス": re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
    "個人のパス": re.compile(r"/(?:Users|home)/[\w.-]+"),
    "生の金額": re.compile(r"[$＄¥￥]\s?\d|\d[\d,]*\s?(?:ドル|円|USD|JPY)\b"),
    "日付（月まで）": re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\d{1,2}/\d{1,2}(?!\d)|\d{1,2}月\d{1,2}日"),
    "秘密情報": re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-ant-[A-Za-z0-9_\-]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
ALLOWED_IPS = {"127.0.0.1", "0.0.0.0"}


def load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _text(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _as_date(v) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        return None


def check_record(path: Path, rec, deny: list[str]) -> list[str]:
    errs: list[str] = []
    name = path.name
    if not isinstance(rec, dict):
        return [f"{name}: 中身が mapping ではない"]
    for k in REQUIRED:
        if k not in rec or rec[k] in (None, "", [], {}):
            errs.append(f"{name}: {k} がない")
    rid = str(rec.get("id") or "")
    if not ID_RE.match(rid):
        errs.append(f"{name}: id {rid!r} は FIR-YYYY-NNNN の形")
    elif path.stem != rid:
        errs.append(f"{name}: id {rid} とファイル名が違う")
    status = rec.get("status")
    if status not in STATUSES:
        errs.append(f"{name}: status {status!r} は {STATUSES} のどれか")
    if status and status != "draft":
        if not MONTH_RE.match(str(rec.get("published") or "")):
            errs.append(f"{name}: draft 以外は published（YYYY-MM）が要る")
        if status == "revised" and not MONTH_RE.match(str(rec.get("revised_at") or "")):
            errs.append(f"{name}: revised は revised_at（YYYY-MM）が要る")
    if rec.get("resource") not in RESOURCES:
        errs.append(f"{name}: resource {rec.get('resource')!r} は {RESOURCES} のどれか")
    if rec.get("mechanism_class") not in MECHANISMS:
        errs.append(f"{name}: mechanism_class {rec.get('mechanism_class')!r} は {MECHANISMS} のどれか")
    for m in rec.get("also") or []:
        if m not in MECHANISMS:
            errs.append(f"{name}: also の {m!r} は {MECHANISMS} のどれか")
    if rec.get("severity") not in SEVERITIES:
        errs.append(f"{name}: severity {rec.get('severity')!r} は {SEVERITIES} のどれか")
    elif rec.get("severity") == "low":
        errs.append(f"{name}: low は台帳だけに残し、FIR にしない（record_from: medium）")
    affects = rec.get("affects") or {}
    if isinstance(affects, dict):
        if not affects.get("components"):
            errs.append(f"{name}: affects.components がない（どの部品で起きるか）")
    else:
        errs.append(f"{name}: affects は mapping")
    impact = rec.get("impact") or {}
    if isinstance(impact, dict):
        for k, v in impact.items():
            if k not in IMPACT_KEYS:
                errs.append(f"{name}: impact.{k} は使えない（比と帯だけ: {IMPACT_KEYS}）")
            elif k == "noticed_by":
                if v not in NOTICED_BY:
                    errs.append(f"{name}: impact.noticed_by は {NOTICED_BY} のどれか")
            elif v is not None and not isinstance(v, (int, float)):
                errs.append(f"{name}: impact.{k} は数（比）か null")
    else:
        errs.append(f"{name}: impact は mapping")
    mit = rec.get("mitigation") or {}
    if isinstance(mit, dict):
        for k in mit:
            if k not in MITIGATION_KEYS:
                errs.append(f"{name}: mitigation.{k} は使えない（{MITIGATION_KEYS}）")
        for g in mit.get("generic_guards") or []:
            if g not in GENERIC_GUARDS:
                errs.append(f"{name}: generic_guards の {g!r} は {GENERIC_GUARDS} のどれか（新しい歯止めは ADR で足す）")
        for d in mit.get("dedicated_checks") or []:
            if not isinstance(d, dict) or not d.get("name") or not _as_date(d.get("review_by")):
                errs.append(f"{name}: dedicated_checks の各項目に name と review_by（日付）が要る")
        fx = mit.get("fixed_in")
        if fx not in (None, "") and not re.fullmatch(r"[0-9a-f]{40}", str(fx)):
            errs.append(f"{name}: mitigation.fixed_in は直した Engine の commit（40 桁）か空")
        if not mit.get("root_fix"):
            errs.append(f"{name}: mitigation.root_fix がない（原因の直し）")
    else:
        errs.append(f"{name}: mitigation は mapping")
    if rec.get("local_ref") not in (None, ""):
        errs.append(f"{name}: local_ref は空にする（台帳の id は配らない）")
    errs += check_deidentified(name, rec, deny)
    return errs


def check_deidentified(name: str, rec: dict, deny: list[str]) -> list[str]:
    errs = []
    body = {k: v for k, v in rec.items() if k not in ("id", "published", "revised_at", "mitigation")}
    mit = dict(rec.get("mitigation") or {})
    mit.pop("fixed_in", None)   # Engine の版（日付の形の版名）は残してよい
    checks = [dict(d) for d in mit.get("dedicated_checks") or [] if isinstance(d, dict)]
    for d in checks:
        d.pop("review_by", None)   # 見直しの期日は工場の中の予定で、製品を割り出せない
    mit["dedicated_checks"] = checks
    text = _text(body) + _text(mit)
    for label, r in DEIDENT.items():
        for m in r.finditer(text):
            if label == "IP アドレス" and m.group(0) in ALLOWED_IPS:
                continue
            errs.append(f"{name}: {label}の形がある: {m.group(0)!r}")
            break
    low = text.lower()
    for d in deny:
        if d and d.lower() in low:
            errs.append(f"{name}: 製品を割り出せる名前がある: {d!r}")
    return errs


def build_index(records: list[tuple[Path, dict]]) -> str:
    lines = ["# 事故記録の一覧", "", "core-gates/incident_check.py --index で作る。手で編集しない。", "",
             "| id | 状態 | 資源 | 仕組み | 帯 | 題 |", "|---|---|---|---|---|---|"]
    for p, r in sorted(records, key=lambda x: x[0].stem):
        lines.append(f"| {r.get('id')} | {r.get('status')} | {r.get('resource')} | {r.get('mechanism_class')} | {r.get('severity')} | {r.get('title')} |")
    return "\n".join(lines) + "\n"


def run(root: Path, deny: list[str]) -> tuple[list[str], list[tuple[Path, dict]]]:
    errs: list[str] = []
    records: list[tuple[Path, dict]] = []
    seen: dict[str, str] = {}
    for p in sorted(root.glob("FIR-*.yaml")):
        try:
            rec = load(p)
        except yaml.YAMLError as e:
            errs.append(f"{p.name}: YAML として読めない: {e}")
            continue
        errs += check_record(p, rec, deny)
        if isinstance(rec, dict):
            rid = str(rec.get("id"))
            if rid in seen:
                errs.append(f"{p.name}: id {rid} が {seen[rid]} と重なる")
            seen[rid] = p.name
            records.append((p, rec))
    for p in sorted(root.glob("*.yaml")):
        if not p.name.startswith("FIR-"):
            errs.append(f"{p.name}: incidents/ に置けるのは FIR-YYYY-NNNN.yaml だけ")
    index = root / "INDEX.md"
    if records and (not index.exists() or index.read_text(encoding="utf-8") != build_index(records)):
        errs.append("INDEX.md が古い（core-gates/incident_check.py --index で作り直す）")
    return errs, records


def check_inventory(engine: Path, inv: list, records: list[tuple[Path, dict]]) -> list[str]:
    """専用のチェックの棚卸し（ADR 0046 の 5 の 3）。消し忘れと載せ忘れを見る"""
    errs: list[str] = []
    by_fir = {str(r.get("id")): r for _, r in records}
    names: dict[str, dict] = {}
    for i, d in enumerate(inv or []):
        tag = f"dedicated_checks[{i}]"
        if not isinstance(d, dict):
            errs.append(f"{tag}: mapping にする"); continue
        for k in ("name", "where", "added", "review_by", "why"):
            if not d.get(k):
                errs.append(f"{tag}: {k} がない")
        if not _as_date(d.get("review_by")):
            errs.append(f"{tag}: review_by は日付")
        if d.get("added") and not MONTH_RE.match(str(d["added"])):
            errs.append(f"{tag}: added は YYYY-MM")
        for w in d.get("where") or []:
            path, _, mark = str(w).partition(":")
            f = engine / path
            if not f.is_file():
                errs.append(f"{tag}: where の {path} がない（外したなら棚卸しからも外す）")
            elif mark and mark not in f.read_text(encoding="utf-8", errors="replace"):
                errs.append(f"{tag}: {path} に {mark!r} がない（外したなら棚卸しからも外す）")
        if d.get("fir"):
            r = by_fir.get(str(d["fir"]))
            if not r:
                errs.append(f"{tag}: fir {d['fir']} の記録がない")
            else:
                mine = {str(x.get("name")): x for x in (r.get("mitigation") or {}).get("dedicated_checks") or [] if isinstance(x, dict)}
                if d.get("name") not in mine:
                    errs.append(f"{tag}: {d['fir']} の mitigation.dedicated_checks に載っていない")
                elif _as_date(mine[d["name"]].get("review_by")) != _as_date(d.get("review_by")):
                    errs.append(f"{tag}: {d['fir']} と review_by が違う")
        names[str(d.get("name"))] = d
    for p, r in records:
        for x in (r.get("mitigation") or {}).get("dedicated_checks") or []:
            if isinstance(x, dict) and str(x.get("name")) not in names:
                errs.append(f"{p.name}: 専用のチェック {x.get('name')!r} が definitions/incident.yaml の dedicated_checks にない")
    return errs


def due_checks(inv: list, today: date, ahead_days: int = 14) -> list[dict]:
    """見直しの期日が来た（ahead_days 日以内に来る）専用のチェック"""
    out = []
    for d in inv or []:
        rb = _as_date(d.get("review_by")) if isinstance(d, dict) else None
        if rb and (rb - today).days <= ahead_days:
            out.append({**d, "review_by": rb.isoformat(), "overdue": rb <= today})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--deny", nargs="*", default=[])
    ap.add_argument("--deny-file")
    ap.add_argument("--json")
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--defs")
    a = ap.parse_args(argv)
    root = Path(a.root)
    if not root.is_dir():
        print(f"{root} がない", file=sys.stderr)
        return 2
    deny = list(a.deny)
    if a.deny_file:
        deny += [ln.strip() for ln in Path(a.deny_file).read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    errs, records = run(root, deny)
    defs_path = Path(a.defs) if a.defs else root.resolve().parent / "definitions" / "incident.yaml"
    if defs_path.is_file():
        inv = (yaml.safe_load(defs_path.read_text(encoding="utf-8")) or {}).get("dedicated_checks") or []
        errs += check_inventory(defs_path.resolve().parent.parent, inv, records)
    if a.index:
        (root / "INDEX.md").write_text(build_index(records), encoding="utf-8")
        errs = [e for e in errs if not e.startswith("INDEX.md")]
    if a.json:
        Path(a.json).write_text(json.dumps({"ok": not errs, "errors": errs, "records": len(records)}, ensure_ascii=False, indent=2), encoding="utf-8")
    for e in errs:
        print(e)
    print(f"事故記録 {len(records)} 件、問題 {len(errs)} 件")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
