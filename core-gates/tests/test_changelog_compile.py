"""CHANGELOG の断片の取りまとめ（ADR 0027 の 1）"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import changelog_compile as cc  # noqa: E402

BASE = """# 変更履歴

前書き。

## [Unreleased]

### 追加
- 既存の追加

## [0.4.0] - 2026-11-01

### 追加
- 古い
"""


def make(tmp_path: Path, frags: dict[str, str], changelog: str = BASE) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    d = tmp_path / "changelog.d"
    d.mkdir()
    (d / "README.md").write_text("断片の書き方", encoding="utf-8")
    for name, text in frags.items():
        (d / name).write_text(text, encoding="utf-8")
    return tmp_path


def unreleased(text: str) -> str:
    return text.split("## [Unreleased]")[1].split("## [0.4.0]")[0]


def test_moves_fragments_by_kind_newest_first(tmp_path):
    repo = make(tmp_path, {
        "700.md": "### 追加\n- 七百\n  続きの行\n",
        "705.md": "### 追加\n- 七百五\n### 修正\n- 直し\n",
        "701.md": "- 見出しなし\n",
    })
    assert cc.compile_repo(repo) == 0
    out = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    u = unreleased(out)
    assert u.index("### 追加") < u.index("### 変更") < u.index("### 修正")
    added = u.split("### 追加")[1].split("###")[0]
    assert added.index("- 七百五") < added.index("- 七百\n") < added.index("既存の追加")
    assert "  続きの行" in added
    assert "- 見出しなし" in u.split("### 変更")[1].split("###")[0]
    assert "- 古い" in out.split("## [0.4.0]")[1]   # 版の項は変えない
    assert "前書き。" in out
    assert sorted(p.name for p in (repo / "changelog.d").iterdir()) == ["README.md"]


def test_no_fragments(tmp_path):
    repo = make(tmp_path, {})
    assert cc.compile_repo(repo) == 3
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == BASE


def test_no_unreleased_keeps_fragments(tmp_path):
    repo = make(tmp_path, {"1.md": "- x\n"}, changelog="# 変更履歴\n")
    assert cc.compile_repo(repo) == 2
    assert (repo / "changelog.d" / "1.md").exists()


def test_idempotent_shape(tmp_path):
    repo = make(tmp_path, {"1.md": "### 追加\n- a\n"})
    cc.compile_repo(repo)
    first = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    (repo / "changelog.d" / "2.md").write_text("### 追加\n- b\n", encoding="utf-8")
    cc.compile_repo(repo)
    second = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert second.count("### 追加") == first.count("### 追加")
    assert "\n\n\n" not in second
