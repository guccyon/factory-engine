"""判断の委任の範囲（ADR 0025 の E4、2026-11-01 改訂）: 見た目と印象（aesthetic）は、既存の見た目か UXSpec に沿う
（precedent あり）adopt で確信度 0.8 以上のときだけ judge が決め、それ以外は人へ回す。DB は要らない。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")

import judgment  # noqa: E402


@pytest.mark.parametrize("verdict", ["adopt", "replace", None])
def test_aesthetic_without_precedent_is_escalated(verdict):
    assert judgment.delegated_verdict("aesthetic", verdict, 0.95) == ("escalate", True)


def test_aesthetic_following_precedent_is_delegated():
    assert judgment.delegated_verdict("aesthetic", "adopt", 0.9, "apps/admin-web/src/components/BookingList.tsx の状態のタグ") == ("adopt", False)


@pytest.mark.parametrize("verdict,conf,precedent", [
    ("adopt", 0.7, "UXSpec の colors"),     # 確信度が下限未満
    ("adopt", None, "UXSpec の colors"),    # 確信度がない
    ("adopt", 0.9, "  "),                   # 沿う先が空
    ("replace", 0.95, "UXSpec の colors"),  # 案を替えるのは人
])
def test_aesthetic_outside_delegation_is_escalated(verdict, conf, precedent):
    assert judgment.delegated_verdict("aesthetic", verdict, conf, precedent) == ("escalate", True)


def test_aesthetic_escalate_is_not_marked_forced():
    assert judgment.delegated_verdict("aesthetic", "escalate") == ("escalate", False)


@pytest.mark.parametrize("category", ["ui", "implementation", "inspection"])
def test_delegated_categories_keep_judge_verdict(category):
    assert judgment.delegated_verdict(category, "adopt") == ("adopt", False)


def test_parse_provisional_accepts_aesthetic():
    text = "### aesthetic: 見出しの書体\n- 選んだ: 明朝\n- 確信度: 0.4\n"
    [item] = judgment.parse_provisional(text)
    assert item["category"] == "aesthetic"
