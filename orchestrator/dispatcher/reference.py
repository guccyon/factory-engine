#!/usr/bin/env python3
"""操業コンソールのヘルプの「API と MCP」「コマンド」の中身（GET /api/reference）。

手で写した説明は実装とずれるので、ここでは書かない。正本は次のコードの説明で、それをその場で読んで表にする:
- 操業コンソールの API: console.py の冒頭の説明
- 事業部の窓口の API: desk_api.py の冒頭の説明
- 窓口の MCP の道具: orchestrator/desk-mcp/factory_desk_mcp.py の TOOLS と冒頭の説明。
  MCP のサーバーはコンソールのイメージの外（ビルドの範囲の外）にあるので、写し（desk_mcp_tools.py）を置く（イメージには *.py を写すので .py にする）。
  写しは `python3 dispatcher/reference.py --sync-mcp` で作り直す。ずれるとテスト（tests/test_reference.py）が落ちる
- factoryctl: factoryctl.py の冒頭の説明（引数なし・知らないコマンドのときに出す使い方と同じ）

モジュールは読み込まない（読み込むと DB への接続などが動く）。ファイルを ast で開いて説明の文字列だけを取る。
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_SNAPSHOT = os.path.join(HERE, "desk_mcp_tools.py")
MCP_SOURCE = os.path.join(HERE, "..", "desk-mcp", "factory_desk_mcp.py")
FACTORYCTL_RUN = "docker exec factory-dispatcher-1 factoryctl"   # compose のプロジェクト名が factory のときのコンテナ名
METHOD = re.compile(r"^\s{1,4}(GET|POST|PUT|DELETE)\s+(.+?)(?:\s{2,}(.*))?$")   # パスと説明の間は 2 文字以上の空き（パスの中の <id か ref> は 1 文字）


def _module(path: str) -> ast.Module:
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


def docstring(path: str) -> str:
    return ast.get_docstring(_module(path), clean=False) or ""


def parse_api(doc: str) -> dict:
    """「  GET  /path  説明」の行を表の行にする。深く字下げした行は前の行の説明の続き。ほかの行は前置き・後書き"""
    lines = doc.strip("\n").splitlines()
    title, intro, rows, notes = lines[0].strip(), [], [], []
    for ln in lines[1:]:
        if not ln.strip():
            continue
        m = METHOD.match(ln)
        if m:
            rows.append({"method": m.group(1), "path": m.group(2), "desc": (m.group(3) or "").strip()})
        elif rows and ln.startswith(" " * 10):
            rows[-1]["desc"] = (rows[-1]["desc"] + " " + ln.strip()).strip()
        elif rows:
            notes.append(ln.strip())
        else:
            intro.append(ln.strip())
    return {"title": title, "intro": intro, "rows": rows, "notes": notes}


def parse_cli(doc: str) -> dict:
    """factoryctl の説明を、見出し（字下げなしの行）ごとのコマンドの表にする。「   # 説明」はコマンドの説明、# だけの行は前のコマンドの説明の続き"""
    lines = doc.strip("\n").splitlines()
    title, groups = lines[0].strip(), []   # 1 行目はそのまま最初のまとまりの見出し
    cur = {"title": title, "rows": [], "notes": []}
    for ln in lines[1:]:
        if not ln.strip():
            continue
        if not ln.startswith(" "):
            if cur["rows"] or cur["notes"]:
                groups.append(cur)
                cur = {"title": ln.strip(), "rows": [], "notes": []}
            else:   # 見出しが 2 行にわたる
                cur["title"] += ln.strip()
            continue
        s = ln.strip()
        if s.startswith("#") and cur["rows"]:
            cur["rows"][-1]["desc"] = (cur["rows"][-1]["desc"] + " " + s.lstrip("# ").strip()).strip()
        elif re.match(r"(factoryctl|docker|export)\b", s):
            parts = re.split(r"\s+#\s", s, maxsplit=1)   # 「  # 説明」。owner/repo#123 の # は前が空きでないので切らない
            cur["rows"].append({"cmd": parts[0].strip(), "desc": parts[1].strip() if len(parts) > 1 else ""})
        else:
            cur["notes"].append(s)
    groups.append(cur)
    return {"title": title, "groups": [g for g in groups if g["rows"] or g["notes"]]}


def mcp_from_source(path: str = MCP_SOURCE) -> dict:
    """MCP のサーバーのファイルから、道具の定義（TOOLS）と冒頭の説明の設定の部分を取る（モジュールは読み込まない）"""
    mod = _module(path)
    env = {}
    tools = server = None
    for node in mod.body:   # TOOLS は LEVEL などの名前を使うので、上から順に評価できる代入だけを評価する
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            try:
                val = eval(compile(ast.Expression(node.value), path, "eval"), {"__builtins__": {}, "False": False, "True": True}, env)   # noqa: S307 リポジトリの中のリテラルだけ
            except Exception:   # noqa: BLE001 関数呼び出しなどを含む代入は飛ばす
                continue
            env[name] = val
            if name == "TOOLS":
                tools = val
            elif name == "SERVER":
                server = val
    doc = ast.get_docstring(mod, clean=False) or ""
    setup = doc.split("道具（", 1)[0].strip()   # 道具の一覧は TOOLS から作るので、説明はその前（設定と登録の例）まで
    return {"server": server, "setup": setup, "tools": tools or []}


def mcp_snapshot() -> dict:
    import desk_mcp_tools
    return desk_mcp_tools.SNAPSHOT


def build() -> dict:
    """ヘルプの 2 つのタブの中身。console は GET /api/reference でこれを返す"""
    return {
        "api": [
            {"id": "console", **parse_api(docstring(os.path.join(HERE, "console.py")))},
            {"id": "desk", **parse_api(docstring(os.path.join(HERE, "desk_api.py")))},
        ],
        "mcp": mcp_snapshot(),
        "cli": {"run": FACTORYCTL_RUN, **parse_cli(docstring(os.path.join(HERE, "factoryctl.py")))},
    }


def snapshot_source(snap: dict) -> str:
    body = json.dumps(snap, ensure_ascii=False, indent=1)
    if "'''" in body:
        raise ValueError("MCP の説明に ''' がある")
    return ('"""窓口の MCP の道具の写し（ヘルプの「API と MCP」用）。手で直さない。\n\n'
            '正本は orchestrator/desk-mcp/factory_desk_mcp.py。直したら python3 dispatcher/reference.py --sync-mcp で作り直す\n"""\n'
            f"import json\n\nSNAPSHOT = json.loads(r'''{body}''')\n")


def sync_mcp() -> None:
    with open(MCP_SNAPSHOT, "w", encoding="utf-8") as f:
        f.write(snapshot_source(mcp_from_source()))


if __name__ == "__main__":
    if "--sync-mcp" in sys.argv:
        sync_mcp()
        print(f"wrote {os.path.relpath(MCP_SNAPSHOT)}")
    else:
        print(json.dumps(build(), ensure_ascii=False, indent=1))
