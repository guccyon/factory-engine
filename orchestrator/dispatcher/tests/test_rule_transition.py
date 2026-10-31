"""overlay のルールの状態の移り変わり（ADR 0013 の 6）: shadow → adopted の昇格と、カナリアで adopted → shadow に戻すこと。
判定は supervisor が judge の裁きの記録から計算し、state の 1 行を書き換える Issue を起票する。GitHub は差し替える。"""
import base64
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import learn
    import supervisor

NOW = datetime.now(timezone.utc)
REPO = "o/r"


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def judged(cur, rid, verdict, days_ago=1):
    p = {"stage": "judged", "category": "inspection", "verdict": verdict, "pr_url": "https://github.com/o/r/pull/1",
         "defect": {"rubric_id": rid, "shadow": True}}
    cur.execute("insert into artifacts(type, correlation_id, created_at, payload) values ('Judgment', 'github:o/r#1', now() - %s * interval '1 day', %s)",
                (days_ago, Jsonb(p)))


def stats(fires, adopt, reject, escalate=0):
    n = adopt + reject
    return {"id": "OV-001", "fires": fires, "adopt": adopt, "reject": reject, "escalate": escalate, "precision": adopt / n if n else None}


def test_promotes_shadow_rule_with_enough_fires_and_precision():
    since = NOW - timedelta(days=4)
    assert learn.transition("shadow", since, stats(5, 4, 1), NOW)["to"] == "adopted"
    assert learn.transition("shadow", since, stats(4, 4, 0), NOW) is None            # 発火が足りない
    assert learn.transition("shadow", since, stats(6, 4, 2), NOW) is None            # 的中率 0.67
    assert learn.transition("shadow", NOW - timedelta(days=2), stats(9, 9, 0), NOW) is None   # shadow で 3 日たっていない
    assert learn.transition("shadow", since, None, NOW) is None


def test_canary_returns_adopted_rule_with_false_positives():
    since = NOW - timedelta(days=2)
    assert learn.transition("adopted", since, stats(4, 2, 2), NOW)["to"] == "shadow"
    assert learn.transition("adopted", since, stats(2, 0, 2), NOW) is None           # 裁かれた件数が少ない
    assert learn.transition("adopted", NOW - timedelta(days=8), stats(4, 1, 3), NOW) is None   # カナリアの期間を過ぎた
    assert learn.transition("retired", since, stats(9, 9, 0), NOW) is None


class FakeGH:
    def __init__(self, state, since):
        self.calls, self.state, self.since = [], state, since

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/contents/.factory-config/overlay/rules"):
            return [{"name": ".gitkeep", "path": ".factory-config/overlay/rules/.gitkeep"},
                    {"name": "OV-001.yaml", "path": ".factory-config/overlay/rules/OV-001.yaml"}]
        if "/contents/" in path:
            return {"content": base64.b64encode(f"id: OV-001\nstate: {self.state}\n".encode()).decode()}
        if "/commits?" in path:
            return [{"commit": {"committer": {"date": self.since.strftime("%Y-%m-%dT%H:%M:%SZ")}}}]
        if method == "POST" and path.endswith("/issues"):
            return {"number": 500, "html_url": "https://github.com/o/r/issues/500"}
        return {}


@pytest.fixture()
def sup(monkeypatch):
    monkeypatch.setattr(supervisor, "REPOS", [REPO])
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    monkeypatch.setattr(supervisor, "LEARN_RULE_TRANSITIONS", True)

    def make(state, since):
        gh = FakeGH(state, since)
        monkeypatch.setattr(supervisor, "gh", gh)
        monkeypatch.setattr(supervisor, "_transitions_checked_at", 0.0)
        return gh
    return make


def test_supervisor_files_promotion_issue_once(cur, sup):
    since = NOW - timedelta(days=5)
    for v in ["adopt"] * 5 + ["reject"]:
        judged(cur, "OV-001", v)
    judged(cur, "OV-001", "reject", days_ago=10)   # shadow に入る前の裁きは数えない
    gh = sup("shadow", since)
    supervisor.rule_transition_step(cur, NOW)
    posts = [c for c in gh.calls if c[0] == "POST"]
    assert len(posts) == 1
    body = posts[0][2]
    assert body["title"] == "OV-001 を採用する" and "factory:planned" in body["labels"]
    assert "`state: shadow` を `state: adopted`" in body["body"]
    cur.execute("select payload from events where name='learn.rule_transition_filed'")
    p = cur.fetchone()["payload"]
    assert (p["rule"], p["from"], p["to"], p["stats"]["fires"]) == ("OV-001", "shadow", "adopted", 6)
    gh = sup("shadow", since)   # 次の周（Issue がまだ片付いていない）
    supervisor.rule_transition_step(cur, NOW)
    assert not [c for c in gh.calls if c[0] == "POST"]


def test_supervisor_does_nothing_without_enough_record(cur, sup):
    for v in ["adopt"] * 3:
        judged(cur, "OV-001", v)
    gh = sup("shadow", NOW - timedelta(days=5))
    supervisor.rule_transition_step(cur, NOW)
    assert not [c for c in gh.calls if c[0] == "POST"]
