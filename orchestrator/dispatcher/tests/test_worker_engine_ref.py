"""ワーカーが取る Engine の版を工場の版にそろえる（#235）。DB は使わない"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
for k, v in {"DATABASE_URL": "postgresql://unused", "GH_TOKEN": "x", "CLAUDE_CODE_OAUTH_TOKEN": "x", "OBJECTSTORE_ENDPOINT": "localhost:1",
             "OBJECTSTORE_ACCESS_KEY": "x", "OBJECTSTORE_SECRET_KEY": "x"}.items():   # 読み込むのに要るだけ。つながない
    os.environ.setdefault(k, v)
import dispatcher  # noqa: E402
import version  # noqa: E402


def test_pinned_only_when_asked(monkeypatch, tmp_path):
    monkeypatch.setattr(version, "VERSION_FILE", str(tmp_path / "none"))
    assert dispatcher.worker_engine_ref({"ENGINE_VERSION": "c5afac9"}) is None   # 既定は渡さない（main を取る）
    assert dispatcher.worker_engine_ref({"WORKER_ENGINE_PIN": "1", "ENGINE_VERSION": "c5afac9"}) == "c5afac9"


def test_unknown_version_is_not_passed(monkeypatch, tmp_path):
    monkeypatch.setattr(version, "VERSION_FILE", str(tmp_path / "none"))
    assert dispatcher.worker_engine_ref({"WORKER_ENGINE_PIN": "1", "ENGINE_VERSION": "unknown"}) is None
    assert dispatcher.worker_engine_ref({"WORKER_ENGINE_PIN": "1"}) is None


def test_baked_version_wins(monkeypatch, tmp_path):
    f = tmp_path / "ENGINE_VERSION"
    f.write_text("8379823\n")
    monkeypatch.setattr(version, "VERSION_FILE", str(f))
    assert dispatcher.worker_engine_ref({"WORKER_ENGINE_PIN": "1", "ENGINE_VERSION": "c9fe85b"}) == "8379823"
