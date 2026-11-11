"""宣言した共有ファイルの直書きを止める（ADR 0051 の 3）"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import aggregate_guard as ag  # noqa: E402

DECL = [{"path": "README.md", "fragments": "readme.d/", "compile": "append_section"},
        {"path": "schema.ts", "compile": "generate", "command": "make gen"}]


def test_direct_edit_by_factory_pr_is_a_violation():
    v = ag.violations(DECL, [("M", "README.md"), ("M", "src/a.ts")])
    assert len(v) == 1 and "readme.d/<Issue 番号>.md" in v[0]


def test_fragment_and_generated_changes_pass():
    assert ag.violations(DECL, [("A", "readme.d/12.md"), ("M", "schema.ts"), ("M", "src/a.ts")]) == []


def test_compile_pr_passes():
    assert ag.is_compile_pr(DECL, [("M", "README.md"), ("D", "readme.d/12.md"), ("D", "readme.d/13.md")])
    assert ag.violations(DECL, [("M", "README.md"), ("D", "readme.d/12.md")]) == []
    assert not ag.is_compile_pr(DECL, [("M", "README.md"), ("A", "readme.d/14.md")])   # 断片を足すのは取りまとめではない
    assert not ag.is_compile_pr(DECL, [("M", "README.md")])   # 断片を消していない直書きは取りまとめではない


def git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def test_main_only_checks_factory_branches(tmp_path: Path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".factory-config").mkdir()
    (tmp_path / ".factory-config/aggregates.yaml").write_text(
        "aggregates:\n  - path: README.md\n    fragments: readme.d/\n    compile: append_section\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# a\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "checkout", "-qb", "factory/issue-1-job-2")
    (tmp_path / "README.md").write_text("# a\n- 足した\n", encoding="utf-8")
    git(tmp_path, "commit", "-qam", "direct")
    assert ag.main([str(tmp_path), "--base", "main", "--branch", "factory/issue-1-job-2"]) == 1
    assert ag.main([str(tmp_path), "--base", "main", "--branch", "feature/human"]) == 0   # 人の PR は見ない
