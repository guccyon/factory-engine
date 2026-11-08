"""操業コンソールの停止アラート（alerts.py、2026-11-08）: 記録の文を読む文にすること、どのチケットのことかを添えること、
画面から消して戻せること、失敗した作業をコンソールからやり直せること。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）
"""
import os
import sys

import pytest

pytest.importorskip("psycopg")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture()
def cur(monkeypatch):
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    os.environ.setdefault("DATABASE_URL", URL)
    import psycopg
    from psycopg.rows import dict_row
    import stoppage
    monkeypatch.setattr(stoppage, "notify", lambda *a, **k: None)
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def _failed_job(cur, n: int, error: str = "exit=1") -> int:
    cur.execute("insert into tickets(source, source_ref, repo, title, url, state) values ('github', %s, 'o/r', %s, %s, 'active') "
                "on conflict (source, source_ref) do update set title=excluded.title returning id",
                (f"o/r#{n}", f"チケット {n}", f"https://github.com/o/r/issues/{n}"))
    tid = cur.fetchone()["id"]
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, error, ticket_id) values (%s,'o/r',%s,'implement','failed',%s,%s) returning id",
                (f"github:o/r#{n}", n, error, tid))
    jid = cur.fetchone()["id"]
    import stoppage
    stoppage.open_stoppage(cur, f"job:{jid}", "factory-bug", "ジョブが失敗した（未分類。人が分類を直す）", waiting_for="原因の調査と rerun",
                           cause=error, correlation_id=f"github:o/r#{n}", job_id=jid)
    return jid


def test_failed_jobs_name_their_ticket_and_offer_rerun(cur):
    import alerts
    a1, a2 = _failed_job(cur, 5), _failed_job(cur, 6)
    v = alerts.view(cur)
    assert [a["key"] for a in v["open"]] == [f"job:{a1}", f"job:{a2}"]
    a = v["open"][0]
    assert a["group"] == "作業の失敗" and "実装" in a["title"] and "rerun" not in a["todo"]
    assert a["target"] == {"label": "チケット 5（o/r#5）", "url": "https://github.com/o/r/issues/5"}   # 同じ文が並んでも、どれのことかがわかる
    assert a["actions"] == [{"type": "rerun", "job_id": a1, "label": "やり直す"}]
    assert {"label": "作業の記録（最後の部分）", "log": a1} in a["links"]
    assert a["record"]["waiting_for"] == "原因の調査と rerun"   # 元の記録の文も詳しく見る人のために残す


def test_incomplete_offers_continue(cur):
    import alerts
    jid = _failed_job(cur, 7, "incomplete(claude_exit=1)")
    a = alerts.view(cur)["open"][0]
    assert "途中で終わった" in a["title"] and a["actions"][0] == {"type": "rerun", "job_id": jid, "label": "続きからやり直す", "continue": True}


def test_swap_is_plain_and_uses_the_word_update(cur):
    import alerts
    import stoppage
    from psycopg.types.json import Jsonb
    cur.execute("insert into events(name, payload) values ('swap.requested', %s) returning id", (Jsonb({"target": "c1bb3591234"}),))
    sid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, payload) values ('swap.escalated', %s, %s)",
                (f"swap:{sid}", Jsonb({"category": "human", "short": "c1bb359", "escalations": [
                    {"id": "secrets_permissions", "why": "秘密情報・権限（Docker のソケット…）を含む", "files": ["orchestrator/.env.example"]}]})))
    stoppage.open_stoppage(cur, f"swap:{sid}", "human", f"工場の入れ替え {sid}（c1bb359）: 人に回す変更を含む: 秘密情報・権限（Docker のソケット）",
                           waiting_for="差分を確かめ、人が手で入れ替える", correlation_id=f"swap:{sid}")
    a = alerts.view(cur)["open"][0]
    shown = a["title"] + a["todo"] + "".join(d["text"] for d in a["detail"])
    assert "入れ替え" not in shown and "ソケット" not in shown and "更新" in a["title"]
    assert "工場の守り" in a["detail"][0]["text"]
    import inbox   # 受信トレイの「対応が必要なもの」も同じ読む文
    it = next(i for i in inbox.items(cur) if i["key"] == f"stoppage:swap:{sid}")
    assert "入れ替え" not in it["title"] + it["action"]


def test_dismiss_and_restore(cur):
    import alerts
    jid = _failed_job(cur, 8)
    assert alerts.set_dismissed(cur, f"job:{jid}", True, "console:t")
    v = alerts.view(cur)
    assert v["open"] == [] and [a["key"] for a in v["dismissed"]] == [f"job:{jid}"]
    assert alerts.set_dismissed(cur, f"job:{jid}", False, "console:t")
    assert [a["key"] for a in alerts.view(cur)["open"]] == [f"job:{jid}"]
    assert not alerts.set_dismissed(cur, "job:999999", True, "console:t")


def test_console_rerun_and_dismiss_api(cur):
    import console
    jid = _failed_job(cur, 9)
    code, out = console.act(cur, f"/api/jobs/{jid}/rerun", {})
    assert code == 200 and out["job_id"]
    cur.execute("select attempt, enqueued_by from jobs where id=%s", (out["job_id"],))
    assert dict(cur.fetchone()) == {"attempt": 2, "enqueued_by": "rerun"}
    code, out = console.act(cur, f"/api/jobs/{jid}/rerun", {})   # 動いている作業があれば積まない
    assert code == 409
    assert console.act(cur, "/api/alerts/dismiss", {"key": f"job:{jid}"})[0] == 200
    assert console.act(cur, "/api/alerts/dismiss", {"key": "nope"})[0] == 404


def test_log_masks_secrets():
    import console
    s = console.SECRET_RE.sub("［伏せた］", "token=abcdefghijklmn ghp_" + "a" * 36 + " ok")
    assert "abcdefghijklmn" not in s and "ghp_" not in s and s.endswith(" ok")
