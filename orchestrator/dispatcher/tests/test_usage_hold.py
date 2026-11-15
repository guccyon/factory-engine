"""契約の使用量の上限に当たったら、戻るまで新しいジョブを取り出さない（2026-11-15。07:00〜07:24 JST に上限のまま 15 本を流して全部落とした）。

  TEST_DATABASE_URL=postgresql://factory@localhost:5432/factory_test python -m pytest orchestrator/dispatcher/tests/test_usage_hold.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")

import usage_hold  # noqa: E402

NOW = datetime(2026, 11, 14, 22, 5, tzinfo=timezone.utc)   # 11/15 07:05 JST


def test_limit_text_from_agent_runner_mark():
    lines = ["[qa] job=1\n", "[qa.err] [agent_runner] claude exited rc=1 is_error=True stdout=... stderr=(空)\n",
             "[qa.err] [agent_runner] usage limit: You've hit your limit · resets 11am (Asia/Tokyo)\n", "FACTORY_RESULT:{}\n"]
    assert usage_hold.limit_text(lines) == "You've hit your limit · resets 11am (Asia/Tokyo)"
    assert usage_hold.limit_text(["[worker] rate limit hit while pushing\n"]) is None


def test_resets_at_reads_the_reset_time():
    jst = timezone(timedelta(hours=9))
    assert usage_hold.resets_at("You've hit your limit · resets 11am (Asia/Tokyo)", NOW) == datetime(2026, 11, 15, 11, 0, tzinfo=jst)
    assert usage_hold.resets_at("Claude AI usage limit reached|1794708000", NOW) == datetime(2026, 11, 15, 2, 0, tzinfo=timezone.utc)
    assert usage_hold.resets_at("5-hour limit reached ∙ resets 3:30pm", NOW) == datetime(2026, 11, 15, 15, 30, tzinfo=timezone.utc)
    assert usage_hold.resets_at("Weekly limit reached · resets Nov 16, 11am (Asia/Tokyo)", NOW) == datetime(2026, 11, 16, 11, 0, tzinfo=jst)
    assert usage_hold.resets_at("usage limit", NOW) is None


def test_hold_until_falls_back_when_unreadable_or_far():
    until, parsed = usage_hold.hold_until("You've hit your limit · resets 11am (Asia/Tokyo)", NOW)
    assert parsed and until == datetime(2026, 11, 15, 2, 2, tzinfo=timezone.utc)
    until, parsed = usage_hold.hold_until("usage limit", NOW)
    assert not parsed and until == NOW + timedelta(minutes=usage_hold.HOLD_MIN)
    until, parsed = usage_hold.hold_until("usage limit reached|1999999999", NOW)   # 読めても遠すぎる時刻は読み違いとみる
    assert not parsed


if URL:
    import psycopg
    from psycopg.rows import dict_row

    for k, v in {"DATABASE_URL": URL, "GH_TOKEN": "x", "CLAUDE_CODE_OAUTH_TOKEN": "x", "OBJECTSTORE_ENDPOINT": "localhost:1",
                 "OBJECTSTORE_ACCESS_KEY": "x", "OBJECTSTORE_SECRET_KEY": "x"}.items():
        os.environ.setdefault(k, v)
    import stoppage


@pytest.fixture()
def cur():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def test_holds_until_reset_then_probes_with_one_job(cur, monkeypatch):
    monkeypatch.setattr(stoppage, "notify", lambda *a, **k: None)
    now = datetime.now(timezone.utc)
    assert usage_hold.held(cur, now) is None
    until = usage_hold.record_hit(cur, None, "github:o/r#1", "usage limit", now)   # 戻る時刻が読めない: HOLD_MIN 分止める
    h = usage_hold.held(cur, now)
    assert h and h["until"] == until and not h["parsed"]
    usage_hold.watch(cur, stoppage, now)
    usage_hold.record_hit(cur, None, "github:o/r#2", "usage limit", now)          # 続けて当たっても停止は 1 件のまま
    usage_hold.watch(cur, stoppage, now)
    assert len(stoppage.open_keys(cur, "usage:")) == 1
    after = until + timedelta(minutes=1)
    assert usage_hold.held(cur, after + timedelta(minutes=1)) is None
    assert not usage_hold.probing(cur, after)                                   # まだ何も流していない: 1 本取り出してよい
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, started_at) values ('c','o/r',3,'plan','running',%s) returning id", (after,))
    probe = cur.fetchone()["id"]
    assert usage_hold.probing(cur, after + timedelta(minutes=1))                # 確かめ中は 2 本目を取らない
    assert not usage_hold.probing(cur, after + timedelta(minutes=usage_hold.PROBE_MIN + 1))   # 動き続けていれば戻っている
    cur.execute("update jobs set state='succeeded', finished_at=%s where id=%s", (after + timedelta(seconds=30), probe))
    assert not usage_hold.probing(cur, after + timedelta(minutes=1))
    usage_hold.watch(cur, stoppage, after + timedelta(minutes=2))
    assert stoppage.open_keys(cur, "usage:") == []


def test_release_by_hand(cur):
    now = datetime.now(timezone.utc)
    usage_hold.record_hit(cur, None, None, "You've hit your limit · resets 11am (Asia/Tokyo)", now)
    assert usage_hold.held(cur, now)
    usage_hold.release(cur, "test")
    assert usage_hold.held(cur, now) is None


def test_usage_limited_jobs_are_requeued_but_not_reported_one_by_one(cur):
    import dispatcher
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, enqueued_by) values ('github:o/r#9','o/r',9,'plan','failed','supervisor') returning *")
    j = cur.fetchone()
    assert dispatcher.requeue_transient(cur, j, by="usage_retry", window_min=dispatcher.USAGE_RETRY_WINDOW_MIN)
    cur.execute("select enqueued_by from jobs where id<>%s", (j["id"],))
    assert [r["enqueued_by"] for r in cur.fetchall()] == ["usage_retry"]
    assert dispatcher.classify_failure({"usage_limited": True, "error": "exit=1"})[0] == "budget"
