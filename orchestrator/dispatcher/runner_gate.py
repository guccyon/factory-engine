#!/usr/bin/env python3
"""ランナーの窓口（ADR 0033）。ランナー（orchestrator/macrunner/runner.py）は、ここへ仕事を取りに来る。工場からランナーへは繋ぎに行かない。

ランナー向け（TLS。工場が作った自分用の証明書。ランナーはペアリングのときにその指紋を覚え、以後はそれ以外の相手と話さない）:
  POST /pair/start   {commit, info}             ペアリングの依頼。commit はランナーの乱数のハッシュ。窓口の乱数と pair_id を返す
  POST /pair/reveal  {pair_id, runner_nonce}    ランナーの乱数を明かす。両側が同じ 6 桁の番号を計算でき、窓口は操業コンソールに出す
  POST /pair/confirm {pair_id, runner_nonce}    Mac で y を押した
  POST /pair/wait    {pair_id, runner_nonce}    コンソールの許可を待つ（25 秒の長いポーリング）。両方そろえば、そのランナー専用の鍵を 1 回だけ渡す
  POST /poll         {info?}                    （鍵の署名）仕事を待つ。返事は {calls: [...]} か {uninstall: true}
  POST /result       {call_id, content, isError}  （鍵の署名）道具の結果
  POST /bye          {}                         （鍵の署名）Mac 側で外した。鍵を捨てる

工場の中向け（平文。compose の網の中だけ、公開しない。合言葉は窓口の置き場のファイルで、dispatcher だけが読み取り専用でマウントする）:
  POST /internal/call    {runner: id|"any", tool, arguments, timeout}   道具を 1 つ呼んで結果を待つ（factoryctl runner check、ジョブの中継）
  GET  /internal/runners                                                窓口に来ているランナーと、いま預けている呼び出しの数

守り:
- ランナーごとの鍵（HMAC）。鍵は窓口の置き場（RUNNER_GATE_HOME）にだけ置き、events にも DB にも書かない。外すと捨てる
- ペアリングは両方の承認（コンソールの「許可」と Mac の「y」）。番号は両側の乱数と証明書の指紋から作る（ランナーは先に乱数のハッシュを出すので、
  間に入った者が番号を合わせられない）。依頼は 10 分で切れる
- 窓口からランナーに送れるのは、ランナーの道具の呼び出し（道具の一覧はランナーの側で固定）と、外す依頼だけ
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import json
import os
import secrets
import ssl
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import runners

HOME = os.environ.get("RUNNER_GATE_HOME", "/var/lib/runner-gate")
WAIT_SEC = 25
ONLINE_SEC = 60          # これより長く /poll に来なければ、呼び出しを預けない
SKEW_SEC = 300           # 署名の時刻のずれの許し


def sas(runner_nonce: str, gate_nonce: str, fingerprint: str) -> str:
    """両側に出す 6 桁の番号。"""
    d = hashlib.sha256(b"factory-pair\0" + bytes.fromhex(runner_nonce) + bytes.fromhex(gate_nonce) + bytes.fromhex(fingerprint)).digest()
    n = int.from_bytes(d[:4], "big") % 1_000_000
    return f"{n // 1000:03d} {n % 1000:03d}"


def sign(key: str, ts: str, path: str, body: bytes) -> str:
    return hmac.new(bytes.fromhex(key), f"{ts}\n{path}\n".encode() + body, hashlib.sha256).hexdigest()


def ensure_cert(home: str) -> tuple[str, str, str]:
    """工場の自分用の証明書（なければ作る）。(cert, key, 指紋 sha256 の 16 進) を返す。"""
    cert, key = os.path.join(home, "cert.pem"), os.path.join(home, "key.pem")
    if not (os.path.exists(cert) and os.path.exists(key)):
        os.makedirs(home, exist_ok=True)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650", "-subj", "/CN=factory-runner-gate",
                        "-keyout", key, "-out", cert], check=True, capture_output=True)
        os.chmod(key, 0o600)
    with open(cert, encoding="ascii") as f:
        der = ssl.PEM_cert_to_DER_cert(f.read())
    return cert, key, hashlib.sha256(der).hexdigest()


class Gate:
    def __init__(self, home: str, cursor, fingerprint: str, clock=time.time):
        self.home, self.cursor, self.fp, self.clock = home, cursor, fingerprint, clock
        self.lock = threading.Condition()
        self.keys = self._load()
        self.pairs: dict[str, dict] = {}
        self.queues: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.calls: dict[str, dict] = {}
        self.last_poll: dict[str, float] = {}
        self.beats: dict[str, datetime] = {}

    # ---- 鍵の置き場 ----
    def _path(self):
        return os.path.join(self.home, "runners.json")

    def _load(self) -> dict:
        try:
            with open(self._path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        os.makedirs(self.home, exist_ok=True)
        tmp = self._path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.keys, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path())

    def forget(self, rid: str, how: str):
        with self.lock:
            self.keys.pop(rid, None)
            self._save()
            self.last_poll.pop(rid, None)
            for c in list(self.queues.pop(rid, [])):
                self._finish(c["call_id"], [{"type": "text", "text": "ランナーが外れた"}], True)
        with self.cursor() as cur:
            runners.uninstalled(cur, rid, how)

    # ---- ペアリング ----
    def pair_start(self, body: dict) -> tuple[int, dict]:
        commit = str(body.get("commit") or "")
        if len(commit) != 64 or any(c not in "0123456789abcdef" for c in commit):
            return 400, {"error": "commit は sha256 の 16 進"}
        info = body.get("info") if isinstance(body.get("info"), dict) else {}
        pid, gn = uuid.uuid4().hex, secrets.token_hex(16)
        with self.lock:
            self._expire()
            if len(self.pairs) >= 20:
                return 429, {"error": "ペアリングの依頼が多すぎる"}
            self.pairs[pid] = {"commit": commit, "gate_nonce": gn, "info": runners.clean(info), "at": self.clock(), "runner_ok": False}
        return 200, {"pair_id": pid, "gate_nonce": gn}

    def _pair(self, body: dict) -> dict | None:
        p = self.pairs.get(str(body.get("pair_id") or ""))
        rn = str(body.get("runner_nonce") or "")
        if not p or len(rn) != 32 or hashlib.sha256(bytes.fromhex(rn) if all(c in "0123456789abcdef" for c in rn) else b"x").hexdigest() != p["commit"]:
            return None
        if "runner_nonce" in p and p["runner_nonce"] != rn:
            return None
        return p

    def pair_reveal(self, body: dict) -> tuple[int, dict]:
        with self.lock:
            p = self._pair(body)
            if not p or "code" in p:
                return 404, {"error": "no such pairing"}
            p["runner_nonce"] = body["runner_nonce"]
            p["code"] = sas(p["runner_nonce"], p["gate_nonce"], self.fp)
            pid = body["pair_id"]
        with self.cursor() as cur:
            runners.pair_requested(cur, pid, p["code"], p["info"])
        return 200, {}

    def pair_confirm(self, body: dict) -> tuple[int, dict]:
        with self.lock:
            p = self._pair(body)
            if not p or "code" not in p:
                return 404, {"error": "no such pairing"}
            p["runner_ok"] = True
        return 200, {}

    def pair_wait(self, body: dict) -> tuple[int, dict]:
        deadline = self.clock() + WAIT_SEC
        while True:
            with self.lock:
                p = self._pair(body)
                if not p or "code" not in p:
                    return 404, {"error": "no such pairing"}
            with self.cursor() as cur:
                st = runners.pair_state(cur, body["pair_id"])
            if st in ("rejected", "expired", "unknown"):
                with self.lock:
                    self.pairs.pop(body["pair_id"], None)
                return 403, {"error": {"rejected": "工場で拒否された", "expired": "依頼が切れた", "unknown": "工場に依頼が見つからない"}[st]}
            if st == "approved" and p["runner_ok"]:
                rid, key = uuid.uuid4().hex, secrets.token_hex(32)
                with self.lock:
                    self.pairs.pop(body["pair_id"], None)
                    self.keys[rid] = {"key": key, "name": p["info"].get("name", ""), "paired_at": datetime.now(timezone.utc).isoformat()}
                    self._save()
                with self.cursor() as cur:
                    runners.register(cur, rid, p["info"])
                return 200, {"runner_id": rid, "key": key}
            if self.clock() >= deadline:
                return 200, {"state": "waiting", "approved": st == "approved"}
            time.sleep(1)

    def _expire(self):
        ttl = runners.PAIR_TTL.total_seconds()
        for k in [k for k, p in self.pairs.items() if self.clock() - p["at"] > ttl]:
            self.pairs.pop(k)

    # ---- 署名 ----
    def verify(self, headers, path: str, body: bytes) -> str | None:
        rid, ts, sig = headers.get("X-Runner-Id", ""), headers.get("X-Runner-Ts", ""), headers.get("X-Runner-Sig", "")
        k = self.keys.get(rid)
        if not k or not ts.isdigit() or abs(self.clock() - int(ts)) > SKEW_SEC:
            return None
        return rid if hmac.compare_digest(sign(k["key"], ts, path, body), sig) else None

    # ---- 仕事 ----
    def poll(self, rid: str, body: dict) -> tuple[int, dict]:
        with self.cursor() as cur:
            pending, self.beats[rid] = runners.hello(cur, rid, body.get("info") if isinstance(body.get("info"), dict) else {}, self.beats.get(rid))
        if pending:
            self.forget(rid, "console")
            return 200, {"uninstall": True}
        deadline = self.clock() + WAIT_SEC
        with self.lock:
            self.last_poll[rid] = self.clock()
            while not self.queues[rid] and self.clock() < deadline:
                self.lock.wait(timeout=1)
                self.last_poll[rid] = self.clock()
            calls = []
            if self.queues[rid]:
                c = self.queues[rid].popleft()
                c["state"] = "sent"
                calls.append({"call_id": c["call_id"], "tool": c["tool"], "arguments": c["arguments"]})
        return 200, {"calls": calls}

    def result(self, rid: str, body: dict) -> tuple[int, dict]:
        cid = str(body.get("call_id") or "")
        with self.lock:
            c = self.calls.get(cid)
            if not c or c["runner_id"] != rid or c["state"] != "sent":
                return 404, {"error": "no such call"}
            content = body.get("content") if isinstance(body.get("content"), list) else []
            self._finish(cid, content, bool(body.get("isError")))
        return 200, {}

    def _finish(self, cid, content, is_error):
        c = self.calls.get(cid)
        if c and c["state"] != "done":
            c.update(state="done", content=content, isError=is_error)
            self.lock.notify_all()

    def online(self) -> list[str]:
        return [r for r, t in self.last_poll.items() if r in self.keys and self.clock() - t <= ONLINE_SEC]

    def call(self, body: dict) -> tuple[int, dict]:
        tool = str(body.get("tool") or "")
        args = body.get("arguments") if isinstance(body.get("arguments"), dict) else {}
        timeout = min(max(int(body.get("timeout") or 120), 1), 900)
        with self.lock:
            want = str(body.get("runner") or "any")
            live = self.online()
            if want == "any":
                if not live:
                    return 503, {"error": "窓口に来ているランナーがない"}
                rid = min(live, key=lambda r: sum(1 for c in self.calls.values() if c["runner_id"] == r and c["state"] != "done"))
            elif want in live:
                rid = want
            else:
                return 503, {"error": f"{want} は窓口に来ていない"}
            cid = uuid.uuid4().hex
            c = {"call_id": cid, "runner_id": rid, "tool": tool, "arguments": args, "state": "queued"}
            self.calls[cid] = c
            self.queues[rid].append(c)
            self.lock.notify_all()
            deadline = self.clock() + timeout
            while c["state"] != "done" and self.clock() < deadline:
                self.lock.wait(timeout=1)
            self.calls.pop(cid, None)
            if c["state"] != "done":
                if c in self.queues[rid]:
                    self.queues[rid].remove(c)
                return 504, {"error": f"{timeout} 秒で結果が返らなかった", "runner_id": rid}
        return 200, {"runner_id": rid, "content": c["content"], "isError": c["isError"]}

    def runners_view(self) -> list[dict]:
        with self.lock:
            live = set(self.online())
            return [{"id": r, "name": k.get("name"), "online": r in live,
                     "inflight": sum(1 for c in self.calls.values() if c["runner_id"] == r)} for r, k in self.keys.items()]


def make_runner_handler(gate: Gate):
    class H(BaseHTTPRequestHandler):
        server_version = "factory-runner-gate"

        def log_message(self, fmt, *a):
            pass

        def _reply(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self._reply(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            if n > 20_000_000:
                return self._reply(413, {"error": "too large"})
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return self._reply(400, {"error": "invalid json"})
            if not isinstance(body, dict):
                return self._reply(400, {"error": "object expected"})
            pair = {"/pair/start": gate.pair_start, "/pair/reveal": gate.pair_reveal, "/pair/confirm": gate.pair_confirm, "/pair/wait": gate.pair_wait}
            if self.path in pair:
                return self._reply(*pair[self.path](body))
            if self.path not in ("/poll", "/result", "/bye"):
                return self._reply(404, {"error": "not found"})
            rid = gate.verify(self.headers, self.path, raw)
            if not rid:
                return self._reply(401, {"error": "署名が違う（ペアリングし直す）"})
            if self.path == "/poll":
                return self._reply(*gate.poll(rid, body))
            if self.path == "/result":
                return self._reply(*gate.result(rid, body))
            gate.forget(rid, "mac")
            return self._reply(200, {})
    return H


def make_internal_handler(gate: Gate, token: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            pass

        def _reply(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _ok(self):
            return hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}")

        def do_GET(self):  # noqa: N802
            if not self._ok():
                return self._reply(401, {"error": "unauthorized"})
            if self.path == "/internal/runners":
                return self._reply(200, {"runners": gate.runners_view()})
            self._reply(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if not self._ok():
                return self._reply(401, {"error": "unauthorized"})
            if self.path != "/internal/call":
                return self._reply(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._reply(400, {"error": "invalid json"})
            self._reply(*gate.call(body if isinstance(body, dict) else {}))
    return H


def internal_token(home: str) -> str:
    """工場の中向けの合言葉（なければ作る）。dispatcher は同じボリュームを読み取り専用でマウントして読む。"""
    path = os.path.join(home, "internal.token")
    if not os.path.exists(path):
        os.makedirs(home, exist_ok=True)
        with open(path, "w", encoding="ascii") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o644)   # dispatcher のコンテナの利用者が読めるように（ボリュームはワーカーにはマウントしない）
    with open(path, encoding="ascii") as f:
        return f.read().strip()


def main():
    from contextlib import contextmanager

    import psycopg
    from psycopg.rows import dict_row

    @contextmanager
    def cursor():
        with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True) as conn, conn.cursor() as cur:
            yield cur

    cert, key, fp = ensure_cert(HOME)
    gate = Gate(HOME, cursor, fp)
    token = internal_token(HOME)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    pub = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("RUNNER_GATE_PORT", "8794"))), make_runner_handler(gate))
    pub.socket = ctx.wrap_socket(pub.socket, server_side=True, do_handshake_on_connect=False)   # 握手は受けた後の各スレッドで（遅い相手で受け口を止めない）
    internal = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("RUNNER_GATE_INTERNAL_PORT", "8795"))), make_internal_handler(gate, token))
    threading.Thread(target=internal.serve_forever, daemon=True).start()

    def sweep():   # 外す依頼を、窓口に来ていないランナーでも 10 分で片付ける（鍵を捨てる）
        while True:
            time.sleep(60)
            try:
                with cursor() as cur:
                    ids = runners.uninstall_pending(cur)
                for rid in ids:
                    if gate.clock() - gate.last_poll.get(rid, 0) > 600:
                        gate.forget(rid, "console")
            except Exception as e:   # noqa: BLE001  DB が一時的に落ちても窓口は止めない
                print(f"[runner-gate] sweep: {e}", file=sys.stderr)
    threading.Thread(target=sweep, daemon=True).start()
    print(f"[runner-gate] listening (fingerprint {fp[:16]}…)", file=sys.stderr)
    pub.serve_forever()


if __name__ == "__main__":
    main()
