"""依頼者ごとの好み（ADR 0040 の決定 6）。DB は使わない"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import requester_prefs as rp  # noqa: E402


class Cur:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.sql = rows or [], fail, []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if self.fail and sql.startswith("select"):
            raise RuntimeError("relation requester_preferences does not exist")

    def fetchall(self):
        return self.rows


def test_applies_by_work_ancestors():
    assert rp.applies([], ["desk.converse"])
    assert rp.applies(["desk"], ["desk.converse"])
    assert rp.applies(["plan.route"], ["desk.converse", "plan.route"])
    assert not rp.applies(["analyze.report"], ["desk.converse", "plan.route"])


def test_for_job_filters_and_uses_opaque_ids():
    rows = [{"id": 3, "statement": "表より箇条書き", "work": [], "axes": {}, "verified_at": date(2026, 11, 8)},
            {"id": 7, "statement": "数字は税抜き", "work": ["analyze.report"], "axes": {}, "verified_at": date(2026, 11, 7)}]
    cur = Cur(rows)
    got = rp.for_job(cur, "o/r", "alice", ["desk.converse", "plan.route"])
    assert got == [{"id": "P-0003", "statement": "表より箇条書き", "work": [], "verified_at": "2026-11-08"}]
    assert "confidence='confirmed'" in cur.sql[1] and "status='active'" in cur.sql[1]


def test_for_job_without_requester_or_table():
    assert rp.for_job(Cur(), "o/r", None, ["desk.converse"]) == []
    cur = Cur(fail=True)
    assert rp.for_job(cur, "o/r", "alice", ["desk.converse"]) == []
    assert cur.sql[-1] == "rollback to savepoint requester_prefs"
