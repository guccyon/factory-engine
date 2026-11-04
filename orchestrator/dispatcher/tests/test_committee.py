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
    import psycopg.types.json
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


def test_inspect_enqueue_retries_when_another_job_holds_the_pr(cur, monkeypatch):
    # 同じ PR の別のジョブ（judge など）が動いている間に検査を積めなくても、次の周回で積み直す（rally #458）
    monkeypatch.setattr(supervisor, "INSPECT_ENABLED", True)
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, kind) values ('github:o/r#5','o/r',12,'supervisor','judge') returning id")
    busy = cur.fetchone()["id"]
    supervisor.enqueue_inspect(cur, P, "abc")
    cur.execute("select count(*) as n from jobs where kind='inspect'")
    assert cur.fetchone()["n"] == 0
    cur.execute("select 1 from events where dedupe_key=%s", (f"supervisor.inspect:{PR}@abc",))
    assert cur.fetchone() is None   # 積めなかった記録は残さない
    cur.execute("update jobs set state='succeeded' where id=%s", (busy,))
    supervisor.enqueue_inspect(cur, P, "abc")
    cur.execute("select count(*) as n from jobs where kind='inspect'")
    assert cur.fetchone()["n"] == 1


def test_pr_sent_to_human_by_committee_does_not_take_a_slot(cur):
    # 委員会が人に回した知見の PR は、同時に流す件数の枠に数えない（人待ちでラインを止めない）
    assert not supervisor.waiting_for_human(cur, PR)
    committee.record(cur, "o/r", 12, f"committee:{PR}", None, result("human"), lambda m, p, b=None: {}, lambda repo, n, text: None)
    assert supervisor.waiting_for_human(cur, PR)


# ------------------------------------------------------------------ 直してかけ直す（2026-11-04 オーナー: 知見の PR は委員会の可決でマージ）
def revise_result():
    r = result("revise")
    r["votes"][3] = {"role": "dissent", "vote": "sendback", "reasons": ["K-0002 の主語を追記の仕方に直す"], "personal_data": False}
    return r


def gh_pr(calls, head="h1"):
    def gh(m, p, b=None):
        calls.append((m, p, b))
        return {"head": {"ref": "factory/issue-5-job-9", "sha": head}} if m == "GET" else {}
    return gh


def test_revise_enqueues_fix_then_second_split_goes_to_human(cur, monkeypatch):
    monkeypatch.setattr(supervisor, "COMMITTEE", True)
    calls, comments = [], []
    comment = lambda repo, n, text: comments.append(text)
    assert committee.record(cur, "o/r", 12, f"committee:{PR}", None, revise_result(), gh_pr(calls), comment)
    cur.execute("select payload from events where name='job.enqueued' and payload->'continue'->>'reason'='review_fix'")
    sig = cur.fetchone()["payload"]["input"]
    assert sig["defects"][0]["evidence"] == "K-0002 の主語を追記の仕方に直す" and sig["defects"][0]["rubric_id"] == "committee:dissent"
    assert committee.decided(cur, PR) is None and "かけ直します" in comments[0]
    cur.execute("update jobs set state='succeeded'")
    supervisor.enqueue_committee(cur, P, "h1")      # 直しがまだ push されていない
    cur.execute("select count(*) as n from jobs where kind='committee_review'")
    assert cur.fetchone()["n"] == 0
    supervisor.enqueue_committee(cur, P, "h2")      # 直した head でかけ直す
    cur.execute("select count(*) as n from jobs where kind='committee_review'")
    assert cur.fetchone()["n"] == 1
    assert committee.record(cur, "o/r", 12, f"committee:{PR}", None, revise_result(), gh_pr(calls, "h2"), comment)
    assert committee.decided(cur, PR)["outcome"] == "human"   # 2 度目に割れたら人へ


def test_reopen_ignores_old_human_decision_and_reenqueues(cur, monkeypatch):
    monkeypatch.setattr(supervisor, "COMMITTEE", True)
    calls = []
    monkeypatch.setattr(supervisor, "gh", lambda m, p, b=None: calls.append((m, p)) or {})
    committee.record(cur, "o/r", 12, f"committee:{PR}", None, result("human"), lambda m, p, b=None: {}, lambda *a: None)
    supervisor.enqueue_committee(cur, P, "s1")
    cur.execute("select count(*) as n from jobs where kind='committee_review'")
    assert cur.fetchone()["n"] == 0
    cur.execute("insert into events(name, payload) values ('committee.reopened', %s)", (psycopg.types.json.Jsonb({"pr_url": PR}),))
    assert committee.decided(cur, PR) is None
    supervisor.enqueue_committee(cur, P, "s1")
    cur.execute("select count(*) as n from jobs where kind='committee_review'")
    assert cur.fetchone()["n"] == 1
    assert ("DELETE", "repos/o/r/issues/12/labels/factory%3Aneeds-approval") in calls
    assert committee.record(cur, "o/r", 12, f"committee:{PR}", None, result("adopt"), lambda m, p, b=None: {}, lambda *a: None)
    assert committee.approved(cur, PR)


def filed(cur, issue, pr=None, age="0 hours"):
    corr = f"github:o/r#{issue}"
    cur.execute("insert into events(name, correlation_id, occurred_at, payload) values ('knowledge.case_filed',%s, now() - %s::interval, %s)",
                (corr, age, psycopg.types.json.Jsonb({"incident": f"x:{issue}"})))
    if pr:
        cur.execute("insert into artifacts(type, correlation_id, payload) values ('PR',%s,%s)",
                    (corr, psycopg.types.json.Jsonb({"pr_url": f"https://github.com/o/r/pull/{pr}"})))
    return corr


def test_knowledge_flows_one_at_a_time_and_human_waits_step_aside(cur):
    """知見のキューは起票の古い順に 1 件ずつ。人の判断待ちに移ったものは次に譲る（2026-11-04 オーナー）"""
    a, b, c = filed(cur, 1, 101), filed(cur, 2, 102), filed(cur, 3)
    assert [q["corr"] for q in committee.queue(cur, "o/r")] == [a, b, c]
    assert committee.turn(cur, "o/r") == a
    cur.execute("insert into events(name, correlation_id, payload) values ('committee.decided',%s,%s)",
                (a, psycopg.types.json.Jsonb({"pr_url": "https://github.com/o/r/pull/101", "outcome": "human"})))
    assert committee.turn(cur, "o/r") == b
    cur.execute("insert into events(name, correlation_id, payload) values ('pr.merged',%s,'{}')", (b,))
    assert committee.turn(cur, "o/r") == c   # まだ PR がない（書いている）ものも順番を持つ
    assert committee.turn(cur, "other/repo") is None


def test_stuck_writer_without_pr_gives_up_its_turn(cur):
    filed(cur, 1, age="3 days")
    b = filed(cur, 2, 102)
    assert committee.turn(cur, "o/r") == b
