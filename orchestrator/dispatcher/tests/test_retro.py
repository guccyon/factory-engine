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
