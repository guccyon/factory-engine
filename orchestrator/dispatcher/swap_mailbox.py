"""工場の更新の依頼の郵便受け（ADR 0031 の決定 2・順番 3）。クラウドの建設者は工場のマシンの中の窓口に届かないので、
factory-engine の Issue に `factory:swap` を付けたものを依頼として拾い、提案・進み具合・結果を同じ Issue のコメントに返す。

dispatcher の更新の周回（swap_loop）から SWAP_MAILBOX_INTERVAL_SEC ごとに呼ぶ。

  1. 拾う: 開いている Issue で `factory:swap` が付いたもの。ラベルを付けたのが SWAP_REQUESTERS（既定は郵便受けのリポジトリの持ち主）の人のものだけ。
     リポジトリは公開なので、ほかの人が付けたラベルは何もせずに飛ばす（コメントも返さない）
  2. 読む: 本文の決まった形の行（`target: main` など。下の FIELDS）だけ。値は決まった形に当たるものだけ受ける。
     ほかの文はデータとして依頼の note に残すだけで、シェルにもプロンプトにも渡さない
  3. 受ける: swap.request で依頼にし、その場で「いま」で受ける（時刻の提案は順番 4 のプランナー）。
     ただし申告が一時停止を伴う種類（control_plane・db）で、いまが全面委任（unattended）の時間帯なら、人が並走する時間帯まで待つ（決めどころ S4）。
     差分の判定が重かったときは更新役が同じ理由で止め（deferred）、ここが時間帯に入ってから依頼し直す
     ほかの更新が終わっていなければ待つ（同時に 1 件）
  4. 返す: 段（起動・一時停止・結果）ごとに 1 回ずつコメントする。更新したら Issue を閉じる。止まったら `factory:swap-failed` を付けて開いたまま
  5. 取り消す: 更新役を起動する前に Issue を閉じたら取り消す

コメントには工場のマシンのパスやホスト名を出さない（リポジトリは公開）。
"""
from __future__ import annotations

import os
import re
from psycopg.types.json import Jsonb

import swap

ENV = os.environ
LABEL = "factory:swap"
RUNNING_LABEL = "factory:swapping"
DONE_LABEL = "factory:swapped"
FAILED_LABEL = "factory:swap-failed"
NOTED = "swap_mailbox.noted"        # 待ちや読めなかったことを 1 回だけ返した印（payload の issue・key）
REPORTED = "swap_mailbox.reported"  # 段を Issue に返した印（payload の event_id）。swap.* ではないので更新の状態には数えない
FIELDS = {
    "target": re.compile(r"^(main|[0-9a-fA-F]{7,40})$"),
    "kind": re.compile(r"^(code|control_plane|db)$"),
    "urgency": re.compile(r"^(normal|urgent)$"),
    "deadline": re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?([+-]\d{2}:?\d{2}|Z)?$"),
}
LINE = re.compile(r"^[ \t]*[-*]?[ \t]*`?(target|kind|urgency|deadline)`?[ \t]*[:：][ \t]*(.*)$", re.I | re.M)
REPORT = ("swap.launched", "swap.drained", "swap.completed", "swap.rolled_back", "swap.escalated", "swap.failed", "swap.preflight_failed",
          "swap.cancelled")
TERMINAL_STATUS = {swap.STATUS[t] for t in swap.TERMINAL}


def repo() -> str:
    return ENV.get("SWAP_MAILBOX_REPO", "guccyon/factory-engine")


def requesters() -> set[str]:
    """ラベルを付けてよい人（GitHub のログイン）。既定は郵便受けのリポジトリの持ち主"""
    names = [n.strip().lower() for n in ENV.get("SWAP_REQUESTERS", "").split(",") if n.strip()]
    return set(names) or {repo().split("/")[0].lower()}


def enabled() -> bool:
    return ENV.get("SWAP_MAILBOX", "1") == "1" and not ENV.get("WORKER_DRY_RUN")


# ------------------------------------------------------------------ 読む
class Unreadable(Exception):
    pass


def parse(body: str | None) -> dict:
    """本文の決まった形の行だけ読む。同じ項目が 2 回あれば後の方。target が無ければ main。値が形に合わなければ Unreadable"""
    out = {"target": "main", "kind": "code", "urgency": "normal", "deadline": None}
    for m in LINE.finditer(body or ""):
        key, val = m.group(1).lower(), re.sub(r"\s+#.*$", "", m.group(2)).strip().strip("`")
        if not FIELDS[key].match(val):
            raise Unreadable(f"`{key}` の値 `{val[:40]}` が読めない")
        out[key] = val.lower() if key != "deadline" else val
    return out


def redact(text: str) -> str:
    """公開の Issue に返す文から、工場のマシンのパス・ホスト名を伏せる"""
    text = re.sub(r"/(?:Users|home)/[^/\s'\"]+", "~", text or "")
    return re.sub(r"\b[\w-]+\.local\b", "<host>.local", text)


# ------------------------------------------------------------------ 記録
def _noted(cur, ref: str, key: str) -> bool:
    cur.execute("select 1 from events where name=%s and payload->>'issue'=%s and payload->>'key'=%s limit 1", (NOTED, ref, key))
    return cur.fetchone() is not None


def _note(cur, ref: str, key: str) -> None:
    cur.execute("insert into events(name, payload) values (%s, %s)", (NOTED, Jsonb({"issue": ref, "key": key})))


def latest_for(cur, ref: str) -> dict | None:
    cur.execute("select id from events where name='swap.requested' and payload->>'source'=%s order by id desc limit 1", (ref,))
    r = cur.fetchone()
    return swap.get(cur, r["id"]) if r else None


def _deferred(s: dict) -> bool:
    last = s["events"][-1]["payload"] if s["events"] else {}
    return s["status"] == "preflight_failed" and last.get("deferred") == "unattended"


def labeler(gh, rp: str, n: int) -> str | None:
    """いま付いている factory:swap を最後に付けた人"""
    who = None
    for e in gh("GET", f"repos/{rp}/issues/{n}/events?per_page=100") or []:
        if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == LABEL:
            who = ((e.get("actor") or {}).get("login") or "").lower()
    return who


# ------------------------------------------------------------------ 周回
def poll(cur, gh, comment, set_labels, log=print, *, unattended=None) -> None:
    """依頼を拾い、進み具合を返す。gh(method, path, body?)・comment(repo, n, text)・set_labels(repo, n, remove, add) は dispatcher のもの"""
    if unattended is None:
        from swapper import unattended as _u
        unattended = lambda: _u(cur)  # noqa: E731
    rp = repo()
    report(cur, gh, comment, set_labels, log)
    for issue in gh("GET", f"repos/{rp}/issues?labels={LABEL}&state=open&per_page=20") or []:
        if issue.get("pull_request"):
            continue
        try:
            pick(cur, gh, comment, set_labels, rp, issue, unattended, log)
        except Exception as e:  # noqa: BLE001  1 件の失敗でほかの依頼を止めない
            log("swap mailbox error", issue.get("number"), repr(e))
    retry_deferred(cur, gh, comment, set_labels, rp, unattended, log)


def pick(cur, gh, comment, set_labels, rp: str, issue: dict, unattended, log) -> int | None:
    n = issue["number"]
    ref = f"{rp}#{n}"
    prev = latest_for(cur, ref)
    if prev and (prev["status"] not in TERMINAL_STATUS or _deferred(prev)):
        return None   # 動いている（か、時間帯を待って retry_deferred が依頼し直す）。終わっていれば、付け直したラベルは新しい依頼として読む
    who = labeler(gh, rp, n)
    if who not in requesters():
        log("swap mailbox: ignored (labeler not allowed)", ref)
        return None
    try:
        f = parse(issue.get("body"))
    except Unreadable as e:
        comment(rp, n, f"工場の更新の依頼として読めませんでした: {e}。\n\n{FORMAT_HELP}")
        set_labels(rp, n, [LABEL], [FAILED_LABEL])
        return None
    return submit(cur, comment, set_labels, rp, n, f, who, (issue.get("title") or "")[:200], unattended, log)


def submit(cur, comment, set_labels, rp, n, f, who, note, unattended, log) -> int | None:
    ref = f"{rp}#{n}"
    if f["kind"] != "code" and unattended():
        if not _noted(cur, ref, "unattended"):
            _note(cur, ref, "unattended")
            comment(rp, n, f"一時停止を伴う更新（`{f['kind']}`）なので、人が並走する時間帯（attended）に入ってから始めます。")
        return None
    act = swap.active(cur)
    if act:
        key = f"busy:{act['id']}"
        if not _noted(cur, ref, key):
            _note(cur, ref, key)
            comment(rp, n, f"更新 {act['id']}（{act['target']}、{swap.STATUS_JA[act['status']]}）が終わってから受けます。")
        return None
    try:
        sid = swap.request(cur, f["target"], who, kind=f["kind"], urgency=f["urgency"], note=note, source=ref, basis="mailbox")
    except swap.SwapError as e:
        comment(rp, n, f"工場の更新の依頼を受けられませんでした: {e}")
        set_labels(rp, n, [LABEL], [FAILED_LABEL])
        return None
    set_labels(rp, n, [LABEL, FAILED_LABEL], [RUNNING_LABEL])
    comment(rp, n, f"工場の更新 {sid} として受けました（`{f['target']}`、申告 `{f['kind']}`）。"
                   f"{swap.ENV.get('SWAP_INTERVAL_SEC', '15')} 秒ほどで更新役が始めます。種類は差分から判定し直し、重い方を採ります。"
                   + (f"\n\n期限 `{f['deadline']}` は記録だけです（時刻の提案は ADR 0031 の順番 4）。" if f["deadline"] else ""))
    log("swap mailbox: requested", ref, sid)
    return sid


def retry_deferred(cur, gh, comment, set_labels, rp, unattended, log) -> None:
    """時間帯を待って止めた依頼（更新役が判定し直して重かったもの）を、attended に入ったら同じ Issue で依頼し直す"""
    if unattended() or swap.active(cur):
        return
    cur.execute("select distinct on (payload->>'source') id, payload from events where name='swap.requested' and payload->>'source' like %s "
                "order by payload->>'source', id desc", (rp + "#%",))
    for r in cur.fetchall():
        s = swap.get(cur, r["id"])
        if not _deferred(s):
            continue
        n = int(s["source"].rsplit("#", 1)[1])
        if (gh("GET", f"repos/{rp}/issues/{n}") or {}).get("state") != "open":
            continue   # 待つ間に閉じられた（取り消し）
        last = s["events"][-1]["payload"]
        f = {"target": s["target"], "kind": swap.heavier(s["kind"] or "code", last.get("judged") or "code"), "urgency": s["urgency"] or "normal",
             "deadline": None}
        if submit(cur, comment, set_labels, rp, n, f, s["by"], r["payload"].get("note") or "", unattended, log):
            return


def report(cur, gh, comment, set_labels, log=print) -> None:
    """郵便受けから来た更新の段を、まだ返していなければ Issue に返す。起動する前に Issue が閉じられていたら取り消す"""
    rp = repo()
    cur.execute("select id from events where name='swap.requested' and payload->>'source' like %s order by id desc limit 10", (rp + "#%",))
    for r in cur.fetchall():
        s = swap.get(cur, r["id"])
        n = int(s["source"].rsplit("#", 1)[1])
        if s["status"] in ("requested", "confirmed"):
            state = (gh("GET", f"repos/{rp}/issues/{n}") or {}).get("state")
            if state == "closed" and swap.cancel(cur, s["id"], "mailbox", "Issue が閉じられた"):
                s = swap.get(cur, s["id"])
        cur.execute("select payload->>'event_id' as e from events where name=%s and correlation_id=%s", (REPORTED, swap.corr(s["id"])))
        done = {int(x["e"]) for x in cur.fetchall()}
        for e in s["events"]:
            if e["name"] not in REPORT or e["id"] in done:
                continue
            cur.execute("insert into events(name, correlation_id, payload) values (%s, %s, %s)",
                        (REPORTED, swap.corr(s["id"]), Jsonb({"swap_id": s["id"], "event_id": e["id"]})))
            if e["name"] == "swap.preflight_failed" and (e["payload"] or {}).get("deferred") == "unattended":
                comment(rp, n, f"更新 {s['id']}: 差分の判定が一時停止を伴う種類（`{e['payload'].get('kind')}`）でした。"
                               "人が並走する時間帯（attended）に入ったら、この Issue のまま依頼し直します。")
                continue
            comment(rp, n, step_text(s, e))
            if e["name"] == "swap.completed":
                set_labels(rp, n, [RUNNING_LABEL], [DONE_LABEL])
                gh("PATCH", f"repos/{rp}/issues/{n}", {"state": "closed", "state_reason": "completed"})
            elif e["name"] in swap.TERMINAL:
                set_labels(rp, n, [RUNNING_LABEL], [FAILED_LABEL] if e["name"] != "swap.cancelled" else [])
            log("swap mailbox: reported", s["id"], e["name"])


def step_text(s: dict, e: dict) -> str:
    p = e.get("payload") or {}
    head = f"更新 {s['id']}: **{swap.STATUS_JA[swap.STATUS[e['name']]]}**"
    bits = []
    if p.get("prev") and p.get("target_sha"):
        bits.append(f"版 `{p['prev']}` → `{p['target_sha']}`")
    if p.get("kind"):
        bits.append(f"種類 `{p['kind']}`" + (f"（申告 `{p['kind_declared']}`）" if p.get("kind_declared") and p["kind_declared"] != p["kind"] else ""))
    if p.get("stopped_sec") is not None:
        bits.append(f"止まっていた時間 {p['stopped_sec']} 秒")
    why = p.get("error") or p.get("reason")
    text = head + ("。" + "、".join(bits) if bits else "")
    if why:
        text += f"\n\n> {redact(str(why))[:600]}"
    if e["name"] in ("swap.escalated", "swap.failed"):
        text += "\n\n人の確認が要ります（停止の記録か受信トレイに出しています）。直した版で依頼し直すときは、ラベル `factory:swap` を付け直してください。"
    elif e["name"] in ("swap.rolled_back", "swap.preflight_failed"):
        text += "\n\n工場は前の版のまま動いています。直した版で依頼し直すときは、ラベル `factory:swap` を付け直してください。"
    return text


FORMAT_HELP = """本文に次の形の行を書きます（ほかの文は記録だけ）。どれも省けます。

```
target: main            # main かコミット（main に入っているもの）
kind: code              # code / control_plane / db（自己申告。差分から判定し直し、重い方を採る）
urgency: normal         # normal / urgent
deadline: 2026-11-06T18:00+09:00
```"""

