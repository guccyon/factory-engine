"""製品の段階（ADR 0028 の決定 4、definitions/product_stages.yaml）: 定義の写し、manifest からの読み取り、運用開始の判定。"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")
DEFS = Path(__file__).resolve().parents[3] / "definitions" / "product_stages.yaml"

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    import psycopg
    import ship
    import supervisor
    from psycopg.rows import dict_row


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def test_constants_match_definitions():
    d = yaml.safe_load(DEFS.read_text(encoding="utf-8"))
    assert ship.DEFAULT_STAGE == d["default"]
    assert set(ship.STAGES) == set(d["stages"])
    for k, v in d["stages"].items():
        assert ship.STAGES[k]["ship_to"] == v["ship_to"], k
        assert ship.STAGES[k]["operation_started"] == v["operation_started"], k
        assert ship.STAGES[k]["ja"] == v["ja"], k


def test_operation_started_follows_stage():
    assert supervisor.operation_started({"product_stage": {"stage": "operating"}}) is True
    assert supervisor.operation_started({"product_stage": {"stage": "launch"}}) is False
    assert supervisor.operation_started({"product_stage": {"stage": "development"}, "autonomy": {}}) is False
    assert supervisor.operation_started({"autonomy": {"operation_started": True}}) is True   # 段階の欄がなかった頃の書き方


def test_dormant_always_approve_wakes_at_operating():
    """from: operation_start の常時承認は、段階が operating になってから効く"""
    au = {"matrix": {"code_change": {"merge": "auto"}}, "phase_grants": {"P0": {"merge": "auto"}},
          "always_human_approve": [{"id": "identity", "from": "operation_start"}]}
    dev = supervisor.decide(["code_change", "always:identity"], {"autonomy": au, "product_stage": {"stage": "development"}})
    op = supervisor.decide(["code_change", "always:identity"], {"autonomy": au, "product_stage": {"stage": "operating"}})
    assert dev["table"] == "auto" and op["table"] == "human_approve"
    assert "運用開始前" not in supervisor.judge_constraints(au, True)
    assert "運用開始前" in supervisor.judge_constraints(au, False)


def test_stage_step_reads_manifest_and_skips_unreadable(cur, monkeypatch):
    monkeypatch.setattr(supervisor, "REPOS", ["o/app", "o/engine"])
    monkeypatch.setattr(supervisor, "RELEASE_TRAIN", ["o/app"])
    calls = []

    def load(repo):
        calls.append(repo)
        if repo == "o/engine":
            raise RuntimeError("404")   # manifest を持たない（Engine）
        return {"product_stage": {"stage": "launch"}}

    monkeypatch.setattr(supervisor, "load_manifest", load)
    supervisor.stage_step(cur, {})
    assert calls == ["o/app", "o/engine"]                  # 列車と REPOS の重なりは 1 回だけ読む
    assert ship.stage_of(cur, "o/app")["stage"] == "launch"
    assert ship.stage_of(cur, "o/engine")["declared"] is None and ship.prod_held(cur, "o/engine")
    supervisor.stage_step(cur, {"o/app": {"product_stage": {"stage": "launch"}}})   # 同じ周回で読んだ manifest を使い回す
    cur.execute("select count(*) as n from events where name='product.stage_observed'")
    assert cur.fetchone()["n"] == 1
