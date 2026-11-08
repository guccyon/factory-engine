"""判断の持ち主（ADR 0044 の決定 1・2）: 製品の仕様は事業主、依頼者が分かれば依頼者、工場の仕様と書いていないものは工場のメンテナー。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")

pytest.importorskip("psycopg")
import judgment  # noqa: E402

PR = "https://github.com/o/r/pull/700"
CORR = "github:o/r#699"


@pytest.mark.parametrize("spec_of,requester,want", [
    ("product", None, "business"),
    ("product", "acme-pm", "requester"),
    ("factory", "acme-pm", "maintainer"),   # 依頼者がいても、工場の仕様はメンテナー
    (None, None, "maintainer"),              # 書いていないものは迷ったときと同じ
])
def test_decider(spec_of, requester, want):
    assert judgment.decider(spec_of, requester) == want


@pytest.fixture()
def cur():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets, desk_conversations restart identity cascade")
    yield c
    conn.close()


def judged(cur, spec_of_by_q: dict, extra: dict | None = None):
    judgment.record_provisional(cur, CORR, None, PR, "### ui 1: 一覧の並び\n- 選んだ: 新しい順\n- 確信度: 0.5\n"
                                                    "### implementation 2: マージの待ちを置く場所\n- 選んだ: merge_step\n- 確信度: 0.6\n")
    inp = judgment.judge_input(cur, PR, "attended")
    ids = {p["question"]: p["artifact_id"] for p in inp["provisional"]}
    vs = [{"artifact_id": ids[q], "verdict": "escalate", "confidence": 0.5, "recommend": "adopt", **({"spec_of": s} if s else {}), **(extra or {})}
          for q, s in spec_of_by_q.items()]
    judgment.record_judged(cur, CORR, None, PR, inp, {"provisional": vs}, "attended", "test")
    cur.execute("select payload from artifacts where type='Judgment' and payload->>'stage'='judged' order by id")
    return {r["payload"]["question"]: r["payload"] for r in cur.fetchall()}


def test_record_keeps_spec_of_and_decider(cur):
    got = judged(cur, {"一覧の並び": "product", "マージの待ちを置く場所": "factory"})
    assert (got["一覧の並び"]["decider"], got["一覧の並び"]["spec_basis"]) == ("business", "judge")
    assert (got["マージの待ちを置く場所"]["decider"], got["マージの待ちを置く場所"]["spec_of"]) == ("maintainer", "factory")


def test_missing_spec_of_goes_to_maintainer(cur):
    got = judged(cur, {"一覧の並び": None})
    assert (got["一覧の並び"]["spec_of"], got["一覧の並び"]["spec_basis"], got["一覧の並び"]["decider"]) == ("factory", "default", "maintainer")


def test_ticket_from_desk_goes_to_requester(cur):
    from psycopg.types.json import Jsonb
    cur.execute("insert into desk_conversations(kind, product, opened_by) values ('request','o/r','acme-pm') returning id")
    cid = cur.fetchone()["id"]
    cur.execute("insert into events(name, payload) values ('desk.ticket_filed', %s)",
                (Jsonb({"conversation_id": cid, "draft_id": 1, "key": "t1", "number": 699, "url": "https://github.com/o/r/issues/699"}),))
    got = judged(cur, {"一覧の並び": "product", "マージの待ちを置く場所": "factory"})
    assert (got["一覧の並び"]["decider"], got["一覧の並び"]["requester"]) == ("requester", "acme-pm")
    assert (got["マージの待ちを置く場所"]["decider"], got["マージの待ちを置く場所"]["requester"]) == ("maintainer", None)
