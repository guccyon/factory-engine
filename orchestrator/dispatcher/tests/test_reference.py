"""ヘルプのリファレンス（reference.py）: コードの説明から表を作れること、説明の書き漏れ・MCP の写しのずれを見つけること。DB は使わない"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import reference  # noqa: E402

HERE = os.path.join(os.path.dirname(__file__), "..")


def src(name: str) -> str:
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def test_parse_api_rows_and_continuations():
    doc = """題

- 前置き
  GET  /api/a                 説明 A
  POST /api/b/<id か ref>/x | y   {"k"} 説明 B
                              続き
- 後書き
"""
    a = reference.parse_api(doc)
    assert a["title"] == "題" and a["intro"] == ["- 前置き"] and a["notes"] == ["- 後書き"]
    assert a["rows"] == [{"method": "GET", "path": "/api/a", "desc": "説明 A"},
                         {"method": "POST", "path": "/api/b/<id か ref>/x | y", "desc": '{"k"} 説明 B 続き'}]


def test_parse_cli_groups():
    doc = """題

  factoryctl a [--x]      # 説明 a
  factoryctl b <owner/repo#123>
      # b の説明
見出し 1 行目
見出し 2 行目:
  factoryctl c   # 説明 c
  （注）
"""
    c = reference.parse_cli(doc)
    assert [g["title"] for g in c["groups"]] == ["題", "見出し 1 行目見出し 2 行目:"]
    assert c["groups"][0]["rows"] == [{"cmd": "factoryctl a [--x]", "desc": "説明 a"}, {"cmd": "factoryctl b <owner/repo#123>", "desc": "b の説明"}]
    assert c["groups"][1]["notes"] == ["（注）"]


def test_build_reads_the_real_sources():
    r = reference.build()
    console, desk = r["api"]
    assert any(x["path"] == "/api/state" for x in console["rows"]) and any(x["path"] == "/api/reference" for x in console["rows"])
    assert any(x["path"] == "/api/desk/inbox/<key>/answer" for x in desk["rows"])
    names = {t["name"] for t in r["mcp"]["tools"]}
    assert {"inbox_read", "inbox_answer", "desk_open"} <= names
    cmds = [x["cmd"] for g in r["cli"]["groups"] for x in g["rows"]]
    assert len(cmds) > 60 and any(c.startswith("factoryctl line scale") for c in cmds)
    assert all(x["desc"] for g in r["cli"]["groups"] for x in g["rows"]), "説明のないコマンドがある（factoryctl.py の冒頭に「  # 説明」を足す）"


def test_mcp_snapshot_matches_server():
    """MCP の道具を直したら写しを作り直す: python3 dispatcher/reference.py --sync-mcp"""
    assert reference.mcp_snapshot() == reference.mcp_from_source()
    with open(reference.MCP_SNAPSHOT, encoding="utf-8") as f:
        assert f.read() == reference.snapshot_source(reference.mcp_from_source())


def test_factoryctl_commands_are_documented():
    """main で受けるコマンド（とサブコマンド）が使い方の説明に書いてあること。書き漏れるとヘルプに出ない"""
    code = src("factoryctl.py")
    doc = reference.docstring(os.path.join(HERE, "factoryctl.py"))
    main = code[code.index("def main(argv):"):]
    top = set(re.findall(r'cmd == "([\w-]+)"', main)) | set(re.findall(r'argv\[1\] == "([\w-]+)"', main))
    for t in re.findall(r"cmd in \(([^)]*)\)", main):
        top |= set(re.findall(r'"([\w-]+)"', t))
    missing = [c for c in sorted(top) if f"factoryctl {c}" not in doc]
    assert not missing, missing
    pairs = re.findall(r'cmd == "([\w-]+)" and len\(argv\) > \d and argv\[2\] == "([\w-]+)"', main)
    pairs += re.findall(r'argv\[1\] == "([\w-]+)" and argv\[2\] == "([\w-]+)"', main)
    missing = [f"{a} {b}" for a, b in pairs if f"factoryctl {a} {b}" not in doc]
    assert not missing, missing


def test_console_routes_are_documented():
    """console.py で受ける API のパスが冒頭の説明に書いてあること"""
    code = src("console.py")
    doc = reference.docstring(os.path.join(HERE, "console.py"))
    paths = set(re.findall(r'path == "(/api/[^"]+)"', code))
    for t in re.findall(r"path in \(([^)]*)\)", code):
        paths |= set(re.findall(r'"(/api/[^"]+)"', t))

    def documented(p: str) -> bool:   # 「/api/factory/drain | resume」のように 1 行にまとめた書き方も認める
        parent, last = p.rsplit("/", 1)
        return p in doc or re.search(re.escape(parent) + r"/[\w-]+ \| " + re.escape(last) + r"\b", doc) is not None
    missing = [p for p in sorted(paths) if not documented(p)]
    assert not missing, missing
    for prefix in re.findall(r're\.fullmatch\(r"(/api/[a-z/-]+)/', code):   # 正規表現の経路は固定の頭が説明にあること
        assert prefix + "/" in doc, prefix


def test_desk_routes_are_documented():
    code = src("desk_api.py")
    doc = reference.docstring(os.path.join(HERE, "desk_api.py"))
    for res in set(re.findall(r'rest\[:1\] == \["([\w-]+)"\]', code)) | set(re.findall(r'rest == \["([\w-]+)"\]', code)):
        assert f"/api/desk/{res}" in doc, res
