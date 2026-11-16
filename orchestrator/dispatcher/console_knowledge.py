"""操業コンソールの知見のページ（/knowledge）と、窓口の知見の問い合わせの中身（ADR 0061 の決定 8）。読むだけ。

事業部の知見（ドメインならではの開発の知見を含む）の正本は製品リポの .factory-config/knowledge/。
コンソールと窓口は GitHub を見ないので、supervisor が残す写し（knowledge_snapshot）を読む。
ドメインの業務知識（工場に依存しないもの）は製品の docs/ にあるので、ここからはそこへの道だけ出す。
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import knowledge_snapshot as ks

# テーマの名前（正本は definitions/learning_taxonomy.yaml の themes。食い違いは tests/test_console_knowledge.py で見る）
THEMES = {1: "利用者の体験", 2: "信頼性と性能", 3: "ドメインの理解", 4: "役割間の言葉と伝達", 5: "成果物の品質", 6: "工程の進め方", 7: "判断の精度", 8: "人の意図と好み"}
CONFIDENCE_JA = {"hypothesis": "仮説", "confirmed": "確認済み", "settled": "定着"}
STATUS_JA = {"draft": "下書き", "active": "有効", "stale": "要再確認", "superseded": "置き換え済み", "retired": "廃止"}
ACTS_ON_JA = {"product": "製品", "process": "工程", "engine": "Engine"}
LIMIT = 200


def _uses(cur, repo: str) -> dict[str, dict]:
    """知見ごとの、ワーカーに渡した回数（直近 60 日）と最後に渡した時刻"""
    cur.execute("""select id, count(*) as n, max(occurred_at) as last from (select jsonb_array_elements_text(payload->'ids') as id, occurred_at
                     from events where name='knowledge.used' and payload->>'repo'=%s and occurred_at >= now() - interval '60 days') u group by id""", (repo,))
    return {r["id"]: {"uses": r["n"], "last_used": r["last"].isoformat() if r["last"] else None} for r in cur.fetchall()}


def _row(i: dict, themes: dict, uses: dict) -> dict:
    u = uses.get(i["id"]) or {}
    return {**i, "theme_ja": themes.get(i.get("theme"), str(i.get("theme") or "")),
            "confidence_ja": CONFIDENCE_JA.get(i.get("confidence"), i.get("confidence") or ""),
            "status_ja": STATUS_JA.get(i.get("status"), i.get("status") or ""),
            "acts_on_ja": ACTS_ON_JA.get(i.get("acts_on"), i.get("acts_on") or ""),
            "uses": u.get("uses", 0), "last_used": u.get("last_used")}


def _match(i: dict, q: str) -> bool:
    if not q:
        return True
    hay = " ".join([i.get("id") or "", i.get("statement") or "", " ".join(i.get("invalidate_when") or []),
                    " ".join(f"{k}:{v}" for k, v in (i.get("scope") or {}).items())]).lower()
    return all(w in hay for w in q.lower().split())


def search(cur, repo: str | None = None, q: str = "", theme: int | None = None, confidence: str | None = None,
           status: str | None = None, limit: int = LIMIT, themes: dict | None = None) -> dict:
    """知見の一覧。製品・テーマ・確かさ・状態・言葉で絞る。status を渡さなければ廃止と置き換え済みは出さない"""
    themes = themes or THEMES
    products, items = [], []
    for snap in ks.latest(cur, repo):
        r = snap.get("repo") or ""
        uses = _uses(cur, r)
        products.append({"repo": r, "at": snap.get("at"), "count": len(snap.get("items") or []),
                         "docs_url": f"https://github.com/{r}/tree/HEAD/docs"})
        for i in snap.get("items") or []:
            if theme and i.get("theme") != theme:
                continue
            if confidence and i.get("confidence") != confidence:
                continue
            if status and status != "all" and i.get("status") != status:
                continue
            if not status and i.get("status") in ("retired", "superseded"):
                continue
            if not _match(i, q):
                continue
            items.append(_row({**i, "repo": r}, themes, uses))
    items.sort(key=lambda x: (x["repo"], x["id"]))
    return {"products": products, "themes": {str(k): v for k, v in themes.items()}, "confidence": CONFIDENCE_JA, "status": STATUS_JA,
            "total": len(items), "items": items[:limit]}


def read(cur, repo: str | None, kid: str, themes: dict | None = None) -> dict | None:
    """知見 1 件。repo がなければ全製品から最初に見つかったもの。どの振り返り・議決で出たかの道も付ける"""
    themes = themes or THEMES
    for snap in ks.latest(cur, repo):
        r = snap.get("repo") or ""
        for i in snap.get("items") or []:
            if i.get("id") == kid:
                d = _row({**i, "repo": r}, themes, _uses(cur, r))
                d["url"] = f"https://github.com/{r}/blob/HEAD/{i.get('path')}"
                d["snapshot_at"] = snap.get("at")
                return d
    return None


def get(cur, path: str) -> tuple[int, dict] | None:
    """/api/knowledge（?repo=&q=&theme=&confidence=&status=）と /api/knowledge/<K-NNNN>（?repo=）"""
    u = urlsplit(path)
    qs = {k: v[0] for k, v in parse_qs(u.query).items()}
    if u.path == "/api/knowledge":
        th = qs.get("theme")
        return 200, search(cur, qs.get("repo") or None, qs.get("q") or "", int(th) if th and th.isdigit() else None,
                           qs.get("confidence") or None, qs.get("status") or None)
    m = re.fullmatch(r"/api/knowledge/(K-\d{4,})", u.path)
    if m:
        d = read(cur, qs.get("repo") or None, m.group(1))
        return (200, d) if d else (404, {"error": "not found"})
    return None
