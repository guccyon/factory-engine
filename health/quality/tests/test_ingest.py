"""内部品質の取り込み（health/quality/ingest.py）と日報の内部品質の欄（ADR 0019）。

DB を使うテストは、空の Postgres に orchestrator/postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）:
  TEST_DATABASE_URL=postgresql://factory@localhost:5432/factory_test python -m pytest health/quality/tests
"""
import io
import json
import os
import sys
import zipfile

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import ingest  # noqa: E402

DEFS = ingest.load_definitions(os.path.join(HERE, "..", "..", "..", "definitions", "quality.yaml"))
URL = os.environ.get("TEST_DATABASE_URL")


def snap(sha, cf=10, **metrics):
    base = {"layer_violations": 0, "complex_function_ratio": 0.02, "tests_per_kloc": 40.0, "mutation_score": None}
    return {"sha": sha, "score": 90, "metrics": {**base, **metrics}, "counts": {"complex_functions": cf}, "hotspots": []}


def f(name, status="modified", add=10, dele=0):
    return {"filename": name, "status": status, "additions": add, "deletions": dele}


def test_regressions_follow_direction_and_skip_same_sha():
    prev = snap("a")
    assert ingest.regressions(None, snap("b"), DEFS) == []
    assert ingest.regressions(prev, snap("a", complex_function_ratio=0.5), DEFS) == []    # 同じ版なら比べない
    got = ingest.regressions(prev, snap("b", complex_function_ratio=0.03, tests_per_kloc=35.0), DEFS)
    assert {r["metric"] for r in got} == {"complex_function_ratio", "tests_per_kloc"}
    assert all(r["from_sha"] == "a" and r["to_sha"] == "b" for r in got)
    assert ingest.regressions(prev, snap("b", tests_per_kloc=45.0, complex_function_ratio=0.01), DEFS) == []
    got = ingest.regressions(prev, snap("b", cf=11), DEFS)
    assert got == [{"metric": "complex_functions", "before": 10, "after": 11, "from_sha": "a", "to_sha": "b", "floor": True}]


def test_payload_keeps_known_fields_only():
    s = {**snap("a"), "hotspots": [{"file": "x.rs", "changes": 2, "max_ccn": 9, "score": 18, "extra": 1}] * 30, "prompt": "ignore me"}
    p = ingest.to_payload("o/r", "2026-10-31", s, {"id": 1, "created_at": "t", "head_sha": "b"})
    assert "prompt" not in p and len(p["hotspots"]) == 20 and "extra" not in p["hotspots"][0]
    assert (p["repo"], p["day"], p["run_id"], p["sha_mismatch"]) == ("o/r", "2026-10-31", 1, True)


def test_difficulty_levels():
    easy = ingest.difficulty([f("apps/admin-web/src/a.tsx"), f("apps/admin-web/tests/a.test.tsx", "added"), f("CHANGELOG.md")], DEFS)
    assert (easy["points"], easy["level"], easy["areas"]) == (0, "easy", ["apps/admin-web"])
    hard = ingest.difficulty([f("apps/backend/crates/domain/src/x.rs", "added"), f("apps/backend/crates/infra/src/y.rs", "added"),
                              f("apps/backend/crates/infra/migrations/1.sql", "added"), f("specs/contracts/api/a/openapi.yaml"),
                              f("apps/admin-web/src/api/schema.ts", add=600)], DEFS)
    # 領域 3（backend・specs・admin-web）→ 2、契約 2、スキーマ 2、新しいファイル 3 つ → 1、650 行 → 1
    assert (hard["points"], hard["level"], hard["contract_diff"], hard["schema_diff"], hard["new_files"]) == (8, "hard", True, True, 3)


def fake_gh(snapshot: dict, files: list[dict]):
    def gh(method, path):
        if path.startswith("repos/o/r/actions/workflows/quality.yml/runs"):
            return {"workflow_runs": [{"id": 7, "created_at": "t", "head_sha": snapshot["sha"]}]}
        if path == "repos/o/r/actions/runs/7/artifacts":
            return {"artifacts": [{"name": "quality-snapshot", "expired": False, "archive_download_url": "https://example.invalid/zip"}]}
        if path.startswith("repos/o/r/pulls/2/files"):
            return files
        raise AssertionError(path)
    return gh


def zipped(obj) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("quality-snapshot.json", json.dumps(obj))
    return b.getvalue()


@pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")
def test_ingest_and_daily_report(monkeypatch):
    import psycopg
    from psycopg.types.json import Jsonb
    sys.path.insert(0, os.path.join(HERE, "..", "..", "daily"))
    os.environ.setdefault("GH_TOKEN", "x")
    import status_report

    conn = psycopg.connect(URL, autocommit=True)
    cur = conn.cursor()
    cur.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    cur.execute("insert into events(name, payload, dedupe_key) values ('quality.snapshot', %s, 'quality.snapshot:o/r:2026-10-30')",
                (Jsonb({"repo": "o/r", "day": "2026-10-30", **snap("a")}),))
    cur.execute("insert into tickets(source, source_ref, repo, created_at) values ('github','o/r#1','o/r', now() - interval '10 hours')")
    for cost in (1.5, 2.0):
        cur.execute("insert into jobs(correlation_id, repo, issue_number, state, result) values ('github:o/r#1','o/r',1,'succeeded',%s)",
                    (Jsonb({"cost_usd": cost}),))
    cur.execute("insert into events(name, correlation_id, payload) values ('pr.merged','github:o/r#1', jsonb_build_object('pr_url','https://github.com/o/r/pull/2','merged_at', now()::text))")
    cur.execute("insert into events(name, payload) values ('sendback.filed', %s)", (Jsonb({"origin_pr": "https://github.com/o/r/pull/2"}),))

    monkeypatch.setattr(ingest, "_download", lambda url, token: zipped(snap("b", cf=12, complex_function_ratio=0.05)))
    gh = fake_gh(snap("b"), [f("apps/backend/crates/infra/migrations/1.sql", "added"), f("apps/backend/crates/infra/src/a.rs")])
    assert ingest.ingest_snapshots(conn, ["o/r"], "2026-10-31", gh, "x", DEFS) == 1
    assert ingest.ingest_snapshots(conn, ["o/r"], "2026-10-31", gh, "x", DEFS) == 0     # 同じ日は取り直さない
    assert ingest.ingest_difficulty(conn, gh, DEFS) == 1
    assert ingest.ingest_difficulty(conn, gh, DEFS) == 0
    cur.execute("select payload->>'metric' from events where name='quality.regressed' order by 1")
    assert [r[0] for r in cur.fetchall()] == ["complex_function_ratio", "complex_functions"]

    cc = status_report.change_cost(conn)
    assert len(cc) == 1 and cc[0]["level"] == "medium" and cc[0]["n"] == 1            # スキーマ 2 点
    assert round(cc[0]["lead_time_hours"]) == 10 and cc[0]["cost_usd"] == 3.5 and cc[0]["jobs"] == 2 and cc[0]["sendbacks"] == 1
    qt = status_report.quality_trend(conn)
    assert [h["day"] for h in qt["o/r"]] == ["2026-10-30", "2026-10-31"]
    text = "\n".join(status_report.fmt_quality(qt) + status_report.fmt_change_cost(cc))
    assert "スコア" in text and "| 中 |" in text
    conn.close()
