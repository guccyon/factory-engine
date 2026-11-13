"""スパイクのレーン（ADR 0055）: 着手判定の spike、設計書の記録、束ね、出口。GitHub は差し替え、DB は TEST_DATABASE_URL の Postgres（なければ飛ばす）。

2026-11-13 オーナー: プランナーはワークフローと着手の時期を決める役で、直し方の方針は別の担当（スパイクの System Architect）が決める。
"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
import planner as pn  # noqa: E402
import spike_line as sl  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
REPO, ISSUE, CORR = "o/r", 5, "github:o/r#5"
ROOT = Path(__file__).resolve().parents[3]
DEFS = yaml.safe_load((ROOT / "definitions" / "planner.yaml").read_text(encoding="utf-8"))
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")


def DD(**kw):
    base = {"summary": "s", "answers": [{"question": "どこで状態を失うか", "answer": "段の記憶"}],
            "cause": {"statement": "次の操作が段の画面にしかない", "evidence": ["lib/state/student_flow.dart:42"]},
            "approach": {"kind": "redesign", "description": "サーバーの状態から次の操作を出す", "layers": ["backend", "mobile"], "files": ["lib/a.dart"]},
            "alternatives": [{"approach": "各画面に戻る道を足す", "why_not": "同じ型がまた出る"}], "bundle": [6, 7, 8],
            "bundle_why": "どれも段の記憶に頼る", "experience_change": False, "experience_note": "", "risks": [], "confidence": 0.8}
    return {**base, **kw}


class Calls:
    def __init__(self, issues=None):
        self.labels, self.comments, self.issues = [], [], issues or {}

    def swap(self, repo, n, remove, add):
        self.labels.append((n, tuple(remove), tuple(add)))

    def comment(self, repo, n, text):
        self.comments.append((n, text))

    def gh(self, method, path, body=None):
        n = int(path.rsplit("/", 1)[-1]) if path.rsplit("/", 1)[-1].isdigit() else None
        return self.issues.get(n, {}) if method == "GET" else {}


def issue(state="open", labels=("factory:qa-defect",), body=""):
    return {"state": state, "labels": [{"name": x} for x in labels], "body": body}


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


def job(cur, kind="spike"):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, kind, state) values (%s,%s,%s,'test',%s,'succeeded') returning id",
                (CORR, REPO, ISSUE, kind))
    return cur.fetchone()["id"]


# ------------------------------------------------------------------ DB の要らないもの
def test_constants_match_definitions():
    lb, sp = DEFS["labels"], DEFS["spike"]
    assert (sl.TRIGGER_LABEL, sl.HUMAN_LABEL, sl.BUNDLED_LABEL) == (lb["spike"], lb["spike_human"], lb["bundled"]) == (pn.SPIKE_LABEL, lb["spike_human"], lb["bundled"])
    assert (sl.MARKER, sl.HUMAN_BELOW_CONFIDENCE) == (sp["designdoc_marker"], sp["exit"]["human_below_confidence"])
    labels = (ROOT / "orchestrator" / "scripts" / "labels.sh").read_text(encoding="utf-8")
    assert all(f'"{x}"' in labels for x in (lb["spike"], lb["spike_human"], lb["bundled"]))
    assert "spike.sh" in (ROOT / "orchestrator" / "worker" / "Dockerfile").read_text(encoding="utf-8")


@pytest.mark.parametrize("dd,want", [
    (DD(), []),
    (DD(experience_change=True), ["体験や仕様が変わる"]),
    (DD(confidence=0.4), ["確信度が低い"]),
    (DD(approach={"kind": "fix", "description": "", "layers": [], "files": []}), ["方針が書かれていない"]),
])
def test_to_human(dd, want):
    assert sl.to_human(dd) == want


def test_bundle_touches_only_idle_open_issues():
    c = Calls({6: issue(), 7: issue(labels=("factory:running", "factory:priority:p0")), 8: issue(state="closed"),
               9: issue(labels=("factory:qa-defect", "factory:priority:p0", "factory:plan"))})
    done = sl.bundle(c.gh, c.swap, c.comment, REPO, ISSUE, [6, 7, 8, 9, ISSUE])
    assert done == [6, 9]                                   # 流れている 7・閉じた 8・自分自身は触らない
    assert all(add == (sl.BUNDLED_LABEL,) for _, _, add in c.labels) and "#5 の方針" in c.comments[0][1]


def test_comment_body_has_marker_and_exit():
    text = sl.comment_body(3, 9, DD(), [6], [])
    assert text.startswith(sl.MARKER) and "作り直す" in text and "#6" in text and "lib/state/student_flow.dart:42" in text
    assert "着手判定をし直します" in text
    assert "人の確認を待ちます" in sl.comment_body(3, 9, DD(experience_change=True, experience_note="欄が増える"), [], ["体験や仕様が変わる"])


# ------------------------------------------------------------------ DB を使うもの
@needs_db
def test_planner_spike_comments_questions_and_labels(cur):
    c = Calls()
    r = {"decision": "spike", "steps": [], "guards": [], "advisories": [], "round": 1, "facts": {}, "versions": {},
         "plan": {"summary": "s", "spike": {"why": "原因が分からない", "questions": ["どこで状態を失うか", " "]}}, "cost_usd": 0.0, "model": "m"}
    jid = job(cur, "plan")
    pid = pn.record(cur, REPO, ISSUE, CORR, jid, r)
    pn.act(cur, REPO, ISSUE, CORR, jid, pid, r, c.swap, c.comment, c.gh, "t")
    assert c.labels == [(ISSUE, (pn.RUNNING_LABEL,), (pn.SPIKE_LABEL,))]
    assert "スパイクのレーン" in c.comments[0][1] and "- どこで状態を失うか" in c.comments[0][1]
    assert sl.questions(cur, CORR) == ["どこで状態を失うか"]


@needs_db
def test_record_and_act_back_to_planner(cur):
    c = Calls({6: issue()})
    jid = job(cur)
    did = sl.record(cur, REPO, ISSUE, CORR, jid, {"designdoc": DD(bundle=[6]), "versions": {"prompt": "1"}, "cost_usd": 0.4})
    assert sl.act(cur, REPO, ISSUE, CORR, jid, did, {"designdoc": DD(bundle=[6])}, c.swap, c.comment, c.gh) == "plan"
    assert c.labels[-1] == (ISSUE, (sl.RUNNING_LABEL,), (sl.PLAN_LABEL,))
    assert [n for n, _ in c.comments] == [6, ISSUE]
    cur.execute("select payload from events where name='spike.decided'")
    ev = cur.fetchone()["payload"]
    assert ev["designdoc_id"] == did and ev["kind"] == "redesign" and ev["bundle"] == [6]


@needs_db
def test_experience_change_goes_to_human(cur):
    c = Calls()
    jid = job(cur)
    r = {"designdoc": DD(bundle=[], experience_change=True, experience_note="ホームに欄が増える")}
    did = sl.record(cur, REPO, ISSUE, CORR, jid, r)
    assert sl.act(cur, REPO, ISSUE, CORR, jid, did, r, c.swap, c.comment, c.gh) == "human"
    assert c.labels[-1] == (ISSUE, (sl.RUNNING_LABEL,), (sl.HUMAN_LABEL,))
    cur.execute("select count(*) as n from events where name='inbox.requested'")
    assert cur.fetchone()["n"] == 1   # 事業側（po）の確認を受信トレイに積む


@needs_db
def test_input_for_reads_questions_and_mentioned_defects(cur):
    import incident
    for n in (6, 7):
        incident.open_incident(cur, REPO, f"https://github.com/{REPO}/issues/{n}#s2", "s2", f"戻れない {n}", issue=f"https://github.com/{REPO}/issues/{n}")
    cur.execute("insert into events(name, correlation_id, payload) values ('plan.spiked', %s, %s)",
                (CORR, __import__("psycopg").types.json.Jsonb({"questions": ["q1"]})))
    c = Calls({ISSUE: issue(body="#6 と同じ"), 6: issue(body="再現の手順")})
    inp = sl.input_for(cur, c.gh, REPO, ISSUE, CORR)
    assert inp["questions"] == ["q1"] and {d["issue"] for d in inp["open_defects"]} == {6, 7}
    assert inp["defect_bodies"] == {"6": "再現の手順"}
