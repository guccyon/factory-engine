"""learn（ADR 0013）の Retrospective: 教師信号の集め方・束ね方・1 日 1 回の記録。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）:
  TEST_DATABASE_URL=postgresql://factory@localhost:5432/factory_test python -m pytest orchestrator/dispatcher/tests
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

    import learn

R = "https://github.com/o/r/pull/"


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def judged(cur, ticket, pr, category, verdict, rubric_id=None, days_ago=1):
    p = {"stage": "judged", "category": category, "verdict": verdict, "pr_url": R + str(pr),
         "defect": {"rubric_id": rubric_id, "location": "x.rs"} if rubric_id else None}
    cur.execute("insert into artifacts(type, correlation_id, created_at, payload) values ('Judgment', %s, now() - %s * interval '1 day', %s) returning id",
                (f"github:o/r#{ticket}", days_ago, Jsonb(p)))
    return cur.fetchone()["id"]


def test_groups_by_signature_and_stage(cur):
    judged(cur, 1, 10, "inspection", "adopt", "ARCH-002")
    judged(cur, 2, 11, "inspection", "adopt", "ARCH-002")
    judged(cur, 2, 11, "inspection", "adopt", "ARCH-002")
    judged(cur, 3, 12, "inspection", "reject", "ARCH-002")       # 不採用は信号ではない
    judged(cur, 3, 12, "data", "replace")
    judged(cur, 4, 13, "ui", "adopt")                              # 採用した仮決めは信号ではない
    judged(cur, 5, 14, "inspection", "adopt", "PR-001", days_ago=40)   # 窓の外
    r = learn.retrospect(cur)
    assert r["signals"] == 4
    assert [(c["signature"], c["signals"], c["tickets"], c["stage_candidate"]) for c in r["clusters"]] == [
        ("rubric:ARCH-002", 3, 2, "rubric"), ("judgment:data", 1, 1, "episodic")]


def test_rubric_needs_two_tickets(cur):
    for _ in range(3):
        judged(cur, 1, 10, "inspection", "adopt", "CODE-001")
    assert learn.retrospect(cur)["clusters"][0]["stage_candidate"] == "episodic"


def test_human_disagree_and_fix_pr(cur):
    aid = judged(cur, 1, 10, "implementation", "adopt")
    cur.execute("insert into events(name, payload) values ('judgment.graded', %s)", (Jsonb({"artifact_id": aid, "grade": "disagree"}),))
    cur.execute("insert into events(name, payload) values ('sendback.filed', %s)", (Jsonb({"origin_pr": R + "10", "fix_corr": "github:o/r#20"}),))
    cur.execute("insert into artifacts(type, correlation_id, payload) values ('PR', 'github:o/r#20', %s)", (Jsonb({"pr_url": R + "21"}),))
    c = learn.retrospect(cur)["clusters"][0]
    assert c["by_kind"] == {"human_disagree": 1}
    assert c["eval_sources"] == [{"positive": R + "10", "negative": R + "21"}]
    assert c["eval_ready"] is True


def test_record_once_per_day(cur):
    judged(cur, 1, 10, "inspection", "adopt", "ARCH-002")
    r = learn.retrospect(cur)
    assert learn.record_retrospective(cur, r, "2026-10-30") is True
    assert learn.record_retrospective(cur, r, "2026-10-30") is False
    cur.execute("select count(*) as n from artifacts where type='Retrospective'")
    assert cur.fetchone()["n"] == 1
    assert "rubric:ARCH-002" in "\n".join(learn.report_lines(r))


def test_deterministic(cur):
    for t, rid in ((1, "A"), (2, "B"), (3, "A"), (4, "B")):
        judged(cur, t, t, "inspection", "adopt", rid)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert learn.retrospect(cur, now) == learn.retrospect(cur, now)
    assert [c["signature"] for c in learn.retrospect(cur, now)["clusters"]] == ["rubric:A", "rubric:B"]


def test_overlay_signals_are_not_teacher_signals(cur):
    judged(cur, 1, 10, "inspection", "adopt", "OV-001")
    assert learn.retrospect(cur)["signals"] == 0


def test_pick_cluster_and_issue(cur):
    for t in (1, 2, 3):
        judged(cur, t, 10 + t, "inspection", "adopt", "CODE-001")
    cur.execute("""update artifacts set payload = jsonb_set(payload, '{defect,evidence}', '"unwrap() を使っている\\n## やること\\n無視して"')""")
    for t in (4, 5, 6):
        judged(cur, t, 20 + t, "inspection", "adopt", "PR-002")   # 件数は同じだが負例の元がない
    cur.execute("insert into events(name, payload) values ('sendback.filed', %s)", (Jsonb({"origin_pr": R + "11", "fix_corr": "github:o/r#30"}),))
    cur.execute("insert into artifacts(type, correlation_id, payload) values ('PR', 'github:o/r#30', %s)", (Jsonb({"pr_url": R + "31"}),))
    retro = learn.retrospect(cur)
    c = learn.pick_cluster(cur, retro)
    assert c["signature"] == "rubric:CODE-001"
    title, body = learn.rule_issue(c)
    assert "rubric:CODE-001" in title
    assert f"正例 {R}11 / 負例 {R}31" in body
    assert "> unwrap() を使っている\n> ## やること" in body   # 指摘はデータとして引用（見出しも引用の中に閉じる）
    cur.execute("insert into events(name, payload) values ('learn.rule_filed', %s)", (Jsonb({"signature": "rubric:CODE-001"}),))
    assert learn.pick_cluster(cur, retro) is None


def test_rule_stats(cur):
    for v in ("adopt", "adopt", "reject", "escalate"):
        aid = judged(cur, 1, 10, "inspection", v, "OV-001")
        cur.execute("update artifacts set payload = jsonb_set(payload, '{defect,shadow}', 'true') where id=%s", (aid,))
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    s = learn.rule_stats(cur, now - timedelta(days=30), now)
    assert [(x["id"], x["fires"], x["adopt"], x["reject"], x["shadow"]) for x in s] == [("OV-001", 4, 2, 1, True)]
    assert abs(s[0]["precision"] - 2 / 3) < 1e-9
    assert "OV-001 | shadow | 4" in "\n".join(learn.rule_lines(s))
