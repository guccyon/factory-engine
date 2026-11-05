"""定期レポート（ADR 0032 の決定 9）。型と文面は DB なしで、組み立て・記録・送信は TEST_DATABASE_URL の Postgres で確かめる。"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
os.environ.setdefault("NOTIFIER", "log")

import notify  # noqa: E402
import report  # noqa: E402
import report_health  # noqa: E402
import report_product  # noqa: E402
import stoppage  # noqa: E402
from report import Report, Section, entry, metric, note  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
DEFS = Path(__file__).resolve().parents[3] / "definitions" / "reports.yaml"
JST = timezone(timedelta(hours=9))
START, END = datetime(2026, 11, 4, tzinfo=JST), datetime(2026, 11, 5, tzinfo=JST)

DOC = {
    "operated_by": "business_owner",
    "connections": {"p": {"party": "provider", "via": "log"}, "o": {"party": "business_owner", "via": "log"}},
    "destinations": {"provider_ops": {"connection": "p"}, "owner_ops": {"connection": "o"}},
}


@pytest.mark.parametrize("mod", [report_health, report_product])
def test_sections_match_definitions(mod):
    d = yaml.safe_load(DEFS.read_text())["reports"][mod.KIND]
    assert d["status"] == "provisional"
    assert [(s["id"], s["ja"], tuple(s["parties"])) for s in d["sections"]] == [(k, t, p) for k, (t, p) in mod.SECTIONS.items()]


def sample() -> Report:
    return Report("report.factory_health", "工場の健康状態", START, END, [
        Section("line", "ライン", ("provider", "business_owner"), [metric("動いていた割合", 97.5, "%", -1.5), entry("開いている停止: x", "human、2.0 時間")]),
        Section("secret", "提供元だけ", ("provider",), [metric("内部の数", 3, "件"), note("メモ")]),
        Section("jobs", "ジョブ", ("provider", "business_owner"), [metric("失敗の割合", None, "%")]),
    ])


def test_roundtrip_and_render_hides_provider_sections():
    rep = sample()
    back = Report.from_dict(rep.to_dict())
    assert back == rep
    owner = report.render(rep, "slack_webhook", "business_owner", "rally")
    assert "*工場の健康状態（11/04）*" in owner and "[rally]" not in owner and "提供元だけ" not in owner
    assert "97.5%（前日比 -1.5%）" in owner and "失敗の割合: 不明" in owner and "仮置き" in owner
    prov = report.render(rep, "log", "provider", "rally")
    assert prov.startswith("**[rally] 工場の健康状態") and "内部の数: 3件" in prov


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


def ev(cur, name, payload, at):
    from psycopg.types.json import Jsonb
    cur.execute("insert into events(name, payload, occurred_at) values (%s, %s, %s)", (name, Jsonb(payload), at))


def test_health_build_counts_the_day(cur):
    h = lambda x: START + timedelta(hours=x)   # noqa: E731
    stoppage.open_stoppage(cur, "idle:a", "idle", "ラインが空", started_at=h(-2), quiet=True)
    stoppage.close_stoppage(cur, "idle:a", ended_at=h(6), quiet=True)          # 0〜6 時は空（前日からの分は切る）
    stoppage.open_stoppage(cur, "drain:b", "human", "一時停止", started_at=h(5), quiet=True)
    stoppage.close_stoppage(cur, "drain:b", ended_at=h(8), quiet=True)         # 5〜8 時。重なりは 1 回だけ → 計 8 時間
    stoppage.open_stoppage(cur, "job:1", "factory-bug", "ジョブの失敗", started_at=h(10), quiet=True)
    stoppage.close_stoppage(cur, "job:1", ended_at=h(10.5), quiet=True)
    stoppage.open_stoppage(cur, "merge:2", "human", "マージ待ち", started_at=h(20), quiet=True)   # 開いたまま
    ev(cur, "job.finished", {"state": "succeeded", "cost_usd": 1.5}, h(9))
    ev(cur, "job.finished", {"state": "failed", "cost_usd": 0.5}, h(11))
    ev(cur, "job.finished", {"state": "succeeded", "cost_usd": 9}, h(25))        # 翌日は数えない
    ev(cur, "inbox.notified", {"key": "k1", "count": 1}, h(1))
    ev(cur, "inbox.cleared", {"key": "k1", "waited_sec": 1800}, h(2))
    ev(cur, "inbox.notified", {"key": "k2", "count": 1}, h(3))
    rep = report_health.build(cur, START, END)
    got = {(s.id, i.label): i.value for s in rep.sections for i in s.items if i.kind == "metric"}
    assert got[("line", "ラインが動いていた割合")] == round(100 * (1 - 8 / 24), 1)
    assert got[("line", "止まった回数（ラインが空・一時停止を除く）")] == 2
    assert got[("line", "止まってから戻るまで（中央値）")] == 30
    assert got[("jobs", "終わったジョブ")] == 2 and got[("jobs", "失敗の割合")] == 50.0
    assert got[("inbox", "判断が済んだ件")] == 1 and got[("inbox", "判断までの待ち時間（中央値）")] == 30
    assert got[("inbox", "期間の終わりに残っていた件")] == 1
    assert got[("cost", "消費（API 単価に換算）")] == 2.0
    assert any(i.kind == "entry" and "マージ待ち" in i.label for s in rep.sections for i in s.items)


def test_daily_step_builds_once_and_delivers_per_destination(cur):
    cfg = notify.parse(DOC)
    now = datetime(2026, 11, 5, 6, 0, tzinfo=JST)
    posted, fail = [], {"provider_ops": 1}

    def poster(d, text):
        if fail.get(d.name):
            fail[d.name] -= 1
            raise OSError("down")
        posted.append((d.name, text.splitlines()[0]))
        return True

    early = report.daily_step(cur, datetime(2026, 11, 5, 4, 0, tzinfo=JST), cfg, poster=poster)
    assert early == {}                                                   # 5 時より前は出さない
    r = report.daily_step(cur, now, cfg, poster=poster)
    assert set(r) == {"report.factory_health", "report.product"}
    assert r["report.factory_health"]["sent"] == ["owner_ops"] and r["report.factory_health"]["failed"] == ["provider_ops"]
    r = report.daily_step(cur, now + timedelta(minutes=1), cfg, poster=poster)
    assert r["report.factory_health"]["sent"] == ["provider_ops"]       # 失敗した宛先だけ送り直す
    assert report.daily_step(cur, now + timedelta(minutes=2), cfg, poster=poster)["report.factory_health"]["sent"] == []
    cur.execute("select count(*) as n from events where name='report.built'")
    assert cur.fetchone()["n"] == 2                                       # 組み立ては種類ごとに 1 日 1 回
    assert sorted(n for n, _ in posted if "健康" in _) == ["owner_ops", "provider_ops"]


def test_daily_step_without_config_only_records(cur):
    r = report.daily_step(cur, datetime(2026, 11, 5, 6, 0, tzinfo=JST), notify.implicit(["log"]), poster=lambda d, t: pytest.fail("送らない"))
    assert r["report.factory_health"]["sent"] == []
    cur.execute("select payload from events where name='report.built' order by id")
    assert [b["payload"]["kind"] for b in cur.fetchall()] == ["report.factory_health", "report.product"]


def test_product_build_reads_tickets_and_merges(cur):
    h = lambda x: START + timedelta(hours=x)   # noqa: E731
    t = "insert into tickets(source, source_ref, repo, title, url, state, priority, held, waiting_on, created_at, updated_at) " \
        "values ('github', %s, split_part(%s, '#', 1), %s, %s, %s, %s, %s, %s, %s, %s)"
    cur.execute(t, ("o/p#1", "o/p#1", "D1: 前からある", "https://github.com/o/p/issues/1", "closed", 50, False, [], h(-48), h(-24)))
    cur.execute(t, ("o/p#2", "o/p#2", "D2: きのう済んだ", "https://github.com/o/p/issues/2", "closed", 50, False, [], h(-30), h(9)))
    cur.execute(t, ("o/p#3", "o/p#3", "D3: 進行中", "https://github.com/o/p/issues/3", "active", 60, False, [], h(-5), h(10)))
    cur.execute(t, ("o/p#4", "o/p#4", "D4: 次", "https://github.com/o/p/issues/4", "planned", 70, False, ["o/p#3"], h(-5), h(-5)))
    cur.execute(t, ("o/p#5", "o/p#5", "D5: 保留", "https://github.com/o/p/issues/5", "planned", 90, True, [], h(-5), h(-5)))
    cur.execute(t, ("x/y#1", "x/y#1", "別の製品", "", "closed", 50, False, [], h(-5), h(9)))
    ev(cur, "release.created", {"repo": "o/p", "app_version": "0.8.0", "prs": ["a", "b"]}, h(12))
    from psycopg.types.json import Jsonb
    cur.execute("insert into events(name, correlation_id, payload, occurred_at) values ('pr.merged', 'github:o/p#2', %s, %s)", (Jsonb({}), h(9)))
    rep = report_product.build(cur, START, END, "o/p")
    sec = {s.id: s.items for s in rep.sections}
    m = {i.label: i.value for s in rep.sections for i in s.items if i.kind == "metric"}
    assert m["済んだ"] == 2 and m["進めている"] == 1 and m["投入待ち"] == 2 and m["保留中"] == 1 and m["進み具合（済んだチケットの割合）"] == 40.0
    assert m["済んだチケット"] == 1 and m["マージした PR"] == 1
    assert [i.label for i in sec["done"] if i.kind == "entry"] == ["D2: きのう済んだ"]
    assert [i.label for i in sec["doing"]] == ["D3: 進行中"]
    assert [(i.label, i.state) for i in sec["next"]] == [("D4: 次", "1 件の完了待ち")]   # 保留は次に流すものに出さない
    assert [i.label for i in sec["releases"]] == ["版 0.8.0"]
    text = report.render(rep, "slack_webhook", "business_owner")
    assert "<https://github.com/o/p/issues/2|D2: きのう済んだ>" in text and "別の製品" not in text
