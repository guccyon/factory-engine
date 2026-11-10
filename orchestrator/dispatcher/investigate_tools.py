"""工場の中での一次調査の道具の中身（ADR 0048 の順番 3。runner_relay.INVESTIGATE_TOOLS を dispatcher の側で受ける）。

調査役（ワーカーのエージェント）には、DB の接続情報も GitHub のトークンも渡さない。読むのはすべてこの口を通る。
  - inv_sql: investigate_db.query（調査用の利用者で別に繋いだ、読み取り専用のトランザクション）
  - inv_job_log: オブジェクトストアのジョブのログの末尾。秘密情報の形は伏せる
  - inv_github: その調査の製品のリポジトリの、決まった場所への GET だけ（製品は grant のときに決め、エージェントには選ばせない）
返す文は長さを切る（エージェントの文脈を食いつぶさない）。
"""
from __future__ import annotations

import json
import re

import investigate_db

MAX_TEXT = 20000
DEFAULT_TAIL = 200
MAX_TAIL = 400
# 秘密情報の形（core-gates/incident_check.py の DEIDENT の「秘密情報」と同じ。dispatcher には core-gates がないので写す）
SECRET = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-ant-[A-Za-z0-9_\-]{20,}|AKIA[0-9A-Z]{16}"
                    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?i:bearer)\s+[A-Za-z0-9._\-]{20,}|postgres(?:ql)?://[^\s:@/]+:[^\s@/]+@")
GITHUB_PATH = re.compile(r"^(?:issues|pulls)(?:/\d{1,7}(?:/(?:comments|reviews|files|commits|timeline))?)?(?:\?[\w=&.,-]*)?$"
                         r"|^actions/runs(?:/\d{1,12}(?:/jobs)?)?(?:\?[\w=&.,-]*)?$"
                         r"|^commits(?:/[0-9a-f]{7,40}(?:/(?:check-runs|status))?)?(?:\?[\w=&.,-]*)?$")


def _text(s: str, is_error: bool = False) -> dict:
    if len(s) > MAX_TEXT:
        s = s[:MAX_TEXT] + f"\n…（{len(s) - MAX_TEXT} 文字を切った。範囲を絞って読み直す）"
    return {"content": [{"type": "text", "text": s}], "isError": is_error}


def redact(text: str) -> str:
    return SECRET.sub("［伏せた］", text)


def sql(arguments: dict, query=investigate_db.query) -> dict:
    stmt = arguments.get("sql")
    if not isinstance(stmt, str):
        return _text("sql は文字列", True)
    try:
        r = query(stmt)
    except investigate_db.Rejected as e:
        return _text(f"流さなかった: {e}", True)
    except investigate_db.Unavailable as e:
        return _text(f"DB は使えない: {e}。ほかの道具（ログ・GitHub・定義）で調べる", True)
    except Exception as e:  # noqa: BLE001  SQL の誤り・時間切れ・権限はエージェントに返して書き直させる
        return _text(f"SQL の失敗: {type(e).__name__}: {str(e)[:500]}", True)
    return _text(redact(json.dumps(r, ensure_ascii=False, default=str)))


def job_log(arguments: dict, blob_key_of, store_get) -> dict:
    """blob_key_of(job_id) → オブジェクトストアのキーか None。store_get(key) → bytes"""
    jid = arguments.get("job_id")
    tail = arguments.get("tail") or DEFAULT_TAIL
    if not isinstance(jid, int) or isinstance(jid, bool) or jid < 1:
        return _text("job_id は 1 以上の整数", True)
    if not isinstance(tail, int) or isinstance(tail, bool) or not 1 <= tail <= MAX_TAIL:
        return _text(f"tail は 1〜{MAX_TAIL}", True)
    key = blob_key_of(jid)
    if not key:
        return _text(f"job {jid} のログはない（終わっていない、または残していない）", True)
    try:
        data = store_get(key)
    except Exception as e:  # noqa: BLE001
        return _text(f"ログを読めない: {type(e).__name__}", True)
    lines = data.decode("utf-8", errors="replace").splitlines()
    return _text(redact("\n".join(lines[-tail:])))


def github(arguments: dict, repo: str | None, get) -> dict:
    """get(path) → JSON。path はその製品のリポジトリの下の、決まった場所だけ"""
    path = (arguments.get("path") or "").strip().lstrip("/")
    if not repo:
        return _text("この調査は製品が決まっていない", True)
    if ".." in path or not GITHUB_PATH.match(path):
        return _text("読めない場所。issues・pulls・actions/runs・commits の下だけ（例 pulls/123/reviews）", True)
    try:
        data = get(f"repos/{repo}/{path}")
    except Exception as e:  # noqa: BLE001
        return _text(f"GitHub を読めない: {type(e).__name__}: {str(e)[:200]}", True)
    return _text(redact(json.dumps(data, ensure_ascii=False, default=str)))


def handle(tool: str, arguments: dict, grant: dict, *, blob_key_of, store_get, gh_get, query=investigate_db.query) -> dict:
    if tool == "inv_sql":
        return sql(arguments, query)
    if tool == "inv_job_log":
        return job_log(arguments, blob_key_of, store_get)
    if tool == "inv_github":
        return github(arguments, grant.get("repo"), gh_get)
    raise RuntimeError(f"知らない道具: {tool}")
