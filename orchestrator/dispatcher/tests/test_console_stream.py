"""操業コンソールのサーバーからの知らせ（SSE、ADR 0059 の 7、#476）: 記録が動いたときだけ changed を送り、何もなければ合図を送り、MAX_SEC で閉じる"""
import io
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import console_stream  # noqa: E402


class FakeCur:
    def __init__(self, versions):
        self.versions, self.row = versions, None

    def execute(self, sql):
        self.row = self.versions.pop(0) if len(self.versions) > 1 else self.versions[0]

    def fetchone(self):
        return self.row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_db(cur):
    @contextmanager
    def db():
        class Conn:
            def cursor(self):
                return cur
        yield Conn()
    return db


def run(versions, max_sec):
    t = [0.0]
    out = io.BytesIO()
    console_stream.serve(out, fake_db(FakeCur(versions)), clock=lambda: t[0],
                         sleep=lambda s: t.__setitem__(0, t[0] + s), max_sec=max_sec)
    return out.getvalue().decode()


def test_changed_only_when_version_moves():
    a = {"e": 1, "a": 0, "j": None}
    b = {"e": 2, "a": 1, "j": None}
    text = run([a, a, b], max_sec=6)
    assert text.startswith("retry: 5000\n\n")
    assert text.count("event: changed") == 2
    assert "data: 1:0:0" in text and "data: 2:1:0" in text


def test_keepalive_when_quiet():
    text = run([{"e": 1, "a": 0, "j": None}], max_sec=40)
    assert text.count("event: changed") == 1
    assert text.count(": keepalive") >= 2


def test_stops_when_page_closed():
    class Closed(io.BytesIO):
        def write(self, b):
            raise BrokenPipeError
    console_stream.serve(Closed(), fake_db(FakeCur([{"e": 1, "a": 0, "j": None}])),
                         clock=lambda: 0.0, sleep=lambda s: None)
