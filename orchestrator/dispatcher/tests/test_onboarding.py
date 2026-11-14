"""オンボーディングの判定の記録と見せ方（onboarding.py・products_page.py、ADR 0045 の追記「操業コンソールに出すもの」）:
記録の確かめ（鍵らしい欄を捨てる）・製品ごとの最新と推移・受信トレイの準備のお願いと稼働 ready の承認・製品のページの断片・設定のサービスとの連携。

例のデータは 1 号機（rally）の判定から作る（先方の情報を載せない）。DB を使うものは TEST_DATABASE_URL の Postgres で（なければ飛ばす）
"""
import copy
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
import onboarding  # noqa: E402
import products_page  # noqa: E402
import readiness  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
DEFS = readiness.load([ROOT / "definitions" / readiness.FILE])
URL = os.environ.get("TEST_DATABASE_URL")
L1_L4 = ["repos-readable", "takeover-written", "inventory-filled", "analysis-matched", "drafts-exist", "reading-confirmed",
         "env-up", "tests-sorted", "screens-captured", "coexist-set", "small-merged", "bundle-merged"]

RALLY = {
    "product": "guccyon/rally", "commit": "1b38de9", "by": "建設者",
    "levels": {"p-repo": 3, "p-backlog": 3, "p-notify": 3, "p-design": 3, "p-docs": 2, "p-prod": 3, "p-store": 1, "p-monitor": 1,
               "o-env": {"level": 3, "tool": "factoryctl env up", "by": "建設者"}, "o-tests": 3, "o-screens": 3, "o-api": 3, "o-stack": 2,
               "o-rules": 3, "o-glossary": 3},
    "met": L1_L4,
    "stack": {"repos": [{"repo": "guccyon/rally apps/backend", "languages": ["Rust"], "frameworks": ["axum"], "tests": ["cargo test"]}],
              "services": [{"name": "ビデオ通話", "role": "ライブレッスン", "repos": ["backend"], "sandbox": "unknown", "handling": "mock",
                            "api_key": "sk-should-not-stay"}]},
    "sources": [{"service": "GitHub", "use": "バックログ", "kind": "account", "state": "connected", "expires_at": "2026-11-24",
                 "token": "ghp_should_not_stay_aaaaaaaaaaaaaaaa"}],
}


def at_l3() -> dict:
    """L3 まで届き、開発を始める必須はそろったが、警告が残る製品（稼働 ready の承認を出す）"""
    d = copy.deepcopy(RALLY)
    d["product"] = "example/sample"
    d["met"] = L1_L4[:9]
    d["levels"] = {"p-repo": 2, "p-backlog": 2, "p-notify": 2, "o-env": 2, "o-tests": 1}
    return d


def test_normalize_checks_and_drops_secret_fields():
    rec, probs = onboarding.normalize(DEFS, RALLY)
    assert probs == []
    assert rec["levels"]["o-env"] == 3 and rec["evidence"]["o-env"] == {"tool": "factoryctl env up", "by": "建設者"}
    assert "api_key" not in rec["stack"]["services"][0] and "token" not in rec["sources"][0]   # 鍵らしい欄は受け取らない
    assert "should_not" not in str(rec)
    bad = {**RALLY, "product": "rally", "levels": {"p-repo": 4, "nope": 1}, "met": ["nope"],
           "stack": {"services": [{"name": "x", "sandbox": "maybe"}]}, "sources": [{"service": "y", "state": "odd"}]}
    _, probs = onboarding.normalize(DEFS, bad)
    assert any("owner/repo" in p for p in probs) and any("p-repo" in p for p in probs) and any("nope" in p for p in probs)
    assert any("sandbox" in p for p in probs) and any("state" in p for p in probs)
    yes = {**RALLY, "stack": {"services": [{"name": "s3", "sandbox": True}, {"name": "x", "sandbox": False}]}}   # YAML の yes / no
    rec, probs = onboarding.normalize(DEFS, yes)
    assert probs == [] and [s["sandbox"] for s in rec["stack"]["services"]] == ["yes", "no"]


def test_judge_all_for_rally():
    rec, _ = onboarding.normalize(DEFS, RALLY)
    res = onboarding.judge_all(DEFS, rec["levels"], rec["met"])
    assert res["ladder"]["reached"] == 4 and res["ladder"]["next"] == 5
    assert res["targets"]["dev"]["verdict"] == "enough"
    assert res["targets"]["store"]["verdict"] == "blocked" and res["targets"]["ops"]["verdict"] == "blocked"


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


def test_record_latest_and_history(cur):
    rid, probs = onboarding.record(cur, DEFS, RALLY, "test")
    assert probs == [] and rid
    later = copy.deepcopy(RALLY)
    later["levels"]["p-monitor"] = 2
    onboarding.record(cur, DEFS, later, "test")
    assert onboarding.record(cur, DEFS, {"product": "x"}, "test")[0] is None
    [r] = onboarding.latest(cur)
    assert r["levels"]["p-monitor"] == 2 and r["recorded_by"] == "test"
    assert len(onboarding.history(cur, "guccyon/rally")) == 2
    [s] = onboarding.summaries(cur, DEFS)
    assert s["past_ready"] and not s["approved"] and s["reached"] == 4
    assert s["weakest"]["verdict"] == "blocked"
    p = onboarding.page(cur, DEFS, "guccyon/rally")
    assert [x["state"] for x in p["rungs"]] == ["reached"] * 4 + ["next"]
    assert len(p["targets"]["ops"]["trend"]) == 2
    assert p["stack"]["services"][0]["sandbox_ja"] == "不明" and p["stack"]["services"][0]["handling_ja"] == "模擬で置き換える"
    assert onboarding.page(cur, DEFS, "no/such") is None
    [c] = onboarding.sources(cur)
    assert c["service"] == "GitHub" and c["state_ja"] == "つながっている" and c["product"] == "guccyon/rally"


def test_inbox_asks_wait_for_remeasure(cur):
    import inbox
    onboarding.record(cur, DEFS, RALLY, "test")
    xs = {i["key"]: i for i in inbox.items(cur) if i["kind"] == "readiness_ask"}
    assert set(xs) == {"readiness:ask:guccyon/rally:ship", "readiness:ask:guccyon/rally:store", "readiness:ask:guccyon/rally:ops"}
    ops = xs["readiness:ask:guccyon/rally:ops"]
    assert ops["who"] == "owner" and ops["detail"][0]["label"] == "監視とログ（必須）"
    assert "今: 画面の画像 → 目標: ＋アラートの受け口" in ops["detail"][0]["text"]
    assert inbox.answer(cur, ops["key"], "cannot", "owner", "console")[0] == "invalid"   # 渡せない理由は必須
    assert inbox.answer(cur, ops["key"], "handed", "owner", "console", note="Grafana を読み取りで")[0] == "answered"
    assert ops["key"] not in {i["key"] for i in inbox.items(cur)}                        # 測り直すまで回答済（反映待ち）
    assert any(i["key"] == ops["key"] and i["answered"] for i in inbox.items(cur, include_answered=True))
    # 測り直して監視が上がると、運用のお願いから監視が消え、件はまた開く
    later = copy.deepcopy(RALLY)
    later["levels"]["p-monitor"] = 3
    onboarding.record(cur, DEFS, later, "test")
    again = next(i for i in inbox.items(cur) if i["key"] == ops["key"])
    assert [d["label"] for d in again["detail"]] == ["既存の文書（Confluence など）", "技術スタックと外部サービスの地図"]
    # 全部そろえば件は閉じる
    full = copy.deepcopy(later)
    full["levels"].update({"p-store": 3, "o-stack": 3, "p-docs": 3})
    onboarding.record(cur, DEFS, full, "test")
    assert not [i for i in inbox.items(cur) if i["kind"] == "readiness_ask"]


def test_ready_approval_only_between_l3_and_approval(cur):
    import inbox
    onboarding.record(cur, DEFS, RALLY, "test")
    assert not [i for i in inbox.items(cur) if i["kind"] == "readiness_ready"]   # L4 に届いた製品には出さない
    d = at_l3()
    onboarding.record(cur, DEFS, d, "test")
    [it] = [i for i in inbox.items(cur) if i["kind"] == "readiness_ready"]
    assert it["key"] == "readiness:ready:example/sample" and "承知のうえで" in it["action"]
    assert it["answer"]["options"][0]["label"] == "承知のうえで始める"
    [s] = [x for x in onboarding.summaries(cur, DEFS) if x["product"] == "example/sample"]
    assert s["ready_ok"] and not s["past_ready"] and s["confirms"] == 0
    assert inbox.answer(cur, it["key"], "approve", "owner", "console")[0] == "answered"
    assert not [i for i in inbox.items(cur, include_answered=True) if i["kind"] == "readiness_ready"]
    [s] = [x for x in onboarding.summaries(cur, DEFS) if x["product"] == "example/sample"]
    assert s["approved"] and s["past_ready"]
    # 必須が欠けていれば出さない
    nd = at_l3()
    nd["product"] = "example/blocked"
    nd["levels"]["o-tests"] = 0
    onboarding.record(cur, DEFS, nd, "test")
    assert not [i for i in inbox.items(cur) if i["key"] == "readiness:ready:example/blocked"]


def test_product_page_fragments(cur):
    assert "まだない" in products_page.body_list([])
    onboarding.record(cur, DEFS, RALLY, "test")
    onboarding.record(cur, DEFS, at_l3(), "test")
    html = products_page.body_list(onboarding.summaries(cur, DEFS))
    assert 'href="/products/guccyon/rally"' in html and "example/sample" in html and "稼働 ready の承認待ち" in html
    page = products_page.body_product(onboarding.page(cur, DEFS, "guccyon/rally"))
    for text in ("オンボーディングのレベル", "準備度メーター", "技術スタックと外部サービス", "下書き（正本は製品のリポジトリ）",
                 "L5 任せられる に足りないもの", "事業主の確認", 'data-tgt-b="ops"', "ビデオ通話", "模擬で置き換える"):
        assert text in page, text
    assert "should_not" not in page
    sample = products_page.body_product(onboarding.page(cur, DEFS, "example/sample"))
    assert "/inbox/readiness%3Aready%3Aexample%2Fsample" in sample
    assert "判定の記録がない" in products_page.body_product(None, "no/such")


def test_console_routes(cur):
    os.environ.setdefault("DATABASE_URL", URL)
    import console
    from console_page import APP_PATH
    for p in ("/products", "/products/", "/products/guccyon/rally", "/settings/connectors"):
        assert APP_PATH.fullmatch(p), p
    for p in ("/products/body", "/products/a/b/c", "/api/products"):
        assert not APP_PATH.fullmatch(p), p
    onboarding.record(cur, DEFS, RALLY, "test")
    [s] = console.products(cur)
    assert s["product"] == "guccyon/rally"
    assert "準備度メーター" in console.product_body(cur, "guccyon/rally")
    assert "guccyon/rally" in console.product_body(cur, "")
