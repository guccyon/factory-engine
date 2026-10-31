"""実行中のジョブのコンテナにつなぎ直す（ADR 0018）。

ワーカーのコンテナは dispatcher から独立して動く（--rm なし、名前は factory-job-<id>、ラベル factory.job=<id>）。
dispatcher を上げ直したら、running のジョブのコンテナの出力を最初から読み直し、終了コードを待って、いつもの記録に戻す。
Attached は run_job が使う subprocess.Popen の代わり（stdout・poll()・wait()・returncode だけ）。
"""
from __future__ import annotations

import subprocess

JOB_LABEL = "factory.job"


def exists(name: str) -> bool:
    return subprocess.run(["docker", "container", "inspect", name], capture_output=True).returncode == 0


def remove(name: str):
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def job_containers() -> dict[int, str]:
    """工場のジョブのコンテナ（動いているものも終わったものも）。{job_id: name}"""
    r = subprocess.run(["docker", "ps", "-a", "--filter", f"label={JOB_LABEL}", "--format", f'{{{{.Label "{JOB_LABEL}"}}}} {{{{.Names}}}}'],
                       capture_output=True, text=True)
    out = {}
    for line in r.stdout.splitlines():
        jid, _, name = line.partition(" ")
        if jid.isdigit() and name:
            out[int(jid)] = name
    return out


class Attached:
    """コンテナの出力（最初から）と終了コード。docker logs -f と docker wait を並べて動かす"""

    def __init__(self, name: str):
        self.name = name
        # wait を先に始める。--rm の前の版のコンテナでも、消える前に終了コードを受け取れる
        self._wait = subprocess.Popen(["docker", "wait", name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self._logs = subprocess.Popen(["docker", "logs", "-f", name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.stdout = self._logs.stdout
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self._logs.poll() is None or self._wait.poll() is None:
            return None
        if self.returncode is None:
            out = (self._wait.stdout.read() or "").strip()
            self.returncode = int(out) if out.lstrip("-").isdigit() else -1   # 終了コードが取れない（コンテナが先に消えた）
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        self._logs.wait(timeout=timeout)
        self._wait.wait(timeout=timeout)
        return self.poll()
