"""操業コンソールのページ構成（2026-11-07）: ページの URL に画面を返すこと、ホームの指標の推移を操業レポートの中身から読むこと、
ラインのカードに元のチケット（出どころと URL）が付くこと。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from console_page import APP_PATH, CATALOG, PAGE, STATIC_DIR, static_file  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")


def test_app_paths():
    for p in ("/inbox", "/inbox/", "/inbox/choice%3Aexit_capture", "/inbox/release%3Ao%2Fr%400.9.3", "/line", "/ship", "/usage", "/cost", "/maint", "/live", "/ask", "/ask/new", "/help", "/settings", "/settings/runners"):
        assert APP_PATH.fullmatch(p), p
    for p in ("/api/state", "/review/1", "/usage/body", "/inbox/a/b", "/settings/../x", "/healthz"):
        assert not APP_PATH.fullmatch(p), p
    assert "__TOKEN__" in PAGE and '"""' not in PAGE


def test_static_files():
    """画面は console_static/ の静的ファイル（ADR 0059）。殻が読む CSS・JS はどれも配れて、外部から何も読まない"""
    import re
    refs = re.findall(r'(?:href|src)="/static/([^"]+)"', PAGE + CATALOG)
    assert {"brand.css", "tokens.css", "components.css", "console.css", "console.js", "catalog.js"} <= set(refs)
    for name in refs:
        body, ctype = static_file(name)
        assert body and ctype.startswith(("text/css", "text/javascript")), name
    assert "http://" not in PAGE + CATALOG and "https://" not in PAGE + CATALOG
    for bad in ("../console.py", "index.html", ".hidden.css", "x/y.css", "nope.css", "Brand.css"):
        assert static_file(bad) is None, bad


def test_layers():
    """部品とページの CSS はブランドの層の原色を直接使わない（刷新は brand.css だけを替える。ADR 0059 の 6）"""
    import re
    def read(n):
        with open(os.path.join(STATIC_DIR, n), encoding="utf-8") as f:
            return f.read()
    prim = re.compile(r"var\(--(brand|neutral|green|amber|red|blue)-\d+")
    for name in ("components.css", "console.css", "catalog.html"):
        assert not prim.search(read(name)), name
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", read("components.css"))
    defined = set(re.findall(r"--([a-z0-9-]+):", read("brand.css") + read("tokens.css") + read("console.css")))   # 骨組みの寸法（--side-w など）は console.css
    used = set(re.findall(r"var\(--([a-z0-9-]+)", read("components.css") + read("console.css")))
    assert used <= defined, used - defined


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


def test_failed_jobs_of_committee_show_their_repo(cur):
    """委員会のジョブ（committee:<PR の URL>）の失敗が、PR の URL をリポジトリとして絞り込みに出さない（11-08）"""
    import console
    from psycopg.types.json import Jsonb
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state) values ('committee:https://github.com/o/r/pull/13','o/r',13,'committee_review','failed') returning id")
    jid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.finished','committee:https://github.com/o/r/pull/13',%s,%s)",
                (jid, Jsonb({"state": "failed", "error": "exit=1"})))
    it = next(i for i in console.snapshot(cur)["items"] if i.get("job_id") == jid)
    assert it["ref"] == "o/r#13"
