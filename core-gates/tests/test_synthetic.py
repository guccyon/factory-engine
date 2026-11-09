"""合成の利用者（ADR 0043 の O4）: 中継（synthetic_proxy.py）の応答時間と仕込み、walk.yaml の形の検査、qa.run の synthetic"""
import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_runner  # noqa: F401,E402
import qa  # noqa: E402
import synthetic_proxy as sp  # noqa: E402
from test_qa import STORY, product  # noqa: E402

DEFS = qa.load_defs()


class Up(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/"):
            body, ctype = json.dumps({"ok": True, "path": self.path}).encode(), "application/json"
        elif self.path == "/go":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/home")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            body, ctype = b"<html><head><title>t</title></head><body><a href='/headquarters/bookings'>x</a></body></html>", "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def upstream():
    s = ThreadingHTTPServer(("127.0.0.1", 0), Up)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown()


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as r:
        return r.status, r.read()


def test_path_key_and_slow_rule():
    assert sp.path_key("/api/bookings/0b6f2a3e-1111-4222-8333-444455556666/events?x=1") == "/api/bookings/{id}/events"
    assert sp.path_key("/api/coaches/12/reviews") == "/api/coaches/{id}/reviews"
    f = {"slow": {"path": "/api/bookings", "method": "GET", "delay_ms": 30}}
    assert sp.slow_hit(f, "GET", "/api/bookings?student=1") == 30
    assert sp.slow_hit(f, "POST", "/api/bookings") == 0 and sp.slow_hit(f, "GET", "/api/slots") == 0 and sp.slow_hit(None, "GET", "/api/x") == 0
    assert b"display:none" in sp.inject_hide(b"<html><head></head></html>", "a.x") and sp.inject_hide(b"x", "") == b"x"


def test_relays_measure_delay_and_hide(upstream, tmp_path):
    env = {"env": "staging", "entries": [{"label": "管理", "port_env": "ADMIN_WEB_PORT", "url": upstream + "/"},
                                         {"label": "生徒", "port_env": "STUDENT_WEB_PORT", "url": upstream + "/"}]}
    friction = {"slow": {"path": "/api/bookings", "delay_ms": 120}, "hide": {"entry": "ADMIN_WEB_PORT", "selector": "a[href='/headquarters/bookings']"}}
    log = tmp_path / "relay.jsonl"
    with sp.Relays(env, friction, log) as relayed:
        admin, student = (e["url"].rstrip("/") for e in relayed["entries"])
        assert admin.startswith("http://127.0.0.1:") and admin != upstream
        assert b"data-synthetic-friction" in get(admin + "/")[1]
        assert b"data-synthetic-friction" not in get(student + "/")[1]   # 仕込みは指定した入口だけ
        for _ in range(2):
            get(admin + "/api/bookings?from=1")
        get(admin + "/api/slots")
        st, body = get(admin + "/go")   # 上流への転送先は中継の URL に書き換える
        assert st == 200 and b"<a" in body
    rows = sp.read_log(log)
    slow = [r for r in rows if r["path"] == "/api/bookings"]
    assert len(slow) == 2 and all(r["ms"] >= 120 and r["delayed_ms"] == 120 for r in slow)
    t = sp.timings(rows)
    assert t[0]["path"] == "/api/bookings" and t[0]["n"] == 2 and t[0]["p95"] >= 120 and t[0]["entry"] == "ADMIN_WEB_PORT"
    assert all(x["path"].startswith("/api/") for x in t)


def walk_doc(**kw):
    d = {"confidence": 0.7, "interpretation": "予約の一覧が遅い",
         "walks": [{"story_id": "coach-replies", "round": 1, "actor": "coach", "entry": "ADMIN_WEB_PORT", "found_entry": True,
                    "attempts_to_entry": 1, "reached_exit": True,
                    "steps": [{"n": 1, "did": "送った", "event": "message.sent", "waited": "none", "stuck": False}], "felt": "ふつう"}]}
    d.update(kw)
    return d


def test_validate_synthetic():
    sd = DEFS["synthetic"]
    plan = [{"story_id": "coach-replies", "rounds": 1}]
    ev, en = {"message.sent"}, {"ADMIN_WEB_PORT", "STUDENT_WEB_PORT"}
    assert qa.validate_synthetic(walk_doc(), sd, plan, ev, en) == []
    errs = qa.validate_synthetic(walk_doc(walks=[{**walk_doc()["walks"][0], "entry": "X", "found_entry": "yes",
                                                  "steps": [{"n": 1, "did": "a", "event": "nope", "waited": "slow", "stuck": 0}]}]), sd, plan, ev, en)
    assert any("entry" in e for e in errs) and any("found_entry" in e for e in errs) and any("nope" in e for e in errs)
    assert any("waited" in e for e in errs) and any("stuck" in e for e in errs)
    assert any("足りない" in e for e in qa.validate_synthetic(walk_doc(), sd, [{"story_id": "coach-replies", "rounds": 2}], ev, en))
    assert qa.walk_plan([STORY, {**STORY, "id": "b"}], {"focus": {"stories": ["b"]}}, sd) == [
        {"story_id": "coach-replies", "rounds": sd["walks_other"]}, {"story_id": "b", "rounds": sd["walks_focus"]}]


def test_run_synthetic_relays_and_returns_timings(tmp_path, monkeypatch, upstream):
    d = product(tmp_path)
    (d / "specs" / "analytics").mkdir(parents=True)
    (d / "specs" / "analytics" / "events.yaml").write_text(yaml.safe_dump({"events": [{"name": "message.sent"}]}), encoding="utf-8")
    port = int(upstream.rsplit(":", 1)[1])
    monkeypatch.setattr(qa, "env_entries", lambda repo_dir, env, host: {"env": env, "entries": [
        {"label": "管理", "port_env": "ADMIN_WEB_PORT", "url": f"http://127.0.0.1:{port}/"}]})
    calls = []

    def fake(step, text, max_turns, cwd, state_dir, mcp_config=None):
        calls.append(text)
        env = json.loads((Path(cwd) / "env.json").read_text(encoding="utf-8"))
        base = env["entries"][0]["url"].rstrip("/")
        assert str(port) not in base   # エージェントには中継の URL だけを渡す
        get(base + "/api/bookings")
        get(base + "/api/bookings")
        w = walk_doc()["walks"][0]
        (Path(cwd) / "out" / "walk.yaml").write_text(yaml.safe_dump(walk_doc(walks=[w, {**w, "round": 2}]), allow_unicode=True), encoding="utf-8")
        return sys.modules["agent_runner"].Result(output="", cost_usd=0.2, usage={}, model="m", num_turns=3)

    monkeypatch.setattr(sys.modules["agent_runner"], "run_agentic", fake)
    inp = {"mode": "synthetic", "release_id": "r1", "friction": {"slow": {"path": "/api/bookings", "delay_ms": 50}},
           "focus": {"stories": ["coach-replies"], "viewpoints": ["flow"]},
           "scenarios": [{"id": "R-1", "title": "予約が消える", "reproduce": ["開く", "戻る"], "expected": "残る"}]}
    res = qa.run(d, inp, tmp_path / "o", DEFS)
    assert res["ok"], res.get("errors")
    assert len(calls) == 1 and "合成の利用者" in calls[0] and "--check-synthetic" in calls[0] and "message.sent" in calls[0]
    assert "厚く見るストーリー: coach-replies" in calls[0] and "R-1" in calls[0]
    sy = res["synthetic"]
    assert sy["plan"] == [{"story_id": "coach-replies", "rounds": DEFS["synthetic"]["walks_focus"]}] and len(sy["walks"]) == 2
    assert sy["timings"][0]["path"] == "/api/bookings" and sy["timings"][0]["p95"] >= 50 and sy["requests"] == 2
    assert sy["planted"] == {"slow": {"path": "/api/bookings", "delay_ms": 50}}


def test_explore_prompt_carries_focus():
    text = qa.explore_prompt(DEFS, qa.charter_of(DEFS, "detour"), [STORY], {"env": "staging", "entries": []},
                             {"focus": {"stories": [], "viewpoints": ["authz"]}})
    assert "厚く見る観点: authz" in text
    assert qa.focus_text({}) == ""
