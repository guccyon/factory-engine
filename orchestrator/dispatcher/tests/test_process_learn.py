"""工程の信号（ADR 0027 の 3）: 集め方・束ね方・分析役への回し方。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import case
    import process_learn as pl

R = "https://github.com/o/r/pull/"


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def job(cur, ticket: int, cost: float | None = None, days_ago: float = 1) -> int:
    cur.execute("insert into jobs(correlation_id, repo, issue_number, state) values (%s,'o/r',%s,'succeeded') returning id",
                (f"github:o/r#{ticket}", ticket))
    jid = cur.fetchone()["id"]
    if cost is not None:
        ev(cur, "job.finished", ticket, {"state": "succeeded", "cost_usd": cost}, jid, days_ago)
    return jid


def ev(cur, name, ticket, payload, job_id=None, days_ago: float = 1):
    cur.execute("insert into events(name, correlation_id, job_id, occurred_at, payload) values (%s,%s,%s, now() - %s * interval '1 day', %s)",
                (name, f"github:o/r#{ticket}" if ticket else None, job_id, days_ago, Jsonb(payload)))


def conflict_job(cur, ticket, files, cost, days_ago=1):
    jid = job(cur, ticket, cost, days_ago)
    ev(cur, "continue.enqueued", ticket, {"reason": "conflict", "pr_url": R + str(ticket)}, jid, days_ago)
    if files is not None:
        pl.record_conflict_files(cur, jid, f"github:o/r#{ticket}", files)
    return jid


def test_parse_conflict_files():
    assert pl.parse_conflict_files("x\n[worker] continue conflict pr=#3 branch=b conflicts=[CHANGELOG.md a.ts ]\n") == ["CHANGELOG.md", "a.ts"]
    # 2026-11-03 から: 機械的に解いた後の conflicts は空、seen に解く前のファイル
    assert pl.parse_conflict_files("[worker] continue conflict pr=#3 branch=b conflicts=[] seen=[CHANGELOG.md ]") == ["CHANGELOG.md"]
    assert pl.parse_conflict_files("[worker] continue conflict pr=#3 branch=b conflicts=[] seen=[]") == []
    assert pl.parse_conflict_files("[worker] continue ci_failed pr=#3 branch=b conflicts=[]") is None


def test_conflict_fix_bundles_by_file_and_splits_cost(cur):
    for t in range(1, 11):
        conflict_job(cur, t, ["CHANGELOG.md"], 4.0)
    conflict_job(cur, 20, ["CHANGELOG.md", "src/a.ts"], 10.0)
    conflict_job(cur, 21, None, 1.0)                 # 記録がない
    conflict_job(cur, 22, [], 0.0)                   # 衝突なしで取り込めた
    conflict_job(cur, 23, ["CHANGELOG.md"], 99.0, days_ago=9)   # 窓の外（前の窓）
    r = pl.retrospect(cur)
    by = {c["signature"]: c for c in r["clusters"]}
    cl = by["process:conflict_fix:CHANGELOG.md"]
    assert (cl["signals"], cl["tickets"], cl["cost"], cl["route"]) == (11, 11, 45.0, True)
    assert cl["prev"] == {"signals": 1, "cost": 99.0}
    assert by["process:conflict_fix:src/a.ts"]["cost"] == 5.0 and not by["process:conflict_fix:src/a.ts"]["route"]
    assert by[f"process:conflict_fix:{pl.UNKNOWN}"]["signals"] == 1
    assert by[f"process:conflict_fix:{pl.NO_CONFLICT}"]["signals"] == 1
    assert r["clusters"][0]["signature"] == "process:conflict_fix:CHANGELOG.md"   # 費用の多い順


def test_route_by_cost_alone(cur):
    conflict_job(cur, 1, ["openapi.yaml"], 60.0)
    assert pl.retrospect(cur)["clusters"][0]["route"] is True


def test_rework_needs_three_on_one_ticket(cur):
    for reason in ("ci_failed", "ci_failed", "incomplete"):
        ev(cur, "continue.enqueued", 1, {"reason": reason}, job(cur, 1, 2.0))
    ev(cur, "job.enqueued", 2, {"by": "rerun"}, job(cur, 2, 1.0))
    ev(cur, "continue.enqueued", 2, {"reason": "conflict"}, job(cur, 2, 1.0))   # 衝突は rework に数えない
    ev(cur, "job.preempted", 2, {"preempted_for": 1}, job(cur, 2, 1.0))
    sigs = [(c["signature"], c["signals"], c["cost"]) for c in pl.retrospect(cur)["clusters"] if c["signal"] == "rework"]
    assert sigs == [("process:rework:ci_failed", 1, 6.0)]


def test_cost_outlier_against_median_of_same_type(cur):
    for t, cost in [(1, 1.0), (2, 1.0), (3, 1.2), (4, 0.9), (5, 5.0)]:
        job(cur, t, cost)
        cur.execute("insert into artifacts(type, correlation_id, payload) values ('PR', %s, %s)",
                    (f"github:o/r#{t}", Jsonb({"pr_url": R + str(t), "change_types": ["code_change"]})))
        ev(cur, "pr.merged", t, {"pr_url": R + str(t)})
    out = [c for c in pl.retrospect(cur)["clusters"] if c["signal"] == "cost_outlier"]
    assert [(c["signature"], c["signals"], c["cost"]) for c in out] == [("process:cost_outlier:code_change", 1, 5.0)]
    assert out[0]["samples"][0]["detail"] == {"median": 1.0, "x": 5.0}


def test_wait_and_stoppage(cur):
    def stop(key, category, hours, ticket=1, closed=True):
        ev(cur, "stoppage.opened", ticket, {"key": key, "category": category, "started_at": (datetime.now(timezone.utc) - timedelta(hours=hours + 1)).isoformat()})
        if closed:
            ev(cur, "stoppage.closed", ticket, {"key": key, "ended_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()})
    stop("ci:" + R + "1", "ticket", 4)          # 待ち（3 時間以上）
    stop("ci:" + R + "2", "ticket", 1)          # 短い停止は stoppage
    stop("judge:" + R + "3", "human", 10)       # 人の判断待ちは数えない
    stop("idle:20261102", "idle", 10, ticket=None)
    stop("stuck:9", "factory-bug", 2, ticket=None)
    got = sorted((c["signature"], c["signals"]) for c in pl.retrospect(cur)["clusters"])
    assert got == [("process:stoppage:ci", 1), ("process:stoppage:stuck", 1), ("process:wait:ci", 1)]


def test_routed_bundle_becomes_case_incident_once_a_week(cur):
    for t in range(1, 11):
        conflict_job(cur, t, ["CHANGELOG.md"], 4.0)
    inc = case.pick_incidents(cur, limit=3)
    assert len(inc) == 1 and inc[0]["kind"] == "process" and inc[0]["signature"] == "process:conflict_fix:CHANGELOG.md"
    inp = case.build_input(cur, inc[0])
    assert inp["process"]["bundle"]["signals"] == 10 and inp["process"]["other_bundles"][0]["signature"] == inc[0]["signature"]
    assert "bundle" not in inp["incident"] and inp["timeline"]
    ev(cur, "case.enqueued", None, {"incident": inc[0]["key"], "kind": "process", "signature": inc[0]["signature"]})
    assert case.pick_incidents(cur, limit=3) == []


def test_record_once_per_day(cur):
    conflict_job(cur, 1, ["CHANGELOG.md"], 4.0)
    r = pl.retrospect(cur)
    assert pl.record_retrospective(cur, r, "2026-11-03") is True
    assert pl.record_retrospective(cur, r, "2026-11-03") is False
    lines = pl.report_lines(r)
    assert any("process:conflict_fix:CHANGELOG.md" in l for l in lines)
