"""窓口の回答役（core-gates/desk_answer.py、ADR 0030 の 4）: 問い返しか回答かの決定、根拠の参照の確かめ、材料の写し方"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_runner
import desk_answer as da

STANCES = {"planner": "計画", "factory_head": "工場長"}
RECORDS = {"line": {"id": "line:status", "desired": {"workers": 2}},
           "tickets": [{"id": "ticket:guccyon/rally#12", "ref": "guccyon/rally#12", "state": "active"}],
           "referenced": [{"ref": "guccyon/rally#12", "jobs": [{"id": "job:345", "state": "failed"}],
                           "events": [{"id": "event:678", "name": "job.finished"}], "open_stoppages": [{"id": "stoppage:s1"}]}]}
INP = {"questions_asked": 0, "max_rounds": 1, "stances": STANCES, "records": RECORDS}
Q = {"question": "どの週ですか", "why": "決まると数えられる", "options": ["今週", "先週"], "recommended": 0}


def O(done=True, question=None, stance="planner", answer="#12 は CI 待ちで止まっています", evidence=None, unknowns=(), switch=False):
    return {"done": done, "question": question or {"question": "", "why": "", "options": [], "recommended": -1}, "stance": stance,
            "answer": answer, "evidence": evidence if evidence is not None else [{"ref": "job:345", "what": "実装のジョブが落ちた"}],
            "unknowns": list(unknowns), "switch_to_request": switch, "confidence": 0.7}


def test_record_ids_walks_every_record():
    assert da.record_ids(RECORDS) == {"line:status", "ticket:guccyon/rally#12", "job:345", "event:678", "stoppage:s1"}


def test_question_once_then_answer():
    d = da.decide(O(done=False, question=Q), INP, None)
    assert d["outcome"] == "question" and d["round"] == 1 and d["max_rounds"] == 1 and d["question"]["recommended"] == 0
    d = da.decide(O(done=False, question=Q), {**INP, "questions_asked": 1}, None)   # 問い返しは 1 回まで。使い切ったら答える
    assert d["outcome"] == "answer"
    d = da.decide(O(done=False, question=Q), {**INP, "force_answer": True}, None)
    assert d["outcome"] == "answer"


def test_answer_keeps_known_refs_and_drops_unknown(tmp_path):
    (tmp_path / "factory").mkdir()
    (tmp_path / "factory" / "roadmap.yaml").write_text("x", encoding="utf-8")
    ev = [{"ref": "job:345", "what": "落ちた"}, {"ref": "job:999", "what": "作り話"}, {"ref": "factory/roadmap.yaml#P5b", "what": "残り"},
          {"ref": "../etc/passwd", "what": "外"}, {"ref": "/etc/passwd", "what": "外"}, {"ref": "product/none.md", "what": "ない"}]
    d = da.decide(O(evidence=ev, unknowns=["いつ直るか", " "], switch=True), INP, tmp_path)
    assert d["outcome"] == "answer" and d["stance"] == "planner" and d["switch_to_request"] is True
    assert [e["ref"] for e in d["evidence"]] == ["job:345", "factory/roadmap.yaml#P5b"]
    assert d["dropped_refs"] == ["job:999", "../etc/passwd", "/etc/passwd", "product/none.md"]
    assert d["unknowns"] == ["いつ直るか"]


def test_no_evidence_or_bad_stance_fails():
    d = da.decide(O(evidence=[{"ref": "job:999", "what": "作り話"}]), INP, None)
    assert d["outcome"] == "failed" and "根拠" in d["errors"][0] and d["dropped_refs"] == ["job:999"]
    d = da.decide(O(stance="qa", answer=" "), INP, None)
    assert d["outcome"] == "failed" and len(d["errors"]) == 2


def test_stage_copies_records_product_and_factory(tmp_path):
    repo = tmp_path / "product"
    (repo / "specs").mkdir(parents=True)
    (repo / "specs" / "roadmap.md").write_text("r", encoding="utf-8")
    (repo / "specs" / "a.png").write_bytes(b"x")
    (repo / "src").mkdir()
    (repo / "src" / "main.rs").write_text("fn main(){}", encoding="utf-8")
    root = tmp_path / "work"
    root.mkdir()
    da.stage(root, {"product": "guccyon/rally", "records": RECORDS,
                    "messages": [{"role": "requester", "body": "#12 はなぜ止まっている？"}]}, repo, [{"number": 12, "title": "t", "labels": []}])
    assert "#12 はなぜ止まっている？" in (root / "conversation.md").read_text(encoding="utf-8")
    assert json.loads((root / "records.json").read_text(encoding="utf-8")) == RECORDS
    assert (root / "product" / "specs" / "roadmap.md").is_file() and not (root / "product" / "specs" / "a.png").exists()
    assert not (root / "product" / "src").exists()            # コードは渡さない（記録と規範で答える）
    assert (root / "factory" / "roadmap.yaml").is_file() and (root / "factory" / "adr").is_dir()
    assert "#12" in (root / "open_issues.md").read_text(encoding="utf-8")


def test_answer_end_to_end_with_fake_agent(monkeypatch, tmp_path):
    seen = {}
    ar = sys.modules["agent_runner"]   # ほかの試験が agent_runner を読み直していても、desk_answer が呼ぶほうを差し替える

    def fake_run(step, prompt, schema=None, max_turns=None, **kw):
        seen.update(step=step, prompt=prompt, schema=schema, cwd=os.getcwd())
        assert (Path(os.getcwd()) / "records.json").is_file()
        return ar.Result(output=O(evidence=[{"ref": "records.json", "what": "記録"}, {"ref": "ticket:guccyon/rally#12", "what": "active"}]),
                                   cost_usd=0.1, model="m")
    monkeypatch.setattr(ar, "run", fake_run)
    out = da.answer({**INP, "product": "guccyon/rally", "messages": []}, None, [])
    assert seen["step"] == "desk.answer" and set(seen["schema"]["properties"]["stance"]["enum"]) == set(STANCES)
    assert "残りの問い返しは 1 回" in seen["prompt"]
    assert out["outcome"] == "answer" and [e["ref"] for e in out["evidence"]] == ["records.json", "ticket:guccyon/rally#12"]
    assert out["cost_usd"] == 0.1 and out["versions"]["prompt"] == da.PROMPT_VERSION


def test_step_is_declared():
    conf = agent_runner.load_agents()
    assert agent_runner.resolve("desk.answer", conf=conf).model
