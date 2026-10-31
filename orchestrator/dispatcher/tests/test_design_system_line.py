"""デザインシステムのデザイナー役の制御プレーン側（design_system_line.py、ADR 0025・0026）: 案を人の選択へ、選択から次の段、正本を実装役へ"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
import choice  # noqa: E402
import design_system_line as dsl  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
REPO, ISSUE, CORR = "o/r", 7, "github:o/r#7"
DEFS = yaml.safe_load((Path(__file__).resolve().parents[3] / "definitions" / "design_system.yaml").read_text(encoding="utf-8"))
CONCEPTS = {"product_understanding": "レッスンの予約", "recommended": "calm", "recommended_reason": "続けやすい",
            "concepts": [{"id": "calm", "name": "落ち着いた上質", "impression": "安心", "mood_words": ["静か", "上質", "誠実"], "tradeoff": "元気さは弱い"},
                         {"id": "sporty", "name": "スポーティ", "impression": "前向き", "mood_words": ["軽快", "明るい", "力強い"], "tradeoff": "上質さは弱い"}]}


def test_constants_match_definitions():
    lb = DEFS["labels"]
    assert (dsl.TRIGGER_LABEL, dsl.CHOICE_LABEL, dsl.AFTER_LABEL) == (lb["trigger"], lb["choice"], lb["after"])
    assert dsl.SPEC_DIR == DEFS["spec"]["dir"] and dsl.KIND in choice.KINDS


def test_issue_section_and_replace():
    sec = dsl.issue_section(3, {"tokens.yaml": "color: {}\n"}, False, "calm")
    assert "specs/design/tokens.yaml" in sec and "コンセプト `calm`" in sec and "全部" in sec and "DesignSystemSpec 3" in sec
    body = dsl.replace_section("人の本文\n", sec)
    again = dsl.replace_section(body, dsl.issue_section(4, {"components.yaml": "b: 1\n"}, True, None))
    assert again.startswith("人の本文") and again.count(dsl.MARK) == 1 and "specs/design/components.yaml" in again and "specs/design/tokens.yaml" not in again
    assert "変わるファイルだけ" in again


needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")


@pytest.fixture()
def cur():
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


class Calls:
    def __init__(self, body="## 受け入れ条件\n- 正本を置く\n"):
        self.labels, self.comments, self.body, self.patched = [], [], body, []

    def swap(self, repo, n, remove, add):
        self.labels.append((n, tuple(remove), tuple(add)))

    def comment(self, repo, n, text):
        self.comments.append(text)

    def gh(self, method, path, body=None):
        if method == "GET":
            return {"body": self.body}
        if method == "PATCH":
            self.body = body["body"]
            self.patched.append(path)
        return {}


def job(cur):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, kind, state) values (%s,%s,%s,'test','design_system','succeeded') returning id",
                (CORR, REPO, ISSUE))
    return cur.fetchone()["id"]


def concept_round(cur, c):
    jid = job(cur)
    keys = {"calm.png": f"design/{jid}/calm.png", "sporty.png": f"design/{jid}/sporty.png"}
    res = {"phase": "concept", "round": 1, "concepts": CONCEPTS, "cost_usd": 1.2, "model": "m"}
    aid = dsl.record_concepts(cur, REPO, ISSUE, CORR, jid, res, keys)
    cid = dsl.request_choice(cur, REPO, ISSUE, CORR, jid, aid, res, keys, c.swap, c.comment)
    return aid, cid


def queued_input(cur):
    cur.execute("select e.payload from jobs j join events e on e.job_id=j.id and e.name='job.enqueued' where j.state='queued' and j.kind='design_system'")
    r = cur.fetchone()
    return r["payload"]["input"] if r else None


@needs_db
def test_concepts_go_to_human_then_spec_is_queued(cur):
    c = Calls()
    aid, cid = concept_round(cur, c)
    ch = choice.get(cur, cid)
    assert ch["kind"] == "concept_tone" and ch["recommended"] == "calm" and ch["options"][0]["image_key"].endswith("calm.png")
    assert ch["subject"] == {"repo": REPO, "ticket": str(ISSUE)}
    assert c.labels == [(ISSUE, ("factory:running",), ("factory:design-choice",))] and "`calm` 落ち着いた上質（推奨）" in c.comments[0]
    assert dsl.apply_choices(cur, c.swap, c.comment) == 0                     # まだ選ばれていない
    assert choice.choose(cur, cid, "sporty", "owner")[0]
    assert dsl.apply_choices(cur, c.swap, c.comment) == 1
    inp = queued_input(cur)
    assert inp["phase"] == "spec" and inp["concept"]["id"] == "sporty"
    assert c.labels[-1] == (ISSUE, ("factory:design-choice",), ("factory:queued",))
    cur.execute("select payload from events where name='design_system.choice_applied'")
    assert cur.fetchone()["payload"]["agreed"] is False
    assert dsl.apply_choices(cur, c.swap, c.comment) == 0                     # 1 度だけ


@needs_db
def test_returned_concepts_are_redrawn_with_the_note(cur):
    c = Calls()
    aid, cid = concept_round(cur, c)
    assert choice.return_to_maker(cur, cid, "owner", "どちらも暗い")[0]
    assert dsl.apply_choices(cur, c.swap, c.comment) == 1
    inp = queued_input(cur)
    assert (inp["phase"], inp["note"], inp["round"]) == ("concept", "どちらも暗い", 2) and inp["previous_concepts"]["recommended"] == "calm"


@needs_db
def test_spec_is_handed_to_implement_once(cur):
    c = Calls()
    jid = job(cur)
    sid = dsl.record_spec(cur, REPO, ISSUE, CORR, jid, {"spec_files": {"tokens.yaml": "color: {}\n", "../x.yaml": "a"}, "delta": False, "cost_usd": 0.9},
                          {"concept": {"id": "calm"}})
    dsl.handoff(cur, c.gh, c.swap, c.comment, REPO, ISSUE, CORR, sid)
    dsl.handoff(cur, c.gh, c.swap, c.comment, REPO, ISSUE, CORR, sid)
    assert len(c.patched) == 1 and c.body.startswith("## 受け入れ条件") and "specs/design/tokens.yaml" in c.body and "../x.yaml" not in c.body
    assert c.labels == [(ISSUE, ("factory:running", "factory:design-system"), ("factory:planned",))]
    cur.execute("select payload from events where name='design_system.handed_off'")
    assert cur.fetchone()["payload"]["files"] == ["tokens.yaml"]


@needs_db
def test_concept_choice_shows_in_inbox_for_design_chief(cur):
    import inbox
    concept_round(cur, Calls())
    item = next(i for i in inbox._choices(cur))
    assert item["who"] == "design_chief" and item["title"].startswith("コンセプトとトーン＆マナーの選択: ")
    assert inbox.CHOICE_TITLES.keys() == choice.KINDS.keys()
