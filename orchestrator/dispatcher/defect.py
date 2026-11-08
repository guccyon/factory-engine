"""Defect の記録（ADR 0028 の決定 5。P5a の defect_metrics）。形は definitions/defects.yaml。

見つけた所（上流 / PR / CI / QA / 診断 / 本番）にかかわらず、events の defect.recorded に 1 つの型で残す。
属性はすべて optional で、分からないものは null のまま残す（紐付け率は日報が数える）。指標の集計は Govern（health/daily/status_report.py）で、
ここは記録するだけ。記録の失敗で呼び出し元の流れ（起票・差し戻し・PR の見張り）は止めない。
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

# definitions/defects.yaml の stages と causes の id の写し（dispatcher のイメージには definitions/ を入れない。test_defect.py が食い違いを見る）
FOUND_IN = ("upstream", "pr", "ci", "qa", "diagnose", "prod")
CAUSED_IN = ("requirement", "design", "implement", "test", "environment")
FIELDS = ("repo", "release_id", "change", "caused_in", "viewpoint", "severity", "title", "reproduce", "issue")


def record(cur, found_in: str, key: str, source: str, *, correlation_id: str | None = None, job_id: int | None = None, **fields) -> bool:
    """Defect を 1 件残す。同じ found_in と key は 1 回だけ。返り値は新しく残したか"""
    if found_in not in FOUND_IN:
        raise ValueError(f"found_in は {'・'.join(FOUND_IN)} のどれか: {found_in}")
    caused = fields.get("caused_in")
    payload = {"key": str(key)[:300], "found_in": found_in, "source": source,
               **{k: fields.get(k) for k in FIELDS}, "caused_in": caused if caused in CAUSED_IN else None}
    if isinstance(payload["title"], str):
        payload["title"] = payload["title"][:200]
    if payload["reproduce"] is not None:
        payload["reproduce"] = [str(x)[:300] for x in list(payload["reproduce"])[:20]]
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('defect.recorded',%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id",
                (correlation_id, job_id, Jsonb(payload), f"defect.recorded:{found_in}:{payload['key']}"))
    return cur.fetchone() is not None


def safe_record(cur, found_in: str, key: str, source: str, log=None, **kw) -> bool:
    """record の失敗を呼び出し元に伝えない形（記録の失敗で起票や見張りを止めない）"""
    try:
        return record(cur, found_in, key, source, **kw)
    except Exception as e:  # noqa: BLE001
        if log:
            log("defect record failed", found_in, key, repr(e))
        return False
