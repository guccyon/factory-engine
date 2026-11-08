"""ワーカーが渡した知見を knowledge.used に残す（ADR 0024 の 6）。DB は使わない"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("GH_TOKEN", "x")
os.environ.setdefault("CLAUDE_CODE_OAUTH_TOKEN", "x")
import dispatcher  # noqa: E402


class Cur:
    def __init__(self):
        self.rows = []

    def execute(self, sql, params=None):
        self.rows.append((sql, params))


def test_records_ids_per_job():
    cur = Cur()
    dispatcher.record_knowledge_used(cur, "github:o/r#1", 7, "o/r", 1, {"pr_url": "u", "knowledge_ids": ["K-0001", 3]}, {"reason": "conflict"})
    (sql, params), = cur.rows
    assert params[0] == "knowledge.used" and params[2] == 7 and params[4] == "knowledge.used:7"
    assert params[3].obj == {"repo": "o/r", "issue": 1, "role": "implement", "ids": ["K-0001"], "pr_url": "u", "continue_reason": "conflict",
                             "entry": "continue", "work": [], "values": {}, "fit": {}}


def test_records_work_values_and_fit():
    """ADR 0040: 業務・軸の値・適応度の内訳を残す。fit は渡した知見に絞る"""
    cur = Cur()
    sel = {"work": ["dev.build", "dev.describe"], "values": {"roles": ["implement"], "request_form": "ticket"},
           "fit": {"K-0001": {"score": 3.5}, "K-0009": {"score": -1}}}
    dispatcher.record_knowledge_used(cur, "c", 8, "o/r", 2, {"pr_url": "u", "knowledge_ids": ["K-0001"], "knowledge_selection": sel}, None)
    (sql, params), = cur.rows
    obj = params[3].obj
    assert obj["entry"] == "ticket" and obj["work"] == ["dev.build", "dev.describe"]
    assert obj["values"] == {"roles": ["implement"], "request_form": "ticket"}
    assert obj["fit"] == {"K-0001": {"score": 3.5}}


def test_nothing_when_no_ids():
    cur = Cur()
    dispatcher.record_knowledge_used(cur, "c", 7, "o/r", 1, {"pr_url": "u"}, None)
    dispatcher.record_knowledge_used(cur, "c", 7, "o/r", 1, {"knowledge_ids": []}, None)
    assert cur.rows == []
