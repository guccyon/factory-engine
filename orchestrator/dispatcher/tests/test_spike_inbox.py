"""方針確認をラベルで承認したら受信トレイの件を閉じる（spike_line.settle_inbox）。2026-11-14 rally #1465 が受信トレイに残った"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import inbox  # noqa: E402
import spike_line  # noqa: E402


class Cur:
    def __init__(self, keys):
        self.keys = keys

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return [{"key": k} for k in self.keys]


def test_resolves_when_label_replaced_or_closed(monkeypatch):
    issues = {"1465": {"state": "open", "labels": [{"name": "factory:plan"}]},
              "1466": {"state": "open", "labels": [{"name": "factory:spike-human"}]},
              "1467": {"state": "closed", "labels": [{"name": "factory:spike-human"}]}}
    resolved = []
    monkeypatch.setattr(inbox, "resolve", lambda cur, key, by, outcome="": resolved.append((key, outcome)) or True)
    gh = lambda m, path, body=None: issues[path.rsplit("/", 1)[-1]]  # noqa: E731
    keys = ["spike:guccyon/rally#1465:10", "spike:guccyon/rally#1466:11", "spike:guccyon/rally#1467:12", "other:x"]
    assert spike_line.settle_inbox(Cur(keys), gh) == ["spike:guccyon/rally#1465:10", "spike:guccyon/rally#1467:12"]
    assert resolved == [("spike:guccyon/rally#1465:10", "approved_by_label"), ("spike:guccyon/rally#1467:12", "closed")]


def test_unreadable_issue_is_left_for_next_cycle(monkeypatch):
    monkeypatch.setattr(inbox, "resolve", lambda *a, **k: True)
    def gh(*a, **k):
        raise RuntimeError("down")
    assert spike_line.settle_inbox(Cur(["spike:guccyon/rally#1:1"]), gh) == []
