"""操業コンソールのページ構成（2026-11-07）: ページの URL に画面を返すこと、ホームの指標の推移を操業レポートの中身から読むこと、
ラインのカードに元のチケット（出どころと URL）が付くこと。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from console_page import APP_PATH, PAGE  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")


def test_app_paths():
    for p in ("/inbox", "/inbox/", "/inbox/choice%3Aexit_capture", "/inbox/release%3Ao%2Fr%400.9.3", "/line", "/ship", "/usage", "/help", "/settings", "/settings/runners"):
        assert APP_PATH.fullmatch(p), p
    for p in ("/api/state", "/review/1", "/usage/body", "/inbox/a/b", "/settings/../x", "/healthz"):
        assert not APP_PATH.fullmatch(p), p
    assert "__TOKEN__" in PAGE and '"""' not in PAGE


@pytest.fixture()
def cur():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    os.environ.setdefault("DATABASE_URL", URL)
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def test_trends_reads_latest_factory_health_report(cur):
    import console
    from psycopg.types.json import Jsonb
    assert console.trends(cur) is None   # レポートがまだなければ出さない
    rep = {"kind": "report.factory_health", "days": ["2026-11-06", "2026-11-07"], "level": "warn", "summary": ["消費が多い"],
           "charts": [{"section": "cost", "label": "消費（API 単価に換算）", "title": "消費（USD）"}, {"section": "x", "label": "ない", "title": "ない"}],
           "sections": [{"id": "cost", "items": [{"kind": "metric", "label": "消費（API 単価に換算）", "value": 612.5, "unit": "USD", "delta": 120, "level": "warn",
                                                 "series": [492.5, 612.5]}, {"kind": "note", "text": "メモ"}]}]}
    cur.execute("insert into events(name, payload) values ('report.built', %s)", (Jsonb({"kind": "report.factory_health", "report": {**rep, "summary": ["古い"]}}),))
    cur.execute("insert into events(name, payload) values ('report.built', %s)", (Jsonb({"kind": "report.product", "report": {"charts": []}}),))
    cur.execute("insert into events(name, payload) values ('report.built', %s)", (Jsonb({"kind": "report.factory_health", "report": rep}),))
    t = console.trends(cur)
    assert t["summary"] == ["消費が多い"] and t["days"] == rep["days"] and t["level"] == "warn"
    assert t["items"] == [{"title": "消費（USD）", "value": 612.5, "unit": "USD", "delta": 120, "level": "warn", "series": [492.5, 612.5]}]   # 無い指標は飛ばす


def test_line_cards_carry_ticket_source(cur):
    import console
    cur.execute("insert into tickets(source, source_ref, repo, title, url, state) values ('github', 'o/r#5', 'o/r', 'チケット', 'https://github.com/o/r/issues/5', 'planned')")
    it = next(i for i in console.snapshot(cur)["items"] if i.get("ref") == "o/r#5")
    assert it["source"] == "github" and it["url"] == "https://github.com/o/r/issues/5"
