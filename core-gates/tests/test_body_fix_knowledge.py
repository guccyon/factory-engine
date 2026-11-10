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



def test_rewrite_keeps_preamble_and_one_mark():
    """11-10: 追記をやめて書き直す。帰属の印（最初の見出しより前）は残し、古い追記の印は外し、印は 1 つだけ"""
    orig = "<!-- attribution -->\n_Requested by X_\n\n## 概要\n古い\n\n## 概要\n言い直し\n\n（この節は検査の指摘を受けて工場が本文に追記した）\n"
    out = body_fix.finalize(orig, "## 概要\nまとめた\n\n（この節は検査の指摘を受けて工場が本文に追記した）")
    assert out.startswith("<!-- attribution -->\n_Requested by X_\n\n## 概要\nまとめた")
    assert "追記した" not in out and out.count(body_fix.MARK) == 1
    again = body_fix.finalize(out, out)   # 2 回目に書き直しても、頭と印は重ならない
    assert again.count("<!-- attribution -->") == 1 and again.count(body_fix.MARK) == 1


def test_rewrite_empty_and_long_preamble():
    assert body_fix.finalize("## a\nb", "  ") == ""
    long_head = "x" * (body_fix.PREAMBLE_MAX + 1) + "\n## a"
    assert body_fix.preamble(long_head) == "" and body_fix.preamble("見出しなし") == ""


def test_body_sha_ignores_crlf_and_edges():
    assert body_fix.body_sha("a\r\nb\n") == body_fix.body_sha("a\nb")
