"""デザインの線（ADR 0025。P4-6 デザイナー役・P4-10 デザイン検査役）の制御プレーン側。

流れ:
  Issue に factory:design → dispatcher が ui_design のジョブに積む（poll）
  → ワーカー（ui_design.sh）が UXSpec と案のファイル（HTML・CSS・PNG）を返す。ここでファイルをオブジェクトストアに置き、
    UXSpec を artifacts に残して、デザイン検査役のジョブ（ui_design_review）を積む
  → 検査役の判断（route 済み）に従って作用する
     approve: 推す案で UXSpec を Issue の本文に足し、factory:planned へ（依存を見て supervisor が実装に回す）
     revise:  指摘と前の UXSpec を渡して ui_design を積み直す（1 回まで。2 回目の直しは検査役の側で人へ倒れる）
     human:   factory:design-choice を付けて Issue に案の一覧を書く。人は factoryctl design pick（P4-7 で選択の画面）で選ぶ
  → 人の選択（ui_design.chosen）は poll の周回で apply_choices が Issue に反映する

デザインシステムに足りない部品（design_system_gaps）は、画面に直書きさせずに別の段（Issue）にして、画面の段の依存にする。
判断は judge と同じ 3 層（根拠・解釈・確信度）で DesignVerdict に残し、人の選択と検査役の推す案の一致を ui_design.chosen に残す。
"""
from __future__ import annotations

import base64
import json
import re

import yaml
from psycopg.types.json import Jsonb

TRIGGER_LABEL, CHOICE_LABEL, AFTER_LABEL = "factory:design", "factory:design-choice", "factory:planned"   # definitions/ui_design.yaml の labels
RUNNING_LABEL, FAILED_LABEL = "factory:running", "factory:failed"
MARK, MARK_END = "<!-- factory-uxspec:v1 -->", "<!-- /factory-uxspec -->"
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}\.(html|css|png)$")
CONTENT_TYPES = {"html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8", "png": "image/png"}
MAX_HTML_INLINE = 20000   # 推す案の HTML を Issue に載せる上限（実装役が見た目の参考にする）
REASON_JA = {"new_screen": "新しい画面", "brand_impact": "ブランドと印象に効く画面", "low_confidence": "検査役の確信度が低い",
             "revisions_exhausted": "直しを 1 回使った後も直しが要る", "inspector": "検査役が人の判断が要るとした"}
DECIDED_JA = {"design_inspector": "デザイン検査役が承認した", "human": "人が選んだ"}


# ------------------------------------------------------------------ ワーカーの出力
def extract_files(lines: list[str]) -> tuple[dict[str, bytes], list[str]]:
    """FACTORY_FILE:<名前>:<base64> の行を取り出す。名前が規則に合わないもの・壊れたものは捨てる。残りの行も返す（ログ用）"""
    files, rest = {}, []
    for line in lines:
        if not line.startswith("FACTORY_FILE:"):
            rest.append(line)
            continue
        name, _, b64 = line[len("FACTORY_FILE:"):].strip().partition(":")
        if not FILE_RE.match(name):
            continue
        try:
            files[name] = base64.b64decode(b64, validate=True)
        except ValueError:
            continue
    return files, rest


def store_files(put, job_id: int, files: dict[str, bytes]) -> dict[str, str]:
    """案のファイルをオブジェクトストアに置き、名前 → キーを返す。put(key, data, content_type)"""
    keys = {}
    for name, data in sorted(files.items()):
        key = f"design/{job_id}/{name}"
        put(key, data, CONTENT_TYPES[name.rsplit(".", 1)[1]])
        keys[name] = key
    return keys


def record_draft(cur, repo: str, issue: int, corr: str, job_id: int, result: dict, keys: dict[str, str]) -> int:
    """デザイナー役の案を UXSpec の成果物として残す。返り値は artifacts の id"""
    spec = result.get("uxspec") or {}
    payload = {"repo": repo, "issue": issue, "round": int(result.get("round") or 1), "screens": spec.get("screens"),
               "states": spec.get("states"), "design_system_constraints": spec.get("design_system_constraints"),
               "design_system_gaps": spec.get("design_system_gaps") or [], "uxspec": spec, "files": keys,
               "model": result.get("model"), "cost_usd": result.get("cost_usd")}
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('UXSpec',%s,%s,'design',%s) returning id",
                (corr, job_id, Jsonb(payload)))
    aid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('ui_design.drafted',%s,%s,%s)",
                (corr, job_id, Jsonb({"repo": repo, "issue": issue, "artifact_id": aid, "round": payload["round"],
                                      "variants": [v.get("id") for v in spec.get("variants") or []], "recommended": spec.get("recommended"),
                                      "gaps": len(payload["design_system_gaps"]), "cost_usd": result.get("cost_usd")})))
    return aid


def enqueue(cur, repo: str, issue: int, corr: str, kind: str, model_tier: str, inp: dict, attempt: int = 1) -> int | None:
    """デザインの線のジョブを積む。入力は job.enqueued の input に残し、dispatcher が標準入力で渡す。同じ Issue の active なジョブがあれば積めない"""
    import psycopg
    try:
        cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind) "
                    "values (%s,%s,%s,%s,'design_line',%s,%s) returning id", (corr, repo, issue, attempt, model_tier, kind))
    except psycopg.errors.UniqueViolation:
        return None
    jid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                (corr, jid, Jsonb({"by": "design_line", "kind": kind, "repo": repo, "issue": issue, "attempt": attempt, "input": inp})))
    return jid


def load_design(cur, artifact_id: int) -> dict | None:
    cur.execute("select id, correlation_id, payload from artifacts where id=%s and type='UXSpec'", (artifact_id,))
    r = cur.fetchone()
    return {"id": r["id"], "corr": r["correlation_id"], **r["payload"]} if r else None


def review_input(cur, get, inp: dict) -> dict:
    """検査役の標準入力: UXSpec と案のファイル（base64）。get(key) -> bytes"""
    d = load_design(cur, int(inp.get("design_artifact_id") or 0))
    if not d:
        return inp
    files = {}
    for name, key in (d.get("files") or {}).items():
        try:
            files[name] = base64.b64encode(get(key)).decode()
        except Exception:  # noqa: BLE001  読めない案は渡さない（検査役が欠けを指摘する）
            continue
    return {**inp, "round": d.get("round") or 1, "uxspec": d.get("uxspec") or {}, "files": files}


# ------------------------------------------------------------------ 検査役の判断
def record_review(cur, repo: str, issue: int, corr: str, job_id: int, inp: dict, result: dict, mode: str = "attended") -> int:
    """デザイン検査役の判断を 3 層で残す。返り値は DesignVerdict の id"""
    v = result.get("verdict") or {}
    conf = v.get("confidence")
    payload = {"repo": repo, "issue": issue, "design_artifact_id": inp.get("design_artifact_id"), "round": inp.get("round") or 1,
               "decision": result.get("decision"), "human_reasons": result.get("human_reasons") or [],
               "inspector_decision": v.get("decision"), "chosen_variant": v.get("chosen_variant"),
               "findings": v.get("findings") or [], "checked": v.get("checked") or [],
               "evidence": v.get("evidence") or [], "interpretation": v.get("interpretation"),
               "score": {"self_reported": max(0.0, min(1.0, float(conf))) if isinstance(conf, (int, float)) else None},
               "human_reason": v.get("human_reason"), "decided_by": "design_inspector", "mode": mode, "model": result.get("model")}
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('DesignVerdict',%s,%s,'design_inspector',%s) returning id",
                (corr, job_id, Jsonb(payload)))
    vid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('ui_design.reviewed',%s,%s,%s)",
                (corr, job_id, Jsonb({"repo": repo, "issue": issue, "verdict_id": vid, "design_artifact_id": payload["design_artifact_id"],
                                      "round": payload["round"], "decision": payload["decision"], "inspector_decision": payload["inspector_decision"],
                                      "human_reasons": payload["human_reasons"], "chosen_variant": payload["chosen_variant"],
                                      "confidence": payload["score"]["self_reported"],
                                      "must": sum(1 for f in payload["findings"] if f.get("severity") == "must"),
                                      "cost_usd": result.get("cost_usd")})))
    return vid


def act_on_review(cur, repo: str, issue: int, corr: str, job_id: int, inp: dict, result: dict, vid: int, gh, comment, model_tier: str, get=None):
    decision = result.get("decision")
    v = result.get("verdict") or {}
    d = load_design(cur, int(inp.get("design_artifact_id") or 0)) or {}
    if decision == "approve":
        handoff(cur, gh, comment, repo, issue, corr, d, v.get("chosen_variant") or (d.get("uxspec") or {}).get("recommended"),
                "design_inspector", vid, get)
    elif decision == "revise":
        nxt = {"round": int(d.get("round") or 1) + 1, "findings": v.get("findings") or [], "previous_uxspec": d.get("uxspec") or {},
               "previous_artifact_id": d.get("id")}
        jid = enqueue(cur, repo, issue, corr, "ui_design", model_tier, nxt, attempt=nxt["round"])
        comment(repo, issue, revise_comment(v, jid))
    else:
        swap(gh, repo, issue, [RUNNING_LABEL], [CHOICE_LABEL])
        cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('ui_design.awaiting_choice',%s,%s,%s,%s) "
                    "on conflict (dedupe_key) do nothing",
                    (corr, job_id, Jsonb({"repo": repo, "issue": issue, "design_artifact_id": d.get("id"), "verdict_id": vid,
                                          "recommended": v.get("chosen_variant"), "reasons": result.get("human_reasons") or []}),
                     f"ui_design.awaiting_choice:{d.get('id')}"))
        comment(repo, issue, choice_comment(d, v, result.get("human_reasons") or []))


# ------------------------------------------------------------------ 人の選択
def choose(cur, repo: str, issue: int, variant: str, by: str, note: str = "") -> dict:
    """人の選択を残す（factoryctl design pick、P4-7 の選択の画面）。GitHub への反映は apply_choices が行う"""
    cur.execute("select payload from events where name='ui_design.awaiting_choice' and payload->>'repo'=%s and (payload->>'issue')::int=%s "
                "order by id desc limit 1", (repo, issue))
    w = cur.fetchone()
    if not w:
        raise ValueError("人の選択を待っている案がない")
    w = w["payload"]
    d = load_design(cur, int(w["design_artifact_id"])) or {}
    ids = [str(x.get("id")) for x in (d.get("uxspec") or {}).get("variants") or []]
    if variant not in ids:
        raise ValueError(f"案は {' / '.join(ids)} のどれか")
    payload = {"repo": repo, "issue": issue, "design_artifact_id": d.get("id"), "verdict_id": w.get("verdict_id"), "variant": variant,
               "recommended": w.get("recommended"), "agree": variant == w.get("recommended"), "by": by, "note": note}
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('ui_design.chosen',%s,%s,%s) on conflict (dedupe_key) do nothing returning id",
                (d.get("corr"), Jsonb(payload), f"ui_design.chosen:{d.get('id')}"))
    if cur.fetchone() is None:
        raise ValueError("この案はもう選ばれている")
    return payload


def apply_choices(cur, gh, comment, get=None) -> int:
    """人が選んだのにまだ Issue に反映していない案を反映する（dispatcher の poll の周回）"""
    cur.execute("select e.correlation_id, e.payload from events e where e.name='ui_design.chosen' and not exists ("
                "select 1 from events h where h.name='ui_design.handed_off' and h.payload->>'design_artifact_id' = e.payload->>'design_artifact_id') "
                "order by e.id")
    n = 0
    for r in cur.fetchall():
        p = r["payload"]
        d = load_design(cur, int(p["design_artifact_id"]))
        if d:
            handoff(cur, gh, comment, p["repo"], int(p["issue"]), r["correlation_id"], d, p["variant"], "human", p.get("verdict_id"), get)
            n += 1
    return n


# ------------------------------------------------------------------ 実装の線へ渡す
def handoff(cur, gh, comment, repo: str, issue: int, corr: str, d: dict, variant: str, decided_by: str, verdict_id, get=None):
    """決まった案の UXSpec を Issue の本文に足し、足りない部品の段を切って依存にし、factory:planned へ。同じ案は 1 度だけ"""
    cur.execute("select 1 from events where dedupe_key=%s", (f"ui_design.handed_off:{d.get('id')}",))
    if cur.fetchone():
        return
    spec = d.get("uxspec") or {}
    gaps_issue = None
    if spec.get("design_system_gaps"):
        title, body = gaps_issue_text(repo, issue, spec)
        gaps_issue = (gh("POST", f"repos/{repo}/issues", {"title": title, "body": body, "labels": [AFTER_LABEL]}) or {}).get("number")
    html = None
    key = (d.get("files") or {}).get(f"{variant}.html")
    if key and get:
        try:
            html = get(key).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001  モックは参考。読めなくても渡す
            html = None
    it = gh("GET", f"repos/{repo}/issues/{issue}") or {}
    body = replace_section(it.get("body") or "", issue_section(issue, spec, variant, decided_by, gaps_issue, html))
    gh("PATCH", f"repos/{repo}/issues/{issue}", {"body": body})
    swap(gh, repo, issue, [RUNNING_LABEL, CHOICE_LABEL, TRIGGER_LABEL], [AFTER_LABEL])
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('ui_design.handed_off',%s,%s,%s) on conflict (dedupe_key) do nothing",
                (corr, Jsonb({"repo": repo, "issue": issue, "design_artifact_id": d.get("id"), "verdict_id": verdict_id, "variant": variant,
                              "decided_by": decided_by, "gaps_issue": gaps_issue}), f"ui_design.handed_off:{d.get('id')}"))
    comment(repo, issue, f"画面の設計が決まりました（案 `{variant}`、{DECIDED_JA.get(decided_by, decided_by)}）。UXSpec を本文に足して、投入待ち（{AFTER_LABEL}）に戻しました。"
                         + (f"\nデザインシステムに足りない部品は #{gaps_issue} で先に足します（この段の依存）。" if gaps_issue else ""))


def swap(gh, repo: str, issue: int, remove: list[str], add: list[str]):
    import urllib.error
    import urllib.parse
    for label in remove:
        try:
            gh("DELETE", f"repos/{repo}/issues/{issue}/labels/{urllib.parse.quote(label, safe='')}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    if add:
        gh("POST", f"repos/{repo}/issues/{issue}/labels", {"labels": add})


# ------------------------------------------------------------------ 文面（純粋な関数）
def chosen_spec(spec: dict, variant: str) -> dict:
    """実装に渡す UXSpec: 決まった案だけを残す（ほかの案は記録に残っている）"""
    out = {k: v for k, v in spec.items() if k not in ("variants", "recommended", "recommended_reason")}
    out["variant"] = next((v for v in spec.get("variants") or [] if str(v.get("id")) == variant), {"id": variant})
    return out


def issue_section(issue: int, spec: dict, variant: str, decided_by: str, gaps_issue: int | None, html: str | None) -> str:
    text = yaml.safe_dump(chosen_spec(spec, variant), allow_unicode=True, sort_keys=False)
    lines = [MARK, "## 画面の設計（UXSpec）", "",
             f"デザインの線で決まった案 `{variant}`（{DECIDED_JA.get(decided_by, decided_by)}）。画面の要素・振る舞い・状態はこの UXSpec に合わせて実装する（ADR 0025）。",
             f"同じ PR で、この UXSpec を `specs/ux/issue-{issue}.yaml` に置く。見た目の細部は後から直してよい。"]
    if gaps_issue:
        lines += ["デザインシステムに足りない部品は別の段で足す。画面に直書きしない。", f"依存: #{gaps_issue}"]
    lines += ["", "```yaml", text.rstrip(), "```"]
    if html and len(html) <= MAX_HTML_INLINE:
        lines += ["", "<details><summary>案のモック（静的 HTML。見た目の参考）</summary>", "", "```html", html.rstrip(), "```", "", "</details>"]
    lines.append(MARK_END)
    return "\n".join(lines)


def replace_section(body: str, section: str) -> str:
    """本文の UXSpec の節を差し替える（なければ末尾に足す）。人が書いた部分は触らない"""
    pat = re.compile(re.escape(MARK) + r".*?" + re.escape(MARK_END), re.S)
    if pat.search(body):
        return pat.sub(lambda _: section, body, count=1)
    return (body.rstrip() + "\n\n" if body.strip() else "") + section + "\n"


def gaps_issue_text(repo: str, issue: int, spec: dict) -> tuple[str, str]:
    gaps = [g if isinstance(g, str) else json.dumps(g, ensure_ascii=False) for g in spec.get("design_system_gaps") or []]
    title = f"デザインシステムに部品を足す（#{issue} の画面で必要）"
    body = "\n".join([f"#{issue} の画面の設計で、デザインシステムに足りない部品が見つかりました（デザイナー役、ADR 0025）。",
                      "画面に直書きせず、先にデザインシステム（`specs/design/` と各アプリのテーマ・部品の実装）に足します。", "",
                      "## 足す部品", *[f"- {g}" for g in gaps], "",
                      "## 受け入れ条件",
                      "- 部品の一覧（`specs/design/`）に、用途・状態（通常・押下・無効・読み込み中・失敗）・使わない場面を書く",
                      "- 両アプリ（使う側）のテーマの値だけで描ける。トークン外の色・余白を使わない",
                      "- 画面のキャプチャの一覧に部品の見本の画面があれば足す"])
    return title, body


def _variants_lines(d: dict) -> list[str]:
    spec = d.get("uxspec") or {}
    return [f"- `{v.get('id')}` {v.get('name')}: {v.get('tradeoff')}" for v in spec.get("variants") or []]


def choice_comment(d: dict, v: dict, reasons: list[str]) -> str:
    spec = d.get("uxspec") or {}
    why = "、".join(REASON_JA.get(r, r) for r in reasons) or "検査役が人の判断が要るとした"
    note = [f"> {v['human_reason']}"] if v.get("human_reason") else []
    return "\n".join([f"## 画面の案を選んでください（{why}）", "",
                      f"デザイナー役が {len(spec.get('variants') or [])} 案を作り、デザイン検査役は `{v.get('chosen_variant')}` を推しています（確信度 {v.get('confidence')}）。",
                      *note, "",
                      "### 案", *_variants_lines(d), "",
                      f"推す理由（デザイナー役）: {spec.get('recommended_reason') or '（なし）'}", "",
                      "案の画像は操業コンソールの選択の画面か `factoryctl design export` で見られます。",
                      f"選ぶ: `factoryctl design pick {d.get('repo')}#{d.get('issue')} <案>`。選ぶまでこの段は流れず、ほかの段は流れます。", "",
                      "---", "_デザイン検査役（ADR 0025）_"])


def revise_comment(v: dict, jid: int | None) -> str:
    fx = [f"- [{f.get('severity')}] {f.get('viewpoint')} / 案 {f.get('variant')}: {f.get('evidence')}" for f in v.get("findings") or []]
    head = f"デザイン検査役が直しを求めました。デザイナー役に戻します（ジョブ {jid}）。" if jid else "デザイン検査役が直しを求めましたが、ジョブを積めませんでした（同じ段のジョブが動いている）。"
    return "\n".join([head, "", *fx[:12], "", "---", "_デザイン検査役（ADR 0025）_"])
