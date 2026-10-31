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


def res(decision="ready", steps=("implement",), guards=(), round_=1, questions=(), summary="項目を足す", marks=()):
    return {"decision": decision, "steps": list(steps), "guards": list(guards), "advisories": list(marks), "round": round_,
            "facts": {"has_design_system": False}, "versions": {"engine": "e" * 40, "product": "p" * 40, "prompt": "2"},
            "plan": {"design_scale": "small", "summary": summary, "questions": list(questions), "evidence": ["issue.md"], "interpretation": "i",
                     "confidence": 0.8, "steps": [{"role": r, "focus": f"{r} の仕事", "why": "要る", "effort": "light"} for r in steps],
                     "alternatives": [{"steps": ["ui_design", "implement"], "why_not": "画面は変えない"}]},
            "cost_usd": 0.03, "model": "m"}


# ------------------------------------------------------------------ DB の要らないもの
def test_constants_match_definitions():
    lb = DEFS["labels"]
    assert (pn.TRIGGER_LABEL, pn.NEEDS_INFO_LABEL, pn.ESCALATE_LABEL) == (lb["trigger"], lb["needs_info"], lb["escalate"])
    assert pn.ROLE_LABELS == {k: v["label"] for k, v in DEFS["roles"].items()}
    assert pn.ROLE_JA == {k: v["ja"] for k, v in DEFS["roles"].items()}
    assert set(pn.GUARD_JA) == set(DEFS["guards"]) and set(pn.ADVISORY_JA) == set(DEFS["advisories"])
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
@pytest.mark.parametrize("steps,label", [(["implement"], "factory:planned"), (["ui_design", "implement"], "factory:design"),
                                         (["design_system", "ui_design", "implement"], "factory:design-system"), (["ui_design"], "factory:design")])
def test_ready_labels_the_first_role_and_keeps_rationale(cur, steps, label):
    c = Calls()
    pid = run(cur, c, res(steps=steps, marks=["small_change_with_designer"]))
    assert c.labels == [(ISSUE, ("factory:running",), (label,))]
    assert pn.path_ja(steps) in c.comments[0] and f"Plan {pid}" in c.comments[0]
    assert "とらなかった経路" in c.comments[0] and "画面は変えない" in c.comments[0] and "小さな改修だがデザイナーを通す" in c.comments[0]
    cur.execute("select payload from events where name='plan.decided'")
    ev = cur.fetchone()["payload"]
    assert (ev["decision"], ev["steps"], ev["plan_id"], ev["cost_usd"], ev["alternatives"]) == ("ready", steps, pid, 0.03, 1)
    assert ev["efforts"] == ["light"] * len(steps) and ev["versions"]["prompt"] == "2"
    [p] = pn.plans(cur, CORR)
    assert p["summary"] == "項目を足す" and p["alternatives"][0]["why_not"] == "画面は変えない" and p["versions"]["engine"] == "e" * 40
    assert p["step_details"][0]["focus"].endswith("の仕事") and p["evidence"] == ["issue.md"] and p["interpretation"] == "i"


@needs_db
def test_ask_then_answer_requeues_next_round(cur):
    c = Calls()
    run(cur, c, res("ask", ["ui_design", "implement"], ["no_design_system"], questions=Q))
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
    run(cur, c, res("escalate", ["ui_design", "implement"], ["rounds_exhausted"], round_=3))
    assert c.labels == [(ISSUE, ("factory:running",), ("factory:plan-human",))]
    assert "往復を使い切った" in c.comments[0] and "factory:plan" in c.comments[0] and "画面のデザイナー → 実装役" in c.comments[0]
    assert len(pn.plans(cur, CORR)) == 1


@needs_db
def test_ready_without_known_roles_goes_to_human(cur):
    c = Calls()
    run(cur, c, res(steps=["bogus"]))
    assert c.labels == [(ISSUE, ("factory:running",), ("factory:plan-human",))]
