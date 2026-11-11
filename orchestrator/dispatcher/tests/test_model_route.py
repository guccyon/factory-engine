"""実装の工程のモデルの振り分け（model_route.py。2026-11-08「確信度の高い工程から安いモデルを試す」）:
定義の写し・小さいチケットの条件・チケットごとに決まる側・最初の 1 回で決めて同じ側で流す・途中終了で既定に戻す"""
import os
import sys
from pathlib import Path

import pytest
import yaml
from psycopg.types.json import Jsonb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import model_route as mr  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
URL = os.environ.get("TEST_DATABASE_URL")


def test_copy_matches_definitions():
    agents = yaml.safe_load((ROOT / "definitions" / "agents.yaml").read_text(encoding="utf-8"))
    ex = agents["experiments"][mr.EXPERIMENT["id"]]
    assert {k: ex[k] for k in ("step", "tier", "share", "eligible", "back_to_default_on")} == {k: mr.EXPERIMENT[k] for k in ("step", "tier", "share", "eligible", "back_to_default_on")}
    assert ex["tier"] in agents["tiers"] and ex["step"] in agents["steps"]
    planner = yaml.safe_load((ROOT / "definitions" / "planner.yaml").read_text(encoding="utf-8"))
    assert tuple(planner["sizing"]["uncounted_layers"]) == mr.UNCOUNTED_LAYERS
    # 検査・judge・QA は Opus のまま（試しの対象にしない）
    for step in ("inspect", "judge", "qa"):
        assert agents["tiers"][agents["steps"][step]["tier"]]["model"].startswith("claude-opus")
    assert agents["tiers"][agents["steps"]["body_fix"]["tier"]]["model"].startswith("claude-haiku")


@pytest.mark.parametrize("size,ok,why", [
    ({"turns_band": "S", "layers": ["web"]}, True, ""),
    ({"turns_band": "S", "layers": ["docs"]}, True, ""),            # 文書だけ
    ({"turns_band": "S", "layers": []}, True, ""),                  # 試験だけ（数える層なし）
    ({"turns_band": "S", "layers": ["backend", "docs"]}, True, ""),
    ({"turns_band": "M", "layers": ["web"]}, False, "band"),
    ({"turns_band": "S", "layers": ["backend", "web"]}, False, "layers"),
    ({"turns_band": "S", "layers": ["db"]}, False, "excluded_layer"),
    ({"turns_band": "S", "layers": ["contract"]}, False, "excluded_layer"),
    (None, False, "no_size"),
])
def test_eligible(size, ok, why):
    assert mr.eligible(size) == (ok, why)


def test_bucket_is_stable_and_near_share():
    assert mr.bucket("github:o/app#1") == mr.bucket("github:o/app#1")
    n = sum(1 for i in range(2000) if mr.bucket(f"github:o/app#{i}") < 0.2)
    assert 300 < n < 500


def test_share_env(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTE_SHARE", "0")
    assert mr.share() == 0.0
    monkeypatch.setenv("MODEL_ROUTE_SHARE", "x")
    assert mr.share() == 0.2


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


def plan(cur, corr, size):
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values ('Plan',%s,'plan',%s)", (corr, Jsonb({"size": size})))


def corr_in(arm_trial: bool) -> str:
    return next(f"github:o/app#{i}" for i in range(1000) if (mr.bucket(f"github:o/app#{i}") < 0.2) == arm_trial)


def test_routes_once_and_sticks(cur):
    t, c = corr_in(True), corr_in(False)
    for corr in (t, c):
        plan(cur, corr, {"turns_band": "S", "layers": ["web"]})
    assert mr.tier_for(cur, t, "tier-large") == "tier-small"
    assert mr.tier_for(cur, c, "tier-large") == "tier-large"
    plan(cur, t, {"turns_band": "XL", "layers": ["web", "backend", "db"]})   # 後から見積もりが変わっても、決めた側で流す
    assert mr.tier_for(cur, t, "tier-large", reason="review_fix") == "tier-small"
    cur.execute("select correlation_id, payload from events where name='model.routed' order by id")
    rows = {r["correlation_id"]: r["payload"] for r in cur.fetchall()}
    assert rows[t]["arm"] == "trial" and rows[t]["eligible"] is True and rows[t]["share"] == 0.2
    assert rows[c]["arm"] == "control" and rows[c]["tier"] == "tier-large"


def test_out_when_not_small(cur):
    corr = corr_in(True)
    plan(cur, corr, {"turns_band": "M", "layers": ["web"]})
    assert mr.tier_for(cur, corr, "tier-large") == "tier-large"
    p = mr.routed(cur, corr)
    assert p["arm"] == "out" and p["why_not"] == "band"
    other = "github:o/app#9999"   # Plan がない（プランナーを通らない）チケット
    assert mr.tier_for(cur, other, "tier-large") == "tier-large" and mr.routed(cur, other)["why_not"] == "no_size"


def test_back_to_default_on_incomplete(cur):
    corr = corr_in(True)
    plan(cur, corr, {"turns_band": "S", "layers": ["web"]})
    assert mr.tier_for(cur, corr, "tier-large") == "tier-small"
    assert mr.tier_for(cur, corr, "tier-large", reason="incomplete") == "tier-large"
    assert mr.tier_for(cur, corr, "tier-large", reason="review_fix") == "tier-large"   # 戻したら戻したまま
    assert mr.tier_for(cur, corr, "tier-large", reason="incomplete") == "tier-large"
    cur.execute("select count(*) as n from events where name='model.routed_back'")
    assert cur.fetchone()["n"] == 1
    assert mr.routed(cur, corr)["arm"] == "trial"   # 比べるときは最初に決めた側で数える


def test_failure_falls_back_without_breaking_transaction(cur, monkeypatch):
    monkeypatch.setattr(mr, "latest_size", lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
    assert mr.tier_for(cur, "github:o/app#1", "tier-large") == "tier-large"
    cur.execute("select 1 as ok")
    assert cur.fetchone()["ok"] == 1


def test_continuation_uses_routed_tier(cur):
    import continuation
    corr = corr_in(True)
    issue = int(corr.rsplit("#", 1)[1])
    plan(cur, corr, {"turns_band": "S", "layers": ["web"]})
    assert mr.tier_for(cur, corr, "tier-large") == "tier-small"
    jid = continuation.enqueue(cur, repo="o/app", pr_number=5, pr_url="https://github.com/o/app/pull/5",
                               branch=f"factory/issue-{issue}-job-1", head_sha="abc", reason="ci_failed", by="test")
    cur.execute("select model_tier from jobs where id=%s", (jid,))
    assert cur.fetchone()["model_tier"] == "tier-small"


def test_table_copy_matches_definitions():
    wf = yaml.safe_load((ROOT / "definitions" / "workflow.yaml").read_text(encoding="utf-8"))
    assert wf["models"] == mr.MODEL_TABLE


def test_table_routes_small_light_regardless_of_share(cur, monkeypatch):
    # ADR 0038 の追記 2026-11-11: 帯 S・厚み light の実装は表で安いモデルに。試しの割合に依らない。途中終了したら既定へ戻す
    monkeypatch.setenv("MODEL_ROUTE_SHARE", "0")
    corr = corr_in(False)
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values ('Plan',%s,'plan',%s)",
                (corr, Jsonb({"size": {"turns_band": "S", "layers": ["web"]},
                              "workflow": {"steps": [{"step": "implement", "run": True, "effort": "light"}]}})))
    assert mr.tier_for(cur, corr, "tier-large") == "tier-small"
    cur.execute("select payload from events where name='model.routed' and correlation_id=%s", (corr,))
    p = cur.fetchone()["payload"]
    assert p["arm"] == "table" and p["effort"] == "light"
    assert mr.tier_for(cur, corr, "tier-large", reason="incomplete") == "tier-large"


def test_table_needs_light_and_small():
    assert mr.table_tier({"turns_band": "S", "layers": ["web"]}, "standard") is None
    assert mr.table_tier({"turns_band": "M", "layers": ["web"]}, "light") is None
    assert mr.table_tier({"turns_band": "S", "layers": ["db"]}, "light") is None   # db・契約は外す
    assert mr.table_tier({"turns_band": "S", "layers": ["web"]}, "light") == "tier-small"
