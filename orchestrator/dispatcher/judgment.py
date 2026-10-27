"""判断（Judgment）の記録（ADR 0006）。ワーカーの「## 仮決め」の節を分解して、根拠・解釈・スコアの 3 層で残す。

ワーカーの出力は外部入力（Issue 本文）の影響を受けるので、ここでは文字列として分解して保存するだけで、
シェルにもプロンプトにも渡さない。長さは切り詰める。
"""
from __future__ import annotations

import re

from psycopg.types.json import Jsonb

TAXONOMY_VERSION = "v1"   # definitions/judgments.yaml の taxonomy_version
CATEGORIES = {"term_boundary", "interface", "design_choice", "acceptance", "scope", "inspection"}
FIELDS = {"選んだ": "decision", "他の案": "alternatives", "根拠": "evidence", "解釈": "interpretation", "確信度": "confidence", "影響": "impact"}
MAX_ITEMS, MAX_LEN = 10, 1500


def parse_provisional(text: str) -> list[dict]:
    items: list[dict] = []
    cur: dict | None = None
    field = None
    for line in (text or "").splitlines():
        m = re.match(r"^###\s+([A-Za-z_]+)\s*[:：]\s*(.+)$", line.strip())
        if m:
            cat = m.group(1).strip().lower()
            cur = {"category": cat if cat in CATEGORIES else "unknown", "category_raw": m.group(1)[:40], "question": m.group(2).strip()[:MAX_LEN]}
            items.append(cur)
            field = None
            continue
        if cur is None:
            continue
        m = re.match(r"^\s*[-*]\s*([^:：]+)[:：]\s*(.*)$", line)
        if m and m.group(1).strip() in FIELDS:
            field = FIELDS[m.group(1).strip()]
            cur[field] = m.group(2).strip()[:MAX_LEN]
        elif field and line.strip():   # 複数行にわたる値は前の項目に足す
            cur[field] = (cur[field] + " " + line.strip())[:MAX_LEN]
    out = []
    for it in items[:MAX_ITEMS]:
        conf = None
        m = re.search(r"\d+(?:\.\d+)?", it.get("confidence", "") or "")
        if m:
            v = float(m.group(0))
            conf = max(0.0, min(1.0, v / 100 if v > 1 else v))
        it["score"] = {"self_reported": conf, "track_record": None, "basis": "self_report"}
        it.pop("confidence", None)
        out.append(it)
    return out


def record_provisional(cur, corr: str, job_id: int, pr_url: str | None, text: str, mode: str | None = None) -> list[dict]:
    items = parse_provisional(text)
    for i, it in enumerate(items, 1):
        payload = {**it, "stage": "provisional", "decided_by": "worker", "taxonomy_version": TAXONOMY_VERSION,
                   "mode": mode or "unknown", "pr_url": pr_url, "seq": i, "human_grade": None}
        cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Judgment',%s,%s,'implement',%s) returning id",
                    (corr, job_id, Jsonb(payload)))
        aid = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('judgment.provisional',%s,%s,%s,%s) on conflict (dedupe_key) do nothing",
                    (corr, job_id, Jsonb({"artifact_id": aid, "category": it["category"], "taxonomy_version": TAXONOMY_VERSION,
                                          "confidence": it["score"]["self_reported"], "pr_url": pr_url}), f"judgment.provisional:{job_id}:{i}"))
    return items


# ------------------------------------------------------------------ モード（ADR 0006 後半）
MODES = ("attended", "unattended")


def _in_window(now_hm: str, window: str) -> bool:
    start, end = window.split("-")
    return start <= now_hm < end if start <= end else (now_hm >= start or now_hm < end)


def current_mode(cur, default: str = "attended", schedule: str = "", tz: str = "Asia/Tokyo") -> tuple[str, str]:
    """(モード, 決め手) を返す。優先: factoryctl の切り替え（期限内）> 時間帯（MODE_SCHEDULE="unattended=22:00-08:00"）> 既定"""
    cur.execute("select payload, occurred_at from events where name='factory.mode_set' order by id desc limit 1")
    r = cur.fetchone()
    if r:
        until = r["payload"].get("until")
        cur.execute("select (%s::timestamptz is null or now() < %s::timestamptz) as active", (until, until))
        if cur.fetchone()["active"] and r["payload"].get("mode") in MODES:
            return r["payload"]["mode"], "factoryctl"
    if schedule:
        cur.execute("select to_char(now() at time zone %s, 'HH24:MI') as hm", (tz,))
        hm = cur.fetchone()["hm"]
        for part in schedule.split(","):
            mode, _, window = part.partition("=")
            if mode.strip() in MODES and window and _in_window(hm, window.strip()):
                return mode.strip(), "schedule"
    return (default if default in MODES else "attended"), "default"


# ------------------------------------------------------------------ judge の入力（制御プレーンの記録だけから組み立てる）
def track_record(cur) -> dict:
    """類型ごとの人の採点との一致率（judgment.graded）"""
    cur.execute("select payload->>'category' as c, count(*) as n, avg(case when payload->>'grade'='agree' then 1.0 else 0.0 end) as rate "
                "from events where name='judgment.graded' group by 1")
    return {r["c"]: {"n": r["n"], "agree_rate": float(r["rate"])} for r in cur.fetchall()}


def judge_input(cur, pr_url: str, mode: str, constraints: str = "") -> dict:
    cur.execute("select id, payload from artifacts where type='Judgment' and payload->>'stage'='provisional' and payload->>'pr_url'=%s order by id", (pr_url,))
    prov = [{"artifact_id": r["id"], **{k: r["payload"].get(k) for k in ("category", "question", "decision", "alternatives", "evidence", "interpretation", "score", "impact")}}
            for r in cur.fetchall()]
    cur.execute("select payload from events where name='gate.evaluated' and payload->>'pr_url'=%s order by id desc limit 1", (pr_url,))
    g = cur.fetchone()
    defects = (g["payload"].get("defects") if g else None) or []
    return {"pr_url": pr_url, "mode": mode, "provisional": prov, "defects": defects, "track_record": track_record(cur), "constraints": constraints}


def record_judged(cur, corr: str, job_id: int, pr_url: str, inp: dict, verdicts: dict, mode: str, mode_basis: str) -> dict:
    """judge の結果を Judgment（stage=judged）と judgment.judged に残す。返り値は起票・印に使う集計"""
    tr = inp.get("track_record", {})
    by_id = {p["artifact_id"]: p for p in inp.get("provisional", [])}
    adopted_defects, replaced, escalated, confidences = [], [], [], []
    rows = [("provisional", v) for v in verdicts.get("provisional", [])] + [("defect", v) for v in verdicts.get("defects", [])]
    for kind, v in rows:
        if kind == "provisional":
            src = by_id.get(v.get("artifact_id"), {})
            category, question = src.get("category", "unknown"), src.get("question")
        else:
            idx = v.get("index")
            src = inp["defects"][idx] if isinstance(idx, int) and 0 <= idx < len(inp.get("defects", [])) else {}
            category, question = "inspection", f"{src.get('rubric_id', '?')} {src.get('location', '')}".strip()
        conf = v.get("confidence")
        conf = max(0.0, min(1.0, float(conf))) if isinstance(conf, (int, float)) else None
        rec = tr.get(category)
        basis = "track_record" if rec and rec["n"] >= 10 else "self_report"
        payload = {"stage": "judged", "category": category, "taxonomy_version": TAXONOMY_VERSION, "question": question,
                   "verdict": v.get("verdict"), "decision": v.get("replacement") or (src.get("decision") if kind == "provisional" else v.get("verdict")),
                   "evidence": (v.get("evidence") or "")[:MAX_LEN], "interpretation": (v.get("interpretation") or "")[:MAX_LEN],
                   "impact": (v.get("impact") or "")[:MAX_LEN],
                   "score": {"self_reported": conf, "track_record": rec, "basis": basis},
                   "mode": mode, "mode_basis": mode_basis, "decided_by": "judge", "pr_url": pr_url, "subject": kind,
                   "source_artifact_id": v.get("artifact_id") if kind == "provisional" else None, "defect": src if kind == "defect" else None,
                   "human_grade": None}
        cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Judgment',%s,%s,'verify',%s) returning id",
                    (corr, job_id, Jsonb(payload)))
        aid = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('judgment.judged',%s,%s,%s)",
                    (corr, job_id, Jsonb({"artifact_id": aid, "category": category, "verdict": v.get("verdict"), "confidence": conf,
                                          "basis": basis, "mode": mode, "pr_url": pr_url})))
        if conf is not None:
            confidences.append(conf)
        if v.get("verdict") == "escalate":
            escalated.append({"artifact_id": aid, "category": category, "question": question})
        elif kind == "defect" and v.get("verdict") == "adopt":
            adopted_defects.append({**src, "judgment_id": aid})
        elif kind == "provisional" and v.get("verdict") == "replace":
            replaced.append({"artifact_id": aid, "category": category, "question": question, "replacement": v.get("replacement"),
                             "interpretation": payload["interpretation"]})
    return {"adopted_defects": adopted_defects, "replaced": replaced, "escalated": escalated,
            "min_confidence": min(confidences) if confidences else None, "count": len(rows)}
