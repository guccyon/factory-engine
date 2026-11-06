"""工場の前段（ADR 0035）の設定が守ることを、ファイルを読むだけで確かめる。docker も Caddy も使わない。

本物の流れ（偽の GitHub を相手にしたログイン、許していないアカウントの拒否、IP の拒否）は edge/README.md の「確かめ方」。
"""
import os
import re

import yaml

ORCH = os.path.join(os.path.dirname(__file__), "..", "..")
EDGE = yaml.safe_load(open(os.path.join(ORCH, "compose.edge.yaml"), encoding="utf-8"))
CADDY = open(os.path.join(ORCH, "edge", "Caddyfile"), encoding="utf-8").read()


def site(host: str) -> str:
    m = re.search(r"^\{\$EDGE_SCHEME\}://" + re.escape(host) + r"\.\{\$EDGE_DOMAIN\} \{\n(.*?)^\}", CADDY, re.S | re.M)
    assert m, host
    return m.group(1)


def test_browser_hosts_need_login_and_desk_does_not():
    assert "import github_login console:8790" in site("console")
    assert "import github_login console:8791" in site("proto")
    desk = site("desk")
    assert "github_login" not in desk and "forward_auth" not in desk
    assert "reverse_proxy desk:8792" in desk


def test_ip_check_runs_first_and_spoofed_identity_is_dropped():
    for host in ("console", "proto", "desk"):
        body = site(host)
        assert body.strip().startswith("route {"), host     # route の中は書いた順に動く
        assert body.index("import allow_ips") < body.index("reverse_proxy" if host == "desk" else "github_login"), host
    assert "request_header -X-Auth-Request-*" in site("desk")
    login = CADDY[CADDY.index("(github_login)"):]
    assert login.index("request_header -X-Auth-Request-*") < login.index("forward_auth")


def test_no_accounts_or_secrets_in_the_files():
    env = EDGE["services"]["oauth2-proxy"]["environment"]
    for key in ("OAUTH2_PROXY_GITHUB_USERS", "OAUTH2_PROXY_CLIENT_ID", "OAUTH2_PROXY_CLIENT_SECRET", "OAUTH2_PROXY_COOKIE_SECRET"):
        assert re.fullmatch(r"\$\{EDGE_[A-Z_]+:\?.+\}", env[key]), key   # .env から。無ければ compose が止まる


def test_proxy_is_internal_and_images_are_pinned():
    services = EDGE["services"]
    assert "ports" not in services["oauth2-proxy"]
    for name in ("edge", "oauth2-proxy"):
        image = services[name]["image"]
        assert re.search(r":v?\d+\.\d+\.\d+", image) and "latest" not in image, image


def test_overlay_only_adds_names_to_existing_services():
    # 工場のサービスには環境変数の上書きだけ（取り外しやすさ。ADR 0035 の決定 1）
    for name in ("console", "desk", "dispatcher"):
        assert set(EDGE["services"][name]) == {"environment"}, name
    assert EDGE["services"]["console"]["environment"]["CONSOLE_ALLOWED_HOSTS"].startswith("${CONSOLE_ALLOWED_HOSTS:-")
