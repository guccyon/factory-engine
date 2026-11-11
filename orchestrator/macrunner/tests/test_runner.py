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
        "app_terminate", "screenshot", "open_url", "push", "logs", "ui_describe", "ui_tap", "ui_swipe", "ui_text", "ui_key",
        "preview_distribute"])


def test_hello_declares_scope(tmp_path, monkeypatch, calls):
    monkeypatch.setenv("MAC_RUNNER_HOME", str(tmp_path))
    info = runner.hello_info(runner.config())
    assert "しない: 鍵を工場に渡すこと" in info["scope"] and info["version"] == runner.VERSION and info["devices"] == "1"


def test_gate_url_must_be_https():
    with pytest.raises(SystemExit):
        runner.Gate("http://127.0.0.1:8794")


def test_child_env_ignores_owner_git_config(monkeypatch):
    # launchd のランナーは保護フォルダにある ~/.gitconfig を読めずに落ちる（2026-11-06）。持ち主の設定を読ませない
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/Users/x/Documents/dotfiles/.gitconfig")
    e = runner.child_env()
    assert e["GIT_CONFIG_GLOBAL"] == os.devnull and e["GIT_CONFIG_NOSYSTEM"] == "1" and e["GIT_TERMINAL_PROMPT"] == "0"
    assert e["PATH"] == os.environ["PATH"]
    out = runner.run([sys.executable, "-c", "import os; print(os.environ['GIT_CONFIG_GLOBAL'])"])
    assert out.decode().strip() == os.devnull


# ---- 審査前の配布（ADR 0043 の O8・ADR 0047） ----

APP = "guccyon/rally:apps/student-app"
FB_APP = "1:123456789:android:0123abcd"


@pytest.fixture
def dist(tmp_path, monkeypatch):
    """配布先 firebase_android を開いた設定。run は差し替え、ビルドの成果物は呼ばれた argv に合わせて作る。"""
    monkeypatch.setenv("MAC_RUNNER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MAC_RUNNER_FIREBASE_APP_ID", FB_APP)
    ks = tmp_path / "upload.jks"
    ks.write_bytes(b"keystore")
    monkeypatch.setenv("MAC_RUNNER_ANDROID_KEYSTORE", str(ks))
    monkeypatch.setenv("MAC_RUNNER_APKSIGNER", "/sdk/apksigner")
    monkeypatch.setenv("MAC_RUNNER_JAVA_HOME", "/jdk")
    os.makedirs(tmp_path / "home" / "repos" / "guccyon__rally" / ".git")
    seen = types.SimpleNamespace(argv=[], env={}, answer="button returned:配布する, gave up:false", merged=True)

    def fake_run(argv, timeout=120, input_bytes=None, cwd=None, env_extra=None):
        seen.argv.append(argv)
        if env_extra:
            seen.env[argv[0]] = env_extra
        if "symbolic-ref" in argv:
            return b"origin/main\n"
        if "rev-parse" in argv:
            return b"abc123def4567890\n"
        if "merge-base" in argv and not seen.merged:
            raise runner.ToolError("not ancestor")
        if argv[0] == "osascript":
            if seen.answer is None:
                raise runner.ToolError("osascript が失敗した（1）: execution error: User canceled. (-128)")
            return seen.answer.encode()
        if "worktree" in argv and "add" in argv:
            os.makedirs(os.path.join(argv[argv.index("--detach") + 1], "apps", "student-app"), exist_ok=True)
        if argv[:3] == ["flutter", "build", "apk"]:
            out = os.path.join(cwd, "build", "app", "outputs", "flutter-apk")
            os.makedirs(out, exist_ok=True)
            open(os.path.join(out, "app-release.apk"), "wb").write(b"apk")
        if argv[0] == "/sdk/apksigner":
            open(argv[argv.index("--out") + 1], "wb").write(b"signed apk")
        return b"ok"
    monkeypatch.setattr(runner, "run", fake_run)
    monkeypatch.setattr(runner, "keychain_password", lambda service: "s3cret-pass")
    cfg = runner.config()
    return types.SimpleNamespace(cfg=cfg, seen=seen, b=runner.Builds(cfg))


def start_item(d, notes="直したところ"):
    bid = "b-000000000001"
    d.b.items[bid] = {"build_id": bid, "app": APP, "kind": "distribute", "channel": "firebase_android", "release_notes": notes}
    return bid, d.cfg["apps"][APP]


def test_distribute_closed_until_mac_is_set_up(server):
    """配布先は、人が Mac で Firebase のアプリと署名の鍵を入れるまで閉じている。"""
    s, _ = server
    res = call(s, "preview_distribute", {"app": APP, "channel": "firebase_android"})
    assert res["isError"] and "開いていない" in res["content"][0]["text"]
    res = call(s, "preview_distribute", {"app": APP, "channel": "testflight_ios"})
    assert res["isError"] and "testflight_ios という配布先はない" in res["content"][0]["text"]


def test_distribute_signs_with_keychain_and_uploads(dist):
    """人が押したら、作業場で android/ を作り、release でビルドし、キーチェーンのパスワードで署名して Firebase に上げる。"""
    bid, app = start_item(dist)
    out = dist.b._distribute(bid, app, "main", {"RALLY_API_URL": "https://staging.example"})
    names = [a[0] if a[0] != "flutter" else " ".join(a[:3]) for a in dist.seen.argv]
    assert names.index("osascript") < names.index("flutter create --platforms=android")   # 人の合図より前に作業場を作らない
    build = next(a for a in dist.seen.argv if a[:3] == ["flutter", "build", "apk"])
    assert "--release" in build and "--dart-define=RALLY_API_URL=https://staging.example" in build
    sign = next(a for a in dist.seen.argv if a[0] == "/sdk/apksigner")
    assert sign[sign.index("--ks-pass") + 1] == "env:FACTORY_KS_PASS"
    assert dist.seen.env["/sdk/apksigner"]["FACTORY_KS_PASS"] == "s3cret-pass"
    assert dist.seen.env["/sdk/apksigner"]["JAVA_HOME"] == "/jdk" and dist.seen.env["/sdk/apksigner"]["PATH"].startswith("/jdk/bin")
    assert not any("s3cret-pass" in x for a in dist.seen.argv for x in a)   # パスワードは argv に出さない
    up = next(a for a in dist.seen.argv if a[0] == "firebase")
    assert up[1] == "appdistribution:distribute" and up[up.index("--app") + 1] == FB_APP
    assert up[up.index("--groups") + 1] == "testers" and up[up.index("--release-notes") + 1] == "直したところ"
    assert up[2].endswith("app-release-signed.apk")
    assert out["sha"] == "abc123def4567890" and out["channel"] == "firebase_android" and len(out["sha256"]) == 64
    assert "s3cret-pass" not in json.dumps(out)


@pytest.mark.parametrize("answer,msg", [
    (None, "やめた"),
    ("button returned:, gave up:true", "押されなかった"),
])
def test_distribute_needs_a_human_at_the_mac(dist, answer, msg):
    """Mac の前で押されなければ（やめた・時間切れ）、ビルドも署名も上げることもしない。"""
    dist.seen.answer = answer
    bid, app = start_item(dist)
    with pytest.raises(runner.ToolError, match=msg):
        dist.b._distribute(bid, app, "main", {})
    assert not any(a[0] in ("flutter", "/sdk/apksigner", "firebase") or "worktree" in a for a in dist.seen.argv)
    assert dist.b.items[bid]["stage"] == "attend"


def test_distribute_refuses_unmerged_commit_before_asking(dist):
    dist.seen.merged = False
    bid, app = start_item(dist)
    with pytest.raises(runner.ToolError, match="マージ済みのコミットだけ"):
        dist.b._distribute(bid, app, "feature/x", {})
    assert not any(a[0] == "osascript" for a in dist.seen.argv)


def test_dialog_text_is_passed_as_argv():
    """確かめの窓の文言は argv で渡し、AppleScript に埋めない（文言に引用符があってもスクリプトにならない）。"""
    seen = []
    orig = runner.run
    runner.run = lambda argv, **kw: seen.append(argv) or "button returned:配布する, gave up:false".encode()
    try:
        runner.attend('x" & do shell script "id', 60)
    finally:
        runner.run = orig
    assert seen[0][-2:] == ['x" & do shell script "id', "60"]
    assert all('do shell script' not in line for line in runner.ATTEND_SCRIPT)


def test_release_notes_redacted_in_log():
    assert runner.redact("preview_distribute", {"release_notes": "abc", "dart_defines": {"A": "1"}}) == {"release_notes": "<3 文字>", "dart_defines": ["A"]}


def test_java_found_or_said(monkeypatch):
    """launchd には JAVA_HOME がない。Android Studio の JDK もなければ、何を入れればよいかを言う。"""
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(runner, "ANDROID_STUDIO_JDK", "/nonexistent")
    with pytest.raises(runner.ToolError, match="MAC_RUNNER_JAVA_HOME"):
        runner.java_env({"java_home": ""})
    assert runner.java_env({"java_home": "/jdk"})["JAVA_HOME"] == "/jdk"


def test_destination_from_factory_overrides_mac(dist):
    """宛先は工場が渡せる（操業コンソールの配布先）。渡したものが確かめの窓に出て、その宛先に上げる。"""
    bid, app = start_item(dist)
    dist.b.items[bid].update({"firebase_app_id": "1:999:android:beef", "groups": "qa,owners"})
    out = dist.b._distribute(bid, app, "main", {})
    dialog = next(a for a in dist.seen.argv if a[0] == "osascript")
    assert "1:999:android:beef" in dialog[-2] and "qa,owners" in dialog[-2]
    up = next(a for a in dist.seen.argv if a[0] == "firebase")
    assert up[up.index("--app") + 1] == "1:999:android:beef" and up[up.index("--groups") + 1] == "qa,owners"
    assert out["firebase_app_id"] == "1:999:android:beef"


def test_bad_destination_from_factory_is_refused(server, monkeypatch, tmp_path):
    s, _ = server
    ks = tmp_path / "k.jks"
    ks.write_bytes(b"k")
    s.r.cfg["android_keystore"] = str(ks)
    res = call(s, "preview_distribute", {"app": APP, "channel": "firebase_android", "firebase_app_id": "1:1:ios:ab"})
    assert res["isError"] and "アプリの ID" in res["content"][0]["text"]
    res = call(s, "preview_distribute", {"app": APP, "channel": "firebase_android", "firebase_app_id": FB_APP, "groups": "a b"})
    assert res["isError"] and "グループ" in res["content"][0]["text"]
