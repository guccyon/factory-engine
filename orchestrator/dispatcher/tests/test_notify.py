"""通知のルーティング（ADR 0032）。設定の検査・宛先の決め方は DB なしで、写しと受信トレイの出し分けは TEST_DATABASE_URL の Postgres で確かめる。"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
os.environ.setdefault("NOTIFIER", "log")

import inbox  # noqa: E402
import notify  # noqa: E402
import notify_inbox  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
DEFS = Path(__file__).resolve().parents[3] / "definitions" / "notifications.yaml"

DOC = {
    "operated_by": "business_owner",
    "connections": {
        "provider_hook": {"party": "provider", "via": "slack_webhook", "secret": "env:HOOK_PROVIDER"},
        "owner_hook": {"party": "business_owner", "via": "slack_webhook", "secret": "env:HOOK_OWNER"},
        "owner_log": {"party": "business_owner", "via": "log"},
    },
    "destinations": {
        "provider_ops": {"connection": "provider_hook"},
        "owner_dev": {"connection": "owner_hook", "quiet": "22-7", "mentions": {"po": "<@UPO>"}},
        "owner_log": {"connection": "owner_log"},
    },
}


def test_kinds_match_definitions():
    defs = yaml.safe_load(DEFS.read_text())["kinds"]
    assert set(defs) == set(notify.KINDS)
    for k, d in defs.items():
        assert tuple(d["parties"]) == notify.KINDS[k]["parties"], k
        assert {p: tuple(v) for p, v in d["defaults"].items()} == notify.KINDS[k]["defaults"], k
        assert d["status"] == notify.KINDS[k]["status"], k


def test_parse_rejects_problems_together():
    bad = {"operated_by": "x",
           "connections": {"a": {"party": "provider", "via": "slack_webhook", "secret": "https://hooks.slack.com/services/T/B/x"},
                           "b": {"party": "business_owner", "via": "slack_app", "secret": "aws-sm:x"}},
           "destinations": {"d1": {"connection": "a", "channel": "#x"}, "d2": {"connection": "nope"}, "d3": {"connection": "b", "channel": "#gen1"}},
           "routes": {"request.inbox": [{"to": "d1"}], "report.unknown": []}}
    with pytest.raises(notify.ConfigError) as e:
        notify.parse(bad)
    text = " ".join(e.value.problems)
    for want in ("operated_by", "値そのもの", "aws-sm はまだ", "チャンネルの ID", "Webhook は URL ごと", "連携 'nope'", "出せない種類", "report.unknown"):
        assert want in text, want


def test_defaults_follow_operated_by_and_who():
    cfg = notify.parse(DOC)
    names = lambda ds: sorted(d.name for d in ds)   # noqa: E731
    assert names(notify.targets(cfg, "report.product")) == ["owner_dev", "owner_log", "provider_ops"]
    assert names(notify.targets(cfg, "report.factory_health")) == ["owner_dev", "owner_log", "provider_ops"]   # 自走でも提供元に写す
    assert names(notify.targets(cfg, "request.inbox", "po")) == ["owner_dev", "owner_log"]
    assert names(notify.targets(cfg, "request.inbox", "owner")) == ["owner_dev", "owner_log"]                  # 自走なら工場の判断も
    assert notify.targets(cfg, "request.inbox", "builder") == []                                               # 建設の判断は送らない
    proxy = notify.parse(dict(DOC, operated_by="provider"))
    assert notify.targets(proxy, "request.inbox", "owner") == []                                               # 代理操業の工場の判断は事業主に送らない
    assert names(notify.targets(proxy, "report.factory_health")) == ["provider_ops"]


def test_routes_override_with_enabled_flag():
    cfg = notify.parse(dict(DOC, routes={"report.product": [{"to": "owner_dev"}, {"to": "provider_ops", "enabled": False}, "owner_log"]}))
    assert [d.name for d in notify.targets(cfg, "report.product")] == ["owner_dev", "owner_log"]


def test_implicit_keeps_todays_channels(monkeypatch):
    monkeypatch.setenv("INBOX_NOTIFY", "github-issue,slack")
    monkeypatch.setenv("INBOX_QUIET", "22-7")
    monkeypatch.setenv("INBOX_SLACK_MENTIONS", "po=<@U1>")
    cfg = notify.implicit()
    ds = notify.targets(cfg, "request.inbox", "builder")    # 今までどおり誰の出番でも送る
    assert [d.name for d in ds] == ["github-issue", "slack"]
    assert ds[1].via == "slack_webhook" and ds[1].secret == "env:SLACK_WEBHOOK_URL" and ds[1].mention("po") == "<@U1>" and ds[1].quiet == "22-7"
    assert notify.targets(cfg, "report.product") == []     # 新しい種類は設定を書くまで送らない


def test_secret_reads_env_only(monkeypatch):
    monkeypatch.setenv("HOOK_OWNER", "https://hooks.slack.com/services/x")
    assert notify.secret("env:HOOK_OWNER") == "https://hooks.slack.com/services/x"
    with pytest.raises(ValueError):
        notify.secret("aws-sm:factory/x")


@pytest.fixture
def cur():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def names(cur, name):
    cur.execute("select payload from events where name=%s order by id", (name,))
    return [r["payload"] for r in cur.fetchall()]


def test_refresh_copies_rejects_and_removes(cur, monkeypatch):
    monkeypatch.setenv("FACTORY_REPOS", "o/product")
    monkeypatch.delenv("NOTIFY_CONFIG_REPO", raising=False)
    assert notify.refresh(cur, lambda repo: None) == "absent" and not notify.cached(cur).explicit
    good = yaml.safe_dump(DOC)
    assert notify.refresh(cur, lambda repo: (good, "s1")) == "changed"
    assert notify.refresh(cur, lambda repo: (good, "s1")) == "same"
    [c] = names(cur, "notify.config_changed")
    assert c["repo"] == "o/product" and c["destinations"] == ["owner_dev", "owner_log", "provider_ops"]
    assert "HOOK" not in str(c["routes"]) and notify.cached(cur).explicit
    assert notify.refresh(cur, lambda repo: ("operated_by: nobody\n", "s2")) == "rejected"
    assert notify.cached(cur).operated_by == "business_owner"                    # 前に通った写しのまま
    assert notify.rejected(cur)["problems"]
    [err] = [i for i in inbox.items(cur) if i["kind"] == "error"]
    assert err["key"].startswith("error:notify-config:") and "operated_by" in err["action"]
    assert notify.refresh(cur, lambda repo: (good, "s1")) == "changed"           # 直したら写し直す
    assert notify.rejected(cur) is None
    assert notify.refresh(cur, lambda repo: None) == "removed" and not notify.cached(cur).explicit


def test_inbox_goes_only_to_owner_destinations(cur):
    cfg = notify.parse(DOC)
    T = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)   # JST 12:00
    inbox.request(cur, "review:1", "review_page", "po", "画面 1")
    inbox.request(cur, "q:1", "ready_question", "builder", "建設の問い")
    sent = []
    r = notify_inbox.tick(cur, T, sender=lambda name, b: sent.append((name, sorted(i["key"] for i, _ in b))) or True, config=cfg)
    assert sorted(sent) == [("owner_dev", ["task:review:1"]), ("owner_log", ["task:review:1"])]
    assert r["sent"] == 1 and sorted(r["channels"]) == ["owner_dev", "owner_log"]
    ns = names(cur, "inbox.notified")
    assert sorted(n["channels"][0] for n in ns) == ["owner_dev", "owner_log"] and all(n["count"] == 1 for n in ns)


def test_inbox_quiet_is_per_destination(cur):
    now = datetime.now(timezone.utc)                             # 記録の時刻は DB の now() なので、今の時刻で確かめる
    h = now.astimezone(notify.TZ).hour
    doc = yaml.safe_load(yaml.safe_dump(DOC))
    doc["destinations"]["owner_dev"]["quiet"] = f"{h}-{(h + 1) % 24}"   # 今の 1 時間だけ owner_dev は送らない
    cfg = notify.parse(doc)
    inbox.request(cur, "review:2", "review_page", "po", "画面 2")
    sent = []
    keep = lambda name, b: sent.append((name, [w for _, w in b])) or True   # noqa: E731
    notify_inbox.tick(cur, now, sender=keep, config=cfg)
    assert sent == [("owner_log", ["new"])]
    later = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1, minutes=5)
    notify_inbox.tick(cur, later, sender=keep, config=cfg)
    assert sent[1:] == [("owner_dev", ["new"])]                  # 明けたら、送れなかった宛先にだけ初回として送る


def test_owner_link_drops_console_url(monkeypatch):
    monkeypatch.setattr(notify_inbox, "CONSOLE_URL", "http://localhost:8790")
    cfg = notify.parse(DOC)
    it = {"key": "choice:1", "kind": "choice", "who": "po", "title": "案の選択", "url": "/#choice-1", "age_sec": 60, "action": "選ぶ"}
    text = notify_inbox.render([(it, "new")], "slack_webhook", cfg.destinations["owner_dev"])
    assert "localhost" not in text and "<@UPO>" in text and "プロダクトオーナー" in text


def test_template_passes():
    doc = yaml.safe_load((Path(__file__).resolve().parents[3] / "templates" / "notifications.example.yaml").read_text())
    cfg = notify.parse(doc)
    assert [d.name for d in notify.targets(cfg, "request.inbox", "po")] == ["owner_dev"]


class FakeSlack:
    """Slack の Web API の代わり（files の 3 手順と chat.postMessage）。受けた呼び出しを残す"""

    def __init__(self, fail=None):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        self.calls, fake = [], self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.calls.append(("GET", self.path, self.headers.get("Authorization"), b""))
                self._reply({"ok": True, "upload_url": f"http://127.0.0.1:{fake.port}/upload/F1", "file_id": "F1"})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.calls.append(("POST", self.path, self.headers.get("Authorization"), body))
                if self.path.startswith("/upload/"):
                    self.send_response(200)
                    self.end_headers()
                    return
                self._reply({"ok": False, "error": fail} if fail else {"ok": True})

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


APP = {"operated_by": "business_owner",
       "connections": {"app": {"party": "business_owner", "via": "slack_app", "secret": "env:SLACK_BOT_TOKEN_TEST"}},
       "destinations": {"gen1_reports": {"connection": "app", "channel": "C0123ABCD"}}}


def test_slack_app_uploads_attachment_and_posts_text(monkeypatch):
    fake = FakeSlack()
    monkeypatch.setattr(notify, "SLACK_API", f"http://127.0.0.1:{fake.port}")
    monkeypatch.setenv("SLACK_BOT_TOKEN_TEST", "xoxb-test")
    d = notify.parse(APP).destinations["gen1_reports"]
    try:
        assert notify.post(d, "*見出し*\n• 1 行目", notify.Attachment("factory-health-2026-11-05.html", b"<html>x</html>", "工場の健康状態"))
        paths = [c[1].split("?")[0] for c in fake.calls]
        assert paths == ["/files.getUploadURLExternal", "/upload/F1", "/files.completeUploadExternal"]
        assert all(c[2] == "Bearer xoxb-test" for c in fake.calls if not c[1].startswith("/upload/"))
        assert "filename=factory-health-2026-11-05.html" in fake.calls[0][1] and "length=14" in fake.calls[0][1]
        assert fake.calls[1][3] == b"<html>x</html>"
        done = json.loads(fake.calls[2][3])
        assert done == {"files": [{"id": "F1", "title": "工場の健康状態"}], "channel_id": "C0123ABCD", "initial_comment": "*見出し*\n• 1 行目"}
        fake.calls.clear()
        assert notify.post(d, "受信トレイ")                                   # 添付がなければ本文だけ
        assert [c[1] for c in fake.calls] == ["/chat.postMessage"] and json.loads(fake.calls[0][3])["channel"] == "C0123ABCD"
    finally:
        fake.close()


def test_slack_app_fails_on_ok_false_and_skips_without_token(monkeypatch):
    fake = FakeSlack(fail="not_in_channel")
    monkeypatch.setattr(notify, "SLACK_API", f"http://127.0.0.1:{fake.port}")
    d = notify.parse(APP).destinations["gen1_reports"]
    try:
        monkeypatch.delenv("SLACK_BOT_TOKEN_TEST", raising=False)
        assert notify.post(d, "x") is False and fake.calls == []             # トークンがなければ送らない
        monkeypatch.setenv("SLACK_BOT_TOKEN_TEST", "xoxb-test")
        with pytest.raises(RuntimeError, match="not_in_channel"):
            notify.post(d, "x")
    finally:
        fake.close()
