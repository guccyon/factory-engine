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
  core-gates/qa.py --check-explore out/explore.yaml --charter detour --repo-dir /work/product   # 夜間の探索の形

入力の mode が explore なら夜間の探索（P5a の nightly_explore）: その夜のチャーターに沿って寄り道し、out/explore.yaml（試したこと・見つけたもの・印）を返す
入力の mode が synthetic なら合成の利用者（ADR 0043 の O4）: 入口ごとの中継（synthetic_proxy.py）越しにストーリーを歩き、out/walk.yaml（行動ログ）を返す。
応答時間は中継が測り、わざと仕込む詰まり（入力の friction）も中継が入れる
  core-gates/qa.py --check-synthetic out/walk.yaml --repo-dir /work/product --env-file env.json --plan plan.json

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


def with_product_severities(defs: dict, repo_dir: Path) -> dict:
    """製品の .factory-config/quality_policy.yaml の S1 の条件と S2〜S4 の基準を、重要度の説明に足した defs（ADR 0043 の O5）。
    重要度の名前（s1〜s4）は変えない。品質方針がなければ defs のまま"""
    p = repo_dir / ".factory-config" / "quality_policy.yaml"
    try:
        qp = yaml.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else None
    except yaml.YAMLError:
        qp = None
    if not isinstance(qp, dict):
        return defs
    sev = dict(defs["severities"])
    conds = {"s1": ((qp.get("s1") or {}).get("conditions") or [])}
    for k, v in ((qp.get("severities") or {}).items() if isinstance(qp.get("severities"), dict) else []):
        if k in sev and isinstance(v, dict):
            if v.get("ja"):
                sev[k] = str(v["ja"])
            conds[k] = v.get("conditions") or []
    for k, cs in conds.items():
        items = [str(c.get("ja")) for c in cs if isinstance(c, dict) and c.get("ja")]
        if k in sev and items:
            sev[k] = f"{sev[k]}。この製品では次のどれかに当たれば {k}: " + " / ".join(items)
    sev["s1"] = str(sev.get("s1")) + "。s1 は工場のラインを止めるので、当たると思ったら推測で下げない（ただし再現できたものだけ）"
    return {**defs, "severities": sev}


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


# 時間をまたぐ手順（ストーリーの time。ADR 0028 の scenario_time）。道具は dispatcher の中継（Mac ランナーと同じ口）で渡る
TIME_WITH_TOOL = """- 時間をまたぐ手順（ストーリーの time: 翌日・再起動のあと・暦月の境）は、mcp__macrunner__env_restart で起こす。
  これは Mac ではなく工場が受け、この環境のサーバーを再起動する（DB は残る）。advance_hours を渡すとサーバーの時計をその時間だけ進めて再起動する
  （翌日なら、日本時間の日付が変わるよう 24。再起動だけなら 0）。終わるまで数十秒かかる。動くのはサーバーの時計だけで、ブラウザの時計は進まない
- 再起動のあとは、ブラウザの文脈を閉じて開き直してから（アプリを開き直したことにする）、次の歩を流す
- 時計を進めたら、流し終わったあとに必ず reset_clock: true で 0 に戻す（ほかのストーリーと人が同じ環境を使う）。使った回数・進めた時間・戻したことを interpretation に書く
- 道具が失敗したら、その歩は skipped にし、exit そのものが時間をまたぐなら blocked にして、道具の返した文を interpretation に書く
"""
TIME_WITHOUT_TOOL = """- 時間をまたぐ手順（翌日・再起動のあと）は、ここではサーバーの再起動も時計の進めもできない（工場の中継が渡っていない）。その歩は skipped にし、
  代わりに「ログインし直し・再読み込みで残るか」までを確かめて、ストーリー全体の decision は exit まで確かめられた範囲で決める
  （exit そのものが時間をまたぐなら blocked にして、何が足りないかを interpretation に書く）
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
{TIME_WITH_TOOL if mobile else TIME_WITHOUT_TOOL}
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



# ------------------------------------------------------------------ 夜間の探索（nightly_explore）
def charter_of(defs: dict, cid: str | None) -> dict:
    """入力のチャーターの id から定義を引く。なければ先頭"""
    cs = defs["explore"]["charters"]
    return next((c for c in cs if c["id"] == cid), cs[0])


def validate_explore(doc, defs: dict, charter: dict, story_ids: set[str], out_dir: Path | None = None) -> list[str]:
    if not isinstance(doc, dict):
        return ["explore.yaml の最上位は対応表（charter・tried・findings・interpretation・confidence）"]
    errs = []
    for k in defs["explore"]["result"]["required"]:
        if k not in doc or (doc.get(k) in (None, "") and k != "findings"):
            errs.append(f"{k} がない")
    if doc.get("charter") not in (None, charter["id"]):
        errs.append(f"charter は {charter['id']}（渡したチャーター）")
    c = doc.get("confidence")
    if not isinstance(c, (int, float)) or isinstance(c, bool) or not 0 <= c <= 1:
        errs.append("confidence は 0〜1 の数")
    tried = doc.get("tried")
    if not isinstance(tried, list) or not tried:
        errs.append("tried は 1 件以上の配列")
    else:
        for j, t in enumerate(tried):
            if not isinstance(t, dict) or not t.get("did") or not t.get("saw"):
                errs.append(f"tried[{j}] に did と saw が要る")
    findings = doc.get("findings")
    if not isinstance(findings, list):
        errs.append("findings は配列（なければ []）")
        findings = []
    if len(findings) > int(defs["explore"]["findings_max"]):
        errs.append(f"findings は {defs['explore']['findings_max']} 件まで（重い順に残す）")
    for j, d in enumerate(findings):
        if not isinstance(d, dict) or not d.get("title") or not d.get("actual"):
            errs.append(f"findings[{j}] に title と actual が要る")
            continue
        if d.get("severity") not in defs["severities"]:
            errs.append(f"findings[{j}].severity は {'・'.join(sorted(defs['severities']))} のどれか")
        if d.get("viewpoint") not in defs["viewpoints"]:
            errs.append(f"findings[{j}].viewpoint は {'・'.join(sorted(defs['viewpoints']))} のどれか")
        if d.get("story_id") not in (None, "") and d["story_id"] not in story_ids:
            errs.append(f"findings[{j}].story_id {d['story_id']!r} は specs/stories/ にない（近いものがなければ null）")
    for j, f in enumerate(doc.get("flags") or []):
        if not isinstance(f, dict) or f.get("viewpoint") not in ("aesthetic", "tone") or not f.get("note"):
            errs.append(f"flags[{j}] は viewpoint（aesthetic・tone）と note")
    if out_dir is not None:
        for t in [*(tried if isinstance(tried, list) else []), *findings]:
            for m in re.finditer(r"([A-Za-z0-9][A-Za-z0-9_-]{0,47}\.png)", json.dumps(t, ensure_ascii=False) if isinstance(t, dict) else ""):
                if not (out_dir / m.group(1)).is_file():
                    errs.append(f"スクリーンショット {m.group(1)} が out/ にない")
    return errs


def explore_prompt(defs: dict, charter: dict, stories: list[dict], env: dict, inp: dict, mobile: bool = False) -> str:
    accounts = ((inp.get("touchable") or {}).get("accounts") or [])
    ex = {"charter": charter["id"], "confidence": 0.7,
          "tried": [{"actor": "student", "where": "予約の一覧", "did": "予約を取り消してから戻るを押した", "saw": "取り消した予約がまだ一覧にある",
                     "story_id": stories[0]["id"] if stories else None}],
          "findings": [{"title": "利用者の言葉で 1 行", "viewpoint": charter["viewpoint"], "severity": "s3", "where": "画面と場所",
                        "reproduce": ["手順 1", "手順 2"], "expected": "利用者が期待すること", "actual": "見えたもの（03-cancel-back.png）",
                        "story_id": None, "known_gap": False}],
          "flags": [{"viewpoint": "aesthetic", "where": "画面と場所", "note": "人に見てほしい違和感"}],
          "interpretation": "何を試し、どこが食い違っていて、どれを重く見たか"}
    smap = [{"id": s["id"], "title": s.get("title"), "actor": s.get("actor"), "goal": s.get("goal"), "entry": s.get("entry"),
             "must_not": s.get("must_not") or [], "known_gaps": s.get("known_gaps") or []} for s in stories]
    return f"""あなたはソフトウェア工場の QA 役です。いまは人のいない夜で、探索テスト（exploratory testing）をします。
QA の定義は「プロダクトの利用者（エンドユーザー、カスタマーサポート、運営担当）にとって使いやすいシステムであるかの品質を担保する」です。
台本（ストーリーの steps）をなぞる判定は昼に別でやっています。今夜は下のチャーター（狙い）に沿って、利用者になったつもりで寄り道し、
ストーリーが書いていないところで利用者が困ることを探してください。

## 今夜のチャーター: {charter['id']}
{charter['ja']}
（見つけたものの観点は、ほかに当てはまらなければ {charter['viewpoint']}）

## 作業の場所
- 作業ディレクトリ（いまの場所）。製品のチェックアウトは product/（読むだけ。書き換えない・コミットしない）
- 利用者の種類は product/specs/stories/actors.yaml、用語は product/specs/glossary.yaml、ストーリーの全文は product/specs/stories/
- 環境の入口と、ログインの手がかりは env.json。ここに書いた URL 以外の環境（本番など）には繋がない
- 成果物は out/ に置く: out/explore.yaml（下の形）と、根拠のスクリーンショット out/<2 桁の番号>-<短い名前>.png（1 枚 600KB まで、全部で 16 枚まで）

## 道具
- 画面はヘッドレスの Chromium を playwright-core で動かす。node で `const {{ chromium }} = require('playwright-core'); const b = await chromium.launch({{ executablePath: process.env.CHROMIUM_BIN, args: ['--no-sandbox'] }});`
  （NODE_PATH は設定済み）。スマートフォンの利用者（生徒アプリ）は幅 390・高さ 844 で開く
- 画面が中継する API（同じオリジンの /api/*）を curl で直接見てもよい。ただし見つけたものは「利用者に見えるもの」で書く
- データは画面か API で作ってよい（合成シードの環境なので消えても困らない）。ただし、ほかの利用者のログインの手がかりを使えなくする操作（退会・停止）はしない
- サーバーの再起動と時計の進めはできない
{MOBILE.format(api=inp.get("mobile_api_url") or defs["worker"]["mobile_api_url"], ref=mobile_ref(inp)) if mobile else ""}
## 決まり
- 見つけたものは {defs['explore']['findings_max']} 件まで、重い順。再現できたものだけ書く（再現の手順は利用者の操作で）
- ストーリーの known_gaps に書いてある食い違いに当たったら known_gap: true を付ける。近いストーリーがあれば story_id に id を、なければ null
- 見た目（色・余白・書体）と言葉の温度は見つけたものにしない。気になったら flags に書く（viewpoint は aesthetic か tone）
- 観点: {json.dumps(defs['viewpoints'], ensure_ascii=False)}
- 重要度: {json.dumps(defs['severities'], ensure_ascii=False)}
- 何も見つからなくてもよい（findings: []）。試したこと（tried）は必ず書く。推測で見つけたものにしない

## env.json の要点
{json.dumps(env, ensure_ascii=False, indent=1)}
ログインの手がかり（モックのトークン。画面のログインに使う）:
{json.dumps(accounts, ensure_ascii=False, indent=1)}

{focus_text(inp)}
## 地図（ストーリーの要点。{len(smap)} 本）
{yaml.safe_dump(smap, allow_unicode=True, sort_keys=False)}
## 出力の形（out/explore.yaml）
{yaml.safe_dump(ex, allow_unicode=True, sort_keys=False)}
書いたら `python3 {HERE / 'qa.py'} --check-explore out/explore.yaml --charter {charter['id']} --repo-dir product` で形を確かめ、ok になるまで直してください。
最後に、何を試して何を見つけたかを 3 行で答えて終わってください。
"""


def run_explore(repo_dir: Path, inp: dict, out_dir: Path, defs: dict) -> dict:
    import agent_runner   # 工程からエージェントを呼ぶ唯一の入口（ADR 0010）
    charter = charter_of(defs, inp.get("charter"))
    stories = load_stories(repo_dir)
    env = env_entries(repo_dir, inp.get("env") or "staging", inp.get("env_host") or defs["worker"]["env_host"])
    env["release_id"] = inp.get("release_id")
    base = {"mode": "explore", "charter": charter["id"], "night": inp.get("night"), "env": env["env"], "release_id": inp.get("release_id")}
    if not env["entries"]:
        return {"ok": False, "errors": [f"環境 {env['env']} の画面の入口が ship.yaml にない"], **base}
    cost, usage, turns, model = 0.0, {}, 0, None
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "out").mkdir()
        (root / "product").symlink_to(repo_dir.resolve(), target_is_directory=True)
        (root / "env.json").write_text(json.dumps({**env, "accounts": (inp.get("touchable") or {}).get("accounts") or []},
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
        state = str(root / ".agent-state")
        mcp = relay_config(root)
        text = explore_prompt(defs, charter, stories, env, inp, mobile=bool(mcp))
        errs: list[str] = []
        doc = None
        for _ in range(2):   # 形が崩れていれば同じ会話で 1 回だけ直させる
            res = agent_runner.run_agentic("qa", text, max_turns=defs["explore"]["max_turns"], cwd=str(root), state_dir=state,
                                           **({"mcp_config": mcp} if mcp else {}))
            cost += res.cost_usd or 0
            turns += res.num_turns or 0
            model = res.model
            for k, v in (res.usage or {}).items():
                if isinstance(v, (int, float)):
                    usage[k] = usage.get(k, 0) + v
            doc = _load(root / "out" / "explore.yaml")
            errs = validate_explore(doc, defs, charter, {s["id"] for s in stories}, root / "out") if not isinstance(doc, str) else [doc]
            if not errs:
                break
            text = "out/explore.yaml に次の崩れがあります。直してください。\n" + "\n".join(f"- {e}" for e in errs)
        out_dir.mkdir(parents=True, exist_ok=True)
        files = screenshots(root / "out")
        for n in files:
            shutil.copy2(root / "out" / n, out_dir / n)
    base.update({"cost_usd": cost, "usage": usage, "num_turns": turns, "model": model, "mobile": bool(mcp), "stories": [], "verdicts": []})
    if errs:
        return {"ok": False, "errors": errs, **base}
    known = {s["id"]: s.get("known_gaps") or [] for s in stories}
    findings = [{**f, "known_gaps": known.get(f.get("story_id") or "", [])} for f in doc.get("findings") or []]
    return {"ok": True, "files": files, "explore": {**doc, "charter": charter["id"], "findings": findings, "flags": doc.get("flags") or []}, **base}


# ------------------------------------------------------------------ 品質の Learn の重点と回帰のシナリオ（ADR 0043 の O9）
def focus_text(inp: dict) -> str:
    """dispatcher が入力に足した重点（厚く流すストーリー・観点）と回帰のシナリオを、プロンプトの節にする。なければ空"""
    f = inp.get("focus") or {}
    sc = inp.get("scenarios") or []
    if not f.get("stories") and not f.get("viewpoints") and not sc:
        return ""
    lines = ["## 品質の Learn の重点（工場が Defect の記録から週 1 回決めた）"]
    if f.get("stories"):
        lines.append(f"- 厚く見るストーリー: {', '.join(f['stories'])}")
    if f.get("viewpoints"):
        lines.append(f"- 厚く見る観点: {', '.join(f['viewpoints'])}")
    if sc:
        lines.append("- 回帰のシナリオ（前に見つかった Defect の再現の手順。直っているかも試し、試したことに書く）:")
        for x in sc[:5]:
            rep = " → ".join(str(r) for r in (x.get("reproduce") or [])[:8])
            lines.append(f"  - {x.get('id')}: {x.get('title')}（{rep}）。期待: {x.get('expected') or '-'}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ 合成の利用者（ADR 0043 の O4 synthetic_users_loop）
def event_names(repo_dir: Path) -> set[str]:
    """製品の行動イベントの名前（specs/analytics/events.yaml）"""
    p = repo_dir / "specs" / "analytics" / "events.yaml"
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return set()
    return {e.get("name") for e in doc.get("events") or [] if isinstance(e, dict) and e.get("name")}


def walk_plan(stories: list[dict], inp: dict, sd: dict) -> list[dict]:
    """歩く予定。重点のストーリーは walks_focus 回、ほかは walks_other 回"""
    focus = set((inp.get("focus") or {}).get("stories") or [])
    return [{"story_id": s["id"], "rounds": int(sd["walks_focus"] if s["id"] in focus else sd["walks_other"])} for s in stories]


def validate_synthetic(doc, sd: dict, plan: list[dict], events: set[str], entries: set[str]) -> list[str]:
    if not isinstance(doc, dict):
        return ["walk.yaml の最上位は対応表（walks・interpretation・confidence）"]
    errs = []
    for k in sd["result"]["required"]:
        if doc.get(k) in (None, "", []):
            errs.append(f"{k} がない")
    c = doc.get("confidence")
    if not isinstance(c, (int, float)) or isinstance(c, bool) or not 0 <= c <= 1:
        errs.append("confidence は 0〜1 の数")
    want = {p["story_id"]: p["rounds"] for p in plan}
    got: dict[str, int] = {}
    for i, w in enumerate(doc.get("walks") or []):
        at = f"walks[{i}]"
        if not isinstance(w, dict):
            errs.append(f"{at} が対応表でない")
            continue
        sid = w.get("story_id")
        if sid not in want:
            errs.append(f"{at}.story_id {sid!r} は歩く予定のストーリーにない")
            continue
        got[sid] = got.get(sid, 0) + 1
        if entries and w.get("entry") not in entries:
            errs.append(f"{at}.entry は env.json の入口の port_env（{'・'.join(sorted(entries))}）のどれか")
        for k in ("found_entry", "reached_exit"):
            if not isinstance(w.get(k), bool):
                errs.append(f"{at}.{k} は真偽")
        a = w.get("attempts_to_entry")
        if not isinstance(a, int) or isinstance(a, bool) or a < 0:
            errs.append(f"{at}.attempts_to_entry は 0 以上の整数")
        steps = w.get("steps")
        if not isinstance(steps, list) or not steps:
            errs.append(f"{at}.steps は 1 歩以上")
            continue
        for j, st in enumerate(steps):
            if not isinstance(st, dict) or not st.get("did"):
                errs.append(f"{at}.steps[{j}] に did が要る")
                continue
            if st.get("waited") not in ("none", "short", "long"):
                errs.append(f"{at}.steps[{j}].waited は none・short・long のどれか")
            if not isinstance(st.get("stuck"), bool):
                errs.append(f"{at}.steps[{j}].stuck は真偽")
            ev = st.get("event")
            if ev not in (None, "") and events and ev not in events:
                errs.append(f"{at}.steps[{j}].event {ev!r} は specs/analytics/events.yaml にない（なければ null）")
    short = [f"{sid}（{got.get(sid, 0)}/{n}）" for sid, n in want.items() if got.get(sid, 0) < n]
    if short:
        errs.append(f"歩いた回数が足りないストーリー: {', '.join(short)}")
    return errs


def synthetic_prompt(defs: dict, stories: list[dict], plan: list[dict], env: dict, inp: dict, events: set[str]) -> str:
    sd = defs["synthetic"]
    accounts = ((inp.get("touchable") or {}).get("accounts") or [])
    s0 = stories[0]["id"] if stories else "<id>"
    ex = {"confidence": 0.7, "interpretation": "どのストーリーを何回歩き、どこで待たされ・迷ったか",
          "walks": [{"story_id": s0, "round": 1, "actor": "cs", "entry": (env.get("entries") or [{}])[0].get("port_env"),
                     "found_entry": True, "attempts_to_entry": 1, "reached_exit": True,
                     "steps": [{"n": 1, "did": "本部の画面のトップから予約の一覧を開いた", "event": None, "waited": "long", "stuck": False,
                                "note": "一覧が出るまで数秒かかった"}],
                     "felt": "利用者として感じたこと 1〜2 行"}]}
    return f"""あなたはソフトウェア工場の「合成の利用者」です。実際の利用者がまだいない製品で、利用者の代わりにユーザーストーリーに沿って画面を歩き、
利用者がどこで待たされ、どこで迷うかの記録（行動ログ）を残します。判定（pass / hold）や不具合探しはしません。ふつうの利用者として、
案内（画面の文言・リンク・ボタン）だけを頼りに目的にたどり着こうとしてください。URL を直接打つ・API を直接呼ぶ・製品のコードを読んで入口を探すのはしない
（それをすると利用者の迷いが記録に残らない）。

## 作業の場所
- 作業ディレクトリ（いまの場所）。製品のチェックアウトは product/（ストーリーと用語を読むだけ。書き換えない）
- 環境の入口と、ログインの手がかりは env.json。ここに書いた URL 以外には繋がない（入口はこのジョブの中継で、応答時間を工場が測っている）
- 成果物は out/walk.yaml（下の形）。スクリーンショットは要らない

## 道具
- 画面はヘッドレスの Chromium を playwright-core で動かす。node で `const {{ chromium }} = require('playwright-core'); const b = await chromium.launch({{ executablePath: process.env.CHROMIUM_BIN, args: ['--no-sandbox'] }});`
  （NODE_PATH は設定済み）。スマートフォンの利用者（生徒アプリ）は幅 390・高さ 844 で開く
- データは画面で作ってよい（合成シードの環境なので消えても困らない）。ほかの利用者のログインの手がかりを使えなくする操作（退会・停止）はしない
- サーバーの再起動と時計の進めはできない（時間をまたぐ歩は飛ばして note に書く）

## 歩き方
- 下の予定のとおり、ストーリーごとに決まった回数だけ歩く（2 回目は 1 回目の記憶に頼らず、もう一度画面の案内から探す）
- 入口（ストーリーの entry と最初の歩の where）を、ログイン直後の画面から案内だけで探す。見つけるまでに開いた画面・押したものの数を attempts_to_entry に。
  見つけられなければ found_entry: false にして、その回はそこまで
- 1 歩ごとに、待たされたか（none: すぐ・short: 1 秒くらい・long: 2 秒より長い）、詰まったか（stuck）、このとき起きたはずの行動イベント（event）を書く。
  event は product/specs/analytics/events.yaml の name から選ぶ（当てはまらなければ null）
- exit まで行けたら reached_exit: true

## env.json の要点
{json.dumps(env, ensure_ascii=False, indent=1)}
ログインの手がかり（モックのトークン。画面のログインに使う）:
{json.dumps(accounts, ensure_ascii=False, indent=1)}

{focus_text(inp)}
## 歩く予定（{sum(p['rounds'] for p in plan)} 回）
{yaml.safe_dump(plan, allow_unicode=True, sort_keys=False)}
## ストーリー
{yaml.safe_dump(stories, allow_unicode=True, sort_keys=False)}
## 行動イベントの名前
{', '.join(sorted(events)) or '（製品に specs/analytics/events.yaml がない。event は null）'}

## 出力の形（out/walk.yaml）
{yaml.safe_dump(ex, allow_unicode=True, sort_keys=False)}
書いたら `python3 {HERE / 'qa.py'} --check-synthetic out/walk.yaml --repo-dir product --env-file env.json --plan plan.json` で形を確かめ、ok になるまで直してください。
最後に、何回歩いてどこで待たされ・迷ったかを 3 行で答えて終わってください。
"""


def run_synthetic(repo_dir: Path, inp: dict, out_dir: Path, defs: dict) -> dict:
    import agent_runner   # 工程からエージェントを呼ぶ唯一の入口（ADR 0010）
    import synthetic_proxy
    sd = defs["synthetic"]
    stories = load_stories(repo_dir)[: sd["stories_max"]]
    env = env_entries(repo_dir, inp.get("env") or "staging", inp.get("env_host") or defs["worker"]["env_host"])
    env["release_id"] = inp.get("release_id")
    friction = inp.get("friction") or {}
    base = {"mode": "synthetic", "env": env["env"], "release_id": inp.get("release_id"), "stories": [], "verdicts": []}
    if not stories:
        return {"ok": False, "errors": ["歩くストーリーがない（specs/stories/ の active なもの）"], **base}
    if not env["entries"]:
        return {"ok": False, "errors": [f"環境 {env['env']} の画面の入口が ship.yaml にない"], **base}
    plan = walk_plan(stories, inp, sd)
    events = event_names(repo_dir)
    cost, usage, turns, model = 0.0, {}, 0, None
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "out").mkdir()
        (root / "product").symlink_to(repo_dir.resolve(), target_is_directory=True)
        log_path = root / "relay.jsonl"
        with synthetic_proxy.Relays(env, friction, log_path) as relayed:
            (root / "env.json").write_text(json.dumps({**relayed, "accounts": (inp.get("touchable") or {}).get("accounts") or []},
                                                      ensure_ascii=False, indent=1), encoding="utf-8")
            (root / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            state = str(root / ".agent-state")
            text = synthetic_prompt(defs, stories, plan, relayed, inp, events)
            entries = {e.get("port_env") for e in env["entries"]}
            errs: list[str] = []
            doc = None
            for _ in range(2):   # 形が崩れていれば同じ会話で 1 回だけ直させる
                res = agent_runner.run_agentic("qa", text, max_turns=sd["max_turns"], cwd=str(root), state_dir=state)
                cost += res.cost_usd or 0
                turns += res.num_turns or 0
                model = res.model
                for k, v in (res.usage or {}).items():
                    if isinstance(v, (int, float)):
                        usage[k] = usage.get(k, 0) + v
                doc = _load(root / "out" / "walk.yaml")
                errs = validate_synthetic(doc, sd, plan, events, entries) if not isinstance(doc, str) else [doc]
                if not errs:
                    break
                text = "out/walk.yaml に次の崩れがあります。直してください。\n" + "\n".join(f"- {e}" for e in errs)
        rows = synthetic_proxy.read_log(log_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    base.update({"cost_usd": cost, "usage": usage, "num_turns": turns, "model": model, "mobile": False})
    if errs:
        return {"ok": False, "errors": errs, **base}
    planted = {k: v for k, v in friction.items() if k in ("slow", "hide") and v}
    return {"ok": True, "files": [], **base,
            "synthetic": {"walks": doc.get("walks") or [], "interpretation": doc.get("interpretation"), "confidence": doc.get("confidence"),
                          "plan": plan, "timings": synthetic_proxy.timings(rows), "requests": len(rows),
                          "hidden_served": sum(1 for r in rows if r.get("hidden")), "planted": planted,
                          "entries": [{"port_env": e.get("port_env"), "label": e.get("label")} for e in env["entries"]]}}


# ------------------------------------------------------------------ 本体
def run(repo_dir: Path, inp: dict, out_dir: Path, defs: dict) -> dict:
    defs = with_product_severities(defs, repo_dir)   # 製品の S1 の条件と S2〜S4 の基準（ADR 0043 の O5）
    if inp.get("mode") == "explore":   # 夜間の探索（nightly_explore）
        return run_explore(repo_dir, inp, out_dir, defs)
    if inp.get("mode") == "synthetic":   # 合成の利用者（ADR 0043 の O4）
        return run_synthetic(repo_dir, inp, out_dir, defs)
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
        return f"out/{p.name} がない"
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return f"{p.name} を YAML として読めない: {str(e)[:200]}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check")
    ap.add_argument("--check-explore")
    ap.add_argument("--check-synthetic")
    ap.add_argument("--env-file")
    ap.add_argument("--plan")
    ap.add_argument("--charter")
    ap.add_argument("--repo-dir", required=True)
    ap.add_argument("--input")
    ap.add_argument("--out-dir")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    defs = load_defs()
    repo_dir = Path(a.repo_dir)
    if a.check_synthetic:
        p = Path(a.check_synthetic)
        doc = _load(p)
        plan = json.loads(Path(a.plan).read_text(encoding="utf-8")) if a.plan else []
        envd = json.loads(Path(a.env_file).read_text(encoding="utf-8")) if a.env_file else {}
        entries = {e.get("port_env") for e in envd.get("entries") or []}
        errs = [doc] if isinstance(doc, str) else validate_synthetic(doc, defs["synthetic"], plan, event_names(repo_dir), entries)
        print("\n".join(errs) if errs else "ok")
        return 1 if errs else 0
    if a.check_explore:
        p = Path(a.check_explore)
        doc = _load(p)
        stories = {s["id"] for s in load_stories(repo_dir)}
        errs = [doc] if isinstance(doc, str) else validate_explore(doc, defs, charter_of(defs, a.charter), stories, p.parent)
        print("\n".join(errs) if errs else "ok")
        return 1 if errs else 0
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
