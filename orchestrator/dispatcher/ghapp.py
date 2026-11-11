"""工場の GitHub の身元（ADR 0041 の決定 2・順番 2）。

GitHub App が設定されていれば（GH_APP_ID と GH_APP_PRIVATE_KEY_B64）、App のインストールトークンを発行して使う。
無ければ今までどおり GH_TOKEN（fine-grained PAT）を使う。切り替えは .env だけで、コードの呼び出し側は変えない。

- token(): 常駐のプロセス（dispatcher・supervisor・pr_events・出荷・通知・factoryctl）が使う。インストールの全権限・全リポジトリ。
  期限（1 時間）の 10 分前に作り直す
- job_token(repo, grant): ワーカーのジョブに渡す。そのジョブのリポジトリと、役の区分（grant）の権限だけに絞る。
  毎回新しく発行する（ジョブの打ち切り JOB_TIMEOUT_SEC より長く持つように。トークンは 1 時間で切れる）

区分（grant）ごとの権限は GRANTS。順番 2 では全ジョブに「作る」（今の PAT と同じ権限）を渡し、区分ごとの絞り込みは順番 3 で行う。
秘密鍵は環境変数（base64）で受け、署名のときだけ 0600 の一時ファイルに書いて openssl に渡し、すぐ消す。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.request
from datetime import datetime

API = "https://api.github.com"
REFRESH_MARGIN_SEC = 600   # 期限のこれだけ前に作り直す

# 役の区分（ADR 0041 の決定 1）→ インストールトークンの権限。metadata: read は GitHub が必ず付ける
GRANTS: dict[str, dict[str, str]] = {
    # 見る: 読むのとコメント・ラベルだけ（順番 3 で使う）
    "observe": {"contents": "read", "pull_requests": "write", "issues": "write", "actions": "read", "checks": "read", "statuses": "read"},
    # 作る: 今の PAT（Contents・Pull requests・Issues の読み書き）＋ CI の結果を読む（続きのジョブが失敗のログを読む）
    "build": {"contents": "write", "pull_requests": "write", "issues": "write", "actions": "read", "checks": "read", "statuses": "read"},
    # 工程を変える（特権のライン。順番 4 で使う）: 作るに加えてワークフロー
    "privileged": {"contents": "write", "pull_requests": "write", "issues": "write", "actions": "read", "checks": "read", "statuses": "read",
                   "workflows": "write"},
}

_lock = threading.Lock()
_installations: dict[str, int] = {}             # owner → installation id
_cached: dict[str, tuple[str, float]] = {}      # owner → (token, 期限 epoch)


def enabled() -> bool:
    return bool(os.environ.get("GH_APP_ID") and os.environ.get("GH_APP_PRIVATE_KEY_B64"))


def available() -> bool:
    """GitHub に書ける身元があるか（App か PAT）"""
    return enabled() or bool(os.environ.get("GH_TOKEN"))


def check():
    """起動時に呼ぶ。どちらの身元も無ければ止める"""
    if not available():
        raise SystemExit("GitHub の身元が無い: GH_APP_ID と GH_APP_PRIVATE_KEY_B64（GitHub App）か GH_TOKEN（PAT）を .env に入れる")


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _sign(data: bytes) -> bytes:
    pem = base64.b64decode(os.environ["GH_APP_PRIVATE_KEY_B64"])
    fd, path = tempfile.mkstemp(prefix="gh-app-", suffix=".pem")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        r = subprocess.run(["openssl", "dgst", "-sha256", "-sign", path], input=data, capture_output=True, timeout=10)
        if r.returncode != 0:
            raise RuntimeError(f"App の秘密鍵で署名できない: {r.stderr.decode(errors='replace')[-300:]}")
        return r.stdout
    finally:
        os.unlink(path)


def app_jwt(now: float | None = None) -> str:
    """App として名乗る JWT（10 分まで。時計のずれに 60 秒戻す）"""
    now = int(now if now is not None else time.time())
    head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": os.environ["GH_APP_ID"]}, separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode()
    return f"{head}.{body}.{_b64url(_sign(signing_input))}"


def _call(method: str, path: str, bearer: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(f"{API}/{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {bearer}", "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json", "User-Agent": "factory-ghapp"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def _default_owner() -> str:
    repos = [r.strip() for r in os.environ.get("FACTORY_REPOS", "").split(",") if r.strip()]
    if not repos:
        raise RuntimeError("FACTORY_REPOS が空で、App のインストール先が決まらない")
    return repos[0].split("/")[0]


def installation_id(owner: str) -> int:
    """owner（ユーザーか組織）に入れた App のインストール。GH_APP_INSTALLATION_ID があればそれ（1 つの owner だけのとき）"""
    if os.environ.get("GH_APP_INSTALLATION_ID"):
        return int(os.environ["GH_APP_INSTALLATION_ID"])
    with _lock:
        if owner in _installations:
            return _installations[owner]
    for inst in _call("GET", "app/installations?per_page=100", app_jwt()) or []:
        if (inst.get("account") or {}).get("login", "").lower() == owner.lower():
            with _lock:
                _installations[owner] = int(inst["id"])
            return int(inst["id"])
    raise RuntimeError(f"GitHub App が {owner} に入っていない")


def _expiry(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _mint(owner: str, repos: list[str] | None = None, permissions: dict | None = None) -> tuple[str, float]:
    body: dict = {}
    if repos:
        body["repositories"] = repos
    if permissions:
        body["permissions"] = permissions
    r = _call("POST", f"app/installations/{installation_id(owner)}/access_tokens", app_jwt(), body)
    return r["token"], _expiry(r["expires_at"])


def token(repo: str | None = None) -> str:
    """常駐のプロセスのトークン。App が無ければ GH_TOKEN"""
    if not enabled():
        return os.environ.get("GH_TOKEN", "")
    owner = repo.split("/")[0] if repo else _default_owner()
    with _lock:
        hit = _cached.get(owner)
    if hit and hit[1] - time.time() > REFRESH_MARGIN_SEC:
        return hit[0]
    tok, exp = _mint(owner)
    with _lock:
        _cached[owner] = (tok, exp)
    return tok


def job_token(repo: str | None, grant: str = "build") -> str:
    """ワーカーのジョブのトークン。そのジョブのリポジトリと区分の権限だけ。App が無ければ GH_TOKEN（PAT は絞れない）"""
    if not enabled():
        return os.environ.get("GH_TOKEN", "")
    perms = GRANTS[grant]
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        return _mint(owner, [name], perms)[0]
    return _mint(_default_owner(), None, perms)[0]   # リポジトリを持たないジョブ（ケースの講評など）
