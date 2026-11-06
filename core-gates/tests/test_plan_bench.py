"""プランナーのベンチ（core-gates/plan_bench.py、ADR 0036 の 4b）: 見立ての取り出し、採点、合格線、採点し直し"""
import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_runner  # noqa: E402
import plan_bench as pb  # noqa: E402

BENCH = {"split_ok_min": 0.83, "band_ok_min": 0.83}


def case(i, split, band="M", layers=("backend",), route=("implement",)):
    return {"id": f"c{i}", "expected": {"split": split, "band": band, "layers": list(layers), "route": list(route)},
            "input": {"title": f"t{i}", "body": "b"}}


def test_predicted_counts_split_marks_and_oversized():
    base = {"plan": {"size": {"layers": ["backend", "docs", "contract"], "turns_band": "L"}}, "steps": ["implement"]}
    assert pb.predicted({**base, "decision": "split", "tasks": [1, 2]})["split"]
    assert pb.predicted({**base, "decision": "ready", "advisories": ["split_after_design"]})["split"]
    assert pb.predicted({**base, "decision": "escalate", "guards": ["oversized"]})["split"]
    p = pb.predicted({**base, "decision": "ready"})
    assert not p["split"] and p["layers"] == ["backend", "contract"] and p["band"] == "L"


def test_score_and_summarize():
    s = pb.score_case(case(1, True, "XL", ["contract", "backend", "web"]), {"split": True, "band": "L", "layers": ["backend", "web"], "route": ["implement"]})
    assert s["split_ok"] and s["band_ok"] and s["layers_jaccard"] == round(2 / 3, 3) and s["route_ok"]
    assert pb.score_case(case(2, False, "S"), {"split": False, "band": "L", "layers": ["backend"], "route": []})["band_ok"] is False
    assert pb.score_case(case(3, False, None), {"split": False, "band": None, "layers": [], "route": []})["band_ok"] is None
    ok = [pb.score_case(case(i, False), {"split": False, "band": "M", "layers": ["backend"], "route": ["implement"]}) for i in range(10)]
    ng = [pb.score_case(case(i, True), {"split": False, "band": "M", "layers": ["backend"], "route": ["implement"]}) for i in range(10, 12)]
    sm = pb.summarize(ok + ng, BENCH)
    assert (sm["split_ok"], sm["cases"], sm["pass"]) == (10, 12, True)
    assert (sm["should_split_ok"], sm["should_split"]) == (0, 2)
    ng.append(pb.score_case(case(12, True), {"split": False, "band": "M", "layers": ["backend"], "route": ["implement"]}))
    assert pb.summarize(ok + ng, BENCH)["pass"] is False
    assert "切る／切らないの一致 10/13" in pb.render(pb.summarize(ok + ng, BENCH), ok + ng)


def test_bench_line_in_definitions():
    defs = yaml.safe_load((Path(__file__).resolve().parents[2] / "definitions" / "planner.yaml").read_text(encoding="utf-8"))
    assert defs["sizing"]["bench"] == BENCH


def test_main_runs_and_rescores(tmp_path, monkeypatch):
    cases = {"cases": [case(1, True, "XL", ["contract", "backend", "web"]), case(2, False, "S")]}
    (tmp_path / "cases.yaml").write_text(yaml.safe_dump(cases, allow_unicode=True), encoding="utf-8")
    outs = iter([{"ready": True, "steps": [{"role": "implement", "focus": "f", "why": "w", "effort": "deep"}], "design_scale": "none",
                  "design_direction_given": False, "confidence": 0.9, "questions": [], "alternatives": [], "summary": "s", "evidence": [],
                  "interpretation": "i", "size": {"layers": ["contract", "backend", "web"], "turns_band": "XL", "basis": "b"},
                  "tasks": [{"key": "t1", "title": "契約", "kind": "contract", "summary": "s", "do": ["d"], "acceptance": ["a"], "depends_on": [], "effort": "light"},
                            {"key": "t2", "title": "実装", "kind": "implement", "summary": "s", "do": ["d"], "acceptance": ["a"], "depends_on": ["t1"], "effort": "standard"}]},
                 {"ready": True, "steps": [{"role": "implement", "focus": "f", "why": "w", "effort": "light"}], "design_scale": "none",
                  "design_direction_given": False, "confidence": 0.9, "questions": [], "alternatives": [], "summary": "s", "evidence": [],
                  "interpretation": "i", "size": {"layers": ["backend"], "turns_band": "S", "basis": "b"}}])
    monkeypatch.setattr(agent_runner, "run", lambda *a, **k: agent_runner.Result(output=next(outs), cost_usd=0.02, model="m"))
    out = tmp_path / "r.json"
    assert pb.main(["--cases", str(tmp_path / "cases.yaml"), "--out", str(out)]) == 0
    r = json.loads(out.read_text(encoding="utf-8"))
    assert r["summary"]["split_ok"] == 2 and r["scored"][0]["result"]["decision"] == "split"
    assert pb.main(["--cases", str(tmp_path / "cases.yaml"), "--score", str(out), "--baseline", str(out)]) == 0
