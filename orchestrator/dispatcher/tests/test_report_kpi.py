"""週次の KPI の Report ライン（ADR 0043 の O2）と KPI の予測誤差（O3）: 測る依頼・shipper の測定・前回との比較・異常の Opportunity の下書きと受信トレイ・
見込みの採点・Learn の束・roadmap の数え方・開発状況レポートの節・プランナーの見込みの整え方。docker と git は使わない"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "core-gates"))
URL = os.environ.get("TEST_DATABASE_URL")
JST = timezone(timedelta(hours=9))
REPO = "o/app"

import plan  # noqa: E402

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import inbox
    import learn
    import report_kpi
    import report_product
    import ship
    import supervisor

KPI = {
    "environments": ["staging"], "service": "postgres", "command": ["psql", "-c", "select 1"],
    "metrics": [
        {"id": "slot_fill_rate", "ja": "コーチの予約枠充足率", "unit": "", "direction": "up_is_good", "expected": [0.3, 0.95]},
        {"id": "video_review_lead_time", "ja": "添削の返却リードタイム", "unit": "時間", "direction": "down_is_good", "change_alert": 0.5},
        {"id": "Bad-Id", "ja": "形が誤り"},
    ],
}


@pytest.fixture()
def cur():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def staged(cur, version="1.0.0"):
    rid = f"{REPO}@{version}"
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('release.created',%s,%s,%s)",
                (f"release:{rid}", Jsonb({"release_id": rid, "repo": REPO, "app_version": version, "prs": []}), f"release.created:{rid}"))
    ship.record(cur, "release.deployed", rid, {"repo": REPO, "env": "staging", "ok": True, "request_id": 0})
    return rid


def measure(cur, monkeypatch, values: dict, at: datetime | None = None) -> int:
    """依頼を書き、shipper の handle で測る（docker の代わりに出力を返す）"""
    import json
    ok, _ = ship.request_kpi(cur, REPO, "staging", "2026-W46", "t", "test")
    assert ok
    monkeypatch.setattr(ship, "source", lambda *a, **k: ("/nonexistent", "c"))
    monkeypatch.setattr(ship, "ship_config", lambda path: {"kpi": KPI})
    monkeypatch.setattr(ship, "sh", lambda args, **k: json.dumps(values))
    for ev in ship.pending(cur):
        ship.handle(cur, ev)
    cur.execute("select id from events where name='release.kpi_measured' order by id desc limit 1")
    mid = cur.fetchone()["id"]
    if at:
        cur.execute("update events set occurred_at=%s where id=%s", (at, mid))
    return mid


def test_kpi_metrics_shape():
    ms = ship.kpi_metrics(KPI, {"slot_fill_rate": 0.5, "video_review_lead_time": "x", "Bad-Id": 1}) if URL else None
    if ms is None:
        pytest.skip("TEST_DATABASE_URL がない")
    assert [m["id"] for m in ms] == ["slot_fill_rate", "video_review_lead_time"]   # 形の誤った id は落とす
    assert ms[0]["value"] == 0.5 and ms[0]["expected"] == [0.3, 0.95]
    assert ms[1]["value"] is None and ms[1]["change_alert"] == 0.5   # 数でない値は不明
    assert ship.kpi_config({"kpi": KPI}, "prod") is None and ship.kpi_config({"kpi": KPI}, "staging")


def test_request_needs_staging_and_week(cur):
    assert ship.request_kpi(cur, REPO, "staging", "2026-W46", "t", "test")[0] is False   # まだ版がない
    staged(cur)
    assert ship.request_kpi(cur, REPO, "staging", "46", "t", "test")[0] is False
    assert ship.request_kpi(cur, REPO, "staging", "2026-W46", "t", "test")[0] is True


def test_measure_failure_is_recorded_without_stoppage(cur, monkeypatch):
    staged(cur)
    ship.request_kpi(cur, REPO, "staging", "2026-W46", "t", "test")
    monkeypatch.setattr(ship, "source", lambda *a, **k: ("/nonexistent", "c"))
    monkeypatch.setattr(ship, "ship_config", lambda path: {})   # kpi がない製品
    for ev in ship.pending(cur):
        ship.handle(cur, ev)
    cur.execute("select payload from events where name='release.kpi_measured'")
    p = cur.fetchone()["payload"]
    assert p["ok"] is False and "kpi" in p["error"]
    cur.execute("select count(*) as n from stoppages")
    assert cur.fetchone()["n"] == 0
    assert ship.pending(cur) == [] and report_kpi.report_step(cur, log=lambda *a: None) == []


def test_weekly_report_opportunities_and_forecast_error(cur, monkeypatch):
    staged(cur)
    t0 = datetime(2026, 11, 2, 7, tzinfo=JST)
    measure(cur, monkeypatch, {"slot_fill_rate": 0.6, "video_review_lead_time": 10}, t0)
    first = report_kpi.report_step(cur, log=lambda *a: None)
    assert len(first) == 1 and first[0]["opportunities"] == [] and first[0]["scored"] == []   # 初回は前回がなく、値は目安の中
    # 前回から今回までに、見込みを書いたチケットの PR がマージされた
    corr = f"github:{REPO}#12"
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values ('Requirements', %s, 'plan', %s)",
                (corr, Jsonb({"acceptance_criteria": ["x"], "kpi_forecast": {"kpi": "slot_fill_rate", "change": 0.05, "why": "枠が見つけやすくなる"}})))
    cur.execute("insert into events(name, correlation_id, payload, occurred_at) values ('pr.merged', %s, %s, %s)",
                (corr, Jsonb({"pr_url": f"https://github.com/{REPO}/pull/13"}), t0 + timedelta(days=2)))
    measure(cur, monkeypatch, {"slot_fill_rate": 0.2, "video_review_lead_time": 20}, t0 + timedelta(days=7))
    out = report_kpi.report_step(cur, log=lambda *a: None)
    assert len(out) == 1
    rows = {r["id"]: r for r in out[0]["rows"]}
    assert {a["kind"] for a in rows["slot_fill_rate"]["anomalies"]} == {"range", "prev", "forecast"}
    assert [a["kind"] for a in rows["video_review_lead_time"]["anomalies"]] == ["prev"]   # 下がるのがよい値が倍になった
    assert len(out[0]["opportunities"]) == report_kpi.OPPORTUNITIES_MAX
    o = out[0]["opportunities"][0]
    assert set(o) >= {"title", "problem", "expected_effect"} and o["expected_effect"]["metric"] == "slot_fill_rate"
    assert o["expected_effect"]["direction"] == "up"
    # 見込みの採点（O3）
    cur.execute("select correlation_id, payload from events where name='kpi.forecast_scored'")
    s = cur.fetchall()
    assert len(s) == 1 and s[0]["correlation_id"] == corr
    assert s[0]["payload"]["forecast"] == 0.05 and round(s[0]["payload"]["actual"], 4) == -0.4 and round(s[0]["payload"]["error"], 4) == -0.45
    # 同じ測定には 1 回だけ
    assert report_kpi.report_step(cur, log=lambda *a: None) == []
    cur.execute("select type, count(*) as n from artifacts where type in ('KPIReport', 'Opportunity') group by type order by type")
    assert [(r["type"], r["n"]) for r in cur.fetchall()] == [("KPIReport", 2), ("Opportunity", 3)]
    cur.execute("select payload from artifacts where type='KPIReport' order by id desc limit 1")
    kr = cur.fetchone()["payload"]
    assert set(kr) >= {"period", "metrics", "diff_from_previous", "explanations"} and "PR 1 件" in kr["explanations"][0]
    # 改善の提案の口（O4 と共通）: opportunity.proposed と受信トレイのプロダクトオーナーの依頼。起票はしない
    import opportunity
    props = opportunity.recent(cur, REPO, report_kpi.SOURCE)
    assert len(props) == 3 and props[0]["theme"] == report_kpi.THEME and props[0]["week"] == "2026-W46"
    xs = [i for i in inbox.items(cur) if i.get("task_kind") == "opportunity"]
    assert len(xs) == 3 and xs[0]["who"] == "po"
    res, _ = inbox.answer(cur, xs[0]["key"], "seen", "t", "console")
    assert res == "answered"
    assert len([i for i in inbox.items(cur) if i.get("task_kind") == "opportunity"]) == 2
    # Learn: 教師信号 kpi_error が kpi:<id> で束になる
    retro = learn.retrospect(cur, now=t0 + timedelta(days=8))
    c = [c for c in retro["clusters"] if c["signature"] == "kpi:slot_fill_rate"]
    assert c and c[0]["by_kind"] == {"kpi_error": 1}
    learn.record_retrospective(cur, retro, "2026-11-10")
    # roadmap の数え方
    rm = {i["id"]: i for p in yaml.safe_load((ROOT / "definitions" / "roadmap.yaml").read_text(encoding="utf-8"))["phases"]
          for i in p["items"]}
    for k in ("report_line", "kpi_error_signal"):
        cur.execute(rm[k]["measure"]["db"]["sql"])
        assert list(cur.fetchone().values())[0] == 1, k


def test_product_report_section(cur, monkeypatch):
    staged(cur)
    t0 = datetime(2026, 11, 2, 7, tzinfo=JST)
    measure(cur, monkeypatch, {"slot_fill_rate": 0.6, "video_review_lead_time": 10}, t0)
    measure(cur, monkeypatch, {"slot_fill_rate": 0.1, "video_review_lead_time": 10}, t0 + timedelta(days=7))
    report_kpi.report_step(cur, log=lambda *a: None)
    rep = report_product.assess(report_product.build(cur, t0, t0 + timedelta(days=8), REPO))
    sec = [s for s in rep.sections if s.id == "kpi"][0]
    m = {i.label: i for i in sec.items if i.kind == "metric"}
    assert m["コーチの予約枠充足率"].level == "warn" and "目安の外" in m["コーチの予約枠充足率"].state
    assert m["添削の返却リードタイム"].level == "" and m["添削の返却リードタイム"].unit == "時間"
    assert any("週次の KPI" in f["text"] for f in rep.findings)
    assert "異常" in "\n".join(report_kpi.lines(report_kpi.latest(cur, REPO)))


def test_sections_copy_matches_definitions():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    d = yaml.safe_load((ROOT / "definitions" / "reports.yaml").read_text(encoding="utf-8"))["reports"]["report.product"]["sections"]
    assert [s["id"] for s in d] == list(report_product.SECTIONS)
    ev = yaml.safe_load((ROOT / "definitions" / "events.yaml").read_text(encoding="utf-8"))["events"]
    for n in ("release.kpi_requested", "release.kpi_measured", "report.kpi_weekly", "report.kpi_skipped", "kpi.forecast_scored"):
        assert n in ev, n
    assert "kpi_error" in yaml.safe_load((ROOT / "definitions" / "learning.yaml").read_text(encoding="utf-8"))["teacher_signals"]


def test_supervisor_requests_once_a_week(cur, monkeypatch):
    monkeypatch.setattr(supervisor, "KPI_WEEKLY", [REPO, "o/none"])
    supervisor.kpi_weekly_step(cur, datetime(2026, 11, 9, 6, tzinfo=JST))   # 月曜 7 時の前
    cur.execute("select count(*) as n from events where name like '%%kpi%%'")
    assert cur.fetchone()["n"] == 0
    staged(cur)
    for h in (7, 8):
        supervisor.kpi_weekly_step(cur, datetime(2026, 11, 9, h, tzinfo=JST))
    cur.execute("select name, payload->>'repo' as repo, payload->>'week' as week from events where name in ('release.kpi_requested', 'report.kpi_skipped') order by id")
    assert [(r["name"], r["repo"], r["week"]) for r in cur.fetchall()] == [
        ("release.kpi_requested", REPO, "2026-W46"), ("report.kpi_skipped", "o/none", "2026-W46")]
    supervisor.kpi_weekly_step(cur, datetime(2026, 11, 16, 7, tzinfo=JST))   # 翌週はまた測る
    cur.execute("select count(*) as n from events where name='release.kpi_requested'")
    assert cur.fetchone()["n"] == 2


def test_planner_kpi_forecast(tmp_path):
    (tmp_path / ".factory-config").mkdir()
    (tmp_path / ".factory-config" / "ship.yaml").write_text(yaml.safe_dump({"kpi": KPI}, allow_unicode=True), encoding="utf-8")
    f = plan.facts(tmp_path)
    assert f["kpi_ids"] == ["slot_fill_rate", "video_review_lead_time"]
    assert plan.kpi_forecast({"kpi": "slot_fill_rate", "change": 0.05, "why": "w"}, f) == {"kpi": "slot_fill_rate", "change": 0.05, "why": "w"}
    assert plan.kpi_forecast({"kpi": "nope", "change": 1, "why": "w"}, f) is None
    assert plan.kpi_forecast({"kpi": "slot_fill_rate", "change": True, "why": "w"}, f) is None
    defs = yaml.safe_load(plan.DEFS.read_text(encoding="utf-8"))
    out = {"requirements": {"acceptance_criteria": ["出る"], "non_goals": [], "success_definition": "出る",
                            "kpi_forecast": {"kpi": "nope", "change": 1, "why": "w"}}}
    req, _, marks = plan.upstream(out, f, "ready", "none", defs)
    assert "kpi_forecast" not in req and "unknown_kpi" in marks
    out["requirements"]["kpi_forecast"] = {"kpi": "video_review_lead_time", "change": -2, "why": "通知"}
    req, _, _ = plan.upstream(out, f, "ready", "none", defs)
    assert req["kpi_forecast"]["change"] == -2
    assert "kpi_forecast" in plan.schema(defs)["properties"]["requirements"]["properties"]
    assert "slot_fill_rate" in plan.prompt(defs, f, 1)
