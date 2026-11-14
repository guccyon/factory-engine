"""操業コンソールのサーバーからの知らせ（SSE。ADR 0059 の 7、#476）。

GET /api/stream は、工場の記録が動いたとき（events の最新の id・ジョブの状態の変わり目）に `event: changed` を送る。
画面はそれを受けて /api/state を読み直す。中身を毎回送らないので、つないでいる画面が増えても重くならない。
つながらない・切れたときは、画面は今までどおり 10 秒ごとの読み直しに戻る（console.js）。
1 本のつなぎは MAX_SEC で閉じる（ブラウザの EventSource が自動でつなぎ直す）。
"""
from __future__ import annotations

import time

POLL_SEC = 2          # 記録を見に行く間隔
KEEPALIVE_SEC = 15    # 何も起きないときの合図（途中の機器につなぎを切らせない）
MAX_SEC = 600         # 1 本のつなぎの長さ

VERSION_SQL = """select (select coalesce(max(id), 0) from events) as e,
                        (select count(*) from jobs where state in ('queued','running')) as a,
                        (select coalesce(max(greatest(enqueued_at, coalesce(started_at, enqueued_at), coalesce(finished_at, enqueued_at))),
                                         'epoch'::timestamptz) from jobs) as j"""


def version(cur) -> str:
    cur.execute(VERSION_SQL)
    r = cur.fetchone()
    return f"{r['e']}:{r['a']}:{r['j'].timestamp() if r['j'] else 0}"


def serve(wfile, db, *, clock=time.monotonic, sleep=time.sleep, max_sec: int = MAX_SEC) -> None:
    """つながっている間、変わったら知らせる。書けなくなったら（画面が閉じた）終わる"""
    start = last_send = clock()
    seen = None
    try:
        wfile.write(b"retry: 5000\n\n")
        wfile.flush()
        while clock() - start < max_sec:
            with db() as conn, conn.cursor() as cur:
                v = version(cur)
            if v != seen:
                wfile.write(f"event: changed\ndata: {v}\n\n".encode())
                wfile.flush()
                seen, last_send = v, clock()
            elif clock() - last_send >= KEEPALIVE_SEC:
                wfile.write(b": keepalive\n\n")
                wfile.flush()
                last_send = clock()
            sleep(POLL_SEC)
    except (BrokenPipeError, ConnectionResetError):
        return
