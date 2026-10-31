"""人の選択と違和感の印（ADR 0025 の P4-7）。

形の検査は DB なしで、記録は TEST_DATABASE_URL の Postgres で確かめる（なければ飛ばす）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")

import choice  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
OPTS = [{"id": "A", "label": "確認の段", "image_key": "choices/1/a.png"}, {"id": "B", "label": "並べる", "image_key": "choices/1/b.png"}]


def test_validate_rejects_bad_shapes():
    assert choice.validate("nope", OPTS)
    assert choice.validate("design_variants", OPTS[:1])                       # 案の選択は 2 案以上
    assert choice.validate("design_variants", [OPTS[0], dict(OPTS[1], id="A")])  # id の重複
    assert choice.validate("design_variants", [OPTS[0], dict(OPTS[1], image_key="../etc/passwd")])
    assert choice.validate("design_variants", [OPTS[0], dict(OPTS[1], image_key="other/x.png")])
    assert choice.validate("taste_sample", OPTS[:1]) is None
    assert choice.validate("design_variants", OPTS) is None


def test_signature_squeezes_spaces_and_clips():
    assert choice.signature("  余白が\n 詰まって  見える ") == "taste:余白が 詰まって 見える"
    assert len(choice.signature("あ" * 100)) == len("taste:") + 40


@pytest.fixture()
def cur():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def events(cur, name):
    cur.execute("select payload from events where name=%s order by id", (name,))
    return [r["payload"] for r in cur.fetchall()]


def test_choose_records_agreement_and_closes(cur):
    aid = choice.request(cur, "design_variants", "github:o/r#383", "送る前の確かめ方", OPTS, recommended="A", subject={"repo": "o/r", "ticket": "#383"})
    assert [c["id"] for c in choice.open_choices(cur)] == [aid]
    assert choice.choose(cur, aid, "B", "console:t", "人数が見えるほうがよい") == (True, "chosen")
    [made] = events(cur, "choice.made")
    assert made["option"] == "B" and made["agreed"] is False and made["reason"] == "人数が見えるほうがよい"
    assert choice.open_choices(cur) == []
    assert choice.choose(cur, aid, "A", "console:t") == (False, "already closed")


def test_choose_unknown_option(cur):
    aid = choice.request(cur, "design_variants", "c", "q", OPTS)
    assert choice.choose(cur, aid, "Z", "t") == (False, "no such option")
    assert choice.choose(cur, 9999, "A", "t") == (False, "no such choice")


def test_return_needs_reason(cur):
    aid = choice.request(cur, "design_variants", "c", "q", OPTS)
    assert choice.return_to_maker(cur, aid, "t", " ") == (False, "reason required")
    assert choice.return_to_maker(cur, aid, "t", "どれも文字が小さい") == (True, "returned")
    assert events(cur, "choice.returned")[0]["reason"] == "どれも文字が小さい"


def test_flag_is_a_teacher_signal_even_after_close(cur):
    aid = choice.request(cur, "taste_sample", "github:o/r#10", "この PR の画面", OPTS[:1], subject={"pr_url": "https://github.com/o/r/pull/10"})
    choice.choose(cur, aid, "A", "t")
    assert choice.flag(cur, aid, "", "t") == (False, "note required")
    assert choice.flag(cur, aid, "余白が詰まって見える", "t", "A") == (True, "flagged")
    [f] = events(cur, "taste.flagged")
    assert f["signature"] == "taste:余白が詰まって見える" and f["subject"]["pr_url"].endswith("/10")


def test_request_rejects_unknown_recommended(cur):
    with pytest.raises(ValueError):
        choice.request(cur, "design_variants", "c", "q", OPTS, recommended="Z")
