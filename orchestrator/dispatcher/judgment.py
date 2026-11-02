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


def record_judged(cur, corr: str, job_id: int, pr_url: str, inp: dict, verdicts: dict, mode: str, mode_basis: str) -> dict:
    """judge の結果を Judgment（stage=judged）と judgment.judged に残す。返り値は起票・印に使う集計。
    同じ PR で人がすでに判断した項目は、judge が人に回しても回し直さず、案を替えもしない（衝突の解消などで head が変わるたびに
    同じ問いが人に戻っていた。11-01 rally #505。judge が人の決めた案を替えた。11-02 rally #581）。人の判断を引き継いだことを記録に残す"""
    tr = inp.get("track_record", {})
    decided = human_decided(cur, pr_url)
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
        verdict, forced = delegated_verdict(category, v.get("verdict"), v.get("confidence"), v.get("precedent"), level)
        if forced:
            v = {**v, "verdict": verdict, "replacement": None}
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
                   "verdict": v.get("verdict"), "decision": v.get("replacement") or (src.get("decision") if kind == "provisional" else v.get("verdict")),
                   "evidence": (v.get("evidence") or "")[:MAX_LEN], "interpretation": (v.get("interpretation") or "")[:MAX_LEN],
                   "impact": (v.get("impact") or "")[:MAX_LEN], "precedent": (v.get("precedent") or "")[:MAX_LEN] or None, "level": level,
                   "score": {"self_reported": conf, "track_record": rec, "basis": basis},
                   "mode": mode, "mode_basis": mode_basis, "decided_by": "judge", "pr_url": pr_url, "subject": kind,
                   "source_artifact_id": v.get("artifact_id") if kind == "provisional" else None, "defect": src if kind == "defect" else None,
                   "human_grade": None, "forced_escalation": forced, "carried_human_decision": carried}
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
        elif kind == "provisional" and v.get("verdict") == "replace":
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
