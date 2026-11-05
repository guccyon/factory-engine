"""工場の側から Mac ランナー（ADR 0028 の決定 3、orchestrator/macrunner）を呼ぶ。

- check: 生きているか、MCP で話せるか、端末が見えるか（factoryctl runner check）
- mcp_config: QA のワーカーの claude に --mcp-config で渡す設定（one_orchestrator で使う）
接続先は MAC_RUNNER_URL（既定 http://host.docker.internal:8793）、トークンは MAC_RUNNER_TOKEN（.env。ホストで scripts/macrunner.sh token）。
"""
import json
import os
import urllib.error
import urllib.request

DEFAULT_URL = "http://host.docker.internal:8793"


def settings(env=os.environ):
    return (env.get("MAC_RUNNER_URL") or DEFAULT_URL).rstrip("/"), env.get("MAC_RUNNER_TOKEN", "")


def mcp_config(env=os.environ):
    """claude --mcp-config に渡す JSON。トークンがなければ None（Mac ランナーを渡さない）。"""
    url, token = settings(env)
    if not token:
        return None
    return {"mcpServers": {"macrunner": {"type": "http", "url": url + "/mcp",
                                         "headers": {"Authorization": f"Bearer {token}"}}}}


def rpc(url, token, method, params=None, mid=1, timeout=30, opener=urllib.request.urlopen):
    body = json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(url + "/mcp", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                                          "Authorization": f"Bearer {token}"})
    with opener(req, timeout=timeout) as r:
        msg = json.loads(r.read())
    if "error" in msg:
        raise RuntimeError(f"{method}: {msg['error'].get('message')}")
    return msg["result"]


def check(env=os.environ, opener=urllib.request.urlopen):
    """(行の一覧, 終了コード)。どこで止まったかを 1 行ずつ出す。"""
    url, token = settings(env)
    lines = [f"接続先: {url}"]
    try:
        with opener(url + "/health", timeout=10) as r:
            h = json.loads(r.read())
        lines.append(f"health: ok（版 {h.get('version')}）")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return lines + [f"health: 答えない（{e}）。ホストで scripts/macrunner.sh status"], 1
    if not token:
        return lines + ["MAC_RUNNER_TOKEN がない（.env に scripts/macrunner.sh token の値を入れる）"], 1
    try:
        init = rpc(url, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "factoryctl", "version": "1"}}, opener=opener)
        lines.append(f"MCP: {init['serverInfo']['name']} {init['serverInfo']['version']}（{init['protocolVersion']}）")
        tools = [t["name"] for t in rpc(url, token, "tools/list", mid=2, opener=opener)["tools"]]
        lines.append(f"道具: {len(tools)} 個（{', '.join(tools)}）")
        res = rpc(url, token, "tools/call", {"name": "sim_devices", "arguments": {}}, mid=3, opener=opener)
    except urllib.error.HTTPError as e:
        return lines + [f"MCP: {e.code}（401 ならトークンが違う）"], 1
    except (urllib.error.URLError, OSError, ValueError, RuntimeError, KeyError) as e:
        return lines + [f"MCP: 失敗（{e}）"], 1
    if res.get("isError"):
        return lines + [f"端末: 見えない（{res['content'][0]['text'][:300]}）"], 1
    devs = json.loads(res["content"][0]["text"])
    booted = [d for d in devs if d["state"] == "Booted"]
    lines.append(f"端末: {len(devs)} 台（起動中 {len(booted)}）" + (f" 例 {devs[0]['name']} / {devs[0]['runtime']}" if devs else ""))
    return lines, 0 if devs else 1
