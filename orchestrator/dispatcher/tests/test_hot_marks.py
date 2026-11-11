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
