"""学習の観測データの書き出し（読むだけ、製品で絞る、長い文は落とす）"""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    import learn_export
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def test_export_filters_by_repo_and_drops_long_text(cur):
    cur.execute("insert into events(name, correlation_id, payload) values "
                "('committee.decided','committee:https://github.com/o/r/pull/1',%s),"
                "('committee.enforce_skipped','x',%s),"
                "('supervisor.merged','github:o/r#2',%s),"
                "('supervisor.merged','github:x/y#3',%s),"
                "('job.started','github:o/r#2',%s)",
                (Jsonb({"pr_url": "https://github.com/o/r/pull/1", "outcome": "adopt", "votes": [{"reasons": ["長い"]}]}),
                 Jsonb({"repo": "o/r", "title": "担保: K-0004 を ci_check で決定論にする"}),
                 Jsonb({"repo": "o/r", "reason": "あ" * 400}), Jsonb({"repo": "x/y"}), Jsonb({})))
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind) values ('github:o/r#2','o/r',2,'body_fix')")
    jid = cur.execute("select id from jobs").fetchone()["id"]
    cur.execute("insert into artifacts(type, correlation_id, job_id, payload) values ('Judgment','github:o/r#2',%s,%s)",
                (jid, Jsonb({"stage": "judged", "category": "inspection", "verdict": "adopt", "defect": {"rubric_id": "OV-001", "shadow": True}})))
    out = learn_export.export(cur, datetime(2026, 1, 1, tzinfo=timezone.utc), "o/r")
    assert out["format"] == "factory.learning-observation/v1"
    assert [e["name"] for e in out["events"]] == ["committee.decided", "committee.enforce_skipped", "supervisor.merged"]
    assert out["events"][0]["payload"] == {"pr_url": "https://github.com/o/r/pull/1", "outcome": "adopt"}   # 票の理由は落とす
    assert out["events"][2]["payload"] == {"repo": "o/r"}   # 長い文は落とす
    assert out["judgments"] == [{**out["judgments"][0], "rubric": "OV-001", "verdict": "adopt", "shadow": True}]
    assert [(d["kind"], d["n"]) for d in out["jobs_daily"]] == [("body_fix", 1)]
    assert out["rule_stats"][0]["id"] == "OV-001" and out["criteria"]["rule_lifecycle"]["canary_days"] == 7
