"""人が判断済みの項目は、head が変わって judge が走り直しても人に回し直さない（11-01 rally #505）。

  TEST_DATABASE_URL=postgresql://factory@localhost:5432/factory_test python -m pytest orchestrator/dispatcher/tests
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import judgment

PR = "https://github.com/o/r/pull/505"
CORR = "github:o/r#496"


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def judge(cur, verdicts, job_id=None):
    judgment.record_provisional(cur, CORR, job_id, PR, "### aesthetic 3: 文字の役割の当て方\n- 選んだ: title に当てる\n- 確信度: 0.6\n"
                                                   "### scope 2: 書体の同梱\n- 選んだ: 同梱しない\n- 確信度: 0.5\n")
    inp = judgment.judge_input(cur, PR, "attended")
    ids = {p["question"]: p["artifact_id"] for p in inp["provisional"]}
    vs = [{"artifact_id": ids[q], "verdict": v, "confidence": c} for q, (v, c) in verdicts.items()]
    return judgment.record_judged(cur, CORR, job_id, PR, inp, {"provisional": vs}, "attended", "test")


def test_human_decided_items_are_not_escalated_again(cur):
    first = judge(cur, {"文字の役割の当て方": ("escalate", 0.6), "書体の同梱": ("escalate", 0.5)})
    assert len(first["escalated"]) == 2
    # 人が 1 件だけ判断した
    aid = next(e["artifact_id"] for e in first["escalated"] if e["question"] == "文字の役割の当て方")
    cur.execute("update artifacts set payload = jsonb_set(payload, '{human_grade}', %s) where id=%s", (Jsonb({"grade": "agree"}), aid))
    # 衝突の解消で head が変わり、judge が同じ問いを人に回した
    second = judge(cur, {"文字の役割の当て方": ("escalate", 0.6), "書体の同梱": ("escalate", 0.5)})
    assert [e["question"] for e in second["escalated"] if e["question"] == "文字の役割の当て方"] == []
    assert any(e["question"] == "書体の同梱" for e in second["escalated"])
    cur.execute("select payload from artifacts where type='Judgment' and payload->>'verdict'='human_decided'")
    rows = cur.fetchall()
    assert rows and rows[0]["payload"]["carried_human_decision"] == {"grade": "agree"}


def test_carried_items_do_not_lower_confidence(cur):
    first = judge(cur, {"文字の役割の当て方": ("escalate", 0.3), "書体の同梱": ("adopt", 0.9)})
    aid = first["escalated"][0]["artifact_id"]
    cur.execute("update artifacts set payload = jsonb_set(payload, '{human_grade}', %s) where id=%s", (Jsonb({"grade": "agree"}), aid))
    second = judge(cur, {"文字の役割の当て方": ("escalate", 0.3), "書体の同梱": ("adopt", 0.9)})
    assert second["escalated"] == [] and second["min_confidence"] == 0.9
