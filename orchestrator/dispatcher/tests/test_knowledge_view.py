"""事業部の知見の写し・コンソールの知見のページ・窓口の知見の問い合わせ・振り返りの議事録（ADR 0061 の決定 8・9）。"""
import base64
import json
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import console_committee  # noqa: E402
import console_knowledge  # noqa: E402
import knowledge_snapshot as ks  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")
ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def k_doc(i, **kw):
    d = {"id": f"K-{i:04d}", "theme": 3, "kind": "fact", "statement": f"知見 {i}: 撮影の場所では 4G 以上の回線が使えない",
         "scope": {"work": ["dev.build"], "roles": ["implement"]}, "acts_on": "product", "confidence": "hypothesis",
         "evidence": [{"case": "C-0001"}, {"pr": 546}], "status": "active", "verified_at": __import__("datetime").date(2026, 11, 8),
         "review_by": __import__("datetime").date(2026, 12, 8), "confidentiality": "product", "related": ["K-0002"],
         "invalidate_when": ["回線の事情が変わった"], "decided_by": "committee:D-0001"}
    d.update(kw)
    return d


class FakeGH:
    def __init__(self, docs):
        self.docs, self.calls = docs, []

    def __call__(self, method, path, body=None):
        self.calls.append(path)
        if path.endswith("/contents/.factory-config/knowledge"):
            return [{"type": "file", "name": n, "path": f".factory-config/knowledge/{n}", "sha": sha} for n, (sha, _) in self.docs.items()] + \
                   [{"type": "file", "name": "INDEX.md", "path": ".factory-config/knowledge/INDEX.md", "sha": "x"},
                    {"type": "dir", "name": "cases", "path": ".factory-config/knowledge/cases", "sha": "y"}]
        name = path.rsplit("/", 1)[1]
        return {"content": base64.b64encode(yaml.safe_dump(self.docs[name][1], allow_unicode=True).encode()).decode()}


def test_theme_names_match_taxonomy():
    with open(os.path.join(ROOT, "definitions", "learning_taxonomy.yaml"), encoding="utf-8") as f:
        themes = {t["id"]: t["ja"] for t in yaml.safe_load(f)["themes"]}
    assert console_knowledge.THEMES == themes


def test_item_withholds_client_statement_and_flattens_evidence():
    it = ks.item(k_doc(1, confidentiality="client"), "p")
    assert it["withheld"] and "4G" not in it["statement"] and it["invalidate_when"] == []
    it = ks.item(k_doc(2), "p")
    assert it["evidence"] == ["case:C-0001", "pr:546"] and it["scope"]["work"] == ["dev.build"]
    assert ks.item({"theme": 1}, "p") is None


def test_fetch_reads_only_changed_files():
    gh = FakeGH({"K-0001.yaml": ("a", k_doc(1)), "K-0002.yaml": ("b", k_doc(2))})
    items, shas = ks.fetch(gh, "o/r")
    assert [i["id"] for i in items] == ["K-0001", "K-0002"] and items[0]["verified_at"] == "2026-11-08"
    gh2 = FakeGH({"K-0001.yaml": ("a", k_doc(1)), "K-0002.yaml": ("c", k_doc(2, statement="直した"))})
    items2, _ = ks.fetch(gh2, "o/r", {"items": items, "shas": shas})
    assert [c for c in gh2.calls if c.endswith(".yaml")] == ["repos/o/r/contents/.factory-config/knowledge/K-0002.yaml"]
    assert items2[1]["statement"] == "直した"


@pytest.fixture()
def cur():
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


@db
def test_refresh_writes_only_on_change_and_console_lists(cur):
    gh = FakeGH({"K-0001.yaml": ("a", k_doc(1)), "K-0002.yaml": ("b", k_doc(2, theme=6, confidence="confirmed")),
                 "K-0003.yaml": ("c", k_doc(3, status="retired"))})
    assert ks.refresh(cur, gh, "o/r") and not ks.refresh(cur, gh, "o/r")
    cur.execute("select count(*) as n from artifacts where type='KnowledgeSnapshot'")
    assert cur.fetchone()["n"] == 1
    cur.execute("insert into events(name, payload) values ('knowledge.used', %s)", (json.dumps({"repo": "o/r", "ids": ["K-0001"]}),))
    got = console_knowledge.search(cur)
    assert [i["id"] for i in got["items"]] == ["K-0001", "K-0002"]   # 廃止は既定で出さない
    assert got["items"][0]["uses"] == 1 and got["items"][0]["theme_ja"] == "ドメインの理解"
    assert got["products"][0]["docs_url"] == "https://github.com/o/r/tree/HEAD/docs"
    assert [i["id"] for i in console_knowledge.search(cur, theme=6)["items"]] == ["K-0002"]
    assert [i["id"] for i in console_knowledge.search(cur, confidence="confirmed")["items"]] == ["K-0002"]
    assert len(console_knowledge.search(cur, status="all")["items"]) == 3
    assert [i["id"] for i in console_knowledge.search(cur, q="知見 1: 4G")["items"]] == ["K-0001"]
    st, d = console_knowledge.get(cur, "/api/knowledge/K-0002?repo=o/r")
    assert st == 200 and d["url"].endswith("/.factory-config/knowledge/K-0002.yaml") and d["confidence_ja"] == "確認済み"
    assert console_knowledge.get(cur, "/api/knowledge/K-0099")[0] == 404
    assert console_knowledge.get(cur, "/api/knowledge?theme=3")[1]["total"] == 1


@db
def test_desk_knowledge_is_limited_to_token_products(cur, monkeypatch):
    import desk_api
    ks.refresh(cur, FakeGH({"K-0001.yaml": ("a", k_doc(1))}), "o/r")
    ks.refresh(cur, FakeGH({"K-0001.yaml": ("a", k_doc(1, statement="ほかの製品"))}), "x/y")
    who = {"name": "n", "role": "requester", "products": ["o/r"]}
    st, out = desk_api.knowledge_route(cur, who, [], {})
    assert st == 200 and [(i["repo"], i["id"]) for i in out["items"]] == [("o/r", "K-0001")] and out["total"] == 1
    assert desk_api.knowledge_route(cur, who, [], {"product": "x/y"})[0] == 403
    st, one = desk_api.knowledge_route(cur, who, ["K-0001"], {"product": "o/r"})
    assert st == 200 and one["knowledge"]["repo"] == "o/r"
    assert desk_api.knowledge_route(cur, who, ["K-0001"], {})[1]["knowledge"]["repo"] == "o/r"   # 範囲の外の製品の同じ id は見ない
    assert desk_api.knowledge_route(cur, who, ["K-0009"], {})[0] == 404


@db
def test_retro_minutes_in_console(cur):
    p = {"repo": "o/r", "sprint": 2, "size": 12, "reason": "size", "tickets": ["o/r#1", "o/r#2"], "titles": {"o/r#1": "画面A"},
         "from": "2026-11-16T03:00:00+00:00", "to": "2026-11-17T03:00:00+00:00", "roles": ["実装", "QA"],
         "failed_roles": [{"role": "検査", "error": "timeout"}],
         "opinions": [{"role": "実装", "perspective": "計画と分割", "kind": "problem", "tickets": ["o/r#1"], "observation": "途中で切れた", "proposal": "分ける"},
                      {"role": "QA", "perspective": "ドメインの気づき", "kind": "insight", "tickets": ["o/r#2"], "observation": "屋外は回線が弱い"}],
         "summary": "分割が粗かった", "knowledge": [{"statement": "画面と API は分ける", "destination": "knowledge", "perspective": "計画と分割",
                                               "from_roles": ["実装"], "evidence_tickets": ["o/r#1", "o/r#2"]}],
         "rejected": [{"statement": "色", "from_roles": ["デザイナー"], "why": "局所的"}],
         "actions": [{"target": "process", "what": "切る基準", "why": "途中終了"}], "previous_effect": "",
         "filed": {"知見の変更セット（振り返り S-2）": "https://github.com/o/r/issues/101"}}
    cur.execute("insert into events(name, payload) values ('retro.held', %s) returning id", (json.dumps(p),))
    mid = cur.fetchone()["id"]
    cur.execute("insert into events(name, payload) values ('retro.size_adjusted', %s)",
                (json.dumps({"repo": "o/r", "sprint": 2, "prev": 12, "next": 14, "reason": "局所的"}),))
    cur.execute("insert into events(name, payload) values ('retro.filed', %s)",
                (json.dumps({"repo": "o/r", "sprint": 2, "what": "knowledge", "issue": "https://github.com/o/r/issues/101"}),))
    cur.execute("""insert into jobs(correlation_id, repo, issue_number, kind, state, result)
                   values ('github:o/r#101','o/r',101,'implement','succeeded','{"pr_url": "https://github.com/o/r/pull/7"}')""")
    cur.execute("insert into events(name, payload) values ('committee.decided', %s)",
                (json.dumps({"pr_url": "https://github.com/o/r/pull/7", "outcome": "adopt", "decision": "D-0009"}),))
    rows = [x for x in console_committee.list_meetings(cur, "knowledge")["meetings"] if x["kind"] == "振り返り"]
    assert rows[0]["title"] == "振り返り S-2（チケット 2 件）" and rows[0]["kind"] == "振り返り" and rows[0]["repo"] == "o/r"
    assert rows[0]["outcome"] == "知見 1 件・打ち手 1 件" and rows[0]["level"] == "bad"   # 欠席がある
    d = console_committee.detail(cur, mid)
    secs = {s["h"]: s for s in d["sections"]}
    tk = next(s for h, s in secs.items() if h.startswith("対象のチケット"))
    assert tk["items"][0] == "https://github.com/o/r/issues/1 画面A" and "件数で区切り" in next(h for h in secs if h.startswith("対象"))
    assert "事業部のリーダー（取りまとめ）" in secs["出席"]["items"] and "欠席: 検査（timeout）" in secs["出席"]["items"]
    assert secs["実装 の意見"]["items"][0].startswith("[計画と分割・問題] 途中で切れた → 案: 分ける")
    assert "置き場: 事業部の知見" in secs["得られた知見"]["items"][0]
    assert secs["打ち手"]["items"] == ["[工程] 切る基準: 途中終了"]
    assert secs["次のスプリントの大きさ"]["text"] == "N = 12 → 14（局所的）"
    assert secs["知見のレビューの結果"]["items"] == ["採用（議決 D-0009）: https://github.com/o/r/pull/7"]
    assert "前回の打ち手の効き目" not in secs   # 空の節は出さない
