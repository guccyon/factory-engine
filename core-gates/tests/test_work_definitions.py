"""業務のツリー（definitions/work.yaml）と知見の軸（definitions/knowledge.yaml の axes）の食い違いを見る（ADR 0040）"""
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))
import knowledge_check as kc  # noqa: E402

DEFS = ENGINE / "definitions"


def load(name):
    return yaml.safe_load((DEFS / name).read_text(encoding="utf-8"))


WORK = load("work.yaml")
NODES = WORK["nodes"]


def mapped_nodes(table):
    out = []
    for v in table.values():
        if v is None:
            continue
        if isinstance(v, list):
            out += v
            continue
        out += v.get("default") or []
        for nodes in (v.get("by_path") or {}).values():
            out += nodes or []
    return out


def test_every_node_has_its_parent():
    for nid in NODES:
        if "." in nid:
            assert nid.rsplit(".", 1)[0] in NODES, nid


def test_nodes_have_known_domain_and_kind():
    for nid, n in NODES.items():
        assert n.get("domain") in WORK["domains"], nid
        assert n.get("kind", "flow") in ("flow", "resource"), nid
        assert re.match(r"^[a-z_]+(\.[a-z_]+)*$", nid), nid


def test_checker_copy_matches_work_yaml():
    assert sorted(kc.WORK_NODES) == sorted(NODES)


def test_artifact_vocabulary_matches_artifacts_yaml():
    types = set(load("artifacts.yaml")["types"])
    parts = set(WORK["artifact_parts"])
    assert all(p.split(".", 1)[0] in types for p in parts)
    assert set(kc.ARTIFACT_TYPES) == types | parts
    for nid, n in NODES.items():
        for a in n.get("artifacts") or []:
            assert a in types | parts, (nid, a)


def test_closed_axes_match_knowledge_yaml():
    kdefs = load("knowledge.yaml")
    axes = kdefs["axes"]
    for axis, vocab in kc.CLOSED_AXES.items():
        assert axis in axes, axis
        v = axes[axis]["vocabulary"]
        if isinstance(v, list):
            assert v == vocab, axis
    stages = list(load("product_stages.yaml")["stages"])
    assert kc.CLOSED_AXES["product_stage"] == stages
    assert set(kdefs["knowledge"]["scope_keys"]) == set(kc.CLOSED_AXES) | set(kc.OPEN_AXES)
    assert list(kdefs["knowledge"]["acts_on"]) == kc.ACTS_ON


def test_every_entry_maps_to_known_nodes():
    for key in ("from_workflow_steps", "from_agent_steps", "from_continue_reasons", "from_desk_task_kinds"):
        for nid in mapped_nodes(WORK[key]):
            assert nid in NODES, (key, nid)


def test_every_step_and_entry_is_mapped():
    """ステップ・役・続きの理由・窓口のタスクの型を足したら、work.yaml の対応も足す（null で「業務を持たない」と明示する）"""
    wf = load("workflow.yaml")
    assert set(WORK["from_workflow_steps"]) == set(wf["steps"])
    for step, v in WORK["from_workflow_steps"].items():
        if isinstance(v, dict):
            assert set(v.get("by_path") or {}) <= set(wf["paths"]), step
    assert set(WORK["from_agent_steps"]) == set(load("agents.yaml")["steps"])
    assert set(WORK["from_desk_task_kinds"]) == set(load("desk.yaml")["task_kinds"])
    src = (ENGINE / "orchestrator" / "dispatcher" / "continuation.py").read_text(encoding="utf-8")
    reasons = re.search(r"^REASONS = \(([^)]*)\)", src, re.M).group(1)
    assert set(WORK["from_continue_reasons"]) == set(re.findall(r'"([a-z_]+)"', reasons))


def test_every_knowledge_entry_uses_a_mapped_role():
    """知見を選ぶ入口（knowledge_context.stage の役・CLI の --role）が from_agent_steps の業務を持つ役であること（ADR 0040 の決定 7）。
    役の名前を書き違えると業務が空になり、知見が 1 件も届かない"""
    roles = set()
    for p in (ENGINE / "core-gates").glob("*.py"):
        roles |= set(re.findall(r'knowledge_context\.stage\([^,]+,\s*[^,]+,\s*"([^"]+)"', p.read_text(encoding="utf-8")))
    for p in (ENGINE / "orchestrator" / "worker").glob("*.sh"):
        roles |= set(re.findall(r"knowledge_context\.py [^\n]*--role ([a-z_.]+)", p.read_text(encoding="utf-8")))
    assert {"implement", "body_fix", "plan", "desk.consult", "desk.answer", "ui_design", "design_system", "ui_design_review",
            "inspect", "judge", "qa"} <= roles
    for role in roles:
        assert WORK["from_agent_steps"].get(role), role


def test_dispatcher_work_lists_match_work_yaml():
    """dispatcher が依頼者の好みを選ぶときの業務は、work.yaml の役の業務と同じ"""
    for name, role in (("desk_line.py", "desk.consult"), ("desk_answer.py", "desk.answer")):
        src = (ENGINE / "orchestrator" / "dispatcher" / name).read_text(encoding="utf-8")
        listed = re.search(r'^[A-Z_]+_WORK = \[([^\]]*)\]', src, re.M).group(1)
        assert re.findall(r'"([a-z_.]+)"', listed) == WORK["from_agent_steps"][role], name


def test_dispatcher_copy_of_work_nodes_matches():
    """依頼者の好みの業務を検査する dispatcher の写し（definitions/ がないコンテナで使う）"""
    src = (ENGINE / "orchestrator" / "dispatcher" / "requester_prefs.py").read_text(encoding="utf-8")
    body = re.search(r"^WORK_NODES = \(([^)]*)\)", src, re.M).group(1)
    assert sorted(re.findall(r'"([a-z_.]+)"', body)) == sorted(NODES)
