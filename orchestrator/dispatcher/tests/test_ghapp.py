"""工場の GitHub の身元（ADR 0041）。App があればインストールトークン、無ければ PAT。"""
import base64
import io
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ghapp  # noqa: E402

needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="openssl が無い")


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def key(tmp_path):
    pem = tmp_path / "k.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(pem), "2048"], check=True, capture_output=True)
    pub = tmp_path / "k.pub"
    subprocess.run(["openssl", "rsa", "-in", str(pem), "-pubout", "-out", str(pub)], check=True, capture_output=True)
    return pem, pub


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("GH_APP_ID", "GH_APP_PRIVATE_KEY_B64", "GH_APP_INSTALLATION_ID", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FACTORY_REPOS", "o/r")
    ghapp._installations.clear()
    ghapp._cached.clear()


def app_env(monkeypatch, pem):
    monkeypatch.setenv("GH_APP_ID", "123")
    monkeypatch.setenv("GH_APP_PRIVATE_KEY_B64", base64.b64encode(pem.read_bytes()).decode())


def fake(monkeypatch, expires="2099-01-01T00:00:00Z"):
    calls = []

    def urlopen(req, timeout=30):
        body = json.loads(req.data) if req.data else None
        calls.append((req.get_method(), req.full_url.split("api.github.com/")[1], body, req.get_header("Authorization")))
        if req.full_url.endswith("app/installations?per_page=100"):
            return Resp(json.dumps([{"id": 9, "account": {"login": "other"}}, {"id": 42, "account": {"login": "O"}}]).encode())
        return Resp(json.dumps({"token": f"ghs_{len(calls)}", "expires_at": expires}).encode())
    monkeypatch.setattr(ghapp.urllib.request, "urlopen", urlopen)
    return calls


def test_without_app_uses_the_pat(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "pat")
    assert not ghapp.enabled() and ghapp.available()
    assert ghapp.token() == "pat" and ghapp.job_token("o/r") == "pat"


def test_check_stops_when_there_is_no_identity():
    with pytest.raises(SystemExit):
        ghapp.check()


@needs_openssl
def test_jwt_is_signed_with_the_app_key(monkeypatch, key, tmp_path):
    pem, pub = key
    app_env(monkeypatch, pem)
    jwt = ghapp.app_jwt(now=1_000_000)
    head, body, sig = jwt.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    claims = json.loads(base64.urlsafe_b64decode(pad(body)))
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": "123"}
    (tmp_path / "sig").write_bytes(base64.urlsafe_b64decode(pad(sig)))
    r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(tmp_path / "sig")],
                       input=f"{head}.{body}".encode(), capture_output=True)
    assert r.returncode == 0
    assert not [f for f in os.listdir(ghapp.tempfile.gettempdir()) if f.startswith("gh-app-")]   # 鍵の一時ファイルを残さない


@needs_openssl
def test_process_token_is_cached_and_job_token_is_scoped(monkeypatch, key):
    app_env(monkeypatch, key[0])
    calls = fake(monkeypatch)
    t1, t2 = ghapp.token(), ghapp.token()
    assert t1 == t2   # 期限まで同じものを使う
    assert [c[1] for c in calls] == ["app/installations?per_page=100", "app/installations/42/access_tokens"]   # owner の大文字小文字は問わない
    assert calls[1][2] == {} and calls[1][3].startswith("Bearer ey")   # 常駐のプロセスはインストールの全権限
    jt = ghapp.job_token("o/r")
    assert jt != t1
    method, path, body, _ = calls[-1]
    assert (method, path) == ("POST", "app/installations/42/access_tokens")
    assert body == {"repositories": ["r"], "permissions": ghapp.GRANTS["build"]}
    assert "workflows" not in body["permissions"]   # 作る段はワークフローを変えられない


@needs_openssl
def test_token_is_minted_again_near_expiry(monkeypatch, key):
    app_env(monkeypatch, key[0])
    monkeypatch.setenv("GH_APP_INSTALLATION_ID", "7")
    calls = fake(monkeypatch, expires="2000-01-01T00:00:00Z")   # すでに切れている
    ghapp.token()
    ghapp.token()
    assert [c[1] for c in calls] == ["app/installations/7/access_tokens"] * 2


def test_only_the_privileged_grant_can_change_workflows():
    assert [g for g, p in ghapp.GRANTS.items() if "workflows" in p] == ["privileged"]
    assert ghapp.GRANTS["observe"]["contents"] == "read"
