"""判断（Judgment）の記録（ADR 0006）。ワーカーの「## 仮決め」の節を分解して、根拠・解釈・スコアの 3 層で残す。

ワーカーの出力は外部入力（Issue 本文）の影響を受けるので、ここでは文字列として分解して保存するだけで、
シェルにもプロンプトにも渡さない。長さは切り詰める。
"""
from __future__ import annotations

import re

from psycopg.types.json import Jsonb

TAXONOMY_VERSION = "v3"   # definitions/judgments.yaml の taxonomy_version
CATEGORIES = {"ui", "aesthetic", "system_interface", "implementation", "error_handling", "data", "scope", "inspection"}
# judge に条件付きで委任する類型（definitions/judgments.yaml の delegate_to_judge: precedent）。ADR 0025 の E4（2026-11-01 改訂）:
# 既存の見た目か UXSpec にそのまま沿う（precedent がある）adopt で、確信度が下限以上のときだけ judge が決める。それ以外は人へ回す
PRECEDENT_ONLY = {"aesthetic": 0.8}
# 2026-11-02 改訂（オーナー決定）: 要素レベル（level=element。部品 1 つの色・アイコン・揃え・余白・置き場所など、
# 前例か UI の定石に揃えるもの）は、案を替える裁き（replace）も含めて judge が決めてよい。確信度の下限を下げる。
# 体験が変わるもの（level=experience、または level の書かれていないもの）は今までどおり
ELEMENT_LEVEL_MIN = {"aesthetic": 0.5}
LEVELS = ("element", "experience")
# ADR 0042（2026-11-08 オーナー承認）: 内部実装（implementation）で、どの案でも利用者に見える振る舞いが同じ（user_visible=none）、
# 後で選び直せる（reversible=true）ものは、確信度が下限以上なら judge が決める。judge が人に回しても推奨があれば工場が推奨を採る
NO_VISIBLE_CHANGE_MIN = {"implementation": 0.5}
USER_VISIBLE = ("none", "changes")

# 判断の持ち主（ADR 0044 の決定 1・2）。judge が spec_of（product / factory）を書き、由来の印があれば factory に確定させる（core-gates/judge.py）。
# product は依頼者が分かれば requester、分からなければ business。factory と、書いていないものは maintainer（迷ったら工場のメンテナー）
SPEC_OF = ("product", "factory")
DECIDERS = ("business", "requester", "maintainer")


def requester_of(cur, corr: str) -> str | None:
    """チケットの依頼者（窓口の会話から起票したチケットなら、その会話を開いた人。ADR 0030・0039）。分からなければ None"""
    m = re.match(r"github:([\w.-]+/[\w.-]+)#(\d+)$", corr or "")
    if not m:
        return None
    try:
        cur.execute("select c.opened_by from events e join desk_conversations c on c.id::text = e.payload->>'conversation_id' "
                    "where e.name='desk.ticket_filed' and (e.payload->>'url' = %s or e.payload->>'ref' = %s) order by e.id desc limit 1",
                    (f"https://github.com/{m.group(1)}/issues/{m.group(2)}", f"{m.group(1)}#{m.group(2)}"))
        r = cur.fetchone()
    except Exception:  # noqa: BLE001  窓口の表がない工場でも判断の記録は続ける
        return None
    return (r or {}).get("opened_by")


def decider(spec_of: str | None, requester: str | None) -> str:
    if spec_of == "product":
        return "requester" if requester else "business"
    return "maintainer"
RECOMMENDS = {"provisional": ("adopt", "replace"), "defect": ("adopt", "reject")}   # escalate のときの judge の推奨
# v1 の類型 id で書かれたとき（古い指示のワーカー）の読み替え。読み替えたことは category_raw に残る
V1_ALIASES = {"term_boundary": "system_interface", "interface": "system_interface", "design_choice": "implementation", "acceptance": "ui"}
FIELDS = {"選んだ": "decision", "他の案": "alternatives", "根拠": "evidence", "解釈": "interpretation", "確信度": "confidence", "影響": "impact",
          "チケットの解釈": "ticket_interpretation"}
MAX_ITEMS, MAX_LEN = 10, 1500


def parse_provisional(text: str) -> list[dict]:
    items: list[dict] = []
    cur: dict | None = None
    field = None
    for line in (text or "").splitlines():
        # 「### 類型 id: 問い」。類型 id のあとに短い名前を付ける書き方（「### design_choice window: 数える期間」）も受ける（rally #80）
        m = re.match(r"^###\s+([A-Za-z_]+)(?:\s+([A-Za-z0-9_-]+))?\s*[:：]\s*(.+)$", line.strip())
        if m:
            cat = m.group(1).strip().lower()
            cat = V1_ALIASES.get(cat, cat)
            cur = {"category": cat if cat in CATEGORIES else "unknown", "category_raw": m.group(1)[:40], "slug": (m.group(2) or "")[:40],
                   "question": m.group(3).strip()[:MAX_LEN]}
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
        # 「- チケットの解釈: はい」（受け入れ条件が 2 通りに読めてその解釈で決めた）を印に。v1 の acceptance で書かれたものも同じ印
        ti = (it.pop("ticket_interpretation", "") or "").strip()
        it["ticket_interpretation"] = it.get("category_raw", "").lower() == "acceptance" or ti.startswith(("はい", "yes", "true"))
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


def delegated_verdict(category: str, verdict: str | None, confidence=None, precedent: str | None = None,
                      level: str | None = None) -> tuple[str | None, bool]:
    """委任の範囲に合わせた裁き。条件付きの類型で条件を満たさない裁きは escalate に替える。返り値は (裁き, 替えたか)。
    要素レベル（level=element）は adopt・replace のどちらも、沿う先（前例か UI の定石）があり確信度が要素レベルの下限以上なら judge が決める"""
    if category in PRECEDENT_ONLY and verdict != "escalate":
        has_precedent = bool((precedent or "").strip())
        conf_ok = isinstance(confidence, (int, float))
        if level == "element" and category in ELEMENT_LEVEL_MIN:
            ok = verdict in ("adopt", "replace") and has_precedent and conf_ok and confidence >= ELEMENT_LEVEL_MIN[category]
        else:
            ok = verdict == "adopt" and has_precedent and conf_ok and confidence >= PRECEDENT_ONLY[category]
        if not ok:
            return "escalate", True
    return verdict, False


def no_visible_change_verdict(category: str, verdict: str | None, confidence=None, user_visible: str | None = None,
                              reversible=None, recommend: str | None = None) -> tuple[str | None, bool]:
    """ADR 0042。振る舞いが変わらず選び直せる内部実装の判断を、人に回さず決める。返り値は (裁き, 工場が推奨を採ったか)。
    judge が escalate にしても、条件を満たし推奨（adopt / replace）があれば推奨を採る。条件を満たさないときは judge の裁きのまま"""
    if verdict != "escalate" or category not in NO_VISIBLE_CHANGE_MIN:
        return verdict, False
    ok = (user_visible == "none" and reversible is True and recommend in ("adopt", "replace")
          and isinstance(confidence, (int, float)) and confidence >= NO_VISIBLE_CHANGE_MIN[category])
    return (recommend, True) if ok else (verdict, False)


# ------------------------------------------------------------------ judge の入力（制御プレーンの記録だけから組み立てる）
def track_record(cur) -> dict:
    """類型ごとの人の採点との一致率（judgment.graded）。類型は採点した記録の今の類型（振り直し後）で数える"""
    cur.execute("select a.payload->>'category' as c, count(*) as n, avg(case when e.payload->>'grade'='agree' then 1.0 else 0.0 end) as rate "
                "from events e join artifacts a on a.id = (e.payload->>'artifact_id')::bigint where e.name='judgment.graded' group by 1")
    return {r["c"]: {"n": r["n"], "agree_rate": float(r["rate"])} for r in cur.fetchall()}


def judge_input(cur, pr_url: str, mode: str, constraints: str = "") -> dict:
    cur.execute("select id, payload from artifacts where type='Judgment' and payload->>'stage'='provisional' and payload->>'pr_url'=%s order by id", (pr_url,))
    prov = [{"artifact_id": r["id"], **{k: r["payload"].get(k) for k in ("category", "question", "decision", "alternatives", "evidence", "interpretation", "score", "impact")}}
            for r in cur.fetchall()]
    cur.execute("select payload from events where name='gate.evaluated' and payload->>'pr_url'=%s order by id desc limit 1", (pr_url,))
    g = cur.fetchone()
    defects = (g["payload"].get("defects") if g else None) or []
    return {"pr_url": pr_url, "mode": mode, "provisional": prov, "defects": defects, "track_record": track_record(cur), "constraints": constraints}


def _key(category, question) -> tuple:
    return (category or "", " ".join(str(question or "").split()))


def human_decided(cur, pr_url: str) -> dict:
    """同じ PR で人がすでに判断した項目（judged の記録に human_grade がある）。(類型, 問い) → 人の判断"""
    cur.execute("select payload from artifacts where type='Judgment' and payload->>'stage'='judged' and payload->>'pr_url'=%s "
                "and payload->'human_grade' is not null and payload->'human_grade' <> 'null'::jsonb order by id", (pr_url,))
    return {_key(r["payload"].get("category"), r["payload"].get("question")): r["payload"]["human_grade"] for r in cur.fetchall()}


def decisions_settled(cur, pr_url: str) -> bool:
    """最後の judge が人に回した項目が 1 件以上あり、そのすべてに人の判断（human_grade）が付いているか。
    付いていれば、ラベルと judge: の停止は次の judge で外れるのを待つだけで、人を待ってはいない"""
    cur.execute("select job_id from artifacts where type='Judgment' and payload->>'stage'='judged' and payload->>'pr_url'=%s "
                "order by id desc limit 1", (pr_url,))
    last = cur.fetchone()
    if not last:
        return False
    cur.execute("select payload->'human_grade' as g from artifacts where type='Judgment' and payload->>'stage'='judged' "
                "and payload->>'pr_url'=%s and job_id is not distinct from %s and payload->>'verdict'='escalate'", (pr_url, last["job_id"]))
    grades = [r["g"] for r in cur.fetchall()]
    return bool(grades) and all(g not in (None, "null") for g in grades)


# ------------------------------------------------------------------ judge が人に回した項目に人が答える（ADR 0034。受信トレイの judgment:<id>）
# 答えの形。keep・dismiss は今の差分のまま、replace・fix・change は直しを同じ担当の続きのジョブ（review_fix）に渡す
DECISIONS = {"keep": False, "replace": True, "dismiss": False, "fix": True, "change": True}


def escalations(cur, pr_url: str, *, open_only: bool = True) -> list[dict]:
    """最後の judge が人に回した項目（新しい judge の記録ほど後ろ）。仮決めの「他の案」も添える。open_only なら人の判断がまだのものだけ"""
    cur.execute("select job_id from artifacts where type='Judgment' and payload->>'stage'='judged' and payload->>'pr_url'=%s "
                "order by id desc limit 1", (pr_url,))
    last = cur.fetchone()
    if not last:
        return []
    cur.execute("select j.id, j.correlation_id, j.created_at, j.payload, s.payload as source from artifacts j "
                "left join artifacts s on s.id = (j.payload->>'source_artifact_id')::bigint "
                "where j.type='Judgment' and j.payload->>'stage'='judged' and j.payload->>'pr_url'=%s and j.job_id is not distinct from %s "
                "and j.payload->>'verdict'='escalate' order by j.id", (pr_url, last["job_id"]))
    out = []
    for r in cur.fetchall():
        p = r["payload"]
        if open_only and p.get("human_grade") not in (None, "null"):
            continue
        out.append({"id": r["id"], "correlation_id": r["correlation_id"], "created_at": r["created_at"], "payload": p,
                    "alternatives": (r["source"] or {}).get("alternatives")})
    return out


def fix_of(p: dict, decision: str, note: str) -> dict | None:
    """人の答えを、続きのジョブに渡す指摘の形（rubric_id・severity・location・evidence・remediation_hint）にする。直さない答えは None"""
    if not DECISIONS.get(decision):
        return None
    if p.get("subject") == "defect" and decision == "fix":
        d = p.get("defect") or {}
        return {**{k: d.get(k) for k in ("rubric_id", "severity", "location", "evidence", "remediation_hint")},
                **({"remediation_hint": f"{d.get('remediation_hint') or ''}\n人の一言: {note}".strip()} if note else {})}
    want = p.get("recommended_replacement") if decision == "replace" else None
    hint = "\n".join(x for x in (f"人が選んだ案: {want}" if want else "", f"人の一言: {note}" if note else "") if x)
    return {"rubric_id": "human_decision", "severity": "major", "location": f"判断: {p.get('question') or ''}"[:300],
            "evidence": f"judge が人に回した判断に、人が直す答えを出した（{decision}）。今の差分の案: {p.get('decision') or '（記録なし）'}"[:MAX_LEN],
            "remediation_hint": hint[:MAX_LEN] or "人の答えに沿って直す"}


def decide(cur, aid: int, decision: str, by: str, via: str, note: str = "") -> tuple[bool, str]:
    """judge が人に回した項目 1 つに人の判断を付ける。記録は human_grade（同じ PR の次の judge が引き継ぐ）と judgment.decided。
    直す答えは judgment.decided の fix に指摘の形で残し、supervisor が同じ担当の続きのジョブに渡す。
    最後の judge が人に回した項目がすべて決まったら、停止 judge: を閉じる（マージの判定が進む。直しがあれば直しを待つ）"""
    import stoppage
    if decision not in DECISIONS:
        return False, f"答えは {' / '.join(DECISIONS)} のどれか"
    cur.execute("select correlation_id, payload from artifacts where id=%s and type='Judgment' for update", (aid,))
    r = cur.fetchone()
    if not r or r["payload"].get("stage") != "judged" or r["payload"].get("verdict") != "escalate":
        return False, "人に回した判断の記録が見つからない"
    p = r["payload"]
    if p.get("human_grade") not in (None, "null"):
        return False, "この判断はもう決まっている"
    grade = {"decision": decision, "note": note or None, "by": by, "via": via}
    cur.execute("update artifacts set payload = jsonb_set(payload, '{human_grade}', %s) where id=%s", (Jsonb(grade), aid))
    fix = fix_of(p, decision, note)
    cur.execute("insert into events(name, correlation_id, payload) values ('judgment.decided', %s, %s)",
                (r["correlation_id"], Jsonb({"artifact_id": aid, "pr_url": p.get("pr_url"), "category": p.get("category"), "subject": p.get("subject"),
                                             "decision": decision, "note": note or None, "by": by, "via": via, "fix": fix})))
    msg = "直しを同じ担当に渡す" if fix else "今の差分のままにする"
    if p.get("pr_url") and decisions_settled(cur, p["pr_url"]):
        stoppage.close_stoppage(cur, f"judge:{p['pr_url']}", remedy=f"人が人に回した判断に答えた（{via}）", closed_by=by[:80] or "inbox")
        msg += "。この PR の判断はすべて決まった"
    return True, msg


def pending_fixes(cur, pr_url: str) -> list[dict]:
    """人が直す答えを出したが、まだ続きのジョブに渡していないもの（judgment.decided の fix。渡したら judgment.fix_enqueued に id が載る）"""
    cur.execute("select d.id, d.payload from events d where d.name='judgment.decided' and d.payload->>'pr_url'=%s "
                "and d.payload->'fix' is not null and d.payload->'fix' <> 'null'::jsonb "
                "and not exists (select 1 from events e where e.name='judgment.fix_enqueued' and e.payload->'decided' @> to_jsonb(d.id)) "
                "order by d.id", (pr_url,))
    return [{"id": r["id"], **r["payload"]} for r in cur.fetchall()]


def record_judged(cur, corr: str, job_id: int, pr_url: str, inp: dict, verdicts: dict, mode: str, mode_basis: str) -> dict:
    """judge の結果を Judgment（stage=judged）と judgment.judged に残す。返り値は起票・印に使う集計。
    同じ PR で人がすでに判断した項目は、judge が人に回しても回し直さず、案を替えもしない（衝突の解消などで head が変わるたびに
    同じ問いが人に戻っていた。11-01 rally #505。judge が人の決めた案を替えた。11-02 rally #581）。人の判断を引き継いだことを記録に残す"""
    tr = inp.get("track_record", {})
    decided = human_decided(cur, pr_url)
    requester = requester_of(cur, corr)
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
        level = v.get("level") if v.get("level") in LEVELS else None
        user_visible = v.get("user_visible") if v.get("user_visible") in USER_VISIBLE else None
        reversible = v.get("reversible") if isinstance(v.get("reversible"), bool) else None
        spec_of = v.get("spec_of") if v.get("spec_of") in SPEC_OF else None
        delegated = False
        if kind == "provisional":
            taken, delegated = no_visible_change_verdict(category, v.get("verdict"), v.get("confidence"), user_visible, reversible, v.get("recommend"))
            if delegated:   # 工場が judge の推奨を採った（ADR 0042）。人には回さない
                v = {**v, "verdict": taken}
        verdict, forced = delegated_verdict(category, v.get("verdict"), v.get("confidence"), v.get("precedent"), level)
        if forced:   # judge の裁きは人に回すときの推奨として残す（受信トレイで「推奨」に出す）
            v = {**v, "verdict": verdict, "recommend": v.get("verdict") if v.get("verdict") in ("adopt", "replace") else v.get("recommend")}
        # 人の判断は escalate だけでなく adopt・replace より優先する。要素レベルの自己承認（#137）で judge が
        # 人の決めた案を replace し、決定を覆す直しが起票された（11-02 rally #581）
        carried = decided.get(_key(category, question)) if kind == "provisional" else None
        if carried:
            v = {**v, "verdict": "human_decided", "replacement": None}
        conf = v.get("confidence")
        conf = max(0.0, min(1.0, float(conf))) if isinstance(conf, (int, float)) else None
        rec = tr.get(category)
        basis = "track_record" if rec and rec["n"] >= 10 else "self_report"
        payload = {"stage": "judged", "category": category, "taxonomy_version": TAXONOMY_VERSION, "question": question,
                   "verdict": v.get("verdict"),
                   "decision": (v.get("replacement") if v.get("verdict") != "escalate" else None) or (src.get("decision") if kind == "provisional" else v.get("verdict")),
                   "evidence": (v.get("evidence") or "")[:MAX_LEN], "interpretation": (v.get("interpretation") or "")[:MAX_LEN],
                   "impact": (v.get("impact") or "")[:MAX_LEN], "precedent": (v.get("precedent") or "")[:MAX_LEN] or None, "level": level,
                   "user_visible": user_visible, "reversible": reversible,
                   "spec_of": spec_of or "factory", "spec_basis": (v.get("spec_basis") or ("judge" if spec_of else "default"))[:300],
                   "decider": decider(spec_of, requester), "requester": requester if spec_of == "product" else None,
                   "score": {"self_reported": conf, "track_record": rec, "basis": basis},
                   "mode": mode, "mode_basis": mode_basis, "decided_by": "judge", "pr_url": pr_url, "subject": kind,
                   "source_artifact_id": v.get("artifact_id") if kind == "provisional" else None, "defect": src if kind == "defect" else None,
                   "human_grade": None, "forced_escalation": forced, "forced_delegation": delegated, "carried_human_decision": carried,
                   "already_applied": bool(v.get("already_applied")) if v.get("verdict") == "replace" else None,
                   # 人に回すときの judge の推奨（受信トレイの「推奨」。ADR 0034）。仮決めは adopt / replace（案は replacement）、指摘は adopt / reject
                   "recommend": v.get("recommend") if v.get("verdict") == "escalate" and v.get("recommend") in RECOMMENDS[kind] else None,
                   "recommended_replacement": (v.get("replacement") or "")[:MAX_LEN] or None
                   if v.get("verdict") == "escalate" and v.get("recommend") == "replace" else None}
        cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Judgment',%s,%s,'verify',%s) returning id",
                    (corr, job_id, Jsonb(payload)))
        aid = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('judgment.judged',%s,%s,%s)",
                    (corr, job_id, Jsonb({"artifact_id": aid, "category": category, "verdict": v.get("verdict"), "confidence": conf,
                                          "basis": basis, "mode": mode, "pr_url": pr_url})))
        if conf is not None and not carried:   # 人が判断済みの項目は judge の確信度の低さで印を付けない
            confidences.append(conf)
        if v.get("verdict") == "escalate":
            escalated.append({"artifact_id": aid, "category": category, "question": question})
        elif kind == "defect" and v.get("verdict") == "adopt":
            adopted_defects.append({**src, "judgment_id": aid})
        elif kind == "provisional" and v.get("verdict") == "replace" and not payload["already_applied"]:
            # 今の差分がすでに差し替え後の案になっているもの（already_applied）は、記録だけ残して差し戻さない。
            # 直す作業のない差し戻しの Issue が起票され、差し戻しの上限に当たってマージが止まった（11-04 rally #571・#570・#677）
            replaced.append({"artifact_id": aid, "category": category, "question": question, "replacement": v.get("replacement"),
                             "interpretation": payload["interpretation"]})
    return {"adopted_defects": adopted_defects, "replaced": replaced, "escalated": escalated,
            "min_confidence": min(confidences) if confidences else None, "count": len(rows)}


# ------------------------------------------------------------------ 類型の振り直し（definitions/judgment-remap-*.yaml。分け直しは人が決める）
def recategorize(cur, remap: dict) -> dict:
    """仮決めを artifact id ごとに新しい類型へ、judge の記録はその元の仮決めに合わせて振り直す。元の類型は category_v1 に残す。冪等"""
    to_ver, mapping = remap["taxonomy_to"], {int(k): v for k, v in (remap.get("provisional") or {}).items()}
    done = {"provisional": 0, "judged": 0, "skipped": 0}
    cur.execute("select id, payload from artifacts where type='Judgment' and coalesce(payload->>'taxonomy_version','v1') <> %s order by id", (to_ver,))
    for r in cur.fetchall():
        p = r["payload"]
        src = r["id"] if p.get("stage") == "provisional" else p.get("source_artifact_id")
        if p.get("category") == "inspection":
            new = {"to": "inspection"}
        elif src in mapping:
            new = mapping[src]
        else:
            done["skipped"] += 1
            continue
        patch = {"category": new["to"], "category_v1": p.get("category"), "taxonomy_version": to_ver,
                 "ticket_interpretation": bool(new.get("ticket_interpretation") or p.get("ticket_interpretation"))}
        cur.execute("update artifacts set payload = payload || %s where id=%s", (Jsonb(patch), r["id"]))
        cur.execute("insert into events(name, payload, dedupe_key) values ('judgment.recategorized', %s, %s) on conflict (dedupe_key) do nothing",
                    (Jsonb({"artifact_id": r["id"], "from": p.get("category"), "to": new["to"], "taxonomy_version": to_ver}),
                     f"judgment.recategorized:{r['id']}:{to_ver}"))
        done["provisional" if p.get("stage") == "provisional" else "judged"] += 1
    return done
