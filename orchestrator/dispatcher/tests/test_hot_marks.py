"""衝突しやすいファイルの印（hot_marks.py、ADR 0038 の追記 2026-11-11）: 直近の PR の変更回数の上位で、衝突の多いファイルに付け、期間で外す"""
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import process_learn
import hot_marks as hm  # noqa: E402

HOT = "apps/admin-web/screenshots/screens.ts"


def test_candidates_need_rank_and_conflicts(monkeypatch):
    monkeypatch.setattr(hm, "TOP_FILES", 2)
    changes = Counter({HOT: 20, "CHANGELOG.md": 40, "a.ts": 15, "b.ts": 1})
    conf = {HOT: {"conflicts": 3}, "CHANGELOG.md": {"conflicts": 9}, "b.ts": {"conflicts": 5}, "a.ts": {"conflicts": 2}}
    got = hm.candidates(changes, conf)
    assert list(got) == [HOT] and got[HOT]["rank"] == 1   # CHANGELOG.md は除く。a.ts は衝突が足りない。b.ts は上位 2 に入らない


def test_candidates_cut_at_top(monkeypatch):
    monkeypatch.setattr(hm, "TOP_FILES", 1)
    changes = Counter({"x.ts": 30, HOT: 20})
    assert hm.candidates(changes, {HOT: {"conflicts": 4}}) == {}   # 上位に入らない


@pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")
def test_step_marks_then_unmarks_after_quiet_days():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    cur = conn.cursor()
    cur.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)

    def conflict(n, at):
        cur.execute("insert into jobs(correlation_id, repo, issue_number, state) values (%s,'o/r',%s,'succeeded') returning id", (f"github:o/r#{n}", n))
        jid = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, occurred_at, payload) values ('continue.enqueued',%s,%s,%s,%s)",
                    (f"github:o/r#{n}", jid, at, Jsonb({"reason": "conflict"})))
        process_learn.record_conflict_files(cur, jid, f"github:o/r#{n}", [HOT])

    calls = []

    def gh(method, path, body=None):
        calls.append(path)
        if "/pulls?" in path:
            return [{"number": i, "merged_at": (t0 + timedelta(hours=i)).isoformat()} for i in range(1, 6)] if "page=1" in path else []
        if path.endswith("files?per_page=100&page=1"):
            return [{"filename": HOT}, {"filename": "x.ts"}]
        return []

    for i in range(3):
        conflict(100 + i, t0 + timedelta(hours=2 + i))
    cur.execute("update events set occurred_at=%s where name='conflict.files'", (t0 + timedelta(hours=3),))
    res = hm.step(cur, gh, ["o/r"], now=t0 + timedelta(days=1))
    assert res["marked"] == [f"o/r:{HOT}"] and HOT in hm.active(cur)
    n_files = sum(1 for p in calls if "/files" in p)
    hm.step(cur, gh, ["o/r"], now=t0 + timedelta(days=2))
    assert sum(1 for p in calls if "/files" in p) == n_files   # PR の変更ファイルは pr.files から読む
    res = hm.step(cur, gh, ["o/r"], now=t0 + timedelta(days=9))
    cur.execute("update events set occurred_at=%s where name='hotfile.marked'", (t0 + timedelta(days=1),))
    res = hm.step(cur, gh, ["o/r"], now=t0 + timedelta(days=9))
    assert res["unmarked"] == [f"o/r:{HOT}"] and HOT not in hm.active(cur)
    res = hm.step(cur, gh, ["o/r"], now=t0 + timedelta(days=10))
    assert res["marked"] == []   # 外した後に新しい衝突がなければ付け直さない
    conn.close()


def test_shape_by_kind_and_append_ratio():
    assert hm.shape("apps/backend/README.md", 90, 10, False) == "append"
    assert hm.shape("specs/contracts/api/admin-web/openapi.yaml", 50, 40, False) == "content"
    assert hm.shape("apps/admin-web/screenshots/screens.ts", 100, 0, False) == "registry"   # コードは断片にしにくい
    assert hm.shape("apps/admin-web/src/api/schema.ts", 10, 10, True) == "generated"
    assert hm.shape("x.md", 0, 0, False) == "unknown"


def test_concurrency_counts_overlapping_open_prs():
    t = datetime(2026, 11, 1, tzinfo=timezone.utc)
    h = lambda n: t + timedelta(hours=n)  # noqa: E731
    assert hm.concurrency([(h(0), h(3)), (h(1), h(4)), (h(2), h(5)), (h(6), h(7))]) == 3
    assert hm.concurrency([(h(0), h(1)), (h(1), h(2))]) == 1   # 閉じた時刻に開いたものは重ならない
    assert hm.concurrency([]) == 0


def test_aggregate_candidates_before_conflicts():
    """衝突がなくても、同時に開いた PR が多い追記型は候補。登録の箇所と中身は候補にしない"""
    changes = Counter({"README.md": 10, "screens.ts": 12, "openapi.yaml": 11, "CHANGELOG.md": 30, "quiet.md": 9})
    prof = {"README.md": {"concurrent": 4}, "screens.ts": {"concurrent": 5}, "openapi.yaml": {"concurrent": 5},
            "CHANGELOG.md": {"concurrent": 9}, "quiet.md": {"concurrent": 1}}
    shapes = {"README.md": "append", "screens.ts": "registry", "openapi.yaml": "content", "quiet.md": "append"}
    got = hm.aggregate_candidates(changes, prof, shapes, set())
    assert list(got) == ["README.md"] and got["README.md"]["shape"] == "append" and got["README.md"]["concurrent"] == 4
    assert list(hm.aggregate_candidates(changes, prof, shapes, {"quiet.md"})) == ["README.md", "quiet.md"]   # 印のある追記型は同時数を問わない


@pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")
def test_step_records_aggregate_candidates_and_stats():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    cur = conn.cursor()
    cur.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    LIST = "tools/knowledge-enforcement-check/README.md"

    def gh(method, path, body=None):
        if "/pulls?" in path:
            return [{"number": i, "created_at": (t0 + timedelta(minutes=10 * i)).isoformat(),
                     "merged_at": (t0 + timedelta(hours=2, minutes=i)).isoformat()} for i in range(1, 5)] if path.endswith("&page=1") else []
        if path.endswith("files?per_page=100&page=1"):
            return [{"filename": LIST, "additions": 1, "deletions": 0}, {"filename": "x.ts", "additions": 3, "deletions": 3}]
        return {}

    res = hm.step(cur, gh, ["o/r"], now=t0 + timedelta(days=1))
    assert res["candidates"] == [f"o/r:{LIST}"] and res["marked"] == []
    [c] = hm.aggregate_candidates_latest(cur, "o/r")
    assert c["file"] == LIST and c["shape"] == "append" and c["concurrent"] == 4 and c["shape_ja"] == "追記型"
    cur.execute("select payload from events where name='pr.files' order by id limit 1")
    assert cur.fetchone()["payload"]["stats"][LIST] == [1, 0]   # 行数も pr.files に残す
    conn.close()


def test_declarations_read_and_default():
    """宣言の読み口（ADR 0051 の 2）。宣言がない・読めなければ CHANGELOG.md だけ"""
    import base64
    decl = "aggregates:\n  - path: README.md\n    fragments: readme.d/\n    compile: append_section\n  - fragments: x.d/\n"
    gh = lambda m, p, b=None: {"content": base64.b64encode(decl.encode()).decode()}  # noqa: E731
    assert hm.declared(gh, "o/r") == {"README.md"}   # path のない行は数えない

    def missing(m, p, b=None):
        raise RuntimeError("404")
    assert hm.declared(missing, "o/r") == {"CHANGELOG.md"}


def test_declared_files_are_not_candidates():
    changes = Counter({HOT: 20, "README.md": 10})
    assert hm.candidates(changes, {HOT: {"conflicts": 3}}, hm.EXEMPT | {HOT}) == {}
    prof = {"README.md": {"concurrent": 4}}
    assert hm.aggregate_candidates(changes, prof, {"README.md": "append"}, set(), hm.EXEMPT | {"README.md"}) == {}


@pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")
def test_step_unmarks_declared_file_same_day():
    """取りまとめに宣言したファイルは、衝突が残っていても印をその日のうちに外し、付け直さない"""
    import base64
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    cur = conn.cursor()
    cur.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    LIST = "tools/knowledge-enforcement-check/README.md"
    state = {"declared": False}

    def gh(method, path, body=None):
        if path.endswith("aggregates.yaml"):
            if not state["declared"]:
                raise RuntimeError("404")
            decl = f"aggregates:\n  - path: {LIST}\n    fragments: knowledge.d/\n    compile: sorted_list\n"
            return {"content": base64.b64encode(decl.encode()).decode()}
        if "/pulls?" in path:
            return [{"number": i, "merged_at": (t0 + timedelta(hours=i)).isoformat()} for i in range(1, 6)] if "page=1" in path else []
        if path.endswith("files?per_page=100&page=1"):
            return [{"filename": LIST, "additions": 1, "deletions": 0}]
        return {}

    for i in range(3):
        cur.execute("insert into jobs(correlation_id, repo, issue_number, state) values (%s,'o/r',%s,'succeeded') returning id",
                    (f"github:o/r#{100 + i}", 100 + i))
        jid = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, occurred_at, payload) values ('continue.enqueued',%s,%s,%s,%s)",
                    (f"github:o/r#{100 + i}", jid, t0 + timedelta(hours=2 + i), Jsonb({"reason": "conflict"})))
        process_learn.record_conflict_files(cur, jid, f"github:o/r#{100 + i}", [LIST])
    cur.execute("update events set occurred_at=%s where name='conflict.files'", (t0 + timedelta(hours=3),))
    res = hm.step(cur, gh, ["o/r"], now=t0 + timedelta(days=1))
    assert res["marked"] == [f"o/r:{LIST}"]
    state["declared"] = True
    res = hm.step(cur, gh, ["o/r"], now=t0 + timedelta(days=2))   # 7 日たたず、衝突も残っているが外す
    assert res["unmarked"] == [f"o/r:{LIST}"] and LIST not in hm.active(cur)
    assert res["candidates"] == []   # 宣言済みは取りまとめの候補にも出さない
    cur.execute("select payload->>'why' as why from events where name='hotfile.unmarked'")
    assert "取りまとめに宣言した" in cur.fetchone()["why"]
    res = hm.step(cur, gh, ["o/r"], now=t0 + timedelta(days=3))
    assert res["marked"] == [] and res["unmarked"] == []
    conn.close()
