"""ジョブの打ち切りの時刻を、出力が止まっていても守る（2026-11-13 rally #1438・#1454）。

run_job はワーカーの標準出力を readline で読みながら締め切りを見ていた。ワーカーが何も出さなくなると readline が戻らず、
締め切りを見る行に届かない。ジョブ 8765・8770 は打ち切り 40 分のところを約 7 時間ワーカーの枠を握り、番人は stall:・stuck: の
停止を開くだけで止めなかった。締め切りの時刻にコンテナを止める時計を別に置き、止まれば出力が閉じて readline が戻る。
"""
from __future__ import annotations

import subprocess
import threading
import time


def kill_container(name: str) -> None:
    subprocess.run(["docker", "kill", name], capture_output=True)


def arm(name: str, deadline: float, kill=kill_container) -> threading.Timer:
    """締め切り（epoch 秒）にコンテナ name を止める時計を起こす。ジョブが先に終わったら cancel() する"""
    t = threading.Timer(max(0.0, deadline - time.time()), kill, args=(name,))
    t.daemon = True
    t.start()
    return t
