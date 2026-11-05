#!/usr/bin/env python3
"""Mac ランナー（ADR 0028 の決定 3、P5a の mac_runner）。

工場の外の機械（macOS のホスト）で常駐し、iOS シミュレーターの操作を決まった道具だけで工場に貸す。
工場のコンテナ（dispatcher・QA のワーカー）からは host.docker.internal 越しに MCP（Streamable HTTP の JSON 応答）で呼ぶ。

- 道具は決まった一覧だけ（下の TOOLS）。任意のシェルは受けない。引数は形を確かめ、サブプロセスは argv の配列で呼ぶ（シェルを通さない）
- 認証はトークン（Authorization: Bearer）。トークンは install のときに作って ~/.config/factory/macrunner.token（0600）に置く
- ビルドするのは許したリポジトリの、既定ブランチに入った（マージ済みの）コミットだけ。ビルドは製品のコードをホストで動かすので、
  検査と judge を通る前のコードは既定で受けない（MAC_RUNNER_ALLOW_UNMERGED=1 で外せる。外すのは人の判断）
- 呼ばれた道具は ~/Library/Logs/factory-macrunner/calls.jsonl に 1 行ずつ残す（入力する文字は長さだけ）
- 対話は要らないので Remote Control ではない。macOS 標準の python3（3.9）で動くよう標準ライブラリだけで書く

使い方（ホストで）:
  python3 runner.py serve           前面で動かす（launchd が呼ぶもの）
  ../scripts/macrunner.sh install   ログインのたびに launchd が常駐させる
"""
import base64
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
PROTOCOL_VERSION = "2025-06-18"
HOME = os.path.expanduser("~")


def env(name, default=""):
    return os.environ.get(name, default)


def config():
    """設定は環境変数から（launchd の plist が渡す）。"""
    root = env("MAC_RUNNER_HOME", os.path.join(HOME, "Library", "Application Support", "factory-macrunner"))
    return {
        "host": env("MAC_RUNNER_BIND", "127.0.0.1"),
        "port": int(env("MAC_RUNNER_PORT", "8793")),
        "token_file": env("MAC_RUNNER_TOKEN_FILE", os.path.join(HOME, ".config", "factory", "macrunner.token")),
        "root": root,
        "log_dir": env("MAC_RUNNER_LOG_DIR", os.path.join(HOME, "Library", "Logs", "factory-macrunner")),
        # ビルドしてよいアプリ: <owner/repo>:<リポジトリの中のパス>:<bundle id> をカンマ区切り
        "apps": parse_apps(env("MAC_RUNNER_APPS", "guccyon/rally:apps/student-app:jp.rally.rallyStudentApp")),
        "allow_unmerged": env("MAC_RUNNER_ALLOW_UNMERGED", "") == "1",
        "flutter": env("MAC_RUNNER_FLUTTER", "flutter"),
        "idb": env("MAC_RUNNER_IDB", "idb"),
        "build_timeout": int(env("MAC_RUNNER_BUILD_TIMEOUT_SEC", "1800")),
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
DEFINE_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
URL = re.compile(r"^[a-z][a-z0-9+.\-]{1,31}://[^\s]{1,2000}$")
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

def run(argv, timeout=120, input_bytes=None, cwd=None, env_extra=None):
    """argv をシェルを通さずに動かす。失敗は ToolError にする。"""
    e = dict(os.environ)
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

    def start(self, app_key, ref, defines):
        app = self.cfg["apps"].get(app_key)
        if not app:
            raise ToolError(f"{app_key} はビルドを許していない（許しているもの: {', '.join(self.cfg['apps']) or 'なし'}）")
        bid = "b-" + uuid.uuid4().hex[:12]
        self.items[bid] = {"build_id": bid, "app": app_key, "ref": ref, "state": "queued", "started_at": now(),
                           "bundle_id": app["bundle_id"]}
        threading.Thread(target=self._run, args=(bid, app, ref, defines), daemon=True).start()
        return self.items[bid]

    def status(self, bid):
        b = self.items.get(bid)
        if not b:
            raise ToolError(f"{bid} というビルドはない（ランナーを上げ直すと消える）")
        return b

    def _run(self, bid, app, ref, defines):
        b = self.items[bid]
        with self.lock:
            b["state"] = "building"
            try:
                b.update(self._build(bid, app, ref, defines))
                b["state"] = "built"
            except ToolError as e:
                b["state"] = "failed"
                b["error"] = str(e)[-2000:]
            except Exception as e:   # ビルドの糸が落ちても状態は残す
                b["state"] = "failed"
                b["error"] = f"{type(e).__name__}: {e}"
            b["finished_at"] = now()

    def _build(self, bid, app, ref, defines):
        repo_dir = os.path.join(self.cfg["root"], "repos", app["repo"].replace("/", "__"))
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            os.makedirs(os.path.dirname(repo_dir), exist_ok=True)
            run(["git", "clone", "--filter=blob:none", "--no-checkout", f"https://github.com/{app['repo']}.git", repo_dir], timeout=600)
        run(["git", "-C", repo_dir, "fetch", "--prune", "origin"], timeout=600)
        default = run(["git", "-C", repo_dir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]).decode().strip() or "origin/main"
        sha = resolve(repo_dir, ref)
        if not self.cfg["allow_unmerged"]:
            try:
                run(["git", "-C", repo_dir, "merge-base", "--is-ancestor", sha, default])
            except ToolError:
                raise ToolError(f"{ref}（{sha[:12]}）は {default} に入っていない。ビルドするのはマージ済みのコミットだけ")
        work = os.path.join(self.cfg["root"], "builds", bid)
        run(["git", "-C", repo_dir, "worktree", "add", "--detach", work, sha], timeout=300)
        app_dir = os.path.join(work, app["path"])
        flutter = self.cfg["flutter"]
        # 製品に ios/ がまだなければ、この作業場の中だけで作る（製品のリポジトリには書かない。配布の署名の設定は P5b の preview_distribution）
        if not os.path.isdir(os.path.join(app_dir, "ios")):
            org, _, name = app["bundle_id"].rpartition(".")
            run([flutter, "create", "--platforms=ios", "--org", org, "--project-name", project_name(app_dir) or name, "."],
                cwd=app_dir, timeout=600)
        run([flutter, "pub", "get"], cwd=app_dir, timeout=600)
        argv = [flutter, "build", "ios", "--simulator", "--debug", "--no-codesign"]
        argv += [f"--dart-define={k}={v}" for k, v in sorted(defines.items())]
        log = run(argv, cwd=app_dir, timeout=self.cfg["build_timeout"])
        app_path = os.path.join(app_dir, "build", "ios", "iphonesimulator", "Runner.app")
        if not os.path.isdir(app_path):
            raise ToolError("ビルドは通ったが Runner.app がない")
        bundle = read_bundle_id(app_path) or app["bundle_id"]
        self._prune(repo_dir, keep=bid)
        return {"sha": sha, "app_path": app_path, "bundle_id": bundle, "log_tail": log.decode("utf-8", "replace")[-1500:]}


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
    tool("build_status", "ビルドの状態（queued / building / built / failed）と、終わっていれば bundle id とコミット", {"build_id": {"type": "string"}}, ["build_id"]),
    tool("app_install", "ビルドしたアプリを起動中のシミュレーターに入れる", {"udid": S_UDID, "build_id": {"type": "string"}}, ["udid", "build_id"]),
    tool("app_launch", "アプリを起動する（起動中なら前面に出す）", {"udid": S_UDID, "bundle_id": {"type": "string"}}, ["udid", "bundle_id"]),
    tool("app_terminate", "アプリを止める", {"udid": S_UDID, "bundle_id": {"type": "string"}}, ["udid", "bundle_id"]),
    tool("screenshot", "画面を撮って PNG で返す", {"udid": S_UDID}, ["udid"]),
    tool("open_url", "URL（Web かアプリのディープリンク）を開く", {"udid": S_UDID, "url": {"type": "string"}}, ["udid", "url"]),
    tool("push", "プッシュ通知を送る（APNs の payload の JSON）", {"udid": S_UDID, "bundle_id": {"type": "string"}, "payload": {"type": "object"}}, ["udid", "bundle_id", "payload"]),
    tool("logs", "直近のログ（アプリのプロセス）を返す", {"udid": S_UDID, "seconds": {"type": "integer", "minimum": 1, "maximum": 600}, "process": {"type": "string", "description": "既定 Runner"}}, ["udid"]),
    tool("ui_describe", "画面のアクセシビリティの木（要素・文言・座標）を返す。idb が要る", {"udid": S_UDID}, ["udid"]),
    tool("ui_tap", "画面の座標（ポイント）を押す。idb が要る", {"udid": S_UDID, "x": {"type": "number"}, "y": {"type": "number"}}, ["udid", "x", "y"]),
    tool("ui_swipe", "画面をなぞる（始点から終点へ）。idb が要る",
         {"udid": S_UDID, "x1": {"type": "number"}, "y1": {"type": "number"}, "x2": {"type": "number"}, "y2": {"type": "number"},
          "seconds": {"type": "number", "minimum": 0.05, "maximum": 5}}, ["udid", "x1", "y1", "x2", "y2"]),
    tool("ui_text", "文字を打つ（入力欄を押してから）。idb が要る", {"udid": S_UDID, "text": {"type": "string", "maxLength": 2000}}, ["udid", "text"]),
    tool("ui_key", "キーを押す（return / delete / escape / tab / space / home）。idb が要る", {"udid": S_UDID, "key": {"type": "string", "enum": sorted(KEYS)}}, ["udid", "key"]),
]
TOOL_NAMES = {t["name"] for t in TOOLS}


class Runner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.builds = Builds(cfg)

    def idb(self, *args, **kw):
        return run([self.cfg["idb"], *args], **kw)

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
        return text({"udid": udid, "state": "Shutdown"})

    def t_sim_erase(self, a):
        udid = need(a, "udid", UDID)
        simctl("erase", udid, timeout=300)
        return text({"udid": udid, "erased": True})

    def t_app_build(self, a):
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
        return text(public_build(self.builds.start(app, ref, defines)))

    def t_build_status(self, a):
        return text(public_build(self.builds.status(need(a, "build_id", BUILD_ID))))

    def t_app_install(self, a):
        udid = booted_udid(need(a, "udid", UDID))
        b = self.builds.status(need(a, "build_id", BUILD_ID))
        if b["state"] != "built":
            raise ToolError(f"{b['build_id']} はまだ入れられない（{b['state']}）")
        simctl("install", udid, b["app_path"], timeout=300)
        return text({"udid": udid, "bundle_id": b["bundle_id"], "sha": b["sha"]})

    def t_app_launch(self, a):
        udid = booted_udid(need(a, "udid", UDID))
        bundle = need(a, "bundle_id", BUNDLE)
        out = simctl("launch", udid, bundle).decode().strip()
        return text({"udid": udid, "bundle_id": bundle, "launched": out})

    def t_app_terminate(self, a):
        udid = booted_udid(need(a, "udid", UDID))
        bundle = need(a, "bundle_id", BUNDLE)
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
        if url.split(":", 1)[0] in ("file", "javascript"):
            raise ToolError("file: と javascript: は開かない")
        simctl("openurl", udid, url)
        return text({"udid": udid, "opened": url})

    def t_push(self, a):
        udid = booted_udid(need(a, "udid", UDID))
        bundle = need(a, "bundle_id", BUNDLE)
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
        process = a.get("process", "Runner")
        if not isinstance(process, str) or not re.match(r"^[A-Za-z0-9_.\-]{1,64}$", process):
            raise ToolError("process の形が違う")
        out = simctl("spawn", udid, "log", "show", "--style", "compact", "--last", f"{seconds}s",
                     "--predicate", f'process == "{process}"', timeout=120).decode("utf-8", "replace")
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


def public_build(b):
    return {k: v for k, v in b.items() if k != "app_path"}


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
    if name == "app_build" and isinstance(out.get("dart_defines"), dict):
        out["dart_defines"] = sorted(out["dart_defines"])
    return out


# ---- MCP（Streamable HTTP。応答は JSON で返し、SSE は使わない） ----

def handle_rpc(runner, msg, log):
    """JSON-RPC の 1 通を処理する。通知（id なし）には None を返す。"""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return rpc_error(None, -32600, "Invalid Request")
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if mid is None:
        return None
    if method == "initialize":
        return rpc_ok(mid, {"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": "factory-macrunner", "version": VERSION},
                            "instructions": "iOS シミュレーターを動かす Mac ランナー。sim_devices → sim_boot → app_build → build_status → app_install → app_launch → screenshot / ui_* の順に使う"})
    if method == "ping":
        return rpc_ok(mid, {})
    if method == "tools/list":
        return rpc_ok(mid, {"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        started = time.time()
        try:
            content, is_error = runner.call(name, args), False
        except ToolError as e:
            content, is_error = [{"type": "text", "text": str(e)}], True
        log({"at": now(), "tool": name, "args": redact(name, args), "error": is_error,
             "ms": int((time.time() - started) * 1000)})
        return rpc_ok(mid, {"content": content, "isError": is_error})
    return rpc_error(mid, -32601, f"Method not found: {method}")


def rpc_ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def rpc_error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def read_token(path):
    with open(path, encoding="utf-8") as f:
        tok = f.read().strip()
    if len(tok) < 32:
        raise SystemExit(f"{path} のトークンが短い（install で作り直す）")
    return tok


def make_handler(runner, token, log):
    class Handler(BaseHTTPRequestHandler):
        server_version = "factory-macrunner/" + VERSION

        def log_message(self, fmt, *args):   # 標準のアクセスログは出さない（calls.jsonl に残す）
            pass

        def authorized(self):
            got = self.headers.get("Authorization", "")
            return got.startswith("Bearer ") and hmac.compare_digest(got[7:].encode(), token.encode())

        def reply(self, code, body=None, ctype="application/json"):
            data = b"" if body is None else (json.dumps(body, ensure_ascii=False).encode() if not isinstance(body, bytes) else body)
            self.send_response(code)
            if data:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":   # 生きているかだけ（道具の一覧や端末は出さない）
                return self.reply(200, {"ok": True, "version": VERSION})
            if self.path == "/mcp":
                return self.reply(405)   # サーバーからの SSE は使わない
            return self.reply(404)

        def do_DELETE(self):
            return self.reply(405)

        def do_POST(self):
            if self.path != "/mcp":
                return self.reply(404)
            if not self.authorized():
                return self.reply(401, {"error": "unauthorized"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                return self.reply(413)
            try:
                msg = json.loads(self.rfile.read(n) or b"null")
            except ValueError:
                return self.reply(400, rpc_error(None, -32700, "Parse error"))
            if isinstance(msg, list):
                out = [r for r in (handle_rpc(runner, m, log) for m in msg) if r is not None]
                return self.reply(200, out) if out else self.reply(202)
            res = handle_rpc(runner, msg, log)
            return self.reply(202) if res is None else self.reply(200, res)

    return Handler


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
    return notes


def serve():
    cfg = config()
    token = read_token(cfg["token_file"])
    runner = Runner(cfg)
    log = make_log(cfg["log_dir"])
    for n in preflight(cfg):
        print(f"[macrunner] {n}", file=sys.stderr)
    httpd = ThreadingHTTPServer((cfg["host"], cfg["port"]), make_handler(runner, token, log))
    print(f"[macrunner] {VERSION} listening on {cfg['host']}:{cfg['port']}", file=sys.stderr)
    httpd.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
    elif len(sys.argv) > 1 and sys.argv[1] == "preflight":
        notes = preflight(config())
        print("\n".join(notes) or "ok")
        sys.exit(1 if notes else 0)
    else:
        print(__doc__)
        sys.exit(2)
