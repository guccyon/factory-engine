#!/usr/bin/env python3
"""QA 役（ADR 0028 の決定 2。P5a の qa_verdict）。触れる環境（Gen1 は staging を兼ねる）に、製品の specs/stories/ のユーザーストーリーを
利用者として流し、ストーリーごとに Verdict（pass・hold・blocked、確信度、根拠）を返す。形は definitions/qa.yaml。

- エージェントは作業ディレクトリ（一時）で動く。製品のチェックアウトは product/ に置き、読むだけにさせる
- 環境の入口は env.json（画面の URL、ログインの手がかり）。画面はヘッドレスの Chromium を playwright-core で動かす
- 成果物は out/verdicts.yaml（ストーリーの数だけ）と out/*.png（根拠のスクリーンショット）
- Verdict の形を決定論で確かめ、崩れていれば 1 回だけ同じ会話で直させる
- 製品のリポジトリにも Issue にも書かない。Defect の起票と固定 Issue のまとめは dispatcher（qa_line.py）

使い方:
  core-gates/qa.py --repo-dir /work/product --input input.json --out-dir /work/qa --out qa.json
  core-gates/qa.py --check out/verdicts.yaml --repo-dir /work/product   # 形だけ確かめる（エージェントが自分で使う）

依存: python3、PyYAML、chromium（CHROMIUM_BIN）、playwright-core（NODE_PATH）、agent_runner（工程 qa）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DEFS = Path(os.environ.get("QA_DEFINITIONS") or HERE.parent / "definitions" / "qa.yaml")
PNG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}\.png$")
PNG_MAX_BYTES = 600_000
PNG_MAX = 16


def load_defs(path: Path = DEFS) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 入力
def load_stories(repo_dir: Path, only: list[str] | None = None) -> list[dict]:
    """specs/stories/ の active なユーザーストーリー。only を渡すとその id だけ（並びは only の順）"""
    out = {}
    d = repo_dir / "specs" / "stories"
    for p in sorted(d.glob("*.yaml")) if d.is_dir() else []:
        try:
            s = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if isinstance(s, dict) and s.get("type") == "UserStory" and (s.get("status") or "active") == "active":
            out[s["id"]] = s
    if only:
        return [out[i] for i in only if i in out]
    return list(out.values())


def env_entries(repo_dir: Path, env: str, host: str) -> dict:
    """製品の .factory-config/ship.yaml から、その環境の画面の入口（ワーカーから届く URL）を作る。
    画面は同じオリジンの /api/* を backend に中継する前提（rally の deploy/compose.yaml）。backend の口は BIND_HOST のままなので渡さない"""
    p = repo_dir / ".factory-config" / "ship.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else {}
    ports = (((cfg or {}).get("environments") or {}).get(env) or {}).get("env") or {}
    entries = []
    for x in (((cfg or {}).get("touch") or {}).get("entries") or []):
        port = ports.get(x.get("port_env"))
        if port:
            entries.append({"label": str(x.get("label") or x.get("port_env")), "port_env": x.get("port_env"),
                            "url": f"http://{host}:{int(port)}{x.get('path') or '/'}"})
    return {"env": env, "entries": entries}


# ------------------------------------------------------------------ 形の検査
def validate(doc, stories: list[dict], defs: dict, out_dir: Path | None = None) -> list[str]:
    if not isinstance(doc, dict) or not isinstance(doc.get("verdicts"), list):
        return ["verdicts.yaml の最上位は verdicts: [...]"]
    errs = []
    decisions = set(defs["decisions"])
    severities = set(defs["severities"])
    viewpoints = set(defs["viewpoints"])
    want = {s["id"]: s for s in stories}
    seen = set()
    for i, v in enumerate(doc["verdicts"]):
        at = f"verdicts[{i}]"
        if not isinstance(v, dict):
            errs.append(f"{at} が対応表でない")
            continue
        for k in defs["verdict"]["required"]:
            if v.get(k) in (None, "", []) and not (k == "evidence" and v.get("decision") == "blocked"):
                errs.append(f"{at}.{k} がない")
        sid = v.get("story_id")
        if sid not in want:
            errs.append(f"{at}.story_id {sid!r} は流したストーリーにない")
            continue
        if sid in seen:
            errs.append(f"{at}.story_id {sid} が 2 回ある")
        seen.add(sid)
        if v.get("decision") not in decisions:
            errs.append(f"{at}.decision は {'・'.join(sorted(decisions))} のどれか")
        c = v.get("confidence")
        if not isinstance(c, (int, float)) or isinstance(c, bool) or not 0 <= c <= 1:
            errs.append(f"{at}.confidence は 0〜1 の数")
        steps = v.get("steps") or []
        if not isinstance(steps, list) or len(steps) != len(want[sid].get("steps") or []):
            errs.append(f"{at}.steps はストーリーの steps と同じ {len(want[sid].get('steps') or [])} 歩")
        else:
            for j, st in enumerate(steps):
                if not isinstance(st, dict) or st.get("result") not in ("ok", "ng", "skipped"):
                    errs.append(f"{at}.steps[{j}].result は ok・ng・skipped のどれか")
        defects = v.get("defects") or []
        if v.get("decision") == "hold" and not defects:
            errs.append(f"{at} は hold なので defects が 1 件以上要る")
        for j, d in enumerate(defects if isinstance(defects, list) else []):
            if not isinstance(d, dict) or not d.get("title") or not d.get("actual"):
                errs.append(f"{at}.defects[{j}] に title と actual が要る")
                continue
            if d.get("severity") not in severities:
                errs.append(f"{at}.defects[{j}].severity は {'・'.join(sorted(severities))} のどれか")
            if d.get("viewpoint") not in viewpoints:
                errs.append(f"{at}.defects[{j}].viewpoint は {'・'.join(sorted(viewpoints))} のどれか")
        for j, f in enumerate(v.get("flags") or []):
            if not isinstance(f, dict) or f.get("viewpoint") not in ("aesthetic", "tone") or not f.get("note"):
                errs.append(f"{at}.flags[{j}] は viewpoint（aesthetic・tone）と note")
        if out_dir is not None:
            for e in v.get("evidence") or []:
                m = re.search(r"([A-Za-z0-9][A-Za-z0-9_-]{0,47}\.png)", str(e))
                if m and not (out_dir / m.group(1)).is_file():
                    errs.append(f"{at}.evidence のスクリーンショット {m.group(1)} が out/ にない")
    missing = [s for s in want if s not in seen]
    if missing:
        errs.append(f"判定のないストーリー: {', '.join(missing)}")
    return errs


def screenshots(out_dir: Path) -> list[str]:
    """dispatcher に渡すスクリーンショット（名前の規則に合い、大きすぎないもの。多ければ先頭から）"""
    names = [p.name for p in sorted(out_dir.glob("*.png")) if PNG_RE.match(p.name) and p.stat().st_size <= PNG_MAX_BYTES]
    return names[:PNG_MAX]


# ------------------------------------------------------------------ Mac ランナー（one_orchestrator）
def relay_config(root: Path, env=os.environ) -> str | None:
    """dispatcher がこのジョブに Mac ランナーへの中継を渡していれば、MCP の設定を書いてパスを返す。合言葉はこのジョブの間だけ有効"""
    url, token = env.get("RUNNER_RELAY_URL"), env.get("RUNNER_RELAY_TOKEN")
    if not url or not token:
        return None
    p = root / ".mcp-relay.json"
    p.write_text(json.dumps({"mcpServers": {"macrunner": {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}}}), encoding="utf-8")
    p.chmod(0o600)
    return str(p)


MOBILE = """
## iOS シミュレーター（Mac ランナー）
mcp__macrunner__* の道具で、Mac の iOS シミュレーターを動かせます（この環境の画面と同じ staging のサーバーに繋ぎます）。
- 生徒アプリ（student-app）のストーリーは、まず iOS で流す。sim_devices → sim_boot → app_build（app: guccyon/rally:apps/student-app、{ref}
  dart_defines: {{"RALLY_API_URL": "{api}"}}）→ build_status で built まで待つ → app_install → app_launch → ui_describe / ui_tap / ui_text / screenshot
- ビルドに 10 分より長くかかる・失敗する・ランナーがいないときは、Web 版（env.json の生徒アプリ）で流し、interpretation にそう書く
- screenshot の画像は根拠として out/ に保存してよい（ファイル名の決まりは同じ）
- Mac のシミュレーターから見た staging のサーバーは {api}（Mac の中なので 127.0.0.1）
"""


def mobile_ref(inp: dict) -> str:
    """app_build の ref。環境に出ている版のコミット（release.built）にそろえる。分からなければ main で、そう書かせる"""
    c = inp.get("commit")
    if c:
        return f"ref: {c}（この環境に出ている版 {inp.get('release_id') or ''} のコミット。ほかの ref にしない）、"
    return "ref は省く（環境の版のコミットが分からないので main。interpretation に「iOS は main でビルドした」と書く）、"


# ------------------------------------------------------------------ プロンプト
def prompt(defs: dict, stories: list[dict], env: dict, inp: dict, mobile: bool = False) -> str:
    accounts = ((inp.get("touchable") or {}).get("accounts") or [])
    ex = {"verdicts": [{
        "story_id": stories[0]["id"] if stories else "<id>", "decision": "hold", "confidence": 0.8,
        "steps": [{"n": 1, "result": "ok", "saw": "実際に見えたもの"}, {"n": 2, "result": "ng", "saw": "..."}],
        "evidence": ["01-chat-list.png", "一覧に「まだメッセージはありません」と出たまま"],
        "interpretation": "根拠をどう読んで決めたか",
        "defects": [{"title": "利用者の言葉で 1 行", "step": 2, "must_not": "当たった must_not（なければ null）", "viewpoint": "flow",
                     "severity": "s2", "reproduce": ["手順 1", "手順 2"], "expected": "ストーリーの sees", "actual": "見えたもの",
                     "known_gap": False}],
        "flags": [{"viewpoint": "tone", "where": "画面と場所", "note": "人に見てほしい違和感"}]}]}
    return f"""あなたはソフトウェア工場の QA 役です。QA の定義は「プロダクトの利用者（エンドユーザー、カスタマーサポート、運営担当）にとって使いやすいシステムであるかの品質を担保する」です。
機能の正しさより「利用者の流れ」を確かめます。下のユーザーストーリーを 1 本ずつ、その利用者になったつもりで、触れる環境の画面で流してください。

## 作業の場所
- 作業ディレクトリ（いまの場所）。製品のチェックアウトは product/（読むだけ。書き換えない・コミットしない）
- ストーリーの正本は product/specs/stories/。利用者の種類は product/specs/stories/actors.yaml、用語は product/specs/glossary.yaml
- 環境の入口と、ログインの手がかりは env.json。ここに書いた URL 以外の環境（本番など）には繋がない
- 成果物は out/ に置く: out/verdicts.yaml（下の形）と、根拠のスクリーンショット out/<2 桁の番号>-<短い名前>.png（1 枚 600KB まで、全部で 16 枚まで）

## 道具
- 画面はヘッドレスの Chromium を playwright-core で動かす。node で `const {{ chromium }} = require('playwright-core'); const b = await chromium.launch({{ executablePath: process.env.CHROMIUM_BIN, args: ['--no-sandbox'] }});`
  （NODE_PATH は設定済み）。スマートフォンの利用者（生徒アプリ）は幅 390・高さ 844 で開く
- 画面が中継する API（同じオリジンの /api/*）を curl で直接見てもよい。ただし判定は「利用者に見えるもの」で決める
- 別の利用者が絡む流れ（生徒が送り、コーチが受ける）は、ブラウザの文脈を 2 つ開いて交互に操作する
- データは、ストーリーの preconditions を満たすために画面か API で作ってよい（合成シードの環境なので消えても困らない）。他のストーリーの前提を壊さない
- 時間をまたぐ手順（翌日・再起動のあと）は、ここではサーバーの再起動も時計の進めもできない。その歩は skipped にし、
  代わりに「ログインし直し・再読み込みで残るか」までを確かめて、ストーリー全体の decision は exit まで確かめられた範囲で決める
  （exit そのものが時間をまたぐなら blocked にして、何が足りないかを interpretation に書く）

{MOBILE.format(api=inp.get("mobile_api_url") or defs["worker"]["mobile_api_url"], ref=mobile_ref(inp)) if mobile else ""}
## 判定の決まり（definitions/qa.yaml）
- pass: {defs['decisions']['pass']}
- hold: {defs['decisions']['hold']}
- blocked: {defs['decisions']['blocked']}
- must_not は、別の利用者として（他人のトークン・他人のスレッドの id で）実際に試して確かめる。試せなかったものは interpretation に書く
- ストーリーの known_gaps に書いてある食い違いに当たったら、Defect に known_gap: true を付ける（hold のままでよい）
- 見た目（色・余白・書体）と言葉の温度は判定しない。気になったら flags に書く（viewpoint は aesthetic か tone）
- 観点: {json.dumps(defs['viewpoints'], ensure_ascii=False)}
- 重要度: {json.dumps(defs['severities'], ensure_ascii=False)}
- 決めきれないときは確信度を下げる。推測で pass にしない

## env.json の要点
{json.dumps(env, ensure_ascii=False, indent=1)}
ログインの手がかり（モックのトークン。画面のログインに使う）:
{json.dumps(accounts, ensure_ascii=False, indent=1)}

## 流すストーリー（{len(stories)} 本）
{yaml.safe_dump(stories, allow_unicode=True, sort_keys=False)}
## 出力の形（out/verdicts.yaml）
ストーリーの数だけ。steps はストーリーの steps と同じ数・同じ順。
{yaml.safe_dump(ex, allow_unicode=True, sort_keys=False)}
書いたら `python3 {HERE / 'qa.py'} --check out/verdicts.yaml --repo-dir product` で形を確かめ、ok になるまで直してください。
最後に、何を流してどう決めたかを 3 行で答えて終わってください。
"""


# ------------------------------------------------------------------ 本体
def run(repo_dir: Path, inp: dict, out_dir: Path, defs: dict) -> dict:
    import agent_runner   # 工程からエージェントを呼ぶ唯一の入口（ADR 0010）
    stories = load_stories(repo_dir, inp.get("stories") or None)[: defs["worker"]["stories_max"]]
    if not stories:
        return {"ok": False, "errors": ["流すストーリーがない（specs/stories/ の active なもの）"]}
    env = env_entries(repo_dir, inp.get("env") or "staging", inp.get("env_host") or defs["worker"]["env_host"])
    env["release_id"] = inp.get("release_id")
    if not env["entries"]:
        return {"ok": False, "errors": [f"環境 {env['env']} の画面の入口が ship.yaml にない"]}
    cost, usage, turns, model = 0.0, {}, 0, None
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "out").mkdir()
        (root / "product").symlink_to(repo_dir.resolve(), target_is_directory=True)
        (root / "env.json").write_text(json.dumps({**env, "accounts": (inp.get("touchable") or {}).get("accounts") or []},
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
        state = str(root / ".agent-state")
        mcp = relay_config(root)
        text = prompt(defs, stories, env, inp, mobile=bool(mcp))
        errs: list[str] = []
        doc = None
        for _ in range(2):   # 形が崩れていれば同じ会話で 1 回だけ直させる
            res = agent_runner.run_agentic("qa", text, max_turns=defs["worker"]["max_turns"], cwd=str(root), state_dir=state,
                                           **({"mcp_config": mcp} if mcp else {}))
            cost += res.cost_usd or 0
            turns += res.num_turns or 0
            model = res.model
            for k, v in (res.usage or {}).items():
                if isinstance(v, (int, float)):
                    usage[k] = usage.get(k, 0) + v
            doc = _load(root / "out" / "verdicts.yaml")
            errs = validate(doc, stories, defs, root / "out") if not isinstance(doc, str) else [doc]
            if not errs:
                break
            text = "out/verdicts.yaml に次の崩れがあります。直してください。\n" + "\n".join(f"- {e}" for e in errs)
        out_dir.mkdir(parents=True, exist_ok=True)
        files = screenshots(root / "out")
        for n in files:
            shutil.copy2(root / "out" / n, out_dir / n)
    base = {"cost_usd": cost, "usage": usage, "num_turns": turns, "model": model, "env": env["env"],
            "release_id": inp.get("release_id"), "stories": [s["id"] for s in stories], "mobile": bool(mcp)}
    if errs:
        return {"ok": False, "errors": errs, **base}
    known = {s["id"]: s.get("known_gaps") or [] for s in stories}
    verdicts = [{**v, "known_gaps": known.get(v["story_id"], [])} for v in doc["verdicts"]]
    return {"ok": True, "verdicts": verdicts, "files": files, **base}


def _load(p: Path):
    if not p.is_file():
        return "out/verdicts.yaml がない"
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return f"verdicts.yaml を YAML として読めない: {str(e)[:200]}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check")
    ap.add_argument("--repo-dir", required=True)
    ap.add_argument("--input")
    ap.add_argument("--out-dir")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    defs = load_defs()
    repo_dir = Path(a.repo_dir)
    if a.check:
        p = Path(a.check)
        doc = _load(p)
        ids = [v.get("story_id") for v in (doc.get("verdicts") or [])] if isinstance(doc, dict) else []
        stories = load_stories(repo_dir, [i for i in ids if isinstance(i, str)]) or load_stories(repo_dir)
        errs = [doc] if isinstance(doc, str) else validate(doc, stories, defs, p.parent)
        print("\n".join(errs) if errs else "ok")
        return 1 if errs else 0
    inp = json.loads(Path(a.input).read_text(encoding="utf-8") or "{}") if a.input else {}
    out = run(repo_dir, inp, Path(a.out_dir), defs)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
