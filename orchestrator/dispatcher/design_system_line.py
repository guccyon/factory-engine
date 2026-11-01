"""デザインシステムのデザイナー役の制御プレーン側（ADR 0025 のデザインシステムの決定、ADR 0026。definitions/design_system.yaml）。

  Issue に factory:design-system → design_system のジョブ（core-gates/design_system.py）→ ここで:
    concept: 案と見本を残し（artifacts の DesignConcept、見本はオブジェクトストアの design/<job>/）、人の選択（HumanChoice の concept_tone）に回す。
             factory:design-choice。ブランドに効くので必ず人（ADR 0025 の E2）
    人の選択: 選ばれたら spec の段を積む。「どれも違う」なら人の一言を付けて concept を積み直す（apply_choices。dispatcher の周回）
    spec:    デザインシステムの正本のファイルを残し（DesignSystemSpec）、Issue の本文に足して factory:planned（実装役がファイルを置き、
             両アプリのテーマと部品をトークンに合わせる。PR は今の検査と judge を通る）。正本から組み立てたカタログ（catalog.html・.png）は
             オブジェクトストアに置き、DesignSystemSpec の catalog に残して、prototype.py の囲いで配る（Issue のコメントに入り口）

状態は制御プレーンの artifacts と events（design_system.*）に置く（ADR 0020）。
"""
from __future__ import annotations

import re

from psycopg.types.json import Jsonb

import choice

# definitions/design_system.yaml の labels と同じ値（dispatcher のコンテナには definitions/ がないので写す。食い違いはテストで見る）
TRIGGER_LABEL, CHOICE_LABEL, AFTER_LABEL = "factory:design-system", "factory:design-choice", "factory:planned"
RUNNING_LABEL, QUEUED_LABEL = "factory:running", "factory:queued"
SPEC_DIR = "specs/design"
KIND = "concept_tone"
MARK, MARK_END = "<!-- factory:design-system -->", "<!-- /factory:design-system -->"
MODEL_TIER = "tier-large"
CATALOG_FILES = ("catalog.html", "catalog.png")   # 正本から組み立てたカタログ（core-gates/design_catalog.py）


def enqueue(cur, repo: str, issue: int, corr: str, inp: dict, attempt: int = 1) -> int | None:
    import psycopg
    try:
        cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind) "
                    "values (%s,%s,%s,%s,'design_system_line',%s,'design_system') returning id", (corr, repo, issue, attempt, MODEL_TIER))
    except psycopg.errors.UniqueViolation:
        return None
    jid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                (corr, jid, Jsonb({"by": "design_system_line", "kind": "design_system", "repo": repo, "issue": issue, "attempt": attempt, "input": inp})))
    return jid


# ------------------------------------------------------------------ concept: 案を人に回す
def record_concepts(cur, repo: str, issue: int, corr: str, job_id: int, result: dict, keys: dict[str, str]) -> int:
    c = result.get("concepts") or {}
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('DesignConcept',%s,%s,'design_system',%s) returning id",
                (corr, job_id, Jsonb({"repo": repo, "issue": issue, "round": int(result.get("round") or 1), "concepts": c, "files": keys,
                                      "cost_usd": result.get("cost_usd"), "model": result.get("model")})))
    aid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('design_system.concepts_drafted',%s,%s,%s)",
                (corr, job_id, Jsonb({"repo": repo, "issue": issue, "concept_artifact_id": aid, "concepts": len(c.get("concepts") or []),
                                      "recommended": c.get("recommended"), "round": int(result.get("round") or 1), "cost_usd": result.get("cost_usd")})))
    return aid


def option_note(c: dict) -> str:
    mood = "・".join(map(str, c.get("mood_words") or []))
    return f"{c.get('impression') or ''}（{mood}）。{c.get('tradeoff') or ''}"


def request_choice(cur, repo: str, issue: int, corr: str, job_id: int, aid: int, result: dict, keys: dict[str, str], swap_label, comment) -> int:
    c = result.get("concepts") or {}
    items = [x for x in c.get("concepts") or [] if isinstance(x, dict)]
    options = [{"id": str(x["id"]), "label": x.get("name"), "note": option_note(x), "image_key": keys.get(f"{x['id']}.png")} for x in items]
    cid = choice.request(cur, KIND, corr, "この製品のコンセプトとトーン＆マナーをどれにしますか", options, recommended=c.get("recommended"),
                         subject={"repo": repo, "ticket": str(issue)}, context=(c.get("product_understanding") or "") + "\n推す理由: " + (c.get("recommended_reason") or ""),
                         job_id=job_id, produced_by="design_system")
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('design_system.awaiting_choice',%s,%s,%s)",
                (corr, job_id, Jsonb({"repo": repo, "issue": issue, "concept_artifact_id": aid, "choice_id": cid, "recommended": c.get("recommended")})))
    swap_label(repo, issue, [RUNNING_LABEL], [CHOICE_LABEL])
    lines = ["デザインシステムのデザイナー役が、コンセプトとトーン＆マナーの案を作りました。操業コンソールの「人の選択」で 1 つ選んでください"
             "（どれも違うときは一言を添えて戻すと、作り直します）。", ""]
    for x in items:
        rec = "（推奨）" if x.get("id") == c.get("recommended") else ""
        lines.append(f"- `{x.get('id')}` {x.get('name')}{rec}: {option_note(x)}")
    comment(repo, issue, "\n".join(lines + ["", f"<sub>DesignConcept {aid} / HumanChoice {cid}</sub>"]))
    return cid


def load(cur, type_: str, aid: int) -> dict | None:
    cur.execute("select id, correlation_id, payload from artifacts where id=%s and type=%s", (aid, type_))
    r = cur.fetchone()
    return {"id": r["id"], "corr": r["correlation_id"], **r["payload"]} if r else None


def apply_choices(cur, swap_label, comment) -> int:
    """人が選んだ（戻した）コンセプトの依頼のうち、まだ次の段を積んでいないものを積む（dispatcher の poll の周回）"""
    cur.execute("select e.payload, h.correlation_id, h.payload as hc from events e join artifacts h on h.id = (e.payload->>'choice_id')::int "
                "where e.name='design_system.awaiting_choice' and h.payload->>'status' in ('chosen','returned') and not exists ("
                "select 1 from events a where a.name='design_system.choice_applied' and a.payload->>'choice_id' = e.payload->>'choice_id') order by e.id")
    n = 0
    for r in cur.fetchall():
        w, hc, corr = r["payload"], r["hc"], r["correlation_id"]
        repo, issue = w["repo"], int(w["issue"])
        d = load(cur, "DesignConcept", int(w["concept_artifact_id"])) or {}
        concepts = (d.get("concepts") or {}).get("concepts") or []
        if hc["status"] == "chosen":
            chosen = next((x for x in concepts if str(x.get("id")) == hc.get("chosen")), None)
            inp = {"phase": "spec", "concept": chosen, "round": 1}
            note = f"コンセプト `{hc.get('chosen')}` が選ばれました。デザインシステムの正本（{SPEC_DIR}/）を書きます。"
        else:
            inp = {"phase": "concept", "note": hc.get("reason") or "", "round": int(d.get("round") or 1) + 1, "previous_concepts": d.get("concepts")}
            note = f"案が戻されました（{hc.get('reason') or ''}）。一言を受けて案を作り直します。"
        jid = enqueue(cur, repo, issue, corr, inp)
        if not jid:
            continue
        cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('design_system.choice_applied',%s,%s,%s,%s) "
                    "on conflict (dedupe_key) do nothing",
                    (corr, jid, Jsonb({"repo": repo, "issue": issue, "choice_id": w["choice_id"], "status": hc["status"], "chosen": hc.get("chosen"),
                                       "agreed": hc.get("chosen") == w.get("recommended") if hc["status"] == "chosen" else None}),
                     f"design_system.choice_applied:{w['choice_id']}"))
        swap_label(repo, issue, [CHOICE_LABEL], [QUEUED_LABEL])
        comment(repo, issue, note + f"（ジョブ {jid}）")
        n += 1
    return n


# ------------------------------------------------------------------ spec: 正本を実装役へ渡す
def record_spec(cur, repo: str, issue: int, corr: str, job_id: int, result: dict, inp: dict, catalog: dict[str, str] | None = None) -> int:
    """正本のファイルと、カタログ（名前 → オブジェクトストアのキー。prototype.py が配る）を残す"""
    files = {k: v for k, v in (result.get("spec_files") or {}).items() if re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}\.yaml$", k)}
    catalog = {k: v for k, v in (catalog or {}).items() if k in CATALOG_FILES and v.startswith("design/")}
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('DesignSystemSpec',%s,%s,'design_system',%s) returning id",
                (corr, job_id, Jsonb({"repo": repo, "issue": issue, "delta": bool(result.get("delta")), "files": files, "catalog": catalog,
                                      "concept_id": (inp.get("concept") or {}).get("id"), "cost_usd": result.get("cost_usd"), "model": result.get("model")})))
    return cur.fetchone()["id"]


def issue_section(spec_id: int, files: dict[str, str], delta: bool, concept_id: str | None) -> str:
    lines = [MARK, "## デザインシステムの正本", "",
             f"デザインシステムのデザイナー役が書いた正本（{'いまのデザインシステムへの追加・変更' if delta else 'オンボーディング'}"
             f"{f'、コンセプト `{concept_id}`' if concept_id else ''}）。実装役は次を同じ PR で行う（ADR 0025）。",
             f"- 下のファイルをそのまま `{SPEC_DIR}/` に置く（{'変わるファイルだけ' if delta else '全部'}。中身は直さない。直しが要れば PR の本文に書く）",
             "- 両アプリのテーマ（色・書体・余白・角丸）を tokens.yaml に合わせ、部品の一覧（components.yaml）にある基本の部品を用意する。画面の作り替えは別のタスク",
             "- 今の画面の見た目が変わるので、PR に前後のキャプチャを付ける", ""]
    for name, text in files.items():
        lines += [f"<details><summary>{SPEC_DIR}/{name}</summary>", "", "```yaml", text.rstrip(), "```", "", "</details>", ""]
    lines += [f"<sub>DesignSystemSpec {spec_id}</sub>", MARK_END]
    return "\n".join(lines)


def replace_section(body: str, section: str) -> str:
    pat = re.compile(re.escape(MARK) + r".*?" + re.escape(MARK_END), re.S)
    if pat.search(body):
        return pat.sub(lambda _: section, body, count=1)
    return (body.rstrip() + "\n\n" if body.strip() else "") + section + "\n"


def handoff(cur, gh, swap_label, comment, repo: str, issue: int, corr: str, spec_id: int):
    """正本を Issue の本文に足し、factory:planned へ（依存を見て supervisor が実装役に回す）。同じ正本は 1 度だけ"""
    cur.execute("select 1 from events where dedupe_key=%s", (f"design_system.handed_off:{spec_id}",))
    if cur.fetchone():
        return
    d = load(cur, "DesignSystemSpec", spec_id) or {}
    it = gh("GET", f"repos/{repo}/issues/{issue}") or {}
    body = replace_section(it.get("body") or "", issue_section(spec_id, d.get("files") or {}, bool(d.get("delta")), d.get("concept_id")))
    gh("PATCH", f"repos/{repo}/issues/{issue}", {"body": body})
    swap_label(repo, issue, [RUNNING_LABEL, TRIGGER_LABEL], [AFTER_LABEL])
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('design_system.handed_off',%s,%s,%s) on conflict (dedupe_key) do nothing",
                (corr, Jsonb({"repo": repo, "issue": issue, "spec_artifact_id": spec_id, "files": sorted(d.get("files") or {}), "delta": bool(d.get("delta"))}),
                 f"design_system.handed_off:{spec_id}"))
    cat = ""
    if "catalog.html" in (d.get("catalog") or {}):
        import prototype
        cat = f"\n\nカタログ（トークンと部品の見本）: {prototype.url(spec_id, 'catalog')}（操業コンソールと同じ端末で開けます）"
    comment(repo, issue, f"デザインシステムの正本（{len(d.get('files') or {})} ファイル）を本文に足して、投入待ち（{AFTER_LABEL}）にしました。実装役が `{SPEC_DIR}/` に置き、アプリのテーマを合わせます。" + cat)
