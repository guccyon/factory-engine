"""工場の事故の台帳と日次の決定論の見回り（factory_incident.py、ADR 0046）。

集計と台帳は TEST_DATABASE_URL の Postgres で確かめる（なければ飛ばす）。
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")

import factory_incident as fi  # noqa: E402

JST = timezone(timedelta(hours=9))
URL = os.environ.get("TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[3]
DEFS = yaml.safe_load((ROOT / "definitions" / "incident.yaml").read_text(encoding="utf-8"))


def test_values_match_definitions():
    assert list(DEFS["severity"]) == list(fi.SEVERITIES)
    assert DEFS["record_from"] == fi.OPEN_FROM[-1]
    ids = {v["id"] for v in DEFS["viewpoints"]}
    import inspect
    src = inspect.getsource(fi)
    for vp in ("PV-tokens-repeat", "PV-tokens-contention", "PV-tokens-self-feedback", "PV-tokens-double-pay", "PV-quota-contention", "PV-cloud-leak"):
        assert vp in ids and vp in src
    events = (ROOT / "definitions" / "events.yaml").read_text(encoding="utf-8")
    for name in fi.EVENTS:
        assert f"  {name}:" in events


@pytest.fixture
def cur(monkeypatch):
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    import psycopg
    from psycopg.rows import dict_row
    monkeypatch.setenv("BUDGET_WINDOWS_WEEKDAY", "07:100")
    monkeypatch.setenv("BUDGET_WINDOWS_WEEKEND", "07:100")
    monkeypatch.setattr(fi, "DEFS_PATH", str(ROOT / "definitions" / "incident.yaml"))
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)   # supervisor と同じ autocommit
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def job(cur, kind, at, cost, corr="github:o/r#1"):
    from psycopg.types.json import Jsonb
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state) values (%s,'o/r',1,%s,'succeeded') returning id", (corr, kind))
    jid = cur.fetchone()["id"]
    cur.execute("insert into events(name, occurred_at, job_id, correlation_id, payload) values ('job.finished',%s,%s,%s,%s)",
                (at, jid, corr, Jsonb({"state": "succeeded", "cost_usd": cost})))


def history(cur, now):
    """直近 14 日に、ふだんの費用（実装 1 チケット 1 日 $4、衝突の解消 $1）"""
    for d in range(1, 15):
        t = now - timedelta(days=d)
        job(cur, "implement", t, 4.0, corr=f"github:o/r#{100 + d}")
        job(cur, "continue", t, 1.0, corr=f"github:o/r#{100 + d}")


def test_quiet_day_leaves_no_signs(cur):
    now = datetime(2026, 11, 10, 4, 30, tzinfo=JST)   # 予算の日は 11/9 の 7:00 から
    history(cur, now)
    job(cur, "implement", datetime(2026, 11, 9, 9, 0, tzinfo=JST), 5.0)
    got = fi.daily_step(cur, now)
    assert got["day"] == "2026-11-09" and got["signs"] == [] and got["opened"] == []
    assert got["llm_tokens"]["cost_vs_daily_budget"] == 0.05
    assert got["external_api"]["measured"] is False
    assert fi.daily_step(cur, now) is None                       # 1 日 1 回
    assert fi.daily_step(cur, now.replace(day=11, hour=3)) is None   # 時刻の前は見ない


def test_runaway_conflict_day_opens_cases(cur):
    now = datetime(2026, 11, 10, 4, 30, tzinfo=JST)
    history(cur, now)
    for i in range(30):   # 衝突の解消が 1 チケットで暴走（$4 × 30 = $120、枠 $100 の 1.2 倍）
        job(cur, "continue", datetime(2026, 11, 9, 8, 0, tzinfo=JST) + timedelta(minutes=10 * i), 4.0, corr="github:o/r#7")
    got = fi.daily_step(cur, now)
    keys = {s["key"]: s for s in got["signs"]}
    assert keys["tokens:day"]["severity"] == "high"
    assert keys["tokens:kind:continue"]["severity"] == "medium"
    assert keys["tokens:ticket:github:o/r#7"]["facts"]["jobs"] == 30
    assert len(got["opened"]) == 3 and all(c.startswith("IC-20261110-") for c in got["opened"])
    cases = fi.list_cases(cur)
    assert {c["severity"] for c in cases} == {"high", "medium"}
    cur.execute("select count(*) as n from events where name='fincident.sign'")
    assert cur.fetchone()["n"] == 3
    # 同じ日にもう一度回しても、兆しも台帳も増えない
    cur.execute("delete from events where name='fincident.patrolled'")
    fi.daily_step(cur, now)
    cur.execute("select count(*) as n from events where name in ('fincident.sign','fincident.opened')")
    assert cur.fetchone()["n"] == 6


def test_quota_and_cloud_signs_stay_low(cur, monkeypatch):
    from psycopg.types.json import Jsonb
    import ephemeral
    now = datetime(2026, 11, 10, 4, 30, tzinfo=JST)
    cur.execute("insert into events(name, correlation_id, occurred_at, payload) values ('env.ephemeral_up','env:o/r#5',%s,%s)",
                (now - timedelta(hours=ephemeral.TTL_HOURS + 13), Jsonb({"repo": "o/r", "pr": 5, "slot": 1})))
    got = fi.daily_step(cur, now)
    cloud = [s for s in got["signs"] if s["resource"] == "cloud_metered"]
    assert len(cloud) == 1 and cloud[0]["severity"] == "low"
    assert got["opened"] == []   # low は台帳に開かない（人が開く）


def test_case_lifecycle(cur):
    cid = fi.open_case(cur, "factoryctl:human", "外部 API の Rate Limit でやり直しが続いた", "external_api", "low", {"usd": 3})
    assert cid.startswith("IC-")
    with pytest.raises(ValueError):
        fi.open_case(cur, "x", "t", "tokens", "high")
    with pytest.raises(ValueError):
        fi.open_case(cur, "x", "t", "llm_tokens", "big")
    assert fi.update_case(cur, cid, "factoryctl:human", mechanism="unbounded_repeat", fir="FIR-2026-0003", note="待たずに呼び直していた")
    c = fi.get_case(cur, cid)
    assert c["mechanism"] == "unbounded_repeat" and c["fir"] == "FIR-2026-0003" and len(c["history"]) == 1
    assert fi.close_case(cur, cid, "factoryctl:human", "直した")
    assert not fi.close_case(cur, cid, "factoryctl:human", "二度目")
    assert fi.list_cases(cur) == [] and fi.list_cases(cur, "all")[0]["status"] == "closed"
    assert not fi.update_case(cur, "IC-19990101-1", "x", note="ない")


def test_daily_report_lines(cur):
    sys.path.insert(0, str(ROOT / "health" / "daily"))
    import status_report
    now = datetime(2026, 11, 10, 4, 30, tzinfo=JST)
    for i in range(30):
        job(cur, "continue", datetime(2026, 11, 9, 8, 0, tzinfo=JST) + timedelta(minutes=i), 4.0, corr="github:o/r#7")
    fi.daily_step(cur, now)
    import psycopg
    with psycopg.connect(URL) as conn:
        iv = status_report.incident_view(conn, now + timedelta(minutes=5))
    lines = status_report.fmt_incident(iv)
    assert "枠の 120%" in lines[0]
    assert any("**[high/llm_tokens]" in x for x in lines)
    assert any(x.startswith("- 台帳で開いている: IC-") for x in lines)
    assert status_report.fmt_incident(None) == ["- 昨日から今日の見回りの記録はない"]
