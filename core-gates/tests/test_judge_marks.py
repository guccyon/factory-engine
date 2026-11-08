"""工場自体の仕様だと決まる由来の印（ADR 0044 の決定 2 の 1）と、judge の spec_of の確定。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import judge  # noqa: E402


def product(tmp_path: Path, owned=None, acts_on="engine") -> Path:
    (tmp_path / ".factory-config/knowledge").mkdir(parents=True)
    man = "schema_version: '0.1'\n" + ("factory_owned_paths:\n" + "".join(f"  - \"{g}\"\n" for g in owned) if owned else "")
    (tmp_path / ".factory-config/manifest.yaml").write_text(man, encoding="utf-8")
    (tmp_path / ".factory-config/knowledge/K-0012.yaml").write_text(f"id: K-0012\nacts_on: {acts_on}\n", encoding="utf-8")
    return tmp_path


def test_enforce_issue_of_engine_knowledge_is_factory(tmp_path):
    # rally #1192（Issue #1122「担保: K-0012 を ci_check で決定論にする」。K-0012 は acts_on: engine）
    root = product(tmp_path)
    marks = judge.factory_marks(root, ["tools/pr-body-check/engine/x.patch", "apps/backend/src/lib.rs"], ["担保: K-0012 を ci_check で決定論にする"])
    assert len(marks) == 1 and "K-0012" in marks[0]


def test_enforce_issue_of_context_knowledge_is_not_marked(tmp_path):
    root = product(tmp_path, acts_on="context")
    assert judge.factory_marks(root, ["apps/x.ts"], ["担保: K-0012 を lint で決定論にする"]) == []


def test_all_files_in_owned_paths_is_factory(tmp_path):
    root = product(tmp_path, owned=["tools/pr-body-check/**", ".factory-config/*.yaml"])
    assert judge.factory_marks(root, ["tools/pr-body-check/a.mjs", ".factory-config/l3.yaml"], ["画面を直す"])[0].startswith("files:")
    assert judge.factory_marks(root, ["tools/pr-body-check/a.mjs", "apps/x.ts"], ["画面を直す"]) == []   # 製品のファイルが混ざれば印なし
    assert judge.factory_marks(root, [], ["画面を直す"]) == []


def test_apply_marks():
    out = {"provisional": [{"artifact_id": 1, "spec_of": "product"}, {"artifact_id": 2}], "defects": [{"index": 0, "spec_of": "factory"}]}
    judge.apply_marks(out, [])
    assert [(v["spec_of"], v["spec_basis"]) for v in out["provisional"]] == [("product", "judge"), ("factory", "default")]
    judge.apply_marks(out, ["knowledge: K-0012"])
    assert {(v["spec_of"], v["spec_basis"]) for v in out["provisional"] + out["defects"]} == {("factory", "mark: knowledge: K-0012")}
