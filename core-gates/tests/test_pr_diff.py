"""PR の差分の取り方（pr_diff）: 300 ファイルを超える PR はファイルごとの差分から組み立てる"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pr_diff  # noqa: E402


class R:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_uses_gh_diff_when_it_works(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R(0, "diff --git a/x b/x\n"))
    assert pr_diff.fetch("o/r", 1, lambda *a: "never") == "diff --git a/x b/x\n"


def test_transient_failure_goes_to_retrying_sh(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R(1, err="HTTP 502"))
    assert pr_diff.fetch("o/r", 1, lambda *a: "retried") == "retried"


def test_too_large_falls_back_to_files_and_puts_vendored_last(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R(1, err="could not find pull request diff: HTTP 406: Sorry, the diff exceeded the maximum number of files (300)."))
    calls = []

    def sh(*a):
        calls.append(a)
        return "\n".join([
            '{"filename":"tools/design-catalog/node_modules/a/index.js","status":"added","additions":10,"deletions":0,"patch":"+x"}',
            '{"filename":"specs/design/tokens.yaml","status":"modified","additions":1,"deletions":1,"patch":"-a\\n+b"}',
            '{"filename":"logo.png","status":"added","additions":0,"deletions":0}'])

    d = pr_diff.fetch("o/r", 678, sh)
    assert calls[0][:3] == ("gh", "api", "repos/o/r/pulls/678/files")
    assert "3 ファイル" in d and "依存の置き場や生成物に見えるファイルが 1 件" in d and "node_modules" in d.splitlines()[1]
    assert d.index("specs/design/tokens.yaml") < d.index("node_modules/a/index.js")
    assert "差分の本文なし" in d
