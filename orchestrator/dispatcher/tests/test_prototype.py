"""動くプロトタイプの配り口（prototype.py、ADR 0026 の決定 3）: 別オリジン・sandbox の囲い、配るファイルの絞り込み、レビューページからの入り口"""
import contextlib
import http.client
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import prototype  # noqa: E402
import review_page as rp  # noqa: E402

FILES = {"a.html": "design/9/a.html", "a-confirm.html": "design/9/a-confirm.html", "base.css": "design/9/base.css",
         "a.png": "design/9/a.png", "evil.html": "other/9/evil.html"}
OBJECTS = {"design/9/a.html": b'<a href="a-confirm.html">send</a>', "design/9/a-confirm.html": b"<p>ok</p>",
           "design/9/base.css": b"p{}", "design/9/a.png": b"\x89PNG", "other/9/evil.html": b"x"}


class FakeCur:
    """artifacts の UXSpec 9 の files だけを返す"""
    def __init__(self):
        self.row = None

    def execute(self, sql, args):
        name, aid = args
        self.row = {"key": FILES.get(name)} if aid == 9 else None

    def fetchone(self):
        return self.row


@contextlib.contextmanager
def fake_db():
    class Conn:
        @contextlib.contextmanager
        def cursor(self):
            yield FakeCur()
    yield Conn()


@pytest.fixture()
def server():
    srv = prototype.serve(fake_db, OBJECTS.get, {"localhost", "127.0.0.1"}, port=0)
    yield srv.server_address[1]
    srv.shutdown()


def get(port, path, host="localhost", method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, headers={"Host": f"{host}:{port}"})
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


def test_parse_only_allows_plain_names():
    assert prototype.parse("/p/9/a-confirm.html") == (9, "a-confirm.html", "html")
    assert prototype.parse("/p/9/base.css?x=1") == (9, "base.css", "css")
    for bad in ("/p/9/../x.html", "/p/9/a.js", "/p/9/.a.html", "/p/x/a.html", "/review/9", "/p/9/" + "a" * 40 + ".html"):
        assert prototype.parse(bad) is None


def test_serves_html_in_a_sandbox_with_no_network(server):
    status, h, body = get(server, "/p/9/a.html")
    assert status == 200 and body.startswith(b"<a href") and h["Content-Type"].startswith("text/html")
    csp = h["Content-Security-Policy"]
    assert csp.startswith("sandbox allow-scripts;") and "allow-same-origin" not in csp
    assert "connect-src 'none'" in csp and "form-action 'none'" in csp and "frame-ancestors 'none'" in csp
    assert f"style-src http://localhost:{server} " in csp
    assert h["X-Content-Type-Options"] == "nosniff" and h["Referrer-Policy"] == "no-referrer"
    assert get(server, "/p/9/base.css")[1]["Content-Type"].startswith("text/css")


def test_refuses_what_is_not_the_designers_file(server):
    assert get(server, "/p/9/missing.html")[0] == 404      # files にない
    assert get(server, "/p/9/evil.html")[0] == 404         # design/ の下でない
    assert get(server, "/p/8/a.html")[0] == 404            # ほかの UXSpec
    assert get(server, "/p/9/a.html", host="evil.example")[0] == 403
    assert get(server, "/p/9/a.html", method="POST")[0] == 405


def test_review_page_links_to_the_prototype_only_when_built():
    spec = {"recommended": "a", "prototype": True, "variants": [{"id": "a", "name": "n", "tradeoff": "t"}]}
    ctx = {"design": {"id": 9, "repo": "o/r", "issue": 5, "uxspec": spec, "files": FILES}, "verdict": None, "choice": None}
    page = rp.render(ctx, "tok", "http://localhost:8791")
    assert 'href="http://localhost:8791/p/9/a.html"' in page and 'rel="noopener noreferrer"' in page and "<iframe" not in page
    assert "動くプロトタイプを開く" not in rp.render(ctx, "tok")                                   # 配り口がない
    assert "動くプロトタイプを開く" not in rp.render({**ctx, "design": {**ctx["design"], "uxspec": {**spec, "prototype": False}}}, "tok", "http://x")
