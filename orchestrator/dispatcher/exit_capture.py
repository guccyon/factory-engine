"""実装後の画面のキャプチャを PR と違和感の印につなぐ（ADR 0025 の 4、P4 の出口）。

製品の CI（rally の `.github/workflows/screenshots.yml`）は、PR の head のコミットに状態（commit status）
「画面のキャプチャ / <アプリ>」を付け、その target_url を CI の成果物（screenshots-<アプリ>）にする。
成果物の manifest.json が、変わった・新しい・消えた・撮れなかった・変わらない画面の一覧。

工場はこれを dispatcher の周回で、見る（observe: 状態を capture.status_seen に記録する。ADR 0050）と取り込む（記録を読む）に分けて読み:
- 変わった画面の後の画像をオブジェクトストアの choices/capture/ の下に置く（操業コンソールが中継する。外部の URL を画面に出さない）
- 人の選択（HumanChoice の kind exit_capture）を作る。受信トレイに「実装後の画面」として出て、画面ごとに違和感の印を付けられる
  （taste.flagged。教師信号 taste_flag として learn が束ねる）。見て違和感がなければ「見た」で閉じる（choice.seen。負例の材料）
- PR の本文に「実装後の画面のキャプチャ」の節を書く（アプリごとの変わった画面の表と成果物のリンク）

決定論的で LLM は使わない。同じ PR・head・アプリは 1 回だけ（events の capture.collected の dedupe_key）。
GitHub の状態の取得は ETag つき（変わっていなければ 304 で上限に数えない）。
"""
from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

import choice

CONTEXT_PREFIX = "画面のキャプチャ / "
ARTIFACT_URL = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/actions/runs/(\d+)/artifacts/(\d+)$")
PR_URL = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)$")
APP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
SCREEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$")
IMAGE_RE = re.compile(r"^images/([A-Za-z0-9][A-Za-z0-9_-]{0,80})\.(base|head|diff)\.png$")
WINDOW_H = 48                   # マージ後もこの時間は見る（キャプチャの CI はマージの後に終わることがある）。仮置き
OPEN_IDLE_H = 48                # 開いている PR も、head がこの時間より前から動いていなければ見ない（人待ちで止まった古い PR を出さない）。仮置き
EXPIRE_DAYS = 7                 # 人が見ないまま開いている依頼はこの日数で閉じる（受信トレイに溜めない）。仮置き
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
KIND_JA = {"changed": "変わった", "added": "新しい", "removed": "消えた"}
MARK, MARK_END = "<!-- factory:screen-capture -->", "<!-- /factory:screen-capture -->"


# ------------------------------------------------------------------ GitHub から読む
def capture_statuses(gh, repo: str, sha: str) -> list[dict]:
    """head のコミットの「画面のキャプチャ / <アプリ>」の状態。アプリごとに新しいもの 1 つ。{app, target_url, description}"""
    out: dict[str, dict] = {}
    for s in gh("GET", f"repos/{repo}/commits/{sha}/statuses?per_page=100") or []:   # 新しい順に返る
        ctx = s.get("context") or ""
        if not ctx.startswith(CONTEXT_PREFIX):
            continue
        app = ctx[len(CONTEXT_PREFIX):].strip()
        if APP_RE.match(app) and app not in out:
            out[app] = {"app": app, "target_url": s.get("target_url") or "", "description": (s.get("description") or "")[:140]}
    return [out[k] for k in sorted(out)]


def artifact_id(repo: str, url: str) -> int | None:
    """成果物の URL から id を取り出す。同じリポジトリの成果物だけ（他所の URL は読まない）"""
    m = ARTIFACT_URL.match(url or "")
    return int(m.group(3)) if m and m.group(1) == repo else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):   # noqa: D401  転送先は自分で開く（トークンを付けたまま外のストレージへ行かない）
        return None


def download_artifact(repo: str, aid: int, token: str, *, user_agent: str = "factory-exit-capture") -> bytes:
    """成果物の zip を取る。API は 302 で一時的なストレージの URL を返すので、そこへはトークンを付けずに行く"""
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/artifacts/{aid}/zip",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": user_agent})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=30) as r:   # 転送しない応答（まれ）
            return _read_capped(r)
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise
        loc = e.headers.get("Location") or ""
    if not loc.startswith("https://"):
        raise ValueError("成果物の転送先が https でない")
    with urllib.request.urlopen(urllib.request.Request(loc, headers={"User-Agent": user_agent}), timeout=60) as r:
        return _read_capped(r)


def _read_capped(r) -> bytes:
    data = r.read(MAX_ZIP_BYTES + 1)
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError("成果物が大きすぎる")
    return data


def read_zip(data: bytes) -> tuple[dict, dict[str, bytes]]:
    """成果物の zip から manifest.json と画像を取り出す。画像は images/<id>.(base|head|diff).png の形のものだけ"""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        if "manifest.json" not in names:
            raise ValueError("manifest.json がない")
        manifest = json.loads(z.read("manifest.json").decode("utf-8"))
        images = {}
        for n in names:
            if IMAGE_RE.match(n) and z.getinfo(n).file_size <= MAX_IMAGE_BYTES:
                images[n] = z.read(n)
    return manifest, images


# ------------------------------------------------------------------ manifest → 人に見せる画面
def screens(manifest: dict) -> list[dict]:
    """人に見せる画面（変わった・新しい・消えた）。変わった画面は違う画素の多い順。{id, title, kind, diff_ratio, image}"""
    out = []
    for kind in ("changed", "added", "removed"):
        items = [i for i in manifest.get(kind) or [] if isinstance(i, dict) and SCREEN_RE.match(str(i.get("id") or ""))]
        if kind == "changed":
            items.sort(key=lambda i: -float(i.get("diff_ratio") or 0))
        for i in items:
            image = i.get("base") if kind == "removed" else i.get("head")
            out.append({"id": i["id"], "title": str(i.get("title") or i["id"])[:80], "kind": kind,
                        "diff_ratio": float(i["diff_ratio"]) if kind == "changed" and i.get("diff_ratio") is not None else None,
                        "image": image if isinstance(image, str) and IMAGE_RE.match(image) else None})
    return out


def failed(manifest: dict) -> list[dict]:
    return [{"id": str(i.get("id"))[:80], "title": str(i.get("title") or i.get("id"))[:80]} for i in manifest.get("failed") or [] if isinstance(i, dict)]


def image_key(repo: str, number: int, sha: str, app: str, screen_id: str) -> str:
    return f"choices/capture/{repo.replace('/', '--')}/{number}/{sha[:12]}/{app}/{screen_id}.png"


def option_note(s: dict) -> str:
    ratio = f"（違う画素 {s['diff_ratio'] * 100:.2f}%）" if s.get("diff_ratio") is not None else ""
    return f"{KIND_JA[s['kind']]}画面{ratio} · {s['id']}"


# ------------------------------------------------------------------ 記録（人の選択と PR の本文）
def collected(cur, pr_url: str, sha: str, app: str) -> bool:
    cur.execute("select 1 from events where dedupe_key=%s", (f"capture.collected:{pr_url}@{sha}:{app}",))
    return cur.fetchone() is not None


def supersede(cur, pr_url: str, app: str, keep: int | None = None) -> int:
    """同じ PR・アプリの前の head の依頼を閉じる（新しい head のキャプチャで置き換わった）"""
    cur.execute("select id from artifacts where type='HumanChoice' and payload->>'status'='open' and payload->>'kind'='exit_capture' "
                "and payload->'subject'->>'pr_url'=%s and payload->'subject'->>'app'=%s and id is distinct from %s", (pr_url, app, keep))
    ids = [r["id"] for r in cur.fetchall()]
    for aid in ids:
        choice.close(cur, aid, "superseded", "factory", "新しい head のキャプチャで置き換わった")
    return len(ids)


def expire(cur, now: datetime | None = None, days: int = EXPIRE_DAYS) -> int:
    """人が見ないまま開いている出口の依頼を閉じる。印は閉じた後も付けられる"""
    now = now or datetime.now(timezone.utc)
    cur.execute("select id from artifacts where type='HumanChoice' and payload->>'status'='open' and payload->>'kind'='exit_capture' "
                "and created_at < %s", (now - timedelta(days=days),))
    ids = [r["id"] for r in cur.fetchall()]
    for aid in ids:
        choice.close(cur, aid, "expired", "factory", f"{days} 日見られなかった")
    return len(ids)


def collect(cur, *, repo: str, number: int, corr: str, sha: str, status: dict, zip_bytes: bytes, put) -> dict:
    """成果物 1 つ（1 アプリ）を読み、画像を置き、人の選択を作って capture.collected を残す。返り値は記録した payload"""
    manifest, images = read_zip(zip_bytes)
    app, pr_url = status["app"], f"https://github.com/{repo}/pull/{number}"
    shown = screens(manifest)
    options = []
    for i, s in enumerate(shown[:choice.MAX_OPTIONS_BY_KIND["exit_capture"]], 1):
        data = images.get(s["image"] or "")
        key = None
        if data:
            key = image_key(repo, number, sha, app, s["id"])
            put(key, data, "image/png")
        options.append({"id": f"s{i}", "label": s["title"], "note": option_note(s), "image_key": key})
    rest = len(shown) - len(options)
    aid = None
    if options:
        context = f"{status.get('description') or ''}。head {sha[:7]}。" + (f"ほか {rest} 画面は成果物で。" if rest > 0 else "") + \
                  "違和感があれば画面ごとに一言で印を付け、なければ「見た」で閉じる"
        aid = choice.request(cur, "exit_capture", corr, f"{app} の実装後の画面（PR #{number}）", options,
                             subject={"repo": repo, "ticket": corr.split("#")[-1] if "#" in corr else "", "pr_url": pr_url, "app": app,
                                      "head_sha": sha, "artifact_url": status.get("target_url") or ""},
                             context=context, produced_by="exit_capture")
        supersede(cur, pr_url, app, keep=aid)
    payload = {"pr_url": pr_url, "repo": repo, "number": number, "head_sha": sha, "app": app, "artifact_url": status.get("target_url") or "",
               "description": status.get("description") or "", "choice_id": aid,
               "screens": [{k: s[k] for k in ("id", "title", "kind", "diff_ratio")} for s in shown],
               "failed": failed(manifest), "unchanged": len(manifest.get("unchanged") or [])}
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('capture.collected',%s,%s,%s) on conflict (dedupe_key) do nothing",
                (corr, Jsonb(payload), f"capture.collected:{pr_url}@{sha}:{app}"))
    return payload


def record_failure(cur, corr: str, pr_url: str, sha: str, app: str, reason: str):
    """成果物が読めない（期限切れ・消えた）。同じ head・アプリでもう取りに行かない"""
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('capture.collected',%s,%s,%s) on conflict (dedupe_key) do nothing",
                (corr, Jsonb({"pr_url": pr_url, "head_sha": sha, "app": app, "error": reason[:200]}), f"capture.collected:{pr_url}@{sha}:{app}"))


def latest_for_head(cur, pr_url: str, sha: str) -> list[dict]:
    """PR の head で集めたアプリごとの結果（PR の本文の節の材料）"""
    cur.execute("select payload from events where name='capture.collected' and payload->>'pr_url'=%s and payload->>'head_sha'=%s "
                "and not payload ? 'error' order by payload->>'app', id", (pr_url, sha))
    by_app = {}
    for r in cur.fetchall():
        by_app[r["payload"]["app"]] = r["payload"]
    return [by_app[k] for k in sorted(by_app)]


def pr_section(results: list[dict], sha: str) -> str:
    lines = [MARK, "## 実装後の画面のキャプチャ", "",
             f"工場が CI の成果物から載せています（head `{sha[:7]}`）。見た目に違和感があれば、操業コンソールの受信トレイ「実装後の画面」で"
             "画面ごとに一言の印を付けてください（教師信号 taste_flag として Learn に入ります。ADR 0025 の 4）。"]
    for r in results:
        sc, fl = r.get("screens") or [], r.get("failed") or []
        counts = "・".join(f"{KIND_JA[k]} {n}" for k in ("changed", "added", "removed") if (n := sum(1 for s in sc if s["kind"] == k)))
        lines += ["", f"### {r['app']}（{counts or '見た目が変わった画面なし'}" + (f"・撮れなかった {len(fl)}" if fl else "") + f"・変わらない {r.get('unchanged', 0)}）"]
        if sc:
            lines += ["", "| | 画面 | id | 違う画素 |", "|---|---|---|---|"]
            lines += [f"| {KIND_JA[s['kind']]} | {_cell(s['title'])} | `{s['id']}` | "
                      + (f"{s['diff_ratio'] * 100:.2f}%" if s.get("diff_ratio") is not None else "") + " |" for s in sc]
        if fl:
            lines += ["", "撮れなかった画面（押す文言やたどり方が今の画面と合っていない可能性）: " + "、".join(f"{_cell(f['title'])}（`{f['id']}`）" for f in fl)]
        if r.get("artifact_url"):
            lines += ["", f"[前・後・差分の画像（CI の成果物）]({r['artifact_url']})"]
    lines.append(MARK_END)
    return "\n".join(lines)


def _cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def replace_section(body: str, section: str) -> str:
    """本文の節を差し替える（なければ末尾に足す）。人と実装役が書いた部分は触らない"""
    pat = re.compile(re.escape(MARK) + r".*?" + re.escape(MARK_END), re.S)
    if pat.search(body):
        return pat.sub(lambda _: section, body, count=1)
    return (body.rstrip() + "\n\n" if body.strip() else "") + section + "\n"


# ------------------------------------------------------------------ 周回（dispatcher の poll_loop から）
def candidates(cur, repos: list[str], now: datetime | None = None, window_h: int = WINDOW_H) -> list[dict]:
    """見る PR: 工場の PR のうち、開いているかマージから window_h 時間以内のもの。head は PR の見張りの pr.head_updated の最新"""
    now = now or datetime.now(timezone.utc)
    cur.execute("select distinct on (payload->>'pr_url') correlation_id, payload->>'pr_url' as url from artifacts "
                "where type='PR' and payload->>'pr_url' is not null order by payload->>'pr_url', id desc")
    out = []
    for r in cur.fetchall():
        m = PR_URL.match(r["url"] or "")
        if not m or m.group(1) not in repos:
            continue
        cur.execute("select name, occurred_at from events where name in ('pr.merged','pr.closed_unmerged') and payload->>'pr_url'=%s order by id desc limit 1",
                    (r["url"],))
        done = cur.fetchone()
        if done and (done["name"] == "pr.closed_unmerged" or done["occurred_at"] < now - timedelta(hours=window_h)):
            continue
        cur.execute("select payload->>'head_sha' as sha, occurred_at from events where name='pr.head_updated' and payload->>'pr_url'=%s order by id desc limit 1",
                    (r["url"],))
        h = cur.fetchone()
        if h and not done and h["occurred_at"] < now - timedelta(hours=OPEN_IDLE_H):
            continue
        if h and h["sha"]:
            out.append({"repo": m.group(1), "number": int(m.group(2)), "url": r["url"], "corr": r["correlation_id"], "sha": h["sha"]})
    return out


def observe(cur, gh, repos: list[str], *, log=print) -> int:
    """小さなポーリング（ADR 0050 の決定 2）。見る PR の head の「画面のキャプチャ / <アプリ>」の状態を、出来事 capture.status_seen として残す。
    同じ PR・head・アプリは 1 回だけ（dedupe_key）。取り込みはこの記録を読む。Webhook（status イベント）が使えたら入口を足すだけでよい"""
    n = 0
    for p in candidates(cur, repos):
        try:
            sts = capture_statuses(gh, p["repo"], p["sha"])
        except Exception as e:  # noqa: BLE001  次の周回でやり直す
            log("capture statuses failed", p["url"], repr(e))
            continue
        for s in sts:
            cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('capture.status_seen', %s, %s, %s) "
                        "on conflict (dedupe_key) do nothing returning id",
                        (p["corr"], Jsonb({"pr_url": p["url"], "repo": p["repo"], "number": p["number"], "head_sha": p["sha"], **s}),
                         f"capture.status_seen:{p['url']}@{p['sha']}:{s['app']}"))
            n += 1 if cur.fetchone() else 0
    return n


def seen_statuses(cur, pr_url: str, sha: str) -> list[dict]:
    """記録に残った、その head のキャプチャの状態（アプリ順）"""
    cur.execute("select payload from events where name='capture.status_seen' and payload->>'pr_url'=%s and payload->>'head_sha'=%s order by payload->>'app'",
                (pr_url, sha))
    return [{k: r["payload"].get(k, "") for k in ("app", "target_url", "description")} for r in cur.fetchall()]


def step(cur, gh, download, put, repos: list[str], *, dry_run: bool = False, log=print) -> int:
    """工場の PR の head ごとに、まだ読んでいないキャプチャを読む。返り値は読んだ成果物の数。
    状態は observe が残した記録（capture.status_seen）から読む（ADR 0050）"""
    n = 0
    expire(cur)
    observe(cur, gh, repos, log=log)
    for p in candidates(cur, repos):
        new = [s for s in seen_statuses(cur, p["url"], p["sha"]) if not collected(cur, p["url"], p["sha"], s["app"])]
        if not new:
            continue
        for s in new:
            aid = artifact_id(p["repo"], s["target_url"])
            if aid is None:
                record_failure(cur, p["corr"], p["url"], p["sha"], s["app"], "target_url が同じリポジトリの成果物でない")
                continue
            try:
                data = download(p["repo"], aid)
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):   # 期限切れ・消えた
                    record_failure(cur, p["corr"], p["url"], p["sha"], s["app"], f"成果物を取れない（HTTP {e.code}）")
                else:
                    log("capture download failed", p["url"], s["app"], repr(e))
                continue
            except Exception as e:  # noqa: BLE001
                log("capture download failed", p["url"], s["app"], repr(e))
                continue
            try:
                got = collect(cur, repo=p["repo"], number=p["number"], corr=p["corr"], sha=p["sha"], status=s, zip_bytes=data, put=put)
            except (ValueError, zipfile.BadZipFile, json.JSONDecodeError) as e:
                record_failure(cur, p["corr"], p["url"], p["sha"], s["app"], f"成果物を読めない: {e}")
                continue
            n += 1
            log("capture collected", p["url"], p["sha"][:7], s["app"], "screens", len(got["screens"]), "choice", got["choice_id"])
        if dry_run:
            continue
        results = latest_for_head(cur, p["url"], p["sha"])
        if not results:
            continue
        try:
            pr = gh("GET", f"repos/{p['repo']}/pulls/{p['number']}")
            body = replace_section(pr.get("body") or "", pr_section(results, p["sha"]))
            if body != (pr.get("body") or ""):
                gh("PATCH", f"repos/{p['repo']}/pulls/{p['number']}", {"body": body})
        except Exception as e:  # noqa: BLE001  本文は次の head か手で。記録は残っている
            log("capture pr body failed", p["url"], repr(e))
    return n
