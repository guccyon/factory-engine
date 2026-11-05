"""ランナーの窓口（runner_gate.py、ADR 0033）を、本物の TLS と本物のランナー（macrunner/runner.py）の側の話し方で通す。

ペアリング（両側の番号が同じ・コンソールの許可と Mac の y の両方が要る）、署名のない・違う呼び出しを断る、
工場の中から道具を呼ぶとランナーが取りに来てこなす、外す依頼で鍵が消える、別の証明書の窓口とは話さない。
空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）。openssl が要る。
"""
import json
import os
import shutil
import ssl
import sys
import threading
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "macrunner"))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL or not shutil.which("openssl"), reason="TEST_DATABASE_URL か openssl がない")

if URL:
    import macrunner_client
    import psycopg
    import runner as mac  # noqa: E402  Mac 側（標準ライブラリだけ）
    import runner_gate
    import runners
    from psycopg.rows import dict_row


@contextmanager
def cursor():
    with psycopg.connect(URL, row_factory=dict_row, autocommit=True) as conn, conn.cursor() as c:
        yield c


def serve(handler, tls=None):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    if tls:
        httpd.socket = tls.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture
def gate(tmp_path, monkeypatch):
    with cursor() as c:
        c.execute("delete from events where name like 'runner.%'")
    monkeypatch.setattr(runner_gate, "WAIT_SEC", 1)
    home = str(tmp_path / "gate")
    cert, key, fp = runner_gate.ensure_cert(home)
    g = runner_gate.Gate(home, cursor, fp)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    pub = serve(runner_gate.make_runner_handler(g), ctx)
    token = runner_gate.internal_token(home)
    internal = serve(runner_gate.make_internal_handler(g, token))
    tf = tmp_path / "token"
    tf.write_text(token)
    env = {"RUNNER_GATE_URL": f"http://127.0.0.1:{internal.server_address[1]}", "RUNNER_GATE_TOKEN_FILE": str(tf)}
    monkeypatch.setenv("MAC_RUNNER_HOME", str(tmp_path / "mac"))
    monkeypatch.setenv("MAC_RUNNER_CRED_FILE", str(tmp_path / "mac" / "cred.json"))
    monkeypatch.setenv("MAC_RUNNER_LOG_DIR", str(tmp_path / "mac" / "logs"))
    monkeypatch.setenv("MAC_RUNNER_GATE", f"https://127.0.0.1:{pub.server_address[1]}")
    monkeypatch.setattr(mac, "hello_info", lambda cfg: {"name": "mac1", "version": mac.VERSION})
    yield g, env, mac.config()
    pub.shutdown()
    internal.shutdown()


def approve_when_requested(approve=True):
    with cursor() as c:
        old = {p["pair_id"] for p in runners.pending_pairs(c)}

    def run():
        for _ in range(100):
            with cursor() as c:
                ps = [p for p in runners.pending_pairs(c) if p["pair_id"] not in old]
                if ps:
                    runners.decide_pair(c, ps[0]["pair_id"], approve, "console:test")
                    return
            threading.Event().wait(0.1)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_pairing_shows_same_code_and_needs_both_sides(gate):
    g, env, cfg = gate
    said = []
    approve_when_requested()
    cred = mac.pair(cfg, ask=lambda q: "y", out=said.append)
    assert cred and mac.load_cred(cfg) == cred
    assert cred["fingerprint"] == g.fp and cred["runner_id"] in g.keys
    with cursor() as c:
        c.execute("select payload from events where name='runner.pair_requested'")
        code = c.fetchone()["payload"]["code"]
        assert [r["id"] for r in runners.view(c)] == [cred["runner_id"]]
    assert any(code in s for s in said)   # Mac の画面とコンソールに同じ番号
    st = os.stat(cfg["cred_file"]).st_mode & 0o777
    assert st == 0o600


def test_pairing_refused_without_mac_yes_or_with_console_reject(gate):
    g, env, cfg = gate
    assert mac.pair(cfg, ask=lambda q: "n", out=lambda s: None) is None
    assert g.keys == {} and mac.load_cred(cfg) is None
    approve_when_requested(approve=False)
    with pytest.raises(mac.GateError, match="拒否"):
        mac.pair(cfg, ask=lambda q: "y", out=lambda s: None)
    assert g.keys == {}


def test_reveal_must_match_commit(gate):
    g, env, cfg = gate
    gw = mac.Gate(cfg["gate"])
    start = gw.post("/pair/start", {"commit": "0" * 64, "info": {}})
    with pytest.raises(mac.GateError) as e:
        gw.post("/pair/reveal", {"pair_id": start["pair_id"], "runner_nonce": "1" * 32})
    assert e.value.status == 404


def paired(gate):
    g, env, cfg = gate
    approve_when_requested()
    return mac.pair(cfg, ask=lambda q: "y", out=lambda s: None)


def test_unsigned_or_wrong_key_is_refused(gate):
    g, env, cfg = gate
    cred = paired(gate)
    gw = mac.Gate(cfg["gate"], cred["fingerprint"])
    with pytest.raises(mac.GateError) as e:
        gw.post("/poll", {})
    assert e.value.status == 401
    with pytest.raises(mac.GateError) as e:
        gw.post("/poll", {}, {**cred, "key": "00" * 32})
    assert e.value.status == 401


def test_other_certificate_is_not_trusted(gate):
    g, env, cfg = gate
    cred = paired(gate)
    with pytest.raises(mac.GateError, match="証明書がペアリングのときと違う"):
        mac.Gate(cfg["gate"], "ab" * 32).post("/poll", {}, cred)


def test_internal_call_is_carried_by_runner_poll(gate, monkeypatch):
    g, env, cfg = gate
    cred = paired(gate)
    devices = [{"udid": "u", "name": "iPhone 17", "state": "Booted", "runtime": "iOS-26-1"}]

    class FakeRunner:
        def call(self, name, args):
            if name != "sim_devices":
                raise mac.ToolError(f"{name} という道具はない")
            return [{"type": "text", "text": json.dumps(devices)}]
    gw = mac.Gate(cfg["gate"], cred["fingerprint"])
    stop = threading.Event()
    logs = []

    def loop():
        while not stop.is_set():
            mac.work_once(cfg, FakeRunner(), cred, gw, logs.append)
    threading.Thread(target=loop, daemon=True).start()
    for _ in range(50):
        if g.online():
            break
        threading.Event().wait(0.1)
    lines, code = macrunner_client.check(env)
    assert code == 0, lines
    assert any("端末" in s and "1 台（起動中 1）" in s for s in lines)
    res = macrunner_client.call("shell", {"cmd": "id"}, env=env, timeout=10)
    assert res["isError"] and "という道具はない" in res["content"][0]["text"]
    assert [r["online"] for r in macrunner_client.runners(env)] == [True]

    req = urllib.request.Request(env["RUNNER_GATE_URL"] + "/internal/runners", headers={"Authorization": "Bearer x"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 401
    stop.set()


def test_console_uninstall_drops_key(gate):
    g, env, cfg = gate
    cred = paired(gate)
    with cursor() as c:
        assert runners.request_uninstall(c, cred["runner_id"], "console:test")[0]
    gw = mac.Gate(cfg["gate"], cred["fingerprint"])
    assert mac.work_once(cfg, None, cred, gw, lambda r: None) == "uninstall"
    assert cred["runner_id"] not in g.keys
    with cursor() as c:
        assert runners.view(c) == []
    with pytest.raises(mac.GateError):
        gw.post("/poll", {}, cred)   # 鍵はもう効かない


def test_bye_from_mac_drops_both_keys(gate):
    g, env, cfg = gate
    cred = paired(gate)
    mac.bye(cfg)
    assert mac.load_cred(cfg) is None and cred["runner_id"] not in g.keys


def test_no_runner_means_503(gate):
    g, env, cfg = gate
    lines, code = macrunner_client.check(env)
    assert code == 1 and "来ているランナーがない" in lines[-1]
    with pytest.raises(RuntimeError, match="503"):
        macrunner_client.call("sim_devices", env=env, timeout=5)


def test_sas_is_the_same_on_both_sides():
    rn, gn, fp = "01" * 16, "ab" * 16, "cd" * 32
    assert mac.sas(rn, gn, fp) == runner_gate.sas(rn, gn, fp)
    assert len(runner_gate.sas(rn, gn, fp)) == 7
