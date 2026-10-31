"""知見の委員会の制御プレーン側（ADR 0024 の 4）: 持ち込みの積み方、議決の記録と PR への作用、マージの関門。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    import committee
    import psycopg
    import supervisor
    from psycopg.rows import dict_row

PR = "https://github.com/o/r/pull/12"
P = {"url": PR, "corr": "github:o/r#5", "repo": "o/r", "number": 12}


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def result(outcome, **kw):
    votes = [{"role": r, "vote": "adopt", "reasons": ["矛盾なし"], "personal_data": False} for r in ("consistency", "evidence", "enforcement", "dissent")]
    return {"kind": "committee_review", "ok": True, "pr_url": PR, "outcome": outcome, "decision": None if outcome == "sendback" else "D-0001",
            "votes": votes, "enforce": [], "added": [".factory-config/knowledge/K-0002.yaml"], "pushed_sha": "abc", **kw}


def test_enqueue_waits_for_free_slot_and_runs_once(cur, monkeypatch):
    monkeypatch.setattr(supervisor, "COMMITTEE", True)
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, kind) values ('github:o/r#5','o/r',12,'supervisor','inspect')")
    supervisor.enqueue_committee(cur, P, "s1")   # 同じ PR の検査が待ち: 積めない
    cur.execute("select count(*) as n from jobs where kind='committee_review'")
    assert cur.fetchone()["n"] == 0
    cur.execute("update jobs set state='succeeded'")
    supervisor.enqueue_committee(cur, P, "s1")
    supervisor.enqueue_committee(cur, P, "s2")   # 委員会が積んだ commit で head が変わっても積み直さない
    cur.execute("select correlation_id, issue_number from jobs where kind='committee_review'")
    assert [(r["correlation_id"], r["issue_number"]) for r in cur.fetchall()] == [(f"committee:{PR}", 12)]


def test_record_acts_on_outcome(cur):
    calls, comments = [], []
    gh = lambda m, p, b=None: calls.append((m, p, b)) or {"html_url": "https://github.com/o/r/issues/77"}
    comment = lambda repo, n, text: comments.append((repo, n, text))
    assert committee.record(cur, "o/r", 12, f"committee:{PR}", None, result("human"), gh, comment)
    assert not committee.record(cur, "o/r", 12, f"committee:{PR}", None, result("adopt"), gh, comment)   # PR ごとに 1 回
    assert calls == [("POST", "repos/o/r/issues/12/labels", {"labels": ["factory:needs-approval"]})]
    assert "人の判断へ" in comments[0][2] and "整合: 採る" in comments[0][2]
    assert not committee.approved(cur, PR)
    cur.execute("select payload from artifacts where type='CommitteeDecision'")
    assert cur.fetchone()["payload"]["outcome"] == "human"


def test_sendback_closes_and_enforce_files_issue(cur):
    calls = []
    gh = lambda m, p, b=None: calls.append((m, p, b)) or {"html_url": "https://github.com/o/r/issues/77"}
    committee.record(cur, "o/r", 12, f"committee:{PR}", None, result("sendback"), gh, lambda *a: None)
    assert calls == [("PATCH", "repos/o/r/pulls/12", {"state": "closed"})]
    calls.clear()
    other = PR.replace("/12", "/13")
    enf = [{"knowledge": "K-0002", "level": "deterministic", "solution_kind": "ci_check", "proposal": "検査を足す"}]
    committee.record(cur, "o/r", 13, f"committee:{other}", None, result("enforce", pr_url=other, enforce=enf), gh, lambda *a: None)
    assert len(calls) == 1 and calls[0][1] == "repos/o/r/issues" and "K-0002" in calls[0][2]["title"]
    assert calls[0][2]["labels"] == ["factory:planned", "factory:learn"]
    assert committee.approved(cur, other) and not committee.approved(cur, PR)


WEEK_RESULT = {"kind": "committee_weekly", "ok": True,
               "facts": {"items": 3, "cases": 2, "decisions": 2, "stale": [{"id": "K-0001", "confidence": "hypothesis", "review_by": "2026-11-30"}]},
               "report": {"summary": "今週は 2 件を採用", "contradictions": [{"ids": ["K-0002", "K-0003"], "why": "重複", "proposal": "supersede"}],
                          "promote": [], "retire": [],
                          "enforce": [{"ids": ["K-0002"], "why": "繰り返す", "proposal": "lint", "solution_kind": "lint"}],
                          "structure": {"needed": False, "proposal": ""}}}


def test_weekly_record_comments_and_files_once(cur):
    calls, comments = [], []
    gh = lambda m, p, b=None: calls.append((m, p, b)) or {"html_url": f"https://github.com/o/r/issues/{90 + len(calls)}"}
    comment = lambda repo, n, text: comments.append((n, text))
    assert committee.weekly_record(cur, "o/r", 50, "github:o/r#50", None, WEEK_RESULT, "2026-W45", gh, comment)
    assert not committee.weekly_record(cur, "o/r", 50, "github:o/r#50", None, WEEK_RESULT, "2026-W45", gh, comment)
    assert comments[0][0] == 50 and "K-0001" in comments[0][1] and "K-0002, K-0003" in comments[0][1]
    titles = [c[2]["title"] for c in calls if c[0] == "POST"]
    assert titles == ["知見の見直し（2026-W45 の定例）", "担保: K-0002 を lint で決定論にする"]


def test_weekly_input_takes_the_week(cur):
    from datetime import datetime, timezone

    from psycopg.types.json import Jsonb
    cur.execute("insert into events(name, payload) values ('committee.decided', %s)",
                (Jsonb({"pr_url": "https://github.com/o/r/pull/3", "outcome": "adopt", "decision": "D-0001", "added": [],
                        "votes": [{"role": "evidence", "vote": "adopt", "reasons": ["長い理由"]}]}),))
    cur.execute("insert into events(name, payload) values ('committee.decided', %s)", (Jsonb({"pr_url": "https://github.com/x/y/pull/3"}),))
    inp = committee.weekly_input(cur, "o/r", datetime.now(timezone.utc))
    assert [d["decision"] for d in inp["decisions"]] == ["D-0001"] and inp["decisions"][0]["votes"] == [{"role": "evidence", "vote": "adopt"}]


def test_supervisor_enqueues_weekly_once(cur, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(supervisor, "REPOS", ["o/r"])
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    monkeypatch.setattr(supervisor, "COMMITTEE_WEEKLY", True)
    monkeypatch.setattr(supervisor, "KNOWLEDGE_DEFS", os.path.join(os.path.dirname(__file__), "..", "..", "..", "definitions", "knowledge.yaml"))
    monkeypatch.setattr(supervisor, "knowledge_root_exists", lambda repo: True)
    monkeypatch.setattr(supervisor, "gh", lambda m, p, b=None: [] if m == "GET" else {"number": 50})
    sunday = datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)   # JST の日曜 9 時
    supervisor.committee_weekly_step(cur, sunday)
    supervisor.committee_weekly_step(cur, sunday)
    cur.execute("select repo, issue_number, kind from jobs")
    assert [(r["repo"], r["issue_number"], r["kind"]) for r in cur.fetchall()] == [("o/r", 50, "committee_weekly")]
    supervisor.committee_weekly_step(cur, datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc))   # 次の週の月曜は時刻前
    cur.execute("select count(*) as n from jobs")
    assert cur.fetchone()["n"] == 1
