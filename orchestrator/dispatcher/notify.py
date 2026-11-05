"""通知のルーティング（ADR 0032）。通知の種類ごとに、宛先へ出し分ける。

- 設定の正本は製品のリポジトリの .factory-config/notifications.yaml（決定 6）。既定ブランチの今の版を読み、
  検査を通った版を制御プレーンに写す（events の notify.config_changed）。送るときはこの写しを引く。並べて動かす工場も同じ版に揃う
- 検査を通らない版は notify.config_rejected に残し（受信トレイの error に出る）、前に通った写しのまま送り続ける
- 設定がない工場は、今の環境変数（INBOX_NOTIFY・SLACK_WEBHOOK_URL・INBOX_QUIET・INBOX_SLACK_MENTIONS）から
  宛先を組み立て、今と同じに動く（決定 8）
- credential の値は設定に書かず、秘密の置き場の参照（secret: env:NAME）だけを書く。値は送る直前に引き、記録・ログに出さない（決定 1）
- 通知の失敗でラインを止めない。送れたかどうかは呼び出し元が記録する（決定 7）
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import yaml
from psycopg.types.json import Jsonb

ENV = os.environ
CONFIG_PATH = ".factory-config/notifications.yaml"
POLL_SEC = int(ENV.get("NOTIFY_CONFIG_POLL_SEC", "300"))   # 製品のリポジトリの設定を読みに行く間隔。304 は GitHub の上限に数えられない
TZ = ZoneInfo(ENV.get("REPORT_TZ", "Asia/Tokyo"))

PARTIES = ("provider", "business_owner")                    # 提供元 / 事業主
VIAS = ("slack_webhook", "slack_app", "github-issue", "log")
READY_VIAS = ("slack_webhook", "github-issue", "log")       # slack_app は形だけ（ADR 0032 の順番 5）
SECRET_STORES = ("env",)                                    # aws-sm（AWS Secrets Manager）などは後で足す
SECRET_REF = re.compile(r"^([a-z][a-z0-9-]*):(.+)$")
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

# definitions/notifications.yaml の kinds の写し（dispatcher のコンテナには definitions/ がない。食い違いはテストで見る）
KINDS = {
    "report.factory_health": {"parties": ("provider", "business_owner"),
                              "defaults": {"provider": ("provider",), "business_owner": ("business_owner", "provider")}, "status": "ready"},
    "report.product": {"parties": ("provider", "business_owner"),
                       "defaults": {"provider": ("business_owner", "provider"), "business_owner": ("business_owner", "provider")}, "status": "ready"},
    "request.inbox": {"parties": ("business_owner",),
                      "defaults": {"provider": ("business_owner",), "business_owner": ("business_owner",)}, "status": "ready"},
    "done.task": {"parties": ("provider", "business_owner"), "defaults": {"provider": (), "business_owner": ()}, "status": "later"},
}

# 受信トレイの件を、誰の出番か（inbox.WHO）でどの組織に送るか（決定 5）。builder（建設の判断）は通知せず、建設者が問い合わせて拾う
WHO_PARTIES = {
    "po": {"provider": ("business_owner",), "business_owner": ("business_owner",)},
    "design_chief": {"provider": ("business_owner",), "business_owner": ("business_owner",)},
    "requester": {"provider": ("business_owner",), "business_owner": ("business_owner",)},
    "owner": {"provider": (), "business_owner": ("business_owner",)},   # 提供元の代理操業の工場の判断は、後で factory of factories の側でまとめる
    "builder": {"provider": (), "business_owner": ()},
}


def _log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), "[notify]", *a, flush=True)


@dataclass(frozen=True)
class Destination:
    name: str
    party: str
    via: str
    secret: str = ""          # 秘密の置き場の参照（env:NAME）。値ではない
    channel: str = ""         # slack_app のときだけ
    quiet: str = ""           # 送らない時間（REPORT_TZ の時。例 22-7）
    mentions: tuple = ()      # ((who, "<@U…>"), …)

    def mention(self, who: str) -> str:
        return dict(self.mentions).get(who, "")


@dataclass
class Config:
    operated_by: str
    destinations: dict[str, Destination]
    routes: dict[str, list[tuple[str, bool]]] = field(default_factory=dict)   # 種類 → [(宛先の名前, 有効)]
    explicit: bool = True     # False は環境変数から組み立てた今までの設定（決定 8）


class ConfigError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def parse(doc) -> Config:
    """notifications.yaml の中身を検査して Config にする。おかしいところはまとめて ConfigError に"""
    p: list[str] = []
    if not isinstance(doc, dict):
        raise ConfigError(["中身が辞書ではない"])
    operated_by = doc.get("operated_by", "provider")
    if operated_by not in PARTIES:
        p.append(f"operated_by は {'/'.join(PARTIES)} のどれか（{operated_by!r}）")
    conns: dict[str, dict] = {}
    for n, c in (doc.get("connections") or {}).items():
        if not NAME.match(str(n)) or not isinstance(c, dict):
            p.append(f"連携 {n!r} の名前か形がおかしい")
            continue
        if c.get("party") not in PARTIES:
            p.append(f"連携 {n}: party は {'/'.join(PARTIES)} のどれか")
        if c.get("via") not in VIAS:
            p.append(f"連携 {n}: via は {'/'.join(VIAS)} のどれか")
        elif c["via"] not in READY_VIAS:
            p.append(f"連携 {n}: via {c['via']} はまだ使えない（{'/'.join(READY_VIAS)}）")
        ref = c.get("secret") or ""
        if c.get("via") in ("slack_webhook", "slack_app"):
            m = SECRET_REF.match(str(ref))
            if not m:
                p.append(f"連携 {n}: secret は <置き場>:<名前>（例 env:SLACK_WEBHOOK_URL_OWNER）。値そのものは書かない")
            elif m.group(1) not in SECRET_STORES:
                p.append(f"連携 {n}: 秘密の置き場 {m.group(1)} はまだ使えない（{'/'.join(SECRET_STORES)}）")
        if str(ref).startswith("https://") or str(ref).startswith("xox"):
            p.append(f"連携 {n}: secret に値そのものが書かれている。秘密の置き場に移して参照だけを書く")
        conns[str(n)] = c
    dests: dict[str, Destination] = {}
    for n, d in (doc.get("destinations") or {}).items():
        if not NAME.match(str(n)) or not isinstance(d, dict):
            p.append(f"宛先 {n!r} の名前か形がおかしい")
            continue
        c = conns.get(str(d.get("connection")))
        if c is None:
            p.append(f"宛先 {n}: 連携 {d.get('connection')!r} がない")
            continue
        if c.get("via") == "slack_app" and not d.get("channel"):
            p.append(f"宛先 {n}: Slack アプリの連携には channel が要る")
        if c.get("via") == "slack_webhook" and d.get("channel"):
            p.append(f"宛先 {n}: Webhook は URL ごとにチャンネルが決まるので channel を書かない")
        q = str(d.get("quiet") or "")
        if q and not re.match(r"^\d{1,2}-\d{1,2}$", q):
            p.append(f"宛先 {n}: quiet は 22-7 のように時で")
        mentions = d.get("mentions") or {}
        if not isinstance(mentions, dict):
            p.append(f"宛先 {n}: mentions は who: <@U…> の辞書")
            mentions = {}
        dests[str(n)] = Destination(str(n), c.get("party", ""), c.get("via", ""), str(c.get("secret") or ""), str(d.get("channel") or ""), q,
                                    tuple(sorted((str(k), str(v)) for k, v in mentions.items())))
    routes: dict[str, list[tuple[str, bool]]] = {}
    for kind, rs in (doc.get("routes") or {}).items():
        spec = KINDS.get(kind)
        if spec is None:
            p.append(f"割り当て: 種類 {kind!r} はない（{', '.join(KINDS)}）")
            continue
        out = []
        for r in rs or []:
            r = {"to": r} if isinstance(r, str) else r
            to, enabled = str((r or {}).get("to")), bool((r or {}).get("enabled", True))
            d = dests.get(to)
            if d is None:
                p.append(f"割り当て {kind}: 宛先 {to!r} がない")
                continue
            if d.party not in spec["parties"]:
                p.append(f"割り当て {kind}: 宛先 {to}（{d.party}）には出せない種類（出してよい組織 {'/'.join(spec['parties'])}）")
                continue
            out.append((to, enabled))
        routes[kind] = out
    if p:
        raise ConfigError(p)
    return Config(operated_by, dests, routes, True)


def implicit(channels: list[str] | None = None, quiet: str | None = None, mentions: dict | None = None) -> Config:
    """設定がない工場の、今までの通知（INBOX_NOTIFY の通知先すべてに受信トレイを送る）。宛先の名前は今の通知先の名前のまま（記録を引き継ぐ）"""
    channels = [c.strip() for c in ENV.get("INBOX_NOTIFY", "log").split(",") if c.strip()] if channels is None else channels
    quiet = ENV.get("INBOX_QUIET", "") if quiet is None else quiet
    if mentions is None:
        mentions = dict(p.split("=", 1) for p in ENV.get("INBOX_SLACK_MENTIONS", "").split(",") if "=" in p)
    dests = {}
    for ch in channels:
        via = "slack_webhook" if ch == "slack" else ch
        dests[ch] = Destination(ch, "provider", via, "env:SLACK_WEBHOOK_URL" if via == "slack_webhook" else "", "", quiet,
                                tuple(sorted(mentions.items())) if via == "slack_webhook" else ())
    return Config("provider", dests, {"request.inbox": [(ch, True) for ch in channels]}, False)


def targets(cfg: Config, kind: str, who: str | None = None) -> list[Destination]:
    """その種類（と受信トレイなら誰の出番か）を送る宛先。無効にした割り当ては除く"""
    if not cfg.explicit:
        return [cfg.destinations[n] for n, on in cfg.routes.get(kind, []) if on and n in cfg.destinations]
    spec = KINDS.get(kind)
    if spec is None:
        return []
    if kind in cfg.routes:
        ds = [cfg.destinations[n] for n, on in cfg.routes[kind] if on and n in cfg.destinations]
    else:
        parties = spec["defaults"].get(cfg.operated_by, ())
        ds = [d for d in cfg.destinations.values() if d.party in parties]
    if who is not None and kind == "request.inbox":
        allowed = WHO_PARTIES.get(who, WHO_PARTIES["owner"]).get(cfg.operated_by, ())
        ds = [d for d in ds if d.party in allowed]
    return ds


def quiet(now: datetime, spec: str) -> bool:
    if not spec or "-" not in spec:
        return False
    a, b = (int(x) for x in spec.split("-", 1))
    h = now.astimezone(TZ).hour
    return a <= h < b if a < b else (h >= a or h < b)


def secret(ref: str) -> str:
    """秘密の置き場の参照から値を引く。値はログ・記録に出さない"""
    m = SECRET_REF.match(ref or "")
    if not m:
        return ""
    store, name = m.groups()
    if store == "env":
        return ENV.get(name, "")
    raise ValueError(f"秘密の置き場 {store} はまだ使えない")


def post(dest: Destination, text: str) -> bool:
    """1 つの宛先に本文を送る。届けられなければ False（例外も出しうる。呼び出し元が握る）"""
    if dest.via == "log":
        _log(dest.name, text.replace("\n", " | "))
        return True
    if dest.via == "github-issue":
        import stoppage   # 状況 Issue の探し方・GitHub の呼び方は stoppage に 1 か所
        if not (stoppage.STATUS_REPO and ENV.get("GH_TOKEN")) or ENV.get("WORKER_DRY_RUN"):
            _log(dest.name, "github-issue skipped (no STATUS_REPO/GH_TOKEN or dry run)")
            return False
        stoppage._gh("POST", f"repos/{stoppage.STATUS_REPO}/issues/{stoppage._status_issue()}/comments", {"body": text})
        return True
    if dest.via == "slack_webhook":
        url = secret(dest.secret)
        if not url.startswith("https://hooks.slack.com/"):
            _log(dest.name, f"slack skipped（{dest.secret} に Webhook の URL がない）")
            return False
        req = urllib.request.Request(url, method="POST", data=json.dumps({"text": text}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15):
            return True
    _log(dest.name, "unsupported via", dest.via)
    return False


# ------------------------------------------------------------------ 設定の読み込みと写し
def config_repo() -> str:
    return ENV.get("NOTIFY_CONFIG_REPO") or next((r.strip() for r in ENV.get("FACTORY_REPOS", "").split(",") if r.strip()), "")


def _fetch(repo: str):
    """既定ブランチの notifications.yaml（本文, blob の sha）。なければ None"""
    import ghapi
    try:
        c = ghapi.request("GET", f"repos/{repo}/contents/{CONFIG_PATH}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return base64.b64decode(c["content"]).decode(), c.get("sha", "")


def _latest(cur):
    cur.execute("select name, payload from events where name in ('notify.config_changed','notify.config_rejected') order by id desc limit 1")
    return cur.fetchone()


def refresh(cur, fetch=None) -> str:
    """製品のリポジトリの設定を読み、変わっていれば検査して写す。返り値は何をしたか（テストと記録向け）"""
    repo = config_repo()
    if not repo:
        return "no-repo"
    got = (fetch or _fetch)(repo)
    last = _latest(cur)
    if got is None:
        if last and last["name"] == "notify.config_changed" and not last["payload"].get("removed"):
            cur.execute("insert into events(name, payload) values ('notify.config_changed', %s)",
                        (Jsonb({"repo": repo, "path": CONFIG_PATH, "removed": True}),))
            _log("config removed; back to the environment variables")
            return "removed"
        return "absent"
    text, sha = got
    h = hashlib.sha256(text.encode()).hexdigest()
    if last and last["payload"].get("hash") == h:
        return "same"
    try:
        doc = yaml.safe_load(text)
        cfg = parse(doc)
    except (ConfigError, yaml.YAMLError) as e:
        problems = e.problems if isinstance(e, ConfigError) else [f"YAML を読めない: {e}"[:300]]
        cur.execute("insert into events(name, payload) values ('notify.config_rejected', %s)",
                    (Jsonb({"repo": repo, "path": CONFIG_PATH, "sha": sha, "hash": h, "problems": problems[:20]}),))
        _log("config rejected", problems[:3])
        return "rejected"
    cur.execute("insert into events(name, payload) values ('notify.config_changed', %s)",
                (Jsonb({"repo": repo, "path": CONFIG_PATH, "sha": sha, "hash": h, "doc": doc, "operated_by": cfg.operated_by,
                        "destinations": sorted(cfg.destinations), "routes": {k: [n for n, on in v if on] for k, v in cfg.routes.items()}}),))
    _log("config changed", sha[:8], sorted(cfg.destinations))
    return "changed"


def cached(cur) -> Config:
    """制御プレーンに写した最新の通った設定。なければ（消されたら）環境変数の今までの設定"""
    cur.execute("select payload from events where name='notify.config_changed' order by id desc limit 1")
    r = cur.fetchone()
    if not r or r["payload"].get("removed"):
        return implicit()
    try:
        return parse(r["payload"]["doc"])
    except ConfigError as e:   # Engine の定義が変わって前の写しが通らなくなった。今までの設定で送り続ける
        _log("cached config no longer valid", e.problems[:3])
        return implicit()


_checked = {"at": 0.0}


def current(cur) -> Config:
    """送るときに使う設定。POLL_SEC ごとに製品のリポジトリを読み直す。読めなくても写しで送る"""
    if time.time() - _checked["at"] >= POLL_SEC:
        _checked["at"] = time.time()
        try:
            refresh(cur)
        except Exception as e:  # noqa: BLE001  設定を読めなくても通知とラインは止めない
            _log("refresh failed", repr(e)[:200])
    return cached(cur)


def rejected(cur) -> dict | None:
    """いちばん新しい版が検査を通らなかったなら、その中身（受信トレイの error に出す）"""
    r = _latest(cur)
    return r["payload"] if r and r["name"] == "notify.config_rejected" else None
