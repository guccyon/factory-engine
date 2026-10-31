"""判断の委任の範囲（ADR 0025 の E4）: 見た目と印象（aesthetic）は judge が何と返しても人へ回す。DB は要らない。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")

import judgment  # noqa: E402


@pytest.mark.parametrize("verdict", ["adopt", "replace", None])
def test_aesthetic_is_always_escalated(verdict):
    assert judgment.delegated_verdict("aesthetic", verdict) == ("escalate", True)


def test_aesthetic_escalate_is_not_marked_forced():
    assert judgment.delegated_verdict("aesthetic", "escalate") == ("escalate", False)


@pytest.mark.parametrize("category", ["ui", "implementation", "inspection"])
def test_delegated_categories_keep_judge_verdict(category):
    assert judgment.delegated_verdict(category, "adopt") == ("adopt", False)


def test_parse_provisional_accepts_aesthetic():
    text = "### aesthetic: 見出しの書体\n- 選んだ: 明朝\n- 確信度: 0.4\n"
    [item] = judgment.parse_provisional(text)
    assert item["category"] == "aesthetic"
