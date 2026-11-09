"""審査前の配布の工場の側（ADR 0047）: 依頼と結果を events に書き、Mac ランナーの道具を呼んで待つ。ランナーと DB は差し替える。"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import distribution  # noqa: E402


class Cur:
    def __init__(self):
        self.events, self.next_id, self.row = [], 100, None

    def execute(self, sql, args):
        if sql.startswith("insert into events"):
            self.next_id += 1
            name, corr, payload = args
            self.events.append({"id": self.next_id, "name": name, "corr": corr, "payload": payload.obj})
            self.row = {"id": self.next_id}
        elif sql.startswith("update events set correlation_id"):
            next(e for e in self.events if e["id"] == args[1])["corr"] = args[0]

    def fetchone(self):
        return self.row


def runner(states):
    """preview_distribute で b-1 を返し、build_status では states を順に返す。呼ばれた (道具, 引数, runner) を残す。"""
    seen = []

    def call(tool, args, runner="any", timeout=120):
        seen.append((tool, args, runner))
        if tool == "preview_distribute":
            return {"runner_id": "r-mac", "content": [{"type": "text", "text": json.dumps({"build_id": "b-1", "state": "queued"})}], "isError": False}
        return {"runner_id": "r-mac", "content": [{"type": "text", "text": json.dumps(states.pop(0))}], "isError": False}
    return call, seen


def test_distributed_is_recorded_with_request():
    cur = Cur()
    call, seen = runner([{"state": "waiting_for_human", "stage": "attend"}, {"state": "building", "stage": "upload"},
                         {"state": "distributed", "stage": "done", "sha": "abc", "build_number": "29420000", "sha256": "f" * 64, "testers": "testers"}])
    ok, p = distribution.distribute(cur, notes="直した", defines={"RALLY_API_URL": "https://s"}, call=call, sleep=lambda s: None, log=lambda m: None)
    assert ok and p["sha"] == "abc" and p["runner"] == "r-mac" and p["build_id"] == "b-1"
    req, res = cur.events
    assert req["name"] == "ship.preview_requested" and req["corr"] == f"dist:{req['id']}" and req["payload"]["notes_chars"] == 3
    assert res["name"] == "ship.preview_distributed" and res["corr"] == req["corr"] and res["payload"]["request_id"] == req["id"]
    assert seen[0] == ("preview_distribute", {"app": distribution.DEFAULT_APP, "channel": "firebase_android", "ref": "main",
                                              "release_notes": "直した", "dart_defines": {"RALLY_API_URL": "https://s"}}, "any")
    assert all(r == "r-mac" for t, _, r in seen[1:])   # 状態は同じ Mac に聞く


def test_human_absent_is_failed_at_attend():
    cur = Cur()
    call, _ = runner([{"state": "failed", "stage": "attend", "error": "300 秒のあいだ Mac の前で押されなかった"}])
    ok, p = distribution.distribute(cur, call=call, sleep=lambda s: None, log=lambda m: None)
    assert not ok and p["stage"] == "attend"
    assert cur.events[-1]["name"] == "ship.preview_failed"


def test_refused_by_runner_is_failed_at_request():
    cur = Cur()

    def call(tool, args, runner="any", timeout=120):
        return {"content": [{"type": "text", "text": "配布先 firebase_android は開いていない"}], "isError": True}
    ok, p = distribution.distribute(cur, call=call, sleep=lambda s: None, log=lambda m: None)
    assert not ok and p["stage"] == "request" and "開いていない" in p["error"]


def test_unknown_channel_is_not_requested():
    cur = Cur()
    ok, p = distribution.distribute(cur, channel="testflight_ios", call=None, log=lambda m: None)
    assert not ok and not cur.events
