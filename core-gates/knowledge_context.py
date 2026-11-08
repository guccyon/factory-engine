#!/usr/bin/env python3
"""エージェントに渡す知見を選んで、作業の文脈のファイルにする（ADR 0024 の段取り 6、決定 7）。LLM を使わない。

選ぶ決まり（definitions/knowledge.yaml の context_for_agents と、ADR 0040 の axes・fit）:
  - status が active で、confidence が confirmed か settled（仮説は渡さない）
  - settled で決定論の担保（enforcement.level: deterministic）が効いているものは渡さない（担保で防げるので文脈を減らす）
  - review_by を過ぎたものは渡さない（再確認されるまで。日次の棚卸しが拾う）
  - acts_on が context（書いていなければ context）。engine・process 宛ては文脈に渡さない
  - 業務軸で候補を引く（ADR 0040 の決定 3）: scope.work が作業の業務のノード自身か祖先、または scope.artifacts が作業の成果物の型（親の型を含む）。
    work も artifacts もない今の知見は、移し替えまで scope.roles で引く（空ならすべて。implement は implementer とも書かれる）
  - ほかの軸を 1 本ずつ突き合わせて適応度を出す。一致は＋重み、知見に値がなければ 0、作業の値が分からなければ 重み×unknown、
    食い違いは exclude の軸なら外し、penalize の軸なら −重み。適応度＝合計＋業務のノードの深さ＋確かさ＋鮮度。下限より低いものは渡さない
  - 機密区分は product と public だけ（client は渡さない）。restricted の中身はここに来ない（git には id しかない）
  - 適応度の高い順（同じなら確認日の新しい順）に、件数と文字数の上限まで。上限で落としたものは結果の skipped に残す

渡すときは確かさと確認日を付ける。渡した id は結果の ids に、適応度と内訳は fit に出し、ワーカーが FACTORY_RESULT に載せ、dispatcher が events の
knowledge.used に残す（数えるのは測る側）。

使い方:
  core-gates/knowledge_context.py --repo . --role implement --out /work/context/knowledge.md [--today YYYY-MM-DD]
      [--work dev.build,dev.describe] [--set request_form=ticket --set purpose=refactor ...] [--files a.py,b/**]
  --work を省くと、役（--role）から definitions/work.yaml の from_agent_steps で業務を引く。製品の段階は製品の manifest から読む。
  標準出力に {"ids": [...], "skipped": {...}, "work": [...], "fit": {...}} の JSON を 1 行。渡すものがなければ --out は作らない。終了コードは常に 0（文脈が無くても作業は進める）
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import knowledge_check as kc  # noqa: E402

MAX_ITEMS = 12          # 仮置き。文脈を太らせない
MAX_CHARS = 12000
ROLE_ALIASES = {"implement": {"implement", "implementer"}}
CONFIDENCE_JA = {"confirmed": "確認済み", "settled": "確定"}
PASSED_CONFIDENTIALITY = {"product", "public"}
DEFS = Path(__file__).resolve().parent.parent / "definitions"

# 定義が読めないとき（製品の CI など）の既定値。正本は definitions/knowledge.yaml の axes と fit
DEFAULT_AXES = {
    "artifacts": {"weight": 2, "on_mismatch": "penalize"}, "roles": {"weight": 1, "on_mismatch": "penalize"},
    "purpose": {"weight": 2, "on_mismatch": "exclude"}, "product_stage": {"weight": 2, "on_mismatch": "exclude"},
    "request_form": {"weight": 2, "on_mismatch": "exclude"}, "code_origin": {"weight": 1, "on_mismatch": "exclude"},
    "contexts": {"weight": 1, "on_mismatch": "penalize"}, "change_types": {"weight": 1, "on_mismatch": "penalize"},
    "files": {"weight": 1, "on_mismatch": "penalize"},
}
DEFAULT_FIT = {"unknown": -0.25, "depth": 0.5, "confidence": {"settled": 1, "confirmed": 0}, "freshness_days": 90, "min_score": 0}
FRESH_BONUS = 0.5


def load_defs() -> tuple[dict, dict, dict]:
    """(axes, fit, work.yaml)。読めなければ既定値（work は空）"""
    axes, fit, work = dict(DEFAULT_AXES), dict(DEFAULT_FIT), {}
    try:
        kd = kc.load_yaml(DEFS / "knowledge.yaml") or {}
        for name, a in (kd.get("axes") or {}).items():
            if name != "work":
                axes[name] = {"weight": a.get("weight", 1), "on_mismatch": a.get("on_mismatch", "penalize")}
        fit.update({k: v for k, v in (kd.get("fit") or {}).items() if k in DEFAULT_FIT})
    except Exception:  # noqa: BLE001 定義が読めなくても文脈は作る
        pass
    try:
        work = kc.load_yaml(DEFS / "work.yaml") or {}
    except Exception:  # noqa: BLE001
        pass
    return axes, fit, work


def ancestors(node: str) -> list[str]:
    """dev.design.screen → [dev.design.screen, dev.design, dev]（成果物の PR.body → [PR.body, PR] も同じ）"""
    parts = str(node).split(".")
    return [".".join(parts[:i]) for i in range(len(parts), 0, -1)]


def as_list(v) -> list[str]:
    if v is None:
        return []
    return [str(x) for x in (v if isinstance(v, list) else [v])]


class Job:
    """この回の作業: 業務のノード・成果物の型・役・軸の値（ADR 0040 の決定 4）"""

    def __init__(self, role: str, work: list[str] | None = None, values: dict | None = None, files: list[str] | None = None,
                 defs: tuple[dict, dict, dict] | None = None):
        self.axes, self.fit, wdefs = defs or load_defs()
        self.role = role
        if work is None:
            work = as_list(((wdefs.get("from_agent_steps") or {}).get(role)))
        self.work = work
        nodes = wdefs.get("nodes") or {}
        self.artifacts = sorted({a for w in work for a in as_list((nodes.get(w) or {}).get("artifacts"))})
        self.values = {k: as_list(v) for k, v in (values or {}).items() if v not in (None, "", [])}
        self.values.setdefault("roles", [role])
        if files:
            self.values["files"] = list(files)
        self.covered = {a for w in work for a in ancestors(w)}
        self.covered_artifacts = {x for a in self.artifacts for x in ancestors(a)}


def candidate(k: dict, job: Job) -> tuple[str | None, int]:
    """業務軸で候補か（理由, 当たった業務のノードの深さ）。理由が None なら候補"""
    scope = k.get("scope") or {}
    work, arts = as_list(scope.get("work")), as_list(scope.get("artifacts"))
    if work or arts:
        hit = [w for w in work if w in job.covered]
        if hit:
            return None, max(len(w.split(".")) for w in hit)
        if any(a in job.covered_artifacts for a in arts):
            return None, 0
        return "other_work", 0
    roles = set(as_list(scope.get("roles")))   # 移し替えまでの今の知見
    if roles and not (roles & ROLE_ALIASES.get(job.role, {job.role})):
        return "other_role", 0
    return None, 0


def match_axis(axis: str, want: list[str], have: list[str]) -> bool:
    if axis == "files":
        return any(fnmatch.fnmatch(h, w) or h.startswith(w.rstrip("*").rstrip("/") + "/") for w in want for h in have)
    if axis == "roles":
        return any(h in want or bool(ROLE_ALIASES.get(h, {h}) & set(want)) for h in have)
    return bool(set(want) & set(have))


def score(k: dict, job: Job, depth: int, today: date) -> tuple[float | None, dict]:
    """適応度と内訳。食い違いで外すなら (None, 内訳)"""
    scope = k.get("scope") or {}
    parts: dict = {"depth": depth * job.fit["depth"]}
    total = parts["depth"]
    legacy = not (scope.get("work") or scope.get("artifacts"))
    for axis, a in job.axes.items():
        if axis == "artifacts" or (legacy and axis == "roles"):
            continue   # 成果物は候補を引くときに見た。今の知見の roles も同じ
        want = as_list(scope.get("paths") if axis == "files" and scope.get("files") is None else scope.get(axis))
        if not want:
            continue
        have = job.values.get(axis)
        w = float(a.get("weight", 1))
        if not have:
            parts[axis] = w * job.fit["unknown"]
        elif match_axis(axis, want, have):
            parts[axis] = w
        elif a.get("on_mismatch") == "exclude":
            parts[axis] = "mismatch"
            return None, parts
        else:
            parts[axis] = -w
        total += parts[axis]
    conf = float((job.fit.get("confidence") or {}).get(k.get("confidence"), 0))
    verified = kc.as_date(k.get("verified_at"))
    fresh = FRESH_BONUS if verified and verified >= today - timedelta(days=int(job.fit["freshness_days"])) else 0
    parts.update({"confidence": conf, "freshness": fresh})
    return round(total + conf + fresh, 3), parts


def eligible(k: dict, today: date) -> str | None:
    """渡すなら None、渡さないならその理由"""
    if k.get("status") != "active":
        return "not_active"
    conf = k.get("confidence")
    if conf not in CONFIDENCE_JA:
        return "hypothesis"
    if conf == "settled" and ((k.get("enforcement") or {}).get("level") == "deterministic"):
        return "enforced"
    rb = kc.as_date(k.get("review_by")) if k.get("review_by") else None
    if rb and rb < today:
        return "expired"
    if k.get("confidentiality") not in PASSED_CONFIDENTIALITY:
        return "confidential"
    if (k.get("acts_on") or "context") != "context":
        return "not_context"
    return None


def render(k: dict) -> str:
    scope = k.get("scope") or {}
    lines = [f"## {k['id']}（{CONFIDENCE_JA[k['confidence']]}、確認日 {k.get('verified_at')}）", "",
             " ".join(str(k.get("statement") or "").split())]
    where = []
    if scope.get("work"):
        where.append("業務 " + ", ".join(map(str, as_list(scope["work"]))))
    if scope.get("artifacts"):
        where.append("成果物 " + ", ".join(map(str, as_list(scope["artifacts"]))))
    if scope.get("files"):
        where.append("ファイル " + ", ".join(map(str, as_list(scope["files"]))))
    if scope.get("paths"):
        where.append("パス " + ", ".join(map(str, scope["paths"])))
    if scope.get("contexts"):
        where.append("コンテキスト " + ", ".join(map(str, scope["contexts"])))
    if where:
        lines += ["", "- 効く範囲: " + "、".join(where)]
    level = (k.get("enforcement") or {}).get("level")
    if level in ("checklist", "deterministic"):
        lines.append(f"- 担保: {level}（{'検査の観点にもなっている' if level == 'checklist' else '機械の検査もある'}）")
    return "\n".join(lines) + "\n"


HEADER = """# この製品の知見

工場の知見の委員会が採用した、この製品についての知見（`.factory-config/knowledge/`）のうち、確認済み以上で今の作業の役割に関係するものです。
確かさと確認日を見て使ってください。目の前のコードや specs/ と食い違うときは目の前の事実を優先し、食い違いを最後の出力の「## 補足」に知見の id を添えて 1 行で書いてください。
Issue と「約束」がこのファイルより優先します。

"""


def build(repo: Path, role: str, today: date, job: Job | None = None) -> tuple[str, list[str], dict, dict]:
    """(文脈の本文, 渡す id, 渡さなかった理由ごとの id, 渡す id ごとの適応度と内訳)"""
    job = job or Job(role, values=manifest_values(repo))
    items, _, _, errors = kc.load(repo)
    skipped: dict[str, list[str]] = {}
    picked = []
    for k in items:
        if not isinstance(k, dict) or not k.get("id"):
            continue
        why = eligible(k, today)
        depth = 0
        if not why:
            why, depth = candidate(k, job)
        if not why:
            sc, parts = score(k, job, depth, today)
            if sc is None:
                why = "mismatch"
            elif sc < float(job.fit["min_score"]):
                why = "low_fit"
            else:
                picked.append((sc, parts, k))
        if why:
            skipped.setdefault(why, []).append(k["id"])
    picked.sort(key=lambda t: (t[0], str(t[2].get("verified_at") or ""), t[2]["id"]), reverse=True)
    text, ids, fit = HEADER, [], {}
    for sc, parts, k in picked:
        block = render(k)
        if len(ids) >= MAX_ITEMS or len(text) + len(block) + 1 > MAX_CHARS:
            skipped.setdefault("over_limit", []).append(k["id"])
            continue
        text += block + "\n"
        ids.append(k["id"])
        fit[k["id"]] = {"score": sc, "parts": parts}
    if errors:
        skipped["unreadable"] = errors[:5]
    return text, ids, skipped, fit


def stage(repo_dir: Path | None, dest: Path, role: str, values: dict | None = None, today: date | None = None) -> dict:
    """エージェントの作業ディレクトリに、この回の業務で選んだ知見を dest/knowledge.md として置く（ADR 0040 の決定 7）。
    プランナー・窓口のように製品のリポジトリを丸ごと読む役が、知見のディレクトリを丸写しにしないためのもの。
    返り値は {ids, work, values, fit}（結果に載せ、dispatcher が knowledge.used に残す）。知見がなければ何も置かない"""
    empty = {"ids": [], "work": [], "values": {}, "fit": {}}
    if not repo_dir or not (repo_dir / ".factory-config" / "knowledge").is_dir():
        return empty
    try:
        job = Job(role, values={**manifest_values(repo_dir), **(values or {})})
        text, ids, _, fit = build(repo_dir, role, today or date.today(), job)
    except Exception:  # noqa: BLE001  知見がなくても役は動ける
        return empty
    if ids:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "knowledge.md").write_text(text, encoding="utf-8")
    return {"ids": ids, "work": job.work, "values": job.values, "fit": fit}


def manifest_values(repo: Path) -> dict:
    """製品の manifest から分かる軸の値（製品の段階）"""
    try:
        m = kc.load_yaml(repo / ".factory-config" / "manifest.yaml") or {}
        stage = (m.get("product_stage") or {}).get("stage") if isinstance(m.get("product_stage"), dict) else m.get("product_stage")
        return {"product_stage": stage} if stage else {}
    except Exception:  # noqa: BLE001
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--role", default="implement")
    ap.add_argument("--out", required=True)
    ap.add_argument("--today")
    ap.add_argument("--work", help="この回の業務のノード（カンマ区切り）。省くと役から引く")
    ap.add_argument("--set", action="append", default=[], metavar="AXIS=VALUE", help="軸の値（例 request_form=ticket）。くり返せる")
    ap.add_argument("--files", help="触る見込みのファイル（カンマ区切り）")
    a = ap.parse_args(argv)
    today = date.fromisoformat(a.today) if a.today else date.today()
    repo = Path(a.repo)
    if not (repo / kc.ROOT).is_dir():
        print(json.dumps({"ids": [], "skipped": {}}, ensure_ascii=False))
        return 0
    try:
        values = manifest_values(repo)
        for kv in a.set:
            key, _, val = kv.partition("=")
            if key and val:
                values[key] = [v for v in val.split(",") if v]
        work = [w for w in a.work.split(",") if w] if a.work else None
        files = [f for f in a.files.split(",") if f] if a.files else None
        job = Job(a.role, work=work, values=values, files=files)
        text, ids, skipped, fit = build(repo, a.role, today, job)
    except Exception as e:  # noqa: BLE001 文脈が作れなくても作業は止めない
        print(json.dumps({"ids": [], "error": repr(e)[:300]}, ensure_ascii=False))
        return 0
    if ids:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
    print(json.dumps({"ids": ids, "skipped": skipped, "work": job.work, "values": job.values, "fit": fit}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
