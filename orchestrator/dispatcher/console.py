#!/usr/bin/env python3
"""工場の操業コンソール（ADR 0009）。工場のいまを見て、順番・保留・停止を操作する常駐サービス。

- 操業 API（JSON）が正本で、画面はその利用者の 1 つ。将来の顧客向けアプリも同じ API を使う
  GET  /api/inbox                         受信トレイ: 人が対応するものの一覧（ADR 0026 の決定 7。inbox.py）。/api/state にも inbox として入る
  GET  /api/usage?days=14                 使用量: トークンの消費と費用（日ごと・役割・モデル・契約の週）とアカウントを足すかの判断の材料（usage.py）
  GET  /usage                             同じ中身のページ（画面の中のページ。中身は GET /usage/body の HTML の断片。usage_page.py）
  GET  /api/state                         ライン（依存待ち・順番待ち・作業中・PR の検査と判定・人の出番・今日の済み）と工場の状態
  POST /api/tickets/<id>/priority         {"level": "p0".."p3"} か {"priority": 0..100}。p0 は作業中のものを止めてでも先に流す
  POST /api/tickets/<id>/hold | resume    順番待ちから一時的に外す / 戻す
  POST /api/jobs/<id>/stop                ジョブを止める（作業中ならコンテナは dispatcher が止める）
  POST /api/jobs/<id>/rerun               {"continue"?} 失敗したジョブを積み直す（factoryctl rerun と同じ。rerun.py）
  GET  /api/jobs/<id>/log                 ジョブの作業の記録の最後の部分（オブジェクトストアの jobs/<id>/worker.log。鍵らしい文字列は伏せる）
  POST /api/alerts/dismiss                {"key", "on"?} 停止のアラートを画面から消す（on=false で戻す）。停止そのものは閉じない（alerts.py）
  POST /api/releases/approve              {"release_id"} 承認ボタン。staging で確かめた版を prod に出す（ADR 0012）
  POST /api/releases/rollback             {"repo", "env"} その環境を 1 つ前の版に戻す
  POST /api/releases/reseed               {"repo"} 触れる環境（staging）の DB を空にして合成シードから入れ直す（ADR 0028 の touchable_env）
  POST /api/budget                        {"mode"?: "daily"|"weekday", "daily_usd"?, "weekday_usd"?: {"mon".."sun"}, "reason"?} 使用量の予算の宣言（usage.set_budget）。
                                          渡したものだけ変える。今の決め方は /api/state の factory.budget
  POST /api/factory/drain | resume        {"reason"} ラインの一時停止（順番待ちを取り出さない）と再開。工場の更新のため（ADR 0020）
  POST /api/line/release-hold             {"reason"} QA 役の差し止め（S1 の Incident。ADR 0043 の O5）を人が解除する。理由は必須
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

import alerts
import choice
import drain
import incident
import line
import inbox
import judgment
import ops
import prototype
import rerun
import review_page
import runners
import ship
import touch
import usage
import usage_page
import version
from console_page import PAGE, APP_PATH

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


SECRET_RE = re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-ant-[A-Za-z0-9_-]{10,}|xox[abpr]-[A-Za-z0-9-]{10,}|"
                       r"(?i:(?:token|secret|password|api_key)\s*[=:]\s*)\S{8,})")
LOG_TAIL_LINES = 80


def job_log_tail(job_id: int) -> str | None:
    """ジョブの作業の記録（worker.log）の最後の部分。停止のアラートから原因を見るため（2026-11-08）。鍵らしい文字列は伏せる"""
    if store() is None:
        return None
    try:
        obj = store().get_object(ENV.get("OBJECTSTORE_BUCKET", "factory-artifacts"), f"jobs/{job_id}/worker.log")
    except Exception:  # noqa: BLE001  ない・読めない
        return None
    try:
        data = obj.read()
    finally:
        obj.close()
        obj.release_conn()
    lines = data[-64000:].decode("utf-8", "replace").splitlines()[-LOG_TAIL_LINES:]
    return SECRET_RE.sub("［伏せた］", "\n".join(lines))


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
        items.append(item("waiting" if t["waiting_on"] else "ready", ticket_id=t["id"], ref=t["source_ref"], title=t["title"], url=t["url"], source=t["source"],
                          priority=t["priority"], held=t["held"], waiting_on=t["waiting_on"]))
    # 順番待ち・作業中（ジョブ）
    cur.execute("select j.*, t.title, t.source, t.source_ref, t.url, t.held, extract(epoch from now() - j.started_at)::int as elapsed "
                "from jobs j left join tickets t on t.id=j.ticket_id where j.state in ('queued','running') "
                "order by j.state desc, j.priority desc, j.enqueued_at")
    rank = 0
    for j in cur.fetchall():
        common = dict(ticket_id=j["ticket_id"], job_id=j["id"], kind=j["kind"], attempt=j["attempt"], priority=j["priority"],
                      ref=j["source_ref"] or f"{j['repo']}#{j['issue_number']}", title=j["title"] or "", url=j["url"], held=bool(j["held"]),
                      source=j["source"] or "github")
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
        cur.execute("select id, title, priority, url from tickets where source='github' and source_ref=%s", (ref,))
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
        items.append(item(stage, ticket_id=t.get("id"), ref=ref, title=t.get("title") or "", pr_url=p["pr_url"], note=note, priority=t.get("priority"),
                          url=t.get("url"), source="github"))
    # 今日の済み・失敗
    cur.execute("select e.payload->>'pr_url' as pr_url, e.correlation_id, e.occurred_at, t.title, t.url from events e "
                "left join tickets t on t.source='github' and t.source_ref=split_part(e.correlation_id, ':', 2) "
                "where e.name='pr.merged' and e.occurred_at >= date_trunc('day', now()) order by e.id desc")
    for r in cur.fetchall():
        items.append(item("done", ref=r["correlation_id"].split(":", 1)[1], title=r["title"] or "", pr_url=r["pr_url"], at=iso(r["occurred_at"]), note="マージ",
                          url=r["url"], source="github"))
    # 番号はチケットの source_ref か、ジョブのリポジトリと番号（owner/repo#n）。correlation_id の後ろ半分を使うと、委員会（committee:<PR の URL>）・
    # 事例（case:<key>）・窓口（desk:<id>）のジョブで URL や key がリポジトリとして絞り込みに出ていた（11-08）
    cur.execute("select e.job_id, e.correlation_id, e.occurred_at, e.payload->>'error' as error, t.title, t.source_ref, j.repo, j.issue_number from events e "
                "left join jobs j on j.id=e.job_id left join tickets t on t.id=j.ticket_id "
                "where e.name='job.finished' and e.payload->>'state'='failed' and e.occurred_at >= date_trunc('day', now()) order by e.id desc limit 20")
    for r in cur.fetchall():
        ref = r["source_ref"] or (f"{r['repo']}#{r['issue_number']}" if r["repo"] else (r["correlation_id"] or ":").split(":", 1)[1])
        items.append(item("done", job_id=r["job_id"], ref=ref, title=r["title"] or "",
                          at=iso(r["occurred_at"]), note=f"失敗（{r['error']}）", failed=True))
    running = sum(1 for i in items if i["stage"] == "running")
    cur.execute("select count(*) as n from tickets where state='planned' and not held")
    planned = cur.fetchone()["n"]
    return {
        "releases": releases(cur),
        "runners": runners.view(cur),
        "runner_pairs": runners.pending_pairs(cur),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "factory": {"heartbeat_age_sec": hb["age"], "mode": mode, "mode_basis": basis, "running": running, "max_parallel": line.spec(cur)["workers"], "scale": line.status(cur), "budget": usage.budget_spec(cur),
                    "cost_today_usd": round(cost, 2), "open_stoppages": stoppages, "host": host_view(cur), "line": drain.status(cur),
                    "qa_hold": incident.hold_status(cur),
                    "planned": planned, "line_low_below": LINE_LOW_BELOW},
        "levels": {k: {"priority": v[0], "name": v[1], "meaning": v[2]} for k, v in ops.LEVELS.items()},
        "items": items,
    }


TREND_KIND = "report.factory_health"


def trends(cur) -> dict | None:
    """ホームの指標の推移。毎朝の操業レポート（工場の健康）が組み立てて残した最新の中身（report.built）から、
    レポートが大きく描いている指標（charts）を読む。数をここで数え直さないので、レポートと食い違わない"""
    cur.execute("select payload from events where name='report.built' and payload->>'kind'=%s order by id desc limit 1", (TREND_KIND,))
    row = cur.fetchone()
    rep = (row or {}).get("payload", {}).get("report") if row else None
    if not rep:
        return None
    by = {(sec.get("id"), i.get("label")): i for sec in rep.get("sections") or [] for i in sec.get("items") or [] if i.get("kind") == "metric"}
    out = []
    for c in rep.get("charts") or []:
        i = by.get((c.get("section"), c.get("label")))
        if i:
            out.append({"title": c.get("title") or c.get("label"), "value": i.get("value"), "unit": i.get("unit") or "", "delta": i.get("delta"),
                        "level": i.get("level") or "", "series": i.get("series") or []})
    return {"days": rep.get("days") or [], "end": rep.get("end"), "level": rep.get("level") or "", "summary": rep.get("summary") or [], "items": out}


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
    if path == "/api/budget":   # 使用量の予算の宣言（ADR 0029 の追記）。{mode, daily_usd, weekday_usd} のうち渡したものだけ変える
        wk = body.get("weekday_usd")
        if wk is not None and not isinstance(wk, dict):
            return 400, {"error": "weekday_usd は {mon..sun: ドル}"}
        try:
            spec = usage.set_budget(cur, by, reason, mode=body.get("mode"), daily_usd=body.get("daily_usd"), weekday_usd=wk)
        except (ValueError, TypeError) as e:
            return 400, {"error": str(e)}
        return 200, {"budget": spec}
    if path in ("/api/factory/drain", "/api/factory/resume"):
        drain.set_drain(cur, path.endswith("/drain"), by, reason)
        return 200, {"line": drain.status(cur)}
    if path == "/api/line/release-hold":   # QA 役の差し止めの解除（ADR 0043 の O5）。人だけ（コンソールの操作は人）
        try:
            ok = incident.release(cur, by, reason)
        except ValueError as e:
            return 400, {"error": str(e)}
        return (200, {"released": True}) if ok else (409, {"error": "差し止めていない"})
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
    m = re.fullmatch(r"/api/jobs/(\d+)/rerun", path)
    if m:
        new_id, msg = rerun.rerun(cur, int(m.group(1)), continue_draft=bool(body.get("continue")), requested_by=by)
        return (200, {"job_id": new_id, "message": msg}) if new_id else (409, {"error": msg})
    if path == "/api/alerts/dismiss":
        key = str(body.get("key") or "")
        if not alerts.set_dismissed(cur, key, body.get("on", True) is not False, by):
            return 404, {"error": "no such stoppage"}
        return 200, {"key": key}
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
                return self._json(200, {**snapshot(cur), "choices": choice.open_choices(cur), "inbox": inbox.view(cur), "trends": trends(cur),
                                        "alerts": alerts.view(cur), "engine_version": version.engine_version()})
        if self.path == "/api/inbox":
            with db() as conn, conn.cursor() as cur:
                return self._json(200, inbox.view(cur))
        if urlsplit(self.path).path in ("/api/usage", usage_page.BODY_PATH):
            u = urlsplit(self.path)
            days = parse_qs(u.query).get("days", ["14"])[0]
            with db() as conn, conn.cursor() as cur:
                v = usage.view(cur, days=min(max(int(days) if days.isdigit() else 14, 1), 90))
            if u.path == usage_page.BODY_PATH:   # 使用量のページの中身（画面の中の /usage が読み込む。タブとヘッダはほかのページと同じ）
                return self._send(200, usage_page.body(v).encode(), "text/html; charset=utf-8")
            return self._json(200, v)
        m = re.fullmatch(r"/api/jobs/(\d+)/log", urlsplit(self.path).path)
        if m:
            text = job_log_tail(int(m.group(1)))
            return self._json(200, {"text": text}) if text is not None else self._json(404, {"error": "作業の記録が見つからない"})
        if self.path in ("/", "/index.html") or APP_PATH.fullmatch(urlsplit(self.path).path):   # 画面のページ（中身は画面の中で URL に合わせて描く）
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


def main():
    if PROTOTYPE_ON:
        prototype.serve(db, read_design, ALLOWED_HOSTS)
        print(f"prototype up: port={prototype.PORT}", flush=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"console up: port={PORT} allowed_hosts={sorted(ALLOWED_HOSTS)}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
