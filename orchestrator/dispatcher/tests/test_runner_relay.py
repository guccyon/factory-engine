"""ジョブごとの Mac ランナーへの中継（runner_relay.py、ADR 0028 の one_orchestrator）: 合言葉・通す道具・期限・記録・MCP の往復"""
import importlib.util
import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import runner_relay as rr  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]


def runner_tools():
    spec = importlib.util.spec_from_file_location("macrunner_runner", ROOT / "orchestrator" / "macrunner" / "runner.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return {t["name"]: t for t in m.TOOLS}


def test_tools_are_copies_of_the_runner_and_match_definitions():
    rt = runner_tools()
    for name, t in rr.TOOLS.items():
        assert rt[name] == t, name   # 形はランナーの一覧の写し
    assert "sim_erase" not in rr.TOOLS and "sim_shutdown" not in rr.TOOLS
    qa = yaml.safe_load((ROOT / "definitions" / "qa.yaml").read_text(encoding="utf-8"))
    assert list(rr.KIND_TOOLS["qa"]) == qa["runner_tools"]


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def relay(result=None, fail=None):
    calls, recs = [], []

    def call(tool, args, timeout):
        calls.append((tool, args, timeout))
        if fail:
            raise RuntimeError(fail)
        return result or {"content": [{"type": "text", "text": "[]"}], "isError": False, "runner_id": "5c21c124abcd"}
    clk = Clock()
    return rr.Relay(call, lambda j, p: recs.append((j, p)), clock=clk), calls, recs, clk


def rpc(r, tok, method, params=None, mid=1):
    return r.handle(f"Bearer {tok}", {"jsonrpc": "2.0", "id": mid, "method": method, **({"params": params} if params else {})})


def test_auth_scope_and_expiry():
    r, calls, recs, clk = relay()
    assert r.handle("", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0] == 401
    tok = r.grant(7, "qa", 60)
    assert rpc(r, "wrong", "tools/list")[0] == 401
    assert rpc(r, r.grant(8, "implement", 60), "tools/list")[1]["result"]["tools"] == []   # 中継を渡さない種類は道具 0
    names = [t["name"] for t in rpc(r, tok, "tools/list")[1]["result"]["tools"]]
    assert names == list(rr.KIND_TOOLS["qa"])
    code, body = rpc(r, tok, "tools/call", {"name": "sim_erase", "arguments": {"udid": "x"}})
    assert code == 200 and body["result"]["isError"] and not calls   # 一覧の外はランナーに届けない
    clk.t += 61
    assert rpc(r, tok, "tools/list")[0] == 401   # 期限切れ
    tok2 = r.grant(9, "qa", 60)
    assert r.revoke(9) == 1 and rpc(r, tok2, "tools/list")[0] == 401


def test_call_records_without_arguments():
    r, calls, recs, _ = relay()
    tok = r.grant(7, "qa", 60)
    code, body = rpc(r, tok, "tools/call", {"name": "ui_text", "arguments": {"udid": "u", "text": "秘密の文字"}})
    assert code == 200 and body["result"] == {"content": [{"type": "text", "text": "[]"}], "isError": False}
    assert calls == [("ui_text", {"udid": "u", "text": "秘密の文字"}, 120)]
    job, p = recs[0]
    assert job == 7 and p["tool"] == "ui_text" and p["ok"] and p["runner"] == "5c21c124" and "秘密" not in json.dumps(p, ensure_ascii=False)
    r2, _, recs2, _ = relay(fail="no runner")
    body = rpc(r2, r2.grant(1, "qa", 60), "tools/call", {"name": "sim_devices"})[1]
    assert body["result"]["isError"] and "no runner" in body["result"]["content"][0]["text"] and recs2[0][1]["ok"] is False


def test_initialize_notifications_and_unknown():
    r, *_ = relay()
    tok = r.grant(1, "qa", 60)
    init = rpc(r, tok, "initialize", {"protocolVersion": "2025-03-26"})[1]["result"]
    assert init["protocolVersion"] == "2025-03-26" and "tools" in init["capabilities"]
    assert r.handle(f"Bearer {tok}", {"jsonrpc": "2.0", "method": "notifications/initialized"}) == (202, None)
    assert rpc(r, tok, "resources/list")[1]["error"]["code"] == -32601
    assert r.handle(f"Bearer {tok}", {"id": 1})[0] == 400


def test_http_roundtrip():
    r, *_ = relay()
    srv = rr.serve(r, 0, "127.0.0.1")
    try:
        port = srv.server_address[1]
        tok = r.grant(1, "qa", 60)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", method="POST",
                                     data=json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "sim_devices"}}).encode(),
                                     headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200 and json.loads(resp.read())["id"] == 3
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/mcp", method="POST", data=b"{}"), timeout=5)
        assert e.value.code == 401
    finally:
        srv.shutdown()
