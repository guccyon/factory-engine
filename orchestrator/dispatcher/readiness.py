"""オンボーディングの準備度の判定（ADR 0045 の決定 8 と追記「操業コンソールに出すもの」）。

定義は definitions/onboarding_readiness.yaml（どの製品にも効くもの）。その製品の今の粒度は工場の置き場にあり、
ここには渡されるだけ。判定は DB にも GitHub にも触れない純粋な関数で、操業コンソールと受信トレイが同じ結果を使う。

- judge(defs, levels, target): 1 つの仕事について、判定（blocked / weak / ready / enough）・割合・警告・お願い
- ladder(defs, met): レベル L1〜L5 のどこまで届いたかと、次のレベルに足りない条件
- ready_line_ok(defs, levels, met): 稼働 ready の承認を出してよいか（L1〜L3 の条件と「開発を始める」の必須。D4）
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

FILE = "onboarding_readiness.yaml"


def candidates(here: Path) -> list[Path]:
    """定義の置き場所の候補（stage_roi.agents_candidates と同じ探し方）。作業ツリーではリポジトリの根の definitions/、
    コンテナでは /definitions（compose で読み取り専用に載せたとき）"""
    out = [Path(os.environ["ROADMAP"]).parent / FILE] if os.environ.get("ROADMAP") else []
    if len(here.parents) > 2:
        out.append(here.parents[2] / "definitions" / FILE)
    return out + [Path("/definitions") / FILE]


def load(paths: list[Path] | None = None) -> dict:
    for p in paths or candidates(Path(__file__).resolve()):
        if p.is_file():
            return yaml.safe_load(p.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"{FILE} が見つからない")


def _band(defs: dict, ratio: float) -> str:
    for b in defs["bands"]:
        if "below" not in b or ratio < b["below"]:
            return b["id"]
    return defs["bands"][-1]["id"]


def band_ja(defs: dict, verdict: str) -> str:
    if verdict == "blocked":
        return defs["blocked_ja"]
    return next(b["ja"] for b in defs["bands"] if b["id"] == verdict)


def judge(defs: dict, levels: dict[str, int], target: str) -> dict:
    """1 つの仕事の判定。levels は項目 id → 粒度（0〜3）。記録に無い項目は 0（無い）として数える。

    返す形: {target, verdict, ratio, blocked: [項目 id], warnings: [{id, level, goal, warn}], asks: [{id, ask}]}
    blocked は必須が最低に届かない項目、warnings は目標に届かない必須でない項目（blocked と重ねない）、
    asks は目標に届かない項目のうちお願いを持つもの（blocked を先に、あとは目標までの差の大きい順）。"""
    if target not in defs["targets"]:
        raise ValueError(f"知らない仕事: {target}")
    num = den = 0.0
    blocked: list[str] = []
    warns: list[dict] = []
    for it in defs["items"]:
        r = it["req"].get(target)
        if not r:
            continue
        v = max(0, min(int(levels.get(it["id"], 0)), len(it["levels"]) - 1))
        den += r["weight"]
        num += r["weight"] * (min(v / r["goal"], 1.0) if r["goal"] else 1.0)
        if r["must"] and v < r["min"]:
            blocked.append(it["id"])
        elif v < r["goal"]:
            warns.append({"id": it["id"], "level": v, "goal": r["goal"], "warn": it["warn"]})
    ratio = num / den if den else 1.0
    verdict = "blocked" if blocked else _band(defs, ratio)
    by_id = {it["id"]: it for it in defs["items"]}
    warns.sort(key=lambda w: w["goal"] - w["level"], reverse=True)
    asks = [{"id": i, "ask": by_id[i]["ask"]} for i in blocked + [w["id"] for w in warns] if by_id[i].get("ask")]
    return {"target": target, "verdict": verdict, "ratio": round(ratio, 3), "blocked": blocked, "warnings": warns, "asks": asks}


def ladder(defs: dict, met: set[str] | list[str]) -> dict:
    """レベルの到達。met は満たした条件 id。レベルは下から順に、全部の条件を満たしたレベルまでを「届いた」とする
    （上のレベルの条件を先に満たしていても、下のレベルが欠けていれば届いたことにしない）。

    返す形: {reached: 届いたレベル（0 は未着手）, next: 次のレベル or None, missing: [{id, ja}]}"""
    met = set(met)
    reached = 0
    for rung in defs["ladder"]:
        if all(c["id"] in met for c in rung["conditions"]):
            reached = rung["level"]
        else:
            break
    nxt = next((r for r in defs["ladder"] if r["level"] == reached + 1), None)
    missing = [c for c in nxt["conditions"] if c["id"] not in met] if nxt else []
    return {"reached": reached, "next": nxt["level"] if nxt else None, "missing": missing}


def ready_line_ok(defs: dict, levels: dict[str, int], met: set[str] | list[str]) -> bool:
    """稼働 ready の承認を受信トレイに出してよいか。L3 まで届き、「開発を始める」の必須が欠けていないこと。
    警告（品質が出にくい）が残っていても出す。始めるかは事業主が決める（D7）"""
    return ladder(defs, met)["reached"] >= defs["ready_line_after"] and not judge(defs, levels, "dev")["blocked"]
