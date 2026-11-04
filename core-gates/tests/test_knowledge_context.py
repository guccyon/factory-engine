"""エージェントに渡す知見の選び方（core-gates/knowledge_context.py、ADR 0024 の 6）"""
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import knowledge_context as kx  # noqa: E402

TODAY = date(2026, 11, 5)


def k(kid, **over):
    base = {"status": "active", "confidence": "confirmed", "verified_at": "2026-11-01", "review_by": "2027-01-30",
            "confidentiality": "product", "scope": "{roles: [implement], paths: [\"apps/**\"]}",
            "statement": f"{kid} の主張"}
    base.update(over)
    return "\n".join([f"id: {kid}", "theme: 4", "kind: practice", "evidence:\n  - pr: 1", "decided_by: committee:D-0001"]
                     + [f"{key}: {v}" for key, v in base.items() if v is not None]) + "\n"


def repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / kx.kc.ROOT
    root.mkdir(parents=True)
    for kid, text in files.items():
        (root / f"{kid}.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def test_passes_only_active_confirmed_or_settled_for_the_role(tmp_path):
    r = repo(tmp_path, {
        "K-0001": k("K-0001"),
        "K-0002": k("K-0002", confidence="hypothesis"),
        "K-0003": k("K-0003", status="draft"),
        "K-0004": k("K-0004", confidence="settled", review_by=None, enforcement="{level: deterministic, solutions: [{kind: ci_check, what: x}]}"),
        "K-0005": k("K-0005", confidence="settled", review_by=None, enforcement="{level: checklist}"),
        "K-0006": k("K-0006", review_by="2026-11-01"),
        "K-0007": k("K-0007", scope="{roles: [judge]}"),
        "K-0008": k("K-0008", scope="{roles: [implementer]}"),
        "K-0009": k("K-0009", scope="{paths: [\"x\"]}"),
        "K-0010": k("K-0010", confidentiality="client"),
        "K-0011": k("K-0011", status="superseded"),
    })
    text, ids, skipped = kx.build(r, "implement", TODAY)
    assert sorted(ids) == ["K-0001", "K-0005", "K-0008", "K-0009"]
    assert skipped["hypothesis"] == ["K-0002"]
    assert skipped["enforced"] == ["K-0004"]
    assert skipped["expired"] == ["K-0006"]
    assert skipped["other_role"] == ["K-0007"]
    assert skipped["confidential"] == ["K-0010"]
    assert sorted(skipped["not_active"]) == ["K-0003", "K-0011"]
    assert "## K-0001（確認済み、確認日 2026-11-01）" in text
    assert "## K-0005（確定、確認日 2026-11-01）" in text
    assert "効く範囲: パス apps/**" in text


def test_newest_first_and_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(kx, "MAX_ITEMS", 2)
    r = repo(tmp_path, {f"K-000{i}": k(f"K-000{i}", verified_at=f"2026-10-0{i}") for i in range(1, 5)})
    _, ids, skipped = kx.build(r, "implement", TODAY)
    assert ids == ["K-0004", "K-0003"]
    assert skipped["over_limit"] == ["K-0002", "K-0001"]


def test_main_writes_nothing_when_nothing_to_pass(tmp_path, capsys):
    r = repo(tmp_path, {"K-0001": k("K-0001", confidence="hypothesis")})
    out = tmp_path / "ctx" / "knowledge.md"
    assert kx.main(["--repo", str(r), "--out", str(out), "--today", "2026-11-05"]) == 0
    assert not out.exists()
    assert json.loads(capsys.readouterr().out)["ids"] == []


def test_main_without_store_is_quiet(tmp_path, capsys):
    out = tmp_path / "knowledge.md"
    assert kx.main(["--repo", str(tmp_path), "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ids": [], "skipped": {}}


def test_main_writes_file_with_header(tmp_path, capsys):
    r = repo(tmp_path, {"K-0001": k("K-0001")})
    out = tmp_path / "ctx" / "knowledge.md"
    kx.main(["--repo", str(r), "--out", str(out), "--today", "2026-11-05"])
    assert json.loads(capsys.readouterr().out)["ids"] == ["K-0001"]
    body = out.read_text(encoding="utf-8")
    assert body.startswith("# この製品の知見") and "K-0001 の主張" in body
