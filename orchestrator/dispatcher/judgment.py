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
