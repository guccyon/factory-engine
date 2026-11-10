"""工場の中での一次調査の道具（investigate_tools.py と runner_relay の investigate、ADR 0048 の順番 3）。DB なしで動く"""
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import investigate_db as idb  # noqa: E402
import investigate_tools as it  # noqa: E402
import runner_relay as rr  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
DEFS = yaml.safe_load((ROOT / "definitions" / "investigate.yaml").read_text(encoding="utf-8"))


def text(res):
    return res["content"][0]["text"]


def test_sql_passes_through_and_reports_errors():
    ok = it.sql({"sql": "select 1"}, query=lambda s: {"columns": ["a"], "rows": [[1]], "truncated": False})
    assert not ok["isError"] and json.loads(text(ok))["rows"] == [[1]]

    def rejected(s):
        raise idb.Rejected("1 回に流せるのは 1 文だけ")
    assert "流さなかった" in text(it.sql({"sql": "x"}, query=rejected))

    def unavailable(s):
        raise idb.Unavailable("INVESTIGATE_DATABASE_URL がない")
    r = it.sql({"sql": "select 1"}, query=unavailable)
    assert r["isError"] and "DB は使えない" in text(r)

    def boom(s):
        raise RuntimeError("permission denied for table jobs")
    assert "SQL の失敗" in text(it.sql({"sql": "select 1"}, query=boom))
    assert it.sql({"sql": 1})["isError"]


def test_sql_rejects_before_db():
    r = it.sql({"sql": "select 1; reset role"})   # 既定の query は INVESTIGATE_DATABASE_URL より前に形を見る
    assert r["isError"] and "流さなかった" in text(r)


def test_job_log_tail_and_redaction():
    log = "\n".join([f"line {i}" for i in range(500)] + ["token ghp_" + "a" * 36, "url postgresql://factory:secret@postgres:5432/factory"])
    r = it.job_log({"job_id": 7, "tail": 3}, lambda j: f"jobs/{j}/worker.log", lambda k: log.encode())
    t = text(r)
    assert "line 499" in t and "line 498" not in t
    assert "ghp_" not in t and "secret" not in t and t.count("［伏せた］") == 2
    assert it.job_log({"job_id": 7}, lambda j: None, None)["isError"]
    assert it.job_log({"job_id": 0}, None, None)["isError"]
    assert it.job_log({"job_id": 7, "tail": 999}, None, None)["isError"]


@pytest.mark.parametrize("path", ["pulls/123", "pulls/123/reviews", "issues/45/comments", "issues?state=open&per_page=5",
                                  "actions/runs?branch=main&per_page=5", "actions/runs/99/jobs", "commits/abcdef1/check-runs"])
def test_github_allowed_paths(path):
    seen = []
    r = it.github({"path": path}, "owner/product", lambda p: seen.append(p) or {"ok": True})
    assert not r["isError"] and seen == [f"repos/owner/product/{path}"]


@pytest.mark.parametrize("path", ["", "contents/.env", "pulls/1/merge", "../other/repo/pulls", "collaborators", "actions/secrets",
                                  "hooks", "pulls/1/../../keys"])
def test_github_denied_paths(path):
    r = it.github({"path": path}, "owner/product", lambda p: pytest.fail("呼ばない"))
    assert r["isError"]


def test_github_needs_repo_from_grant():
    assert it.github({"path": "pulls/1"}, None, lambda p: pytest.fail("呼ばない"))["isError"]


def test_long_text_is_cut():
    r = it.sql({"sql": "select 1"}, query=lambda s: {"columns": ["a"], "rows": [["x" * 30000]], "truncated": False})
    assert len(text(r)) < it.MAX_TEXT + 200 and "切った" in text(r)


def test_relay_gives_investigate_only_its_tools():
    assert set(rr.KIND_TOOLS["investigate"]) == {"inv_sql", "inv_job_log", "inv_github"}
    assert not set(rr.KIND_TOOLS["investigate"]) & set(rr.KIND_TOOLS["qa"])
    for v in DEFS["sources"]["db"]["views"]:
        assert v in rr.INVESTIGATE_TOOLS["inv_sql"]["description"]
    assert rr.INVESTIGATE_TOOLS["inv_job_log"]["inputSchema"]["properties"]["tail"]["maximum"] == DEFS["sources"]["job_log"]["max_tail_lines"] == it.MAX_TAIL


def test_relay_routes_investigate_tools_locally():
    calls = []

    def local(tool, args, grant):
        calls.append((tool, grant["repo"]))
        return it.handle(tool, args, grant, blob_key_of=lambda j: None, store_get=None, gh_get=lambda p: {"p": p},
                         query=lambda s: {"columns": [], "rows": [], "truncated": False})
    relay = rr.Relay(call=lambda *a: pytest.fail("Mac には送らない"), record=lambda *a: None, local=local)
    tok = relay.grant(5, "investigate", 60, repo="owner/product")
    code, body = relay.handle(f"Bearer {tok}", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert {t["name"] for t in body["result"]["tools"]} == {"inv_sql", "inv_job_log", "inv_github"}
    code, body = relay.handle(f"Bearer {tok}", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                "params": {"name": "inv_github", "arguments": {"path": "pulls/1"}}})
    assert not body["result"]["isError"] and calls == [("inv_github", "owner/product")]
    code, body = relay.handle(f"Bearer {tok}", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                "params": {"name": "env_restart", "arguments": {}}})
    assert body["result"]["isError"]   # QA の道具は渡していない
