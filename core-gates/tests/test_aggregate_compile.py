"""共有ファイルの取りまとめ（ADR 0051 の 2）"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import aggregate_compile as ac  # noqa: E402

CHANGELOG = "# 変更履歴\n\n## [Unreleased]\n\n### 追加\n- 既存\n\n## [0.1.0]\n- 古い\n"
README = "# backend\n\n## 機能\n\n- 予約\n\n## 動かし方\n\n手順。\n"
LIST = "# 一覧\n\n<!-- aggregate:start -->\nK-0001\nK-0003\n<!-- aggregate:end -->\n\n後ろ。\n"
DECL = """aggregates:
  - path: CHANGELOG.md
    fragments: changelog.d/
    compile: changelog
  - path: apps/backend/README.md
    fragments: apps/backend/readme.d/
    compile: append_section
    default_section: 機能
  - path: tools/x/README.md
    fragments: tools/x/list.d/
    compile: sorted_list
"""


def put(root: Path, rel: str, text: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def repo(tmp_path: Path, decl: str | None = DECL) -> Path:
    put(tmp_path, "CHANGELOG.md", CHANGELOG)
    put(tmp_path, "apps/backend/README.md", README)
    put(tmp_path, "tools/x/README.md", LIST)
    if decl is not None:
        put(tmp_path, ".factory-config/aggregates.yaml", decl)
    return tmp_path


def test_default_is_changelog_only(tmp_path):
    r = repo(tmp_path, decl=None)
    assert [a["path"] for a in ac.load(r)] == ["CHANGELOG.md"]
    put(r, "changelog.d/12.md", "### 追加\n- 新しい\n")
    assert ac.compile_repo(r) == 0
    assert "- 新しい\n- 既存" in (r / "CHANGELOG.md").read_text() and not (r / "changelog.d/12.md").exists()
    assert ac.compile_repo(r) == 3   # 断片がなければ何もしない


def test_append_section_adds_to_matching_heading_in_order(tmp_path):
    r = repo(tmp_path)
    put(r, "apps/backend/readme.d/10.md", "## 機能\n- 振替\n")
    put(r, "apps/backend/readme.d/11.md", "- 見出しなし（既定の節へ）\n\n## 運用\n- 新しい節\n")
    put(r, "apps/backend/readme.d/README.md", "書き方")
    assert ac.compile_repo(r) == 0
    out = (r / "apps/backend/README.md").read_text()
    assert out.index("- 予約") < out.index("- 振替") < out.index("- 見出しなし") < out.index("## 動かし方")   # 節の末尾に、古い断片から
    assert out.rstrip().endswith("## 運用\n\n- 新しい節")
    assert (r / "apps/backend/readme.d/README.md").exists() and not (r / "apps/backend/readme.d/10.md").exists()


def test_sorted_list_dedupes_and_sorts_between_marks(tmp_path):
    r = repo(tmp_path)
    put(r, "tools/x/list.d/20.md", "K-0002\nK-0001\n")
    assert ac.compile_repo(r) == 0
    assert (r / "tools/x/README.md").read_text() == "# 一覧\n\n<!-- aggregate:start -->\nK-0001\nK-0002\nK-0003\n<!-- aggregate:end -->\n\n後ろ。\n"


def test_view_merges_pending_fragments_without_writing(tmp_path):
    r = repo(tmp_path)
    put(r, "tools/x/list.d/20.md", "K-0002\n")
    a = next(x for x in ac.load(r) if x["path"] == "tools/x/README.md")
    assert "K-0002" in ac.view(r, a)
    assert "K-0002" not in (r / "tools/x/README.md").read_text() and (r / "tools/x/list.d/20.md").exists()


def test_invalid_declarations(tmp_path):
    for bad in ("aggregates:\n  - path: a.md\n    compile: unknown\n",
                "aggregates:\n  - path: a.md\n    compile: append_section\n",
                "aggregates:\n  - path: a.ts\n    compile: generate\n",
                "aggregates:\n  - path: ../a.md\n    fragments: a.d/\n    compile: sorted_list\n"):
        put(tmp_path, ".factory-config/aggregates.yaml", bad)
        with pytest.raises(ac.Invalid):
            ac.load(tmp_path)
        assert ac.compile_repo(tmp_path) == 2


def test_sorted_list_without_marks_is_reported(tmp_path):
    r = repo(tmp_path)
    put(r, "tools/x/README.md", "# 印なし\n")
    put(r, "tools/x/list.d/20.md", "K-0002\n")
    assert ac.compile_repo(r) == 2 and (r / "tools/x/list.d/20.md").exists()   # 断片は残す


def test_generate_runs_command_and_reports_change(tmp_path):
    put(tmp_path, ".factory-config/aggregates.yaml",
        "aggregates:\n  - path: gen.txt\n    compile: generate\n    command: echo made > gen.txt\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert ac.compile_repo(tmp_path) == 0 and (tmp_path / "gen.txt").read_text() == "made\n"
