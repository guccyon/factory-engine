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


def test_supervisor_files_knowledge_issues_one_at_a_time(cur, monkeypatch):
    """知見は製品ごとに 1 件ずつ。前の知見の PR がマージされるまで次を起票しない（2026-11-04 オーナー、番号の衝突を避ける）"""
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
    supervisor.knowledge_issue_step(cur)   # 1 件目が流れている間は積まない
    posts = [c for c in calls if c[0] == "POST"]
    assert len(posts) == 1 and posts[0][1] == "repos/o/r/issues"
    assert posts[0][2]["labels"] == ["factory:planned", "factory:learn"]
    cur.execute("select correlation_id from events where name='knowledge.case_filed'")
    first = cur.fetchone()["correlation_id"]
    ev(cur, "pr.merged", first, {"pr_url": "https://github.com/o/r/pull/90"})
    supervisor.knowledge_issue_step(cur)   # 済んだので次を 1 件
    supervisor.knowledge_issue_step(cur)
    cur.execute("select payload from events where name='knowledge.case_filed' order by id")
    assert [r["payload"]["incident"] for r in cur.fetchall()] == ["sendback:0", "sendback:1"]
    cur.execute("select correlation_id from events where name='knowledge.case_filed' order by id desc limit 1")
    ev(cur, "pr.merged", cur.fetchone()["correlation_id"], {"pr_url": "https://github.com/o/r/pull/91"})
    supervisor.knowledge_issue_step(cur)   # 1 日の上限（2 件）に達した
    assert len([c for c in calls if c[0] == "POST"]) == 2


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


# ------------------------------------------------------------------ 工程の改善案の Issue（ADR 0027 の 3）
def process_analyzed(cur, sig="process:conflict_fix:CHANGELOG.md", day="2026-11-03"):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, state, kind) values (%s,'o/r',0,'succeeded','case_review') returning id",
                (f"case:{sig}@{day}",))
    jid = cur.fetchone()["id"]
    bundle = {"signature": sig, "signals": 689, "tickets": 120, "cost": 2919.79, "prev": {"signals": 0, "cost": 0.0}}
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                (f"case:{sig}@{day}", jid, Jsonb({"by": "supervisor", "kind": "case_review",
                                                  "input": {"process": {"bundle": bundle, "window": {"from": "a", "to": "b"}}}})))
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('case.analyzed',%s,%s,%s)",
                (f"case:{sig}@{day}", jid, Jsonb({"incident": f"{sig}@{day}", "kind": "process", "ticket": None,
                                                  "summary": "全 PR が 1 つの CHANGELOG.md に追記して衝突した", "viewpoints": [],
                                                  "root_causes": ["共有ファイルへの追記"], "learnings": [L(6, "変更ごとの断片にする")]})))


def engine_gh(calls, open_issues=()):
    def gh(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return list(open_issues)
        return {"number": 200, "html_url": "https://github.com/e/f/issues/200"}
    return gh


def test_process_issue_body():
    a = {"incident": "process:conflict_fix:CHANGELOG.md@2026-11-03", "event_id": 3, "summary": "s", "root_causes": ["r"],
         "learnings": [L(6, "変更ごとの断片にする")], "process": {"bundle": {"signals": 689, "tickets": 120, "cost": 2919.79}, "window": {}}}
    title, body = case.process_issue(a, None)
    assert title == "工程の改善案: process:conflict_fix:CHANGELOG.md（689 件・$2,919.79）"
    assert "変更ごとの断片にする" in body and "人の承認" in body and "すでに減っている見込み" in body


def test_supervisor_files_process_proposal_once_to_engine_repo(cur, monkeypatch):
    calls = []
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    monkeypatch.setattr(supervisor, "PROCESS_ISSUES", True)
    monkeypatch.setattr(supervisor, "ENGINE_REPO", "e/f")
    monkeypatch.setattr(supervisor, "gh", engine_gh(calls))
    process_analyzed(cur)
    supervisor.process_issue_step(cur)
    supervisor.process_issue_step(cur)
    posts = [c for c in calls if c[0] == "POST"]
    assert len(posts) == 1 and posts[0][1] == "repos/e/f/issues" and posts[0][2]["labels"] == ["factory:process-proposal"]
    assert "689 件" in posts[0][2]["title"]
    cur.execute("select payload from events where name='process.proposal_filed'")
    assert [r["payload"]["issue"] for r in cur.fetchall()] == ["https://github.com/e/f/issues/200"]


def test_supervisor_skips_when_same_signature_is_open(cur, monkeypatch):
    calls = []
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    monkeypatch.setattr(supervisor, "PROCESS_ISSUES", True)
    monkeypatch.setattr(supervisor, "gh", engine_gh(calls, [{"title": "工程の改善案: process:conflict_fix:CHANGELOG.md（10 件・$5.00）",
                                                             "html_url": "https://github.com/e/f/issues/9"}]))
    process_analyzed(cur)
    supervisor.process_issue_step(cur)
    assert not [c for c in calls if c[0] == "POST"]
    cur.execute("select payload from events where name='process.proposal_filed'")
    assert cur.fetchone()["payload"]["duplicate_of"] == "https://github.com/e/f/issues/9"


def test_knowledge_issue_carries_narrow_scope():
    """ADR 0040 の決定 8: 分析役が書いた業務・成果物・状況の軸を、知見の scope の下書きとして Issue に載せる（狭く生まれる）"""
    x = {**L(4), "work": ["dev.describe"], "artifacts": ["PR.body", "bad value!"],
         "situation": {"purpose": "bugfix", "product_stage": "", "request_form": "ticket", "code_origin": "factory", "other": "x"}}
    assert case.scope_of(x) == {"work": ["dev.describe"], "artifacts": ["PR.body"], "purpose": ["bugfix"], "request_form": ["ticket"],
                                "code_origin": ["factory"]}
    a = {"incident": "sendback:x", "kind": "sendback", "event_id": 9, "summary": "s", "viewpoints": [], "root_causes": [], "learnings": [x, L(4)]}
    _, body = case.knowledge_issue(a, {4: "言葉と伝達"})
    assert "`{work: [dev.describe], artifacts: [PR.body], purpose: [bugfix], request_form: [ticket], code_origin: [factory]}`" in body
    assert "分析役が書かなかった" in body and "`scope` は学びの「効く範囲」をそのまま写す" in body


def test_build_input_carries_knowledge_used_and_record_keeps_reach(cur):
    """ADR 0040 の決定 8: 分析役にその回に渡した知見を見せ、真因と同じ知見が届かなかったら knowledge.missed を残す"""
    ev(cur, "knowledge.used", "github:o/r#7", {"role": "implement", "entry": "ticket", "ids": ["K-0001"], "work": ["dev.build", "dev.describe"],
                                               "values": {"request_form": ["ticket"]}})
    inp = case.build_input(cur, {"key": "sendback:x", "kind": "sendback", "pr_url": "https://github.com/o/r/pull/8", "corr": "github:o/r#7"})
    assert inp["knowledge_used"] == [{"job_id": None, "role": "implement", "entry": "ticket", "ids": ["K-0001"],
                                      "work": ["dev.build", "dev.describe"], "values": {"request_form": ["ticket"]}}]
    inp["correlation_id"] = "github:o/r#7"
    reach = [{"id": "K-0001", "delivered": True, "role": "implement", "work": ["dev.build"]},
             {"id": "K-0002", "delivered": False, "reason": "other_work", "role": "implement", "work": ["dev.build"]},
             {"id": "K-0003", "delivered": False, "reason": "hypothesis", "role": "implement", "work": []},
             {"id": "bad", "delivered": False, "reason": "x"}]
    assert case.record(cur, "case:sendback:x", None, inp, {"report": {"summary": "s", "knowledge_reach": reach}})
    assert not case.record(cur, "case:sendback:x", None, inp, {"report": {"summary": "s", "knowledge_reach": reach}})   # 1 度だけ
    cur.execute("select name, payload from events where name like 'knowledge.%%' and name <> 'knowledge.used' order by id")
    got = [(r["name"], r["payload"]["id"], r["payload"].get("reason"), r["payload"].get("scope"), r["payload"]["repo"]) for r in cur.fetchall()]
    assert got == [("knowledge.delivered_failed", "K-0001", None, None, "o/r"),
                   ("knowledge.missed", "K-0002", "other_work", True, "o/r"),
                   ("knowledge.missed", "K-0003", "hypothesis", False, "o/r")]
