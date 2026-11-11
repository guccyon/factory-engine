#!/usr/bin/env python3
"""調査委員会の週 1 回の見回り（ADR 0046 の 6、順番 3・4）。工場の費用の記録を読み、兆しを調べ、事故記録（FIR）の下書きを作って査読する。

1 つのジョブで 3 つのエージェントを順に呼ぶ（書いた役は査読しない）:
  1. 見回り＋調査（工程 incident.patrol）: その週の記録（制御プレーンが組み立てた week.json）と既存の FIR を読み、観点（上限 8 個）に 7 割・
     自由な探索に 3 割で兆しを挙げ、なぜなぜで掘って、事故か・型・帯・台帳のどの件か・既存の FIR と重なるかを決める
  2. 記録（工程 incident.record）: 新しい事故（帯 medium 以上・既存の FIR と重ならない）ごとに、製品の情報を抜いた FIR の下書きと、
     Engine の改善 Issue の題と本文を書く。決定論の検査（incident_check.py）で落ちたら、指摘を渡して 1 回だけ書き直させる
  3. 査読（工程 incident.review）: 割り出せないか・ほかの工場でも起きうるか・既存の記録と重複しないかを見る

- 観点の選び方は決定論（select_viewpoints）。その週の兆し・開いている台帳・既存の FIR の型を重く、残りは定義の順
- 材料は一時ディレクトリに写してから読ませる。Engine のチェックアウトには触らせない
- 委員会は直さない。台帳の更新・送り出し口への下書き・Engine の改善 Issue の起票は、dispatcher と supervisor が結果を見て行う
- 専用のチェックの見直し（ADR 0046 の 5 の 3）: definitions/incident.yaml の dedicated_checks のうち、見直しの期日が来た（14 日以内に来る）ものを
  見回り役に渡し、まだ要るか（keep）を決めさせる。要らないものは dispatcher が外す Issue の案にする
- 予算は definitions/incident.yaml の committee.patrol.weekly.budget_usd と investigate.budget_usd の和まで。超えたら下書きを作らない

使い方:
  core-gates/incident_review.py --input week.json --out result.json [--engine-dir <Engine のチェックアウト>]

依存: python3、PyYAML、agent_runner（工程 incident.patrol / incident.record / incident.review。runner とモデルは definitions/agents.yaml）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import incident_check as ic
import yaml

HERE = Path(__file__).resolve().parent
SEV_ORDER = {s: i for i, s in enumerate(ic.SEVERITIES)}   # critical が 0
PATROL_TURNS = 30
RECORD_TURNS = 12
REVIEW_TURNS = 12
MAX_DRAFTS = 2


def load_firs(root: Path) -> list[dict]:
    out = []
    for p in sorted((root / ic.ROOT).glob("FIR-*.yaml")):
        try:
            r = ic.load(p)
        except yaml.YAMLError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def select_viewpoints(defs: dict, signs: list[dict], cases: list[dict], firs: list[dict], limit: int) -> list[dict]:
    """観点を上限まで選ぶ（ADR 0046 の 5 の 5）。その週の兆し（3 点）・開いている台帳（2 点）・既存の FIR（1 点）の型に当たるものを重く。
    同点は定義の順。観点が増えても 1 回に入れる数は増やさない"""
    vps = defs.get("viewpoints") or []
    score = {v["id"]: 0 for v in vps}
    for s in signs:
        for vid in s.get("viewpoints") or []:
            if vid in score:
                score[vid] += 3
    for v in vps:
        for c in cases:
            if c.get("resource") == v.get("resource") and (not c.get("mechanism") or c.get("mechanism") == v.get("mechanism")):
                score[v["id"]] += 2
        for f in firs:
            if f.get("status") != "withdrawn" and f.get("resource") == v.get("resource") and v.get("mechanism") in [f.get("mechanism_class"), *(f.get("also") or [])]:
                score[v["id"]] += 1
    order = {v["id"]: i for i, v in enumerate(vps)}
    picked = sorted(vps, key=lambda v: (-score[v["id"]], order[v["id"]]))[:limit]
    return [{**v, "score": score[v["id"]]} for v in picked]


def next_id(year: int, firs: list[dict], taken: list[str]) -> str:
    """番号は年をまたいで通し（definitions/incident.yaml の layout）。送り出し口にある下書きの番号も使わない"""
    nums = [int(m.group(2)) for x in [*(f.get("id") for f in firs), *taken] if (m := ic.ID_RE.match(str(x or "")))]
    return f"FIR-{year}-{(max(nums) + 1 if nums else 1):04d}"


# ------------------------------------------------------------------ 形
def patrol_schema(defs: dict) -> dict:
    s = {"type": "string"}
    return {"type": "object", "properties": {
        "summary": s,
        "findings": {"type": "array", "maxItems": 6, "items": {"type": "object", "properties": {
            "viewpoint": s, "title": s,
            "resource": {"type": "string", "enum": list(defs["resources"])},
            "mechanism": {"type": "string", "enum": [*defs["mechanisms"], "unknown"]},
            "severity": {"type": "string", "enum": list(ic.SEVERITIES)},
            "is_incident": {"type": "boolean"},
            "case": s, "overlaps": s, "why": s, "evidence": s, "engine_fix": s,
            "record": {"type": "boolean"}},
            "required": ["viewpoint", "title", "resource", "mechanism", "severity", "is_incident", "case", "overlaps", "why", "evidence", "engine_fix", "record"]}},
        "new_viewpoints": {"type": "array", "maxItems": 2, "items": {"type": "object", "properties": {
            "resource": s, "mechanism": s, "question": s, "why": s}, "required": ["resource", "mechanism", "question", "why"]}},
        "check_reviews": {"type": "array", "maxItems": 10, "items": {"type": "object", "properties": {
            "name": s, "keep": {"type": "boolean"}, "why": s}, "required": ["name", "keep", "why"]}}},
        "required": ["summary", "findings", "new_viewpoints", "check_reviews"]}


RECORD_SCHEMA = {"type": "object", "properties": {
    "yaml": {"type": "string"}, "issue_title": {"type": "string"}, "issue_body": {"type": "string"}},
    "required": ["yaml", "issue_title", "issue_body"]}

REVIEW_SCHEMA = {"type": "object", "properties": {
    "ok": {"type": "boolean"},
    "problems": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["identifiable", "not_general", "duplicate", "wrong_type", "unclear"]},
        "where": {"type": "string"}, "why": {"type": "string"}}, "required": ["kind", "where", "why"]}}},
    "required": ["ok", "problems"]}


# ------------------------------------------------------------------ プロンプト
def patrol_prompt(defs: dict, vps: list[dict], free_share: float, due: list[dict] | None = None) -> str:
    res = "\n".join(f"- {k}（{v['ja']}）: 見る数字は {'・'.join(v.get('look_at') or [])}" for k, v in defs["resources"].items())
    mech = "\n".join(f"- {k}（{v['ja']}）: {v['how']}" for k, v in defs["mechanisms"].items())
    sev = "\n".join(f"- {k}: {'、'.join(v)}" for k, v in defs["severity"].items())
    vp = "\n".join(f"- {v['id']}（{v['resource']} × {v['mechanism']}）: {v['question']}" for v in vps)
    chk = "\n".join(f"- {d['name']}（場所 {', '.join(d.get('where') or [])}、記録 {d.get('fir') or 'なし'}、期日 {d['review_by']}）: {d.get('why') or ''}" for d in due or [])
    return f"""あなたは工場の事故の調査委員会の見回り役と調査役です。工場の費用の記録を事業所（製品）を横断して読み、お金の損の兆しを挙げ、なぜなぜで掘ります。

事故＝工場の振る舞いが原因で、事業主に実際のお金がかかった出来事。ラインの停止・人への承認依頼の積み上がり・静かな停止のような可用性の問題は事故にしません
（ただし、それでお金の損が出たときはその分を事故にします）。製品の利用者に起きる障害も別のものです。

作業ディレクトリの材料（読むだけ。何も書き換えない）:
- week.json: この週の記録。日次の見回りの結果（patrols）・兆し（signs）・台帳の件（cases）・日ごとジョブの種類ごとの費用（cost_by_day_kind）・
  費用の多いチケット（top_tickets）・立っている使い捨て環境（ephemeral）・外部サービスの API（測っていなければ measured: false）
- incidents/: これまでの事故記録（FIR）。同じ型のものがあれば、新しい記録ではなく再発（overlaps にその id）にする
- incident.yaml: 事故の型（資源 × 仕組み）・影響の帯・製品の情報を抜く決まり

資源:
{res}

仕組み:
{mech}

影響の帯（比で決める）:
{sev}

この回の観点（力の {round((1 - free_share) * 100)}% をここに使う）:
{vp}

残りの {round(free_share * 100)}% は観点にない自由な探索に使う（観点にない型の兆し、測っていない資源の穴など）。

出すもの:
- summary: この週のまとめ（3〜5 文）。費用の傾向、気になること、測れていないこと
- findings: 兆しごとに 1 件（最大 6 件。お金の損が大きい順）
  - viewpoint: 観点の id。自由な探索なら "free"
  - resource / mechanism: 型。仕組みが決められなければ "unknown"
  - severity: 帯。比が足りなければ low
  - is_incident: 事故か（可用性だけの問題や、ふだんの範囲の費用なら false）
  - case: 台帳の id（cases にある IC-...）。台帳にないものは空
  - overlaps: 同じ型・同じ仕組みの既存の FIR の id。なければ空
  - why: なぜなぜの要約（3〜5 回）。工場のどの部品のどの振る舞いが費用を膨らませたか
  - evidence: 根拠にした week.json の数字（比や件数）
  - engine_fix: Engine の直し方の案（原因の直し。専用のチェックではなく原因を直す。一般の歯止めで足りるならそう書く）。なければ空
  - record: 新しい FIR の下書きにするか（事故で、帯が medium 以上で、overlaps が空のとき true）
- new_viewpoints: 自由な探索で見つけた、まだ観点にない型（資源 × 仕組み）と問い。なければ空
- check_reviews: 下の「見直しの期日が来た専用のチェック」ごとに、まだ要るか（keep）と理由。原因の直しが効いていて、
  week.json にその型の兆しがなければ keep は false（工場を太らせない）。迷うときは true にして理由を書く（人が期日を延ばす）。なければ空

見直しの期日が来た専用のチェック:
{chk or '（なし）'}

決まり:
- 推測で数字を作らない。week.json にない数字は「測っていない」と書く
- 日本語の平易な文で
"""


def record_prompt(defs: dict, finding: dict, fid: str, deny_count: int, errors: list[str] | None = None) -> str:
    di = defs["deidentify"]
    retry = ("\n前の下書きは決定論の検査で次の指摘を受けました。直してください:\n" + "\n".join(f"- {e}" for e in errors) + "\n") if errors else ""
    return f"""あなたは工場の事故の調査委員会の記録役です。見回りで見つかった事故を、ほかの工場にも配れる事故記録（FIR）の下書きにします。

作業ディレクトリの材料（読むだけ）:
- finding.json: 見回り役と調査役が出した事故の所見
- week.json: この週の記録（生の事実。比を出すのに使う。生の金額・件数は FIR に書かない）
- incidents/: これまでの FIR（書き方の手本。同じ書き方にする）
- incident.yaml: 形・型・帯・製品の情報を抜く決まり
{retry}
出すもの:
- yaml: FIR の YAML の全文。id は {fid}、status は draft、local_ref は null。項目は incidents/ の手本と同じ
  （id, title, status, resource, mechanism_class, also, severity, affects, symptom, mechanism, impact, detection, mitigation, related, recurrences, local_ref, note）
  - impact は {', '.join(defs['impact_keys'])} だけ。比が出せないものは null
  - mitigation.fixed_in は空（まだ直していない）。root_fix に原因の直し方、generic_guards は {', '.join(defs['mitigation']['generic_guards'])} から
  - affects.components は Engine の部品の名前（製品の名前ではない）
- issue_title / issue_body: Engine の改善 Issue（公開のリポジトリに出る）。題は「事故の直し: 〜」。本文は症状・仕組み・直し方の案・型・帯。
  FIR と同じく製品の情報を抜く。所見に直し方の案がなければ両方とも空

製品の情報を抜く決まり（FIR と Issue の両方）:
- 書かない: {'；'.join(di['forbid'])}
- 残す: {'；'.join(di['keep'])}
- 製品・リポジトリ・事業主の名前 {deny_count} 個は検査で照合される。役で言い換える（「製品のリポジトリ」「事業主」など）

日本語の平易な文で。
"""


def review_prompt(deny: list[str]) -> str:
    names = "、".join(deny) if deny else "（なし）"
    return f"""あなたは工場の事故の調査委員会の査読役です。記録役が書いた事故記録（FIR）の下書きと Engine の改善 Issue の文を、公開してよいかの目で読みます。
あなたは書いた役ではありません。直さず、問題だけを挙げます。

作業ディレクトリの材料（読むだけ）:
- draft.yaml: FIR の下書き
- issue.json: Engine の改善 Issue の題と本文（空なら Issue なし）
- incidents/: これまでの FIR
- incident.yaml: 形・型・帯・製品の情報を抜く決まり

見ること:
- identifiable: 製品・事業主・人・日付・金額などを割り出せる書き方がないか。言い換えても業務の固有の言葉で製品が分かるものも含む。
  この工場の製品・リポジトリ・事業主の名前: {names}
- not_general: ほかの工場でも起きうる書き方か（この製品だけの話になっていないか）
- duplicate: incidents/ の既存の記録と同じ事故・同じ型の再発ではないか
- wrong_type: 資源・仕組み・帯が所見と合っているか
- unclear: 受け取った工場が「自分は当たるか」「どう直すか」を読み取れるか

ok は問題が 1 つもないときだけ true。
"""


# ------------------------------------------------------------------ 実行
def _material(root: Path, engine: Path, files: dict[str, object]) -> None:
    if (engine / ic.ROOT).is_dir():
        shutil.copytree(engine / ic.ROOT, root / "incidents")
    else:
        (root / "incidents").mkdir()
    shutil.copy(engine / "definitions" / "incident.yaml", root / "incident.yaml")
    for name, v in files.items():
        (root / name).write_text(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1), encoding="utf-8")


def _call(step: str, prompt: str, schema: dict, max_turns: int, engine: Path, files: dict) -> object:
    import agent_runner  # 工程からエージェントを呼ぶ唯一の入口（ADR 0010）
    with tempfile.TemporaryDirectory() as d:
        _material(Path(d), engine, files)
        cwd = os.getcwd()
        os.chdir(d)   # agent_runner はカレントディレクトリでエージェントを動かす
        try:
            return agent_runner.run(step, prompt, schema=schema, max_turns=max_turns)
        finally:
            os.chdir(cwd)


class Meter:
    def __init__(self, cap: float):
        self.cap, self.cost, self.usage, self.model = cap, 0.0, {}, ""

    def add(self, res) -> dict:
        self.cost += float(res.cost_usd or 0)
        for k, v in (res.usage or {}).items():
            if isinstance(v, (int, float)):
                self.usage[k] = self.usage.get(k, 0) + v
        self.model = self.model or res.model
        return res.output if isinstance(res.output, dict) else {}

    @property
    def left(self) -> bool:
        return self.cost < self.cap


def check_draft(fid: str, text: str, issue: dict, deny: list[str]) -> tuple[dict | None, list[str]]:
    try:
        rec = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return None, [f"YAML として読めない: {str(e)[:200]}"]
    if not isinstance(rec, dict):
        return None, ["中身が mapping ではない"]
    rec.update({"id": fid, "status": "draft", "local_ref": None})
    rec.pop("published", None)
    errs = ic.check_record(Path(f"{fid}.yaml"), rec, deny)
    if issue.get("title") or issue.get("body"):
        errs += ic.check_deidentified("Engine の改善 Issue", {"title": issue.get("title") or "", "body": issue.get("body") or ""}, deny)
    return rec, errs


def draft_one(finding: dict, fid: str, week: dict, deny: list[str], defs: dict, engine: Path, meter: Meter) -> dict:
    errors: list[str] = []
    rec, issue = None, {}
    for _ in range(2):   # 検査で落ちたら指摘を渡して 1 回だけ書き直させる
        out = meter.add(_call("incident.record", record_prompt(defs, finding, fid, len(deny), errors or None), RECORD_SCHEMA, RECORD_TURNS, engine,
                              {"finding.json": finding, "week.json": week}))
        issue = {"title": (out.get("issue_title") or "").strip(), "body": (out.get("issue_body") or "").strip()}
        rec, errors = check_draft(fid, out.get("yaml") or "", issue, deny)
        if not errors or not meter.left:
            break
    d = {"id": fid, "case": finding.get("case") or "", "title": finding.get("title"), "resource": finding.get("resource"),
         "mechanism": finding.get("mechanism"), "severity": finding.get("severity"), "check": errors,
         "yaml": yaml.safe_dump(rec, allow_unicode=True, sort_keys=False) if rec else "", "issue": issue if issue.get("title") else None}
    if errors:
        return {**d, "review": None, "ok": False}
    rv = meter.add(_call("incident.review", review_prompt(deny), REVIEW_SCHEMA, REVIEW_TURNS, engine,
                         {"draft.yaml": d["yaml"], "issue.json": issue}))
    review = {"ok": bool(rv.get("ok")), "problems": rv.get("problems") or []}
    return {**d, "review": review, "ok": review["ok"] and not review["problems"]}


def review_week(week: dict, defs: dict, engine: Path) -> dict:
    weekly = ((defs.get("committee") or {}).get("patrol") or {})
    cap = float((weekly.get("weekly") or {}).get("budget_usd", 10)) + float((weekly.get("investigate") or {}).get("budget_usd", 5))
    limit = int((weekly.get("weekly") or {}).get("viewpoints", 8))
    free_share = float((weekly.get("weekly") or {}).get("free_share", 0.3))
    max_drafts = int((weekly.get("weekly") or {}).get("max_drafts", MAX_DRAFTS))
    firs = load_firs(engine)
    vps = select_viewpoints(defs, week.get("signs") or [], week.get("cases") or [], firs, limit)
    today = datetime.fromisoformat(week["to"]).date() if week.get("to") else date.today()
    due = ic.due_checks(defs.get("dedicated_checks") or [], today)
    facts = {"viewpoints": [v["id"] for v in vps], "firs": len(firs), "budget_usd": cap, "checks_due": [d["name"] for d in due],
             "spent_usd": week.get("spent_usd"), "signs": len(week.get("signs") or []), "cases": len(week.get("cases") or [])}
    if not week.get("spent_usd") and not week.get("signs") and not week.get("cases") and not due:
        return {"facts": facts, "result": {"summary": "この週は工場の費用の記録がない", "findings": [], "new_viewpoints": [], "check_reviews": []},
                "drafts": [], "cost_usd": 0, "usage": {}, "model": None}
    meter = Meter(cap)
    found = meter.add(_call("incident.patrol", patrol_prompt(defs, vps, free_share, due), patrol_schema(defs), PATROL_TURNS, engine, {"week.json": week}))
    findings = found.get("findings") or []
    known_cases = {c.get("id") for c in week.get("cases") or []}
    known_firs = {f.get("id") for f in firs}
    for f in findings:   # 知らない id は空にする（作り話の id で台帳や記録を指さない）
        if f.get("case") and f["case"] not in known_cases:
            f["case"] = ""
        if f.get("overlaps") and f["overlaps"] not in known_firs:
            f["overlaps"] = ""
    todo = sorted([f for f in findings if f.get("record") and f.get("is_incident") and not f.get("overlaps")
                   and f.get("mechanism") in defs["mechanisms"] and SEV_ORDER.get(f.get("severity"), 9) <= SEV_ORDER[defs.get("record_from", "medium")]],
                  key=lambda f: SEV_ORDER[f["severity"]])
    drafts: list[dict] = []
    taken = list(week.get("outbox_ids") or [])
    year = int(str(week.get("week") or "2026")[:4])
    for f in todo[:max_drafts]:
        if not meter.left:
            drafts.append({"title": f.get("title"), "case": f.get("case") or "", "ok": False, "check": ["見回りの予算を使い切った"], "skipped": True})
            continue
        fid = next_id(year, firs, taken + [d["id"] for d in drafts if d.get("id")])
        drafts.append(draft_one(f, fid, week, list(week.get("deny") or []), defs, engine, meter))
    by_name = {d["name"]: d for d in due}
    reviews = [{**r, "where": by_name[r["name"]].get("where"), "fir": by_name[r["name"]].get("fir"), "review_by": by_name[r["name"]]["review_by"]}
               for r in found.get("check_reviews") or [] if r.get("name") in by_name]   # 期日の来ていない・知らない名前は捨てる
    return {"facts": facts, "result": {"summary": found.get("summary") or "", "findings": findings, "new_viewpoints": found.get("new_viewpoints") or [],
                                       "check_reviews": reviews},
            "drafts": drafts, "cost_usd": round(meter.cost, 4), "usage": meter.usage, "model": meter.model}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--engine-dir", default=str(HERE.parent))
    a = ap.parse_args(argv)
    engine = Path(a.engine_dir)
    defs = yaml.safe_load((engine / "definitions" / "incident.yaml").read_text(encoding="utf-8"))
    week = json.loads(Path(a.input).read_text(encoding="utf-8") or "{}")
    if not re.match(r"^\d{4}-W\d{2}$", str(week.get("week") or "")):
        print("[incident_review] week がない", file=sys.stderr)
        return 2
    out = review_week(week, defs, engine)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
