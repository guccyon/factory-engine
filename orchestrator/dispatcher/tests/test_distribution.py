"""審査前の配布の工場の側（ADR 0047）: 依頼・拾う・配布先を events に書き、Mac ランナーの道具を呼んで待つ。ランナーは差し替える。
  TEST_DATABASE_URL=postgresql://factory@localhost:5432/factory_test python -m pytest orchestrator/dispatcher/tests/test_distribution.py
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    import psycopg
    from psycopg.rows import dict_row

    import console
    import distribution

APP = "guccyon/rally:apps/student-app"
QUIET = {"sleep": lambda s: None, "log": lambda m: None}


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def runner(states):
    """preview_distribute で b-1 を返し、build_status では states を順に返す。呼ばれた (道具, 引数, runner) を残す。"""
    seen = []

    def call(tool, args, runner="any", timeout=120):
        seen.append((tool, args, runner))
        if tool == "preview_distribute":
            return {"runner_id": "r-mac", "content": [{"type": "text", "text": json.dumps({"build_id": "b-1", "state": "queued"})}], "isError": False}
        return {"runner_id": "r-mac", "content": [{"type": "text", "text": json.dumps(states.pop(0))}], "isError": False}
    return call, seen


DONE = {"state": "distributed", "stage": "done", "sha": "abc", "build_number": "29420000", "sha256": "f" * 64, "testers": "testers"}


def names(cur):
    cur.execute("select name from events order by id")
    return [r["name"] for r in cur.fetchall()]


def test_factoryctl_distributes_and_records(cur):
    call, seen = runner([{"state": "waiting_for_human", "stage": "attend"}, DONE])
    ok, p = distribution.distribute(cur, notes="直した", defines={"RALLY_API_URL": "https://s"}, call=call, **QUIET)
    assert ok and p["sha"] == "abc" and p["runner"] == "r-mac"
    assert names(cur) == ["ship.preview_requested", "ship.preview_started", "ship.preview_distributed"]
    assert seen[0] == ("preview_distribute", {"app": APP, "channel": "firebase_android", "ref": "main",
                                              "release_notes": "直した", "dart_defines": {"RALLY_API_URL": "https://s"}}, "any")
    assert all(r == "r-mac" for _, _, r in seen[1:])   # 状態は同じ Mac に聞く
    assert distribution.tick(cur, call=call, **QUIET) == 0   # 終わったものは拾い直さない


def test_console_request_is_picked_up_once_by_dispatcher(cur):
    """コンソールのボタンは依頼を書くだけ。dispatcher の tick が 1 回だけ拾って動かす。配布中は次の依頼を断る"""
    code, body = console.act(cur, "/api/distribute", {"app": APP, "notes": "ボタンから"})
    assert code == 200
    code, body2 = console.act(cur, "/api/distribute", {"app": APP})
    assert code == 409 and "まだ終わっていない" in body2["error"]
    call, seen = runner([DONE])
    assert distribution.tick(cur, call=call, **QUIET) == 1
    assert distribution.tick(cur, call=call, **QUIET) == 0
    assert [t for t, _, _ in seen] == ["preview_distribute", "build_status"]
    r = distribution.recent(cur, APP)[0]
    assert r["result"] == "ship.preview_distributed" and r["req"]["via"] == "console"


def test_destination_set_in_console_is_passed_to_runner(cur):
    assert console.act(cur, "/api/distribute/target", {"app": APP, "firebase_app_id": "1:123:android:ab", "groups": "testers, qa"})[0] == 200
    assert console.act(cur, "/api/distribute/target", {"app": APP, "firebase_app_id": "1:123:ios:ab"})[0] == 400
    call, seen = runner([DONE])
    distribution.distribute(cur, call=call, **QUIET)
    assert seen[0][1]["firebase_app_id"] == "1:123:android:ab" and seen[0][1]["groups"] == "testers,qa"
    # 空にすると工場の宛先を外し、Mac の設定に戻す
    console.act(cur, "/api/distribute/target", {"app": APP, "firebase_app_id": ""})
    call, seen = runner([DONE])
    distribution.distribute(cur, call=call, **QUIET)
    assert "firebase_app_id" not in seen[0][1]


def test_human_absent_is_failed_at_attend(cur):
    call, _ = runner([{"state": "failed", "stage": "attend", "error": "300 秒のあいだ Mac の前で押されなかった"}])
    ok, p = distribution.distribute(cur, call=call, **QUIET)
    assert not ok and p["stage"] == "attend" and names(cur)[-1] == "ship.preview_failed"


def test_refused_by_runner_is_failed_at_request(cur):
    def call(tool, args, runner="any", timeout=120):
        return {"content": [{"type": "text", "text": "配布先 firebase_android は開いていない"}], "isError": True}
    ok, p = distribution.distribute(cur, call=call, **QUIET)
    assert not ok and p["stage"] == "request" and "開いていない" in p["error"]


def test_unknown_channel_or_app_is_not_requested(cur):
    assert not distribution.distribute(cur, channel="testflight_ios", call=None, **QUIET)[0]
    assert not distribution.request(cur, app="evil/repo:x")[0]
    assert names(cur) == []


def test_lost_request_is_failed(cur):
    ok, req = distribution.request(cur, via="console")
    distribution.claim(cur, req, "dispatcher")
    cur.execute("update events set occurred_at = now() - interval '3 hours' where name='ship.preview_started'")
    assert distribution.tick(cur, call=None, **QUIET) == 0
    assert names(cur)[-1] == "ship.preview_failed" and distribution.recent(cur)[0]["res"]["stage"] == "lost"


def test_console_shows_preview_target(cur):
    cur.execute("insert into events(name, payload) values ('release.created', '{\"repo\": \"guccyon/rally\", \"release_id\": \"guccyon/rally@0.1.0\"}')")
    distribution.request(cur, via="console")
    rel = [r for r in console.releases(cur) if r["repo"] == "guccyon/rally"][0]
    pv = [t for t in rel["targets"] if t["kind"] == "preview"][0]
    assert pv["app"] == APP and pv["busy"] and pv["items"][0]["state"] == "queued" and pv["target"] is None
