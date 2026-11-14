"""オンボーディングの判定の記録と、その見せ方（ADR 0045 の追記「操業コンソールに出すもの」、作る順 2〜5）。

- 記録: オンボーディングの道具（と建設者）が factoryctl readiness record で、製品ごとの今の粒度と根拠・満たした条件・
  技術スタックと外部サービスの下書き・取り込み元を記録する。判定（仕事ごとの判定・レベルの到達・稼働 ready を出してよいか）は
  記録のときに readiness.py で出し、記録と一緒に残す（あとで定義の線を変えても、その時の判定が履歴に残る）
- 置き場: 表は足さず、events の onboarding.assessed に置く（状態は events に置く。ADR 0020。DB の変更なしで入れられる）。
  製品ごとにいちばん新しい記録が今の値で、古い記録が推移
- 見せ方: 操業コンソールの製品のページ（products_page.py）・ホームのカード・設定の「サービスとの連携」と、受信トレイの
  「準備のお願い」「稼働 ready の承認」（inbox.py が items() を集め、答えは answer() に渡す）
- 鍵・トークンの値は受け取らない・出さない。外部サービスは検証用の環境の「あり・無い・不明・要らない」、取り込み元は状態と期限だけ
- 製品は記録に書かれた製品の名前（owner/repo）で束ねるだけ（ジョブとチケットを製品に付けるのは Gen2.0）
"""
from __future__ import annotations

import re
from datetime import datetime

from psycopg.types.json import Jsonb

import readiness

EVENT = "onboarding.assessed"
APPROVED = "onboarding.ready_approved"
DEFERRED = "onboarding.ready_deferred"
ASK_ANSWERED = "onboarding.ask_answered"
PRODUCT = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")

# 製品に組み込まれた外部サービス（直接のサービス）の、検証用の環境の有無と工場の環境での扱い（08_console-product.md の 3）
SANDBOX = {"yes": "あり", "no": "無い", "unknown": "不明", "na": "要らない"}
HANDLING = {"connect": "検証用の環境につなぐ", "mock": "模擬で置き換える", "skip": "送らない", "unavailable": "使えない"}
# 工場の生産に使う間接のサービスの取り込み元（鍵番ができるまでは、取り込みの形と状態だけを出す）
SOURCE_KIND = {"oauth": "OAuth", "export": "書き出したファイル", "account": "工場のアカウント"}
SOURCE_STATE = {"connected": "つながっている", "expired": "期限切れ", "revoked": "取り消し", "export": "書き出しファイル"}
MAX_TEXT = 300

# 記録に受け取る欄（ほかの欄は捨てる。鍵の値を誤って入れても残さない）
REPO_FIELDS = ("repo", "role", "languages", "frameworks", "tests", "note")
SERVICE_FIELDS = ("name", "role", "repos", "sandbox", "handling", "note")
SOURCE_FIELDS = ("service", "use", "kind", "scope", "state", "granted_by", "expires_at", "last_used_at", "received_at", "note")
EVIDENCE_FIELDS = ("tool", "commit", "by", "note", "at")


def _text(v, n: int = MAX_TEXT) -> str:
    return str(v if v is not None else "")[:n]


def _texts(v) -> list[str]:
    if v is None:
        return []
    return [_text(x, 80) for x in (v if isinstance(v, list) else [v])][:20]


def _pick(d: dict, fields: tuple[str, ...]) -> dict:
    out = {}
    for k in fields:
        if k not in d or d[k] in (None, ""):
            continue
        out[k] = _texts(d[k]) if isinstance(d[k], list) else _text(d[k])
    return out


# ------------------------------------------------------------------ 記録
def normalize(defs: dict, doc: dict) -> tuple[dict, list[str]]:
    """記録の中身を確かめて、受け取る形にする。返り値は (中身, 問題)。問題があれば記録しない"""
    probs: list[str] = []
    if not isinstance(doc, dict):
        return {}, ["中身は辞書（YAML か JSON）"]
    product = str(doc.get("product") or "")
    if not PRODUCT.match(product):
        probs.append("product は owner/repo の形")
    items = {it["id"]: it for it in defs["items"]}
    levels: dict[str, int] = {}
    evidence: dict[str, dict] = {}
    for k, v in (doc.get("levels") or {}).items():
        if k not in items:
            probs.append(f"知らない項目: {k}")
            continue
        lv = v.get("level") if isinstance(v, dict) else v
        if not isinstance(lv, int) or not 0 <= lv < len(items[k]["levels"]):
            probs.append(f"{k} の粒度は 0〜{len(items[k]['levels']) - 1} の整数")
            continue
        levels[k] = lv
        if isinstance(v, dict):
            ev = _pick(v, EVIDENCE_FIELDS)
            if ev:
                evidence[k] = ev
    conds = {c["id"] for r in defs["ladder"] for c in r["conditions"]}
    met = [str(c) for c in doc.get("met") or []]
    probs += [f"知らない条件: {c}" for c in met if c not in conds]
    stack = doc.get("stack") or {}
    services = []
    for s in stack.get("services") or []:
        s = dict(s) if isinstance(s, dict) else {}
        if isinstance(s.get("sandbox"), bool):   # YAML は yes / no を真偽値に読む
            s["sandbox"] = "yes" if s["sandbox"] else "no"
        s = _pick(s, SERVICE_FIELDS)
        if not s.get("name"):
            probs.append("外部サービスには name が要る")
            continue
        if s.get("sandbox", "unknown") not in SANDBOX:
            probs.append(f"{s['name']} の sandbox は {' / '.join(SANDBOX)} のどれか")
        if s.get("handling") and s["handling"] not in HANDLING:
            probs.append(f"{s['name']} の handling は {' / '.join(HANDLING)} のどれか")
        s.setdefault("sandbox", "unknown")
        services.append(s)
    sources = []
    for s in doc.get("sources") or []:
        s = _pick(s if isinstance(s, dict) else {}, SOURCE_FIELDS)
        if not s.get("service"):
            probs.append("取り込み元には service が要る")
            continue
        if s.get("kind") and s["kind"] not in SOURCE_KIND:
            probs.append(f"{s['service']} の kind は {' / '.join(SOURCE_KIND)} のどれか")
        if s.get("state") and s["state"] not in SOURCE_STATE:
            probs.append(f"{s['service']} の state は {' / '.join(SOURCE_STATE)} のどれか")
        sources.append(s)
    out = {"product": product, "commit": _text(doc.get("commit"), 40), "by": _text(doc.get("by") or "", 80), "note": _text(doc.get("note")),
           "levels": levels, "evidence": evidence, "met": sorted(set(met) & conds),
           "stack": {"draft": stack.get("draft", True) is not False, "commit": _text(stack.get("commit") or doc.get("commit"), 40),
                     "repos": [_pick(r, REPO_FIELDS) for r in stack.get("repos") or [] if isinstance(r, dict) and r.get("repo")],
                     "services": services},
           "sources": sources}
    return out, probs


def judge_all(defs: dict, levels: dict[str, int], met: list[str]) -> dict:
    """記録と一緒に残す判定。仕事ごとの判定・レベルの到達・稼働 ready の承認を出してよいか"""
    return {"schema_version": defs.get("schema_version"),
            "targets": {t: readiness.judge(defs, levels, t) for t in defs["targets"]},
            "ladder": readiness.ladder(defs, met),
            "ready_ok": readiness.ready_line_ok(defs, levels, met)}


def record(cur, defs: dict, doc: dict, by: str) -> tuple[int | None, list[str]]:
    """判定の記録を 1 件残す。返り値は (events の id, 問題)。問題があれば残さない"""
    rec, probs = normalize(defs, doc)
    if probs:
        return None, probs
    rec["recorded_by"] = _text(by, 80)
    rec["result"] = judge_all(defs, rec["levels"], rec["met"])
    cur.execute("insert into events(name, correlation_id, payload) values (%s, %s, %s) returning id",
                (EVENT, f"product:{rec['product']}", Jsonb(rec)))
    return cur.fetchone()["id"], []


# ------------------------------------------------------------------ 読む
def _row(r: dict) -> dict:
    return {**r["payload"], "id": r["id"], "at": r["occurred_at"]}


def latest(cur, product: str | None = None) -> list[dict]:
    """製品ごとのいちばん新しい記録（製品の名前の順）"""
    cur.execute("select distinct on (payload->>'product') id, occurred_at, payload from events where name=%s "
                "and (%s::text is null or payload->>'product'=%s) order by payload->>'product', id desc", (EVENT, product, product))
    return [_row(r) for r in cur.fetchall()]


def history(cur, product: str, limit: int = 12) -> list[dict]:
    """その製品の記録（新しい順）。推移に使う"""
    cur.execute("select id, occurred_at, payload from events where name=%s and payload->>'product'=%s order by id desc limit %s",
                (EVENT, product, limit))
    return [_row(r) for r in cur.fetchall()]


def approval(cur, product: str) -> dict | None:
    """稼働 ready の承認（いちばん新しいもの）"""
    cur.execute("select occurred_at, payload from events where name=%s and payload->>'product'=%s order by id desc limit 1", (APPROVED, product))
    r = cur.fetchone()
    return {**r["payload"], "at": r["occurred_at"]} if r else None


def past_ready(defs: dict, rec: dict, appr: dict | None) -> bool:
    """稼働 ready の線を越えたか。承認したか、承認の仕組みの前に L4 以上へ届いていた製品"""
    return bool(appr) or rec["result"]["ladder"]["reached"] > defs["ready_line_after"]


def weakest(defs: dict, rec: dict) -> tuple[str, dict]:
    """いちばん弱い仕事（始められない → 割合の低い順）"""
    order = {"blocked": 0, "weak": 1, "ready": 2, "enough": 3}
    t = min(defs["targets"], key=lambda t: (order[rec["result"]["targets"][t]["verdict"]], rec["result"]["targets"][t]["ratio"]))
    return t, rec["result"]["targets"][t]


def _human_missing(defs: dict, rec: dict) -> list[dict]:
    """次のレベルに足りない条件のうち、事業主の確認が要るもの"""
    lad = rec["result"]["ladder"]
    human = {c["id"] for r in defs["ladder"] for c in r["conditions"] if c.get("human")}
    return [c for c in lad["missing"] if c["id"] in human]


def summary(defs: dict, rec: dict, appr: dict | None) -> dict:
    """ホームのカードと製品の一覧の 1 行"""
    lad = rec["result"]["ladder"]
    t, j = weakest(defs, rec)
    asks = {a["id"] for x in rec["result"]["targets"].values() for a in x["asks"]}
    rung = {r["level"]: r["ja"] for r in defs["ladder"]}
    return {"product": rec["product"], "at": _iso(rec["at"]), "reached": lad["reached"], "reached_ja": rung.get(lad["reached"], "未着手"),
            "next": lad["next"], "next_ja": rung.get(lad["next"]) if lad["next"] else None,
            "weakest": {"target": t, "target_ja": defs["targets"][t]["ja"], "verdict": j["verdict"], "verdict_ja": readiness.band_ja(defs, j["verdict"]),
                        "ratio": j["ratio"]},
            "asks": len(asks), "confirms": len(_human_missing(defs, rec)), "ready_ok": rec["result"]["ready_ok"],
            "approved": bool(appr), "past_ready": past_ready(defs, rec, appr)}


def summaries(cur, defs: dict) -> list[dict]:
    return [summary(defs, r, approval(cur, r["product"])) for r in latest(cur)]


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def sources(cur) -> list[dict]:
    """設定の「サービスとの連携」: 製品ごとのいちばん新しい記録の取り込み元（工場の生産に使う間接のサービス）"""
    out = []
    for r in latest(cur):
        for s in r.get("sources") or []:
            out.append({**s, "product": r["product"], "kind_ja": SOURCE_KIND.get(s.get("kind"), s.get("kind") or ""),
                        "state_ja": SOURCE_STATE.get(s.get("state"), s.get("state") or "不明"), "recorded_at": _iso(r["at"])})
    return out


# ------------------------------------------------------------------ 受信トレイ（inbox.py が集める）
ASK_EFFECT = ("渡したと答えても、この件は済にしない。工場が測り直して粒度が上がった項目からお願いが消え、全部消えたら閉じる。"
              "渡せないときは理由を書く（測り直すまで回答済に置く）")
READY_EFFECT = ("稼働 ready を承認したと記録し、製品のページに承認済みと出してホームのカードから外す。"
                "最初に積むチケット（壊れたテストの無効化など）の順番は、このあと決める")


def _lv_ja(defs: dict, item_id: str, lv: int) -> str:
    it = next(i for i in defs["items"] if i["id"] == item_id)
    return it["levels"][max(0, min(lv, len(it["levels"]) - 1))]


def inbox_items(cur, defs: dict) -> list[dict]:
    """準備のお願い（製品と仕事の組ごとに 1 件）と、稼働 ready の承認（製品ごとに 1 件）"""
    out = []
    by_id = {it["id"]: it for it in defs["items"]}
    for rec in latest(cur):
        p, res, since = rec["product"], rec["result"], rec["at"]
        appr = approval(cur, p)
        for t, tj in defs["targets"].items():
            j = res["targets"][t]
            if not j["asks"]:
                continue
            req = {it["id"]: it["req"].get(t) or {} for it in defs["items"]}
            detail = []
            for a in j["asks"]:
                r = req[a["id"]]
                must = "（必須）" if a["id"] in j["blocked"] else ""
                lv = rec["levels"].get(a["id"], 0)
                detail.append({"label": by_id[a["id"]]["ja"] + must,
                               "text": f"{a['ask']}（今: {_lv_ja(defs, a['id'], lv)} → 目標: {_lv_ja(defs, a['id'], r.get('goal', lv))}）"})
            out.append({"key": f"readiness:ask:{p}:{t}", "kind": "readiness_ask", "who": "owner",
                        "title": f"{p} の準備のお願い（{tj['ja']}）: {len(j['asks'])} 件",
                        "action": f"今の判定は「{readiness.band_ja(defs, j['verdict'])}」。お願いを読んで、渡せるものを渡す",
                        "detail": detail, "url": f"/products/{p}", "since": since, "ref": p,
                        "answer": {"type": "choose", "options": [{"id": "handed", "label": "渡した（測り直しを待つ）"}],
                                   "extra": [{"id": "cannot", "label": "渡せない理由を書く", "note_required": True}],
                                   "note_hint": "コメント（任意。渡したものや場所）", "effect": ASK_EFFECT, "allowed": ["owner"],
                                   "target": ("readiness_ask", (p, t, [a["id"] for a in j["asks"]], rec["id"]))}})
        if res["ready_ok"] and not past_ready(defs, rec, appr):
            dev = res["targets"]["dev"]
            weak = dev["verdict"] == "weak"
            detail = [{"label": "開発を始める", "text": f"{readiness.band_ja(defs, dev['verdict'])}（目標の {round(dev['ratio'] * 100)}%）"}]
            detail += [{"label": "警告: " + by_id[w["id"]]["ja"], "text": w["warn"]} for w in dev["warnings"]]
            out.append({"key": f"readiness:ready:{p}", "kind": "readiness_ready", "who": "owner",
                        "title": f"{p} の稼働 ready の承認",
                        "action": ("L1〜L3 の条件と「開発を始める」の必須はそろった。品質が出にくい警告が残っているので、承知のうえで始めるかを決める"
                                   if weak else "L1〜L3 の条件と「開発を始める」の必須はそろった。承認すると開発を始める"),
                        "detail": detail, "url": f"/products/{p}", "since": since, "ref": p,
                        "answer": {"type": "approve", "options": [{"id": "approve", "label": "承知のうえで始める" if weak else "稼働 ready を承認する"}],
                                   "extra": [{"id": "defer", "label": "見送る（理由を書く）", "note_required": True}],
                                   "effect": READY_EFFECT, "allowed": ["owner"],
                                   "target": ("readiness_ready", (p, rec["id"], dev["verdict"]))}})
    return out


def answer(cur, kind: str, ref: tuple, option: str, by: str, via: str, note: str) -> tuple[bool, str]:
    """受信トレイの答えの作用（inbox._apply から）"""
    if kind == "readiness_ask":
        product, target, asks, rec_id = ref
        cur.execute("insert into events(name, correlation_id, payload) values (%s, %s, %s)",
                    (ASK_ANSWERED, f"product:{product}", Jsonb({"product": product, "target": target, "option": option, "asks": asks,
                                                                "assessment_id": rec_id, "note": note, "by": by, "via": via})))
        return True, ("渡したと記録した。測り直して粒度が上がった項目から消える" if option == "handed" else "渡せない理由を記録した")
    if kind == "readiness_ready":
        product, rec_id, verdict = ref
        name = APPROVED if option == "approve" else DEFERRED
        cur.execute("insert into events(name, correlation_id, payload) values (%s, %s, %s)",
                    (name, f"product:{product}", Jsonb({"product": product, "assessment_id": rec_id, "verdict": verdict, "note": note,
                                                        "by": by, "via": via})))
        return True, "稼働 ready を承認した" if option == "approve" else "見送ったと記録した（次に測り直したらまた出る）"
    return False, f"答えの作用が分からない: {kind}"


# ------------------------------------------------------------------ 製品のページ
def page(cur, defs: dict, product: str) -> dict | None:
    """製品のページの中身（products_page.py が HTML にする。/api/products/<製品> もこの形）"""
    hist = history(cur, product)
    if not hist:
        return None
    rec = hist[0]
    appr = approval(cur, product)
    by_id = {it["id"]: it for it in defs["items"]}
    lad = rec["result"]["ladder"]
    rungs = [{"level": r["level"], "ja": r["ja"],
              "state": "reached" if r["level"] <= lad["reached"] else "next" if r["level"] == lad["next"] else "later"} for r in defs["ladder"]]
    human = {c["id"] for r in defs["ladder"] for c in r["conditions"] if c.get("human")}
    nxt = next((r for r in defs["ladder"] if r["level"] == lad["next"]), None)
    conds = [{"id": c["id"], "ja": c["ja"], "met": c["id"] in rec["met"], "human": c["id"] in human} for c in (nxt or {}).get("conditions", [])]
    items = []
    for it in defs["items"]:
        lv = rec["levels"].get(it["id"], 0)
        reqs = {t: r for t, r in it["req"].items()}
        items.append({"id": it["id"], "ja": it["ja"], "group": it["group"], "level": lv, "levels": it["levels"], "level_ja": it["levels"][lv],
                      "must": [t for t, r in reqs.items() if r.get("must")], "req": reqs, "evidence": rec["evidence"].get(it["id"]),
                      "measured": it["id"] in rec["levels"]})
    targets = {}
    for t, tj in defs["targets"].items():
        j = rec["result"]["targets"][t]
        targets[t] = {**j, "ja": tj["ja"], "verdict_ja": readiness.band_ja(defs, j["verdict"]),
                      "warnings": [{**w, "ja": by_id[w["id"]]["ja"]} for w in j["warnings"]],
                      "blocked_ja": [by_id[i]["ja"] for i in j["blocked"]],
                      "asks": [{**a, "ja": by_id[a["id"]]["ja"]} for a in j["asks"]],
                      "trend": [{"at": _iso(h["at"]), "ratio": h["result"]["targets"][t]["ratio"], "verdict": h["result"]["targets"][t]["verdict"]}
                                for h in reversed(hist) if t in h["result"]["targets"]]}
    stack = rec.get("stack") or {}
    services = [{**s, "sandbox_ja": SANDBOX.get(s.get("sandbox"), "不明"), "handling_ja": HANDLING.get(s.get("handling"), "")}
                for s in stack.get("services") or []]
    return {"product": product, "at": _iso(rec["at"]), "commit": rec.get("commit"), "by": rec.get("by"), "note": rec.get("note"),
            "assessments": len(hist), "summary": summary(defs, rec, appr),
            "approval": {k: _iso(v) for k, v in appr.items() if k in ("by", "via", "at", "note", "verdict")} if appr else None,
            "ready_line_after": defs["ready_line_after"], "rungs": rungs, "next_conditions": conds,
            "groups": defs["groups"], "items": items, "targets": targets,
            "stack": {"draft": stack.get("draft", True), "commit": stack.get("commit"), "repos": stack.get("repos") or [], "services": services}}
