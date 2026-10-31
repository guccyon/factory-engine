"""事例型の分析役（ADR 0023）: 出来事の選び方、入力の組み立て、セッションのクレンジング、supervisor の積み方、記録。"""
import io
import json
import os
import sys
import tarfile
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")
TAX = os.path.join(os.path.dirname(__file__), "..", "..", "..", "definitions", "learning_taxonomy.yaml")

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import case
    import supervisor

PR = "https://github.com/o/r/pull/7"


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def ev(cur, name, corr=None, payload=None, ago_min=10):
    cur.execute("insert into events(name, correlation_id, payload, occurred_at) values (%s,%s,%s, now() - %s * interval '1 minute')",
                (name, corr, Jsonb(payload or {}), ago_min))


def sendback(cur, archived=True):
    cur.execute("insert into artifacts(type, correlation_id, payload) values ('Judgment','github:o/r#5',%s)",
                (Jsonb({"stage": "judged", "category": "inspection", "verdict": "adopt", "pr_url": PR, "defect": {"rubric_id": "PR-002"}}),))
    ev(cur, "pr.opened", "github:o/r#5", {"pr_url": PR}, 120)
    if archived:
        ev(cur, "handoff.archived", "github:o/r#5", {"blob_key": "tickets/o/r/5/handoff-1.tar.gz"}, 5)


def stoppage(cur, key, minutes, category="factory-bug", corr=None):
    ev(cur, "stoppage.opened", corr, {"key": key, "category": category, "what": "止まった", "detected_by": "watcher",
                                      "started_at": (datetime.now(timezone.utc) - timedelta(minutes=minutes + 5)).isoformat()}, minutes + 5)
    ev(cur, "stoppage.closed", None, {"key": key, "closed_by": "watcher", "remedy": "直した"}, 5)


def test_picks_archived_sendbacks_and_long_stoppages_alternately(cur):
    sendback(cur)
    stoppage(cur, "idle:1", 90, "idle")
    stoppage(cur, "job:9", 20)                 # 短い
    stoppage(cur, "budget:17", 300, "budget")  # 予算の使い切りは分析しない
    stoppage(cur, "drain:1", 120, "human")     # 人の一時停止も
    got = case.pick_incidents(cur, limit=5)
    assert [i["key"] for i in got] == [f"sendback:{PR}", "stoppage:idle:1"]
    ev(cur, "case.enqueued", None, {"incident": f"sendback:{PR}"})
    assert [i["key"] for i in case.pick_incidents(cur, limit=5)] == ["stoppage:idle:1"]


def test_sendback_waits_for_archive(cur):
    sendback(cur, archived=False)
    assert case.pick_incidents(cur) == []


def test_build_input_has_judgments_and_ticket_timeline(cur):
    sendback(cur)
    inc = case.pick_incidents(cur)[0]
    inp = case.build_input(cur, inc)
    assert inp["correlation_id"] == "github:o/r#5" and inp["incident"]["kind"] == "sendback"
    assert inp["judgments"][0]["verdict"] == "adopt"
    assert [e["name"] for e in inp["timeline"]] == ["pr.opened", "handoff.archived"]


def session_jsonl():
    rows = [{"type": "user", "message": {"content": "秘密の指示"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "予約枠をスロットと呼ぶ。token=ghp_" + "a" * 30},
                                                          {"type": "tool_use", "name": "Bash", "input": {"command": "cargo test"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "大量の出力"}]}}]
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode()


def test_cleanse_session_keeps_agent_text_and_masks_secrets():
    text = case.cleanse_session(session_jsonl())
    assert "予約枠をスロットと呼ぶ" in text and "[Bash] cargo test" in text
    assert "ghp_" not in text and "大量の出力" not in text and "秘密の指示" not in text


def test_attach_archive_reads_handoff_files_and_session():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in {"./jobs/3/summary.md": "報告".encode(), "./ticket.json": b"{}", "./agent/claude-code/s.jsonl": session_jsonl(),
                           "./jobs/3/diff.patch": b"big"}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    inp = case.attach_archive({"incident": {"key": "k"}}, buf.getvalue())
    assert set(inp["handoff_files"]) == {"jobs/3/summary.md", "ticket.json"}
    assert "スロット" in inp["session_excerpt"]


def test_supervisor_enqueues_up_to_per_day_and_record_is_once(cur, monkeypatch):
    monkeypatch.setattr(supervisor, "REPOS", ["o/r"])
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    monkeypatch.setattr(supervisor, "CASE_REVIEW", True)
    monkeypatch.setattr(supervisor, "CASE_REVIEW_DEFS", TAX)
    monkeypatch.setattr(supervisor, "over_budget", lambda c: None)
    sendback(cur)
    stoppage(cur, "idle:1", 90, "idle")
    stoppage(cur, "job:8", 70, corr="github:o/r#8")
    supervisor.case_review_step(cur)
    supervisor.case_review_step(cur)   # 2 回目は積まない
    cur.execute("select correlation_id, repo, issue_number, kind from jobs order by id")
    rows = cur.fetchall()
    got = [(r["correlation_id"], r["repo"], r["issue_number"]) for r in rows]
    assert got[0] == (f"case:sendback:{PR}", "o/r", 5)   # チケットのない停止は issue 0、ある停止はそのチケットに付ける
    assert set(got[1:]) == {("case:stoppage:idle:1", "o/r", 0), ("case:stoppage:job:8", "o/r", 8)}
    cur.execute("select payload from events where name='job.enqueued' order by id limit 1")
    inp = cur.fetchone()["payload"]["input"]
    result = {"kind": "case_review", "ok": True, "report": {"summary": "s", "viewpoints": [], "root_causes": ["r"],
                                                          "learnings": [{"theme": 4, "statement": "用語", "target": "term_alignment",
                                                                         "proposal": "p", "recurring": True}]}}
    assert case.record(cur, f"case:sendback:{PR}", None, inp, result)
    assert not case.record(cur, f"case:sendback:{PR}", None, inp, result)
    cur.execute("select payload from artifacts where type='CaseAnalysis'")
    p = cur.fetchone()["payload"]
    assert p["incident"] == f"sendback:{PR}" and p["learnings"][0]["theme"] == 4 and p["had_session"] is False
