"""オンボーディングの準備度の判定（readiness.py、ADR 0045 の決定 8 と追記）:
定義の形・必須の欠けで始められない・割合の線・警告とお願いの並び・レベルの到達・稼働 ready の承認を出す条件"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import readiness as rd  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
DEFS = rd.load([ROOT / "definitions" / rd.FILE])
ALL_MET = {c["id"] for r in DEFS["ladder"] for c in r["conditions"]}


def full() -> dict:
    return {it["id"]: len(it["levels"]) - 1 for it in DEFS["items"]}


def test_definitions_shape():
    ids = [it["id"] for it in DEFS["items"]]
    assert len(ids) == len(set(ids))
    for it in DEFS["items"]:
        assert len(it["levels"]) == 4, it["id"]
        assert it["group"] in DEFS["groups"]
        assert it["req"], it["id"]
        for t, r in it["req"].items():
            assert t in DEFS["targets"]
            assert 0 <= r["min"] <= r["goal"] <= 3, it["id"]
            assert r["weight"] > 0
            assert not r["must"] or r["min"] > 0, f"{it['id']}: 必須なら最低は 1 以上"
    # どの仕事にも必須が 1 つはある（必須の無い仕事は始められないことが起きない）
    for t in DEFS["targets"]:
        assert any(it["req"].get(t, {}).get("must") for it in DEFS["items"]), t
    assert [r["level"] for r in DEFS["ladder"]] == [1, 2, 3, 4, 5]
    assert DEFS["ready_line_after"] == 3
    cond = [c["id"] for r in DEFS["ladder"] for c in r["conditions"]]
    assert len(cond) == len(set(cond))
    assert "below" not in DEFS["bands"][-1]


def test_nothing_is_blocked():
    j = rd.judge(DEFS, {}, "dev")
    assert j["verdict"] == "blocked"
    assert set(j["blocked"]) == {"p-repo", "p-backlog", "p-notify", "o-env", "o-tests"}
    assert rd.band_ja(DEFS, j["verdict"]) == "始められない"
    # お願いは必須の欠けが先
    assert [a["id"] for a in j["asks"]][:3] == ["p-repo", "p-backlog", "p-notify"]


def test_full_is_enough_with_no_warnings():
    for t in DEFS["targets"]:
        j = rd.judge(DEFS, full(), t)
        assert (j["verdict"], j["ratio"], j["blocked"], j["warnings"], j["asks"]) == ("enough", 1.0, [], [], [])


def test_musts_at_min_but_rest_empty_is_weak():
    lv = {"p-repo": 2, "p-backlog": 2, "p-notify": 2, "o-env": 2, "o-tests": 1}
    j = rd.judge(DEFS, lv, "dev")
    assert j["verdict"] == "weak" and not j["blocked"]
    assert rd.band_ja(DEFS, "weak") == "品質が出にくい"
    ids = [w["id"] for w in j["warnings"]]
    assert ids[0] in ("o-screens", "o-api", "o-rules")   # 目標までの差の大きい順
    assert "o-api" in ids and "o-api" not in [a["id"] for a in j["asks"]]   # お願いを持たない項目は警告だけ


def test_one_short_of_goal_is_ready():
    lv = full()
    lv.update({"o-screens": 1, "o-api": 1, "p-design": 1})
    j = rd.judge(DEFS, lv, "dev")
    assert j["verdict"] == "ready" and 0.7 <= j["ratio"] < 0.92
    assert {w["id"] for w in j["warnings"]} == {"o-screens", "o-api", "p-design"}
    # 少し欠けるだけなら「十分」のまま（警告は出る）
    lv = full()
    lv["o-rules"] = 2
    j = rd.judge(DEFS, lv, "dev")
    assert j["verdict"] == "enough" and [w["id"] for w in j["warnings"]] == ["o-rules"]


def test_only_items_for_the_target_count():
    # ストアに出すにはストア配信だけ見る。開発の項目が空でも関係ない
    assert rd.judge(DEFS, {"p-store": 3}, "store")["verdict"] == "enough"
    assert rd.judge(DEFS, {"p-store": 1}, "store")["blocked"] == ["p-store"]


def test_out_of_range_levels_are_clamped_and_unknown_target_rejected():
    assert rd.judge(DEFS, {"p-store": 9}, "store")["verdict"] == "enough"
    assert rd.judge(DEFS, {"p-store": -2}, "store")["verdict"] == "blocked"
    with pytest.raises(ValueError):
        rd.judge(DEFS, {}, "nope")


@pytest.mark.parametrize("met,reached,nxt", [
    (set(), 0, 1),
    ({"repos-readable", "takeover-written", "inventory-filled"}, 1, 2),
    ({"repos-readable", "takeover-written", "inventory-filled", "env-up", "tests-sorted", "screens-captured"}, 1, 2),  # L2 が欠けたら L3 を満たしても 1
    (ALL_MET, 5, None),
])
def test_ladder(met, reached, nxt):
    lad = rd.ladder(DEFS, met)
    assert (lad["reached"], lad["next"]) == (reached, nxt)
    if nxt:
        assert lad["missing"]


def test_ladder_missing_names_next_conditions():
    lad = rd.ladder(DEFS, {"repos-readable", "takeover-written", "inventory-filled", "analysis-matched"})
    assert [c["id"] for c in lad["missing"]] == ["drafts-exist", "reading-confirmed"]


def test_ready_line():
    l3 = {c["id"] for r in DEFS["ladder"] if r["level"] <= 3 for c in r["conditions"]}
    musts = {"p-repo": 2, "p-backlog": 2, "p-notify": 2, "o-env": 2, "o-tests": 1}
    assert rd.ready_line_ok(DEFS, musts, l3)                     # 警告が残っていても出す（始めるかは事業主）
    assert not rd.ready_line_ok(DEFS, {**musts, "p-repo": 1}, l3)  # 必須が欠けたら出さない
    assert not rd.ready_line_ok(DEFS, musts, l3 - {"screens-captured"})  # L3 に届かなければ出さない


def test_candidates_do_not_fail_in_shallow_paths():
    assert rd.candidates(Path("/app/readiness.py"))[-1] == Path("/definitions") / rd.FILE
