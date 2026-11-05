"""Mac ランナー（ADR 0028 の決定 3、ADR 0033）: 引数の形、範囲、決まった道具だけを argv で呼ぶこと。

xcrun・idb・flutter は呼ばず、runner.run を差し替えて呼ばれた argv を確かめる（Linux でも動く）。
窓口から来た呼び出しは runner.execute でこなすので、ここでもそれを直に呼ぶ。窓口との話（ペアリング・署名・取りに行く形）は
dispatcher/tests/test_runner_gate.py が本物の TLS で確かめる。
"""
import json
import os
import sys
import types

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import runner  # noqa: E402

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
    logs = []
    yield types.SimpleNamespace(r=runner.Runner(runner.config()), logs=logs), logs


def call(s, name, args=None):
    return runner.execute(s.r, name, args or {}, s.logs.append)


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


def test_tool_list_is_fixed():
    """道具の一覧は PROTOCOL.md と SCOPE.md の約束。足すときはこのテストと文書を同じ変更で直し、オーナーの承認を通す。"""
    assert sorted(runner.TOOL_NAMES) == sorted([
        "sim_devices", "sim_boot", "sim_shutdown", "sim_erase", "app_build", "build_status", "app_install", "app_launch",
        "app_terminate", "screenshot", "open_url", "push", "logs", "ui_describe", "ui_tap", "ui_swipe", "ui_text", "ui_key"])


def test_hello_declares_scope(tmp_path, monkeypatch, calls):
    monkeypatch.setenv("MAC_RUNNER_HOME", str(tmp_path))
    info = runner.hello_info(runner.config())
    assert "しない: 署名" in info["scope"] and info["version"] == runner.VERSION and info["devices"] == "1"


def test_gate_url_must_be_https():
    with pytest.raises(SystemExit):
        runner.Gate("http://127.0.0.1:8794")
