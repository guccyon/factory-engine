"""知見の制限付きの保管先（ADR 0024 の 5）: 専用スキーマと専用の利用者・中身を出さない一覧・読んだ記録・retired。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL（管理者の利用者）を向けて動かす（なければ飛ばす）。
保管先の利用者の接続は、テストの中で factory_restricted に合言葉を付けて作る。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    import psycopg
    from psycopg.conninfo import make_conninfo
    from psycopg.rows import dict_row

    import jobdb
    import restricted

SECRET = "影響を受けた生徒: 山田 太郎（yamada@example.com）"
PW = "restricted-test-pw"


def connect(**over):
    return psycopg.connect(make_conninfo(URL, **over), row_factory=dict_row, autocommit=True)


@pytest.fixture()
def cur():
    conn = connect()
    c = conn.cursor()
    c.execute(f"alter role factory_restricted login password '{PW}'")
    c.execute("truncate events restart identity cascade")
    yield c
    conn.close()


@pytest.fixture()
def rcur(cur):
    conn = connect(user="factory_restricted", password=PW)
    c = conn.cursor()
    c.execute("truncate restricted.reads, restricted.records restart identity cascade")
    yield c
    conn.close()


def test_events_carry_no_content_and_list_has_no_body(rcur, cur):
    rid = restricted.add(rcur, cur, "guccyon/rally", SECRET, ["K-0007"], "admin")
    assert rid == "R-0001"
    cur.execute("select payload::text as p from events")
    assert all("山田" not in r["p"] for r in cur.fetchall())
    listed = restricted.list_records(rcur)
    assert [r["rid"] for r in listed] == ["R-0001"] and "body" not in listed[0]


def test_read_records_who_and_why(rcur, cur):
    rid = restricted.add(rcur, cur, "guccyon/rally", SECRET, [], "admin")
    assert restricted.read(rcur, cur, rid, "admin", "障害の再発の確認") == SECRET
    rcur.execute("select read_by, purpose from restricted.reads")
    assert rcur.fetchone() == {"read_by": "admin", "purpose": "障害の再発の確認"}
    cur.execute("select payload from events where name='restricted.read'")
    assert cur.fetchone()["payload"] == {"id": rid, "by": "admin", "purpose": "障害の再発の確認"}
    assert restricted.list_records(rcur)[0]["reads"] == 1


@pytest.mark.parametrize("kwargs", [{"product": "rally"}, {"body": "  "}, {"knowledge_ids": ["K-1"]}])
def test_add_rejects_bad_input(rcur, cur, kwargs):
    args = {"product": "guccyon/rally", "body": SECRET, "knowledge_ids": [], "by": "admin", **kwargs}
    with pytest.raises(restricted.RestrictedError):
        restricted.add(rcur, cur, **args)


def test_read_needs_purpose_and_valid_id(rcur, cur):
    rid = restricted.add(rcur, cur, "guccyon/rally", SECRET, [], "admin")
    for r, p in ((rid, " "), ("R-9999", "確認"), ("K-0001", "確認")):
        with pytest.raises(restricted.RestrictedError):
            restricted.read(rcur, cur, r, "admin", p)
    rcur.execute("select count(*) as n from restricted.reads")
    assert rcur.fetchone()["n"] == 0


def test_retire_drops_content_but_keeps_id_and_trail(rcur, cur):
    rid = restricted.add(rcur, cur, "guccyon/rally", SECRET, [], "admin")
    restricted.read(rcur, cur, rid, "admin", "確認")
    assert restricted.retire(rcur, cur, rid, "admin", "保管の期限")
    assert not restricted.retire(rcur, cur, rid, "admin", "二度目")
    with pytest.raises(restricted.RestrictedError):
        restricted.read(rcur, cur, rid, "admin", "確認")
    rcur.execute("select body from restricted.records")
    assert rcur.fetchone()["body"] == ""
    assert restricted.add(rcur, cur, "guccyon/rally", "次", [], "admin") == "R-0002"   # id は再利用しない
    r = restricted.list_records(rcur, "guccyon/rally")
    assert r[0]["retired_at"] and r[0]["reads"] == 1


def test_other_users_cannot_touch_the_schema(rcur, cur):
    restricted.add(rcur, cur, "guccyon/rally", SECRET, [], "admin")
    cur.execute("drop role if exists restricted_probe")
    cur.execute("create role restricted_probe login")
    try:
        with connect(user="restricted_probe") as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("select body from restricted.records")
    finally:
        cur.execute("drop role restricted_probe")


def test_job_user_cannot_connect_to_control_plane(cur):
    dbname = cur.connection.info.dbname
    jobdb._READY.clear()
    jobdb.ensure_role(cur, "factory_job_test", "job-pw", dbname)
    jobdb.ensure_role(cur, "factory_job_test", "job-pw", dbname)   # 2 度目も通る（周回ごとに 1 度）
    jobdb._READY.clear()
    jobdb.ensure_role(cur, "factory_job_test", "job-pw", dbname)   # 既にある利用者でも通る
    jobdb.create(cur, "job_test_1", "factory_job_test")
    try:
        cur.execute("select rolsuper, rolcreatedb, rolcreaterole from pg_roles where rolname='factory_job_test'")
        assert cur.fetchone() == {"rolsuper": False, "rolcreatedb": True, "rolcreaterole": False}
        with connect(user="factory_job_test", password="job-pw", dbname="job_test_1") as c:
            c.execute("create table t (id int)")   # 自分のジョブの DB は持ち物
            c.execute("create database job_test_sqlx")   # sqlx::test がテストごとに DB を作れる
            c.execute("drop database job_test_sqlx")
        with pytest.raises(psycopg.OperationalError):
            connect(user="factory_job_test", password="job-pw")   # 制御プレーンの DB には繋げない
    finally:
        cur.execute("drop database if exists job_test_1 with (force)")
        cur.execute("drop role factory_job_test")
        cur.execute(f"grant connect on database {dbname} to public")
