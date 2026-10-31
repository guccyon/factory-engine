"""事例型の分析役（ADR 0023）: 差し戻しと停止を 1 件ずつ、観点（仕様・伝達・検査・工程）ごとになぜなぜで掘る。

- supervisor が 1 日に数件、分析する出来事を選び（pick_incidents）、制御プレーンの記録だけで入力を組み立てて（build_input）、ジョブ（kind=case_review）を積む
- dispatcher はジョブを起こすときに、後片付け（ADR 0021）のアーカイブからセッションの抜粋をクレンジングして入力に足す（attach_archive）
- 結果は events の case.analyzed と artifacts の CaseAnalysis に残すだけ（record）。作用先にもワーカーのプロンプトにも入れない。
  知見の置き場と committee は設計中（ADR 0023 の「まだ決めていないこと」）
"""
from __future__ import annotations

import io
import json
import re
import tarfile
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

LOOKBACK_DAYS = 14             # これより前の出来事は選ばない
STOPPAGE_MIN_MINUTES = 60      # 分析する停止の長さの下限（definitions/learning_taxonomy.yaml の case_review.incidents）
SKIP_STOPPAGE_PREFIXES = ("budget:", "drain:")   # 予算の枠の使い切りと、人の一時停止は分析しない
TIMELINE_MAX = 80              # 入力に入れる出来事の数の上限
SESSION_MAX_CHARS = 120_000    # セッションの抜粋の上限
FILE_MAX_CHARS = 20_000        # アーカイブの 1 ファイルの上限
HANDOFF_FILES = ("summary.md", "provisional.md", "review-defects.md", "signal.json", "diffstat.txt", "worker.err")

# 秘密情報に見える文字列は伏せる（トークン、鍵、接続文字列のパスワード）
SECRET_RES = [re.compile(p) for p in (
    r"gh[pousr]_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"sk-ant-[A-Za-z0-9_\-]{20,}", r"sk-[A-Za-z0-9]{32,}",
    r"AKIA[0-9A-Z]{16}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+", r"://[^/\s:@]+:[^/\s@]+@")]


def mask(text: str) -> str:
    for r in SECRET_RES:
        text = r.sub("[伏せた]", text)
    return text


# ------------------------------------------------------------------ 出来事を選ぶ
def pick_incidents(cur, now: datetime | None = None, limit: int = 3) -> list[dict]:
    """まだ分析していない出来事を新しい順に選ぶ。差し戻し（会話の記録の保存の後）と、閉じた長い停止を交互に"""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=LOOKBACK_DAYS)
    cur.execute("select payload->>'incident' as k from events where name='case.enqueued'")
    done = {r["k"] for r in cur.fetchall()}
    # 差し戻し: judge が検査の指摘か仮決めの差し替えを採った PR。そのチケットの引き継ぎがアーカイブされていること
    cur.execute("""select a.payload->>'pr_url' as pr_url, a.correlation_id as corr, count(*) as n, max(a.created_at) as at
                     from artifacts a
                    where a.type='Judgment' and a.payload->>'stage'='judged' and a.payload->>'verdict' in ('adopt','replace')
                      and a.created_at >= %s and a.payload->>'pr_url' is not null
                      and exists (select 1 from events h where h.name='handoff.archived' and h.correlation_id=a.correlation_id)
                    group by 1, 2 order by max(a.created_at) desc""", (since,))
    sendbacks = [{"key": f"sendback:{r['pr_url']}", "kind": "sendback", "corr": r["corr"], "pr_url": r["pr_url"], "adopted": r["n"]}
                 for r in cur.fetchall() if f"sendback:{r['pr_url']}" not in done]
    cur.execute("""select key, category, what, correlation_id as corr, started_at, ended_at from stoppages
                    where ended_at is not null and ended_at >= %s and ended_at - started_at >= make_interval(mins => %s)
                    order by ended_at desc""", (since, STOPPAGE_MIN_MINUTES))
    stops = [{"key": f"stoppage:{r['key']}", "kind": "stoppage", "corr": r["corr"], "stoppage_key": r["key"], "category": r["category"]}
             for r in cur.fetchall() if not r["key"].startswith(SKIP_STOPPAGE_PREFIXES) and f"stoppage:{r['key']}" not in done]
    out = []
    while len(out) < limit and (sendbacks or stops):
        for q in (sendbacks, stops):
            if q and len(out) < limit:
                out.append(q.pop(0))
    return out


# ------------------------------------------------------------------ 入力（制御プレーンの記録だけ）
def _short(p: dict, keep: int = 400) -> dict:
    s = json.dumps(p or {}, ensure_ascii=False, default=str)
    return json.loads(s) if len(s) <= keep else {"_truncated": mask(s[:keep])}


def build_input(cur, inc: dict) -> dict:
    inp: dict = {"incident": {k: v for k, v in inc.items() if k != "corr"}, "correlation_id": inc.get("corr")}
    if inc["kind"] == "stoppage":
        cur.execute("select key, category, what, waiting_for, cause, remedy, prevention, detected_by, started_at, ended_at, job_id "
                    "from stoppages where key=%s", (inc["stoppage_key"],))
        r = cur.fetchone() or {}
        inp["stoppage"] = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
        around = (r.get("started_at"), r.get("ended_at"))
    else:
        cur.execute("""select payload from artifacts where type='Judgment' and payload->>'pr_url'=%s and payload->>'stage'='judged' order by id""",
                    (inc["pr_url"],))
        inp["judgments"] = [_short({k: j["payload"].get(k) for k in ("category", "verdict", "defect", "decision", "interpretation", "mode")}, 1500)
                            for j in cur.fetchall()]
        around = (None, None)
    if inc.get("corr"):   # そのチケットの出来事の流れ（名前・時刻・中身の抜粋）
        cur.execute("select name, occurred_at, job_id, payload from events where correlation_id=%s order by id", (inc["corr"],))
        rows = cur.fetchall()
    elif around[0]:       # チケットのない停止（ラインが空など）: その前後の工場の出来事
        cur.execute("select name, occurred_at, job_id, payload from events where occurred_at between %s and %s "
                    "and name not in ('supervisor.heartbeat') order by id", (around[0] - timedelta(minutes=30), around[1]))
        rows = cur.fetchall()
    else:
        rows = []
    inp["timeline"] = [{"name": e["name"], "at": e["occurred_at"].isoformat(), "job_id": e["job_id"], "payload": _short(e["payload"])}
                       for e in rows[-TIMELINE_MAX:]]
    return inp


# ------------------------------------------------------------------ セッションの抜粋（後片付けのアーカイブから）
def latest_archive_key(cur, corr: str | None) -> str | None:
    if not corr:
        return None
    cur.execute("select payload->>'blob_key' as k from events where name='handoff.archived' and correlation_id=%s order by id desc limit 1", (corr,))
    r = cur.fetchone()
    return r["k"] if r else None


def cleanse_session(jsonl: bytes, cap: int = SESSION_MAX_CHARS) -> str:
    """エージェントの会話の記録（Claude Code の jsonl）から、エージェントの文と使ったツールの名前・引数の要約だけを残す。
    ツールの出力（ファイルの中身・コマンドの出力）は落とす。秘密情報に見える文字列は伏せる"""
    out: list[str] = []
    for line in jsonl.decode("utf-8", "replace").splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        msg = m.get("message") or {}
        if m.get("type") != "assistant" or not isinstance(msg.get("content"), list):
            continue
        for c in msg["content"]:
            if c.get("type") == "text" and c.get("text", "").strip():
                out.append(c["text"].strip())
            elif c.get("type") == "tool_use":
                arg = c.get("input") or {}
                brief = arg.get("command") or arg.get("file_path") or arg.get("pattern") or arg.get("description") or ""
                out.append(f"[{c.get('name')}] {str(brief)[:200]}")
    text = mask("\n".join(out))
    return text if len(text) <= cap else text[: cap // 3] + "\n…（中略）…\n" + text[-(cap * 2 // 3):]


def attach_archive(inp: dict, tar_gz: bytes) -> dict:
    """アーカイブ（ボリュームの tar.gz）から、ジョブごとの報告・仮決め・指摘と、会話の記録の抜粋を入力に足す"""
    files: dict[str, str] = {}
    sessions: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(tar_gz), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            name = m.name.lstrip("./")
            base = name.rsplit("/", 1)[-1]
            data = tf.extractfile(m)
            if data is None:
                continue
            if name.startswith("jobs/") and base in HANDOFF_FILES:
                files[name] = mask(data.read(FILE_MAX_CHARS * 4).decode("utf-8", "replace")[:FILE_MAX_CHARS])
            elif base == "ticket.json":
                files[name] = mask(data.read(FILE_MAX_CHARS * 4).decode("utf-8", "replace")[:FILE_MAX_CHARS])
            elif name.startswith("agent/") and base.endswith(".jsonl"):
                sessions.append(cleanse_session(data.read()))
    inp["handoff_files"] = files
    inp["session_excerpt"] = "\n\n---\n\n".join(sessions)[:SESSION_MAX_CHARS]
    return inp


# ------------------------------------------------------------------ 記録
def record(cur, corr: str, job_id: int, inp: dict, result: dict) -> bool:
    """分析の結果を残す。同じ出来事は 1 度だけ"""
    rep = (result or {}).get("report") or {}
    inc = inp.get("incident") or {}
    payload = {"incident": inc.get("key"), "kind": inc.get("kind"), "ticket": inp.get("correlation_id"),
               "summary": rep.get("summary", ""), "viewpoints": rep.get("viewpoints", []), "root_causes": rep.get("root_causes", []),
               "learnings": rep.get("learnings", []), "had_session": bool(inp.get("session_excerpt"))}
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('case.analyzed',%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (corr, job_id, Jsonb(payload), f"case.analyzed:{inc.get('key')}"))
    if not cur.fetchone():
        return False
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('CaseAnalysis',%s,%s,'case_review',%s)",
                (corr, job_id, Jsonb(payload)))
    return True


# ------------------------------------------------------------------ 知見の変更セットの Issue（ADR 0024 の 3）
KNOWLEDGE_ROOT = ".factory-config/knowledge"
# Issue は公開のリポジトリに立つ。分析の文に紛れた個人情報・環境の名前に見える形は伏せる（守りの本体は変更セットの CI と委員会の根拠の担当）
PII_RES = [re.compile(p) for p in (
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", r"\b\d{1,3}(?:\.\d{1,3}){3}\b", r"\b[\w\-]+\.local\b",
    r"\b0\d{1,4}-\d{1,4}-\d{3,4}\b", r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")]


def scrub(text: str) -> str:
    text = mask(text or "")
    for r in PII_RES:
        text = r.sub("[伏せた]", text)
    return text


def first_themes(tax: dict) -> dict[int, str]:
    """最初に回すテーマ（learning_taxonomy.yaml の status: first）。{id: 名前}"""
    return {t["id"]: t.get("ja", "") for t in tax.get("themes", []) if t.get("status") == "first"}


def pending_knowledge(cur, themes: dict[int, str], limit: int) -> list[dict]:
    """まだ知見の Issue にしていない分析で、最初に回すテーマの学びがあるもの（古い順）"""
    cur.execute("""select a.id, a.correlation_id as corr, a.payload from events a
                    where a.name='case.analyzed'
                      and not exists (select 1 from events f where f.name='knowledge.case_filed' and f.payload->>'incident'=a.payload->>'incident')
                    order by a.id""")
    out = []
    for r in cur.fetchall():
        ls = [x for x in (r["payload"].get("learnings") or []) if x.get("theme") in themes]
        if ls:
            out.append({"event_id": r["id"], "corr": r["corr"], **r["payload"], "learnings": ls})
        if len(out) >= limit:
            break
    return out


def knowledge_issue(a: dict, themes: dict[int, str]) -> tuple[str, str]:
    """事例の分析 1 件から、事例記録と知見の下書きを 1 本の変更セットの PR にする Issue"""
    summary = scrub(a.get("summary", "")).strip()
    head = summary.splitlines()[0][:60] if summary else a.get("incident", "")
    title = f"知見: 事例の記録と知見の下書きを足す（{head}）"
    kind = {"sendback": "差し戻し", "stoppage": "停止"}.get(a.get("kind"), a.get("kind", ""))
    causes = "\n".join(f"- {scrub(c)}" for c in a.get("root_causes") or []) or "- （なし）"
    views = []
    for v in a.get("viewpoints") or []:
        if v.get("applies") is False:
            continue
        whys = "\n".join(f"  {i}. なぜ: {scrub(w)}" for i, w in enumerate(v.get("whys") or [], 1))
        views.append(f"- {v.get('viewpoint', '')}（確からしさ {v.get('confidence', '-')}）\n{whys}\n  - 真因: {scrub(v.get('root_cause', ''))}")
    learns = "\n".join(f"{i}. テーマ {x['theme']}（{themes.get(x['theme'], '')}）: {scrub(x.get('statement', ''))}\n"
                       f"   - 作用先の候補: {x.get('target', '')} / 提案: {scrub(x.get('proposal', ''))} / 繰り返しそう: {'はい' if x.get('recurring') else 'いいえ'}"
                       for i, x in enumerate(a.get("learnings") or [], 1))
    body = f"""事例型の分析役（ADR 0023）が {kind} の出来事を分析した結果を、知見の置き場（`{KNOWLEDGE_ROOT}/`、ADR 0024）に足してください。

## 出来事
- 種類: {kind}
- 分析の記録: 制御プレーンの events `case.analyzed`（id {a.get('event_id')}）

## 要約
{summary or '（なし）'}

## 観点ごとのなぜなぜ
{chr(10).join(views) or '- （なし）'}

## 真因
{causes}

## 学びの候補
{learns}

## やること（1 本の PR）
1. `{KNOWLEDGE_ROOT}/README.md` を読み、置き場の決まりに従う
2. 事例記録 `{KNOWLEDGE_ROOT}/cases/C-NNNN.md` を 1 つ足す（番号は空いている次の番号）。出来事・観点ごとのなぜなぜ・真因・学びを書く
3. 学び 1 つにつき知見 `{KNOWLEDGE_ROOT}/K-NNNN.yaml` を 1 つ足す。`status: draft`、`confidence: hypothesis`、`verified_at` は今日、`review_by` は 30 日以内、`evidence` に `case:C-NNNN`、`theme` は上の番号、`kind` は中身に合うもの（なぜなぜの結論なら causal）
4. 既存の知見（`K-*.yaml`）を全部読み、新しい知見と重なる・矛盾するものの扱い（keep・amend・supersede・retire・recheck）を決める。置き換えるときは supersedes と superseded_by を両方に書き、古い方を superseded にする。ファイルは消さない
5. `python3 /work/engine/core-gates/knowledge_check.py --repo . --index` で `INDEX.md` を作り直し、`python3 /work/engine/core-gates/knowledge_check.py --repo .` が通ることを確かめる
6. PR の本文に、足した知見と、既存の知見ごとの扱いと理由を並べる

## 守ること
- **個人情報は書かない**。利用者・人を指す情報（氏名・連絡先・利用者 ID・本文の引用など）は抽象化して切る（「生徒 A さん」ではなく「予約の確定待ちの生徒」）
- 学びの提案（作用先への変更）はこの PR では実装しない。知見として残すだけにする
- `{KNOWLEDGE_ROOT}/` の外のファイルは変えない
"""
    return title, body
