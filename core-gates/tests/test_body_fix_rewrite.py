"""PR 本文の説明の部分を作り直す（2026-11-10。追記では PR-002 を満たせず、同じ PR で繰り返して本文が膨らんだ）"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import body_fix  # noqa: E402

BLOATED = """Closes #621

ワーカーの要約

## 工場の記録
- job: 2074
- model: m

## 概要
古いまとめ 1

（この節は検査の指摘を受けて工場が本文に追記した）

<!-- factory:screen-capture -->
## 実装後の画面のキャプチャ
img
<!-- /factory:screen-capture -->

## 概要
古いまとめ 2

（この節は検査の指摘を受けて工場が本文に追記した）

## 続きのジョブの記録

<details><summary>既定ブランチの取り込みと衝突の解消（job 2106）</summary>
x
</details>
"""


def test_split_keeps_machine_parts_out_of_desc():
    p = body_fix.split_body(BLOATED)
    assert p["links"] == ["Closes #621"]
    assert "## 工場の記録" in p["protected"] and "## 続きのジョブの記録" in p["protected"] and "job 2106" in p["protected"]
    assert p["marked"] and "screen-capture" in p["marked"][0]
    assert "古いまとめ 1" in p["desc"] and "古いまとめ 2" in p["desc"] and "ワーカーの要約" in p["desc"]
    assert "工場の記録" not in p["desc"] and "Closes" not in p["desc"] and "screen-capture" not in p["desc"]
    assert p["prev_sha"] is None


def test_assemble_replaces_desc_and_keeps_protected_last():
    p = body_fix.split_body(BLOATED)
    desc = "## 概要\n新しい説明\n\n## 動作確認\nok"
    out = body_fix.assemble(p, desc)
    assert out.startswith("Closes #621\n")
    assert out.count("## 概要") == 1 and "古いまとめ" not in out
    assert out.index("screen-capture") < out.index("## 工場の記録") < out.index("## 続きのジョブの記録")
    # 続きのジョブは「## 続きのジョブの記録」を末尾とみて記録を足す
    assert out.rstrip().endswith("</details>")
    assert f"<!-- factory:body-fix sha={body_fix.fingerprint(desc)} -->" in out


def test_unchanged_desc_is_detected_after_rewrite():
    p = body_fix.split_body(BLOATED)
    out = body_fix.assemble(p, "## 概要\n新しい説明")
    # 続きのジョブが記録を足しても、説明の部分は変わっていない
    out2 = out + "<details><summary>続き（job 2136）</summary>\ny\n</details>\n"
    p2 = body_fix.split_body(out2)
    assert p2["prev_sha"] == body_fix.fingerprint(p2["desc"])
    assert "job 2136" in p2["protected"]
    # 人が説明を書き換えたら変わったとみる
    p3 = body_fix.split_body(out2.replace("新しい説明", "人が直した説明"))
    assert p3["prev_sha"] != body_fix.fingerprint(p3["desc"])


def test_rewrite_is_idempotent_on_layout():
    p = body_fix.split_body(BLOATED)
    out = body_fix.assemble(p, "## 概要\nA")
    p2 = body_fix.split_body(out)
    out2 = body_fix.assemble(p2, "## 概要\nA")
    assert out2 == out


def test_clean_desc_drops_protected_and_links_from_model():
    text = "Closes #1\n## 概要\nA\n## 工場の記録\n- job: 9\n## 動作確認\nok\n<!-- factory:x -->\n（この節は検査の指摘を受けて工場が本文に追記した）"
    d = body_fix.clean_desc(text)
    assert d == "## 概要\nA\n## 動作確認\nok"


def test_validate():
    assert body_fix.validate("## 概要\nA\n## 背景\nB") is None
    assert "level-2" in body_fix.validate("概要\nA")
    assert "duplicated" in body_fix.validate("## 概要\nA\n## 概要\nB")
    assert "too long" in body_fix.validate("## 概要\n" + "x" * body_fix.MAX_DESC_OUT)
