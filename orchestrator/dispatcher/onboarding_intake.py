"""オンボーディングで事業主から受け取る口（ADR 0045 の追記 2026-11-15「受け取る口と、棚卸しと解析のジョブ」。Gen1 のプロトタイプ）。

- 問いの定義は definitions/onboarding_intake.yaml（どの製品にも効く質問票）。答えは events の onboarding.intake に 1 回の提出ごとに残す
  （表は足さない。判定の記録と同じく状態は events に置く。ADR 0020）。同じ問いの答えはいちばん新しいものが今の値で、古いものが履歴
- 口は 3 つで、どれもここの provide() を通る: 窓口の API と MCP（事業主とその代理の Claude。desk_api.py）、操業コンソールの製品のページ
  （console.py）、factoryctl onboarding provide（建設者）
- 鍵・パスワード・トークンの値は受け取らない。鍵らしい文字列があれば、その提出ごと記録せずに断る（置き場の名前を渡してもらう）
- ファイルは本文をオブジェクトストア（onboarding/<製品>/files/）に置き、記録には名前・大きさ・指紋・置き場だけを残す
- 製品の構成（どのリポが製品か・役割・工場が書くか）は答えの repos から組み立てる（composition）。解析のジョブ（onboarding_line.py）と、
  開発のジョブが既存のリポを参照で読む（dispatcher の参照のリポ）のもとになる
- 提供してもらう項目の粒度（p-*）は答えから決定論で出す（derive_levels）。工場が作る項目（o-*）は解析のジョブが出す
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
from pathlib import Path

import yaml
from psycopg.types.json import Jsonb

FILE = "onboarding_intake.yaml"
EVENT = "onboarding.intake"
ANALYZED = "onboarding.analyzed"
PRODUCT = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
REPO = PRODUCT
ARN = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]{1,512}$")
URL = re.compile(r"^https?://[^\s]{1,500}$")
FILE_NAME = re.compile(r"^[^/\\\x00]{1,120}$")
YES = ("yes", "はい", "true", "ok")
VIAS = ("desk", "console", "factoryctl", "onboard")


class IntakeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def candidates(here: Path) -> list[Path]:
    out = [Path(os.environ["ROADMAP"]).parent / FILE] if os.environ.get("ROADMAP") else []
    if len(here.parents) > 2:
        out.append(here.parents[2] / "definitions" / FILE)
    return out + [Path("/definitions") / FILE]


def load(paths: list[Path] | None = None) -> dict:
    for p in paths or candidates(Path(__file__).resolve()):
        if p.is_file():
            return yaml.safe_load(p.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"{FILE} が見つからない")


def _secret(text: str) -> bool:
    from case import SECRET_RES   # 伏せる型は case.py の 1 か所（06_iam の「秘密を出さない」）
    return any(r.search(text) for r in SECRET_RES) or bool(re.search(r"(?i)aws_secret_access_key|BEGIN [A-Z ]*PRIVATE KEY", text))


def _clip(v, n: int) -> str:
    return str(v if v is not None else "").strip()[:n]


# ------------------------------------------------------------------ 問い
def questions(defs: dict, analysis: list[dict] | None = None) -> dict[str, dict]:
    """問いの id → 定義。解析のジョブが足した問い（a- で始まる）も文の問いとして入れる"""
    out = {q["id"]: q for q in defs["questions"]}
    for a in analysis or []:
        if a.get("id") and a["id"].startswith(defs.get("analysis_question_prefix", "a-")):
            out[a["id"]] = {"id": a["id"], "group": "analysis", "ja": a.get("question") or a["id"], "kind": "text", "for": ["dev"],
                            "help": a.get("why") or "", "from_analysis": True}
    return out


def latest_analysis(cur, product: str) -> dict | None:
    cur.execute(f"select id, occurred_at, job_id, payload from events where name='{ANALYZED}' and correlation_id=%s order by id desc limit 1",
                (f"product:{product}",))
    r = cur.fetchone()
    return {**r["payload"], "id": r["id"], "at": r["occurred_at"], "job_id": r["job_id"]} if r else None


# ------------------------------------------------------------------ 受け取る
def _repos(defs: dict, value) -> tuple[list[dict], list[str]]:
    probs, out = [], []
    if not isinstance(value, list) or not value:
        return [], ["repos は {repo, role, access} の並び"]
    for r in value[: defs["limits"]["repos"]]:
        if not isinstance(r, dict) or not REPO.match(str(r.get("repo") or "")):
            probs.append("repos の repo は owner/name の形")
            continue
        e = {"repo": str(r["repo"])}
        if r.get("remove") is True:
            out.append({**e, "remove": True})
            continue
        role, access = str(r.get("role") or "other"), str(r.get("access") or "read")
        if role not in defs["repo_roles"]:
            probs.append(f"{r['repo']} の role は {' / '.join(defs['repo_roles'])} のどれか")
        if access not in defs["repo_access"]:
            probs.append(f"{r['repo']} の access は read / write")
        e.update(role=role, access=access, main=r.get("main") is True and access == "write")
        if r.get("note"):
            e["note"] = _clip(r["note"], 300)
        out.append(e)
    return out, probs


def normalize(defs: dict, qs: dict[str, dict], entry: dict) -> tuple[dict | None, str | None, bytes | None]:
    """答え 1 件を確かめて受け取る形にする。返り値は (答え, 問題, ファイルの中身)"""
    if not isinstance(entry, dict):
        return None, "答えは {question, value} の形", None
    qid = str(entry.get("question") or "")
    q = qs.get(qid)
    if not q:
        return None, f"知らない問い: {qid}", None
    out: dict = {"question": qid}
    note = _clip(entry.get("note"), 1000)
    if note:
        if _secret(note):
            return None, f"{qid} の添え書きに鍵らしい文字列がある（値は渡さず置き場の名前を）", None
        out["note"] = note
    kind, value, n = q["kind"], entry.get("value"), defs["limits"]["text_chars"]
    if entry.get("skip") is True:   # 「無い」「分からない」。足りないの一覧からは消えるが、粒度は上げない
        return {**out, "skip": True}, None, None
    if kind == "repos":
        repos, probs = _repos(defs, value)
        return ({**out, "repos": repos}, None, None) if not probs else (None, "、".join(probs), None)
    if kind == "file":
        f = entry.get("file") if isinstance(entry.get("file"), dict) else {}
        name = _clip(f.get("name"), 120)
        if not FILE_NAME.match(name or ""):
            return None, f"{qid} のファイルの名前がない", None
        if f.get("content_b64"):
            try:
                data = base64.b64decode(str(f["content_b64"]), validate=True)
            except ValueError:
                return None, f"{name} の content_b64 が読めない", None
        elif f.get("text") is not None:
            data = str(f["text"]).encode()
        else:
            return None, f"{name} の中身（content_b64 か text）がない", None
        if len(data) > defs["limits"]["file_bytes"]:
            return None, f"{name} が大きすぎる（{defs['limits']['file_bytes'] // 1048576}MB まで）", None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is not None and _secret(text):
            return None, f"{name} に鍵らしい文字列がある（伏せてから渡してください）", None
        return {**out, "file": {"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "text": text is not None}}, None, data
    if kind == "confirm":
        v = str(value if value is not None else "").strip().lower()
        if v not in YES:
            return None, f"{qid} は「はい」で答える（直したいところは note に）", None
        return {**out, "value": "yes"}, None, None
    if kind == "choice":
        v = str(value or "")
        if v not in q["options"]:
            return None, f"{qid} は {' / '.join(q['options'])} のどれか", None
        return {**out, "value": v}, None, None
    v = _clip(value, n)
    if not v:
        return None, f"{qid} の答えが空（無いときは skip: true）", None
    if _secret(v):
        return None, f"{qid} に鍵らしい文字列がある（値は渡さず置き場の名前を）", None
    if kind == "arn" and not ARN.match(v):
        return None, f"{qid} は IAM ロールの ARN（arn:aws:iam::<12 桁>:role/<名前>）", None
    if kind == "url" and not URL.match(v) and v not in ("無い", "なし", "none"):
        return None, f"{qid} は http(s) の URL（無ければ「無い」）", None
    return {**out, "value": v}, None, None


def file_key(product: str, meta: dict) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", meta["name"])[-80:]
    return f"onboarding/{product}/files/{meta['sha256'][:16]}-{safe}"


def provide(cur, defs: dict, product: str, entries, by: str, via: str, put=None) -> dict:
    """答えを受け取る。1 件でも問題があれば何も記録せず IntakeError(400)。返り値は {id, accepted}
    put(key, data, content_type) はファイルの本文の置き場（オブジェクトストア）"""
    if not PRODUCT.match(product or ""):
        raise IntakeError(400, "product は owner/repo の形")
    if via not in VIAS:
        raise IntakeError(400, f"via は {' / '.join(VIAS)}")
    if not isinstance(entries, list) or not entries:
        raise IntakeError(400, "answers は {question, value} の並び")
    an = latest_analysis(cur, product)
    qs = questions(defs, (an or {}).get("questions"))
    out, probs, files = [], [], []
    for e in entries[:60]:
        rec, prob, data = normalize(defs, qs, e)
        if prob:
            probs.append(prob)
            continue
        if data is not None:
            if put is None:
                probs.append("ファイルの置き場（オブジェクトストア）が無いので、ファイルは受け取れない")
                continue
            rec["file"]["key"] = file_key(product, rec["file"])
            files.append((rec["file"]["key"], data, "text/plain; charset=utf-8" if rec["file"]["text"] else "application/octet-stream"))
        out.append(rec)
    if probs:
        raise IntakeError(400, "；".join(probs))
    for key, data, ctype in files:
        put(key, data, ctype)
    cur.execute("insert into events(name, correlation_id, payload) values (%s, %s, %s) returning id",
                (EVENT, f"product:{product}", Jsonb({"product": product, "by": _clip(by, 80), "via": via, "answers": out})))
    return {"id": cur.fetchone()["id"], "accepted": [r["question"] for r in out]}


def request_run(cur, product: str, by: str, via: str, reason: str = "") -> int:
    """棚卸しと解析のやり直しを頼む（答えを足さずに測り直したいとき）"""
    if not PRODUCT.match(product or ""):
        raise IntakeError(400, "product は owner/repo の形")
    cur.execute("insert into events(name, correlation_id, payload) values ('onboarding.run_requested', %s, %s) returning id",
                (f"product:{product}", Jsonb({"product": product, "by": _clip(by, 80), "via": via, "reason": _clip(reason, 300)})))
    return cur.fetchone()["id"]


# ------------------------------------------------------------------ 読む
def _rows(cur, product: str) -> list[dict]:
    cur.execute(f"select id, occurred_at, payload from events where name='{EVENT}' and correlation_id=%s order by id", (f"product:{product}",))
    return cur.fetchall()


def state(cur, defs: dict, product: str) -> dict:
    """製品の今の答え（問いごとにいちばん新しいもの）・製品の構成・足りないもの・解析の結果の要約"""
    an = latest_analysis(cur, product)
    qs = questions(defs, (an or {}).get("questions"))
    answers: dict[str, dict] = {}
    repos: dict[str, dict] = {}
    files: list[dict] = []
    last_id = 0
    for r in _rows(cur, product):
        last_id = r["id"]
        p = r["payload"]
        for a in p.get("answers") or []:
            meta = {"by": p.get("by"), "via": p.get("via"), "at": r["occurred_at"], "event_id": r["id"]}
            if a.get("repos") is not None:
                for e in a["repos"]:
                    if e.get("remove"):
                        repos.pop(e["repo"], None)
                    else:
                        repos[e["repo"]] = {**e, **meta}
                answers[a["question"]] = {**a, **meta}
                continue
            if a.get("file"):
                files.append({"question": a["question"], **a["file"], **meta})
            answers[a["question"]] = {**a, **meta}
    if repos:
        answers.setdefault("repos", {"question": "repos"})["repos"] = list(repos.values())
    elif "repos" in answers and not answers["repos"].get("skip"):
        answers.pop("repos")
    # 読みの確かめは、いちばん新しい解析の後のものだけ効く
    rc = answers.get("reading_confirmed")
    if rc and an and rc.get("event_id", 0) < an["id"]:
        answers["reading_confirmed"] = {**rc, "stale": True}
    targets = sorted({t for q in qs.values() for t in q.get("for") or []})
    missing = {t: [q["id"] for q in qs.values() if t in (q.get("required") or []) and not _answered(answers.get(q["id"]))] for t in targets}
    open_analysis = [q["id"] for q in qs.values() if q.get("from_analysis") and not _answered(answers.get(q["id"]))]
    return {"product": product, "questions": list(qs.values()), "answers": answers, "repos": list(repos.values()), "files": files,
            "missing": missing, "open_analysis_questions": open_analysis, "last_intake_id": last_id, "analysis": an}


def _answered(a: dict | None) -> bool:
    return bool(a) and not a.get("stale") and (a.get("skip") or a.get("value") is not None or bool(a.get("repos")) or bool(a.get("file")))


def composition(st: dict) -> dict:
    """製品の構成。main は工場が主に書くリポ（無ければ書けるリポの最初）、refs は読み取りだけのリポ"""
    repos = st.get("repos") or []
    writes = [r for r in repos if r.get("access") == "write"]
    main = next((r for r in writes if r.get("main")), writes[0] if writes else None)
    return {"product": st["product"], "main": main["repo"] if main else None,
            "repos": [{"repo": r["repo"], "role": r.get("role"), "access": r.get("access")} for r in repos],
            "refs": [{"repo": r["repo"], "role": r.get("role")} for r in repos if r.get("access") != "write"]}


def derive_levels(defs: dict, st: dict) -> dict[str, int]:
    """答えから、提供してもらう項目（p-*）の粒度を決定論で出す。同じ項目は高い方"""
    ans = st["answers"]
    ok = {qid for qid, a in ans.items() if _answered(a) and not a.get("skip")}
    out: dict[str, int] = {}

    def up(item: str, lv: int):
        out[item] = max(out.get(item, 0), lv)

    repos = st.get("repos") or []
    if repos:
        up("p-repo", 2 if any(r.get("access") == "write" for r in repos) else 1)
    for q in defs["questions"]:
        rz = q.get("raises")
        if not rz or q["id"] not in ok:
            continue
        if any(n not in ok for n in rz.get("needs") or []):
            continue
        if q["id"] == "review_rules" and out.get("p-repo", 0) < 2:   # 書けるリポがあるときだけ「＋レビューの決まり」
            continue
        lv = (rz.get("by_value") or {}).get(ans[q["id"]].get("value"), 0) if rz.get("by_value") else rz.get("level", 0)
        if lv:
            up(rz["item"], lv)
    if out.get("p-backlog") == 2 and any(r.get("access") == "write" for r in repos):   # Issues に工場が書ける
        out["p-backlog"] = 3
    return out


def products(cur) -> list[str]:
    cur.execute(f"select distinct payload->>'product' as p from events where name in ('{EVENT}', 'onboarding.run_requested') order by 1")
    return [r["p"] for r in cur.fetchall() if r["p"]]


def summary(defs: dict, st: dict) -> dict:
    """画面と窓口に出す要約（ファイルの本文と記録の細部は出さない）"""
    qs = []
    for q in st["questions"]:
        a = st["answers"].get(q["id"]) or {}
        v = a.get("value")
        if q["kind"] == "choice" and v:
            v = q["options"].get(v, v)
        if a.get("repos"):
            v = "、".join(f"{r['repo']}（{defs['repo_roles'].get(r.get('role'), r.get('role'))}・{'書く' if r.get('access') == 'write' else '読む'}）"
                         for r in a["repos"])
        if q["kind"] == "file":
            names = [f["name"] for f in st["files"] if f["question"] == q["id"]]
            v = "、".join(names) if names else None
        qs.append({"id": q["id"], "ja": q["ja"], "kind": q["kind"], "group": q.get("group"), "help": q.get("help", ""),
                   "for": q.get("for") or [], "required": q.get("required") or [], "options": q.get("options"),
                   "answered": _answered(a), "skipped": bool(a.get("skip")), "stale": bool(a.get("stale")), "value": v, "note": a.get("note"),
                   "by": a.get("by"), "at": a.get("at").isoformat() if hasattr(a.get("at"), "isoformat") else a.get("at")})
    an = st.get("analysis") or {}
    return {"product": st["product"], "questions": qs, "missing": st["missing"], "composition": composition(st),
            "levels_from_answers": derive_levels(defs, st), "last_intake_id": st["last_intake_id"],
            "analysis": {k: an.get(k) for k in ("id", "job_id", "summary", "counts", "docs", "network", "intake_id")} if an else None}


def reference_repos(cur, repo: str) -> list[dict]:
    """そのリポを工場が書く製品の、読み取りだけのリポ（開発のジョブが参照で読む。既存の backend・モバイルなど）"""
    cur.execute(f"select 1 from events where name='{EVENT}' and payload::text like %s limit 1", (f'%"{repo}"%',))
    if not cur.fetchone():   # ふつうの製品（オンボーディングの答えが無い）は定義も読まない
        return []
    defs = load()
    for product in products(cur):
        comp = composition(state(cur, defs, product))
        if any(r["repo"] == repo and r["access"] == "write" for r in comp["repos"]):
            return comp["refs"]
    return []
