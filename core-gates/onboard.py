#!/usr/bin/env python3
"""オンボーディングの棚卸しと解析（S1・S2。ADR 0045 の追記 2026-11-15。Gen1 のプロトタイプ）。

既存の製品のリポジトリ（複数）を読み、リポごとの技術・ビルド・テスト・出し方と、API・画面・リポ間の依存・DB・用語の候補を起こす。
読むだけで何も書かない（作用は dispatcher の onboarding_line.py が結果を見て行う）。先方のリポジトリの名前や中身は、
この工程の出力として工場の記録とオブジェクトストアにだけ残る（公開のリポジトリには書かない）。

使い方:
  core-gates/onboard.py --input input.json --root /work/repos --out result.json

  --root の下に、リポジトリごとのディレクトリ（名前は owner__name）がある。input.json は dispatcher が組み立てた材料
  （製品の構成・事業主の答え・文の資料・前の判定）

依存: python3、agent_runner（工程 onboard。runner とモデルは definitions/agents.yaml）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

WORKDIR = ".onboard"   # 材料の置き場（--root の下。コンテナは使い捨て）
PROMPT_VERSION = "1"
MAX_TURNS = int(os.environ.get("ONBOARD_MAX_TURNS", "120"))
ROLES = ["backend", "mobile", "web", "bff", "infra", "docs", "other"]
BUILT = ["o-stack", "o-api", "o-screens", "o-rules", "o-glossary"]
MET = ["repos-readable", "inventory-filled", "analysis-matched", "drafts-exist"]

_S = {"type": "string"}
_SL = {"type": "array", "items": _S}


def schema() -> dict:
    lv = {"type": "object", "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 3}, "note": _S}, "required": ["level", "note"]}
    return {"type": "object", "properties": {
        "summary": _S,
        "repos": {"type": "array", "maxItems": 20, "items": {"type": "object", "properties": {
            "repo": _S, "role": {"type": "string", "enum": ROLES}, "languages": _SL, "frameworks": _SL,
            "build": _S, "test": _S, "run_local": _S, "ci": _S,
            "ship": {"type": "object", "properties": {"mechanism": _S, "dispatchable": {"type": ["boolean", "null"]}, "inputs": _S, "rollback": _S,
                                                      "environments": _SL, "note": _S}, "required": ["mechanism", "note"]},
            "docs": _SL, "note": _S}, "required": ["repo", "role", "languages", "frameworks", "build", "test", "ci", "ship"]}},
        "services": {"type": "array", "maxItems": 40, "items": {"type": "object", "properties": {
            "name": _S, "role": _S, "repos": _SL, "sandbox": {"type": "string", "enum": ["yes", "no", "unknown", "na"]},
            "handling": {"type": "string", "enum": ["connect", "mock", "skip", "unavailable"]}, "note": _S}, "required": ["name", "role", "repos", "sandbox"]}},
        "api": {"type": "object", "properties": {
            "source": {"type": "string", "enum": ["openapi", "code", "none"]}, "endpoints_total": {"type": "integer"},
            "matched_ratio": {"type": ["number", "null"], "minimum": 0, "maximum": 1}, "note": _S,
            "endpoints": {"type": "array", "maxItems": 300, "items": {"type": "object", "properties": {
                "method": _S, "path": _S, "summary": _S, "auth": _S, "where": _S, "used_by": _SL}, "required": ["method", "path", "summary"]}}},
            "required": ["source", "endpoints_total", "matched_ratio", "endpoints", "note"]},
        "screens": {"type": "array", "maxItems": 200, "items": {"type": "object", "properties": {
            "repo": _S, "name": _S, "where": _S, "calls": _SL, "note": _S}, "required": ["repo", "name", "where", "calls"]}},
        "db": {"type": "object", "properties": {"engine": _S, "migrations": _S, "tables": {"type": ["integer", "null"]}, "note": _S},
               "required": ["engine", "migrations", "note"]},
        "tests": {"type": "array", "maxItems": 20, "items": {"type": "object", "properties": {
            "repo": _S, "frameworks": _SL, "how_to_run": _S, "estimate": {"type": ["integer", "null"]}, "needs": _SL, "note": _S},
            "required": ["repo", "frameworks", "how_to_run"]}},
        "design": {"type": "object", "properties": {"form": {"type": "string", "enum": ["U0", "U1", "U2", "U3", "mixed"]}, "note": _S},
                   "required": ["form", "note"]},
        "network": {"type": "object", "properties": {
            "backend_entry": {"type": "string", "enum": ["public", "private", "unknown"]}, "auth": _S, "cors": _S, "domains": _SL,
            "bff_note": _S}, "required": ["backend_entry", "auth", "cors", "bff_note"]},
        "glossary": {"type": "array", "maxItems": 80, "items": {"type": "object", "properties": {"term": _S, "meaning": _S, "where": _S},
                                                                 "required": ["term", "meaning"]}},
        "docs": {"type": "object", "properties": {"architecture_md": _S, "api_yaml": _S, "screens_md": _S, "glossary_yaml": _S},
                 "required": ["architecture_md", "api_yaml", "screens_md", "glossary_yaml"]},
        "levels": {"type": "object", "properties": {k: lv for k in BUILT}, "required": BUILT},
        "met": {"type": "array", "items": {"type": "string", "enum": MET}},
        "questions": {"type": "array", "maxItems": 15, "items": {"type": "object", "properties": {
            "question": _S, "why": _S, "concern": _S}, "required": ["question", "why"]}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
        "required": ["summary", "repos", "services", "api", "screens", "db", "tests", "design", "network", "glossary", "docs", "levels", "met",
                     "questions", "confidence"]}


def dir_name(repo: str) -> str:
    return repo.replace("/", "__")


def stage(root: Path, inp: dict) -> Path:
    """材料を --root/.onboard/ に置く（エージェントはここと各リポを読む）"""
    w = root / WORKDIR
    w.mkdir(parents=True, exist_ok=True)
    comp = inp.get("composition") or {}
    lines = ["| ディレクトリ | リポ | 役割 | 工場の扱い |", "|---|---|---|---|"]
    for r in comp.get("repos") or []:
        ok = (root / dir_name(r["repo"])).is_dir()
        lines.append(f"| {dir_name(r['repo']) if ok else '（取れなかった）'} | {r['repo']} | {r.get('role')} | "
                     f"{'工場が PR を出す' if r.get('access') == 'write' else '読み取りだけ'} |")
    (w / "repos.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    ans = inp.get("answers") or {}
    (w / "answers.md").write_text("\n\n".join(f"## {v.get('question') or k}（{k}）\n\n{v.get('value') or ''}\n" + (f"\n補足: {v['note']}\n" if v.get("note") else "")
                                              for k, v in ans.items()) or "（まだない）\n", encoding="utf-8")
    fdir = w / "files"
    fdir.mkdir(exist_ok=True)
    names = []
    for i, f in enumerate(inp.get("files") or []):
        if f.get("text"):
            p = fdir / f"{i + 1:02d}.txt"
            p.write_text(f["text"], encoding="utf-8")
            names.append(f"- files/{p.name}: {f['name']}（{f['question']}）")
        else:
            names.append(f"- （本文は渡していない）{f['name']}（{f['question']}、{f.get('bytes')} バイト）")
    (w / "files.md").write_text("\n".join(names) or "（まだない）\n", encoding="utf-8")
    prevq = inp.get("previous_questions") or []
    (w / "previous_questions.md").write_text("\n".join(f"- {q.get('id')}: {q.get('question')}" for q in prevq) or "（まだない）\n", encoding="utf-8")
    return w


def prompt(inp: dict) -> str:
    return f"""あなたは仮想開発事業部のオンボーディング担当です。既存の製品を工場が引き受けるため、製品のリポジトリを読んで、棚卸し（S1）と解析（S2）を行います。
この結果をもとに、工場はすぐに開発（たとえば既存のモバイルの機能を Web に作る、BFF でエンドポイントを足す）を始めます。開発の担当が迷わない材料にしてください。

作業ディレクトリの下に、リポジトリごとのディレクトリがあります。読むだけにし、何も書き換えないでください。材料は {WORKDIR}/ にあります。
- {WORKDIR}/repos.md: 製品の構成（どのディレクトリがどのリポか、役割、工場の扱い）
- {WORKDIR}/answers.md: 事業主の答え（最初に作るもの・ログインのしかた・staging など）
- {WORKDIR}/files.md と {WORKDIR}/files/: 事業主が渡した資料（文のものだけ本文がある）
- {WORKDIR}/previous_questions.md: 前の解析で事業主に聞いたこと（答えは answers.md に a- の id で入っている）

起こすもの:
- repos: リポごとに言語・枠組み・ビルド・テスト・手元での動かし方・CI・出し方（仕組み、外から起動できるか、版と環境を渡せるか、戻す入口、環境）。
  出し方は .github/workflows の on: と inputs、IaC（Terraform・CDK など）、README から読む。分からないことは「不明」と書く
- services: 製品に組み込まれた外部サービス（決済・認証・プッシュ・エラー収集・ストレージなど）。検証用の環境の有無が分からなければ unknown
- api: backend の API。OpenAPI などの定義があれば source=openapi で、それとルーティングのコードを突き合わせ、一致した割合を matched_ratio に。
  定義が無ければ source=code でルーティングから起こし、matched_ratio はモバイルが呼んでいるパスのうち backend に見つかった割合。
  endpoints の used_by には、そのエンドポイントを呼んでいる画面やリポを入れる
- screens: モバイル（と既存の Web）の画面。where はファイルかルート、calls は呼んでいる API のパス
- db: DB の種類・マイグレーションの置き場・テーブルの数
- tests: リポごとのテストの道具・流し方・本数の見込み・動かすのに要るもの（DB・外部サービス・鍵）
- design: UI の提供形態（U0 UI なし・U1 デザインツール・U2 コードの部品とカタログ・U3 画面ごとのデザイン・mixed）
- network: Web から backend を呼ぶときの見立て。backend の入口が公開か VPC の中か（IaC から）、認証のしかた（トークンの種類・置き場）、CORS の設定、
  ドメイン。bff_note には「ブラウザから直接呼ばず、同じドメインの BFF からサーバどうしで呼ぶ」形にするときに気をつけること
- glossary: 製品の用語の候補（コードと文書に出る言葉と意味）
- docs: 人が読む文書の下書き 4 つ（日本語）。architecture_md（構成と、リポ間の依存）、api_yaml（OpenAPI 3 の下書き。長ければ主なものだけ）、
  screens_md（画面と流れと呼ぶ API の表）、glossary_yaml（用語の候補）
- levels: 工場が作る項目の粒度（0〜3）と根拠。
  o-stack [無い, 言語と主な依存, ＋外部サービスの一覧, ＋検証用の環境の有無]、o-api [無い, 一覧だけ, 契約を起こした, ＋実物と照合済]、
  o-screens [無い, 一覧だけ, 画像と流れつき, ＋事業主の確認済]、o-rules [無い, 読み取った慣習, 書き起こした, ＋社内の開発者が確認済]、
  o-glossary [無い, 候補, 下書き, ＋事業主の確認済]。人の確認が要る粒度（3 のうち確認済のもの）はつけない
- met: 満たした条件だけ。repos-readable（全リポを読めた）、inventory-filled（全リポでビルド・テスト・出し方が埋まった。不明は不明と書けていればよい）、
  analysis-matched（API と画面を起こし、照合の割合を出せた）、drafts-exist（用語と構成の下書きがある）
- questions: リポから読めず、開発を始めるのに要ること（最大 15）。前の解析の問いに答えがあれば、もう聞かない。why に、なぜ要るか
- confidence: 0〜1

決まり:
- 鍵・パスワード・トークンの値を見つけても書き写さない（「どこにある」だけ書く）
- 推測は推測と書く。見たファイルのパスを根拠に添える
- 日本語の平易な文で。識別子・パスはそのまま
"""


def onboard(inp: dict, root: Path) -> dict:
    import agent_runner   # 工程からエージェントを呼ぶ唯一の入口（ADR 0010）
    stage(root, inp)
    cwd = os.getcwd()
    os.chdir(root)
    try:
        res = agent_runner.run("onboard", prompt(inp), schema=schema(), max_turns=MAX_TURNS)
    finally:
        os.chdir(cwd)
    out = res.output if isinstance(res.output, dict) else {}
    return {"analysis": out, "versions": {"prompt": PROMPT_VERSION}, "cost_usd": res.cost_usd, "usage": res.usage, "model": res.model,
            "num_turns": res.num_turns}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    inp = json.loads(Path(a.input).read_text(encoding="utf-8") or "{}")
    out = onboard(inp, Path(a.root))
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return 0 if out["analysis"] else 1


if __name__ == "__main__":
    sys.exit(main())
