#!/usr/bin/env python3
"""工場の操業コンソール（ADR 0009）。工場のいまを見て、順番・保留・停止を操作する常駐サービス。

- 操業 API（JSON）が正本で、画面はその利用者の 1 つ。将来の顧客向けアプリも同じ API を使う
  GET  /api/inbox                         受信トレイ: 人が対応するものの一覧（ADR 0026 の決定 7。inbox.py）。/api/state にも inbox として入る
  GET  /api/usage?days=14                 使用量: トークンの消費と費用（日ごと・役割・モデル・契約の週）とアカウントを足すかの判断の材料（usage.py）
  GET  /usage                             同じ中身のページ（usage_page.py）
  GET  /api/state                         ライン（依存待ち・順番待ち・作業中・PR の検査と判定・人の出番・今日の済み）と工場の状態
  POST /api/tickets/<id>/priority         {"level": "p0".."p3"} か {"priority": 0..100}。p0 は作業中のものを止めてでも先に流す
  POST /api/tickets/<id>/hold | resume    順番待ちから一時的に外す / 戻す
  POST /api/jobs/<id>/stop                ジョブを止める（作業中ならコンテナは dispatcher が止める）
  POST /api/releases/approve              {"release_id"} 承認ボタン。staging で確かめた版を prod に出す（ADR 0012）
  POST /api/releases/rollback             {"repo", "env"} その環境を 1 つ前の版に戻す
  POST /api/releases/reseed               {"repo"} 触れる環境（staging）の DB を空にして合成シードから入れ直す（ADR 0028 の touchable_env）
  POST /api/factory/drain | resume        {"reason"} ラインの一時停止（順番待ちを取り出さない）と再開。工場の更新のため（ADR 0020）
  POST /api/host/keep-awake               {"prevent_sleep": true|false} 工場の稼働中にホスト（Mac）のスリープを止めるか
  GET  /api/host/keep-awake?factory=running|stopped   ホストの keep-awake.sh が 2 分ごとに問い合わせる（居ることの知らせを兼ねる）
  POST /api/runners/<id>/uninstall        {"reason"?} そのランナーを外す（次の連絡でランナーが自分の常駐を外し、鍵を捨てる）。/api/state の runners に一覧
  POST /api/runners/pairs/<pair_id>/approve | reject   ペアリングの依頼を許可・拒否する（ADR 0033。Mac 側の y とそろうと繋がる）。/api/state の runner_pairs に一覧
  POST /api/inbox/answer                  {"key", "option", "note"?, "on"?} 受信トレイの 1 件にその場で答える（ADR 0034。inbox.answer が唯一の口で、先に答えた方が勝つ）
  POST /api/choices/<id>/choose           {"option", "reason"?} 人の選択（画面の案・抜き取り。ADR 0025）。下の 4 つも inbox.answer を通る
  POST /api/choices/<id>/return           {"reason"} どの案も選ばず作り手に戻す
  POST /api/choices/<id>/flag             {"note", "option"?} 違和感の印（教師信号 taste_flag）
  POST /api/choices/<id>/seen             {"note"?} 実装後の画面を見て閉じる（exit_capture。ADR 0025 の 4）
  GET  /api/choices/<id>/image/<option>   案の画像（オブジェクトストアの choices/ の下から中継する）
  GET  /review/<UXSpec の id>             デザインのレビューページ（ADR 0026）。PRD の要約・ユーザーフロー・画面定義書・案・検査役の判断と、承認・直し・違和感
  GET  /review/<UXSpec の id>/image/<名前>.png   レビューページの案の画像（案の HTML はこの origin では開かない）
- 出荷の操作は events に依頼を書くだけで、docker を動かすのは shipper（このサービスに docker のソケットは渡さない）
- 操作は ops.py を通り、dispatcher・supervisor・factoryctl と同じ振る舞いになる。誰が操作したかは events に残す
- 外部のサービス・CDN に依存しない（閉じた網・オンプレミスで動かせるように）。GitHub にも触らない（制御プレーンだけを読む）
- 守り: 開いてよいホスト名だけを受ける（DNS rebinding）。操作は画面に埋めた合言葉のヘッダーがあるときだけ受ける（他サイトからの CSRF）
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import psycopg
from psycopg.rows import dict_row

import choice
import drain
import line
import inbox
import judgment
import ops
import prototype
import review_page
import runners
import ship
import touch
import usage
import usage_page

ENV = os.environ
DATABASE_URL = ENV["DATABASE_URL"]
PORT = int(ENV.get("CONSOLE_PORT", "8790"))
ALLOWED_HOSTS = {h.strip() for h in ENV.get("CONSOLE_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()}
OPERATOR = ENV.get("CONSOLE_OPERATOR", "builder")
MAX_PARALLEL = int(ENV.get("MAX_PARALLEL", "2"))
LINE_LOW_BELOW = int(ENV.get("WATCH_LINE_LOW_BELOW", "2"))   # 段の残りがこれを切ると赤く出す（番人の知らせと同じ値。dispatcher.py）
JOB_TIMEOUT = int(ENV.get("JOB_TIMEOUT_SEC", "2400"))
TZ = ENV.get("REPORT_TZ", "Asia/Tokyo")
PROTOTYPE_ON = ENV.get("PROTOTYPE_PORT", "") not in ("", "0")   # 動くプロトタイプの配り口（別ポート。prototype.py）。未設定なら出さない
TOKEN = secrets.token_urlsafe(24)   # 起動ごとに変わる。画面を開き直せば新しいものが入る
HOST_AGENT_FRESH_SEC = 600   # これより長く keep-awake.sh から連絡がなければ、ホストの見張りは居ないとみなす（トグルを出さない）
host_agent: dict = {}
_store = None


def read_design(key: str) -> bytes | None:
    """オブジェクトストアの design/ の下を読む（レビューページの画像と動くプロトタイプ）"""
    if not key or not key.startswith("design/") or ".." in key or store() is None:
        return None
    obj = store().get_object(ENV.get("OBJECTSTORE_BUCKET", "factory-artifacts"), key)
    try:
        return obj.read()
    finally:
        obj.close()
        obj.release_conn()


def store():
    """案の画像を読むオブジェクトストア。設定がなければ None（画像なしで選択の画面は動く）"""
    global _store
    if _store is None and ENV.get("OBJECTSTORE_ENDPOINT"):
        from minio import Minio
        _store = Minio(ENV["OBJECTSTORE_ENDPOINT"], access_key=ENV.get("OBJECTSTORE_ACCESS_KEY"), secret_key=ENV.get("OBJECTSTORE_SECRET_KEY"), secure=False)
    return _store         # keep-awake.sh の最後の連絡（プロセスの中だけ。再起動しても次の問い合わせで戻る）
PR_STAGE_EVENTS = ("supervisor.merge_decided", "gate.evaluated", "supervisor.judge_enqueued", "judgment.judged", "supervisor.inspector_approved")


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)


def iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def item(stage: str, **kw) -> dict:
    p = kw.get("priority")
    return {"stage": stage, **kw, **({"level": ops.level_of(p)} if p is not None else {})}


# ------------------------------------------------------------------ 読む（制御プレーンだけ）
def snapshot(cur) -> dict:
    cur.execute("select set_config('timezone', %s, false)", (TZ,))
    cur.execute("select max(occurred_at) as last, extract(epoch from now() - max(occurred_at))::int as age from events where name='supervisor.heartbeat'")
    hb = cur.fetchone()
    mode, basis = judgment.current_mode(cur, ENV.get("FACTORY_MODE") or "attended", ENV.get("MODE_SCHEDULE", ""))
    cur.execute("select coalesce(sum(nullif(payload->>'cost_usd','')::numeric), 0) as usd from events "
                "where name='job.finished' and occurred_at >= date_trunc('day', now())")
    cost = float(cur.fetchone()["usd"])
    cur.execute("select key, category, what, waiting_for, started_at from stoppages where ended_at is null order by started_at")
    stoppages = [{**r, "started_at": iso(r["started_at"])} for r in cur.fetchall()]

    items: list[dict] = []
    # 依存待ち・投入待ち（supervisor が写した planned のチケット）
    cur.execute("select * from tickets where state='planned' order by priority desc, created_at")
    for t in cur.fetchall():
        items.append(item("waiting" if t["waiting_on"] else "ready", ticket_id=t["id"], ref=t["source_ref"], title=t["title"], url=t["url"],
                          priority=t["priority"], held=t["held"], waiting_on=t["waiting_on"]))
    # 順番待ち・作業中（ジョブ）
    cur.execute("select j.*, t.title, t.source_ref, t.url, t.held, extract(epoch from now() - j.started_at)::int as elapsed "
                "from jobs j left join tickets t on t.id=j.ticket_id where j.state in ('queued','running') "
                "order by j.state desc, j.priority desc, j.enqueued_at")
    rank = 0
    for j in cur.fetchall():
        common = dict(ticket_id=j["ticket_id"], job_id=j["id"], kind=j["kind"], attempt=j["attempt"], priority=j["priority"],
                      ref=j["source_ref"] or f"{j['repo']}#{j['issue_number']}", title=j["title"] or "", url=j["url"], held=bool(j["held"]))
        if j["state"] == "queued":
            rank += 0 if j["held"] else 1
            items.append(item("queued", rank=None if j["held"] else rank, enqueued_at=iso(j["enqueued_at"]), **common))
        else:
            items.append(item("running", elapsed_sec=j["elapsed"], timeout_sec=JOB_TIMEOUT, stopping=j["cancel_requested"],
                              preempted_for=j["preempted_for"], **common))
    # 工場の PR（検査・判定・マージ待ち・人の出番）
    cur.execute("select distinct on (a.payload->>'pr_url') a.payload->>'pr_url' as pr_url, a.correlation_id, "
                "coalesce((a.payload->>'incomplete')::boolean, false) as incomplete, a.created_at "
                "from artifacts a where a.type='PR' and a.payload->>'pr_url' like 'https://github.com/%' and a.created_at > now() - interval '14 days' "
                "and not exists (select 1 from events e where e.name in ('pr.merged','pr.closed_unmerged') and e.payload->>'pr_url'=a.payload->>'pr_url') "
                "order by a.payload->>'pr_url', a.id desc")
    for p in cur.fetchall():
        cur.execute("select name, payload from events where name = any(%s) and payload->>'pr_url'=%s order by id desc limit 1",
                    (list(PR_STAGE_EVENTS), p["pr_url"]))
        e = cur.fetchone()
        cur.execute("select key, what from stoppages where ended_at is null and key in (%s, %s)", (f"judge:{p['pr_url']}", f"sendback:{p['pr_url']}"))
        human = cur.fetchone()
        ref = p["correlation_id"].split(":", 1)[1]
        cur.execute("select id, title, priority from tickets where source='github' and source_ref=%s", (ref,))
        t = cur.fetchone() or {}
        name, pl = (e["name"], e["payload"]) if e else (None, {})
        if p["incomplete"]:
            stage, note = "human", "途中で終わったドラフト（仕上げるかやり直すか）"
        elif human:
            stage, note = "human", human["what"]
        elif name == "supervisor.merge_decided" and pl.get("table") == "human_approve":
            stage, note = "human", "マージに人の承認が要る（" + ", ".join(pl.get("change_types") or []) + "）"
        else:
            stage, note = "review", {None: "CI 待ち", "supervisor.merge_decided": "検査中", "gate.evaluated": "判定待ち",
                                     "supervisor.judge_enqueued": "検査役の承認待ち" if pl.get("skipped") else "判定中",
                                     "judgment.judged": "検査役の承認待ち", "supervisor.inspector_approved": "マージ待ち"}[name]
        items.append(item(stage, ticket_id=t.get("id"), ref=ref, title=t.get("title") or "", pr_url=p["pr_url"], note=note, priority=t.get("priority")))
    # 今日の済み・失敗
    cur.execute("select e.payload->>'pr_url' as pr_url, e.correlation_id, e.occurred_at, t.title from events e "
                "left join tickets t on t.source='github' and t.source_ref=split_part(e.correlation_id, ':', 2) "
                "where e.name='pr.merged' and e.occurred_at >= date_trunc('day', now()) order by e.id desc")
    for r in cur.fetchall():
        items.append(item("done", ref=r["correlation_id"].split(":", 1)[1], title=r["title"] or "", pr_url=r["pr_url"], at=iso(r["occurred_at"]), note="マージ"))
    cur.execute("select e.job_id, e.correlation_id, e.occurred_at, e.payload->>'error' as error, t.title from events e "
                "left join jobs j on j.id=e.job_id left join tickets t on t.id=j.ticket_id "
                "where e.name='job.finished' and e.payload->>'state'='failed' and e.occurred_at >= date_trunc('day', now()) order by e.id desc limit 20")
    for r in cur.fetchall():
        items.append(item("done", job_id=r["job_id"], ref=(r["correlation_id"] or ":").split(":", 1)[1], title=r["title"] or "",
                          at=iso(r["occurred_at"]), note=f"失敗（{r['error']}）", failed=True))
    running = sum(1 for i in items if i["stage"] == "running")
    cur.execute("select count(*) as n from tickets where state='planned' and not held")
    planned = cur.fetchone()["n"]
    return {
        "releases": releases(cur),
        "runners": runners.view(cur),
        "runner_pairs": runners.pending_pairs(cur),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "factory": {"heartbeat_age_sec": hb["age"], "mode": mode, "mode_basis": basis, "running": running, "max_parallel": line.spec(cur)["workers"], "scale": line.status(cur),
                    "cost_today_usd": round(cost, 2), "open_stoppages": stoppages, "host": host_view(cur), "line": drain.status(cur),
                    "planned": planned, "line_low_below": LINE_LOW_BELOW},
        "levels": {k: {"priority": v[0], "name": v[1], "meaning": v[2]} for k, v in ops.LEVELS.items()},
        "items": items,
    }


def host_view(cur) -> dict | None:
    """ホストの見張り（keep-awake.sh）が居るときだけ返す。居なければ（EC2 や Mac 以外）トグルを出さない"""
    seen = host_agent.get("seen")
    if not seen:
        return None
    age = int((datetime.now(timezone.utc) - seen).total_seconds())
    if age > HOST_AGENT_FRESH_SEC:
        return None
    prevent = ops.keep_awake(cur)
    return {"agent_age_sec": age, "prevent_sleep": prevent, "factory_running": host_agent["factory_running"],
            "sleep_prevented": prevent and host_agent["factory_running"]}


def releases(cur) -> list[dict]:
    """製品ごとの出荷の状態（ADR 0012）: 環境ごとのいまの版と前の版、処理中の依頼、最後の結果"""
    cur.execute("select distinct payload->>'repo' as repo from events where name='release.created' order by 1")
    out = []
    for repo in [r["repo"] for r in cur.fetchall()]:
        st = ship.status(cur, repo)
        cur.execute("select name, payload->>'release_id' as rid, payload->>'env' as env from events e where name = any(%s) "
                    "and payload->>'release_id' like %s and not exists (select 1 from events r where r.name = any(%s) "
                    "and (r.payload->>'request_id')::bigint = e.id) order by id", (list(ship.REQUESTS), f"{repo}@%", list(ship.RESULTS)))
        pending = [{"request": r["name"].split(".", 1)[1], "release_id": r["rid"], "env": r["env"]} for r in cur.fetchall()]
        cur.execute("select id, occurred_at, name, payload from events where name = any(%s) and payload->>'repo'=%s order by id desc limit 1",
                    (list(ship.RESULTS), repo))
        last = cur.fetchone()
        t = touch.latest(cur, repo)
        if t:   # 触れる環境の入口（URL・QR・ログインの手がかり）。QR は工場の中で作る（外部のサービスに依存しない）
            t = {k: t.get(k) for k in ("release_id", "env", "reachable_from_phone", "accounts", "note", "at")} | {
                "entries": [{**e, "qr_svg": touch.qr_svg(e["url"]) if e.get("qr") else None} for e in t.get("entries") or []]}
        out.append({"repo": repo, "environments": st["environments"], "pending": pending, "prod_held": ship.prod_held(cur, repo), "stage": ship.stage_of(cur, repo), "touch": t,
                    "last": {"name": last["name"], "at": iso(last["occurred_at"]), **{k: last["payload"].get(k) for k in
                             ("release_id", "env", "seconds", "from", "error")}} if last else None})
    return out


# ------------------------------------------------------------------ 操作（ops を通す）
ANSWER_CODES = {"answered": 200, "noted": 200, "not_found": 404, "invalid": 400, "already": 409, "failed": 409}


def answered(res: str, msg: str) -> tuple[int, dict]:
    code = ANSWER_CODES.get(res, 409)
    return (code, {"result": res, "message": msg}) if code == 200 else (code, {"result": res, "error": msg})


def act(cur, path: str, body: dict) -> tuple[int, dict]:
    by = f"console:{OPERATOR}"
    reason = str(body.get("reason") or "")[:500]
    m = re.fullmatch(r"/api/tickets/(\d+)/(priority|hold|resume)", path)
    if m:
        tid, what = int(m.group(1)), m.group(2)
        if what == "priority":
            if body.get("level") in ops.LEVELS:
                value = ops.LEVELS[body["level"]][0]
            elif isinstance(body.get("priority"), int):
                value = body["priority"]
            else:
                return 400, {"error": "level（p0〜p3）か priority（0〜100）が要る"}
            t = ops.set_priority(cur, tid, value, by, reason)
        else:
            t = ops.set_held(cur, tid, what == "hold", by, reason)
        return (200, {"ticket": {"id": t["id"], "priority": t["priority"], "held": t["held"]}}) if t else (404, {"error": "no such ticket"})
    if path == "/api/releases/approve":   # 本番に出すのも受信トレイの件（release:<id>）への答え。開いていなければ出荷の側の理由を返す
        rid = str(body.get("release_id") or "")
        res, msg = inbox.answer(cur, f"release:{rid}", "approve", by, "console")
        if res == "not_found":
            ok, msg = ship.request_approve(cur, rid, by, "console")
            return (200, {"message": msg}) if ok else (409, {"error": msg})
        return answered(res, msg)
    if path == "/api/releases/rollback":
        repo, env = str(body.get("repo") or ""), str(body.get("env") or "prod")
        if env not in ("staging", "prod"):
            return 400, {"error": "env は staging か prod"}
        ok, msg = ship.request_rollback(cur, repo, env, by, "console")
        return (200, {"message": msg}) if ok else (409, {"error": msg})
    if path == "/api/releases/reseed":
        ok, msg = ship.request_reseed(cur, str(body.get("repo") or ""), "staging", by, "console")
        return (200, {"message": msg}) if ok else (409, {"error": msg})
    if path == "/api/line/scale":   # ラインの流量の宣言（ADR 0028）。{workers, tickets, governance, desk} のうち渡したものだけ変える
        try:
            line.scale(cur, by, reason, **{k: body.get(k) for k in line.FIELDS})
        except (ValueError, TypeError) as e:
            return 400, {"error": str(e)}
        return 200, {"scale": line.status(cur)}
    if path in ("/api/factory/drain", "/api/factory/resume"):
        drain.set_drain(cur, path.endswith("/drain"), by, reason)
        return 200, {"line": drain.status(cur)}
    if path == "/api/host/keep-awake":
        if not isinstance(body.get("prevent_sleep"), bool):
            return 400, {"error": "prevent_sleep（true か false）が要る"}
        return 200, {"prevent_sleep": ops.set_keep_awake(cur, body["prevent_sleep"], by, reason)}
    m = re.fullmatch(r"/api/choices/(\d+)/(choose|return|flag|seen)", path)
    if m:   # 人の選択とレビューページのボタン。答えは受信トレイの口（inbox.answer）を通る（ADR 0034 の決定 1）
        aid, what = int(m.group(1)), m.group(2)
        opt = str(body.get("option") or "")
        if what == "flag":
            if choice.get(cur, aid) is None:
                return 404, {"error": "no such choice"}
            if not inbox.find(cur, f"choice:{aid}"):   # 閉じた依頼にも印は付けられる（受信トレイの件ではないので印だけ残す）
                ok, msg = choice.flag(cur, aid, str(body.get("note") or ""), by, opt or None)
                return (200, {"message": msg}) if ok else (409, {"error": msg})
            return answered(*inbox.answer(cur, f"choice:{aid}", "flag", by, "console", note=str(body.get("note") or ""), on=opt or None))
        return answered(*inbox.answer(cur, f"choice:{aid}", {"choose": opt, "return": "return", "seen": "seen"}[what], by, "console", note=reason))
    if path == "/api/inbox/answer":   # 受信トレイの件の上のボタン。{"key", "option", "note"?, "on"?}
        return answered(*inbox.answer(cur, str(body.get("key") or ""), str(body.get("option") or ""), by, "console",
                                      note=str(body.get("note") or reason), on=str(body["on"]) if body.get("on") else None))
    m = re.fullmatch(r"/api/runners/pairs/([0-9a-f]{32})/(approve|reject)", path)
    if m:
        ok, msg = runners.decide_pair(cur, m.group(1), m.group(2) == "approve", by, reason)
        return (200, {"message": msg}) if ok else (409, {"error": msg})
    m = re.fullmatch(r"/api/runners/([0-9a-f]{32})/uninstall", path)
    if m:
        ok, msg = runners.request_uninstall(cur, m.group(1), by, reason)
        return (200, {"message": msg}) if ok else (404, {"error": msg})
    m = re.fullmatch(r"/api/jobs/(\d+)/stop", path)
    if m:
        before = ops.stop_job(cur, int(m.group(1)), by, reason)
        return (200, {"stopped_from": before}) if before else (409, {"error": "job is not active"})
    return 404, {"error": "not found"}


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ALLOWED_HOSTS

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        if not self._host_ok():
            return self._json(403, {"error": "host not allowed"})
        if self.path == "/api/state":
            with db() as conn, conn.cursor() as cur:
                return self._json(200, {**snapshot(cur), "choices": choice.open_choices(cur), "inbox": inbox.view(cur)})
        if self.path == "/api/inbox":
            with db() as conn, conn.cursor() as cur:
                return self._json(200, inbox.view(cur))
        if urlsplit(self.path).path in ("/api/usage", usage_page.PATH):
            u = urlsplit(self.path)
            days = parse_qs(u.query).get("days", ["14"])[0]
            with db() as conn, conn.cursor() as cur:
                v = usage.view(cur, days=min(max(int(days) if days.isdigit() else 14, 1), 90))
            if u.path == usage_page.PATH:
                return self._send(200, usage_page.render(v).encode(), "text/html; charset=utf-8")
            return self._json(200, v)
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE.replace("__TOKEN__", TOKEN).encode(), "text/html; charset=utf-8")
        url = urlsplit(self.path)
        m = re.fullmatch(r"/api/choices/(\d+)/image/([A-Za-z0-9_-]{1,20})", url.path)
        if m:
            return self._choice_image(int(m.group(1)), m.group(2))
        m = re.fullmatch(r"/review/(\d+)", url.path)
        if m:
            with db() as conn, conn.cursor() as cur:
                ctx = review_page.load(cur, int(m.group(1)))
            if not ctx:
                return self._json(404, {"error": "no such design"})
            return self._send(200, review_page.render(ctx, TOKEN, prototype.PUBLIC_URL if PROTOTYPE_ON else "").encode(), "text/html; charset=utf-8")
        m = re.fullmatch(r"/review/(\d+)/image/([A-Za-z0-9][A-Za-z0-9_-]{0,31}\.png)", url.path)
        if m:
            return self._review_image(int(m.group(1)), m.group(2))
        if url.path == "/api/host/keep-awake":   # ホストの keep-awake.sh から。読むだけで工場の状態は変えない（合言葉は要らない）
            q = parse_qs(url.query)
            host_agent.update(seen=datetime.now(timezone.utc), factory_running=q.get("factory", [""])[0] == "running")
            with db() as conn, conn.cursor() as cur:
                return self._json(200, {"prevent_sleep": ops.keep_awake(cur)})
        if self.path == "/healthz":
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def _choice_image(self, aid: int, option_id: str):
        with db() as conn, conn.cursor() as cur:
            cur.execute("select payload from artifacts where id=%s and type='HumanChoice'", (aid,))
            row = cur.fetchone()
        opt = next((o for o in (row["payload"]["options"] if row else []) if o["id"] == option_id), None)
        key = (opt or {}).get("image_key")
        if not key or not choice.image_key_ok(key) or store() is None:
            return self._json(404, {"error": "no image"})
        obj = store().get_object(ENV.get("OBJECTSTORE_BUCKET", "factory-artifacts"), key)
        try:
            data = obj.read()
        finally:
            obj.close()
            obj.release_conn()
        self._send(200, data, "image/png" if key.endswith(".png") else "application/octet-stream")

    def _review_image(self, aid: int, name: str):
        """レビューページの案の画像。その UXSpec の files に載っている PNG だけを、オブジェクトストアの design/ の下から中継する"""
        with db() as conn, conn.cursor() as cur:
            cur.execute("select payload->'files'->>%s as key from artifacts where id=%s and type='UXSpec'", (name, aid))
            row = cur.fetchone()
        data = read_design((row or {}).get("key"))
        if data is None:
            return self._json(404, {"error": "no image"})
        self._send(200, data, "image/png")

    def do_POST(self):  # noqa: N802
        if not self._host_ok():
            return self._json(403, {"error": "host not allowed"})
        if not secrets.compare_digest(self.headers.get("X-Console-Token", ""), TOKEN):
            return self._json(403, {"error": "token required（画面を開き直してください）"})
        n = int(self.headers.get("Content-Length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid json"})
        with db() as conn, conn.cursor() as cur:
            code, out = act(cur, self.path, body if isinstance(body, dict) else {})
        self._json(code, out)

    def log_message(self, fmt, *a):  # 操作だけ出す
        if self.command == "POST":
            sys.stdout.write(f"[console] {self.command} {self.path} {a[1] if len(a) > 1 else ''}\n")


PAGE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>工場の操業コンソール</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--sub:#5b6474;--line:#e2e5ea;--accent:#2f5bd3;--p0:#c62828;--p1:#d97706;--p2:#4b5563;--p3:#9ca3af;--ok:#1f7a4d;--warn:#b45309}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--ink:#e6e9ef;--sub:#9aa3b2;--line:#2a313d;--accent:#7aa2ff;--p2:#9ca3af;--p3:#6b7280}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
header{padding:16px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;border-bottom:1px solid var(--line);background:var(--card)}
h1{font-size:16px;margin:0 12px 0 0}.stat{white-space:nowrap;padding:4px 10px;border:1px solid var(--line);border-radius:999px;color:var(--sub)}.stat b{color:var(--ink)}
.bad b{color:var(--p0)}.sw{cursor:pointer;display:inline-flex;gap:8px;align-items:center}.sw b.on{color:var(--ok)}.sw input{appearance:none;-webkit-appearance:none;margin:0;width:36px;height:20px;border-radius:999px;background:var(--line);position:relative;cursor:pointer;transition:background .15s}.sw input::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.3);transition:left .15s}.sw input:checked{background:var(--ok)}.sw input:checked::after{left:18px}.sw input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}main{padding:16px;display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}
.lane{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;min-height:80px}
.lane h2{font-size:13px;margin:0 0 8px;color:var(--sub);display:flex;justify-content:space-between}
.it{border:1px solid var(--line);border-left:4px solid var(--p2);border-radius:8px;padding:8px;margin:0 0 8px}
.it.p0{border-left-color:var(--p0)}.it.p1{border-left-color:var(--p1)}.it.p3{border-left-color:var(--p3)}.it.held{opacity:.6}
.t{font-weight:600;word-break:break-word}.m{color:var(--sub);font-size:12px}.m a{color:var(--accent)}
.badge{display:inline-block;font-size:11px;padding:0 6px;border-radius:4px;color:#fff;background:var(--p2);margin-right:4px}
.badge.p0{background:var(--p0)}.badge.p1{background:var(--p1)}.badge.p3{background:var(--p3)}
.acts{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}button,select{font:inherit;font-size:12px;padding:3px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);cursor:pointer}
button.danger{color:var(--p0)}button.armed{background:var(--p0);color:#fff}#stops{padding:0 16px}#stops div{margin-top:8px;padding:8px;border-radius:8px;border:1px solid var(--warn);color:var(--warn);background:var(--card)}
#ship{padding:0 16px}.rel{margin-top:12px;padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--card);display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}
.rel .env b{font-size:15px}.touch{flex-basis:100%;display:flex;flex-wrap:wrap;gap:12px}.touch .e{border:1px solid var(--line);border-radius:8px;padding:8px;max-width:260px}
.touch .e svg{display:block;width:160px;height:auto;margin-top:6px}.touch code{font-size:11px;word-break:break-all}button.go{background:var(--ok);color:#fff;border-color:var(--ok)}a.btn{font-size:12px;padding:3px 10px;border-radius:6px;background:var(--ok);color:#fff;text-decoration:none}
#runners{padding:0 16px}.rn{margin-top:12px;padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--card)}.rn h2{font-size:13px;margin:0 0 6px;color:var(--sub)}.rn table{border-collapse:collapse;width:100%;font-size:12px}.rn td,.rn th{text-align:left;padding:4px 8px 4px 0;border-top:1px solid var(--line);vertical-align:top}.rn th{color:var(--sub);font-weight:400;border-top:0}.rn code{font-size:11px;word-break:break-all}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--p3);margin-right:4px}.dot.on{background:var(--ok)}
#choices{padding:0 16px}.ch{margin-top:12px;padding:12px;border:1px solid var(--accent);border-radius:10px;background:var(--card)}
.opts{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));margin:8px 0}.opt{border:1px solid var(--line);border-radius:8px;padding:8px}
.opt.rec{border-color:var(--ok)}.opt img{width:100%;border-radius:6px;border:1px solid var(--line);background:#fff}.ch input{font:inherit;font-size:12px;padding:3px 6px;border:1px solid var(--line);border-radius:6px;width:min(360px,100%)}
#bell{position:relative;display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;font-size:13px}#bell svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2}
#bell .n{min-width:18px;padding:0 5px;border-radius:999px;background:var(--line);color:var(--sub);font-size:11px;font-weight:700;text-align:center}#bell.has .n{background:var(--p0);color:#fff}
#inbox{padding:0 16px}#inbox[hidden]{display:none}.ib{margin-top:12px;padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--card)}.ib h2{font-size:14px;margin:0 0 8px}
.ib a{color:var(--accent)}.ib ul{list-style:none;margin:0;padding:0}.ib li{padding:8px 0;border-top:1px solid var(--line);display:grid;gap:2px}.ib li:first-child{border-top:0}.who{display:inline-block;font-size:11px;padding:0 6px;border-radius:4px;border:1px solid var(--line);color:var(--sub);margin-right:6px}
.dt{margin:4px 0 0;display:grid;grid-template-columns:max-content 1fr;gap:2px 10px;font-size:12px}.dt dt{color:var(--sub)}.dt dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}
@media(max-width:560px){.dt{grid-template-columns:1fr}.dt dd{margin-bottom:4px}}
.ans{margin-top:4px}.ans input{font:inherit;font-size:12px;padding:3px 6px;border:1px solid var(--line);border-radius:6px;width:min(320px,100%)}.ans.done{color:var(--ok);font-size:12px}.ib li.answered{opacity:.7}
.ch:target,.rel:target,#ship:target{outline:2px solid var(--accent);outline-offset:2px}
.zm{display:block;cursor:zoom-in}
#zoom{position:fixed;inset:0;z-index:10;background:#0f1115;display:flex;flex-direction:column}#zoom[hidden]{display:none}
#zoom .bar{display:flex;gap:8px;align-items:center;padding:8px 12px;color:#e6e9ef;font-size:13px}#zoom .bar .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#zoom .bar a,#zoom .bar button{font:inherit;font-size:12px;padding:4px 10px;border:1px solid #4b5563;border-radius:6px;background:transparent;color:#e6e9ef;text-decoration:none;cursor:pointer}
#zoom .vp{flex:1;overflow:auto;display:flex;padding:0 12px 12px}#zoom img{margin:0 auto auto;max-width:100%;background:#fff;border-radius:4px;cursor:zoom-out}
#zoom.fit img{margin:auto;max-height:100%;cursor:zoom-in}
#msg{position:fixed;bottom:12px;right:12px;background:var(--ink);color:var(--bg);padding:8px 12px;border-radius:8px;display:none}
</style></head><body>
<header><h1>工場の操業コンソール</h1><button id="bell" aria-expanded="false" aria-controls="inbox" title="受信トレイ: 人が対応するもの"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>受信トレイ <span class="n" id="bell-n">0</span></button><a class="stat" href="/usage" title="トークンの消費と費用">使用量</a><span id="hdr"></span></header>
<section id="inbox" hidden></section>
<section id="stops"></section>
<section id="ship"></section>
<section id="runners"></section>
<section id="choices"></section>
<main id="lanes"></main><div id="msg"></div>
<div id="zoom" hidden role="dialog" aria-modal="true" aria-label="画面の拡大"><div class="bar"><span class="t" id="zoom-t"></span><button data-zm="-1" title="前の画面（←）">←</button><button data-zm="1" title="次の画面（→）">→</button><button data-zm="fit" title="幅に合わせる・全体を見る（画像のクリックでも切り替え）" id="zoom-fit">全体を見る</button><a id="zoom-tab" href="#" target="_blank" rel="noopener">別タブで開く</a><button data-zm="close" title="閉じる（Esc）">閉じる</button></div><div class="vp"><img id="zoom-img" alt=""></div></div>
<script>
const TOKEN="__TOKEN__";
const LANES=[["human","人の出番"],["running","作業中"],["queued","順番待ち"],["ready","投入待ち（上限）"],["waiting","依存待ち"],["review","検査・判定・マージ待ち"],["done","今日の済み"]];
let levels={};
const CAT={env:"実行環境",perm:"権限",human:"人の判断待ち","factory-bug":"工場の不具合",ticket:"チケット",external:"外部",budget:"予算・上限",idle:"ラインが空"};
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const dur=s=>s==null?"":s<60?s+"秒":s<3600?Math.floor(s/60)+"分":Math.floor(s/3600)+"時間"+Math.floor(s%3600/60)+"分";
const link=(u,t)=>u&&/^https:\/\//.test(u)?`<a href="${esc(u)}" target="_blank" rel="noopener">${esc(t)}</a>`:esc(t);
function toast(t){const m=document.getElementById("msg");m.textContent=t;m.style.display="block";setTimeout(()=>m.style.display="none",3000)}
async function post(path,body){const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Console-Token":TOKEN},body:JSON.stringify(body||{})});
 const j=await r.json().catch(()=>({}));toast(r.ok?"受け付けました":"失敗: "+(j.error||r.status));if(r.ok&&(path.startsWith("/api/choices/")||path==="/api/inbox/answer"))force=true;load();return r.ok}
function acts(i){let h="";
 if(i.ticket_id&&["waiting","ready","queued","running"].includes(i.stage)){
  h+=`<select data-t="${i.ticket_id}" aria-label="優先度">`+Object.entries(levels).map(([k,v])=>`<option value="${k}" ${k===i.level?"selected":""}>${k} ${esc(v.name)}</option>`).join("")+`</select>`;
  if(i.stage!=="running")h+=i.held?`<button data-a="resume" data-t="${i.ticket_id}">再開</button>`:`<button data-a="hold" data-t="${i.ticket_id}">保留</button>`}
 if(i.job_id&&["queued","running"].includes(i.stage)&&!i.stopping)h+=`<button class="danger" data-a="stop" data-j="${i.job_id}">止める</button>`;
 return h?`<div class="acts">${h}</div>`:""}
const VER=r=>r?esc(r.split("@").pop()):"なし";
function shipView(rs){return rs.map(r=>{const st=r.environments.staging||{},pr=r.environments.prod||{};
 const busy=r.pending.length?`<span class="m">処理中: ${r.pending.map(p=>({ship_requested:"staging へ出荷",approved:"本番へ出荷",rollback_requested:"前の版へ戻す",reseed_requested:"シードから入れ直す"}[p.request]||p.request)).join("・")}</span>`:"";
 let b="";if(!r.pending.length){
  if(r.prod_held)b+=`<span class="m">本番には出さない（段階: ${esc(r.stage.stage)}＝${esc(r.stage.ja)}。staging だけ動かす）</span>`;
  else if(st.release_id&&st.release_id!==pr.release_id)b+=`<button class="go" data-ship="approve" data-r="${esc(st.release_id)}">本番に出す（${VER(st.release_id)}）</button>`;
  if(!r.prod_held&&pr.previous&&!pr.rollback_blocked)b+=`<button class="danger" data-ship="rollback" data-repo="${esc(r.repo)}">本番を前の版に戻す（${VER(pr.previous)} へ）</button>`;
  if(pr.rollback_blocked)b+=`<span class="m">戻せない: ${esc(pr.rollback_blocked)}</span>`;
  if(r.touch)b+=`<button class="danger" data-ship="reseed" data-repo="${esc(r.repo)}">検証の DB をシードから入れ直す</button>`}
 const t=r.touch,tv=t?`<div class="touch"><span class="m">触れる環境 ${VER(t.release_id)}${t.reachable_from_phone?"":"（この機械からだけ開ける。SHIP_TOUCH_HOST・SHIP_TOUCH_BIND が未設定）"}</span>${(t.entries||[]).map(e=>`<div class="e"><div>${esc(e.label)}</div><a href="${/^https?:\/\//.test(e.url)?esc(e.url):"#"}" target="_blank" rel="noopener">${esc(e.url)}</a>${e.qr_svg||""}</div>`).join("")}${(t.accounts||[]).length?`<div class="e"><div>ログインの手がかり</div>${t.accounts.map(a=>`<div class="m">${esc(a.label)}</div><code>${esc(a.value)}</code>`).join("")}</div>`:""}${t.note?`<span class="m">${esc(t.note)}</span>`:""}</div>`:"";
 const l=r.last;const last=l?`<span class="m">直近: ${esc({"release.deployed":"出荷","release.rolled_back":"戻した","release.built":"イメージを作った","release.reseeded":"シードから入れ直した","release.ship_failed":"失敗"}[l.name]||l.name)} ${VER(l.release_id)}${l.env?" "+esc({staging:"検証",prod:"本番"}[l.env]||l.env):""}${l.seconds!=null?" "+l.seconds+" 秒":""}${l.error?" — "+esc(l.error).slice(0,120):""}</span>`:"";
 return `<div class="rel"><span class="t">${esc(r.repo)}</span><span class="env">検証 <b>${VER(st.release_id)}</b></span><span class="env">本番 <b>${VER(pr.release_id)}</b></span>${busy}${b}${last}${tv}</div>`}).join("")}
function pairsView(ps){return ps.map(p=>`<div class="rn" style="border-color:var(--accent)"><h2>新しいランナーの接続の依頼</h2><div><b>${esc(p.name||"?")}</b> <span class="m">${esc(p.platform||"")} · ${esc(p.version||"")}</span></div>
 <div style="font-size:28px;font-weight:700;letter-spacing:.08em;margin:6px 0">${esc(p.code)}</div><div class="m">Mac のターミナルに同じ番号が出ていれば「許可」を押し、Mac 側でも y を押す。違えば「拒否」。10 分で切れる</div>
 <div class="acts"><button class="go" data-pair="approve" data-p="${esc(p.pair_id)}">許可</button><button class="danger" data-pair="reject" data-p="${esc(p.pair_id)}">拒否</button></div></div>`).join("")}
function runnersView(rs){if(!rs.length)return"";
 return `<div class="rn"><h2>登録済みのランナー（工場の外の機械に入れた常駐プロセス）</h2><table><tr><th>機械</th><th>状態</th><th>版・道具</th><th>置き場</th><th></th></tr>${rs.map(r=>`<tr>
  <td><b>${esc(r.name||"?")}</b><div class="m">${esc(r.kind||"")} ${esc(r.platform||"")}</div></td>
  <td><span class="dot ${r.online?"on":""}"></span>${r.uninstall_pending?"外す依頼の処理待ち":r.online?"動いている":r.last_seen_age_sec==null?"連絡待ち":"連絡なし（"+dur(r.last_seen_age_sec)+"前）"}<div class="m">登録 ${esc((r.registered_at||"").slice(0,16).replace("T"," "))}</div></td>
  <td>${esc(r.version||"")}<div class="m">${esc(r.tools||"")}${r.devices?" · 端末 "+esc(r.devices)+" 台":""}</div></td>
  <td><code>${esc(r.path||"")}</code><div class="m">常駐 ${esc(r.label||"")}${r.xcode?" · "+esc(r.xcode):""}</div><div class="m">できること: ${esc(r.scope||"")}</div><div class="m">外しても残るもの: <code>${esc(r.home||"")}</code>・<code>${esc(r.logs||"")}</code>・トークン</div></td>
  <td>${r.uninstall_pending?"":`<button class="danger" data-runner="${esc(r.id)}">外す</button>`}</td></tr>`).join("")}</table>
  <div class="m">外すと工場とランナーの鍵を捨て、ランナーが自分の常駐（LaunchAgent）を外して止まる。手で外すときはその機械で <code>scripts/macrunner.sh uninstall</code></div></div>`}
const WHO_ORDER=["owner","po","design_chief","builder","requester"];
// 受信トレイの 1 件の答え方（ADR 0034 の決定 2）。押したら何が起きるか（effect）を必ず出す。取り返しのつかない作用は 2 度押し
function answerView(i,n){const a=i.answer;
 if(i.answered){const w=i.answered;return `<div class="ans done">答え済み: ${esc(w.by||"?")}（${esc({console:"操業コンソール",review_page:"レビューページ",factoryctl:"factoryctl",mcp:"Claude",github:"GitHub",slack:"Slack"}[w.via]||w.via||"")}）が『${esc(w.label||w.option||"")}』と回答。反映を待っている</div>`}
 if(!a)return "";
 const agg=i.key==="choice:exit_capture";   // まとめた実装後の画面: 印は画面ごとに下の欄で付ける
 const k=esc(i.key),note=!agg&&(a.extra.length||a.type==="ack")?`<input data-note="${n}" placeholder="${a.note_hint?esc(a.note_hint):a.extra.some(e=>e.id==="return")?"一言（理由・違和感。戻す・違和感では必須）":"一言（任意。違和感では必須）"}" maxlength="300">`:"";
 const opts=a.options.map(o=>`<button class="${o.id===a.recommended||a.type!=="choose"?"go":""}" data-ib="${k}" data-o="${esc(o.id)}" data-n="${n}"${a.irreversible?' data-irrev="1"':""}>${a.type==="choose"&&o.id.length<=2?esc(o.id)+". ":""}${esc(o.label)}${o.id===a.recommended?"（推奨）":""}</button>`).join("");
 const extra=a.extra.map(e=>`<button class="${e.id==="return"?"danger":""}" data-ib="${k}" data-o="${esc(e.id)}" data-n="${n}" data-need="${e.note_required?1:""}">${esc(e.label)}</button>`).join("");
 return `<div class="ans"><div class="m">答えると: ${esc(a.effect)}${a.irreversible?"（取り返しがつかない。2 度押しで確定）":""}</div><div class="acts">${agg?`<a href="#choices" style="align-self:center">画面ごとに違和感を書く（下の欄へ）</a>`:""}${opts}${extra}${note}</div></div>`}
// 問いの中身（judge が人に回した判断の今の案・他の案・見立てなど）。答える前に読めるように件の下に出す
function detailView(i){return i.detail&&i.detail.length?`<dl class="dt">${i.detail.map(d=>`<dt>${esc(d.label)}</dt><dd>${esc(d.text)}</dd>`).join("")}</dl>`:""}
function inboxView(ib){if(!ib.items.length)return `<div class="ib"><h2>受信トレイ</h2><div class="m">人が対応するものはありません</div></div>`;
 return `<div class="ib"><h2>受信トレイ（${ib.count} 件、待ちの長い順）</h2><ul>${ib.items.map((i,n)=>{const t=i.url&&/^(https:\/\/|\/#|\/review\/)/.test(i.url)?(i.url.startsWith("/")?`<a href="${esc(i.url)}">${esc(i.title)}</a>`:link(i.url,i.title)):esc(i.title);
  return `<li${i.answered?' class="answered"':""}><div class="t"><span class="who">${esc(ib.who[i.who]||i.who)}</span>${t}</div><div class="m">${[esc(ib.kinds[i.kind]||i.kind),i.age_sec!=null?dur(i.age_sec)+"待ち":"",esc(i.ref),esc(i.action)].filter(Boolean).join(" · ")}</div>${detailView(i)}${answerView(i,n)}</li>`}).join("")}</ul></div>`}
const KIND={design_variants:"画面の体験の案の選択",design_look:"画面の見た目の案の選択",concept_tone:"コンセプトとトーン＆マナーの選択",taste_sample:"抜き取り",exit_capture:"実装後の画面"};
function choicesView(cs){return cs.map(c=>{const sub=c.subject||{};
 if((c.kind==="design_variants"||c.kind==="design_look")&&/^\d+$/.test(sub.uxspec||""))return `<div class="ch"><div class="t">${esc(KIND[c.kind]||c.kind)}: ${esc(c.question)}（${c.options.length} 案）</div><div class="m">${esc(c.context)}</div><div class="acts"><a class="btn" href="/review/${esc(sub.uxspec)}" target="_blank" rel="noopener">レビューページで判断する</a></div></div>`;
 const exit=c.kind==="exit_capture";
 const opts=c.options.map(o=>`<div class="opt ${o.id===c.recommended?"rec":""}">${o.image_key?`<a class="zm" href="/api/choices/${c.id}/image/${esc(o.id)}" target="_blank" rel="noopener" title="押すと拡大（別タブは ⌘/Ctrl＋クリック）"><img loading="lazy" alt="${esc(o.id)}. ${esc(o.label)}" src="/api/choices/${c.id}/image/${esc(o.id)}"></a>`:""}
  <div class="t">${esc(o.id)}. ${esc(o.label)}${o.id===c.recommended?' <span class="m">（推奨）</span>':""}</div><div class="m">${esc(o.note)}</div>
  <div class="acts">${exit?`<input data-fnote="${c.id}" data-o="${esc(o.id)}" placeholder="この画面の違和感を一言（Enter で送る）" maxlength="300">`:`<button class="go" data-ch="choose" data-c="${c.id}" data-o="${esc(o.id)}">この案にする</button>`}<button data-ch="flag" data-c="${c.id}" data-o="${esc(o.id)}">違和感を送る</button></div></div>`).join("");
 const tail=exit?`<span class="m">違和感は画面ごとの欄に一言書いて「違和感を送る」（例: 余白が詰まって見える）。全部見終えたら</span><button class="go" data-ch="seen" data-c="${c.id}">見た（ほかに違和感なし）</button>`
  :`<input id="note-${c.id}" placeholder="一言（理由・違和感。任意、戻すときは必須）" maxlength="300"><button data-ch="return" data-c="${c.id}">どれも違う（戻す）</button>`;
 return `<div class="ch" id="choice-${c.id}"><div class="t">${esc(KIND[c.kind]||c.kind)}: ${esc(c.question)}</div><div class="m">${[sub.pr_url?link(sub.pr_url,"PR"):"",sub.artifact_url?link(sub.artifact_url,"CI の成果物"):"",esc(sub.ticket||""),esc(c.context)].filter(Boolean).join(" · ")}</div>
  <div class="opts">${opts}</div><div class="acts">${tail}</div></div>`}).join("")}
function card(i){const lv=i.level||"p2";let meta=[link(i.pr_url||i.url,i.ref||"")];
 if(i.job_id)meta.push(`ジョブ ${i.job_id}${i.kind&&i.kind!=="implement"?"（"+esc(i.kind)+"）":""}${i.attempt>1?" 試行"+i.attempt:""}`);
 if(i.rank)meta.push(`${i.rank} 番目`);if(i.held)meta.push("保留中");
 if(i.stage==="running")meta.push(`経過 ${dur(i.elapsed_sec)} / 打ち切り ${dur(i.timeout_sec)}`+(i.stopping?(i.preempted_for?" 割り込みで停止中":" 停止中"):""));
 if(i.waiting_on&&i.waiting_on.length)meta.push("待っている: "+i.waiting_on.map(esc).join(" "));
 if(i.note)meta.push(esc(i.note));
 return `<div class="it ${lv}${i.held?" held":""}"><div class="t">${i.priority!=null?`<span class="badge ${lv}">${esc(lv)}</span>`:""}${esc(i.title||"（題名なし）")}</div><div class="m">${meta.filter(Boolean).join(" · ")}</div>${acts(i)}</div>`}
function scaleView(sc,l){if(!sc)return"";const d=sc.desired,r=sc.running,n=(v)=>v==null?"?":v,paused=l&&l.draining;
 const who=sc.at?`${esc(sc.by||"")} が ${esc(String(sc.at).slice(0,16).replace("T"," "))} に宣言${sc.reason?"（"+esc(sc.reason)+"）":""}`:"環境変数の値（宣言なし）";
 return `<span class="stat ${paused?"bad":""}" title="流量（いま / 宣言）。${who}。変えるのは factoryctl line scale か POST /api/line/scale${paused?"。ラインは一時停止中なので、ワーカーの枠は取り出さない":""}">流量 ワーカー <b>${n(r.workers)} / ${d.workers}</b> · チケット <b>${n(r.tickets)} / ${d.tickets}</b> · 委員会 <b>${n(r.governance)} / ${d.governance}</b> · 窓口 <b>${n(r.desk)} / ${d.desk}</b></span>`}
function lineView(l){if(!l)return"";const w=[l.running.length?`実行中 ${l.running.length} 件の終わり待ち`:"",l.shipping.length?`出荷 ${l.shipping.length} 件の処理待ち`:""].filter(Boolean).join("・");
 return `<label class="stat sw ${l.draining&&!l.ready?"bad":""}" title="オフにすると順番待ちを取り出さない（実行中のジョブは最後まで流す）。工場の更新のとき">ライン <input type="checkbox" role="switch" id="line" ${l.draining?"":"checked"}><b class="${l.draining?"":"on"}">${l.draining?(l.ready?"一時停止中（更新してよい）":"一時停止中（"+w+"）"):"流している"}</b></label>`}
// 周回の描き直しは中身が変わった区画だけ（同じ HTML を入れ直すと画像が読み直され、高さが変わってスクロールが飛ぶ）
const put=(id,h)=>{const e=document.getElementById(id);if(e&&e._h!==h){e._h=h;e.innerHTML=h}};
let force=false;
async function load(){if(document.hidden&&!force)return;let s;try{s=await (await fetch("/api/state")).json()}catch(e){toast("読み込めませんでした");return}
 levels=s.levels;const f=s.factory;const hb=f.heartbeat_age_sec;
 put("hdr",[
  `<span class="stat ${hb==null||hb>180?"bad":""}">工場長 <b>${hb==null?"未起動":hb>180?dur(hb)+"前に停止":"動いている"}</b></span>`,
  `<span class="stat">モード <b>${f.mode==="unattended"?"全面委任":"人が並走"}</b></span>`,
  scaleView(f.scale,f.line),
  `<span class="stat">今日の費用 <b>$${f.cost_today_usd.toFixed(2)}</b></span>`,
  `<span class="stat ${f.line_low_below>0&&f.planned<f.line_low_below?"bad":""}" title="投入待ちの段（保留を除く）。${f.line_low_below} 件を切ると番人が知らせる">段の残り <b>${f.planned}</b></span>`,
  lineView(f.line),
  f.host?`<label class="stat sw" title="Mac 上の見張りが ${dur(f.host.agent_age_sec)}前に連絡。オンの間は工場の稼働中に Mac を眠らせない">Mac <input type="checkbox" role="switch" id="keepawake" ${f.host.prevent_sleep?"checked":""}><b class="${f.host.sleep_prevented?"on":""}">${f.host.sleep_prevented?"起こしたまま":"スリープできる"}</b></label>`:""].join(" "));
 put("ship",shipView(s.releases||[]));
 put("runners",pairsView(s.runner_pairs||[])+runnersView(s.runners||[]));
 const ib=s.inbox||{count:0,items:[]},bell=document.getElementById("bell");document.getElementById("bell-n").textContent=ib.count;bell.classList.toggle("has",ib.count>0);
 bell.setAttribute("aria-label",`受信トレイ ${ib.count} 件`);document.title=(ib.count?`(${ib.count}) `:"")+"工場の操業コンソール";
 const ie=document.getElementById("inbox"),ibusy=ie.contains(document.activeElement)||[...ie.querySelectorAll("input")].some(i=>i.value.trim());
 if(!ibusy||force)put("inbox",inboxView(ib));
 // 一言を書きかけ（どこかの欄に文字がある・欄を触っている）の間は人の選択を描き直さない。送った後（force）だけ描き直す
 const ce=document.getElementById("choices"),busy=ce.contains(document.activeElement)||[...ce.querySelectorAll("input")].some(i=>i.value.trim());
 if(!busy||force)put("choices",choicesView(s.choices||[]));force=false;
 put("stops",f.open_stoppages.map(x=>`<div>止まっている: ${esc(x.what)}（${esc(CAT[x.category]||x.category)}）${x.waiting_for?" — 待っているもの: "+esc(x.waiting_for):""}</div>`).join(""));
 put("lanes",LANES.map(([k,n])=>{const xs=s.items.filter(i=>i.stage===k);
  return `<section class="lane"><h2><span>${n}</span><span>${xs.length}</span></h2>${xs.map(card).join("")||'<div class="m">なし</div>'}</section>`}).join(""))}
document.addEventListener("change",e=>{const t=e.target;if(t.id==="keepawake")return post("/api/host/keep-awake",{prevent_sleep:t.checked});if(t.id==="line")return post(t.checked?"/api/factory/resume":"/api/factory/drain",{reason:"操業コンソール"});if(t.matches("select[data-t]"))post(`/api/tickets/${t.dataset.t}/priority`,{level:t.value})});
document.addEventListener("click",e=>{const s=e.target.closest("button[data-ship]");if(!s)return;
 if(!s.classList.contains("armed")){s.dataset.label=s.textContent;s.classList.add("armed");s.textContent="もう一度押すと実行";setTimeout(()=>{s.classList.remove("armed");s.textContent=s.dataset.label},4000);return}
 if(s.dataset.ship==="approve")post("/api/releases/approve",{release_id:s.dataset.r});else if(s.dataset.ship==="reseed")post("/api/releases/reseed",{repo:s.dataset.repo});else post("/api/releases/rollback",{repo:s.dataset.repo,env:"prod"})});
document.addEventListener("click",e=>{const b=e.target.closest("button[data-ib]");if(!b)return;
 const note=((document.querySelector(`#inbox input[data-note="${b.dataset.n}"]`)||{}).value||"").trim();
 if(b.dataset.need&&!note)return toast("一言を入れてください");
 if(b.dataset.irrev&&!b.classList.contains("armed")){b.dataset.label=b.textContent;b.classList.add("armed");b.textContent="もう一度押すと確定";setTimeout(()=>{b.classList.remove("armed");b.textContent=b.dataset.label},4000);return}
 post("/api/inbox/answer",{key:b.dataset.ib,option:b.dataset.o,note})});
document.addEventListener("click",e=>{const b=e.target.closest("button[data-pair]");if(!b)return;post(`/api/runners/pairs/${b.dataset.p}/${b.dataset.pair}`,{reason:"操業コンソール"})});
document.addEventListener("click",e=>{const b=e.target.closest("button[data-runner]");if(!b)return;
 if(!b.classList.contains("armed")){b.classList.add("armed");b.textContent="もう一度押すと外す";setTimeout(()=>{b.classList.remove("armed");b.textContent="外す"},4000);return}
 post(`/api/runners/${b.dataset.runner}/uninstall`,{reason:"操業コンソール"})});
document.addEventListener("keydown",e=>{const t=e.target;if(e.key!=="Enter"||e.isComposing||e.keyCode===229||!t.matches("input[data-fnote]"))return;   // 229: Safari は変換の確定の Enter で isComposing を立てない（11-06「テーブルの」だけの印が 2 件送られた）e.preventDefault();
 const b=document.querySelector(`button[data-ch=flag][data-c="${t.dataset.fnote}"][data-o="${t.dataset.o}"]`);if(b)b.click()});
document.addEventListener("click",e=>{const b=e.target.closest("button[data-ch]");if(!b)return;const id=b.dataset.c;
 const fin=b.dataset.o?document.querySelector(`input[data-fnote="${id}"][data-o="${b.dataset.o}"]`):null;   // 実装後の画面は画面ごとの欄
 const note=((fin||document.getElementById("note-"+id)||{}).value)||"";
 if(b.dataset.ch==="seen")return post(`/api/choices/${id}/seen`,{reason:note});
 if(b.dataset.ch==="choose")return post(`/api/choices/${id}/choose`,{option:b.dataset.o,reason:note});
 if(b.dataset.ch==="flag"){if(!note.trim()){if(fin)fin.focus();return toast("違和感の一言を入れてください")}return post(`/api/choices/${id}/flag`,{option:b.dataset.o,note}).then(ok=>{if(ok&&fin){fin.value="";fin.placeholder="送りました。ほかにもあれば一言（Enter で送る）"}})}
 if(!note.trim())return toast("戻す理由を一言入れてください");post(`/api/choices/${id}/return`,{reason:note})});
document.addEventListener("click",e=>{const b=e.target.closest("button[data-a]");if(!b)return;
 if(b.dataset.a==="stop"){if(!b.classList.contains("armed")){b.classList.add("armed");b.textContent="もう一度押すと止める";setTimeout(()=>{b.classList.remove("armed");b.textContent="止める"},4000);return}
  return post(`/api/jobs/${b.dataset.j}/stop`,{})}
 post(`/api/tickets/${b.dataset.t}/${b.dataset.a}`,{})});
document.getElementById("bell").addEventListener("click",()=>{const p=document.getElementById("inbox"),b=document.getElementById("bell");p.hidden=!p.hidden;b.setAttribute("aria-expanded",String(!p.hidden));
 try{localStorage.setItem("inbox-open",p.hidden?"0":"1")}catch(e){}});
try{if(localStorage.getItem("inbox-open")==="1"){document.getElementById("inbox").hidden=false;document.getElementById("bell").setAttribute("aria-expanded","true")}}catch(e){}
// 画面の拡大（11-07）。案の画像を押すとその場で幅いっぱいに出す（縦長はスクロール）。押すと全体、← → で同じ問いの他の案へ、Esc で閉じる。⌘/Ctrl＋クリックは別タブ
let zl=[],zi=0;const Z=document.getElementById("zoom");
function zshow(){const a=zl[zi];if(!a)return;const g=a.querySelector("img");document.getElementById("zoom-img").src=a.href;document.getElementById("zoom-img").alt=g.alt;
 document.getElementById("zoom-t").textContent=`${g.alt}（${zi+1} / ${zl.length}）`;document.getElementById("zoom-tab").href=a.href}
const zfit=on=>{Z.classList.toggle("fit",on);document.getElementById("zoom-fit").textContent=on?"幅に合わせる":"全体を見る"};
function zclose(){Z.hidden=true;zfit(false);document.getElementById("zoom-img").removeAttribute("src")}
document.addEventListener("click",e=>{const a=e.target.closest("a.zm");if(!a||e.metaKey||e.ctrlKey||e.shiftKey||e.button)return;e.preventDefault();
 zl=[...a.closest(".opts").querySelectorAll("a.zm")];zi=zl.indexOf(a);zfit(false);Z.hidden=false;zshow();Z.querySelector(".vp").scrollTop=0});
Z.addEventListener("click",e=>{const b=e.target.closest("[data-zm]");
 if(e.target.id==="zoom-img"||(b&&b.dataset.zm==="fit"))return zfit(!Z.classList.contains("fit"));
 if(b&&b.dataset.zm==="close"||e.target.classList.contains("vp"))return zclose();
 if(b){zi=(zi+Number(b.dataset.zm)+zl.length)%zl.length;zshow();Z.querySelector(".vp").scrollTop=0}});
document.addEventListener("keydown",e=>{if(Z.hidden)return;if(e.key==="Escape")zclose();else if(e.key==="ArrowRight"||e.key==="ArrowLeft"){zi=(zi+(e.key==="ArrowRight"?1:-1)+zl.length)%zl.length;zshow();Z.querySelector(".vp").scrollTop=0}});
load();setInterval(load,10000);document.addEventListener("visibilitychange",()=>{if(!document.hidden)load()});
</script></body></html>"""


def main():
    if PROTOTYPE_ON:
        prototype.serve(db, read_design, ALLOWED_HOSTS)
        print(f"prototype up: port={prototype.PORT}", flush=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"console up: port={PORT} allowed_hosts={sorted(ALLOWED_HOSTS)}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
