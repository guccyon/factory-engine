"""事業部の様子の補い（console_live.py）: 工程とジョブの種類の対応に漏れがない・推移は jobs の時刻から数え直す

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import console_live  # noqa: E402
import line  # noqa: E402
import usage  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")


def test_every_kind_has_one_step():
    kinds = [k for st in console_live.STEPS for k in st["kinds"]]
    assert len(kinds) == len(set(kinds))
    for k in [*usage.KIND_JA, *line.GOVERNANCE_KINDS, *line.DESK_KINDS, "qa"]:
        assert k in kinds, k
        assert k in console_live.KIND_JA, k


@pytest.fixture()
def cur():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def test_trend(cur):
    now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
    h = lambda n: now - timedelta(hours=n)  # noqa: E731
    # 5 時間前に積んで 4 時間前に始め、2 時間前に終わった。1 時間前に積んでまだ順番待ち
    cur.execute("""insert into jobs(correlation_id, repo, issue_number, state, enqueued_at, started_at, finished_at) values
                   ('github:o/r#1', 'o/r', 1, 'succeeded', %s, %s, %s), ('github:o/r#2', 'o/r', 2, 'queued', %s, null, null)""",
                (h(5), h(4), h(2), h(1)))
    tr = {p["at"]: p for p in console_live.trend(cur, now)}
    assert len(tr) == console_live.TREND_HOURS * 60 // console_live.BUCKET_MIN + 1
    assert (tr[h(4.5)]["running"], tr[h(4.5)]["queued"]) == (0, 1)
    assert (tr[h(3)]["running"], tr[h(3)]["queued"]) == (1, 0)
    assert (tr[h(0.5)]["running"], tr[h(0.5)]["queued"]) == (0, 1)
