"""停止の記録と通知（ADR 0005）。--quiet で閉じても、始まりを通知した停止は終わりを通知する（#181）。

TEST_DATABASE_URL の Postgres で確かめる（なければ飛ばす）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
os.environ.setdefault("NOTIFIER", "log")

import stoppage  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
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


@pytest.fixture
def sent(monkeypatch):
    xs = []
    monkeypatch.setattr(stoppage, "notify", lambda kind, s, cur=None: xs.append((kind, s.get("key"))))
    return xs


def test_quiet_close_still_notifies_when_open_was_notified(cur, sent):
    stoppage.open_stoppage(cur, "sendback:x", "human", "差し戻し待ち")
    assert stoppage.close_stoppage(cur, "sendback:x", remedy="マージ済み", closed_by="builder", quiet=True)
    assert sent == [("opened", "sendback:x"), ("closed", "sendback:x")]


def test_quiet_close_stays_quiet_when_open_was_quiet(cur, sent):
    stoppage.open_stoppage(cur, "past:y", "env", "過去分", quiet=True)
    assert stoppage.close_stoppage(cur, "past:y", quiet=True)
    assert sent == []


def test_legacy_open_without_flag_counts_as_notified(cur, sent):
    from psycopg.types.json import Jsonb
    cur.execute("insert into events(name, payload, dedupe_key) values ('stoppage.opened', %s, 'stoppage.opened:old:z')",
                (Jsonb({"key": "old:z", "category": "human", "what": "古い記録"}),))
    assert stoppage.close_stoppage(cur, "old:z", quiet=True)
    assert sent == [("closed", "old:z")]


def test_close_without_quiet_notifies(cur, sent):
    stoppage.open_stoppage(cur, "past:w", "env", "過去分", quiet=True)
    assert stoppage.close_stoppage(cur, "past:w")
    assert sent == [("closed", "past:w")]
