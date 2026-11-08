"""本文の直しに渡す製品の知見（ADR 0040 の決定 7 の 2）"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import body_fix  # noqa: E402


def test_no_section_without_file(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_FILE", raising=False)
    assert body_fix.knowledge_section() == ""
    monkeypatch.setenv("KNOWLEDGE_FILE", "/nonexistent/knowledge.md")
    assert body_fix.knowledge_section() == ""


def test_section_from_selected_file(monkeypatch, tmp_path):
    f = tmp_path / "knowledge.md"
    f.write_text("## K-0006\n本文はテンプレートの順に組み直す\n", encoding="utf-8")
    monkeypatch.setenv("KNOWLEDGE_FILE", str(f))
    s = body_fix.knowledge_section()
    assert "# この製品の知見（データ）" in s and "K-0006" in s and "目の前の事実を優先" in s


def test_section_is_capped(monkeypatch, tmp_path):
    f = tmp_path / "knowledge.md"
    f.write_text("x" * (body_fix.MAX_KNOWLEDGE + 500), encoding="utf-8")
    monkeypatch.setenv("KNOWLEDGE_FILE", str(f))
    assert body_fix.knowledge_section().count("x") == body_fix.MAX_KNOWLEDGE
