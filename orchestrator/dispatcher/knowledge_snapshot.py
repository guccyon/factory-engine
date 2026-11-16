"""事業部の知見の写し（ADR 0061 の決定 8）。

知見の正本は製品リポの .factory-config/knowledge/K-NNNN.yaml。操業コンソールと窓口（desk）は GitHub を見ないので、
supervisor が製品ごとに 1 時間に 1 回ほど読み、変わっていたら制御プレーンに写しを残す（artifacts の KnowledgeSnapshot）。
コンソールの知見の一覧と、Claude からの問い合わせ（MCP の knowledge_search・knowledge_read）はこの写しを読む。

- 読むのは変わったファイルだけ（ディレクトリの一覧の sha を前の写しと比べる）
- 機密区分 client の知見は本文を写さない（ワーカーに渡さないのと同じ。knowledge_context）。id・テーマ・状態は写す
- 写しは中身が変わったときだけ足す（同じなら何も書かない）
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone

import yaml
from psycopg.types.json import Jsonb

ROOT = ".factory-config/knowledge"
TYPE = "KnowledgeSnapshot"
FILE_RE = re.compile(r"^K-\d{4,}\.ya?ml$")
INTERVAL_S = 3600
WITHHELD = "（機密区分 client のため本文は写していません。製品リポで見てください）"
TEXT_MAX = 1500

_last: dict[str, float] = {}   # 製品 → 最後に読んだ時刻（プロセスの中だけ。入れ直したら最初の周回で読む）


def due(repo: str, now: float) -> bool:
    return now - _last.get(repo, 0) >= INTERVAL_S


def latest(cur, repo: str | None = None) -> list[dict]:
    """製品ごとの最新の写し（payload）。repo を渡せばその製品だけ"""
    cur.execute("""select distinct on (payload->>'repo') payload from artifacts where type=%s and (%s::text is null or payload->>'repo'=%s)
                   order by payload->>'repo', id desc""", (TYPE, repo, repo))
    return [r["payload"] for r in cur.fetchall()]


def _s(v, n: int = TEXT_MAX) -> str:
    s = v if isinstance(v, str) else ("" if v is None else str(v))
    s = s.strip()
    return s if len(s) <= n else s[:n] + "…"


def _evidence(xs) -> list[str]:
    out = []
    for e in xs or []:
        if isinstance(e, dict):   # {case: C-0001}・{pr: 546} の 1 組
            out += [f"{k}:{v}" for k, v in e.items()]
        elif e:
            out.append(str(e))
    return out[:30]


def _scope(sc) -> dict:
    if not isinstance(sc, dict):
        return {}
    return {k: v for k, v in sc.items() if v not in (None, "", [], {})}


def item(doc: dict, path: str) -> dict | None:
    """知見 1 件を一覧・問い合わせに出す形にする。形が崩れていれば None（決定論の検査が別に見る）"""
    if not isinstance(doc, dict) or not doc.get("id"):
        return None
    client = doc.get("confidentiality") == "client"
    enf = doc.get("enforcement") or {}
    return {"id": str(doc["id"]), "path": path, "theme": doc.get("theme"), "kind": doc.get("kind"),
            "statement": WITHHELD if client else _s(doc.get("statement")),
            "invalidate_when": [] if client else [_s(x, 300) for x in doc.get("invalidate_when") or []],
            "confidence": doc.get("confidence"), "status": doc.get("status"),
            "verified_at": str(doc.get("verified_at") or ""), "review_by": str(doc.get("review_by") or ""),
            "confidentiality": doc.get("confidentiality"), "withheld": client,
            "scope": _scope(doc.get("scope")), "acts_on": doc.get("acts_on"),
            "evidence": _evidence(doc.get("evidence")), "decided_by": doc.get("decided_by") or doc.get("decision"),
            "enforcement": enf.get("level") if isinstance(enf, dict) else None,
            "related": doc.get("related") or [],
            "supersedes": doc.get("supersedes"), "superseded_by": doc.get("superseded_by")}


def fetch(gh, repo: str, prev: dict | None = None) -> tuple[list[dict], dict[str, str]]:
    """製品の知見を読む。前の写しと sha が同じファイルは読み直さない。読めなければ例外"""
    listing = gh("GET", f"repos/{repo}/contents/{ROOT}") or []
    if not isinstance(listing, list):
        raise RuntimeError(f"{ROOT} の一覧が読めない")
    prev = prev or {}
    old_sha, old_items = prev.get("shas") or {}, {i["path"]: i for i in prev.get("items") or []}
    items, shas = [], {}
    for f in sorted((x for x in listing if isinstance(x, dict) and x.get("type") == "file" and FILE_RE.match(x.get("name") or "")),
                    key=lambda x: x["name"]):
        path, sha = f["path"], f.get("sha") or ""
        shas[path] = sha
        if old_sha.get(path) == sha and path in old_items:
            items.append(old_items[path])
            continue
        got = gh("GET", f"repos/{repo}/contents/{path}") or {}
        try:
            doc = yaml.safe_load(base64.b64decode(got.get("content") or "").decode("utf-8"))
        except (ValueError, yaml.YAMLError):
            doc = None
        it = item(doc, path)
        if it:
            items.append(json.loads(json.dumps(it, ensure_ascii=False, default=str)))   # YAML の日付などを文字列に
    return items, shas


def refresh(cur, gh, repo: str, now: datetime | None = None) -> bool:
    """読み直して、変わっていれば写しを足す。足したら True"""
    cur_list = latest(cur, repo)
    prev = cur_list[0] if cur_list else None
    items, shas = fetch(gh, repo, prev)
    if prev and prev.get("shas") == shas:
        return False
    at = (now or datetime.now(timezone.utc)).isoformat()
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values (%s,%s,'supervisor',%s)",
                (TYPE, f"knowledge:{repo}", Jsonb({"repo": repo, "at": at, "items": items, "shas": shas})))
    return True
