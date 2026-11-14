"""工場状況の宛先の公開範囲の検査（#226）。GitHub は呼ばず、応答を差し替えて確かめる。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
os.environ.setdefault("NOTIFIER", "log")

import stoppage  # noqa: E402


def fake(private: dict, fail: tuple = ()):
    def gh(method, path, body=None):
        repo = path.removeprefix("repos/")
        if repo in fail:
            raise OSError("down")
        return {"full_name": repo, "private": private[repo]}
    return gh


@pytest.fixture
def env(monkeypatch):
    def set_(status, repos):
        monkeypatch.setattr(stoppage, "STATUS_REPO", status)
        monkeypatch.setenv("FACTORY_REPOS", repos)
    return set_


def test_public_status_with_private_product_refuses(env):
    env("o/engine", "o/shop,o/public-app")
    why = stoppage.visibility_problem(fake({"o/engine": False, "o/shop": True, "o/public-app": False}))
    assert why and "o/shop" in why and "o/public-app" not in why


def test_all_public_is_fine(env):
    env("o/engine", "o/app")
    assert stoppage.visibility_problem(fake({"o/engine": False, "o/app": False})) is None


def test_private_status_is_fine(env):
    env("o/status", "o/shop")
    assert stoppage.visibility_problem(fake({"o/status": True, "o/shop": True})) is None


def test_status_is_the_product_itself(env):
    env("o/shop", "o/shop")
    assert stoppage.visibility_problem(fake({})) is None


def test_unknown_visibility_does_not_block(env):
    env("o/engine", "o/shop")
    assert stoppage.visibility_problem(fake({"o/shop": True}, fail=("o/engine",))) is None
    assert stoppage.visibility_problem(fake({"o/engine": False}, fail=("o/shop",))) is None


def test_no_status_repo(env):
    env("", "o/shop")
    assert stoppage.visibility_problem(fake({})) is None
