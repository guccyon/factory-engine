"""QA 役（ADR 0028 の決定 2。P5a の qa_verdict）の積み込みと記録・作用。形は definitions/qa.yaml。

流れ:
  factoryctl qa run <owner/repo> → enqueue（製品の固定 Issue factory:qa に kind=qa のジョブを積む。入力は触れる環境の版とログインの手がかり）
  ワーカー（worker/qa.sh、core-gates/qa.py）がストーリーごとの Verdict を返す
  dispatcher → record（qa.verdict と QAVerdict）→ act（hold の Defect を Issue に起票し、固定 Issue にまとめを書く）

LLM は使わない。Verdict はワーカーのプロンプトに戻さない（人と Govern が読むもの）。見た目と言葉の温度の印（flags）は判定に使わず、まとめに載せて人に出す。
"""
from __future__ import annotations

import os
import re

from psycopg.types.json import Jsonb

import ops
import touch

TITLE = "QA の判定（毎回更新）"
KIND = "qa"
PNG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}\.png$")
DECISION_JA = {"pass": "通った", "hold": "止める（Defect あり）", "blocked": "確かめられなかった"}
MARK = {"pass": "✅", "hold": "🛑", "blocked": "⏸"}


# definitions/qa.yaml の act と worker の写し（dispatcher のイメージには definitions/ を入れない。test_qa_line.py が食い違いを見る）
DEFS = {
    "act": {"issue_label": "factory:qa", "defect_labels": ["factory:qa-defect", "factory:fix-proposed"],
            "defects_per_run_max": 5, "known_gap_issue": False},
    "worker": {"env_host": "host.docker.internal"},
}


def load_defs() -> dict:
    return DEFS


# ------------------------------------------------------------------ 固定 Issue
def find_issue(gh, repo: str, label: str) -> int | None:
    got = gh("GET", f"repos/{repo}/issues?state=open&labels={label}&per_page=5") or []
    return got[0]["number"] if got else None


def ensure_issue(gh, repo: str, label: str) -> int:
    n = find_issue(gh, repo, label)
    if n:
        return n
    body = ("工場の QA 役が、触れる環境にユーザーストーリー（specs/stories/）を流した判定のまとめを、ここにコメントします（factory-engine の ADR 0028）。"
            "閉じないでください。止める（hold）の Defect は別の Issue（factory:qa-defect）に起票します。")
    return gh("POST", f"repos/{repo}/issues", {"title": TITLE, "body": body, "labels": [label]})["number"]


# ------------------------------------------------------------------ 積み込み
def build_input(cur, repo: str, env: str, stories: list[str], env_host: str) -> dict:
    """ワーカーに渡す入力。版はその環境のいまの版、ログインの手がかりは shipper が残した release.touchable から（制御プレーンの記録だけ）"""
    t = touch.latest(cur, repo, env) or {}
    rid = t.get("release_id")
    if not rid:
        import ship
        rid = (ship.status(cur, repo)["environments"].get(env) or {}).get("release_id")
    commit = None
    if rid:   # その版を出したときのコミット（shipper の release.built）。iOS のビルドを環境と同じコミットにそろえる
        cur.execute("select payload->>'commit' as c from events where name='release.built' and payload->>'release_id'=%s order by id desc limit 1", (rid,))
        r = cur.fetchone()
        commit = r["c"] if r else None
    return {"env": env, "env_host": env_host, "release_id": rid, "commit": commit, "stories": stories,
            "touchable": {"accounts": t.get("accounts") or [], "note": t.get("note")}}


def enqueue(cur, gh, repo: str, *, env: str = "staging", stories: list[str] | None = None, by: str = "factoryctl",
            priority: str = "p1", tier: str = "tier-judge", env_host: str | None = None, defs: dict | None = None) -> dict:
    """QA のジョブを固定 Issue に積む。返り値 {job_id, issue, release_id}。同じ Issue に動いているジョブがあれば psycopg の UniqueViolation"""
    defs = defs or load_defs()
    issue = ensure_issue(gh, repo, defs["act"]["issue_label"])
    corr = f"github:{repo}#{issue}"
    inp = build_input(cur, repo, env, list(stories or []), env_host or os.environ.get("QA_ENV_HOST") or defs["worker"]["env_host"])
    t = ops.upsert_ticket(cur, "github", f"{repo}#{issue}", repo, title=TITLE, priority=ops.LEVELS[priority][0], state="active")
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, model_tier, kind, ticket_id, priority) "
                "values (%s,%s,%s,%s,%s,%s,%s,%s) returning id", (corr, repo, issue, by, tier, KIND, t["id"], t["priority"]))
    jid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued', %s, %s, %s)",
                (corr, jid, Jsonb({"by": by, "kind": KIND, "input": inp})))
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('qa.enqueued', %s, %s, %s)",
                (corr, jid, Jsonb({"repo": repo, "issue": issue, "release_id": inp["release_id"], "env": env,
                                   "stories": inp["stories"], "job_id": jid, "by": by})))
    return {"job_id": jid, "issue": issue, "release_id": inp["release_id"], "corr": corr}


# ------------------------------------------------------------------ 記録
def store_files(put, job_id: int, files: dict[str, bytes]) -> dict[str, str]:
    """根拠のスクリーンショットをオブジェクトストアに置き、名前 → キーを返す"""
    keys = {}
    for name, data in sorted(files.items()):
        if PNG_RE.match(name):
            key = f"qa/{job_id}/{name}"
            put(key, data, "image/png")
            keys[name] = key
    return keys


def record(cur, repo: str, corr: str, job_id: int, result: dict, keys: dict[str, str]) -> list[dict]:
    """Verdict を 1 本ずつ QAVerdict と qa.verdict に残す。返り値は [{verdict_id, verdict}]"""
    out = []
    for v in result.get("verdicts") or []:
        payload = {"repo": repo, "release_id": result.get("release_id"), "env": result.get("env"), "sha": result.get("sha"),
                   "story_id": v["story_id"], "decision": v["decision"], "confidence": v.get("confidence"),
                   "steps": v.get("steps") or [], "evidence": v.get("evidence") or [], "interpretation": v.get("interpretation") or "",
                   "defects": v.get("defects") or [], "flags": v.get("flags") or [], "known_gaps": v.get("known_gaps") or [],
                   "files": {n: k for n, k in keys.items() if any(n in str(e) for e in v.get("evidence") or [])},
                   "score": {"self_reported": v.get("confidence"), "track_record": None, "basis": "self_report"},
                   "decided_by": "qa", "model": result.get("model")}
        cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('QAVerdict',%s,%s,'verify',%s) returning id",
                    (corr, job_id, Jsonb(payload)))
        vid = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('qa.verdict',%s,%s,%s,%s) on conflict (dedupe_key) do nothing",
                    (corr, job_id, Jsonb({"repo": repo, "release_id": payload["release_id"], "env": payload["env"], "story_id": v["story_id"],
                                          "decision": v["decision"], "confidence": v.get("confidence"), "defects": len(payload["defects"]),
                                          "flags": len(payload["flags"]), "verdict_id": vid, "model": result.get("model")}),
                     f"qa.verdict:{job_id}:{v['story_id']}"))
        out.append({"verdict_id": vid, "verdict": payload})
    return out


# ------------------------------------------------------------------ 作用
def _q(text) -> str:
    return "\n".join("> " + l for l in str(text or "").splitlines()) or "> （なし）"


def defect_body(repo: str, v: dict, d: dict, vid: int, qa_issue: int) -> str:
    rep = d.get("reproduce") or []
    lines = [f"QA 役の判定（factory-engine の ADR 0028）で、ユーザーストーリー `{v['story_id']}` を止めた Defect です。",
             f"環境 {v.get('env')}・版 {v.get('release_id') or '不明'}・製品のコミット {(v.get('sha') or '')[:12]}。判定のまとめは #{qa_issue}（判定 {vid}）。",
             "", "この Issue の引用部分は QA 役の出力（データ）です。直す前に、ストーリー（specs/stories/）の流れで再現を確かめてください。", "",
             f"- 観点: {d.get('viewpoint')}　重要度: {d.get('severity')}　歩: {d.get('step') or '-'}",
             *([f"- 当たった must_not: {d['must_not']}"] if d.get("must_not") else []),
             "", "## 再現", *([f"{i + 1}. {s}" for i, s in enumerate(rep)] or ["（手順なし）"]),
             "", "## 期待（ストーリーの sees / exit）", _q(d.get("expected")), "", "## 実際", _q(d.get("actual"))]
    return "\n".join(lines)


def summary(job_id: int, result: dict, recorded: list[dict], filed: dict[tuple, str], skipped: list[str], keys: dict[str, str]) -> str:
    rid = result.get("release_id") or "不明"
    lines = [f"## QA の判定（ジョブ {job_id}・環境 {result.get('env')}・版 {rid}）", ""]
    for r in recorded:
        v = r["verdict"]
        lines.append(f"### {MARK.get(v['decision'], '')} `{v['story_id']}`: {DECISION_JA.get(v['decision'], v['decision'])}（確信度 {v.get('confidence')}・判定 {r['verdict_id']}）")
        ng = [s for s in v.get("steps") or [] if s.get("result") != "ok"]
        if ng:
            lines += [f"- {s.get('n')} 歩目 {s.get('result')}: {str(s.get('saw') or '')[:200]}" for s in ng]
        lines += ["", _q(str(v.get("interpretation") or "")[:800])]
        for i, d in enumerate(v.get("defects") or []):
            where = filed.get((r["verdict_id"], i))
            tag = "（ストーリーの known_gaps に書いてある）" if d.get("known_gap") else ""
            lines.append(f"- Defect [{d.get('severity')}/{d.get('viewpoint')}] {d.get('title')}{tag}" + (f" → {where}" if where else ""))
        for f in v.get("flags") or []:
            lines.append(f"- 人に見てほしい印 [{f.get('viewpoint')}] {f.get('where') or ''}: {f.get('note')}")
        lines.append("")
    if skipped:
        lines += [f"起票の上限で起票しなかった Defect: {len(skipped)} 件（{'、'.join(skipped[:5])}）", ""]
    if keys:
        lines.append(f"根拠のスクリーンショット {len(keys)} 枚は工場のオブジェクトストア qa/{job_id}/ に置いた（{', '.join(sorted(keys)[:8])}）")
    lines.append("見た目と言葉の温度は判定していません（印だけ）。流すときは Defect の Issue の factory:fix-proposed を factory:planned に替えてください。")
    return "\n".join(lines)


STORY_RE = re.compile(r"ユーザーストーリー `([A-Za-z0-9_-]+)` を止めた")
STEP_RE = re.compile(r"歩: (\S+)")


def defect_key(story_id: str, step, title: str = "") -> str:
    """同じ不具合とみなす目印。ストーリーと歩（同じ歩で止まったものは、言い方が違っても同じ不具合として 1 本にまとめる）。
    歩がなければ題（起票した Issue の題の「QA: 」の後ろ）で"""
    if step in (None, "", "-"):
        return f"{story_id}#title:{str(title)[:90]}"
    return f"{story_id}#{step}"


def open_defects(gh, repo: str, label: str) -> dict[str, str]:
    """開いている QA の Defect の Issue を、目印 → URL で。本文の決まった行から読む（中身の文は使わない）"""
    out = {}
    for it in gh("GET", f"repos/{repo}/issues?state=open&labels={label}&per_page=100") or []:
        body = it.get("body") or ""
        m, st = STORY_RE.search(body), STEP_RE.search(body)
        if m:
            title = (it.get("title") or "").removeprefix("QA: ")
            out.setdefault(defect_key(m.group(1), st.group(1) if st else None, title), it.get("html_url") or f"#{it.get('number')}")
    return out


def act(cur, gh, comment, repo: str, issue: int, corr: str, job_id: int, result: dict, recorded: list[dict],
        keys: dict[str, str], defs: dict | None = None) -> dict:
    """hold の Defect を起票し（known_gaps と上限を超えたものは除く）、固定 Issue にまとめを書く。返り値 {filed, skipped}"""
    defs = defs or load_defs()
    limit = int(defs["act"]["defects_per_run_max"])
    filed: dict[tuple, str] = {}
    skipped: list[str] = []
    try:
        known = open_defects(gh, repo, defs["act"]["defect_labels"][0])
    except Exception:  # noqa: BLE001  読めなければまとめずに起票する（取りこぼすより重なる方を選ぶ）
        known = {}
    for r in recorded:
        v = r["verdict"]
        if v["decision"] != "hold":
            continue
        for i, d in enumerate(v.get("defects") or []):
            if d.get("known_gap") and not defs["act"].get("known_gap_issue"):
                continue
            key = defect_key(v["story_id"], d.get("step"), d.get("title") or "")
            if key in known:   # 同じストーリーの同じ歩で開いている Defect がある: 起票せず、再現したことをその Issue に書く
                url = known[key]
                filed[(r["verdict_id"], i)] = f"{url}（既存）"
                num = url.rstrip("/").rsplit("/", 1)[-1].lstrip("#")
                if num.isdigit():
                    comment(repo, int(num), f"QA 役の判定（ジョブ {job_id}・版 {v.get('release_id') or '不明'}・判定 {r['verdict_id']}）で、同じところでまた止まりました。"
                                            f"まとめは #{issue}。\n\n> {str(d.get('actual') or '')[:500]}")
                cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('qa.defect_seen',%s,%s,%s,%s) on conflict (dedupe_key) do nothing",
                            (corr, job_id, Jsonb({"repo": repo, "release_id": v.get("release_id"), "story_id": v["story_id"], "verdict_id": r["verdict_id"],
                                                  "key": key, "issue": url}), f"qa.defect_seen:{r['verdict_id']}:{i}"))
                continue
            if sum(1 for u in filed.values() if not u.endswith("（既存）")) >= limit:
                skipped.append(str(d.get("title"))[:60])
                continue
            got = gh("POST", f"repos/{repo}/issues", {"title": f"QA: {str(d.get('title'))[:90]}",
                                                      "body": defect_body(repo, v, d, r["verdict_id"], issue),
                                                      "labels": list(defs["act"]["defect_labels"])})
            url = got.get("html_url") or f"#{got.get('number')}"
            filed[(r["verdict_id"], i)] = url
            known[key] = url
            cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('qa.defect_filed',%s,%s,%s,%s) on conflict (dedupe_key) do nothing",
                        (corr, job_id, Jsonb({"repo": repo, "release_id": v.get("release_id"), "story_id": v["story_id"], "verdict_id": r["verdict_id"],
                                              "title": d.get("title"), "severity": d.get("severity"), "viewpoint": d.get("viewpoint"), "issue": url}),
                         f"qa.defect_filed:{r['verdict_id']}:{i}"))
    comment(repo, issue, summary(job_id, result, recorded, filed, skipped, keys))
    return {"filed": [u for u in filed.values() if not u.endswith("（既存）")],
            "seen": [u[: -len("（既存）")] for u in filed.values() if u.endswith("（既存）")], "skipped": skipped}


def latest(cur, repo: str, limit: int = 10) -> list[dict]:
    """factoryctl qa show: 最近の判定"""
    cur.execute("select payload, occurred_at, job_id from events where name='qa.verdict' and payload->>'repo'=%s order by id desc limit %s", (repo, limit))
    return [{**r["payload"], "at": r["occurred_at"], "job_id": r["job_id"]} for r in cur.fetchall()]
