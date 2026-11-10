"""失敗したジョブの停止を閉じてよいか（11-10: チケットに紐づかないジョブの失敗が、対象の PR が片付いても開いたまま残った）と、本文の書き直しを積むか"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    for k, v in {"DATABASE_URL": URL, "GH_TOKEN": "x", "CLAUDE_CODE_OAUTH_TOKEN": "x", "OBJECTSTORE_ENDPOINT": "localhost:1",
                 "OBJECTSTORE_ACCESS_KEY": "x", "OBJECTSTORE_SECRET_KEY": "x"}.items():
        os.environ.setdefault(k, v)
    import dispatcher


@pytest.fixture
def cur():
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def job(cur, kind, issue, state="failed", corr=None, started=True):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, started_at) values (%s,'o/r',%s,%s,%s,%s) returning id",
                (corr or f"c-{kind}-{issue}", issue, kind, state, "2026-11-07T00:00:00Z" if started else None))
    return cur.fetchone()["id"]


def merged(cur, issue, name="pr.merged"):
    from psycopg.types.json import Jsonb
    cur.execute("insert into events(name, payload) values (%s, %s)", (name, Jsonb({"pr_url": f"https://github.com/o/r/pull/{issue}"})))


def test_open_while_nothing_settled(cur):
    j = job(cur, "committee_review", 10)
    assert dispatcher.job_failure_settled(cur, j) is None


def test_closed_when_target_pr_merged_or_closed(cur):
    a, b = job(cur, "body_fix", 11), job(cur, "committee_review", 12)
    merged(cur, 11)
    merged(cur, 12, "pr.closed_unmerged")
    assert "マージされた" in dispatcher.job_failure_settled(cur, a)
    assert "閉じられた" in dispatcher.job_failure_settled(cur, b)


def test_closed_when_same_kind_later_succeeded_on_same_pr(cur):
    a = job(cur, "committee_review", 13)
    job(cur, "committee_review", 13, state="succeeded", corr="other")
    assert "後で通った" in dispatcher.job_failure_settled(cur, a)


def test_not_by_pr_for_kinds_without_pr_target(cur):
    """振り返り（case_review）や実装は issue_number が PR ではないので、PR の片付きでは閉じない"""
    a, b = job(cur, "case_review", 14), job(cur, "implement", 15)
    merged(cur, 14)
    merged(cur, 15)
    job(cur, "case_review", 14, state="succeeded", corr="other")
    assert dispatcher.job_failure_settled(cur, a) is None and dispatcher.job_failure_settled(cur, b) is None


def test_rerun_still_closes(cur):
    a = job(cur, "implement", 16, corr="c1")
    job(cur, "implement", 16, state="running", corr="c1")
    assert "再実行された" in dispatcher.job_failure_settled(cur, a)


def fixed(cur, issue, body, state="succeeded"):
    from psycopg.types.json import Jsonb
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, result) values ('c','o/r',%s,'body_fix',%s,%s)",
                (issue, state, Jsonb({"body_sha": dispatcher.body_sha(body)})))


def test_body_fix_skips_unchanged_body_and_caps(cur, monkeypatch):
    """11-10: PR の中身が変わるたびに本文まで裁き直し、同じ PR に本文の直しが 20 回積まれた。同じ本文は直し直さず、2 回まで"""
    assert dispatcher.body_fix_skip_reason(cur, "o/r", 20, "## a") is None
    fixed(cur, 20, "## 工場が書き直した本文")
    assert dispatcher.body_fix_skip_reason(cur, "o/r", 20, "## 工場が書き直した本文\r\n") == "unchanged"
    assert dispatcher.body_fix_skip_reason(cur, "o/r", 20, "## 人が直した本文") is None
    assert dispatcher.body_fix_skip_reason(cur, "o/r", 20, None) is None   # 本文が取れなくても積む（上限だけ見る）
    fixed(cur, 20, "## 2 回目")
    assert dispatcher.body_fix_skip_reason(cur, "o/r", 20, "## 人が直した本文") == "limit"
    fixed(cur, 21, "x", state="failed")   # 失敗は数えない
    assert dispatcher.body_fix_skip_reason(cur, "o/r", 21, "x") is None


def test_enqueue_body_fix_records_skip(cur, monkeypatch):
    fixed(cur, 22, "## 同じ")
    monkeypatch.setattr(dispatcher, "gh", lambda m, p, b=None: {"body": "## 同じ"})
    dispatcher.enqueue_body_fix(cur, "o/r", "c", "https://github.com/o/r/pull/22", 22, [{"rubric_id": "PR-002"}])
    cur.execute("select payload from events where name='pr.body_fix_skipped'")
    assert cur.fetchone()["payload"]["reason"] == "unchanged"
    cur.execute("select count(*) as n from jobs where issue_number=22 and state='queued'")
    assert cur.fetchone()["n"] == 0
