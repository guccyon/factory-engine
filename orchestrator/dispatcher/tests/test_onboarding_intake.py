"""オンボーディングで事業主から受け取る口と、棚卸しと解析のジョブ（ADR 0045 の追記 2026-11-15。onboarding_intake.py・onboarding_line.py）:
答えの確かめ（鍵らしい文字列を断る・ARN・リポ）・今の答えと製品の構成・答えから出す粒度・解析を積む時期・解析の結果から判定を測り直す・
窓口の API とコンソールの口・開発のジョブの参照のリポ。

例のデータは架空の製品（example/*）。DB を使うものは TEST_DATABASE_URL の Postgres で（なければ飛ばす）
"""
import base64
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
import onboarding  # noqa: E402
import onboarding_intake as intake  # noqa: E402
import onboarding_line  # noqa: E402
import products_page  # noqa: E402
import readiness  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
DEFS_I = intake.load([ROOT / "definitions" / intake.FILE])
DEFS_R = readiness.load([ROOT / "definitions" / readiness.FILE])
URL = os.environ.get("TEST_DATABASE_URL")
P = "example/web"
REPOS = [{"repo": "example/api", "role": "backend", "access": "read"}, {"repo": "example/app", "role": "mobile", "access": "read"},
         {"repo": "example/infra", "role": "infra", "access": "read"}, {"repo": "example/web", "role": "web", "access": "write", "main": True}]


@pytest.fixture()
def cur():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets, desk_conversations, desk_tokens restart identity cascade")
    yield c
    conn.close()


class Store(dict):
    def put(self, key, data, ctype):
        self[key] = data

    def get(self, key):
        return self[key]


def provide(cur, answers, store=None, by="事業主"):
    return intake.provide(cur, DEFS_I, P, answers, by, "desk", put=store.put if store is not None else None)


def test_definitions_point_at_known_items():
    """質問票の raises は準備度の項目を指す（定義どうしのずれを CI で落とす）"""
    items = {it["id"] for it in DEFS_R["items"]}
    ids = [q["id"] for q in DEFS_I["questions"]]
    assert len(ids) == len(set(ids))
    for q in DEFS_I["questions"]:
        assert q["kind"] in DEFS_I["kinds"]
        rz = q.get("raises")
        if rz:
            assert rz["item"] in items, q["id"]
            assert all(n in ids for n in rz.get("needs") or [])
        assert set(q.get("required") or []) <= set(q.get("for") or []) <= set(DEFS_R["targets"])
    assert {"p-cloud", "p-flag"} <= items


def test_provide_rejects_secrets_and_bad_shapes_without_recording(cur):
    bad = [{"question": "test_accounts", "value": "password: hunter2hunter2"}]
    with pytest.raises(intake.IntakeError) as e:
        provide(cur, bad)
    assert "鍵らしい" in str(e.value)
    for answers, word in (([{"question": "aws_deploy_role", "value": "AKIA" + "ABCDEFGHIJKLMNOP"}], "鍵らしい"),
                          ([{"question": "aws_deploy_role", "value": "arn:aws:iam::12:role/x"}], "ARN"),
                          ([{"question": "repos", "value": [{"repo": "api", "role": "backend"}]}], "owner/name"),
                          ([{"question": "backlog", "value": "trello"}], "github_issues"),
                          ([{"question": "nope", "value": "x"}], "知らない問い"),
                          ([{"question": "docs", "file": {"name": "a.md", "text": "x"}}], "置き場")):   # ファイルは置き場が要る
        with pytest.raises(intake.IntakeError) as e:
            provide(cur, answers)
        assert word in str(e.value)
    # 1 件でも問題があれば全部を記録しない
    with pytest.raises(intake.IntakeError):
        provide(cur, [{"question": "notify", "value": "#dev"}, {"question": "web_scope", "value": "token=abcdefghijk"}])
    cur.execute("select count(*) as n from events where name=%s", (intake.EVENT,))
    assert cur.fetchone()["n"] == 0
    st = Store()
    with pytest.raises(intake.IntakeError):   # ファイルの中の鍵も断る
        provide(cur, [{"question": "docs", "file": {"name": "env.txt", "text": "GH=ghp_" + "a" * 28}}], st)
    assert not st


def test_state_composition_and_levels(cur):
    st_store = Store()
    provide(cur, [{"question": "repos", "value": REPOS}, {"question": "notify", "value": "Slack #web-dev"},
                  {"question": "backlog", "value": "github_issues"},
                  {"question": "docs", "file": {"name": "運用手順.md", "content_b64": base64.b64encode("# 手順\n".encode()).decode()}}], st_store)
    s = intake.state(cur, DEFS_I, P)
    comp = intake.composition(s)
    assert comp["main"] == "example/web" and [r["repo"] for r in comp["refs"]] == ["example/api", "example/app", "example/infra"]
    assert s["files"][0]["key"] in st_store and st_store[s["files"][0]["key"]] == "# 手順\n".encode()
    assert "web_scope" in s["missing"]["dev"] and "approvers" in s["missing"]["dev"] and "repos" not in s["missing"]["dev"]
    lv = intake.derive_levels(DEFS_I, s)
    assert lv == {"p-repo": 2, "p-notify": 1, "p-backlog": 3, "p-docs": 1}
    # 承認者は通知の宛先があるときだけ 2、判断の受け手で 3。ARN は needs の順で上がる
    provide(cur, [{"question": "approvers", "value": "@owner"}, {"question": "contacts", "value": "開発の窓口"},
                  {"question": "aws_deploy_role", "value": "arn:aws:iam::123456789012:role/web-deploy"},
                  {"question": "flags", "value": "none"}, {"question": "staging", "skip": True}])
    s = intake.state(cur, DEFS_I, P)
    lv = intake.derive_levels(DEFS_I, s)
    assert lv["p-notify"] == 3 and "p-cloud" not in lv and "p-flag" not in lv   # アカウントが無いので配信のロールだけでは上げない・flag は「無い」
    assert s["answers"]["staging"]["skip"] and "staging" not in s["missing"].get("ship", [])
    provide(cur, [{"question": "aws_account", "value": "123456789012 ap-northeast-1（既存と別）"}])
    assert intake.derive_levels(DEFS_I, intake.state(cur, DEFS_I, P))["p-cloud"] == 2
    # リポを外す
    provide(cur, [{"question": "repos", "value": [{"repo": "example/infra", "remove": True}]}])
    assert [r["repo"] for r in intake.state(cur, DEFS_I, P)["repos"]] == ["example/api", "example/app", "example/web"]
    sm = intake.summary(DEFS_I, intake.state(cur, DEFS_I, P))
    q = next(x for x in sm["questions"] if x["id"] == "repos")
    assert "example/api（backend・API・読む）" in q["value"] and next(x for x in sm["questions"] if x["id"] == "docs")["value"] == "運用手順.md"


def test_reference_repos_for_the_written_repo(cur):
    assert intake.reference_repos(cur, "example/web") == []
    provide(cur, [{"question": "repos", "value": REPOS}])
    assert [r["repo"] for r in intake.reference_repos(cur, "example/web")] == ["example/api", "example/app", "example/infra"]
    assert intake.reference_repos(cur, "example/api") == []   # 工場が書かないリポには付けない
    assert intake.reference_repos(cur, "guccyon/rally") == []


NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def test_plan_waits_while_answering_then_runs_and_reassesses(cur):
    assert onboarding_line.plan(cur, DEFS_I, P, NOW)[0] == "none"   # リポがまだ無い
    provide(cur, [{"question": "repos", "value": REPOS}, {"question": "web_scope", "value": "予約の画面を Web に"}])
    cur.execute("update events set occurred_at=%s", (NOW - timedelta(minutes=2),))
    assert onboarding_line.plan(cur, DEFS_I, P, NOW)[0] == "wait"   # 答えが続いている
    intake.request_run(cur, P, "事業主", "desk")
    what, info = onboarding_line.plan(cur, DEFS_I, P, NOW)
    assert what == "run" and info["composition"]["main"] == P   # 頼まれたらすぐ
    jids = onboarding_line.enqueue(cur, "tier-large", now=NOW)
    assert len(jids) == 1
    cur.execute("select repo, issue_number, kind, correlation_id from jobs where id=%s", (jids[0],))
    j = cur.fetchone()
    assert (j["repo"], j["kind"], j["correlation_id"]) == (P, "onboard", "onboarding:" + P) and j["issue_number"] < -onboarding_line.ISSUE_BASE
    assert onboarding_line.job_repos(cur, jids[0]) == [r["repo"] for r in REPOS]
    assert onboarding_line.enqueue(cur, "tier-large", now=NOW) == []   # 流れている間は積まない
    cur.execute("update jobs set state='succeeded' where id=%s", (jids[0],))
    assert onboarding_line.plan(cur, DEFS_I, P, NOW)[0] == "assess"   # 解析の結果の記録が判定を測り直す（ここでは結果を記録していない）
    onboarding_line.enqueue(cur, "tier-large", now=NOW)
    assert onboarding_line.plan(cur, DEFS_I, P, NOW)[0] == "none"
    provide(cur, [{"question": "notify", "value": "#web"}])   # 解析に効かない答えは、判定だけ測り直す
    assert onboarding_line.plan(cur, DEFS_I, P, NOW + timedelta(hours=1))[0] == "assess"
    assert onboarding_line.enqueue(cur, "tier-large", now=NOW + timedelta(hours=1)) == []
    rec = onboarding.latest(cur, P)[0]
    assert rec["levels"]["p-notify"] == 1 and "takeover-written" in rec["met"]
    assert onboarding_line.plan(cur, DEFS_I, P, NOW + timedelta(hours=1))[0] == "none"
    provide(cur, [{"question": "auth_flow", "value": "Cognito のトークン"}])   # 解析に効く答えは、待ってから解析をやり直す
    cur.execute("update events set occurred_at=%s where name=%s and id=(select max(id) from events where name=%s)",
                (NOW, intake.EVENT, intake.EVENT))
    assert onboarding_line.plan(cur, DEFS_I, P, NOW + timedelta(minutes=30))[0] == "run"


ANALYSIS = {"summary": "backend は REST、モバイルは Flutter。", "repos": [
    {"repo": "example/api", "role": "backend", "languages": ["Go"], "frameworks": ["echo"], "build": "make", "test": "go test ./...", "ci": "GitHub Actions",
     "ship": {"mechanism": "GitHub Actions で ECS", "dispatchable": True, "note": ""}}],
    "services": [{"name": "プッシュ通知", "role": "知らせ", "repos": ["example/app"], "sandbox": "unknown", "handling": "mock"}],
    "api": {"source": "code", "endpoints_total": 42, "matched_ratio": 0.9, "note": "", "endpoints": [{"method": "GET", "path": "/v1/bookings", "summary": "予約の一覧"}]},
    "screens": [{"repo": "example/app", "name": "予約の一覧", "where": "lib/bookings.dart", "calls": ["/v1/bookings"]}],
    "db": {"engine": "PostgreSQL", "migrations": "db/migrations", "tables": 30, "note": ""}, "tests": [], "design": {"form": "U3", "note": ""},
    "network": {"backend_entry": "public", "auth": "Bearer", "cors": "無い", "bff_note": "同じドメインの BFF から呼ぶ"},
    "glossary": [{"term": "予約", "meaning": "レッスンの枠を押さえること"}],
    "docs": {"architecture_md": "# 構成\n", "api_yaml": "openapi: 3.0.0\n", "screens_md": "# 画面\n", "glossary_yaml": "- 予約\n"},
    "levels": {"o-stack": {"level": 3, "note": "x"}, "o-api": {"level": 3, "note": "照合 90%"}, "o-screens": {"level": 3, "note": "x"},
               "o-rules": {"level": 3, "note": "x"}, "o-glossary": {"level": 3, "note": "x"}},
    "met": ["repos-readable", "inventory-filled", "analysis-matched", "drafts-exist", "env-up"],
    "questions": [{"question": "staging にテスト用のアカウントはあるか", "why": "BFF の確かめに要る"}], "confidence": 0.7}


def test_record_analysis_caps_human_levels_and_adds_questions(cur):
    st = Store()
    provide(cur, [{"question": "repos", "value": REPOS}])
    intake.request_run(cur, P, "事業主", "desk")
    jid = onboarding_line.enqueue(cur, "tier-large", now=NOW)[0]
    cur.execute("update jobs set state='succeeded' where id=%s", (jid,))
    job = {"id": jid, "correlation_id": "onboarding:" + P}
    assert onboarding_line.record(cur, job, {"ok": False}, "failed", "exit=1", put=st.put) is None
    aid = onboarding_line.record(cur, job, {"analysis": ANALYSIS, "cost_usd": 3.2}, "succeeded", None, put=st.put)
    an = intake.latest_analysis(cur, P)
    assert an["id"] == aid and an["docs"]["api.yaml"] in st and an["counts"]["endpoints"] == 42
    assert an["questions"][0]["id"] == f"a-{jid}-1"
    rec = onboarding.latest(cur, P)[0]
    assert rec["levels"]["o-api"] == 3 and rec["levels"]["o-screens"] == 2 and rec["levels"]["o-rules"] == 2   # 人の確かめが要る粒度は上げない
    assert "env-up" not in rec["met"] and {"repos-readable", "analysis-matched", "takeover-written"} <= set(rec["met"])
    assert rec["stack"]["repos"][0]["languages"] == ["Go"]
    # 解析が足した問いに答えられる。読みの確かめで o-screens・o-glossary が 3 になり、reading-confirmed がつく
    provide(cur, [{"question": f"a-{jid}-1", "value": "ある（Secrets Manager の web/staging-users）"}, {"question": "reading_confirmed", "value": "はい"}])
    onboarding_line.assess(cur, DEFS_R, DEFS_I, P, by="test")
    rec = onboarding.latest(cur, P)[0]
    assert rec["levels"]["o-screens"] == 3 and rec["levels"]["o-glossary"] == 3 and "reading-confirmed" in rec["met"]
    assert intake.state(cur, DEFS_I, P)["open_analysis_questions"] == []
    # 次の解析の後は、前の確かめは古くなる
    cur.execute("insert into events(name, correlation_id, payload) values (%s, %s, %s)", (intake.ANALYZED, f"product:{P}", '{"product": "%s"}' % P))
    assert intake.state(cur, DEFS_I, P)["answers"]["reading_confirmed"]["stale"]


def test_desk_route_and_console_body(cur):
    import desk
    import desk_api
    import psycopg
    from psycopg.rows import dict_row
    tok = desk.create_token(cur, "owner-a", "requester", [P], "test")
    other = desk.create_token(cur, "owner-b", "requester", ["example/other"], "test")
    st = Store()

    def call(method, path, body=None, token=tok):
        try:
            return desk_api.route(method, path, {}, body or {}, token, connect=lambda: psycopg.connect(URL, row_factory=dict_row, autocommit=True),
                                  put=st.put, get=st.get)
        except desk.DeskError as e:
            return e.status, {"error": str(e)}

    code, out = call("GET", f"/api/desk/onboarding/{P}")
    assert code == 200 and "repos" in out["missing"]["dev"] and out["assessment"] is None
    assert call("GET", f"/api/desk/onboarding/{P}", token=other)[0] == 404
    code, out = call("POST", f"/api/desk/onboarding/{P}/answers", {"answers": [{"question": "repos", "value": REPOS},
                                                                              {"question": "design", "file": {"name": "tokens.json", "text": "{}"}}]})
    assert code == 201 and out["accepted"] == ["repos", "design"] and out["status"]["composition"]["main"] == P
    assert call("POST", f"/api/desk/onboarding/{P}/answers", {"answers": [{"question": "notify", "value": "secret: abcdefghijkl"}]})[0] == 400
    assert call("POST", f"/api/desk/onboarding/{P}/run", {"reason": "すぐに"})[0] == 202
    assert call("GET", f"/api/desk/onboarding/{P}/docs/api.yaml")[0] == 404
    # コンソールの製品のページ: 判定がなくても答えがあれば出す
    sm = intake.summary(DEFS_I, intake.state(cur, DEFS_I, P))
    html = products_page.body_product(None, P, intake=sm)
    assert "オンボーディングで渡すもの" in html and 'data-ob="web_scope"' in html and "example/api" in html and "<script" not in html
