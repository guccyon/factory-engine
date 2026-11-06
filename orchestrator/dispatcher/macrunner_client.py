"""工場の側から Mac ランナー（ADR 0028 の決定 3、ADR 0033）を呼ぶ。ランナーの窓口（runner_gate.py）の中向けの口を使う。

- check: 窓口に届くか、登録済みのランナーが窓口に来ているか、道具を 1 つ呼べるか（factoryctl runner check）
- call: 道具を 1 つ呼んで結果を待つ（one_orchestrator の中継が使う）
接続先は RUNNER_GATE_URL（既定 http://runner-gate:8795）、合言葉は RUNNER_GATE_TOKEN_FILE（窓口のボリュームを読み取り専用でマウントしたもの）。
合言葉は dispatcher の中だけで使い、ジョブのワーカーには渡さない（ADR 0033）。
"""
import json
import os
import urllib.error
import urllib.request

DEFAULT_URL = "http://runner-gate:8795"
DEFAULT_TOKEN_FILE = "/run/runner-gate/internal.token"


def settings(env=os.environ):
    url = (env.get("RUNNER_GATE_URL") or DEFAULT_URL).rstrip("/")
    try:
        with open(env.get("RUNNER_GATE_TOKEN_FILE") or DEFAULT_TOKEN_FILE, encoding="ascii") as f:
            token = f.read().strip()
    except OSError:
        token = ""
    return url, token


def _req(url, token, method, path, body=None, timeout=30, opener=urllib.request.urlopen):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with opener(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": str(e)}


def runners(env=os.environ, opener=urllib.request.urlopen) -> list[dict]:
    url, token = settings(env)
    code, body = _req(url, token, "GET", "/internal/runners", opener=opener)
    if code != 200:
        raise RuntimeError(f"/internal/runners: {code} {body.get('error')}")
    return body.get("runners", [])


def call(tool: str, arguments: dict | None = None, runner: str = "any", timeout: int = 120,
         env=os.environ, opener=urllib.request.urlopen) -> dict:
    """{runner_id, content, isError}。届かない・ランナーがいない・時間切れは RuntimeError。"""
    url, token = settings(env)
    code, body = _req(url, token, "POST", "/internal/call",
                      {"runner": runner, "tool": tool, "arguments": arguments or {}, "timeout": timeout},
                      timeout=timeout + 30, opener=opener)
    if code != 200:
        raise RuntimeError(f"{tool}: {code} {body.get('error')}")
    return body


def check(env=os.environ, opener=urllib.request.urlopen):
    """(行の一覧, 終了コード)。どこで止まったかを 1 行ずつ出す。"""
    url, token = settings(env)
    lines = [f"窓口: {url}"]
    if not token:
        return lines + ["窓口の合言葉が読めない（runner-gate が一度も上がっていないか、ボリュームのマウントがない）"], 1
    try:
        rs = runners(env, opener)
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
        return lines + [f"窓口: 答えない（{e}）。docker compose ps runner-gate"], 1
    live = [r for r in rs if r.get("online")]
    lines.append(f"ペアリング済み: {len(rs)} 台（窓口に来ている {len(live)} 台）" + "".join(f"\n  - {r.get('name')} {r['id'][:8]} {'来ている' if r.get('online') else '来ていない'}" for r in rs))
    if not live:
        return lines + ["窓口に来ているランナーがない（Mac で scripts/macrunner.sh status。ペアリングがまだなら install）"], 1
    try:
        res = call("sim_devices", {}, timeout=60, env=env, opener=opener)
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
        return lines + [f"道具: 失敗（{e}）"], 1
    text = (res.get("content") or [{}])[0].get("text", "")
    if res.get("isError"):
        return lines + [f"端末: 見えない（{text[:300]}）"], 1
    devs = json.loads(text)
    booted = [d for d in devs if d["state"] == "Booted"]
    lines.append(f"端末（{res['runner_id'][:8]}）: {len(devs)} 台（起動中 {len(booted)}）" + (f" 例 {devs[0]['name']} / {devs[0]['runtime']}" if devs else ""))
    return lines, 0 if devs else 1
