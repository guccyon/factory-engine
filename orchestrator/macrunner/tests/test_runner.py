"""Mac ランナー（ADR 0028 の決定 3）: MCP の受け答え、認証、引数の形、決まった道具だけを argv で呼ぶこと。

xcrun・idb・flutter は呼ばず、runner.run を差し替えて呼ばれた argv を確かめる（Linux でも動く）。
工場の側の factoryctl runner check（dispatcher/macrunner_client.py）も、本物の HTTP で同じランナーに当てる。
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "dispatcher"))
import runner  # noqa: E402
import macrunner_client  # noqa: E402

TOKEN = "t" * 64
UDID = "11111111-2222-3333-4444-555555555555"
DEVICES = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-1": [{"udid": UDID, "name": "iPhone 17", "state": "Booted"}],
                       "com.apple.CoreSimulator.SimRuntime.watchOS-12-0": [{"udid": "x", "name": "Watch", "state": "Shutdown"}]}}


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_run(argv, timeout=120, input_bytes=None, cwd=None, env_extra=None):
        seen.append(argv)
        if argv[:4] == ["xcrun", "simctl", "list", "devices"]:
            return json.dumps(DEVICES).encode()
        if argv[:3] == ["xcrun", "simctl", "io"]:
            with open(argv[-1], "wb") as f:
                f.write(b"\x89PNG fake")
        return b"ok"
    monkeypatch.setattr(runner, "run", fake_run)
    return seen


@pytest.fixture
def server(tmp_path, monkeypatch, calls):
    monkeypatch.setenv("MAC_RUNNER_HOME", str(tmp_path / "home"))
    cfg = runner.config()
    logs = []
    httpd = runner.ThreadingHTTPServer(("127.0.0.1", 0), runner.make_handler(runner.Runner(cfg), TOKEN, logs.append))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", logs
    httpd.shutdown()


def post(url, body, token=TOKEN):
    req = urllib.request.Request(url + "/mcp", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, (json.loads(r.read()) if r.status == 200 else None)


def call(url, name, args=None):
    _, res = post(url, {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": args or {}}})
    return res["result"]


def test_health_needs_no_token_but_mcp_does(server):
    url, _ = server
    with urllib.request.urlopen(url + "/health") as r:
        assert json.loads(r.read())["ok"] is True
    with pytest.raises(urllib.error.HTTPError) as e:
        post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token="wrong")
    assert e.value.code == 401


def test_initialize_list_and_notification(server):
    url, _ = server
    _, init = post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["serverInfo"]["name"] == "factory-macrunner"
    assert init["result"]["capabilities"]["tools"]
    status, _ = post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202
    _, lst = post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in lst["result"]["tools"]}
    assert {"sim_devices", "app_build", "screenshot", "ui_tap", "logs"} <= names
    assert not any("shell" in n or "exec" in n for n in names)   # 任意のコマンドの道具はない
    _, bad = post(url, {"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    assert bad["error"]["code"] == -32601


def test_devices_only_ios(server):
    url, _ = server
    res = call(url, "sim_devices")
    assert not res["isError"]
    devs = json.loads(res["content"][0]["text"])
    assert devs == [{"udid": UDID, "name": "iPhone 17", "state": "Booted", "runtime": "iOS-26-1"}]


def test_screenshot_returns_png(server, calls):
    url, _ = server
    res = call(url, "screenshot", {"udid": UDID})
    assert res["content"][0]["type"] == "image" and res["content"][0]["mimeType"] == "image/png"
    assert any(a[:4] == ["xcrun", "simctl", "io", UDID] for a in calls)


@pytest.mark.parametrize("name,args,msg", [
    ("screenshot", {"udid": "; rm -rf /"}, "udid の形"),
    ("app_launch", {"udid": UDID, "bundle_id": "-x"}, "bundle_id の形"),
    ("open_url", {"udid": UDID, "url": "file:///etc/passwd"}, ""),
    ("ui_tap", {"udid": UDID, "x": 10, "y": True}, "y は数"),
    ("ui_tap", {"udid": UDID, "x": 10, "y": 10, "cmd": "id"}, "知らない引数"),
    ("app_build", {"app": "evil/repo:apps/x"}, "ビルドを許していない"),
    ("app_build", {"app": "guccyon/rally:apps/student-app", "ref": "--upload-pack=x"}, "ref の形"),
    ("app_build", {"app": "guccyon/rally:apps/student-app", "dart_defines": {"a;b": "1"}}, "dart_defines"),
    ("shell", {"cmd": "id"}, "という道具はない"),
])
def test_rejects_bad_arguments(server, calls, name, args, msg):
    url, logs = server
    before = len(calls)
    res = call(url, name, args)
    assert res["isError"] is True
    assert msg in res["content"][0]["text"]
    assert all(a[:4] == ["xcrun", "simctl", "list", "devices"] for a in calls[before:])   # 一覧を見る以外は何も動かさない
    assert logs[-1]["tool"] == name and logs[-1]["error"] is True


def test_not_booted_is_refused(server, monkeypatch, calls):
    url, _ = server
    monkeypatch.setitem(DEVICES["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-26-1"][0], "state", "Shutdown")
    res = call(url, "ui_tap", {"udid": UDID, "x": 1, "y": 2})
    assert res["isError"] and "起動していない" in res["content"][0]["text"]


def test_text_is_redacted_in_log_and_passed_as_argv(server, calls):
    url, logs = server
    res = call(url, "ui_text", {"udid": UDID, "text": "secret pass $(id)"})
    assert not res["isError"]
    assert calls[-1] == ["idb", "ui", "text", "--udid", UDID, "secret pass $(id)"]   # シェルを通さないので展開されない
    assert logs[-1]["args"]["text"] == "<17 文字>"


def test_build_refuses_unmerged_commit(tmp_path, monkeypatch):
    """マージ済みでないコミットはビルドしない（製品のコードをホストで動かすので）。"""
    monkeypatch.setenv("MAC_RUNNER_HOME", str(tmp_path))
    cfg = runner.config()
    os.makedirs(tmp_path / "repos" / "guccyon__rally" / ".git")
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        if "symbolic-ref" in argv:
            return b"origin/main\n"
        if "rev-parse" in argv:
            return b"abc123\n"
        if "merge-base" in argv:
            raise runner.ToolError("not ancestor")
        return b""
    monkeypatch.setattr(runner, "run", fake_run)
    b = runner.Builds(cfg)
    with pytest.raises(runner.ToolError, match="マージ済みのコミットだけ"):
        b._build("b-000000000000", cfg["apps"]["guccyon/rally:apps/student-app"], "feature/x", {})
    assert not any("flutter" in a[0] or "worktree" in a for a in seen)


def test_factoryctl_check_against_runner(server):
    url, _ = server
    lines, code = macrunner_client.check({"MAC_RUNNER_URL": url, "MAC_RUNNER_TOKEN": TOKEN})
    assert code == 0, lines
    assert any(line.startswith("端末: 1 台（起動中 1）") for line in lines)
    lines, code = macrunner_client.check({"MAC_RUNNER_URL": url, "MAC_RUNNER_TOKEN": "x" * 64})
    assert code == 1 and "401" in lines[-1]


def test_mcp_config_only_with_token():
    assert macrunner_client.mcp_config({}) is None
    cfg = macrunner_client.mcp_config({"MAC_RUNNER_TOKEN": TOKEN})
    assert cfg["mcpServers"]["macrunner"]["url"] == "http://host.docker.internal:8793/mcp"


def test_idb_retries_until_accessibility_answers(tmp_path, monkeypatch):
    """起動の直後は画面の読み取りが答えないことがある（実機で 10 秒後に通った）。その失敗だけ読み直す。"""
    monkeypatch.setenv("MAC_RUNNER_HOME", str(tmp_path))
    monkeypatch.setattr(runner, "IDB_RETRY_SEC", 0)
    n = []

    def fake_run(argv, **kw):
        n.append(argv)
        if len(n) < 3:
            raise runner.ToolError("accessibility service did not answer, retry the read")
        return b"[]"
    monkeypatch.setattr(runner, "run", fake_run)
    assert runner.Runner(runner.config()).idb("ui", "describe-all") == b"[]"
    assert len(n) == 3

    def other(argv, **kw):
        n.append(argv)
        raise runner.ToolError("no such device")
    monkeypatch.setattr(runner, "run", other)
    n.clear()
    with pytest.raises(runner.ToolError):
        runner.Runner(runner.config()).idb("ui", "tap")
    assert len(n) == 1   # ほかの失敗は読み直さない


def test_shutdown_cleans_companion(server, calls):
    url, _ = server
    assert not call(url, "sim_shutdown", {"udid": UDID})["isError"]
    assert calls[-2] == ["pkill", "-f", f"idb_companion.*{UDID}"]
    assert calls[-1] == ["idb", "disconnect", UDID]   # idb の記録からも外す（起動し直した端末で繋がらなくなるので）


@pytest.mark.parametrize("name,args,msg", [
    ("app_launch", {"udid": UDID, "bundle_id": "com.apple.Preferences"}, "操作を許していない"),
    ("push", {"udid": UDID, "bundle_id": "com.apple.mobilesafari", "payload": {"aps": {}}}, "操作を許していない"),
    ("open_url", {"udid": UDID, "url": "https://example.com/"}, "http(s) だけ"),
    ("open_url", {"udid": UDID, "url": "tel://123"}, "は開かない"),
])
def test_scope_limits_apps_and_urls(server, calls, name, args, msg):
    url, _ = server
    res = call(url, name, args)
    assert res["isError"] and msg in res["content"][0]["text"]


def test_scope_allows_allowed_app_and_local_url(server, calls):
    url, _ = server
    assert not call(url, "app_launch", {"udid": UDID, "bundle_id": "jp.rally.rallyStudentApp"})["isError"]
    assert not call(url, "open_url", {"udid": UDID, "url": "http://127.0.0.1:8081/login"})["isError"]


def test_initialize_declares_scope(server):
    url, _ = server
    _, init = post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert "しない: 署名" in init["result"]["instructions"]


def test_checkin_and_self_uninstall(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_RUNNER_HOME", str(tmp_path))
    monkeypatch.setattr(runner, "run", lambda argv, **kw: json.dumps(DEVICES).encode() if "list" in argv else b"")
    cfg = runner.config()
    rid = runner.runner_id(cfg)
    assert rid == runner.runner_id(cfg) and len(rid) == 32   # 置き場に残して同じ id を使う
    asked = []

    class Resp:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return self.body

    def opener(u, timeout=10):
        asked.append(u)
        return Resp(b'{"uninstall": true}')
    assert runner.checkin(cfg, rid, opener=opener) is True
    assert "/api/runners/checkin?" in asked[0] and f"id={rid}" in asked[0] and "scope=" in asked[0]

    def down(u, timeout=10):
        raise OSError("refused")
    assert runner.checkin(cfg, rid, opener=down) is False   # 工場が止まっていてもランナーは動き続ける


def test_tool_list_is_fixed():
    """道具の一覧は PROTOCOL.md と SCOPE.md の約束。足すときはこのテストと文書を同じ変更で直し、オーナーの承認を通す。"""
    assert sorted(runner.TOOL_NAMES) == sorted([
        "sim_devices", "sim_boot", "sim_shutdown", "sim_erase", "app_build", "build_status", "app_install", "app_launch",
        "app_terminate", "screenshot", "open_url", "push", "logs", "ui_describe", "ui_tap", "ui_swipe", "ui_text", "ui_key"])
