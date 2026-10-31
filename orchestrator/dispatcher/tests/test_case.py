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


# ------------------------------------------------------------------ 知見の変更セットの Issue（ADR 0024 の 3）
def analyzed(cur, incident, learnings, ticket="github:o/r#5"):
    ev(cur, "case.analyzed", f"case:{incident}", {
        "incident": incident, "kind": "sendback", "ticket": ticket, "summary": "仕様の言葉が役割の間でずれた\n二行目",
        "viewpoints": [{"viewpoint": "communication", "applies": True, "whys": ["受け入れ条件の語が違った", "用語集を引かなかった"],
                        "root_cause": "用語集を引く手順がない", "confidence": "mid"},
                       {"viewpoint": "spec", "applies": False, "whys": [], "root_cause": "", "confidence": "low"}],
        "root_causes": ["用語集を引く手順がない（連絡は someone@example.com）"], "learnings": learnings, "had_session": True})


def L(theme, statement="学び"):
    return {"theme": theme, "statement": statement, "target": "term_alignment", "proposal": "用語集を引く", "recurring": True}


def fake_gh(calls, exists=True):
    import urllib.error

    def gh(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            if not exists:
                raise urllib.error.HTTPError(path, 404, "nf", {}, None)
            return {"name": "README.md"}
        return {"number": 40 + len(calls), "html_url": f"https://github.com/o/r/issues/{40 + len(calls)}"}
    return gh


def test_pending_knowledge_keeps_first_themes_only(cur):
    import yaml
    themes = case.first_themes(yaml.safe_load(open(TAX, encoding="utf-8")))
    assert set(themes) == {4, 6}
    analyzed(cur, "sendback:a", [L(4), L(2)])
    analyzed(cur, "sendback:b", [L(1)])          # 最初に回すテーマの学びがない分析は起票しない
    analyzed(cur, "stoppage:c", [L(6)])
    got = case.pending_knowledge(cur, themes, 5)
    assert [a["incident"] for a in got] == ["sendback:a", "stoppage:c"]
    assert [x["theme"] for x in got[0]["learnings"]] == [4]


def test_knowledge_issue_body_scrubs_personal_data():
    a = {"incident": "sendback:x", "kind": "sendback", "event_id": 9, "summary": "生徒の連絡先 a.b@example.com が 192.168.0.3 の build.local に出た",
         "viewpoints": [{"viewpoint": "process", "applies": True, "whys": ["電話 090-1234-5678"], "root_cause": "r", "confidence": "high"}],
         "root_causes": ["token=abcdef123"], "learnings": [L(6, "id 123e4567-e89b-12d3-a456-426614174000 の予約")]}
    title, body = case.knowledge_issue(a, {6: "工程の進め方"})
    for leaked in ("a.b@example.com", "192.168.0.3", "build.local", "090-1234-5678", "abcdef123", "123e4567-e89b"):
        assert leaked not in title + body
    assert "status: draft" in body and "knowledge_check.py" in body and "テーマ 6（工程の進め方）" in body


def test_supervisor_files_knowledge_issues_up_to_per_day_once(cur, monkeypatch):
    calls = []
    monkeypatch.setattr(supervisor, "REPOS", ["o/r"])
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    monkeypatch.setattr(supervisor, "KNOWLEDGE_ISSUES", True)
    monkeypatch.setattr(supervisor, "KNOWLEDGE_ISSUES_PER_DAY", 2)
    monkeypatch.setattr(supervisor, "CASE_REVIEW_DEFS", TAX)
    monkeypatch.setattr(supervisor, "gh", fake_gh(calls))
    for i in range(3):
        analyzed(cur, f"sendback:{i}", [L(4)])
    supervisor.knowledge_issue_step(cur)
    supervisor.knowledge_issue_step(cur)   # 1 日の上限に達したので積まない
    posts = [c for c in calls if c[0] == "POST"]
    assert len(posts) == 2 and posts[0][1] == "repos/o/r/issues"
    assert posts[0][2]["labels"] == ["factory:planned", "factory:learn"]
    cur.execute("select payload from events where name='knowledge.case_filed' order by id")
    assert [r["payload"]["incident"] for r in cur.fetchall()] == ["sendback:0", "sendback:1"]


def test_supervisor_skips_repo_without_knowledge_root(cur, monkeypatch):
    calls = []
    monkeypatch.setattr(supervisor, "REPOS", ["o/r"])
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    monkeypatch.setattr(supervisor, "KNOWLEDGE_ISSUES", True)
    monkeypatch.setattr(supervisor, "CASE_REVIEW_DEFS", TAX)
    monkeypatch.setattr(supervisor, "gh", fake_gh(calls, exists=False))
    analyzed(cur, "sendback:z", [L(4)])
    supervisor.knowledge_issue_step(cur)
    assert not [c for c in calls if c[0] == "POST"]


def test_knowledge_paths_are_their_own_change_type():
    import yaml
    rules = yaml.safe_load(open(os.path.join(os.path.dirname(TAX), "change_type_rules.yaml"), encoding="utf-8"))
    files = [{"filename": ".factory-config/knowledge/K-0001.yaml"}, {"filename": ".factory-config/knowledge/INDEX.md"}]
    assert supervisor.classify(files, rules) == ["knowledge"]
    d = supervisor.decide(["knowledge"], {"autonomy": {"matrix": {"factory_rule": {"merge": "auto"}}}})
    assert d["table"] == "human_approve"   # 委員会ができるまでは、manifest に無い種別として人の承認に倒れる
