"""触れる環境（ADR 0028 の touchable_env）。Release 候補を 1 コマンドで staging に立て、シードを入れ、人が URL と QR で触れるようにする。

Gen1 は staging を QA の環境と兼ねる（使い捨ての環境は P5b の ephemeral_env）。手を動かすのは常駐の shipper（ship.py）で、
ここは依頼を順に書いて結果を待ち、shipper が残した入口（release.touchable）を人に見せる形にするだけ。

  factoryctl env up <owner/repo> [--release <id>] [--fresh] [--no-wait] [--timeout 3600] [--by ..]
  factoryctl env show <owner/repo> [--env staging]

up の版の選び方: --release があればその版。なければ、まだどの Release にも入っていないマージ済みの PR があれば次の版の Release を切り、
なければいま staging に出ている版のまま（出し直さない）。--fresh は出したあとに DB を空にして合成シードから入れ直す。
"""
from __future__ import annotations

import io
import time

import ship

try:   # QR はあれば付ける（dispatcher のイメージを作り直すまでは無い）
    import segno
except ImportError:  # pragma: no cover
    segno = None


def latest(cur, repo: str, env: str = "staging") -> dict | None:
    """その環境のいまの版の、最新の入口。いまの版の入口がなければ None（古い版の URL を見せない）"""
    rid = ship.status(cur, repo)["environments"].get(env, {}).get("release_id")
    if not rid:
        return None
    cur.execute("select payload, occurred_at from events where name='release.touchable' and payload->>'release_id'=%s "
                "and payload->>'env'=%s order by id desc limit 1", (rid, env))
    r = cur.fetchone()
    return {**r["payload"], "at": r["occurred_at"].isoformat()} if r else None


def qr_svg(url: str) -> str | None:
    """操業コンソールに埋める QR（SVG の文字列）"""
    if segno is None:
        return None
    return segno.make(url, error="m").svg_inline(scale=4, border=2, dark="#111", light="#fff")


def qr_text(url: str) -> str | None:
    """端末に出す QR"""
    if segno is None:
        return None
    buf = io.StringIO()
    segno.make(url, error="m").terminal(out=buf, compact=True, border=2)
    return buf.getvalue()


def render(t: dict) -> str:
    """入口を端末向けの文字にする（factoryctl env show / up）"""
    lines = [f"{t['release_id']} の触れる環境（{t['env']}）"]
    if not t.get("reachable_from_phone"):
        lines.append("  ※ この機械の中からしか開けない。スマートフォンから触るには工場の SHIP_TOUCH_HOST と SHIP_TOUCH_BIND を決める"
                     "（orchestrator/README の「触れる環境」）")
    for e in t.get("entries") or []:
        lines.append(f"\n■ {e['label']}\n  {e['url']}")
        if e.get("qr"):
            q = qr_text(e["url"])
            lines.append(q if q else "  （QR は segno が入っていないので出せない。dispatcher のイメージを作り直すと出る）")
    if t.get("accounts"):
        lines.append("\nログインの手がかり:")
        lines += [f"  {a['label']}: {a['value']}" for a in t["accounts"]]
    if t.get("note"):
        lines.append(f"\n{t['note']}")
    return "\n".join(lines)


def choose(cur, repo: str, release_id: str | None) -> tuple[str | None, bool, str]:
    """立てる版を決める。(release_id, 出す必要があるか, 説明)"""
    staged = ship.status(cur, repo)["environments"].get("staging", {}).get("release_id")
    if release_id:
        if not ship.release_of(cur, release_id):
            return None, False, f"{release_id} という Release はない"
        return release_id, release_id != staged, f"{release_id} を立てる"
    if ship.unreleased(cur, repo):
        version = ship.next_version(cur, repo)
        rid, prs = ship.create_release(cur, repo, version)
        if rid:
            return rid, True, f"{rid} を切った（PR {len(prs)} 件）"
    if staged:
        return staged, False, f"まだ Release に入っていない PR がないので、いま staging に出ている {staged} のまま"
    return None, False, "立てる版がない（Release もマージ済みの PR もない）"


def wait(cur, request_id: int, timeout: float, poll: float = 3.0, settle: float = 960, log=print) -> tuple[bool, str]:
    """shipper がその依頼を片付けるまで待つ。途中の記録（作った・出した・シードを書いた）も出す。
    終わりは入口（release.touchable）が出たとき。結果（出した・入れ直した）のあとに入口が出ないまま settle 秒たったら
    （ship.yaml に touch がない版。シードの上限 900 秒＋手がかり 60 秒）、入口なしで終わりにする"""
    seen: set[int] = set()
    t0 = time.monotonic()
    done_at = None
    while time.monotonic() - t0 < timeout:
        cur.execute("select id, name, payload from events where (payload->>'request_id')::bigint = %s order by id", (request_id,))
        for r in cur.fetchall():
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            p = r["payload"]
            if r["name"] == "release.ship_failed":
                return False, f"失敗: {p.get('error')}"
            log(f"  {r['name']}" + (f"（{p['seconds']} 秒）" if p.get("seconds") is not None else ""))
            if r["name"] == "release.touchable":
                return True, "立った"
            if r["name"] in ship.RESULTS:
                done_at = time.monotonic()
        if done_at is not None and time.monotonic() - done_at > settle:
            return True, "出したが、入口の記録が出なかった（ship.yaml に touch がない版）"
        time.sleep(poll)
    return False, f"{int(timeout)} 秒待っても終わらなかった（shipper が動いているか factoryctl release status で見る）"


def up(cur, repo: str, release_id: str | None = None, fresh: bool = False, by: str = "builder",
       timeout: float = 3600, do_wait: bool = True, log=print) -> int:
    rid, needs_ship, msg = choose(cur, repo, release_id)
    log(msg)
    if not rid:
        return 1
    if needs_ship:
        ok, m = ship.request_ship(cur, rid, by, "factoryctl env up")
        log(m)
        if not ok:
            return 1
        if not do_wait:
            return 0
        ok, m = wait(cur, _last_request(cur, "release.ship_requested", rid), timeout, log=log)
        log(m)
        if not ok:
            return 1
    if fresh:
        ok, m = ship.request_reseed(cur, repo, "staging", by, "factoryctl env up")
        log(m)
        if not ok:
            return 1
        if not do_wait:
            return 0
        ok, m = wait(cur, _last_request(cur, "release.reseed_requested", rid), timeout, log=log)
        log(m)
        if not ok:
            return 1
    t = latest(cur, repo)
    if not t:
        log("入口の記録がない（ship.yaml に touch がない版か、まだ出していない）")
        return 1 if needs_ship or fresh else 0
    log("\n" + render(t))
    return 0


def _last_request(cur, name: str, rid: str) -> int:
    cur.execute("select id from events where name=%s and payload->>'release_id'=%s order by id desc limit 1", (name, rid))
    return cur.fetchone()["id"]
