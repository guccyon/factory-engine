"""ジョブの出力の読み取り（output_pump）: 出力が止まっても打ち切りの時刻と止める印で抜けること（2026-11-14 job 8765・8770）。
本物のプロセス（python -c）で試す。DB は使わない。
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import output_pump  # noqa: E402


def _spawn(code: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-u", "-c", code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def test_reads_to_end():
    proc = _spawn("print('a'); print('FACTORY_RESULT:{}')")
    lines = []
    cancelled, timed_out = output_pump.follow(proc, time.time() + 30, lambda: False, lines.append, proc.kill, tick=0.1)
    assert (cancelled, timed_out) == (False, False)
    assert lines == ["a\n", "FACTORY_RESULT:{}\n"]
    assert proc.returncode == 0


def test_silent_process_hits_deadline():
    # 1 行出したあと黙り込む。前の作りでは readline で待ち続けた
    proc = _spawn("import time; print('start'); time.sleep(600)")
    lines = []
    t0 = time.time()
    cancelled, timed_out = output_pump.follow(proc, time.time() + 1.0, lambda: False, lines.append, proc.kill, tick=0.1)
    assert timed_out and not cancelled
    assert time.time() - t0 < 10
    assert lines == ["start\n"]
    assert proc.poll() is not None


def test_cancel_seen_without_output():
    proc = _spawn("import time; time.sleep(600)")
    calls = []

    def should_cancel():
        calls.append(1)
        return True

    t0 = time.time()
    cancelled, timed_out = output_pump.follow(proc, time.time() + 60, should_cancel, lambda _l: None, proc.kill,
                                              tick=0.1, cancel_every=0.3)
    assert cancelled and not timed_out
    assert time.time() - t0 < 10
    assert calls


def test_output_after_eof_waits_for_exit():
    # 標準出力を閉じてからしばらく動くプロセスも、終了を待って抜ける
    proc = _spawn("import os, sys, time; print('x'); sys.stdout.flush(); os.close(1); time.sleep(0.5)")
    lines = []
    cancelled, timed_out = output_pump.follow(proc, time.time() + 30, lambda: False, lines.append, proc.kill, tick=0.1)
    assert (cancelled, timed_out) == (False, False)
    assert lines == ["x\n"]
    assert proc.returncode == 0
