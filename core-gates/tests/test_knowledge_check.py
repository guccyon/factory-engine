"""知見の変更セットの決定論の検査（core-gates/knowledge_check.py、ADR 0024）"""
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import knowledge_check as kc  # noqa: E402

TODAY = date(2026, 11, 1)

GOOD = """id: K-0001
theme: 4
kind: term_usage
statement: 利用者向けの文言では「スロット」ではなく「予約枠」と書く
scope: {contexts: [scheduling], roles: [implementer], paths: ["apps/**"]}
confidence: confirmed
evidence:
  - case: C-0001
  - pr: "guccyon/rally#301"
status: active
verified_at: 2026-11-01
review_by: 2027-01-29
decided_by: committee:D-0001
confidentiality: product
"""


def repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / kc.ROOT
    (root / "cases").mkdir(parents=True)
    (root / "committee").mkdir()
    (root / "cases" / "C-0001.md").write_text("# 事例\n予約枠の呼び方が揺れた\n", encoding="utf-8")
    (root / "committee" / "D-0001.yaml").write_text("id: D-0001\n", encoding="utf-8")
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    return tmp_path


def run(tmp_path, files, index=True):
    r = repo(tmp_path, files)
    if index:
        kc.main(["--repo", str(r), "--index", "--today", str(TODAY)])
    return kc.main(["--repo", str(r), "--today", str(TODAY), "--json", str(tmp_path / "out.json")]), (tmp_path / "out.json").read_text(encoding="utf-8")


def test_good_item_passes(tmp_path):
    rc, out = run(tmp_path, {"K-0001.yaml": GOOD})
    assert rc == 0, out


def test_missing_fields_and_bad_values(tmp_path):
    bad = GOOD.replace("theme: 4", "theme: 9").replace("kind: term_usage", "kind: rumor").replace("decided_by: committee:D-0001\n", "")
    rc, out = run(tmp_path, {"K-0001.yaml": bad})
    assert rc == 1 and "theme は 1〜8" in out and "kind は" in out and "decided_by" in out


def test_review_by_must_fit_confidence(tmp_path):
    rc, out = run(tmp_path, {"K-0001.yaml": GOOD.replace("confidence: confirmed", "confidence: hypothesis")})
    assert rc == 1 and "hypothesis の期限" in out


def test_settled_needs_enforcement_or_spec(tmp_path):
    settled = GOOD.replace("confidence: confirmed", "confidence: settled").replace("review_by: 2027-01-29\n", "")
    rc, out = run(tmp_path, {"K-0001.yaml": settled})
    assert rc == 1 and "settled は決定論の担保" in out
    ok = settled + "enforcement:\n  level: deterministic\n  solutions:\n    - {kind: lint, what: UI 文言の禁止語, issue: \"guccyon/rally#318\"}\n"
    (tmp_path / "b").mkdir()
    rc, out = run(tmp_path / "b", {"K-0001.yaml": ok})
    assert rc == 0, out


def test_supersedes_must_be_mutual_and_old_not_active(tmp_path):
    old = GOOD.replace("K-0001", "K-0002").replace("「予約枠」", "「枠」")
    new = GOOD.replace("status: active", "status: active\nsupersedes: [K-0002]")
    rc, out = run(tmp_path, {"K-0001.yaml": new, "K-0002.yaml": old})
    assert rc == 1 and "superseded_by に K-0001 がない" in out and "superseded にする" in out
    old2 = old.replace("status: active", "status: superseded\nsuperseded_by: [K-0001]")
    (tmp_path / "c").mkdir()
    rc, out = run(tmp_path / "c", {"K-0001.yaml": new, "K-0002.yaml": old2})
    assert rc == 0, out


def test_evidence_case_must_exist(tmp_path):
    rc, out = run(tmp_path, {"K-0001.yaml": GOOD.replace("case: C-0001", "case: C-0099")})
    assert rc == 1 and "C-0099 が cases/ にない" in out


def test_personal_data_is_rejected_everywhere(tmp_path):
    pii = GOOD.replace("と書く", "と書く（taro@example.com から 090-1234-5678 で問い合わせ）")
    rc, out = run(tmp_path, {"K-0001.yaml": pii})
    assert rc == 1 and "メールアドレス" in out and "電話番号" in out
    (tmp_path / "d").mkdir()
    r = repo(tmp_path / "d", {"K-0001.yaml": GOOD})
    (r / kc.ROOT / "cases" / "C-0001.md").write_text("生徒 3f2b8c1e-1d2a-4b5c-9e8f-0a1b2c3d4e5f が予約できなかった\n", encoding="utf-8")
    kc.main(["--repo", str(r), "--index", "--today", str(TODAY)])
    assert kc.main(["--repo", str(r), "--today", str(TODAY)]) == 1


def test_restricted_evidence_is_only_an_opaque_id(tmp_path):
    rc, out = run(tmp_path, {"K-0001.yaml": GOOD.replace("  - pr: \"guccyon/rally#301\"", "  - restricted: 影響を受けた生徒の一覧")})
    assert rc == 1 and "不透明な id" in out


def test_index_must_be_regenerated_and_inventory_lists_stale(tmp_path, capsys):
    rc, out = run(tmp_path, {"K-0001.yaml": GOOD}, index=False)
    assert rc == 1 and "INDEX.md" in out
    r = tmp_path
    capsys.readouterr()
    kc.main(["--repo", str(r), "--inventory", "--today", "2027-02-01"])
    assert '"K-0001"' in capsys.readouterr().out
