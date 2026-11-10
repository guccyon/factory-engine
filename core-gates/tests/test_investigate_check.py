"""工場の中での一次調査の決定論の検査（core-gates/investigate_check.py、ADR 0048）"""
import copy
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import investigate_check as iv  # noqa: E402

ENGINE = Path(__file__).resolve().parents[2]
DEFS = yaml.safe_load((ENGINE / "definitions" / "investigate.yaml").read_text(encoding="utf-8"))

CLEAN_ISSUE = """## 症状
操業レポートの危険の欄が、製品の作業に関係しない内部のジョブの停止で埋まり、製品の停止が見えなくなる。

## 条件
内部のジョブの停止が欄の上限を超える日。上限に対して 3 倍の停止があった。

## 関係
ADR 0046・ADR 0048 の決定 5。部品は操業レポートの組み立て（危険の欄の並べ方）。
"""

HANDOFF = {
    "id": "INV-2026-0001",
    "question": "なぜ危険の欄に製品の停止が出なかったのか",
    "scope": {"product": "owner/product", "engine_ref": "4cbecf0f6262cf1c34c0ef80a80f3b08484486e0", "window": ["2026-11-09T00:00Z", "2026-11-10T00:00Z"]},
    "facts": [{"ref": "stoppage:812", "what": "内部のジョブの停止が 12 件", "how": "investigate.stoppages を日付で"}],
    "hypotheses": [{"claim": "上限で落ちた", "confidence": "medium", "supported_by": ["stoppage:812"], "refuted_by": [],
                    "next_check": "並べ方のコードを見る"}],
    "ruled_out": [{"claim": "停止が記録されていない", "by": ["stoppage:812"]}],
    "not_looked_at": ["Engine のソース（レポートの組み立て）"],
    "cost": {"usd": 2.8, "turns": 40},
    "stop_reason": "needs_code",
}


def test_clean_issue_passes():
    assert iv.check_issue(CLEAN_ISSUE, ["owner/product", "プロダクト名"]) == []


def test_issue_rejects_customer_shapes():
    for text, label in [
        ("詳しくは https://example.com/x を見る", "URL"),
        ("rally#1031 が止まった", "番号の参照"),
        ("#1031 の停止", "Issue・PR の番号"),
        ("連絡先 someone@example.com", "メールアドレス"),
        ("電話 03-1234-5678 まで", "電話番号"),
        ("参照 job:12345 の結果", "記録の参照"),
        ("restricted:R-0003 を見た", "restricted の id"),
        ("費用は $2.8 だった", "生の金額"),
        ("2026-11-09 に起きた", "日付（月まで）"),
        ("mac-mini.local の上で", "ホスト名"),
        ("192.168.1.20 に接続", "IP アドレス"),
        ("token ghp_" + "a" * 36, "秘密情報"),
    ]:
        errs = iv.check_issue(text)
        assert any(e.startswith(label) for e in errs), (text, errs)


def test_issue_rejects_denied_names_case_insensitively():
    errs = iv.check_issue("Owner/Product の停止", ["owner/product"])
    assert any(e.startswith("製品・人の名前") for e in errs)


def test_issue_keeps_adr_numbers_and_loopback():
    assert iv.check_issue("ADR 0048 と ADR0046 に従う。127.0.0.1 の待ち受け") == []


def test_handoff_good():
    assert iv.check_handoff(HANDOFF) == []


def test_handoff_missing_and_bad_values():
    h = copy.deepcopy(HANDOFF)
    del h["facts"]
    h["id"] = "INV-26-1"
    h["stop_reason"] = "tired"
    h["hypotheses"][0]["confidence"] = "sure"
    h["hypotheses"][0]["supported_by"] = ["12345"]
    h["cost"] = {"usd": "a lot"}
    errs = iv.check_handoff(h)
    for want in ["facts がない", "id 'INV-26-1'", "stop_reason 'tired'", "confidence は", "supported_by の '12345'", "cost.usd は数", "cost.turns は数"]:
        assert any(want in e for e in errs), (want, errs)


def test_handoff_needs_at_least_one_fact_with_ref_shape():
    h = copy.deepcopy(HANDOFF)
    h["facts"] = []
    assert any("1 件以上" in e for e in iv.check_handoff(h))
    h["facts"] = [{"ref": "12345", "what": "x", "how": "y"}]
    assert any("種類:id の形" in e for e in iv.check_handoff(h))


def test_handoff_rejects_secrets():
    h = copy.deepcopy(HANDOFF)
    h["facts"][0]["what"] = "ログに sk-ant-" + "x" * 30
    assert any("秘密情報" in e for e in iv.check_handoff(h))


def test_cli(tmp_path):
    p = tmp_path / "body.md"
    p.write_text("rally の #12 が止まった", encoding="utf-8")
    assert iv.main(["--issue", str(p), "--deny", "rally"]) == 1
    p.write_text(CLEAN_ISSUE, encoding="utf-8")
    assert iv.main(["--issue", str(p)]) == 0
    hp = tmp_path / "inv.yaml"
    hp.write_text(yaml.safe_dump(HANDOFF, allow_unicode=True), encoding="utf-8")
    assert iv.main(["--handoff", str(hp)]) == 0
    assert iv.main(["--handoff", str(tmp_path / "none.yaml")]) == 2


def test_values_match_definitions():
    h = DEFS["handoff"]
    assert list(DEFS["stop_reasons"]) == iv.STOP_REASONS
    assert h["required"] == iv.HANDOFF_REQUIRED
    assert h["scope_keys"] == iv.SCOPE_KEYS
    assert h["fact_keys"] == iv.FACT_KEYS
    assert h["hypothesis_keys"] == iv.HYPOTHESIS_KEYS
    assert h["confidence"] == iv.CONFIDENCE
    assert h["cost_keys"] == iv.COST_KEYS
    assert h["id"] == iv.HANDOFF_ID.pattern
    assert h["ref"] == iv.REF.pattern


def test_definitions_shape():
    lim = DEFS["limits"]
    assert lim["per_case_usd"] <= lim["per_day_usd"]
    assert lim["over_budget"] == "handoff"
    src = DEFS["sources"]
    assert {"restricted", "desk_tokens"} <= set(src["db"]["exclude"])
    assert not set(src["db"]["views"]) & set(src["db"]["exclude"])
    assert all(not p.startswith(tuple(src["engine_docs"]["not"])) for p in src["engine_docs"]["paths"])
    for p in src["engine_docs"]["not"]:
        assert (ENGINE / p.rstrip("/")).exists(), p   # 読ませないと書いた場所が今もあるか（名前が変わったら直す）
    assert DEFS["issue_check"]["on_fail"] == "hold_for_provider"
    assert set(DEFS["outcomes"]) == {"identified", "outside", "unidentified"}
