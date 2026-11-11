#!/usr/bin/env python3
"""Mac ランナー（ADR 0028 の決定 3、P5a の mac_runner）。

工場の外の機械（macOS のホスト）で常駐し、iOS シミュレーターの操作を決まった道具だけで工場に貸す。
ランナーは待ち受けない。工場のランナーの窓口（dispatcher/runner_gate.py）へ TLS で仕事を取りに行き、結果を返す（ADR 0033）。

- 道具は決まった一覧だけ（下の TOOLS）。任意のシェルは受けない。引数は形を確かめ、サブプロセスは argv の配列で呼ぶ（シェルを通さない）
- 工場とはペアリングで繋ぐ（工場の「許可」と Mac の「y」。両側に同じ 6 桁の番号を出す）。受け取った鍵と窓口の証明書の指紋は
  ~/.config/factory/macrunner.json（0600）に置き、その窓口とだけ話す
- ビルドするのは許したリポジトリの、既定ブランチに入った（マージ済みの）コミットだけ。ビルドは製品のコードをホストで動かすので、
  検査と judge を通る前のコードは受けない（外す設定は置かない）
- 操作できるアプリは許したアプリだけ、開ける URL は許したホストの http(s) と許したスキームだけ（範囲の宣言は SCOPE.md と SCOPE）
- 外す依頼が来たら、鍵を捨て、自分の LaunchAgent を外して止まる
- 呼ばれた道具は ~/Library/Logs/factory-macrunner/calls.jsonl に 1 行ずつ残す（入力する文字は長さだけ）
- 対話は要らないので Remote Control ではない。macOS 標準の python3（3.9）で動くよう標準ライブラリだけで書く

使い方（ホストで）:
  python3 runner.py pair            ペアリングする（対話。install が呼ぶ）
  python3 runner.py serve           窓口へ取りに行き続ける（launchd が呼ぶもの）
  python3 runner.py bye             Mac 側で外したことを窓口に知らせ、鍵を捨てる（uninstall が呼ぶ）
  ../scripts/macrunner.sh install   ペアリングして、ログインのたびに launchd が常駐させる
"""
import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid

VERSION = "0.5.0"
HOME = os.path.expanduser("~")


def env(name, default=""):
    return os.environ.get(name, default)


def config():
    """設定は環境変数から（launchd の plist が渡す）。"""
    root = env("MAC_RUNNER_HOME", os.path.join(HOME, "Library", "Application Support", "factory-macrunner"))
    return {
        # 工場のランナーの窓口（ADR 0033）。ペアリングのときに使い、鍵と一緒に覚える
        "gate": env("MAC_RUNNER_GATE", "https://127.0.0.1:8794"),
        "cred_file": env("MAC_RUNNER_CRED_FILE", os.path.join(HOME, ".config", "factory", "macrunner.json")),
        "root": root,
        "log_dir": env("MAC_RUNNER_LOG_DIR", os.path.join(HOME, "Library", "Logs", "factory-macrunner")),
        # ビルドしてよいアプリ: <owner/repo>:<リポジトリの中のパス>:<bundle id> をカンマ区切り
        "apps": parse_apps(env("MAC_RUNNER_APPS", "guccyon/rally:apps/student-app:jp.rally.rallyStudentApp")),
        # 開ける URL: このホストの http(s)（触れる環境）と、このスキーム（アプリのディープリンク）だけ
        "url_hosts": [h.strip() for h in env("MAC_RUNNER_URL_HOSTS", "127.0.0.1,localhost").split(",") if h.strip()],
        "url_schemes": [x.strip() for x in env("MAC_RUNNER_URL_SCHEMES", "").split(",") if x.strip()],
        "label": env("MAC_RUNNER_LABEL", "local.factory.macrunner"),
        "plist": env("MAC_RUNNER_PLIST", os.path.join(HOME, "Library", "LaunchAgents", "local.factory.macrunner.plist")),
        "flutter": env("MAC_RUNNER_FLUTTER", "flutter"),
        "idb": env("MAC_RUNNER_IDB", "idb"),
        "build_timeout": int(env("MAC_RUNNER_BUILD_TIMEOUT_SEC", "1800")),
        # 審査前の配布（P5b の preview_distribution、ADR 0043 の O8・ADR 0047）。配布先（channel）ごとに、人が Mac で設定を入れたときだけ開く。
        # 署名の鍵のパスワードは Mac のキーチェーン、配布先へのログインは Mac の持ち主のもの。工場には渡さない
        "firebase": env("MAC_RUNNER_FIREBASE", "firebase"),
        "firebase_app_id": env("MAC_RUNNER_FIREBASE_APP_ID", ""),          # Firebase の Android アプリの ID（1:<番号>:android:<16 進>）
        "firebase_groups": env("MAC_RUNNER_FIREBASE_GROUPS", "testers"),   # 配る相手（Firebase のテスターのグループの別名、カンマ区切り）
        "android_keystore": env("MAC_RUNNER_ANDROID_KEYSTORE", os.path.join(HOME, ".config", "factory", "android-upload.jks")),
        "android_key_alias": env("MAC_RUNNER_ANDROID_KEY_ALIAS", "upload"),
        "keychain_service": env("MAC_RUNNER_KEYCHAIN_SERVICE", "factory-macrunner-android-keystore"),
        "apksigner": env("MAC_RUNNER_APKSIGNER", ""),
        "java_home": env("MAC_RUNNER_JAVA_HOME", ""),                      # 空なら JAVA_HOME か Android Studio に入っている JDK                      # 空なら Android SDK の build-tools の新しいものを探す
        "attend_timeout": int(env("MAC_RUNNER_ATTEND_TIMEOUT_SEC", "300")),
    }


def parse_apps(spec):
    apps = {}
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        bits = part.split(":")
        if len(bits) != 3:
            raise ValueError(f"MAC_RUNNER_APPS の形が違う: {part}")
        repo, path, bundle = bits
        apps[f"{repo}:{path}"] = {"repo": repo, "path": path, "bundle_id": bundle}
    return apps


# ---- 引数の形 ----

UDID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
BUNDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,154}$")
REF = re.compile(r"^(?!-)[A-Za-z0-9._/\-]{1,200}$")
BUILD_ID = re.compile(r"^b-[0-9a-f]{12}$")
FIREBASE_APP_ID = re.compile(r"^1:[0-9]{1,20}:android:[0-9a-f]{1,40}$")
GROUP = re.compile(r"^[a-z0-9-]{1,64}$")
CHANNELS = ("firebase_android",)   # 配布先。testflight_ios は Gen2（ADR 0047）
DEFINE_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
URL = re.compile(r"^[a-z][a-z0-9+.\-]{1,31}://[^\s]{1,2000}$")
IDB_RETRIES, IDB_RETRY_SEC = 4, 5
KEYS = {"return": "40", "delete": "42", "escape": "41", "tab": "43", "space": "44", "home": None}


class ToolError(Exception):
    """利用者（呼んだエージェント）に返す失敗。MCP の isError: true になる。"""


def need(args, name, pattern=None, kind=str):
    v = args.get(name)
    if v is None:
        raise ToolError(f"{name} がない")
    if not isinstance(v, kind) or isinstance(v, bool) and kind is not bool:
        raise ToolError(f"{name} の型が違う")
    if pattern is not None and not pattern.match(v):
        raise ToolError(f"{name} の形が違う")
    return v


def number(args, name, lo, hi, default=None):
    v = args.get(name, default)
    if v is None:
        raise ToolError(f"{name} がない")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ToolError(f"{name} は数")
    if not (lo <= v <= hi):
        raise ToolError(f"{name} は {lo}〜{hi}")
    return v


# ---- サブプロセス ----

def child_env(base=None):
    """子プロセスに渡す環境。git（flutter の中の git も）は持ち主の ~/.gitconfig を読まない。
    持ち主の設定に頼らず、どの Mac でも同じに動かすため。launchd から起動したランナーは、~/.gitconfig の実体が
    「書類」などの保護フォルダにあると読めずに落ちる（2026-11-06、app_build が Operation not permitted）"""
    e = dict(os.environ if base is None else base)
    e["GIT_CONFIG_GLOBAL"] = os.devnull
    e["GIT_CONFIG_NOSYSTEM"] = "1"
    e.setdefault("GIT_TERMINAL_PROMPT", "0")
    return e


def run(argv, timeout=120, input_bytes=None, cwd=None, env_extra=None):
    """argv をシェルを通さずに動かす。失敗は ToolError にする。"""
    e = child_env()
    if env_extra:
        e.update(env_extra)
    try:
        p = subprocess.run(argv, input=input_bytes, capture_output=True, timeout=timeout, cwd=cwd, env=e)
    except FileNotFoundError:
        raise ToolError(f"{argv[0]} が見つからない（ホストに入っていない）")
    except subprocess.TimeoutExpired:
        raise ToolError(f"{argv[0]} が {timeout} 秒で終わらなかった")
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or b"").decode("utf-8", "replace")[-2000:]
        raise ToolError(f"{' '.join(argv[:3])} が失敗した（{p.returncode}）: {tail}")
    return p.stdout


def simctl(*args, **kw):
    return run(["xcrun", "simctl", *args], **kw)


def booted_udid(udid):
    """操作の前に、その端末が起動しているかを確かめる（止まっている端末への操作はわかりにくい失敗になる）。"""
    for d in list_devices():
        if d["udid"] == udid:
            if d["state"] != "Booted":
                raise ToolError(f"{udid} は起動していない（{d['state']}）。先に sim_boot")
            return udid
    raise ToolError(f"{udid} という端末はない")


def list_devices():
    data = json.loads(simctl("list", "devices", "available", "-j").decode())
    out = []
    for runtime, devs in sorted(data.get("devices", {}).items()):
        if "iOS" not in runtime:
            continue
        for d in devs:
            out.append({"udid": d["udid"], "name": d["name"], "state": d["state"],
                        "runtime": runtime.rsplit(".", 1)[-1]})
    return out


# ---- ビルド（非同期。数分かかるので build_id を返して build_status で見る） ----

class Builds:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()       # ビルドは 1 本ずつ（Xcode と Flutter のキャッシュを取り合わない）
        self.items = {}

    def start(self, app_key, ref, defines, kind="simulator", extra=None):
        app = self.cfg["apps"].get(app_key)
        if not app:
            raise ToolError(f"{app_key} はビルドを許していない（許しているもの: {', '.join(self.cfg['apps']) or 'なし'}）")
        bid = "b-" + uuid.uuid4().hex[:12]
        self.items[bid] = {"build_id": bid, "app": app_key, "ref": ref, "kind": kind, "state": "queued", "started_at": now(),
                           "bundle_id": app["bundle_id"], **(extra or {})}
        work = self._distribute if kind == "distribute" else self._build
        threading.Thread(target=self._run, args=(bid, app, ref, defines, work), daemon=True).start()
        return self.items[bid]

    def status(self, bid):
        b = self.items.get(bid)
        if not b:
            raise ToolError(f"{bid} というビルドはない（ランナーを上げ直すと消える）")
        return b

    def _run(self, bid, app, ref, defines, work=None):
        b = self.items[bid]
        work = work or self._build
        with self.lock:
            b["state"] = "building"
            try:
                b.update(work(bid, app, ref, defines))
                b["state"] = "distributed" if b.get("kind") == "distribute" else "built"
            except ToolError as e:
                b["state"] = "failed"
                b["error"] = str(e)[-2000:]
            except Exception as e:   # ビルドの糸が落ちても状態は残す
                b["state"] = "failed"
                b["error"] = f"{type(e).__name__}: {e}"
            b["finished_at"] = now()

    def _checkout(self, app, ref):
        """許したリポジトリを取り、ref が既定ブランチに入った（マージ済みの）コミットかを確かめる。(repo_dir, sha) を返す。"""
        repo_dir = os.path.join(self.cfg["root"], "repos", app["repo"].replace("/", "__"))
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            os.makedirs(os.path.dirname(repo_dir), exist_ok=True)
            run(["git", "clone", "--filter=blob:none", "--no-checkout", f"https://github.com/{app['repo']}.git", repo_dir], timeout=600)
        run(["git", "-C", repo_dir, "fetch", "--prune", "origin"], timeout=600)
        default = run(["git", "-C", repo_dir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]).decode().strip() or "origin/main"
        sha = resolve(repo_dir, ref)
        try:
            run(["git", "-C", repo_dir, "merge-base", "--is-ancestor", sha, default])
        except ToolError:
            raise ToolError(f"{ref}（{sha[:12]}）は {default} に入っていない。ビルドするのはマージ済みのコミットだけ")
        return repo_dir, sha

    def _workspace(self, bid, app, repo_dir, sha, platform="ios"):
        """ビルドの作業場を作り、製品に ios/（android/）がまだなければこの作業場の中だけで作る（製品のリポジトリには書かない）。app_dir を返す。"""
        work = os.path.join(self.cfg["root"], "builds", bid)
        run(["git", "-C", repo_dir, "worktree", "add", "--detach", work, sha], timeout=300)
        app_dir = os.path.join(work, app["path"])
        flutter = self.cfg["flutter"]
        if not os.path.isdir(os.path.join(app_dir, platform)):
            org, _, name = app["bundle_id"].rpartition(".")
            run([flutter, "create", f"--platforms={platform}", "--org", org, "--project-name", project_name(app_dir) or name, "."],
                cwd=app_dir, timeout=600)
        run([flutter, "pub", "get"], cwd=app_dir, timeout=600)
        return app_dir

    def _build(self, bid, app, ref, defines):
        repo_dir, sha = self._checkout(app, ref)
        app_dir = self._workspace(bid, app, repo_dir, sha)
        argv = [self.cfg["flutter"], "build", "ios", "--simulator", "--debug", "--no-codesign"]
        argv += [f"--dart-define={k}={v}" for k, v in sorted(defines.items())]
        log = run(argv, cwd=app_dir, timeout=self.cfg["build_timeout"])
        app_path = os.path.join(app_dir, "build", "ios", "iphonesimulator", "Runner.app")
        if not os.path.isdir(app_path):
            raise ToolError("ビルドは通ったが Runner.app がない")
        bundle = read_bundle_id(app_path) or app["bundle_id"]
        self._prune(repo_dir, keep=bid)
        return {"sha": sha, "app_path": app_path, "bundle_id": bundle, "log_tail": log.decode("utf-8", "replace")[-1500:]}

    def _distribute(self, bid, app, ref, defines):
        """署名つきでビルドし、審査前の配布先に上げる（P5b の preview_distribution、ADR 0043 の O8・ADR 0047）。今の配布先は firebase_android だけ。

        - 人が Mac の前にいる合図でだけ通る: Mac の画面に確かめの窓を出し、人が「配布する」を押したときだけ続ける。
          押されない（画面がロックされている・席にいない）まま時間が来たら、何もせずに失敗を返す
        - 署名の鍵のパスワードは Mac のキーチェーンから読み、署名の道具（apksigner）にだけ環境変数で渡す。返り値・記録・工場には出さない
        - Firebase へは Mac の持ち主が firebase login したログインで上げる。ランナーはその資格を読まない
        - 作るのはマージ済みのコミットだけ（シミュレーター向けと同じ）
        """
        b = self.items[bid]
        b["stage"] = "checkout"
        channel, notes = b["channel"], b.get("release_notes", "")
        dest = {"firebase_app_id": b.get("firebase_app_id"), "groups": b.get("groups")}
        closed = channel_closed(self.cfg, channel, dest)
        if closed:
            raise ToolError(closed)
        app_id, groups = destination(self.cfg, dest)
        repo_dir, sha = self._checkout(app, ref)
        build_number = str(int(time.time() // 60))   # 上げるたびに増える番号（Android の versionCode。分の通し番号）
        b.update({"sha": sha, "build_number": build_number, "state": "waiting_for_human", "stage": "attend"})
        attend(f"工場が {app['repo']} の {app['path']} を配布しようとしています（{channel}）。\n\n"
               f"コミット {sha[:12]}（マージ済み）、ビルド番号 {build_number}\n"
               f"宛先: Firebase のアプリ {app_id}\n配る相手: テスターのグループ {groups}\n\n"
               "この Mac の署名の鍵で署名し、この Mac の Firebase のログインで送ります。よければ「配布する」を押してください。",
               self.cfg["attend_timeout"])
        b.update({"state": "building", "stage": "build", "approved_at": now()})
        app_dir = self._workspace(bid, app, repo_dir, sha, platform="android")
        argv = [self.cfg["flutter"], "build", "apk", "--release", f"--build-number={build_number}"]
        argv += [f"--dart-define={k}={v}" for k, v in sorted(defines.items())]
        run(argv, cwd=app_dir, timeout=self.cfg["build_timeout"])
        built = os.path.join(app_dir, "build", "app", "outputs", "flutter-apk", "app-release.apk")
        if not os.path.isfile(built):
            raise ToolError("ビルドは通ったが app-release.apk がない")
        b["stage"] = "sign"
        signed = os.path.join(app_dir, "build", "app-release-signed.apk")
        password = keychain_password(self.cfg["keychain_service"])
        run([apksigner(self.cfg), "sign", "--ks", self.cfg["android_keystore"], "--ks-key-alias", self.cfg["android_key_alias"],
             "--ks-pass", "env:FACTORY_KS_PASS", "--key-pass", "env:FACTORY_KS_PASS", "--out", signed, built],
            timeout=300, env_extra={"FACTORY_KS_PASS": password, **java_env(self.cfg)})
        del password
        with open(signed, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        b["stage"] = "upload"
        argv = [self.cfg["firebase"], "appdistribution:distribute", signed, "--app", app_id,
                "--groups", groups, "--non-interactive"]
        if notes:
            argv += ["--release-notes", notes]
        log = run(argv, cwd=app_dir, timeout=900)
        self._prune(repo_dir, keep=bid)
        return {"sha": sha, "build_number": build_number, "channel": channel, "sha256": digest, "stage": "done",
                "testers": groups, "firebase_app_id": app_id, "log_tail": log.decode("utf-8", "replace")[-1500:]}

    def _prune(self, repo_dir, keep, n=3):
        """古いビルドの作業場を消す（新しい順に n 本残す）。消せなくてもビルドは失敗にしない。"""
        base = os.path.join(self.cfg["root"], "builds")
        dirs = sorted((d for d in os.listdir(base) if BUILD_ID.match(d) and d != keep),
                      key=lambda d: os.path.getmtime(os.path.join(base, d)), reverse=True)
        for d in dirs[n - 1:]:
            try:
                run(["git", "-C", repo_dir, "worktree", "remove", "--force", os.path.join(base, d)], timeout=120)
            except ToolError:
                shutil.rmtree(os.path.join(base, d), ignore_errors=True)
            self.items.pop(d, None)


def resolve(repo_dir, ref):
    for cand in (f"origin/{ref}", ref):
        try:
            return run(["git", "-C", repo_dir, "rev-parse", "--verify", "--quiet", cand + "^{commit}"]).decode().strip()
        except ToolError:
            continue
    raise ToolError(f"{ref} が見つからない")


def project_name(app_dir):
    try:
        with open(os.path.join(app_dir, "pubspec.yaml"), encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^name:\s*([a-z0-9_]+)\s*$", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def read_bundle_id(app_path):
    try:
        out = run(["/usr/libexec/PlistBuddy", "-c", "Print :CFBundleIdentifier", os.path.join(app_path, "Info.plist")])
        return out.decode().strip()
    except ToolError:
        return None


def destination(cfg, a=None):
    """配る宛先（Firebase のアプリの ID、テスターのグループ）。工場が渡せばそれ、なければ Mac の設定。
    宛先は秘密ではない（上げるのは Mac の持ち主の firebase login なので、持ち主が触れないアプリには配れない）。確かめの窓に出す"""
    a = a or {}
    return (a.get("firebase_app_id") or cfg["firebase_app_id"], a.get("groups") or cfg["firebase_groups"])


def channel_closed(cfg, channel, a=None):
    """配布先が開いていなければ、その理由（人がすること）を返す。開いていれば空。"""
    if channel not in CHANNELS:
        return f"{channel} という配布先はない（あるもの: {', '.join(CHANNELS)}）"
    app_id, groups = destination(cfg, a)
    missing = []
    if not isinstance(app_id, str) or not FIREBASE_APP_ID.match(app_id):
        missing.append("Firebase の Android アプリの ID（操業コンソールの配布先か、Mac の MAC_RUNNER_FIREBASE_APP_ID）")
    if not isinstance(groups, str) or not all(GROUP.match(g) for g in groups.split(",")) or len(groups) > 200:
        missing.append("テスターのグループの形（英小文字・数字・-、カンマ区切り）")
    if not os.path.isfile(cfg["android_keystore"]):
        missing.append(f"署名の鍵 {cfg['android_keystore']}（scripts/macrunner.sh android-key で作る）")
    if missing:
        return "配布先 firebase_android は開いていない。Mac で入れる: " + "、".join(missing)
    return ""


def keychain_password(service):
    """署名の鍵のパスワードを Mac のキーチェーンから読む（scripts/macrunner.sh android-key が入れる）。値は失敗の文にも出さない"""
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True, timeout=60, env=child_env())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        raise ToolError("キーチェーンを読めなかった（security）")
    if out.returncode != 0:
        raise ToolError(f"キーチェーンに {service} がない（scripts/macrunner.sh android-key で作る）")
    return out.stdout.decode().rstrip("\n")


ANDROID_STUDIO_JDK = "/Applications/Android Studio.app/Contents/jbr/Contents/Home"


def java_env(cfg):
    """apksigner は Java で動く。launchd から起こしたランナーには JAVA_HOME も Java の PATH もないので、
    MAC_RUNNER_JAVA_HOME → JAVA_HOME → Android Studio に入っている JDK の順に探して渡す（2026-11-12、初回の配布が sign で
    「Unable to locate a Java Runtime」で止まった。flutter は Android Studio の JDK を自分で探すのでビルドは通っていた）"""
    home = cfg.get("java_home") or env("JAVA_HOME")
    if not home and os.path.isfile(os.path.join(ANDROID_STUDIO_JDK, "bin", "java")):
        home = ANDROID_STUDIO_JDK
    if not home:
        raise ToolError("Java が見つからない（Android Studio を入れるか、MAC_RUNNER_JAVA_HOME に JDK の場所を入れて install し直す）")
    return {"JAVA_HOME": home, "PATH": os.path.join(home, "bin") + os.pathsep + env("PATH", "/usr/bin:/bin")}


def apksigner(cfg):
    if cfg["apksigner"]:
        return cfg["apksigner"]
    sdk = env("ANDROID_HOME") or env("ANDROID_SDK_ROOT") or os.path.join(HOME, "Library", "Android", "sdk")
    tools = os.path.join(sdk, "build-tools")
    try:
        versions = sorted(os.listdir(tools), key=lambda v: [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v)])
    except OSError:
        versions = []
    for v in reversed(versions):
        p = os.path.join(tools, v, "apksigner")
        if os.path.isfile(p):
            return p
    raise ToolError(f"apksigner が見つからない（Android SDK の build-tools。{tools}）")


ATTEND_BUTTON = "配布する"
ATTEND_SCRIPT = ['on run argv',
                 'display dialog (item 1 of argv) with title "工場の Mac ランナー" buttons {"やめる", "%s"} '
                 'default button "やめる" cancel button "やめる" giving up after ((item 2 of argv) as integer) with icon caution' % ATTEND_BUTTON,
                 'end run']


def attend(message, timeout):
    """人が Mac の前にいる合図（ADR 0043 の O8）。Mac の画面に確かめの窓を出し、人が押したときだけ戻る。
    やめた・時間切れ（席にいない・画面のロック）・窓が出せない（ログインしていない）は ToolError。文言は argv で渡し、スクリプトに埋めない"""
    argv = ["osascript"]
    for line in ATTEND_SCRIPT:
        argv += ["-e", line]
    argv += [message, str(int(timeout))]
    try:
        out = run(argv, timeout=timeout + 30).decode("utf-8", "replace")
    except ToolError as e:
        if "-128" in str(e):
            raise ToolError("Mac の前の人がやめた（何もしていない）")
        raise ToolError(f"Mac の画面に確かめの窓を出せなかった（何もしていない）: {e}")
    if f"button returned:{ATTEND_BUTTON}" not in out or "gave up:true" in out:
        raise ToolError(f"{timeout} 秒のあいだ Mac の前で押されなかった（人がいないとみなして何もしていない）")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---- 道具 ----

def tool(name, description, props, required=()):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}}


S_UDID = {"type": "string", "description": "端末の UDID（sim_devices で見る）"}
TOOLS = [
    tool("sim_devices", "使える iOS シミュレーターの一覧（UDID・名前・状態・OS）", {}),
    tool("sim_boot", "シミュレーターを起動する（画面は出さずに動かす）。起動済みならそのまま", {"udid": S_UDID}, ["udid"]),
    tool("sim_shutdown", "シミュレーターを止める", {"udid": S_UDID}, ["udid"]),
    tool("sim_erase", "シミュレーターを初期化する（アプリとデータを消す。止めてから）", {"udid": S_UDID}, ["udid"]),
    tool("app_build", "許されたアプリを、マージ済みのコミットからシミュレーター向けの debug（署名なし）でビルドし始める。build_id を返すので build_status で終わりを待つ",
         {"app": {"type": "string", "description": "<owner/repo>:<パス>（例 guccyon/rally:apps/student-app）"},
          "ref": {"type": "string", "description": "コミットかブランチ（既定 main）"},
          "dart_defines": {"type": "object", "description": "--dart-define に渡す値（例 {\"RALLY_API_URL\": \"http://127.0.0.1:8080\"}）", "additionalProperties": {"type": "string"}}},
         ["app"]),
    tool("build_status", "ビルドの状態（queued / waiting_for_human / building / built / distributed / failed。配布は stage も）と、終わっていれば bundle id とコミット", {"build_id": {"type": "string"}}, ["build_id"]),
    tool("preview_distribute", "許されたアプリを、マージ済みのコミットから署名つきでビルドして審査前の配布先に上げ始める（今は firebase_android だけ）。"
         "Mac の画面で人が押したときだけ進む（人がいなければ失敗）。Mac で配布先の設定を入れていなければ使えない。build_id を返すので build_status で distributed か failed を待つ",
         {"app": {"type": "string", "description": "<owner/repo>:<パス>（例 guccyon/rally:apps/student-app）"},
          "channel": {"type": "string", "enum": list(CHANNELS)},
          "ref": {"type": "string", "description": "コミットかブランチ（既定 main）"},
          "release_notes": {"type": "string", "maxLength": 1000, "description": "テスターに見せる変更点"},
          "firebase_app_id": {"type": "string", "description": "宛先の Firebase の Android アプリの ID（省くと Mac の設定）"},
          "groups": {"type": "string", "description": "配る相手のテスターのグループ（カンマ区切り。省くと Mac の設定）"},
          "dart_defines": {"type": "object", "description": "--dart-define に渡す値（例 {\"RALLY_API_URL\": \"https://…\"}）", "additionalProperties": {"type": "string"}}},
         ["app", "channel"]),
    tool("app_install", "ビルドしたアプリを起動中のシミュレーターに入れる", {"udid": S_UDID, "build_id": {"type": "string"}}, ["udid", "build_id"]),
    tool("app_launch", "アプリを起動する（起動中なら前面に出す）", {"udid": S_UDID, "bundle_id": {"type": "string"}}, ["udid", "bundle_id"]),
    tool("app_terminate", "アプリを止める", {"udid": S_UDID, "bundle_id": {"type": "string"}}, ["udid", "bundle_id"]),
    tool("screenshot", "画面を撮って PNG で返す", {"udid": S_UDID}, ["udid"]),
    tool("open_url", "URL（Web かアプリのディープリンク）を開く", {"udid": S_UDID, "url": {"type": "string"}}, ["udid", "url"]),
    tool("push", "プッシュ通知を送る（APNs の payload の JSON）", {"udid": S_UDID, "bundle_id": {"type": "string"}, "payload": {"type": "object"}}, ["udid", "bundle_id", "payload"]),
    tool("logs", "直近のログ（許したアプリのプロセスだけ。com.apple. の部品のログは除く）を返す", {"udid": S_UDID, "seconds": {"type": "integer", "minimum": 1, "maximum": 600}}, ["udid"]),
    tool("ui_describe", "画面のアクセシビリティの木（要素・文言・座標）を返す。idb が要る", {"udid": S_UDID}, ["udid"]),
    tool("ui_tap", "画面の座標（ポイント）を押す。idb が要る", {"udid": S_UDID, "x": {"type": "number"}, "y": {"type": "number"}}, ["udid", "x", "y"]),
    tool("ui_swipe", "画面をなぞる（始点から終点へ）。idb が要る",
         {"udid": S_UDID, "x1": {"type": "number"}, "y1": {"type": "number"}, "x2": {"type": "number"}, "y2": {"type": "number"},
          "seconds": {"type": "number", "minimum": 0.05, "maximum": 5}}, ["udid", "x1", "y1", "x2", "y2"]),
    tool("ui_text", "文字を打つ（入力欄を押してから）。idb が要る", {"udid": S_UDID, "text": {"type": "string", "maxLength": 2000}}, ["udid", "text"]),
    tool("ui_key", "キーを押す（return / delete / escape / tab / space / home）。idb が要る", {"udid": S_UDID, "key": {"type": "string", "enum": sorted(KEYS)}}, ["udid", "key"]),
]
TOOL_NAMES = {t["name"] for t in TOOLS}
# 範囲の宣言（SCOPE.md と同じ中身の要約）。登録済みのランナーの一覧に出す
SCOPE_SUMMARY = ("できる: iOS シミュレーターの起動・停止・初期化、許したアプリのマージ済みのコミットからの署名なしビルドと入れる・起動・停止、"
                 "画面の撮影・読み取り・操作、許した URL を開く、プッシュ通知、アプリのログ。"
                 "人が Mac の画面で押したときだけ、Mac の鍵で署名して審査前の配布先（Firebase App Distribution）に上げる（Mac で設定を入れたときだけ）。"
                 "しない: 鍵を工場に渡すこと、ストアへの公開、実機、シミュレーターの外の Mac のファイルとアプリ、任意のコマンド。"
                 "外と話すのは、工場への連絡と、製品のリポジトリ（GitHub）と依存のパッケージの取得だけ")


class Runner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.builds = Builds(cfg)

    def idb(self, *args, **kw):
        """idb を呼ぶ。起動の直後は画面の読み取りの窓口がまだ答えないことがあるので、そのときだけ間を置いて読み直す。"""
        for attempt in range(IDB_RETRIES):
            try:
                return run([self.cfg["idb"], *args], **kw)
            except ToolError as e:
                if "did not answer" not in str(e) or attempt == IDB_RETRIES - 1:
                    raise
                time.sleep(IDB_RETRY_SEC)

    def call(self, name, args):
        """道具を 1 つ動かす。返り値は MCP の content の配列。"""
        if name not in TOOL_NAMES:
            raise ToolError(f"{name} という道具はない")
        if not isinstance(args, dict):
            raise ToolError("arguments はオブジェクト")
        allowed = set(next(t for t in TOOLS if t["name"] == name)["inputSchema"]["properties"])
        extra = set(args) - allowed
        if extra:
            raise ToolError(f"知らない引数: {', '.join(sorted(extra))}")
        return getattr(self, "t_" + name)(args)

    def t_sim_devices(self, a):
        return text(list_devices())

    def t_sim_boot(self, a):
        udid = need(a, "udid", UDID)
        try:
            simctl("boot", udid)
        except ToolError as e:
            if "Booted" not in str(e) and "current state: Booted" not in str(e):
                raise
        simctl("bootstatus", udid, "-b", timeout=300)
        return text({"udid": udid, "state": "Booted"})

    def t_sim_shutdown(self, a):
        udid = need(a, "udid", UDID)
        try:
            simctl("shutdown", udid)
        except ToolError as e:
            if "Shutdown" not in str(e):
                raise
        # idb は端末ごとに idb_companion を残すので、止めた端末の分は片付ける（夜間に回すと端末の数だけ残る）
        try:
            run(["pkill", "-f", f"idb_companion.*{udid}"])
        except ToolError:
            pass   # 残っていない（pkill は 1 を返す）
        # pkill だけでは idb の記録（/tmp/idb/state）に死んだ補助プロセスが残り、起動し直した端末で ui_* が繋がらない（実機で確認）。idb からも外す
        try:
            self.idb("disconnect", udid, timeout=30)
        except ToolError:
            pass   # idb がない・繋いでいなかった
        return text({"udid": udid, "state": "Shutdown"})

    def t_sim_erase(self, a):
        udid = need(a, "udid", UDID)
        simctl("erase", udid, timeout=300)
        return text({"udid": udid, "erased": True})

    def t_app_build(self, a):
        return text(public_build(self.builds.start(*build_args(a))))

    def t_preview_distribute(self, a):
        channel = need(a, "channel")
        closed = channel_closed(self.cfg, channel, a)
        if closed:
            raise ToolError(closed)
        notes = a.get("release_notes", "")
        if not isinstance(notes, str) or len(notes) > 1000:
            raise ToolError("release_notes は 1000 文字までの文字列")
        app, ref, defines = build_args(a)
        return text(public_build(self.builds.start(app, ref, defines, kind="distribute", extra={
            "channel": channel, "release_notes": notes, "firebase_app_id": a.get("firebase_app_id"), "groups": a.get("groups")})))

    def t_build_status(self, a):
        return text(public_build(self.builds.status(need(a, "build_id", BUILD_ID))))

    def t_app_install(self, a):
        udid = booted_udid(need(a, "udid", UDID))
        b = self.builds.status(need(a, "build_id", BUILD_ID))
        if b["state"] != "built":
            raise ToolError(f"{b['build_id']} はまだ入れられない（{b['state']}）")
        simctl("install", udid, b["app_path"], timeout=300)
        return text({"udid": udid, "bundle_id": b["bundle_id"], "sha": b["sha"]})

    def allowed_bundle(self, a):
        """操作できるアプリは、許したアプリ（MAC_RUNNER_APPS）とそのビルドの bundle id だけ。端末の標準のアプリやほかのアプリは触らない。"""
        bundle = need(a, "bundle_id", BUNDLE)
        ok = {x["bundle_id"] for x in self.cfg["apps"].values()} | {b.get("bundle_id") for b in self.builds.items.values() if b.get("state") == "built"}
        if bundle not in ok:
            raise ToolError(f"{bundle} は操作を許していない（許しているもの: {', '.join(sorted(x for x in ok if x))}）")
        return bundle

    def t_app_launch(self, a):
        udid = booted_udid(need(a, "udid", UDID))
        bundle = self.allowed_bundle(a)
        out = simctl("launch", udid, bundle).decode().strip()
        return text({"udid": udid, "bundle_id": bundle, "launched": out})

    def t_app_terminate(self, a):
        udid = booted_udid(need(a, "udid", UDID))
        bundle = self.allowed_bundle(a)
        try:
            simctl("terminate", udid, bundle)
        except ToolError as e:
            if "found nothing to terminate" not in str(e):
                raise
        return text({"udid": udid, "bundle_id": bundle, "terminated": True})

    def t_screenshot(self, a):
        udid = booted_udid(need(a, "udid", UDID))
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            simctl("io", udid, "screenshot", "--type=png", path, timeout=60)
            with open(path, "rb") as f:
                data = f.read()
        finally:
            os.unlink(path)
        return [{"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/png"}]

    def t_open_url(self, a):
        udid = booted_udid(need(a, "udid", UDID))
        url = need(a, "url", URL)
        scheme = url.split(":", 1)[0]
        if scheme in ("http", "https"):
            host = re.match(r"^https?://([^/:?#@]+)", url)
            if not host or host.group(1).lower() not in self.cfg["url_hosts"]:
                raise ToolError(f"開けるのは {', '.join(self.cfg['url_hosts'])} の http(s) だけ（MAC_RUNNER_URL_HOSTS）")
        elif scheme not in self.cfg["url_schemes"]:
            raise ToolError(f"{scheme}: は開かない（開けるスキーム: {', '.join(self.cfg['url_schemes']) or 'なし'}。MAC_RUNNER_URL_SCHEMES）")
        simctl("openurl", udid, url)
        return text({"udid": udid, "opened": url})

    def t_push(self, a):
        udid = booted_udid(need(a, "udid", UDID))
        bundle = self.allowed_bundle(a)
        payload = a.get("payload")
        if not isinstance(payload, dict) or "aps" not in payload:
            raise ToolError("payload は aps を持つオブジェクト")
        body = json.dumps(payload).encode()
        if len(body) > 4096:
            raise ToolError("payload は 4KB まで")
        simctl("push", udid, bundle, "-", input_bytes=body)
        return text({"udid": udid, "bundle_id": bundle, "pushed": True})

    def t_logs(self, a):
        udid = booted_udid(need(a, "udid", UDID))
        seconds = int(number(a, "seconds", 1, 600, 60))
        process = "Runner"   # 許したアプリ（Flutter の iOS アプリのプロセス名）だけ。ほかのプロセスのログは読まない
        out = simctl("spawn", udid, "log", "show", "--style", "compact", "--last", f"{seconds}s",
                     "--predicate", f'process == "{process}" AND NOT subsystem BEGINSWITH "com.apple."', timeout=120).decode("utf-8", "replace")
        return [{"type": "text", "text": out[-20000:] or "（ログなし）"}]

    def t_ui_describe(self, a):
        udid = booted_udid(need(a, "udid", UDID))
        out = self.idb("ui", "describe-all", "--udid", udid, "--json", timeout=60).decode("utf-8", "replace")
        return [{"type": "text", "text": out[-60000:]}]

    def t_ui_tap(self, a):
        udid = booted_udid(need(a, "udid", UDID))
        x, y = number(a, "x", 0, 5000), number(a, "y", 0, 5000)
        self.idb("ui", "tap", "--udid", udid, str(round(x)), str(round(y)))
        return text({"tapped": [round(x), round(y)]})

    def t_ui_swipe(self, a):
        udid = booted_udid(need(a, "udid", UDID))
        pts = [str(round(number(a, k, 0, 5000))) for k in ("x1", "y1", "x2", "y2")]
        dur = number(a, "seconds", 0.05, 5, 0.3)
        self.idb("ui", "swipe", "--udid", udid, "--duration", str(dur), *pts)
        return text({"swiped": pts})

    def t_ui_text(self, a):
        udid = booted_udid(need(a, "udid", UDID))
        s = need(a, "text")
        if len(s) > 2000:
            raise ToolError("text は 2000 文字まで")
        self.idb("ui", "text", "--udid", udid, s)
        return text({"typed_chars": len(s)})

    def t_ui_key(self, a):
        udid = booted_udid(need(a, "udid", UDID))
        key = need(a, "key")
        if key not in KEYS:
            raise ToolError("key の値が違う")
        if key == "home":
            self.idb("ui", "button", "--udid", udid, "HOME")
        else:
            self.idb("ui", "key", "--udid", udid, KEYS[key])
        return text({"key": key})


def build_args(a):
    """app_build と preview_distribute に共通の引数を確かめる。(app, ref, defines) を返す。"""
    app = need(a, "app")
    ref = a.get("ref", "main")
    if not isinstance(ref, str) or not REF.match(ref) or ".." in ref:
        raise ToolError("ref の形が違う")
    defines = a.get("dart_defines") or {}
    if not isinstance(defines, dict) or len(defines) > 20:
        raise ToolError("dart_defines はオブジェクト（20 個まで）")
    for k, v in defines.items():
        if not DEFINE_KEY.match(k) or not isinstance(v, str) or len(v) > 500 or "\n" in v:
            raise ToolError(f"dart_defines の {k} の形が違う")
    return app, ref, defines


def public_build(b):
    return {k: v for k, v in b.items() if k not in ("app_path", "release_notes")}


def text(obj):
    return [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}]


def redact(name, args):
    """記録に残す引数。打った文字と通知の中身は長さだけ残す。"""
    if not isinstance(args, dict):
        return {}
    out = dict(args)
    if name == "ui_text" and isinstance(out.get("text"), str):
        out["text"] = f"<{len(out['text'])} 文字>"
    if name == "push":
        out["payload"] = "<省略>"
    if name == "preview_distribute" and isinstance(out.get("release_notes"), str):
        out["release_notes"] = f"<{len(out['release_notes'])} 文字>"
    if name in ("app_build", "preview_distribute") and isinstance(out.get("dart_defines"), dict):
        out["dart_defines"] = sorted(out["dart_defines"])
    return out


def make_log(log_dir):
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "calls.jsonl")
    lock = threading.Lock()

    def log(rec):
        with lock, open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return log


def preflight(cfg):
    """起動のときに足りないものを言う（止めはしない。idb がなければ ui_* が失敗を返すだけ）。"""
    notes = []
    if sys.platform != "darwin":
        notes.append("macOS ではない: シミュレーターの道具は失敗を返す")
    for exe, what in (("xcrun", "Xcode"), (cfg["flutter"], "Flutter"), (cfg["idb"], "idb（ui_* に要る）"), ("git", "git")):
        if not shutil.which(exe):
            notes.append(f"{exe} が見つからない（{what}）")
    if not os.path.isfile(cfg["android_keystore"]):   # 宛先（アプリの ID）は工場が渡せるので、Mac に要るのは鍵と Firebase CLI のログイン
        notes.append(f"審査前の配布は閉じている: 署名の鍵 {cfg['android_keystore']} がない（scripts/macrunner.sh android-key。配布を使わないなら、このままでよい）")
    elif not shutil.which(cfg["firebase"]):
        notes.append(f"{cfg['firebase']} が見つからない（Firebase CLI。npm install -g firebase-tools のあと firebase login）")
    return notes


# ---- 工場のランナーの窓口との話（ADR 0033。ランナーは待ち受けず、窓口へ取りに行く） ----

def execute(runner, name, args, log):
    """窓口から来た道具の呼び出しを 1 つ動かす。返り値は {content, isError}。"""
    started = time.time()
    try:
        content, is_error = runner.call(name, args if isinstance(args, dict) else {}), False
    except ToolError as e:
        content, is_error = [{"type": "text", "text": str(e)}], True
    log({"at": now(), "tool": name, "args": redact(name, args), "error": is_error, "ms": int((time.time() - started) * 1000)})
    return {"content": content, "isError": is_error}


def sas(runner_nonce, gate_nonce, fingerprint):
    """両側に出す 6 桁の番号（dispatcher/runner_gate.py の sas と同じ計算）。"""
    d = hashlib.sha256(b"factory-pair\0" + bytes.fromhex(runner_nonce) + bytes.fromhex(gate_nonce) + bytes.fromhex(fingerprint)).digest()
    n = int.from_bytes(d[:4], "big") % 1000000
    return "%03d %03d" % (n // 1000, n % 1000)


def sign(key, ts, path, body):
    return hmac.new(bytes.fromhex(key), ("%s\n%s\n" % (ts, path)).encode() + body, hashlib.sha256).hexdigest()


class GateError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Gate:
    """窓口への TLS の接続。ペアリングで覚えた証明書の指紋とだけ話す（ペアリングの前は、相手の指紋を番号に混ぜて人が確かめる）。"""

    def __init__(self, url, fingerprint=None, timeout=40):
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" or not u.hostname:
            raise SystemExit(f"窓口の URL は https://ホスト:ポート（{url}）")
        self.host, self.port, self.fp, self.timeout = u.hostname, u.port or 8794, fingerprint, timeout
        self.seen_fp = None

    def post(self, path, obj, cred=None):
        body = json.dumps(obj).encode()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # 自分用の証明書なので CA では確かめず、指紋で確かめる（下）
        conn = http.client.HTTPSConnection(self.host, self.port, context=ctx, timeout=self.timeout)
        try:
            conn.connect()
            self.seen_fp = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
            if self.fp and not hmac.compare_digest(self.seen_fp, self.fp):
                raise GateError(0, "窓口の証明書がペアリングのときと違う。話さない（ペアリングし直す）")
            headers = {"Content-Type": "application/json"}
            if cred:
                ts = str(int(time.time()))
                headers.update({"X-Runner-Id": cred["runner_id"], "X-Runner-Ts": ts, "X-Runner-Sig": sign(cred["key"], ts, path, body)})
            conn.request("POST", path, body=body, headers=headers)
            r = conn.getresponse()
            data = r.read()
            try:
                out = json.loads(data or b"{}")
            except ValueError:
                out = {}
            if r.status != 200:
                raise GateError(r.status, out.get("error") or f"窓口が {r.status} を返した")
            return out
        finally:
            conn.close()


def load_cred(cfg):
    try:
        with open(cfg["cred_file"], encoding="utf-8") as f:
            c = json.load(f)
        return c if all(k in c for k in ("runner_id", "key", "fingerprint", "gate")) else None
    except (OSError, ValueError):
        return None


def save_cred(cfg, cred):
    os.makedirs(os.path.dirname(cfg["cred_file"]), exist_ok=True)
    fd = os.open(cfg["cred_file"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cred, f)


def drop_cred(cfg):
    try:
        os.unlink(cfg["cred_file"])
    except OSError:
        pass


def hello_info(cfg):
    import platform
    import socket
    tools = [n for exe, n in (("xcrun", "Xcode"), (cfg["flutter"], "Flutter"), (cfg["idb"], "idb")) if shutil.which(exe)]
    try:
        devices = str(len(list_devices()))
    except (ToolError, ValueError, KeyError):
        devices = ""
    def ver(argv):
        try:
            return run(argv, timeout=30).decode("utf-8", "replace").strip().splitlines()[0][:80]
        except (ToolError, IndexError):
            return ""
    return {"name": socket.gethostname().split(".")[0], "kind": "mac", "version": VERSION, "label": cfg["label"],
            "path": os.path.abspath(__file__), "platform": ("macOS " + platform.mac_ver()[0]).strip(),
            "tools": "・".join(tools), "devices": devices, "home": cfg["root"], "logs": cfg["log_dir"], "capacity": "1",
            "xcode": ver(["xcodebuild", "-version"]), "flutter": ver([cfg["flutter"], "--version"]),
            "scope": SCOPE_SUMMARY + "。アプリ: " + "、".join(sorted(cfg["apps"]))}


def pair(cfg, ask=input, out=print):
    """ペアリング（ADR 0033 の決定 1）。両側に同じ 6 桁の番号を出し、工場の「許可」と、ここでの y がそろったら鍵を受け取る。"""
    g = Gate(cfg["gate"])
    rn = os.urandom(16).hex()
    info = hello_info(cfg)
    start = g.post("/pair/start", {"commit": hashlib.sha256(bytes.fromhex(rn)).hexdigest(), "info": info})
    fp = g.seen_fp
    g.fp = fp   # 以後はこの相手とだけ話す
    pid = start["pair_id"]
    g.post("/pair/reveal", {"pair_id": pid, "runner_nonce": rn})
    code = sas(rn, start["gate_nonce"], fp)
    out(f"工場の操業コンソールの「登録済みのランナー」に、次の番号の接続の依頼が出ています: {code}")
    out("同じ番号なら、コンソールで「許可」を押してから、ここで y を押してください（10 分で切れます）")
    if ask("同じ番号ですか？ [y/N] ").strip().lower() != "y":
        out("やめました（工場の依頼は 10 分で切れます）")
        return None
    g.post("/pair/confirm", {"pair_id": pid, "runner_nonce": rn})
    out("工場の「許可」を待っています…")
    deadline = time.time() + 600
    while time.time() < deadline:
        r = g.post("/pair/wait", {"pair_id": pid, "runner_nonce": rn})
        if r.get("key"):
            cred = {"runner_id": r["runner_id"], "key": r["key"], "fingerprint": fp, "gate": cfg["gate"]}
            save_cred(cfg, cred)
            out(f"繋がりました（ランナー {r['runner_id'][:8]}）")
            return cred
    out("10 分で許可されませんでした。やり直してください")
    return None


def self_uninstall(cfg, log):
    """工場の「外す」に応える: 鍵を捨て、LaunchAgent の plist を消し、launchd から外す（ここで止まる）。
    ビルドの作業場とログは残す（消すのは人）。窓口の側は、外す依頼を返したときに鍵を捨てている。"""
    log({"at": now(), "tool": "self_uninstall", "args": {}, "error": False, "ms": 0})
    drop_cred(cfg)
    try:
        os.unlink(cfg["plist"])
    except OSError:
        pass
    try:
        run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), cfg["label"])])
    except ToolError:
        pass
    os._exit(0)   # launchd の外で動かしていたときも止まる


def work_once(cfg, runner, cred, gate, log, info=None):
    """窓口に 1 回取りに行き、来た仕事をこなす。返り値は 'ok' / 'uninstall'。"""
    r = gate.post("/poll", {"info": info} if info else {}, cred)
    if r.get("uninstall"):
        return "uninstall"
    for c in r.get("calls") or []:
        res = execute(runner, c.get("tool"), c.get("arguments"), log)
        gate.post("/result", {"call_id": c.get("call_id"), **res}, cred)
    return "ok"


def serve():
    cfg = config()
    cred = load_cred(cfg)
    if not cred:
        raise SystemExit("ペアリングしていない（scripts/macrunner.sh install でペアリングする）")
    runner = Runner(cfg)
    log = make_log(cfg["log_dir"])
    for n in preflight(cfg):
        print(f"[macrunner] {n}", file=sys.stderr)
    gate = Gate(cred["gate"], cred["fingerprint"])
    print(f"[macrunner] {VERSION} → {cred['gate']}（ランナー {cred['runner_id'][:8]}）", file=sys.stderr)
    last_info, backoff = 0, 5
    while True:
        try:
            info = hello_info(cfg) if time.time() - last_info > 300 else None
            if work_once(cfg, runner, cred, gate, log, info) == "uninstall":
                self_uninstall(cfg, log)
            if info:
                last_info = time.time()
            backoff = 5
        except GateError as e:
            print(f"[macrunner] 窓口: {e}", file=sys.stderr)
            time.sleep(60 if e.status in (0, 401) else backoff)
            backoff = min(backoff * 2, 60)
        except OSError as e:   # 工場が止まっている・網が切れている。止まらずに待つ
            print(f"[macrunner] 窓口に届かない: {e}", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def bye(cfg):
    """Mac 側で外したことを窓口に知らせ（鍵を捨てさせる）、こちらの鍵も捨てる（scripts/macrunner.sh uninstall が呼ぶ）。"""
    cred = load_cred(cfg)
    if cred:
        try:
            Gate(cred["gate"], cred["fingerprint"], timeout=10).post("/bye", {}, cred)
        except (GateError, OSError) as e:
            print(f"窓口に知らせられなかった（工場の一覧から「外す」で消せます）: {e}", file=sys.stderr)
    drop_cred(cfg)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "serve":
        serve()
    elif cmd == "pair":
        try:
            sys.exit(0 if pair(config()) else 1)
        except (GateError, OSError) as e:
            print(f"窓口と話せなかった: {e}", file=sys.stderr)
            sys.exit(1)
    elif cmd == "paired":
        sys.exit(0 if load_cred(config()) else 1)
    elif cmd == "bye":
        bye(config())
    elif cmd == "preflight":
        notes = preflight(config())
        print("\n".join(notes) or "ok")
        sys.exit(1 if notes else 0)
    else:
        print(__doc__)
        sys.exit(2)
