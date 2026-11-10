"""工場の中での一次調査の、調査用の利用者とビュー（postgres/init/008_investigate.sql）と、SQL を流す口（investigate_db.py）。ADR 0048 の順番 2。

形の検査は DB なしで動く。権限の検査は、空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL（管理者）を向けて動かす（なければ飛ばす）。
テストの中で factory_investigate に一時の合言葉を付けて繋ぎ、終わったら NOLOGIN に戻す。
"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import investigate_db as idb  # noqa: E402

ENGINE = Path(__file__).resolve().parents[3]
URL = os.environ.get("TEST_DATABASE_URL")


def test_values_match_definitions():
    db = yaml.safe_load((ENGINE / "definitions" / "investigate.yaml").read_text(encoding="utf-8"))["sources"]["db"]
    assert db["statement_timeout_s"] == idb.TIMEOUT_S
    assert db["max_rows"] == idb.MAX_ROWS
    sql = (ENGINE / "orchestrator" / "postgres" / "init" / "008_investigate.sql").read_text(encoding="utf-8")
    for v in db["views"]:
        assert f"create or replace view investigate.{v} as" in sql, v
    assert f"statement_timeout = '{idb.TIMEOUT_S}s'" in sql


@pytest.mark.parametrize("sql", [
    "select 1",
    "  WITH x as (select 1) select * from x;",
    "select ';' as semi from investigate.jobs -- 末尾のコメント; drop",
    "/* 前置き */ select 1",
])
def test_check_sql_accepts(sql):
    assert idb.check_sql(sql)


@pytest.mark.parametrize("sql,why", [
    ("", "空"),
    ("select 1; select 2", "1 文だけ"),
    ("select 1; reset role", "1 文だけ"),
    ("update investigate.jobs set state = 'x'", "SELECT か WITH"),
    ("set role factory", "SELECT か WITH"),
    ("-- select\ndelete from jobs", "SELECT か WITH"),
])
def test_check_sql_rejects(sql, why):
    with pytest.raises(idb.Rejected, match=why):
        idb.check_sql(sql)


def test_url_must_be_the_investigate_user(monkeypatch):
    monkeypatch.delenv(idb.ENV, raising=False)
    with pytest.raises(idb.Unavailable):
        idb.url()
    monkeypatch.setenv(idb.ENV, "postgresql://factory:pw@postgres:5432/factory")
    with pytest.raises(idb.Unavailable, match="管理者の接続では流さない"):
        idb.url()
    monkeypatch.setenv(idb.ENV, "postgresql://factory_investigate:pw@postgres:5432/factory")
    assert idb.url()


@pytest.fixture()
def admin():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    psycopg = pytest.importorskip("psycopg")
    conn = psycopg.connect(URL, autocommit=True)
    cur = conn.cursor()
    cur.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state) values ('github:o/r#1', 'o/r', 1, 'implement', 'failed')")
    cur.execute("insert into events(name, correlation_id, job_id, payload) values "
                "('job.finished', 'github:o/r#1', 1, '{\"state\":\"failed\",\"cost_usd\":1.25,\"billable_tokens\":900}'), "
                "('restricted.read', null, null, '{\"record_id\":1,\"purpose\":\"x\"}'), "
                "('judgment.provisional', 'github:o/r#1', 1, '{}'), "
                "('budget.set', null, null, '{\"daily\":300}')")
    cur.execute("alter role factory_investigate login password 'test-only'")
    yield cur
    cur.execute("alter role factory_investigate nologin password null")
    conn.close()


@pytest.fixture()
def inv_url(admin, monkeypatch):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    d = conninfo_to_dict(URL)
    d.update(user="factory_investigate", password="test-only")
    u = make_conninfo(**d)
    monkeypatch.setenv(idb.ENV, u)
    return u


def test_views_read(inv_url):
    r = idb.query("select kind, state, cost_usd from investigate.costs")
    assert r["columns"] == ["kind", "state", "cost_usd"]
    assert r["rows"] == [["implement", "failed", "1.25"]]
    names = {row[0] for row in idb.query("select name from investigate.events")["rows"]}
    assert "restricted.read" not in names and "job.finished" in names
    assert idb.query("select count(*) from investigate.judgments")["rows"] == [[1]]
    assert idb.query("select count(*) from investigate.line_declarations")["rows"] == [[1]]


def test_rows_are_cut(inv_url):
    r = idb.query("select generate_series(1, 20)", max_rows=5)
    assert len(r["rows"]) == 5 and r["truncated"] is True


@pytest.mark.parametrize("sql", [
    "select * from public.jobs",
    "select * from public.events",
    "select * from desk_tokens",
    "select * from restricted.records",
    "select set_config('role', 'factory', true), (select count(*) from public.jobs)",
])
def test_cannot_reach_base_tables(inv_url, sql):
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.Error):
        idb.query(sql)


def test_cannot_write_even_without_read_only(inv_url):
    psycopg = pytest.importorskip("psycopg")
    conn = psycopg.connect(inv_url, autocommit=True)
    try:
        cur = conn.cursor()
        cur.execute("set default_transaction_read_only = off")
        for sql in ["update public.jobs set state = 'cancelled'", "insert into public.events(name) values ('x')",
                    "create table public.x(i int)", "select claim_job()"]:
            with pytest.raises(psycopg.Error):
                cur.execute(sql)
    finally:
        conn.close()


def test_role_defaults(admin):
    admin.execute("select rolcanlogin, rolsuper, rolinherit, rolconfig from pg_roles where rolname = 'factory_investigate'")
    _, sup, inh, cfg = admin.fetchone()
    assert sup is False and inh is False
    assert "default_transaction_read_only=on" in cfg and "statement_timeout=10s" in cfg
