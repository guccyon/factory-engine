"""PR ごとの診断のまとめ（core-gates/diagnose.py、ADR 0028 の ci_diagnose）"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import diagnose as dg  # noqa: E402

ENGINE = Path(__file__).resolve().parents[2]


def osv(root, vulns):
    """vulns: [(lockfile, ecosystem, name, version, ids, score)]"""
    results = {}
    for lock, eco, name, ver, ids, score in vulns:
        r = results.setdefault(lock, {"source": {"path": f"{root}/{lock}", "type": "lockfile"}, "packages": []})
        r["packages"].append(
            {
                "package": {"name": name, "version": ver, "ecosystem": eco},
                "vulnerabilities": [{"id": i} for i in ids],
                "groups": [{"ids": ids, "aliases": ids, "max_severity": score}],
            }
        )
    return {"results": list(results.values())}


def write(tmp_path, name, doc):
    p = tmp_path / name
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return str(p)


def run(tmp_path, capsys, **files):
    args = ["summarize", "--out", str(tmp_path / "diagnose.json")]
    for k, v in files.items():
        args += [f"--{k.replace('_', '-')}", v]
    assert dg.main(args) == 0
    return json.loads((tmp_path / "diagnose.json").read_text()), capsys.readouterr().out


def test_cvss_severity():
    assert dg.cvss_severity("9.8") == "critical"
    assert dg.cvss_severity("7.5") == "high"
    assert dg.cvss_severity("5") == "medium"
    assert dg.cvss_severity("2.1") == "low"
    assert dg.cvss_severity("") == "unknown"
    assert dg.cvss_severity(None) == "unknown"


def test_deps_reports_only_vulns_new_in_head(tmp_path, capsys):
    base = osv("/w/base", [("apps/backend/Cargo.lock", "crates.io", "old", "1.0.0", ["RUSTSEC-2020-0001"], "5.3")])
    head = osv(
        "/w/head",
        [
            # 版を上げてもまだ同じ脆弱性 → 持ち込みではない
            ("apps/backend/Cargo.lock", "crates.io", "old", "1.0.1", ["RUSTSEC-2020-0001"], "5.3"),
            ("apps/admin-web/package-lock.json", "npm", "leftpad", "0.0.1", ["GHSA-aaaa-bbbb-cccc", "CVE-2024-1"], "9.1"),
        ],
    )
    report, md = run(
        tmp_path,
        capsys,
        deps_head=write(tmp_path, "h.json", head),
        deps_base=write(tmp_path, "b.json", base),
        root_head="/w/head",
        root_base="/w/base",
    )
    deps = report["diagnoses"]["deps"]
    assert deps["status"] == "found"
    assert deps["total"] == 2 and deps["compared_with_base"] is True
    assert [(f["location"], f["rule"], f["severity"]) for f in deps["findings"]] == [
        ("apps/admin-web/package-lock.json", "GHSA-aaaa-bbbb-cccc", "critical")
    ]
    assert "npm:leftpad 0.0.1" in md


def test_deps_without_base_reports_all_and_says_so(tmp_path, capsys):
    head = osv("/w", [("Cargo.lock", "crates.io", "a", "1", ["RUSTSEC-1"], "")])
    report, _ = run(tmp_path, capsys, deps_head=write(tmp_path, "h.json", head), root_head="/w")
    deps = report["diagnoses"]["deps"]
    assert deps["compared_with_base"] is False and len(deps["findings"]) == 1
    assert "base を比べられなかった" in deps["note"]


def test_missing_or_broken_tool_output_is_error_not_crash(tmp_path, capsys):
    report, md = run(
        tmp_path,
        capsys,
        deps_head=str(tmp_path / "none.json"),
        sast=write(tmp_path, "s.json", "{broken"),
        secrets=write(tmp_path, "g.json", {"not": "a list"}),
    )
    assert {k: v["status"] for k, v in report["diagnoses"].items()} == {"deps": "error", "sast": "error", "secrets": "error"}
    assert "測れなかった" in md


def test_clean_run(tmp_path, capsys):
    report, md = run(
        tmp_path,
        capsys,
        deps_head=write(tmp_path, "h.json", {"results": []}),
        deps_base=write(tmp_path, "b.json", {"results": []}),
        sast=write(tmp_path, "s.json", {"results": [], "errors": []}),
        secrets=write(tmp_path, "g.json", []),
    )
    assert report["counts"] == {"deps": 0, "sast": 0, "secrets": 0}
    assert report["mode"] == "shadow"
    assert "### " not in md  # 詳細の表は出ない


def test_sast_maps_severity_and_location(tmp_path, capsys):
    sg = {
        "results": [
            {"check_id": "python.lang.security.eval", "path": "a.py", "start": {"line": 3}, "extra": {"severity": "WARNING", "message": "eval\n を使わない"}},
            {"check_id": "x.err", "path": "b.py", "start": {"line": 1}, "extra": {"severity": "ERROR", "message": "m"}},
        ],
        "errors": [{"type": "x"}],
    }
    report, _ = run(tmp_path, capsys, sast=write(tmp_path, "s.json", sg))
    sast = report["diagnoses"]["sast"]
    assert [(f["severity"], f["location"]) for f in sast["findings"]] == [("high", "b.py:1"), ("medium", "a.py:3")]
    assert sast["findings"][1]["message"] == "eval を使わない"
    assert sast["tool_errors"] == 1


def test_secret_values_never_leave(tmp_path, capsys):
    leak = "AKIAABCDEFGHIJKLMNOP"
    gl = [{"RuleID": "aws-access-token", "Description": "AWS", "File": "conf.env", "StartLine": 2, "Commit": "0123456789abcdef", "Secret": leak, "Match": f"KEY={leak}"}]
    report, md = run(tmp_path, capsys, secrets=write(tmp_path, "g.json", gl))
    raw = (tmp_path / "diagnose.json").read_text()
    assert leak not in raw and leak not in md
    f = report["diagnoses"]["secrets"]["findings"][0]
    assert f["location"] == "conf.env:2" and f["commit"] == "0123456789ab"


def test_pipe_in_message_does_not_break_table(tmp_path, capsys):
    sg = {"results": [{"check_id": "r", "path": "a|b.py", "start": {"line": 1}, "extra": {"severity": "INFO", "message": "x | y"}}]}
    _, md = run(tmp_path, capsys, sast=write(tmp_path, "s.json", sg))
    row = [l for l in md.splitlines() if "r`" in l][0]
    assert row.count(" | ") == 3  # 列の区切りだけ


def test_engine_workflow_is_the_template():
    """factory-engine 自身の CI は テンプレートそのもの（ENGINE_REF: self）"""
    tpl = (ENGINE / "templates/ci/diagnose.yml").read_text(encoding="utf-8")
    own = (ENGINE / ".github/workflows/diagnose.yml").read_text(encoding="utf-8")
    assert own == tpl
    assert re.search(r"^  ENGINE_REF: self$", tpl, re.M)
