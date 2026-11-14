"""締め切りの時計（deadline.py）。出力が止まったワーカーを締め切りで止める（2026-11-13 ジョブ 8765・8770 が約 7 時間枠を握った）"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import deadline  # noqa: E402


def test_kills_at_deadline():
    hit = threading.Event()
    got = []
    deadline.arm("factory-job-1", time.time() + 0.05, kill=lambda n: (got.append(n), hit.set()))
    assert hit.wait(2)
    assert got == ["factory-job-1"]


def test_past_deadline_kills_now():
    hit = threading.Event()
    deadline.arm("factory-job-2", time.time() - 100, kill=lambda n: hit.set())
    assert hit.wait(2)


def test_cancel_before_deadline():
    got = []
    t = deadline.arm("factory-job-3", time.time() + 0.2, kill=got.append)
    t.cancel()
    time.sleep(0.4)
    assert got == []
