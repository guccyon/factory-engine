"""取りまとめの候補を製品の改善チケットにする（ADR 0051 の 4）"""
import base64
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import supervisor


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def cand(cur, file, rank, shape="append"):
    cur.execute("insert into events(name, payload) values ('hotfile.aggregate_candidate', %s)",
                (Jsonb({"repo": "o/r", "file": file, "rank": rank, "shape": shape, "shape_ja": "追記型", "why": f"{file} がよく変わる"}),))


def test_files_one_issue_per_day_skipping_declared(cur, monkeypatch):
    posted = []
    decl = "aggregates:\n  - path: apps/backend/README.md\n    fragments: apps/backend/readme.d/\n    compile: append_section\n"

    def gh(method, path, body=None):
        if path.endswith("aggregates.yaml"):
            return {"content": base64.b64encode(decl.encode()).decode()}
        posted.append(body)
        return {"number": len(posted), "html_url": f"https://github.com/o/r/issues/{len(posted)}"}

    monkeypatch.setattr(supervisor, "gh", gh)
    monkeypatch.setattr(supervisor, "REPOS", ["o/r"])
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    cand(cur, "apps/backend/README.md", 1)        # 宣言済み
    cand(cur, "apps/admin-web/README.md", 2)
    cand(cur, "tools/x/README.md", 3)
    day1 = datetime(2026, 11, 12, 3, tzinfo=timezone.utc)
    supervisor.aggregate_issue_step(cur, day1)
    supervisor.aggregate_issue_step(cur, day1)   # 1 日 1 件
    [b] = posted
    assert b["title"] == "共有ファイル apps/admin-web/README.md を断片で更新する形にする"
    assert b["labels"] == ["factory:plan", "factory:aggregate"] and "append_section" in b["body"]
    supervisor.aggregate_issue_step(cur, datetime(2026, 11, 13, 3, tzinfo=timezone.utc))
    assert posted[1]["title"].startswith("共有ファイル tools/x/README.md")   # 翌日は次の候補。起票済みのファイルは足さない


def test_generated_body_mentions_generate():
    t, b = supervisor.aggregate_issue_body({"file": "src/api/schema.ts", "shape": "generated", "shape_ja": "生成物", "why": "w"})
    assert "compile: generate" in b and "schema.ts" in t
