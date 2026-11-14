"""操業コンソールの「問い合わせ」のページの API（ADR 0059、#474）。人から事業部への入口。

窓口（desk.py。ADR 0030・0039）の関数をそのまま使う。窓口の API（desk_api.py）・MCP から出した相談と同じ一覧に並ぶ。
コンソールは Gen1 ではログインがないので、操作する人は運用主（CONSOLE_OPERATOR）として扱い、すべての製品の相談が見える。

  GET  /api/ask                        相談の一覧（新しい順）と、似た依頼を探すための題
  GET  /api/ask/<id>                   会話・今のステップ・下書き・チケット
  POST /api/ask                        {"kind": "request"|"inquiry", "product", "body", "level"?, "visibility"?: "all"|"me"}
                                       visibility は依頼ごとに見える人（既定は all＝全員）。今は記録だけで、窓口の API の見え方はまだ変えない
  POST /api/ask/<id>/messages          {"body"} 返事・言い足し。提出の後は {"answer": "accept"|"more"|"return", "body"?}
  POST /api/ask/<id>/confirm           {"draft_id", "level"?} 下書きを確かめて投入する。実行の確認は {"answer": "run"|"cancel", "note"?}
  POST /api/ask/<id>/close             {"reason"?}

依頼の本文は窓口と同じくデータとして記録するだけで、ここではシェルにもプロンプトにも渡さない（読むのは窓口の相談役のジョブ）。
"""
from __future__ import annotations

import re

import desk

VISIBILITY = ("all", "me")


def operator(name: str) -> dict:
    return {"name": name, "role": "operator", "products": []}


def get(cur, path: str, who: dict) -> tuple[int, dict] | None:
    """GET の /api/ask… を処理する。ほかのパスは None"""
    if path == "/api/ask":
        return 200, {"conversations": desk.list_conversations(cur, who, limit=200)}
    m = re.fullmatch(r"/api/ask/(\d+)", path)
    if m:
        try:
            return 200, desk.read_conversation(cur, who, int(m.group(1)))
        except desk.DeskError as e:
            return e.status, {"error": str(e)}
    return None


def post(cur, path: str, body: dict, who: dict) -> tuple[int, dict] | None:
    """POST の /api/ask… を処理する。ほかのパスは None"""
    try:
        if path == "/api/ask":
            vis = body.get("visibility") or "all"
            if vis not in VISIBILITY:
                return 400, {"error": "見える人は all（全員）か me（自分だけ）"}
            c = desk.open_conversation(cur, who, body.get("kind") or "request", body.get("product") or "", body.get("body") or "",
                                       level=body.get("level") or None, visibility=vis)
            return 200, {"conversation": desk.view_conversation(c)}
        m = re.fullmatch(r"/api/ask/(\d+)/(messages|confirm|close)", path)
        if not m:
            return None
        cid, what = int(m.group(1)), m.group(2)
        if what == "messages":
            if body.get("answer"):
                return 200, desk.receive(cur, who, cid, str(body["answer"]), str(body.get("body") or ""))
            return 200, desk.post_message(cur, who, cid, body.get("body") or "")
        if what == "confirm":
            if body.get("answer"):
                return 200, desk.confirm_route(cur, who, cid, str(body["answer"]), str(body.get("note") or ""))
            return 200, desk.confirm_draft(cur, who, cid, int(body.get("draft_id") or 0), body.get("level") or None)
        return 200, desk.close_conversation(cur, who, cid, str(body.get("reason") or ""))
    except desk.DeskError as e:
        return e.status, {"error": str(e)}
    except (TypeError, ValueError):
        return 400, {"error": "値の形が違う"}
