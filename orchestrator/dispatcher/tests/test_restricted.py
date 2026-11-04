"""知見の制限付きの保管先（ADR 0024 の 5）: 暗号にして置く・中身を出さない一覧・読んだ記録・鍵の誤り・retired。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    import psycopg
    from psycopg.rows import dict_row

    import restricted

KEY = "k" * 24
SECRET = "影響を受けた生徒: 山田 太郎（yamada@example.com）"


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, restricted_reads, restricted_records restart identity cascade")
    yield c
    conn.close()


def test_body_is_stored_encrypted_and_events_carry_no_content(cur):
    rid = restricted.add(cur, "guccyon/rally", SECRET, ["K-0007"], "admin", KEY)
    assert rid == "R-0001"
    cur.execute("select body from restricted_records")
    assert "山田".encode() not in bytes(cur.fetchone()["body"])
    cur.execute("select payload::text as p from events")
    assert all("山田" not in r["p"] for r in cur.fetchall())
    listed = restricted.list_records(cur)
    assert [r["rid"] for r in listed] == ["R-0001"] and "body" not in listed[0]


def test_read_records_who_and_why_before_returning(cur):
    rid = restricted.add(cur, "guccyon/rally", SECRET, [], "admin", KEY)
    assert restricted.read(cur, rid, "admin", "障害の再発の確認", KEY) == SECRET
    cur.execute("select read_by, purpose from restricted_reads")
    assert cur.fetchone() == {"read_by": "admin", "purpose": "障害の再発の確認"}
    cur.execute("select payload from events where name='restricted.read'")
    assert cur.fetchone()["payload"] == {"id": rid, "by": "admin", "purpose": "障害の再発の確認"}
    assert restricted.list_records(cur)[0]["reads"] == 1


def test_wrong_key_fails_and_the_attempt_is_kept(cur):
    rid = restricted.add(cur, "guccyon/rally", SECRET, [], "admin", KEY)
    with pytest.raises(restricted.RestrictedError):
        restricted.read(cur, rid, "someone", "試し", "x" * 24)
    cur.execute("select name from events where name like 'restricted.read%' order by id")
    assert [r["name"] for r in cur.fetchall()] == ["restricted.read", "restricted.read_failed"]


@pytest.mark.parametrize("kwargs", [
    {"key": "short"},
    {"product": "rally"},
    {"body": "  "},
    {"knowledge_ids": ["K-1"]},
])
def test_add_rejects_bad_input(cur, kwargs):
    args = {"product": "guccyon/rally", "body": SECRET, "knowledge_ids": [], "by": "admin", "key": KEY, **kwargs}
    with pytest.raises(restricted.RestrictedError):
        restricted.add(cur, **args)


def test_read_needs_purpose_and_valid_id(cur):
    rid = restricted.add(cur, "guccyon/rally", SECRET, [], "admin", KEY)
    with pytest.raises(restricted.RestrictedError):
        restricted.read(cur, rid, "admin", " ", KEY)
    with pytest.raises(restricted.RestrictedError):
        restricted.read(cur, "R-9999", "admin", "確認", KEY)
    with pytest.raises(restricted.RestrictedError):
        restricted.read(cur, "K-0001", "admin", "確認", KEY)
    cur.execute("select count(*) as n from restricted_reads")
    assert cur.fetchone()["n"] == 0


def test_retire_drops_content_but_keeps_id_and_trail(cur):
    rid = restricted.add(cur, "guccyon/rally", SECRET, [], "admin", KEY)
    restricted.read(cur, rid, "admin", "確認", KEY)
    assert restricted.retire(cur, rid, "admin", "保管の期限")
    assert not restricted.retire(cur, rid, "admin", "二度目")
    with pytest.raises(restricted.RestrictedError):
        restricted.read(cur, rid, "admin", "確認", KEY)
    cur.execute("select octet_length(body) as n from restricted_records")
    assert cur.fetchone()["n"] == 0
    assert restricted.add(cur, "guccyon/rally", "次", [], "admin", KEY) == "R-0002"   # id は再利用しない
    r = restricted.list_records(cur, "guccyon/rally")
    assert r[0]["retired_at"] and r[0]["reads"] == 1
