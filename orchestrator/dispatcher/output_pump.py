"""ジョブの出力を読みながら、打ち切りの時刻と止める印を見る（2026-11-14）。

前は本体のループで `proc.stdout.readline()` を直接呼んでいた。ワーカーの出力が止まると readline が返らず、
打ち切りの時刻（JOB_TIMEOUT_SEC）も止める印も見ないまま待ち続けた。rally #1438（job 8770）と #1454（job 8765）は
40 分の上限で約 7 時間ワーカーを握り、23:38 UTC にまとめて timeout で落ちた。

出力は別のスレッドで読んでキューに積み、本体は短い間隔でキューを見る。出力がなくても時刻と印は見られる。
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable, IO

_EOF = object()


def _reader(stream: IO[str], q: "queue.Queue[object]") -> None:
    try:
        for line in iter(stream.readline, ""):
            q.put(line)
    except (ValueError, OSError):   # 読んでいる途中で閉じられた
        pass
    finally:
        q.put(_EOF)


def follow(proc, deadline: float, should_cancel: Callable[[], bool], on_line: Callable[[str], None],
           kill: Callable[[], None], *, tick: float = 2.0, cancel_every: float = 10.0,
           clock: Callable[[], float] = time.time) -> tuple[bool, bool]:
    """proc の標準出力を最後まで（または打ち切りまで）読む。

    戻り値は (cancelled, timed_out)。打ち切り・止める印のときは kill() を呼び、proc の終了を少し待つ。
    should_cancel は cancel_every 秒に 1 回だけ呼ぶ（DB を叩きすぎない）。
    """
    q: "queue.Queue[object]" = queue.Queue()
    t = threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True)
    t.start()
    next_cancel_check = clock() + cancel_every
    eof = False
    while True:
        try:
            item = q.get(timeout=tick)
        except queue.Empty:
            item = None
        if item is _EOF:
            eof = True
        elif item is not None:
            on_line(item)   # type: ignore[arg-type]
        if eof and proc.poll() is not None:
            return False, False
        now = clock()
        cancelled = False
        if now >= next_cancel_check:
            next_cancel_check = now + cancel_every
            cancelled = bool(should_cancel())
        timed_out = now > deadline
        if cancelled or timed_out:
            kill()
            try:
                proc.wait(timeout=30)
            except Exception:  # noqa: BLE001  終了を待てなくても記録は進める
                pass
            # kill の後に残った出力を拾う（FACTORY_RESULT が最後に出ていることがある）
            t.join(timeout=5)
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if item is not _EOF:
                    on_line(item)   # type: ignore[arg-type]
            return cancelled, timed_out and not cancelled
