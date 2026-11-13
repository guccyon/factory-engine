"""スパイクのレーン（ADR 0055。Gen1 のプロトタイプ）。

  着手判定（planner.act）が spike → Issue に factory:spike → dispatcher が spike のジョブに積む（worker/spike.sh → core-gates/spike.py、System Architect）
  → ここで:
    - 設計書（DesignDoc）を artifacts に残し、Issue のコメント（印 <!-- factory:designdoc -->）に書く
    - 束ねた Issue（同じ原因の開いている不具合）に「#N の方針でまとめて直す」と書き、factory:bundled を付ける（流れている最中のものには触らない）
    - 出口: 体験や仕様が変わる方針か、確信度が低ければ factory:spike-human（受信トレイ。事業側の判断）。そうでなければ factory:plan に戻し、
      プランナーが設計書に従って着手判定をし直す

ジョブの入力（input_for）は、プランナーが渡した「スパイクで決めること」（最後の plan.spiked）と、開いている QA の不具合（planner.open_defects）、
本文で触れている不具合の本文。制御プレーンの記録と GitHub の読み取りで組み立てる。定義は definitions/planner.yaml の spike と labels。
"""
from __future__ import annotations

import re

from psycopg.types.json import Jsonb

# definitions/planner.yaml の labels・spike と同じ値（dispatcher のコンテナには definitions/ がないので写す。食い違いはテストで見る）
TRIGGER_LABEL, HUMAN_LABEL, BUNDLED_LABEL = "factory:spike", "factory:spike-human", "factory:bundled"
PLAN_LABEL, RUNNING_LABEL, FAILED_LABEL = "factory:plan", "factory:running", "factory:failed"
MARKER = "<!-- factory:designdoc -->"
HUMAN_BELOW_CONFIDENCE = 0.6
# 束ねてよい（まだ流れていない）Issue のラベル。これ以外の工場のラベル（running・queued・pr-open など）が付いていれば触らない
IDLE_LABELS = {"factory:plan", "factory:fix-proposed", "factory:needs-info", "factory:qa-defect", "factory:spike"}
KIND_JA = {"fix": "原因を直す", "revert": "原因の変更を戻す", "redesign": "作り直す"}
LAYER_JA = {"db": "DB", "contract": "契約", "backend": "backend", "web": "Web", "mobile": "モバイル", "docs": "文書", "other": "その他"}
ISSUE_REF = re.compile(r"#(\d+)")


def questions(cur, corr: str) -> list[str]:
    cur.execute("select payload from events where name='plan.spiked' and correlation_id=%s order by id desc limit 1", (corr,))
    r = cur.fetchone()
    return list((r or {}).get("payload", {}).get("questions") or [])


def input_for(cur, gh, repo: str, issue: int, corr: str) -> dict:
    """spike のジョブの標準入力。材料がなくても方針は考えられるので、失敗は空で続ける"""
    import planner
    out = {"questions": questions(cur, corr), "open_defects": [], "defect_bodies": {}}
    try:
        out["open_defects"] = planner.open_defects(cur, repo, corr)
        body = (gh("GET", f"repos/{repo}/issues/{issue}") or {}).get("body") or ""
        known = {d["issue"] for d in out["open_defects"]}
        for n in sorted({int(m) for m in ISSUE_REF.findall(body)} & known)[:10]:
            out["defect_bodies"][str(n)] = ((gh("GET", f"repos/{repo}/issues/{n}") or {}).get("body") or "")[:4000]
    except Exception:  # noqa: BLE001, S110
        pass
    return out


def record(cur, repo: str, issue: int, corr: str, job_id: int, result: dict) -> int:
    dd = result.get("designdoc") or {}
    payload = {"repo": repo, "issue": issue, "designdoc": dd, "versions": result.get("versions") or {}, "model": result.get("model"),
               "cost_usd": result.get("cost_usd"), "num_turns": result.get("num_turns")}
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('DesignDoc',%s,%s,'spike',%s) returning id",
                (corr, job_id, Jsonb(payload)))
    did = cur.fetchone()["id"]
    a = dd.get("approach") or {}
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('spike.decided',%s,%s,%s)",
                (corr, job_id, Jsonb({"designdoc_id": did, "repo": repo, "issue": issue, "kind": a.get("kind"), "bundle": dd.get("bundle") or [],
                                      "experience_change": bool(dd.get("experience_change")), "confidence": dd.get("confidence")})))
    return did


def to_human(dd: dict) -> list[str]:
    """出口で人に回す理由（空なら回さない）"""
    why = []
    if dd.get("experience_change"):
        why.append("体験や仕様が変わる")
    conf = dd.get("confidence")
    if not isinstance(conf, (int, float)) or conf < HUMAN_BELOW_CONFIDENCE:
        why.append("確信度が低い")
    if not (dd.get("approach") or {}).get("description"):
        why.append("方針が書かれていない")
    return why


def comment_body(job_id: int, did: int, dd: dict, bundled: list[int], human: list[str]) -> str:
    a, c = dd.get("approach") or {}, dd.get("cause") or {}
    lines = [MARKER, f"## 設計書（スパイク・System Architect、ジョブ {job_id}）", "", dd.get("summary") or "", "",
             "### 原因", c.get("statement") or "-"]
    lines += [f"- {e}" for e in c.get("evidence") or []]
    lines += ["", f"### 方針: {KIND_JA.get(a.get('kind'), a.get('kind') or '-')}", a.get("description") or "-"]
    if a.get("revert_of"):
        lines.append(f"- 戻す変更: {a['revert_of']}")
    if a.get("layers"):
        lines.append("- 触る層: " + "・".join(LAYER_JA.get(x, x) for x in a["layers"]))
    if a.get("files"):
        lines.append("- 主なファイル: " + "、".join(f"`{f}`" for f in a["files"][:15]))
    if dd.get("answers"):
        lines += ["", "### プランナーの問いへの答え"] + [f"- {x.get('question')}: {x.get('answer')}" for x in dd["answers"]]
    if dd.get("alternatives"):
        lines += ["", "### とらなかった方針"] + [f"- {x.get('approach')}: {x.get('why_not')}" for x in dd["alternatives"]]
    if bundled:
        lines += ["", "### まとめて直す不具合", " ".join(f"#{n}" for n in bundled) + (f"（{dd.get('bundle_why')}）" if dd.get("bundle_why") else "")]
    if dd.get("experience_change"):
        lines += ["", "### 体験・仕様の変化", dd.get("experience_note") or "-"]
    if dd.get("risks"):
        lines += ["", "### 気をつけること"] + [f"- {r}" for r in dd["risks"]]
    if dd.get("open_questions"):
        lines += ["", "### 決められなかったこと"] + [f"- {q}" for q in dd["open_questions"]]
    lines += ["", f"確信度: {dd.get('confidence')}", ""]
    if human:
        lines.append(f"**人の確認を待ちます**（{'・'.join(human)}）。方針でよければラベル `{HUMAN_LABEL}` を `{PLAN_LABEL}` に替えてください。"
                     "直すところがあればコメントに書いてから替えると、プランナーが読みます。")
    else:
        lines.append("この方針で着手判定をし直します（プランナーは方針を決め直さず、経路と見積もりと時期を決めます）。")
    lines.append(f"\n<sub>DesignDoc {did}</sub>")
    return "\n".join(lines)


def bundle(gh, swap_label, comment, repo: str, issue: int, numbers: list[int]) -> list[int]:
    """同じ原因の不具合に印を付ける。まだ流れていない開いた Issue だけ"""
    done = []
    for n in sorted({int(x) for x in numbers if isinstance(x, int) or str(x).isdigit()} - {issue})[:20]:
        try:
            it = gh("GET", f"repos/{repo}/issues/{n}") or {}
        except Exception:  # noqa: BLE001
            continue
        labels = {lb.get("name") for lb in it.get("labels") or [] if isinstance(lb, dict)}
        if it.get("state") != "open" or "pull_request" in it or {x for x in labels if x.startswith("factory:") and not x.startswith("factory:priority:")} - IDLE_LABELS:
            continue
        swap_label(repo, n, [PLAN_LABEL, "factory:fix-proposed", TRIGGER_LABEL], [BUNDLED_LABEL])
        comment(repo, n, f"同じ原因の不具合として、#{issue} の方針（スパイクの設計書）でまとめて直します。この Issue は単独では流さず、#{issue} の PR で閉じます（ADR 0055）。")
        done.append(n)
    return done


def act(cur, repo: str, issue: int, corr: str, job_id: int, did: int, result: dict, swap_label, comment, gh) -> str:
    """設計書を Issue に書き、束ね、出口のラベルを付ける。返り値は出口（human か plan）"""
    import inbox
    dd = result.get("designdoc") or {}
    bundled = bundle(gh, swap_label, comment, repo, issue, dd.get("bundle") or [])
    human = to_human(dd)
    comment(repo, issue, comment_body(job_id, did, dd, bundled, human))
    if human:
        swap_label(repo, issue, [RUNNING_LABEL], [HUMAN_LABEL])
        try:
            inbox.request(cur, f"spike:{repo}#{issue}:{did}", "spike_exit", "po", f"{repo}#{issue} の直し方の方針を確かめる（{'・'.join(human)}）",
                          url=f"https://github.com/{repo}/issues/{issue}", action=f"方針でよければラベル {HUMAN_LABEL} を {PLAN_LABEL} に替える",
                          correlation_id=corr)
        except Exception:  # noqa: BLE001, S110  受信トレイがなくてもラベルとコメントで人に届く
            pass
        return "human"
    swap_label(repo, issue, [RUNNING_LABEL], [PLAN_LABEL])
    return "plan"
