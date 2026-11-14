"""db_value が、引数のない測り方の SQL を % の置き換えなしで渡すこと（2026-11-13 の日報で mac_runner が「不明（ProgrammingError）」）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import status_report  # noqa: E402


class Cur:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args):
        self.log.append((sql, args))

    def fetchone(self):
        return (3,)


class Conn:
    def __init__(self):
        self.log = []

    def cursor(self):
        return Cur(self.log)


def test_no_args_passes_none():
    c = Conn()
    assert status_report.db_value(c, "select count(*) from events where payload->>'tool' like 'ui\\_%'") == 3
    assert c.log[0][1] is None


def test_args_are_passed():
    c = Conn()
    status_report.db_value(c, "select %s", ("x",))
    assert c.log[0][1] == ("x",)
