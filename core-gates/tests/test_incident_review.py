"""事故の調査委員会の週次の見回り（core-gates/incident_review.py、ADR 0046）"""
import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_runner
import incident_review as ir

ENGINE = Path(__file__).resolve().parents[2]
DEFS = yaml.safe_load((ENGINE / "definitions" / "incident.yaml").read_text(encoding="utf-8"))

GOOD = """id: FIR-0000-0000
title: 実装のやり直しが前の成果を捨て、同じ費用をもう一度払った
status: draft
resource: llm_tokens
mechanism_class: double_pay
severity: medium
affects:
  components: [続きのジョブ]
symptom: [同じチケットの実装のジョブが短い間に何度も最初から走る]
mechanism: 打ち切られたジョブの続きで、引き継ぎを読まずに最初から作り直していた
impact: {cost_vs_daily_budget: 0.4, share_of_spend: 0.3, wasted_vs_median: 6.0, noticed_by: factory}
detection: {viewpoints: [PV-tokens-double-pay]}
mitigation:
  fixed_in: ""
  root_fix: 続きのジョブは前の引き継ぎから始める
  generic_guards: [ticket_ceilings]
  dedicated_checks: []
related: {adr: ["0017"]}
recurrences: []
local_ref: IC-20261108-1
"""

FINDING = {"viewpoint": "PV-tokens-double-pay", "title": "やり直しの二重払い", "resource": "llm_tokens", "mechanism": "double_pay",
           "severity": "medium", "is_incident": True, "case": "IC-20261108-1", "overlaps": "", "why": "なぜなぜ", "evidence": "枠の 40%",
           "engine_fix": "引き継ぎから始める", "record": True}


def week(**kw):
    return {"week": "2026-W46", "spent_usd": 120.0, "signs": [{"key": "tokens:ticket:x", "viewpoints": ["PV-tokens-double-pay"]}],
            "cases": [{"id": "IC-20261108-1", "resource": "llm_tokens", "mechanism": None}], "outbox_ids": [], "deny": ["acme-shop", "acme"], **kw}


def test_select_viewpoints_weights_signs_cases_and_records():
    vps = ir.select_viewpoints(DEFS, week()["signs"], week()["cases"], [{"resource": "cloud_metered", "mechanism_class": "leak", "status": "draft"}], 3)
    ids = [v["id"] for v in vps]
    assert ids[0] == "PV-tokens-double-pay"                     # 兆し 3 ＋ 台帳 2
    assert len(ids) == 3 and all(v["resource"] == "llm_tokens" for v in vps[:3])
    assert len(ir.select_viewpoints(DEFS, [], [], [], 8)) == min(8, len(DEFS["viewpoints"]))


def test_next_id_skips_records_and_outbox():
    assert ir.next_id(2026, [{"id": "FIR-2026-0002"}], []) == "FIR-2026-0003"
    assert ir.next_id(2027, [{"id": "FIR-2026-0002"}], ["FIR-2026-0005"]) == "FIR-2027-0006"   # 番号は年をまたいで通し
    assert ir.next_id(2026, [], []) == "FIR-2026-0001"


def test_check_draft_forces_id_and_finds_names():
    rec, errs = ir.check_draft("FIR-2026-0003", GOOD, {}, [])
    assert errs == [] and rec["id"] == "FIR-2026-0003" and rec["local_ref"] is None
    _, errs = ir.check_draft("FIR-2026-0003", GOOD.replace("続きのジョブ]", "acme-shop の続きのジョブ]"), {}, ["acme-shop"])
    assert any("acme-shop" in e for e in errs)
    _, errs = ir.check_draft("FIR-2026-0003", GOOD, {"title": "事故の直し: x", "body": "https://github.com/o/r/pull/3 を見て"}, [])
    assert any("Engine の改善 Issue" in e for e in errs)
    _, errs = ir.check_draft("FIR-2026-0003", "a: [", {}, [])
    assert errs and "YAML" in errs[0]


def fake_runner(monkeypatch, record_outputs, review_ok=True, patrol=None):
    calls = []

    def run(step, prompt, *, schema=None, tier=None, max_turns=3, retries=3):
        here = sorted(p.name for p in Path(".").iterdir())
        calls.append((step, here, prompt))
        if step == "incident.patrol":
            out = patrol or {"summary": "費用の週", "findings": [dict(FINDING), {**FINDING, "title": "ふだんの範囲", "is_incident": False, "record": False, "case": "IC-X"}],
                             "new_viewpoints": [], "check_reviews": []}
        elif step == "incident.record":
            out = record_outputs.pop(0)
        else:
            out = {"ok": review_ok, "problems": [] if review_ok else [{"kind": "identifiable", "where": "title", "why": "業務の言葉"}]}
        return agent_runner.Result(output=out, cost_usd=1.0, model="m", usage={"output_tokens": 10})
    monkeypatch.setattr(agent_runner, "run", run)
    return calls


def run_main(tmp_path, wk):
    (tmp_path / "in.json").write_text(json.dumps(wk), encoding="utf-8")
    out = tmp_path / "out.json"
    assert ir.main(["--input", str(tmp_path / "in.json"), "--out", str(out), "--engine-dir", str(ENGINE)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def test_three_roles_draft_and_review(tmp_path, monkeypatch):
    calls = fake_runner(monkeypatch, [{"yaml": GOOD, "issue_title": "事故の直し: 続きのジョブの二重払い", "issue_body": "続きのジョブが引き継ぎを読まない"}])
    r = run_main(tmp_path, week())
    assert [c[0] for c in calls] == ["incident.patrol", "incident.record", "incident.review"]
    assert calls[0][1] == ["incident.yaml", "incidents", "week.json"] and "PV-tokens-double-pay" in calls[0][2]
    assert "finding.json" in calls[1][1] and "draft.yaml" in calls[2][1] and "acme-shop" in calls[2][2]
    assert r["result"]["findings"][1]["case"] == ""            # 知らない台帳の id は消す
    d = r["drafts"][0]
    assert d["ok"] and d["id"].startswith("FIR-2026-") and d["case"] == "IC-20261108-1" and d["issue"]["title"].startswith("事故の直し")
    assert "local_ref: null" in d["yaml"] and r["cost_usd"] == 3.0


def test_record_is_retried_once_with_check_errors(tmp_path, monkeypatch):
    bad = {"yaml": GOOD.replace("noticed_by: factory}", "noticed_by: factory, usd: 120}"), "issue_title": "", "issue_body": ""}
    calls = fake_runner(monkeypatch, [bad, {"yaml": GOOD, "issue_title": "", "issue_body": ""}])
    r = run_main(tmp_path, week())
    assert [c[0] for c in calls] == ["incident.patrol", "incident.record", "incident.record", "incident.review"]
    assert "impact.usd" in calls[2][2] and r["drafts"][0]["ok"] and r["drafts"][0]["issue"] is None


def test_review_rejects_and_overlap_is_not_drafted(tmp_path, monkeypatch):
    fake_runner(monkeypatch, [{"yaml": GOOD, "issue_title": "", "issue_body": ""}], review_ok=False)
    r = run_main(tmp_path, week())
    assert not r["drafts"][0]["ok"] and r["drafts"][0]["review"]["problems"][0]["kind"] == "identifiable"
    over = {"summary": "s", "findings": [{**FINDING, "overlaps": "FIR-2026-0001"}], "new_viewpoints": []}
    calls = fake_runner(monkeypatch, [], patrol=over)
    r = run_main(tmp_path, week())
    assert [c[0] for c in calls] == ["incident.patrol"] and r["drafts"] == []   # 既存の記録の再発は新しい下書きにしない


def test_quiet_week_skips_agents(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runner, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("呼ばない")))
    r = run_main(tmp_path, week(spent_usd=0, signs=[], cases=[]))
    assert r["drafts"] == [] and r["cost_usd"] == 0


def test_bad_input_exits_2(tmp_path):
    (tmp_path / "in.json").write_text("{}", encoding="utf-8")
    assert ir.main(["--input", str(tmp_path / "in.json"), "--out", str(tmp_path / "o.json"), "--engine-dir", str(ENGINE)]) == 2


def test_due_dedicated_checks_go_to_patrol(tmp_path, monkeypatch):
    name = DEFS["dedicated_checks"][0]["name"]
    calls = fake_runner(monkeypatch, [], patrol={"summary": "s", "findings": [], "new_viewpoints": [],
                                                "check_reviews": [{"name": name, "keep": False, "why": "断片化の後の PR だけになった"},
                                                                  {"name": "知らない", "keep": False, "why": "x"}]})
    r = run_main(tmp_path, week(spent_usd=0, signs=[], cases=[], to="2027-01-20T12:00:00+09:00"))
    assert name in calls[0][2] and r["facts"]["checks_due"] == [name]
    assert [c["name"] for c in r["result"]["check_reviews"]] == [name] and r["result"]["check_reviews"][0]["fir"] == "FIR-2026-0001"
    calls = fake_runner(monkeypatch, [{"yaml": GOOD, "issue_title": "", "issue_body": ""}])
    run_main(tmp_path, week(to="2026-11-15T12:00:00+09:00"))
    assert "（なし）" in calls[0][2]
