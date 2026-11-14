"""繰り返すエラーを束ねて 1 件の停止にする（factory-engine#308）。

- fault: log() の引数から握りつぶした例外を数え、まとめて factory.fault に書く（DB なしで見る部分もある）
- repeat_watch: 同じ例外・同じ種類のジョブの同じ失敗が続いたら repeat: の停止を 1 件開き、出なくなったら閉じる

TEST_DATABASE_URL の Postgres で確かめる（なければ DB の部分は飛ばす）。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
os.environ.setdefault("NOTIFIER", "log")

import fault  # noqa: E402
import repeat_watch  # noqa: E402
import stoppage  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")


def test_parse_only_swallowed_exceptions():
    assert fault.parse(("notify report failed", "IndexError(2)")) == ("notify report failed", "IndexError", "IndexError(2)")
    assert fault.parse(("ci rerun failed", "https://x/pull/1", "ci", "<HTTPError 404: 'Not Found'>"))[1] == "HTTPError"
    assert fault.parse(("tick error", "psycopg.errors.UndefinedColumn('x')"))[1] == "UndefinedColumn"
    assert fault.parse(("supervisor up: repos=[]",)) is None             # ふつうのログ
    assert fault.parse(("job 3 failed", "exit=1")) is None                # 例外の repr がない
    # 印は「どんなエラーか」。回ごとに変わる数・引用した値は落とす。工程が違っても同じ根っこなら同じ印
    assert fault.root_signature("IndexError", "IndexError(2)") == fault.root_signature("IndexError", "IndexError(3)")
    assert fault.root_signature("KeyError", "KeyError('a')") != fault.root_signature("IndexError", "IndexError(2)")
    assert fault.root_signature("OperationalError", "OperationalError('could not translate host name \"postgres\"')") == fault.ENV_SIGNATURE
    for s in ("OperationalError('connection failed: connection to server at \"172.18.0.2\", port 5432 failed: timeout expired')",
              "OperationalError('the connection is closed')", "URLError(TimeoutError('The read operation timed out'))",
              "<urlopen error [Errno 61] Connection refused>", "OperationalError('consuming input failed: server closed the connection unexpectedly')"):
        assert fault.root_signature(s.lstrip("<").split("(")[0].split(" ")[0], s) == fault.ENV_SIGNATURE, s


@pytest.fixture
def cur(monkeypatch):
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    import psycopg
    from psycopg.rows import dict_row

    monkeypatch.setattr(stoppage, "notify", lambda kind, s, cur=None: None)
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")

    with fault._lock:
        fault._counts.clear()
    yield c
    conn.close()


def _age(cur, name, minutes):
    cur.execute("update events set occurred_at = now() - make_interval(mins => %s) where name=%s", (minutes, name))


def test_fault_flush_bundles_counts(cur):
    """55 回の同じ例外は 1 行で書かれる。足りなければ開かず、5 回を超えたら 1 件だけ開く"""
    for _ in range(3):
        fault.note_log("supervisor", ("notify report failed", "IndexError(2)"))
    assert fault.flush(cur, force=True) == 1
    assert repeat_watch.step(cur) == []   # 3 回はまだ
    for _ in range(52):
        fault.note_log("supervisor", ("notify report failed", "IndexError(2)"))
    fault.flush(cur, force=True)
    cur.execute("select count(*) as n from events where name='factory.fault'")
    assert cur.fetchone()["n"] == 2
    opened = repeat_watch.step(cur)
    assert len(opened) == 1 and opened[0].startswith("repeat:IndexError:")
    cur.execute("select payload from events where name='stoppage.opened'")
    p = cur.fetchone()["payload"]
    assert p["category"] == "factory-bug" and "55 回" in p["what"] and p["cause"] == "IndexError(2)"
    assert repeat_watch.step(cur) == []   # 重ねて開かない
    # 出なくなって QUIET_MIN を過ぎたら閉じる
    _age(cur, "factory.fault", repeat_watch.QUIET_MIN + 5)
    repeat_watch.step(cur)
    assert stoppage.open_keys(cur, "repeat:") == []


def _failed_job(cur, kind, error, minutes_ago=0):
    from psycopg.types.json import Jsonb
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state) values ('c', 'o/r', -1, %s, 'failed') returning id", (kind,))
    jid = cur.fetchone()["id"]
    cur.execute("insert into events(name, job_id, payload, occurred_at) values ('job.finished', %s, %s, now() - make_interval(mins => %s))",
                (jid, Jsonb({"state": "failed", "error": error}), minutes_ago))
    return jid


def _classify(p):   # dispatcher.classify_failure の代わり（自動で回復するものは None）
    return None if p.get("error") == "lock_held" else ("factory-bug", "x", "y")


def test_job_failures_bundle_and_quiet_members(cur):
    """同じ種類・同じ失敗のジョブが 3 件で 1 件の束ねた停止。種類や失敗が違うもの・自動で回復するものは数えない"""
    _failed_job(cur, "investigate", "exit=125")
    _failed_job(cur, "investigate", "exit=125")
    _failed_job(cur, "investigate", "exit=1")
    _failed_job(cur, "judge", "exit=125")
    for _ in range(3):
        _failed_job(cur, "implement", "lock_held")
    assert repeat_watch.job_step(cur, _classify) == []
    third = _failed_job(cur, "investigate", "exit=125")
    opened = repeat_watch.job_step(cur, _classify)
    assert len(opened) == 1 and opened[0].startswith("repeat:job:investigate:exit=125#")
    cur.execute("select payload, job_id from events where name='stoppage.opened'")
    r = cur.fetchone()
    assert "3 件" in r["payload"]["what"] and r["job_id"] == third
    assert repeat_watch.bundled_job_signatures(cur) == {"investigate:exit=125"}
    assert repeat_watch.job_signature("investigate", "exit=125") in repeat_watch.bundled_job_signatures(cur)
    # 出なくなったら閉じる
    _age(cur, "job.finished", repeat_watch.QUIET_MIN + 5)
    repeat_watch.job_step(cur, _classify)
    assert stoppage.open_keys(cur, "repeat:") == []


def test_inbox_shows_repeat_to_builder(cur):
    import inbox
    for _ in range(3):
        _failed_job(cur, "investigate", "exit=125")
    repeat_watch.job_step(cur, _classify)
    items = [i for i in inbox._stoppages(cur, datetime.now(timezone.utc)) if i["key"].startswith("stoppage:repeat:")]
    assert len(items) == 1 and items[0]["who"] == "builder"
    assert "繰り返し" in items[0]["title"]


def test_env_failures_bundle_into_one(cur):
    """名前解決・接続の失敗は工程ごとに分けず、工場の環境の不調 1 件にまとめる（11-14 Docker の VM の詰まりで 7 件に分かれた）"""
    for comp, label in (("supervisor", "tick error"), ("dispatcher", "watch error"), ("dispatcher", "ops error")):
        for _ in range(2):
            fault.note_log(comp, (label, "OperationalError('could not translate host name \"postgres\" to address: Temporary failure in name resolution')"))
    fault.note_log("dispatcher", ("capture statuses failed", "URLError(gaierror(-3, 'Temporary failure in name resolution'))"))
    fault.note_log("supervisor", ("notify report failed", "IndexError(2)"))   # 別の原因は別の印のまま
    fault.flush(cur, force=True)
    opened = repeat_watch.step(cur)
    assert len(opened) == 1 and opened[0].startswith("repeat:env:network#")
    cur.execute("select payload from events where name='stoppage.opened'")
    p = cur.fetchone()["payload"]
    assert p["category"] == "env" and "7 回" in p["what"] and "環境" in p["what"]



def test_same_root_from_different_steps_is_one_and_old_keys_merge(cur):
    """同じ根っこのエラーは工程が違っても 1 件。前の版で工程ごとに開いていた件は、新しい 1 件に寄せて閉じる（11-14 オーナー）"""
    from psycopg.types.json import Jsonb
    # 前の版の印（工程ごと）で開いていた 2 件
    for old in ("supervisor:tick_error:OperationalError", "dispatcher:watch_error:OperationalError"):
        cur.execute("insert into events(name, payload) values ('factory.fault', %s)",
                    (Jsonb({"signature": old, "component": old.split(":")[0], "label": "x", "exc": "OperationalError", "count": 6,
                            "sample": "OperationalError('could not translate host name \"postgres\" to address')", "first_at": 1.0, "last_at": 2.0}),))
        stoppage.open_stoppage(cur, f"repeat:{old}#20261114T0116", "factory-bug", "old")
    for comp in ("supervisor", "dispatcher"):
        fault.note_log(comp, ("ops error", "KeyError('job_7')"))
        fault.note_log(comp, ("watch error", "KeyError('job_8')"))
        fault.note_log(comp, ("tick error", "KeyError('job_9')"))
    fault.flush(cur, force=True)
    repeat_watch.step(cur)
    keys = sorted(stoppage.open_keys(cur, "repeat:"))
    assert len(keys) == 2   # 名前解決の 1 件と KeyError の 1 件
    assert any(k.startswith("repeat:env:network#") for k in keys) and any(k.startswith("repeat:KeyError:") for k in keys)
    cur.execute("select payload->>'remedy' as r from events where name='stoppage.closed'")
    assert [r["r"] for r in cur.fetchall()].count(None) == 0
    import inbox
    from datetime import datetime, timezone
    items = [i for i in inbox._stoppages(cur, datetime.now(timezone.utc)) if i["key"].startswith("stoppage:repeat:KeyError")]
    assert len(items) == 1 and "6 回" in items[0]["title"]   # 最新の回数で見せる


def test_repeat_is_a_notice_that_can_be_dismissed(cur):
    """工場の中の不調は判断待ちでなくお知らせ。人の出番に数えず、見たら消せる（受信トレイとコンソールの両方から）。11-14 オーナー"""
    import alerts
    import inbox
    for _ in range(6):
        fault.note_log("dispatcher", ("watch error", "OperationalError('could not translate host name \"postgres\" to address')"))
    fault.flush(cur, force=True)
    [key] = repeat_watch.step(cur)
    v = inbox.view(cur)
    [it] = [i for i in v["items"] if i["key"] == f"stoppage:{key}"]
    assert it["kind"] == "notice" and v["kinds"]["notice"] == "お知らせ"
    assert it["answer"]["type"] == "ack" and v["count"] == 0   # 人の出番に数えない
    assert inbox.answer(cur, f"stoppage:{key}", "seen", "owner", "console")[0] == "answered"
    assert not [i for i in inbox.items(cur) if i["key"] == f"stoppage:{key}"]
    assert key in alerts.dismissed(cur)                        # コンソールの「工場の不調」からも消える
    assert stoppage.open_keys(cur, "repeat:") == [key]         # 停止は開いたまま（収まれば番人が閉じる）


def test_console_folds_job_stoppages_into_the_bundle(cur):
    """束ねた停止が開いている同じ失敗のジョブは、「工場の不調」「作業の失敗」に 1 行ずつ出さず、束ねた 1 件の中に数える"""
    import alerts
    ids = [_failed_job(cur, "investigate", "exit=125") for _ in range(3)]
    other = _failed_job(cur, "judge", "exit=1")
    for j in ids + [other]:
        stoppage.open_stoppage(cur, f"job:{j}", "factory-bug", "失敗", job_id=j)
    [bkey] = repeat_watch.job_step(cur, _classify)
    keys = [a["key"] for a in alerts.view(cur)["open"]]
    assert bkey in keys and f"job:{other}" in keys
    assert not any(f"job:{j}" in keys for j in ids)
    [b] = [a for a in alerts.view(cur)["open"] if a["key"] == bkey]
    assert any(d["label"] == "まとめたジョブの停止" and "3 件" in d["text"] for d in b["detail"])
