"""ワークフローの宣言の決定論の当てはめ（ADR 0038）。プランナー（plan.py）がエージェントの答えに重ねる。

- 道（ADR 0037）のひな形にあるステップだけを並べる。知らないステップは落として印 unknown_step
- 上流の役と実装役の run は、経路（steps の役の並び。今までのプランナーの答え）に合わせる。経路が正で、宣言はその上の厚みと理由
- 飛ばせないステップ（definitions/workflow.yaml の mandatory）を飛ばす宣言は run: true に戻し、印 mandatory_skip:<ステップ>
  厚みが下限（min_effort）より薄ければ下限に上げる（同じ印）
- 上限は、宣言 → 帯の既定値 → 天井 の順に決める。天井を超える宣言は天井に切り詰め、印 cap_clamped:<種類>
- deep に理由がなければ印 deep_without_why（厚みは変えない）。宣言がなければ、ひな形と既定値で埋めて印 no_workflow（今の動きと同じ）

LLM を通さない。返り値の workflow がそのまま Plan に残り、dispatcher（workflow_shadow.py）が数えて比べる。
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DEFS = Path(os.environ.get("WORKFLOW_DEFINITIONS") or HERE.parent / "definitions" / "workflow.yaml")
DOC_PREFIXES = ("specs/", "docs/", "changelog.d/")


def load(path: Path | None = None) -> dict:
    return yaml.safe_load((path or DEFS).read_text(encoding="utf-8"))


def schema(defs: dict) -> dict:
    """エージェントの答えの workflow の形（plan.py の schema に入れる）"""
    steps = list(defs["steps"])
    efforts = defs["efforts"]["order"]
    return {"type": "object", "properties": {
        "path": {"type": "string", "enum": list(defs["paths"])},
        "steps": {"type": "array", "maxItems": 16, "items": {"type": "object", "properties": {
            "step": {"type": "string", "enum": steps},
            "run": {"type": "string", "enum": ["true", "false", "auto"]},
            "effort": {"type": "string", "enum": efforts},
            "why": {"type": "string"},
            "scope": {"type": "array", "maxItems": 20, "items": {"type": "string"}}},
            "required": ["step", "run", "why"]}},
        "caps": {"type": "array", "maxItems": 12, "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(defs["caps"]["kinds"])},
            "value": {"type": "number", "minimum": 0},
            "why": {"type": "string"}}, "required": ["kind", "value"]}},
        "handoff": {"type": "object", "properties": {
            "ship": {"type": "string", "enum": defs["handoff"]["ship"]},
            "learn": {"type": "string", "enum": defs["handoff"]["learn"]}}},
        "start": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": defs["start"]["values"]},
            "after": {"type": "array", "maxItems": 8, "items": {"type": "integer"}},
            "files": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
            "why": {"type": "string"}}, "required": ["mode"]},
        "basis": {"type": "string"}},
        "required": ["path", "steps", "start", "basis"]}


def _run(v) -> bool | str:
    if v in (True, "true"):
        return True
    if v == "auto":
        return "auto"
    return False


def docs_only(files: list[str]) -> bool:
    """見込みのファイルが文書と changelog の断片だけか（検査役を飛ばしてよい条件）"""
    return bool(files) and all(f.startswith(DOC_PREFIXES) or f.endswith(".md") for f in files)


def _effort_up(e: str | None, floor: str, order: list[str]) -> str:
    e = e if e in order else "standard"
    return floor if order.index(e) < order.index(floor) else e


def declared_caps(v) -> dict:
    """答えの caps（[{kind, value}] か {kind: value}）を {kind: value} にする"""
    if isinstance(v, dict):
        return dict(v)
    return {c["kind"]: c.get("value") for c in v or [] if isinstance(c, dict) and c.get("kind")}


def caps_for(declared: dict | None, band: str | None, defs: dict) -> tuple[dict, list[str]]:
    """上限を決める。返り値は (上限, 切り詰めた種類)"""
    c = defs["caps"]
    base = {**c["defaults"]["any"], **c["defaults"].get(band if band in c["defaults"] else "M", {})}
    out, clamped = dict(base), []
    for k, v in (declared or {}).items():
        if k not in c["kinds"] or not isinstance(v, (int, float)) or v < 0:
            continue
        out[k] = v
    for k, v in list(out.items()):
        ceil = c["ceilings"].get(k)
        if ceil is not None and v > ceil:
            out[k] = ceil
            clamped.append(k)
    return out, clamped


def normalize(decl: dict | None, roles: list[str], route_details: list[dict], expected_files: list[str], band: str | None,
              defs: dict, path: str | None = None) -> tuple[dict, list[str]]:
    """エージェントの宣言に決定論を当てる。返り値は (正規化したワークフロー, 印)"""
    marks: list[str] = []
    decl = decl if isinstance(decl, dict) else {}
    if not decl:
        marks.append("no_workflow")
    path = path or (decl.get("path") if decl.get("path") in defs["paths"] else "ticket")
    template = defs["paths"][path]
    order = defs["efforts"]["order"]
    given = {}
    for s in decl.get("steps") or []:
        if not isinstance(s, dict):
            continue
        if s.get("step") not in template:
            marks.append(f"unknown_step:{s.get('step')}")
            continue
        given.setdefault(s["step"], s)
    details = {d.get("role"): d for d in route_details or [] if isinstance(d, dict)}
    steps = []
    for name in template:
        kind = defs["steps"][name]["kind"]
        g = given.get(name) or {}
        st = {"step": name, "run": _run(g.get("run", "true")), "effort": g.get("effort") if g.get("effort") in order else "standard",
              "why": g.get("why") or ""}
        if g.get("scope"):
            st["scope"] = [str(x) for x in g["scope"]][:20]
        if kind in ("upstream",) or name == "implement":   # 経路が正（プランナーの役の並び）
            st["run"] = name in roles
            d = details.get(name) or {}
            if st["run"]:
                st["effort"] = d.get("effort") if d.get("effort") in order else st["effort"]
                st["why"] = st["why"] or d.get("why") or ""
        rule = defs["mandatory"].get(name)
        if rule:
            forced = rule.get("always") or (rule.get("when") == "code_change" and not docs_only(expected_files))
            if name == "judge":
                if st["run"] is False:
                    st["run"] = "auto"
                    marks.append("mandatory_skip:judge")
            elif forced and st["run"] is not True:
                st["run"] = True
                marks.append(f"mandatory_skip:{name}")
            if st["run"] is True and rule.get("min_effort"):
                up = _effort_up(st["effort"], rule["min_effort"], order)
                if up != st["effort"]:
                    st["effort"] = up
                    marks.append(f"mandatory_skip:{name}")
        if st["run"] is True and st["effort"] == "deep" and not st["why"]:
            marks.append(f"deep_without_why:{name}")
        steps.append(st)
    dc = declared_caps(decl.get("caps"))
    caps, clamped = caps_for(dc, band, defs)
    marks += [f"cap_clamped:{k}" for k in clamped]
    h = decl.get("handoff") if isinstance(decl.get("handoff"), dict) else {}
    handoff = {"ship": h.get("ship") if h.get("ship") in defs["handoff"]["ship"] else defs["handoff"]["ship"][0],
               "learn": h.get("learn") if h.get("learn") in defs["handoff"]["learn"] else defs["handoff"]["learn"][0]}
    s = decl.get("start") if isinstance(decl.get("start"), dict) else {}
    after = sorted({int(x) for x in s.get("after") or [] if isinstance(x, int) or (isinstance(x, str) and x.isdigit())})
    start = {"mode": "after" if s.get("mode") == "after" and after else "now", "after": after if s.get("mode") == "after" else [],
             "files": [str(f) for f in s.get("files") or []][:20], "why": s.get("why") or ""}
    return {"path": path, "steps": steps, "caps": caps, "caps_declared": {k: v for k, v in dc.items() if k in defs["caps"]["kinds"]},
            "handoff": handoff, "start": start, "basis": decl.get("basis") or ""}, marks


def prompt_section(defs: dict) -> str:
    """プランナーのプロンプトに入れる、ワークフローの宣言の書き方"""
    tpl = " → ".join(defs["paths"]["ticket"])
    must = []
    for k, r in defs["mandatory"].items():
        if k not in defs["paths"]["ticket"]:
            continue
        if r.get("always"):
            must.append(f"{k}（常に）")
        elif r.get("when") == "code_change":
            must.append(f"{k}（コードを変えるなら。文書と changelog.d/ だけなら飛ばせる。厚みは {r.get('min_effort')} まで下げてよい）")
        elif r.get("when") == "questions_or_findings":
            must.append(f"{k}（仮決めか指摘があれば必ず動くので auto）")
    eff = defs["efforts"]["table"]
    efforts = "\n".join(f"  - {k}: " + "／".join(f"{e}＝{v}" for e, v in t.items()) for k, t in eff.items())
    c = defs["caps"]
    caps = "、".join(f"{k}（{v['ja']}。天井 {c['ceilings'].get(k)}）" for k, v in c["kinds"].items() if not k.startswith(("quick.", "analysis.")))
    dflt = "、".join(f"{b}: " + "・".join(f"{k.split('.')[0]} {v}" for k, v in d.items() if k.endswith("turns")) for b, d in c["defaults"].items() if b != "any")
    return f"""- workflow: このチケットのワークフローの宣言（ADR 0038）。道 path は ticket（チケット化）。ひな形のステップは {tpl}。
  steps に、ステップごとに run（true・false・auto）と why（理由）、必要なら effort（{'・'.join(defs['efforts']['order'])}）と scope（qa なら流すストーリーのファイル）を書く。
  上流の役と implement は、上の経路（steps の役）と同じにする。要らないステップは false にして理由を書く（例: 画面のない変更で qa を飛ばす）。
  飛ばせないステップ: {'、'.join(must)}。
  厚み（どこを厚くするか）:
{efforts}
  deep は費用が増えるので、付けたら why を必ず書く。迷ったら standard。
  caps に上限を [{{kind, value, why}}] で書いてよい（書かない種類は大きさの帯の既定値: {dflt}、衝突の解消 {c['defaults']['any']['retries.conflict']} 回など）。種類: {caps}。
  天井を超える値は天井に切り詰められる。衝突しやすいファイルに触るなら衝突の解消を少なめに、のように、このチケットに合わせて決める。
  handoff: ship（train＝次の出荷の列車／none＝出荷しない（工場の内部・文書だけ）／expedite＝列車を待たず staging に出す提案）、learn（signals＝信号を記録するだけ／case_review＝結果にかかわらず分析役に回す）。
  start: 着手のしかた。inflight.md の、流れているタスクが触る（見込みの）ファイルとこのチケットの expected_files を突き合わせ、
  重なるなら mode を after にして after にそのチケットの番号、files に重なるファイル、why に理由を書く。重ならなければ now。
  ただし、ほぼすべてのタスクが触るファイル（CHANGELOG のようなもの）は並べ方では解かない（並べるとラインが 1 本になる）。そのファイルだけの重なりなら now にしてよい。
  basis: 宣言の根拠。どの知見（knowledge/）と実績（sizes.md・caps.md）を見たかを書く
"""
