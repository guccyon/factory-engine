"""工場の事故の台帳と日次の決定論の見回り（factory_incident.py、ADR 0046）。

集計と台帳は TEST_DATABASE_URL の Postgres で確かめる（なければ飛ばす）。
"""
import json
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


# ------------------------------------------------------------------ 週次の見回り（ADR 0046 の順番 3・4）
def test_weekly_due_and_deny_names():
    assert fi.weekly_due(datetime(2026, 11, 15, 11, 59, tzinfo=JST), DEFS) is None        # 日曜 12 時の前
    assert fi.weekly_due(datetime(2026, 11, 15, 12, 0, tzinfo=JST), DEFS) == "2026-W46"
    assert fi.weekly_due(datetime(2026, 11, 15, 12, 0, tzinfo=JST), {"committee": {"patrol": {"weekly": {"enabled": False}}}}) is None
    assert fi.deny_names(["acme/shop-api", "acme/x"]) == ["acme/shop-api", "acme", "shop-api", "acme/x"]


def test_weekly_enqueue_once(cur):
    now = datetime(2026, 11, 15, 12, 30, tzinfo=JST)
    history(cur, now)
    for i in range(30):
        job(cur, "continue", datetime(2026, 11, 14, 8, 0, tzinfo=JST) + timedelta(minutes=i), 4.0, corr="github:o/r#7")
    fi.daily_step(cur, datetime(2026, 11, 15, 4, 30, tzinfo=JST))
    got = fi.enqueue_weekly(cur, now, ["o/r"], DEFS)
    assert got["week"] == "2026-W46" and got["signs"] >= 1 and got["cases"] >= 1
    cur.execute("select kind, issue_number from jobs where id=%s", (got["job_id"],))
    assert cur.fetchone() == {"kind": "incident_review", "issue_number": 0}
    cur.execute("select payload from events where name='job.enqueued' and job_id=%s", (got["job_id"],))
    inp = cur.fetchone()["payload"]["input"]
    assert inp["top_tickets"][0]["correlation_id"] == "github:o/r#7" and inp["deny"] == ["o/r"]   # 3 文字未満の持ち主・名前は照合しない
    assert inp["external_api"] == {"measured": False} and inp["spent_usd"] > 100
    assert fi.enqueue_weekly(cur, now + timedelta(hours=1), ["o/r"], DEFS) is None     # 1 週 1 回


RESULT = {"facts": {"viewpoints": ["PV-tokens-double-pay"]},
          "report": {"summary": "やり直しの二重払いが目立つ週", "new_viewpoints": [], "findings": [
              {"viewpoint": "PV-tokens-double-pay", "title": "やり直しの二重払い", "resource": "llm_tokens", "mechanism": "double_pay", "severity": "medium",
               "is_incident": True, "case": "", "overlaps": "", "why": "引き継ぎを読まない", "evidence": "枠の 40%", "engine_fix": "引き継ぎから", "record": True},
              {"viewpoint": "free", "title": "ふだんの範囲", "resource": "llm_tokens", "mechanism": "unknown", "severity": "low",
               "is_incident": False, "case": "", "overlaps": "", "why": "", "evidence": "", "engine_fix": "", "record": False}]},
          "drafts": [{"id": "FIR-2026-0003", "case": "", "title": "やり直しの二重払い", "resource": "llm_tokens", "mechanism": "double_pay", "severity": "medium",
                      "ok": True, "yaml": "id: FIR-2026-0003\n", "issue": {"title": "事故の直し: やり直しの二重払い", "body": "本文"}},
                     {"id": "FIR-2026-0004", "case": "", "title": "落ちた", "ok": False, "check": ["impact.usd は使えない"], "review": None}],
          "cost_usd": 3.2}


def test_record_weekly_opens_outboxes_and_files(cur):
    s = fi.record_weekly(cur, 99, "2026-W46", json.loads(json.dumps(RESULT)))
    assert len(s["opened"]) == 1 and s["outboxed"] == ["FIR-2026-0003"] and s["issues_proposed"] == ["FIR-2026-0003"]
    assert s["rejected"][0]["check"] == ["impact.usd は使えない"]
    c = fi.get_case(cur, s["opened"][0])
    assert c["mechanism"] == "double_pay" and c["fir"] == "FIR-2026-0003" and c["by"] == "committee.weekly"
    # 同じ週をもう一度記録しても増えない
    fi.record_weekly(cur, 99, "2026-W46", json.loads(json.dumps(RESULT)))
    cur.execute("select count(*) as n from events where name in ('fincident.outboxed','fincident.reviewed','fincident.opened')")
    assert cur.fetchone()["n"] == 3
    # 送り出し口: 台帳の id は配らない
    ob = fi.outbox(cur)
    assert [x["id"] for x in ob] == ["FIR-2026-0003"] and "case" not in ob[0]
    # Engine の改善 Issue: 1 回だけ起票し、台帳に URL を足す
    posted = []

    def gh(method, path, body=None):
        if method == "GET":
            return []
        posted.append(body)
        return {"html_url": "https://github.com/e/engine/issues/1"}
    assert fi.file_issues(cur, gh, "e/engine", "factory:process-proposal") == ["https://github.com/e/engine/issues/1"]
    assert fi.file_issues(cur, gh, "e/engine", "factory:process-proposal") == [] and len(posted) == 1
    assert fi.get_case(cur, s["opened"][0])["engine_issue"] == "https://github.com/e/engine/issues/1"
    assert fi.take(cur, "FIR-2026-0003", "factoryctl:human") and not fi.take(cur, "FIR-2026-0003", "x") and fi.outbox(cur) == []
    assert not fi.take(cur, "FIR-2026-9999", "x")


def test_open_duplicate_issue_is_pointed(cur):
    fi.record_weekly(cur, 99, "2026-W46", json.loads(json.dumps(RESULT)))
    gh = lambda m, p, b=None: [{"title": "事故の直し: やり直しの二重払い", "html_url": "https://github.com/e/engine/issues/7"}] if m == "GET" else (_ for _ in ()).throw(AssertionError("起票しない"))
    assert fi.file_issues(cur, gh, "e/engine", "l") == ["https://github.com/e/engine/issues/7"]
    cur.execute("select payload from events where name='fincident.issue_filed'")
    assert cur.fetchone()["payload"]["duplicate"] is True


def test_daily_report_shows_weekly_and_outbox(cur):
    sys.path.insert(0, str(ROOT / "health" / "daily"))
    import status_report
    import psycopg
    now = datetime(2026, 11, 16, 4, 30, tzinfo=JST)
    fi.daily_step(cur, now)
    fi.record_weekly(cur, 99, "2026-W46", json.loads(json.dumps(RESULT)))
    with psycopg.connect(URL) as conn:
        lines = status_report.fmt_incident(status_report.incident_view(conn, datetime.now(timezone.utc)))
    assert any(x.startswith("- 週次の見回り（2026-W46）: 所見 2 件・うち事故 1 件") for x in lines)
    assert any("送り出し口に公開待ちの事故記録の下書き 1 件" in x for x in lines)
