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


# 2026-11-02 改訂: 要素レベル（level=element）は、前例か定石があれば replace も含めて確信度 0.5 以上で judge が決める
@pytest.mark.parametrize("verdict", ["adopt", "replace"])
def test_element_level_is_delegated_with_lower_bar(verdict):
    assert judgment.delegated_verdict("aesthetic", verdict, 0.6, "既存の数値の列と同じ右寄せ（UI の定石）", "element") == (verdict, False)


@pytest.mark.parametrize("verdict,conf,precedent", [
    ("adopt", 0.4, "定石: 既存の部品を使い回す"),   # 要素レベルの下限未満
    ("replace", None, "定石"),                     # 確信度がない
    ("adopt", 0.9, ""),                            # 沿う先が空
    (None, 0.9, "定石"),                           # 裁きがない
])
def test_element_level_outside_delegation_is_escalated(verdict, conf, precedent):
    assert judgment.delegated_verdict("aesthetic", verdict, conf, precedent, "element") == ("escalate", True)


@pytest.mark.parametrize("level", ["experience", None])
def test_experience_level_keeps_previous_rule(level):
    assert judgment.delegated_verdict("aesthetic", "adopt", 0.6, "UXSpec の colors", level) == ("escalate", True)
    assert judgment.delegated_verdict("aesthetic", "replace", 0.95, "UXSpec の colors", level) == ("escalate", True)
    assert judgment.delegated_verdict("aesthetic", "adopt", 0.85, "UXSpec の colors", level) == ("adopt", False)


# ADR 0042: 内部実装で利用者に見える振る舞いが変わらず、後で選び直せるものは、judge が人に回しても推奨を工場が採る
@pytest.mark.parametrize("recommend", ["adopt", "replace"])
def test_no_visible_change_takes_recommend(recommend):
    assert judgment.no_visible_change_verdict("implementation", "escalate", 0.55, "none", True, recommend) == (recommend, True)


@pytest.mark.parametrize("category,conf,visible,reversible,recommend", [
    ("implementation", 0.4, "none", True, "adopt"),        # 確信度が下限未満
    ("implementation", None, "none", True, "adopt"),       # 確信度がない
    ("implementation", 0.9, "changes", True, "adopt"),     # 利用者に見える差がある
    ("implementation", 0.9, None, True, "adopt"),          # 書いていない
    ("implementation", 0.9, "none", False, "adopt"),       # 選び直しにくい
    ("implementation", 0.9, "none", None, "adopt"),        # 書いていない
    ("implementation", 0.9, "none", True, None),           # 推奨がない
    ("system_interface", 0.9, "none", True, "adopt"),      # 委任の外の類型
    ("data", 0.9, "none", True, "adopt"),
    ("ui", 0.9, "none", True, "adopt"),
])
def test_no_visible_change_outside_delegation_keeps_escalate(category, conf, visible, reversible, recommend):
    assert judgment.no_visible_change_verdict(category, "escalate", conf, visible, reversible, recommend) == ("escalate", False)


@pytest.mark.parametrize("verdict", ["adopt", "replace", None])
def test_no_visible_change_keeps_judge_decision(verdict):
    # judge が自分で決めたものは、印の有無にかかわらず今までどおり通す（本 ADR は人に回すものを減らすだけ）
    assert judgment.no_visible_change_verdict("implementation", verdict, 0.3, "changes", False, None) == (verdict, False)
