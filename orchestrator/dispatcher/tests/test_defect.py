"""Defect の記録（defect.py、ADR 0028 の決定 5）: 定義の写し・1 回だけ残す・知らない値・記録の失敗で止めない"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import defect  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
URL = os.environ.get("TEST_DATABASE_URL")


def test_copy_matches_definitions():
    d = yaml.safe_load((ROOT / "definitions" / "defects.yaml").read_text(encoding="utf-8"))
    assert tuple(s["id"] for s in d["stages"]) == defect.FOUND_IN
    assert tuple(d["causes"]) == defect.CAUSED_IN
    assert set(defect.FIELDS) | {"key", "found_in", "source"} == set(d["fields"])
    ev = yaml.safe_load((ROOT / "definitions" / "events.yaml").read_text(encoding="utf-8"))["events"]
    assert ev["defect.recorded"]["required"] == ["key", "found_in", "source"]


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


def test_record_once_and_unknown_cause_is_null(cur):
    assert defect.record(cur, "ci", "https://github.com/o/app/pull/1:test", "pr_events", repo="o/app",
                         change="https://github.com/o/app/pull/1", caused_in="implement", viewpoint="ci:test") is True
    assert defect.record(cur, "ci", "https://github.com/o/app/pull/1:test", "pr_events") is False
    assert defect.record(cur, "qa", "k", "qa_line", caused_in="気分", reproduce=["a"] * 30) is True
    cur.execute("select payload from events where name='defect.recorded' order by id")
    a, b = [r["payload"] for r in cur.fetchall()]
    assert a["caused_in"] == "implement" and a["release_id"] is None and a["viewpoint"] == "ci:test"
    assert b["caused_in"] is None and len(b["reproduce"]) == 20
    with pytest.raises(ValueError):
        defect.record(cur, "どこか", "k", "t")
    assert defect.safe_record(cur, "どこか", "k", "t") is False   # 呼び出し元は止めない


def test_introduced_by_the_job_that_wrote_the_pr(cur):
    """混入させた工程・ジョブ・tier は、PR の最新の版を書いた実装のジョブから引く（モデルを比べる材料。2026-11-08）"""
    pr = "https://github.com/o/app/pull/7"
    for tier, state in (("tier-large", "succeeded"), ("tier-small", "succeeded"), ("tier-small", "failed")):
        cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, model_tier, state, finished_at, result) "
                    "values ('github:o/app#3','o/app',3,'implement',%s,%s,now(),%s)", (tier, state, defect.Jsonb({"pr_url": pr})))
    assert defect.record(cur, "ci", f"{pr}:test", "pr_events", change=pr, caused_in="implement") is True
    assert defect.record(cur, "qa", "q1", "qa_line") is True   # 変更が分からないものは null のまま
    cur.execute("select payload from events where name='defect.recorded' order by id")
    a, b = [r["payload"] for r in cur.fetchall()]
    assert (a["introduced_in"], a["introduced_job"], a["introduced_tier"]) == ("implement", 2, "tier-small")
    assert b["introduced_in"] is None and b["introduced_job"] is None
