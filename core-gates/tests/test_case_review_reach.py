"""事例の分析で真因と同じだった知見が、その回に届いたか（core-gates/case_review.py の reach、ADR 0040 の決定 8）"""
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import case_review as cr  # noqa: E402

TODAY = date(2026, 11, 9)
BASE = ("theme: 4\nkind: practice\nstatus: active\nverified_at: '2026-11-01'\nreview_by: '2027-01-01'\nconfidentiality: product\n"
        "evidence:\n  - pr: 1\ndecided_by: committee:D-0001\nstatement: s\n")


def repo(tmp_path: Path, items: dict[str, str]) -> Path:
    d = tmp_path / ".factory-config" / "knowledge"
    d.mkdir(parents=True)
    for kid, extra in items.items():
        (d / f"{kid}.yaml").write_text(f"id: {kid}\n" + BASE + extra, encoding="utf-8")
    return tmp_path


def test_reach_reasons(tmp_path):
    r = repo(tmp_path, {
        "K-0001": "confidence: confirmed\nscope: {work: [dev.describe]}\n",                 # 渡した
        "K-0002": "confidence: confirmed\nscope: {work: [dev.design.screen]}\n",           # 業務が合わない
        "K-0003": "confidence: hypothesis\nscope: {work: [dev.build]}\n",                  # 仮説
        "K-0004": "confidence: confirmed\nscope: {work: [dev.build], request_form: [one_off]}\n",   # 軸の食い違い
        "K-0005": "confidence: confirmed\nscope: {work: [dev.build]}\n",                   # 選ばれるのに渡っていない
    })
    used = [{"role": "implement", "ids": ["K-0001"], "work": ["dev.build", "dev.describe"], "values": {"request_form": ["ticket"], "roles": ["implement"]}}]
    got = cr.reach(r, [{"id": f"K-000{i}"} for i in range(1, 7)], used, TODAY)
    assert [(x["id"], x["delivered"], x.get("reason")) for x in got] == [
        ("K-0001", True, None), ("K-0002", False, "other_work"), ("K-0003", False, "hypothesis"),
        ("K-0004", False, "mismatch"), ("K-0005", False, "over_limit")]          # K-0006 は置き場にないので落とす
    assert got[0]["work"] == ["dev.build", "dev.describe"]


def test_reach_without_used_is_not_wired(tmp_path):
    r = repo(tmp_path, {"K-0001": "confidence: confirmed\nscope: {work: [dev.build]}\n"})
    assert cr.reach(r, [{"id": "K-0001"}], [], TODAY) == [{"id": "K-0001", "role": "implement", "work": ["dev.build", "dev.describe"],
                                                           "delivered": False, "reason": "not_wired"}]
    assert cr.reach(None, [{"id": "K-0001"}], [], TODAY) == []


def test_schema_and_prompt_ask_for_same_as_knowledge():
    import yaml
    tax = yaml.safe_load((Path(__file__).resolve().parents[2] / "definitions" / "learning_taxonomy.yaml").read_text(encoding="utf-8"))
    assert "same_as_knowledge" in cr.schema(tax)["required"]
    assert "knowledge_used" in cr.prompt(tax, {"kind": "sendback"})
