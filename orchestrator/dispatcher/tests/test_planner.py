"""プランナーの制御プレーン側（planner.py、ADR 0025・0026）: 判定の記録、経路どおりのラベル、問いと返事、定義との食い違い"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
import planner as pn  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
REPO, ISSUE, CORR = "o/r", 5, "github:o/r#5"
DEFS = yaml.safe_load((Path(__file__).resolve().parents[3] / "definitions" / "planner.yaml").read_text(encoding="utf-8"))
Q = [{"question": "進め方は", "why": "決まると着手できる", "options": ["倣う", "案を出す", "作る"], "recommended": 0}]


def res(decision="ready", route="implement", notes=(), round_=1, questions=(), summary="項目を足す"):
    return {"decision": decision, "route": route, "notes": list(notes), "round": round_, "facts": {"has_design_system": False},
            "plan": {"design_scale": "small", "summary": summary, "questions": list(questions), "evidence": [], "interpretation": "i", "confidence": 0.8},
            "cost_usd": 0.03, "model": "m"}


# ------------------------------------------------------------------ DB の要らないもの
def test_constants_match_definitions():
    lb = DEFS["labels"]
    assert (pn.TRIGGER_LABEL, pn.NEEDS_INFO_LABEL, pn.ESCALATE_LABEL) == (lb["trigger"], lb["needs_info"], lb["escalate"])
    assert pn.ROUTE_LABELS == lb["routes"]
    assert pn.ROUTE_JA == {k: v["ja"] for k, v in DEFS["routes"].items()}
    assert (pn.MAX_ROUNDS, pn.MARKER) == (DEFS["asking"]["max_rounds"], DEFS["asking"]["answer_marker"])


def test_questions_comment_has_marker_and_options():
    text = pn.questions_comment(7, 1, 2, "要約", Q, ["no_design_system"])
    assert text.startswith(pn.MARKER) and "（1/2）" in text and "c. 作る" in text and "a. 倣う（推奨）" in text
    assert "デザインシステムがなく" in text and "Plan 7" in text


@pytest.mark.parametrize("bodies,want", [
    ([], False),
    (["受け付けました"], False),                                   # 問いがまだない
    (["受け付けました", pn.MARKER + " 質問"], False),               # 返事がまだない
    ([pn.MARKER + " 質問", "倣ってください"], True),
    ([pn.MARKER + " 質問", "<!-- factory:x --> 工場の知らせ"], False),   # 工場のコメントは返事に数えない
    ([pn.MARKER + " 質問", "   "], False),
    ([pn.MARKER + " 1", "返事", pn.MARKER + " 2"], False),          # 最後の問いより後だけを見る
])
def test_answered(bodies, want):
    assert pn.answered([{"body": b} for b in bodies]) is want


# ------------------------------------------------------------------ DB を使うもの
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
    def __init__(self, comments=()):
        self.labels, self.comments, self.issue_comments = [], [], list(comments)

    def swap(self, repo, n, remove, add):
        self.labels.append((n, tuple(remove), tuple(add)))

    def comment(self, repo, n, text):
        self.comments.append(text)

    def gh(self, method, path, body=None):
        if "labels=" in path:
            return [{"number": ISSUE}, {"number": 6, "pull_request": {}}]
        if path.endswith("/comments?per_page=100"):
            return [{"body": b} for b in self.issue_comments]
        return {}


def job(cur, kind="plan"):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, kind, state) values (%s,%s,%s,'test',%s,'succeeded') returning id",
                (CORR, REPO, ISSUE, kind))
    return cur.fetchone()["id"]


def run(cur, c, r):
    jid = job(cur)
    pid = pn.record(cur, REPO, ISSUE, CORR, jid, r)
    pn.act(cur, REPO, ISSUE, CORR, jid, pid, r, c.swap, c.comment)
    return pid


@needs_db
@pytest.mark.parametrize("route,label", [("implement", "factory:planned"), ("ui_design", "factory:design"), ("design_system", "factory:design-system")])
def test_ready_routes_by_label(cur, route, label):
    c = Calls()
    pid = run(cur, c, res(route=route))
    assert c.labels == [(ISSUE, ("factory:running",), (label,))]
    assert pn.ROUTE_JA[route] in c.comments[0] and f"Plan {pid}" in c.comments[0]
    cur.execute("select payload from events where name='plan.decided'")
    ev = cur.fetchone()["payload"]
    assert (ev["decision"], ev["route"], ev["plan_id"], ev["cost_usd"]) == ("ready", route, pid, 0.03)
    cur.execute("select payload from artifacts where type='Plan'")
    assert cur.fetchone()["payload"]["summary"] == "項目を足す"


@needs_db
def test_ask_then_answer_requeues_next_round(cur):
    c = Calls()
    run(cur, c, res("ask", "ui_design", ["no_design_system"], questions=Q))
    assert c.labels == [(ISSUE, ("factory:running",), ("factory:needs-info",))] and c.comments[0].startswith(pn.MARKER)
    cur.execute("select count(*) as n from events where name='plan.asked'")
    assert cur.fetchone()["n"] == 1
    c.issue_comments = [c.comments[0]]
    assert pn.poll_answers(cur, c.gh, c.swap, [REPO], "tier-large") == 0          # 返事がまだない
    c.issue_comments.append("既存の画面に倣ってください")
    assert pn.poll_answers(cur, c.gh, c.swap, [REPO], "tier-large") == 1
    assert c.labels[-1] == (ISSUE, ("factory:needs-info",), ("factory:queued",))
    cur.execute("select j.kind, j.enqueued_by, e.payload from jobs j join events e on e.job_id=j.id and e.name='job.enqueued' where j.state='queued'")
    r = cur.fetchone()
    assert (r["kind"], r["enqueued_by"], r["payload"]["input"]) == ("plan", "planner", {"round": 2})
    assert pn.poll_answers(cur, c.gh, c.swap, [REPO], "tier-large") == 0          # 同じチケットの active なジョブは 1 本


@needs_db
def test_escalate_goes_to_human(cur):
    c = Calls()
    run(cur, c, res("escalate", "ui_design", ["rounds_exhausted"], round_=3))
    assert c.labels == [(ISSUE, ("factory:running",), ("factory:plan-human",))]
    assert "往復を使い切った" in c.comments[0] and "factory:plan" in c.comments[0]
