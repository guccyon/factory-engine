"""ジョブごとの Mac ランナーへの中継（ADR 0028 の one_orchestrator。macrunner/PROTOCOL.md の決めたいこと 1）。

QA のワーカー（エージェント）に、iOS シミュレーターの道具を MCP（Streamable HTTP の JSON の応答だけ）で渡す。
ランナーの鍵と窓口の合言葉は dispatcher の外に出さない。ワーカーに渡すのは、このジョブの間だけ有効な中継の合言葉だけ。

- 中継は dispatcher の中のスレッド（RUNNER_RELAY_PORT、既定 8796）。compose の網の中だけで、外に公開しない
- dispatcher がジョブを起こすときに grant（合言葉・ジョブ・通す道具・期限）を作り、終わったら revoke する
- 通す道具はジョブの種類ごとの一覧（QA は見る・操作する・ビルドする道具。sim_erase・sim_shutdown は通さない）。知らない道具・一覧の外は断る
- 呼んだ道具は events の runner.relayed に残す（引数は道具と長さだけ。打った文字は残さない）
- 道具の形（inputSchema）はランナーの一覧（macrunner/runner.py の TOOLS）の写し。test_runner_relay.py が食い違いを見る
- 工場の側の道具（LOCAL_TOOLS）も同じ口で渡す。いまは触れる環境の再起動と時計の進め（env_restart。ADR 0028 の scenario_time）だけで、
  Mac には送らず dispatcher が shipper に依頼する（local）。どの製品かは grant のときに決め、エージェントには選ばせない
"""
from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL_VERSION = "2025-06-18"
S_UDID = {"type": "string", "description": "端末の UDID（sim_devices で見る）"}


def _tool(name, description, props, required=()):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}}


# macrunner/runner.py の TOOLS のうち、ジョブに渡してよいものの写し（sim_erase・sim_shutdown は渡さない）
TOOLS = {t["name"]: t for t in [
    _tool("sim_devices", "使える iOS シミュレーターの一覧（UDID・名前・状態・OS）", {}),
    _tool("sim_boot", "シミュレーターを起動する（画面は出さずに動かす）。起動済みならそのまま", {"udid": S_UDID}, ["udid"]),
    _tool("app_build", "許されたアプリを、マージ済みのコミットからシミュレーター向けの debug（署名なし）でビルドし始める。build_id を返すので build_status で終わりを待つ",
          {"app": {"type": "string", "description": "<owner/repo>:<パス>（例 guccyon/rally:apps/student-app）"},
           "ref": {"type": "string", "description": "コミットかブランチ（既定 main）"},
           "dart_defines": {"type": "object", "description": "--dart-define に渡す値（例 {\"RALLY_API_URL\": \"http://127.0.0.1:8080\"}）", "additionalProperties": {"type": "string"}}},
          ["app"]),
    _tool("build_status", "ビルドの状態（queued / waiting_for_human / building / built / distributed / failed。配布は stage も）と、終わっていれば bundle id とコミット", {"build_id": {"type": "string"}}, ["build_id"]),
    _tool("app_install", "ビルドしたアプリを起動中のシミュレーターに入れる", {"udid": S_UDID, "build_id": {"type": "string"}}, ["udid", "build_id"]),
    _tool("app_launch", "アプリを起動する（起動中なら前面に出す）", {"udid": S_UDID, "bundle_id": {"type": "string"}}, ["udid", "bundle_id"]),
    _tool("app_terminate", "アプリを止める", {"udid": S_UDID, "bundle_id": {"type": "string"}}, ["udid", "bundle_id"]),
    _tool("screenshot", "画面を撮って PNG で返す", {"udid": S_UDID}, ["udid"]),
    _tool("open_url", "URL（Web かアプリのディープリンク）を開く", {"udid": S_UDID, "url": {"type": "string"}}, ["udid", "url"]),
    _tool("push", "プッシュ通知を送る（APNs の payload の JSON）", {"udid": S_UDID, "bundle_id": {"type": "string"}, "payload": {"type": "object"}}, ["udid", "bundle_id", "payload"]),
    _tool("logs", "直近のログ（許したアプリのプロセスだけ。com.apple. の部品のログは除く）を返す", {"udid": S_UDID, "seconds": {"type": "integer", "minimum": 1, "maximum": 600}}, ["udid"]),
    _tool("ui_describe", "画面のアクセシビリティの木（要素・文言・座標）を返す。idb が要る", {"udid": S_UDID}, ["udid"]),
    _tool("ui_tap", "画面の座標（ポイント）を押す。idb が要る", {"udid": S_UDID, "x": {"type": "number"}, "y": {"type": "number"}}, ["udid", "x", "y"]),
    _tool("ui_swipe", "画面をなぞる（始点から終点へ）。idb が要る",
          {"udid": S_UDID, "x1": {"type": "number"}, "y1": {"type": "number"}, "x2": {"type": "number"}, "y2": {"type": "number"},
           "seconds": {"type": "number", "minimum": 0.05, "maximum": 5}}, ["udid", "x1", "y1", "x2", "y2"]),
    _tool("ui_text", "文字を打つ（入力欄を押してから）。idb が要る", {"udid": S_UDID, "text": {"type": "string", "maxLength": 2000}}, ["udid", "text"]),
    _tool("ui_key", "キーを押す（return / delete / escape / tab / space / home）。idb が要る",
          {"udid": S_UDID, "key": {"type": "string", "enum": ["delete", "escape", "home", "return", "space", "tab"]}}, ["udid", "key"]),
]}
# 工場の側の道具（definitions/qa.yaml の env_tools と同じ）。Mac ランナーではなく dispatcher が受ける
LOCAL_TOOLS = {t["name"]: t for t in [
    _tool("env_restart", "触れる環境のサーバーを再起動する（DB は残す）。advance_hours を渡すと、サーバーの時計をその時間だけ進めて再起動する"
          "（翌日を確かめるなら 24）。reset_clock で進めた時計を 0 に戻す。終わるまで数十秒かかる。進めた時計は、次に版を出すと 0 に戻る",
          {"advance_hours": {"type": "number", "minimum": 0, "maximum": 168, "description": "進める時間（0〜168）。0 なら再起動だけ"},
           "reset_clock": {"type": "boolean", "description": "進めた時計を 0 に戻して再起動する（advance_hours とは一緒に使わない）"}}),
]}
# 工場の中での一次調査の道具（ADR 0048 の決定 2・3、definitions/investigate.yaml の sources）。読むだけ。中身は investigate_tools.py
INVESTIGATE_TOOLS = {t["name"]: t for t in [
    _tool("inv_sql", "制御プレーンの記録を SQL で読む。読めるのは investigate スキーマのビュー（jobs・events・stoppages・tickets・judgments・"
          "desk_conversations・costs・line_declarations）だけ。1 文・SELECT か WITH だけ、10 秒・500 行まで",
          {"sql": {"type": "string", "maxLength": 8000, "description": "1 文の SELECT（例 select kind, state, cost_usd from investigate.costs order by finished_at desc limit 20）"}},
          ["sql"]),
    _tool("inv_job_log", "ジョブのログの末尾を読む（秘密情報の形は伏せてある）",
          {"job_id": {"type": "integer", "minimum": 1}, "tail": {"type": "integer", "minimum": 1, "maximum": 400, "description": "末尾の行数（既定 200）"}},
          ["job_id"]),
    _tool("inv_github", "調査の対象の製品の GitHub を読む（GET だけ）。path は issues・pulls・actions/runs・commits の下（例 pulls/123、issues/45/comments、"
          "pulls/123/reviews、actions/runs?branch=main&per_page=5）",
          {"path": {"type": "string", "maxLength": 300}}, ["path"]),
]}
LOCAL_TOOLS.update(INVESTIGATE_TOOLS)
# ジョブの種類ごとに通す道具（definitions/qa.yaml の runner_tools と env_tools、definitions/investigate.yaml の sources と同じ）
KIND_TOOLS = {"qa": tuple(TOOLS) + ("env_restart",), "investigate": tuple(INVESTIGATE_TOOLS)}
CALL_TIMEOUT = {"app_build": 60, "build_status": 30}   # ビルドは build_status で待つので、呼び出し自体は短い


class Relay:
    def __init__(self, call, record, clock=time.time, local=None):
        """call(tool, arguments, timeout) → {content, isError, runner_id}。record(job_id, payload) は events への記録。
        local(tool, arguments, grant) → {content, isError} は工場の側の道具（LOCAL_TOOLS）。なければその道具は断る"""
        self.call, self.record, self.clock, self.local = call, record, clock, local
        self.lock = threading.Lock()
        self.grants: dict[str, dict] = {}

    def grant(self, job_id: int, kind: str, ttl_sec: int, repo: str | None = None) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.grants[token] = {"job_id": job_id, "tools": KIND_TOOLS.get(kind, ()), "expires": self.clock() + ttl_sec, "calls": 0,
                                  "repo": repo}
        return token

    def revoke(self, job_id: int) -> int:
        with self.lock:
            gone = [t for t, g in self.grants.items() if g["job_id"] == job_id]
            for t in gone:
                del self.grants[t]
        return len(gone)

    def _find(self, auth: str) -> dict | None:
        if not auth.startswith("Bearer "):
            return None
        tok = auth[7:].strip()
        with self.lock:
            for t, g in list(self.grants.items()):
                if g["expires"] < self.clock():
                    del self.grants[t]
                    continue
                if hmac.compare_digest(t, tok):
                    return g
        return None

    def handle(self, auth: str, msg) -> tuple[int, dict | None]:
        """JSON-RPC 1 件を受ける。(HTTP の状態, 応答)。通知（id のないもの）は 202 で応答なし"""
        g = self._find(auth or "")
        if g is None:
            return 401, {"error": "unauthorized"}
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if mid is None:
            return 202, None
        if method == "initialize":
            return 200, _ok(mid, {"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                                  "capabilities": {"tools": {"listChanged": False}},
                                  "serverInfo": {"name": "factory-relay", "version": "0.2"}})
        if method == "ping":
            return 200, _ok(mid, {})
        if method == "tools/list":
            return 200, _ok(mid, {"tools": [TOOLS.get(n) or LOCAL_TOOLS[n] for n in g["tools"]]})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in g["tools"] or not isinstance(args, dict):
                return 200, _ok(mid, _err(f"この道具はこのジョブに渡していない: {name}"))
            if name in LOCAL_TOOLS and self.local is None:
                return 200, _ok(mid, _err(f"この工場ではこの道具を使えない: {name}"))
            t0 = self.clock()
            try:
                res = self.local(name, args, g) if name in LOCAL_TOOLS else self.call(name, args, CALL_TIMEOUT.get(name, 120))
                out = {"content": res.get("content") or [], "isError": bool(res.get("isError"))}
                runner = res.get("runner_id")
            except Exception as e:  # noqa: BLE001  届かない・ランナーがいない・時間切れはエージェントに道具の失敗として返す
                out, runner = _err(f"{'工場' if name in LOCAL_TOOLS else 'ランナー'}に届かない: {str(e)[:300]}"), None
            g["calls"] += 1
            try:
                self.record(g["job_id"], {"tool": name, "ok": not out["isError"], "runner": "factory" if name in LOCAL_TOOLS else ((runner or "")[:8] or None),
                                          "ms": int((self.clock() - t0) * 1000), "args_len": len(json.dumps(args, ensure_ascii=False)), "n": g["calls"]})
            except Exception:  # noqa: BLE001  記録の失敗で道具は止めない
                pass
            return 200, _ok(mid, out)
        return 200, {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def serve(relay: Relay, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401  アクセスのログは出さない（合言葉を残さない）
            pass

        def _send(self, code: int, body: dict | None):
            data = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
            self.send_response(code)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            if self.path.split("?")[0] != "/mcp":
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                return self._send(413, {"error": "too large"})
            try:
                msg = json.loads(self.rfile.read(n) or b"null")
            except ValueError:
                return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            code, body = relay.handle(self.headers.get("Authorization") or "", msg)
            self._send(code, body)

        def do_GET(self):  # noqa: N802  サーバーから送る流れ（SSE）は持たない
            self._send(405, {"error": "method not allowed"})

        do_DELETE = do_GET

    srv = ThreadingHTTPServer((host, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True, name="runner-relay").start()
    return srv
