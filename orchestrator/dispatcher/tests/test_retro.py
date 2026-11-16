"""振り返り（ADR 0061）の制御プレーン側: チケットの数え方、スプリントの区切り、N の自動調整。"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import retro  # noqa: E402

DEFS_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "definitions", "knowledge.yaml")
with open(DEFS_PATH, encoding="utf-8") as f:
    DEFS = yaml.safe_load(f)["retro"]

URL = os.environ.get("TEST_DATABASE_URL")
NOW = datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ N の自動調整（DB なし）
def m(tickets=12, knowledge=None, sendbacks=None):
    ts = [f"o/r#{i}" for i in range(tickets)]
    return retro.metrics(ts, knowledge or [], sendbacks)


def test_defs_are_sane():
    s = DEFS["size"]
    assert s["min"] <= s["initial"] <= s["max"] and s["step"] > 0
    assert "factory:learn" in DEFS["exclude_labels"]
    assert {"production", "planning", "ordering", "prevention", "domain"} <= set(DEFS["prompts"])


def test_metrics_counts_evidence_and_coverage():
    k = [{"id": "K-1", "evidence_tickets": ["o/r#0", "o/r#1", "o/r#2"]}, {"id": "K-2", "evidence_tickets": ["o/r#2", "o/r#3"]}]
    got = m(10, k, ["範囲が広すぎる", "根拠の PR が違う"])
    assert got["median_evidence"] == 2.5 and got["coverage"] == 0.4 and got["broad_sendback_ratio"] == 0.5


def test_local_knowledge_raises_n():
    k = [{"id": f"K-{i}", "evidence_tickets": [f"o/r#{i}"]} for i in range(6)]
    assert retro.adjust(12, m(12, k), DEFS) == (14, "知見 1 件あたりの根拠のチケットが中央値 1 件（局所的）")


def test_abstract_knowledge_lowers_n_within_range():
    k = [{"id": "K-1", "evidence_tickets": ["o/r#0", "o/r#1"]}]   # 12 件中 2 件にしか触れない
    n, why = retro.adjust(12, m(12, k), DEFS)
    assert n == 10 and "抽象的" in why
    assert retro.adjust(8, m(12, k), DEFS)[0] == 8   # 下限


def test_mixed_signals_hold():
    k = [{"id": "K-1", "evidence_tickets": ["o/r#0"]}]   # 局所的で、しかも被覆が低い
    n, why = retro.adjust(12, m(12, k), DEFS)
    assert n == 12 and "両方" in why


def test_empty_streak_raises_n_on_second_time():
    assert retro.adjust(12, m(12, []), DEFS, empty_streak=0)[0] == 12
    assert retro.adjust(12, m(12, []), DEFS, empty_streak=1)[0] == 14


def test_balanced_keeps_n():
    k = [{"id": "K-1", "evidence_tickets": ["o/r#0", "o/r#1", "o/r#2", "o/r#3"]},
         {"id": "K-2", "evidence_tickets": ["o/r#4", "o/r#5", "o/r#6"]}]
    assert retro.adjust(12, m(12, k), DEFS) == (12, "ちょうど良い（合図なし）")


# ------------------------------------------------------------------ スプリントの区切り（DB）
db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")


@pytest.fixture()
def cur():
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def close_ticket(cur, n, at, repo="o/r"):
    cur.execute("insert into tickets(source, source_ref, repo, title, state, updated_at) values ('github',%s,%s,%s,'closed',%s)",
                (f"{repo}#{n}", repo, f"チケット {n}", at))


def fake_gh(labels_by_issue):
    calls = []

    def gh(method, path, body=None):
        calls.append(path)
        n = int(path.rsplit("/", 1)[1])
        return {"title": f"チケット {n}", "labels": [{"name": l} for l in labels_by_issue.get(n, ["factory:planned"])]}
    gh.calls = calls
    return gh


@db
def test_tickets_before_start_are_not_counted(cur):
    close_ticket(cur, 1, NOW - timedelta(days=1))
    since = retro.started_at(cur, "o/r", NOW)
    assert retro.started_at(cur, "o/r", NOW + timedelta(hours=1)) == since   # 一度決めたら動かない
    gh = fake_gh({})
    assert retro.classify(cur, "o/r", gh, DEFS["exclude_labels"], since) == 0 and gh.calls == []


@db
def test_internal_issues_are_excluded_and_asked_once(cur):
    since = retro.started_at(cur, "o/r", NOW)
    close_ticket(cur, 1, NOW + timedelta(minutes=1))
    close_ticket(cur, 2, NOW + timedelta(minutes=2))
    gh = fake_gh({2: ["factory:planned", "factory:learn"]})
    assert retro.classify(cur, "o/r", gh, DEFS["exclude_labels"], since) == 2
    assert retro.classify(cur, "o/r", gh, DEFS["exclude_labels"], since) == 0   # 聞き直さない
    assert len(gh.calls) == 2
    assert [p["ref"] for p in retro.pending(cur, "o/r")] == ["o/r#1"]


@db
def test_sprint_closes_at_n_with_oldest_first(cur):
    since = retro.started_at(cur, "o/r", NOW)
    for i in range(1, 15):
        close_ticket(cur, i, NOW + timedelta(minutes=i))
    retro.classify(cur, "o/r", fake_gh({}), DEFS["exclude_labels"], since)
    s = retro.maybe_close(cur, "o/r", DEFS, NOW + timedelta(hours=1))
    assert s["sprint"] == 1 and s["reason"] == "size" and s["tickets"] == [f"o/r#{i}" for i in range(1, 13)]
    assert [p["ref"] for p in retro.pending(cur, "o/r")] == ["o/r#13", "o/r#14"]
    assert retro.maybe_close(cur, "o/r", DEFS, NOW + timedelta(hours=2)) is None   # 2 件では閉じない


@db
def test_time_floor_closes_small_sprint(cur):
    since = retro.started_at(cur, "o/r", NOW)
    for i in range(1, 4):
        close_ticket(cur, i, NOW + timedelta(days=1, minutes=i))
    retro.classify(cur, "o/r", fake_gh({}), DEFS["exclude_labels"], since)
    assert retro.maybe_close(cur, "o/r", DEFS, NOW + timedelta(days=13)) is None
    s = retro.maybe_close(cur, "o/r", DEFS, NOW + timedelta(days=14, hours=1))
    assert s["reason"] == "time" and len(s["tickets"]) == 3


@db
def test_adjusted_size_is_used_for_next_sprint(cur):
    k = [{"id": f"K-{i}", "evidence_tickets": [f"o/r#{i}"]} for i in range(6)]
    p = retro.record_adjust(cur, "o/r", 1, m(12, k), DEFS)
    assert (p["prev"], p["next"]) == (12, 14)
    retro.record_adjust(cur, "o/r", 1, m(12, k), DEFS)   # 同じスプリントは 1 回だけ記録される
    cur.execute("select count(*) as n from events where name='retro.size_adjusted'")
    assert cur.fetchone()["n"] == 1
    assert retro.size(cur, "o/r", DEFS) == 14
    retro.record_adjust(cur, "o/r", 2, m(12, []), DEFS)
    assert retro.empty_streak(cur, "o/r") == 1


# ------------------------------------------------------------------ 議題・議事録・起票（DB）
LEAD = {"summary": "計画の分け方で 2 件が途中終了した。",
        "knowledge": [{"statement": "画面とAPIを同じチケットにすると途中終了しやすい", "kind": "causal", "theme": 6, "perspective": "計画と分割",
                       "evidence_tickets": ["o/r#1", "o/r#2"], "from_roles": ["プランナー", "実装"], "destination": "knowledge",
                       "acts_on": "context", "work": "計画", "affects": [], "why_universal": "2 件で同じ"},
                      {"statement": "撮影の場所では 4G 以上の回線が使えないことが多い", "kind": "fact", "theme": 3, "perspective": "ドメインの気づき",
                       "evidence_tickets": ["o/r#3"], "from_roles": ["QA"], "destination": "product_docs",
                       "acts_on": "context", "work": "事業部の開発すべて", "affects": [], "why_universal": "利用者の環境"}],
        "rejected": [{"statement": "この画面だけの色", "from_roles": ["デザイナー"], "why": "局所的"}],
        "actions": [{"what": "プランナーの切る基準を見直す", "target": "process", "why": "途中終了", "tickets": ["o/r#1"]}],
        "previous_effect": "", "inventory": {"contradictions": [], "promote": [], "retire": [], "widen": [], "structure": {"needed": False, "proposal": ""}},
        "docs_structure": {"needed": False, "proposal": ""}}
RESULT = {"kind": "retro", "ok": True, "facts": {"items": 3, "stale": [], "unused": []},
          "opinions": [{"role": "実装", "perspective": "計画と分割", "tickets": ["o/r#1"], "kind": "problem", "observation": "途中で切れた", "proposal": "分ける"}],
          "failed_roles": [], "lead": LEAD, "cost_usd": 4.2, "model": "m"}
SPRINT = {"repo": "o/r", "sprint": 1, "size": 12, "reason": "size", "tickets": ["o/r#1", "o/r#2", "o/r#3"],
          "titles": {"o/r#1": "画面A"}, "from": "2026-11-16T03:00:00+00:00", "to": "2026-11-17T03:00:00+00:00"}


@db
def test_build_input_collects_roles_and_material(cur):
    for n, kinds in ((1, ["plan", "implement", "inspect"]), (2, ["implement", "judge"]), (3, ["implement", "qa", "body_fix"])):
        for k in kinds:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, result) values (%s,'o/r',%s,%s,'succeeded','{\"cost_usd\": 1.5}')",
                        (f"github:o/r#{n}", n, k))
        cur.execute("insert into events(name, correlation_id, payload) values ('stoppage.opened',%s,'{\"reason\":\"ci\"}')", (f"github:o/r#{n}",))
    cur.execute("insert into events(name, correlation_id, payload, occurred_at) values ('incident.opened','x',%s,'2026-11-16T12:00:00Z')",
                (__import__("json").dumps({"incident_id": "o/r:9", "severity": "S2", "repo": "o/r"}),))
    inp = retro.build_input(cur, SPRINT, DEFS)
    roles = {r["role"]: r["tickets"] for r in inp["roles"]}
    assert roles["実装"] == ["o/r#1", "o/r#2", "o/r#3"] and "PR 本文の直し" not in roles and len(inp["roles"]) <= DEFS["committee"]["max_roles"]
    t1 = inp["tickets"][0]
    assert t1["title"] == "画面A" and t1["cost_usd"] == 4.5 and t1["stoppages"] == 1
    assert inp["operations"][0]["incident_id"] == "o/r:9"
    assert "planning" in inp["prompts"] and inp["previous"] is None


@db
def test_record_minutes_issues_and_adjust(cur):
    posts, comments = [], []

    def gh(method, path, body=None):
        posts.append((method, path, body))
        return {"html_url": f"https://github.com/{path.split('/')[1]}/{path.split('/')[2]}/issues/{len(posts) + 100}", "number": len(posts) + 100}
    comment = lambda repo, n, text: comments.append((repo, n, text))
    assert retro.record(cur, "o/r", 50, "github:o/r#50", None, SPRINT, RESULT, DEFS, gh, comment, "o/engine")
    assert not retro.record(cur, "o/r", 50, "github:o/r#50", None, SPRINT, RESULT, DEFS, gh, comment, "o/engine")   # スプリントごとに 1 回
    titles = {p[2]["title"]: (p[1], p[2]["labels"]) for p in posts}
    assert titles["知見の変更セット（振り返り S-1）"] == ("repos/o/r/issues", ["factory:planned", "factory:learn"])
    assert titles["ドメインの知識を docs/ に足す（振り返り S-1）"][1] == ["factory:planned", "factory:domain-docs"]
    assert titles["振り返りの打ち手: プランナーの切る基準を見直す"][0] == "repos/o/engine/issues"
    kbody = next(p[2]["body"] for p in posts if p[2]["title"].startswith("知見の変更セット"))
    assert "画面とAPI" in kbody and "4G" not in kbody   # ドメインの知識は docs/ の方へ
    md = comments[0][2]
    assert "**実装**（計画と分割、o/r#1）" in md and "採らなかった候補" in md and "置き場: 製品の docs/" in md
    cur.execute("select payload from events where name='retro.held'")
    held = cur.fetchone()["payload"]
    assert held["roles"] == ["実装"] and len(held["filed"]) == 3
    cur.execute("select correlation_id, payload from events where name='knowledge.case_filed'")
    kf = cur.fetchone()
    assert kf["payload"]["source"] == "retro" and kf["correlation_id"].startswith("github:o/r#")
    cur.execute("select payload from events where name='retro.size_adjusted'")
    adj = cur.fetchone()["payload"]
    assert adj["metrics"]["knowledge"] == 2 and adj["prev"] == 12


@db
def test_supervisor_enqueues_one_retro_per_sprint_and_stops_old_steps(cur, monkeypatch):
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    import supervisor
    monkeypatch.setattr(supervisor, "gh", lambda m, p, b=None: [{"number": 50, "title": retro.ISSUE_TITLE}] if m == "GET" else {})
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('retro.sprint_closed','retro:o/r',%s,'retro.sprint:o/r:1')",
                (__import__("json").dumps(SPRINT),))
    supervisor.enqueue_retro(cur, "o/r", DEFS)
    cur.execute("update jobs set state='succeeded'")
    supervisor.enqueue_retro(cur, "o/r", DEFS)
    cur.execute("select kind, correlation_id from jobs")
    assert [(r["kind"], r["correlation_id"]) for r in cur.fetchall()] == [("retro", "github:o/r#50")]
    cur.execute("select payload from events where name='job.enqueued'")
    assert cur.fetchone()["payload"]["input"]["sprint"] == 1
    monkeypatch.setattr(supervisor, "RETRO", True)
    monkeypatch.setattr(supervisor, "COMMITTEE_WEEKLY", True)
    monkeypatch.setattr(supervisor, "KNOWLEDGE_ISSUES", True)
    monkeypatch.setattr(supervisor, "knowledge_root_exists", lambda r: (_ for _ in ()).throw(AssertionError("呼ばない")))
    supervisor.committee_weekly_step(cur)
    supervisor.knowledge_issue_step(cur)
