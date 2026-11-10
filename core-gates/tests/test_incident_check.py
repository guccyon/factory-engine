"""事故記録の決定論の検査（core-gates/incident_check.py、ADR 0046）"""
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import incident_check as ic  # noqa: E402

ENGINE = Path(__file__).resolve().parents[2]

GOOD = """id: FIR-2026-0001
title: 共有の追記ファイルの衝突の解消が止まらなかった
status: draft
resource: llm_tokens
mechanism_class: contention
also: [unbounded_repeat]
severity: high
affects:
  components: [衝突の解消の続きのジョブ]
symptom: [衝突の解消のジョブが大半を占める]
mechanism: "1 本マージされるたびに残りがすべて衝突した"
impact: {cost_vs_daily_budget: 2.5, share_of_spend: 0.8, noticed_by: builder}
mitigation:
  fixed_in: 7cb1fd71b2b678bd6ff2599be7a85981de6e2c25
  root_fix: 追記を PR ごとの断片にした（ADR 0027）
  generic_guards: [ticket_ceilings]
  dedicated_checks:
    - {name: 古い PR のための移行の処理, review_by: 2027-02-01}
local_ref: null
"""


def write(tmp_path: Path, files: dict[str, str], index: bool = True) -> Path:
    root = tmp_path / "incidents"
    root.mkdir()
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    if index:
        _, records = ic.run(root, [])
        (root / "INDEX.md").write_text(ic.build_index(records), encoding="utf-8")
    return root


def errors(tmp_path, text, deny=()):
    root = write(tmp_path, {"FIR-2026-0001.yaml": text})
    errs, _ = ic.run(root, list(deny))
    return errs


def test_good_record_passes(tmp_path):
    assert errors(tmp_path, GOOD) == []


def test_missing_required_and_bad_values(tmp_path):
    text = GOOD.replace("resource: llm_tokens", "resource: tokens").replace("severity: high\n", "")
    errs = errors(tmp_path, text)
    assert any("resource" in e for e in errs)
    assert any("severity がない" in e for e in errs)


def test_low_is_not_a_record(tmp_path):
    errs = errors(tmp_path, GOOD.replace("severity: high", "severity: low"))
    assert any("台帳だけ" in e for e in errs)


def test_published_needs_month(tmp_path):
    errs = errors(tmp_path, GOOD.replace("status: draft", "status: published"))
    assert any("published（YYYY-MM）" in e for e in errs)
    (tmp_path / "b").mkdir()
    assert errors(tmp_path / "b", GOOD.replace("status: draft", "status: published\npublished: 2026-11")) == []


def test_raw_cost_and_unknown_impact_key(tmp_path):
    text = GOOD.replace("noticed_by: builder}", "noticed_by: builder, usd: 1772}").replace("1 本マージされるたびに", "$1,772 を使い、1 本マージされるたびに")
    errs = errors(tmp_path, text)
    assert any("impact.usd" in e for e in errs)
    assert any("生の金額" in e for e in errs)


def test_identifying_shapes_are_rejected(tmp_path):
    for bad, label in [
        ("guccyon/rally#1228 で起きた", "Issue・PR の番号"),
        ("#337 で直した", "Issue・PR の番号"),
        ("https://github.com/x/y を見る", "URL"),
        ("mac-mini.local で動く", "ホスト名"),
        ("/Users/someone/src で動く", "個人のパス"),
        ("2026-11-02 に起きた", "日付"),
        ("11/2 に起きた", "日付"),
        ("a32fe9c で直した", "コミットのハッシュ"),
    ]:
        sub = tmp_path / label.replace("・", "_").replace("（", "").replace("）", "") / str(abs(hash(bad)))
        sub.mkdir(parents=True)
        errs = errors(sub, GOOD.replace("1 本マージされるたびに", bad + "。1 本マージされるたびに"))
        assert any(label in e for e in errs), (bad, errs)


def test_adr_numbers_and_fixed_in_are_allowed(tmp_path):
    assert errors(tmp_path, GOOD.replace("（ADR 0027）", "（ADR 0027・ADR 0038）")) == []


def test_deny_names(tmp_path):
    errs = errors(tmp_path, GOOD.replace("共有の追記ファイル", "Rally の追記ファイル"), deny=["rally"])
    assert any("割り出せる名前" in e for e in errs)


def test_local_ref_must_be_empty(tmp_path):
    errs = errors(tmp_path, GOOD.replace("local_ref: null", "local_ref: IC-0003"))
    assert any("local_ref" in e for e in errs)


def test_dedicated_check_needs_review_by(tmp_path):
    errs = errors(tmp_path, GOOD.replace(", review_by: 2027-02-01", ""))
    assert any("review_by" in e for e in errs)


def test_unknown_generic_guard_and_bad_fixed_in(tmp_path):
    text = GOOD.replace("generic_guards: [ticket_ceilings]", "generic_guards: [new_guard]").replace(
        "fixed_in: 7cb1fd71b2b678bd6ff2599be7a85981de6e2c25", "fixed_in: v1.2")
    errs = errors(tmp_path, text)
    assert any("new_guard" in e for e in errs)
    assert any("fixed_in" in e for e in errs)


def test_id_and_filename(tmp_path):
    root = write(tmp_path, {"FIR-2026-0002.yaml": GOOD})
    errs, _ = ic.run(root, [])
    assert any("ファイル名が違う" in e for e in errs)


def test_index_is_checked(tmp_path):
    root = write(tmp_path, {"FIR-2026-0001.yaml": GOOD}, index=False)
    errs, _ = ic.run(root, [])
    assert any("INDEX.md" in e for e in errs)
    assert ic.main(["--root", str(root), "--index"]) == 0
    assert ic.main(["--root", str(root)]) == 0


def test_values_match_definition():
    d = yaml.safe_load((ENGINE / "definitions" / "incident.yaml").read_text(encoding="utf-8"))
    assert ic.REQUIRED == d["record"]["required"]
    assert ic.STATUSES == list(d["record"]["status"])
    assert ic.RESOURCES == list(d["resources"])
    assert ic.MECHANISMS == list(d["mechanisms"])
    assert ic.SEVERITIES == list(d["severity"])
    assert ic.IMPACT_KEYS == d["impact_keys"]
    assert ic.NOTICED_BY == d["noticed_by"]
    assert ic.GENERIC_GUARDS == list(d["mitigation"]["generic_guards"])
    assert ic.MITIGATION_KEYS == d["mitigation"]["keys"]
    for v in d["viewpoints"]:
        assert v["resource"] in ic.RESOURCES and v["mechanism"] in ic.MECHANISMS


def test_engine_records_pass():
    root = ENGINE / "incidents"
    errs, records = ic.run(root, [])
    assert errs == []
    ids = {v["id"] for v in yaml.safe_load((ENGINE / "definitions" / "incident.yaml").read_text(encoding="utf-8"))["viewpoints"]}
    for _, r in records:
        for vp in (r.get("detection") or {}).get("viewpoints") or []:
            assert vp in ids


# ------------------------------------------------------------------ 専用のチェックの棚卸し（ADR 0046 の 5 の 3）
from datetime import date as _date  # noqa: E402

_ENGINE = Path(__file__).resolve().parents[2]
_INV = yaml.safe_load((_ENGINE / "definitions" / "incident.yaml").read_text(encoding="utf-8"))["dedicated_checks"]


def test_engine_inventory_matches_code_and_records():
    errs, records = ic.run(_ENGINE / "incidents", [])
    assert ic.check_inventory(_ENGINE, _INV, records) == []
    assert ic.main(["--root", str(_ENGINE / "incidents")]) == 0


def test_inventory_finds_removed_code_and_missing_entries():
    _, records = ic.run(_ENGINE / "incidents", [])
    gone = [{**_INV[0], "where": ["orchestrator/worker/entrypoint.sh:もう無い印"]}, *_INV[1:]]
    assert any("もう無い印" in e for e in ic.check_inventory(_ENGINE, gone, records))
    assert any("definitions/incident.yaml の dedicated_checks にない" in e for e in ic.check_inventory(_ENGINE, _INV[1:], records))
    later = [{**_INV[0], "review_by": "2027-05-01"}, *_INV[1:]]
    assert any("review_by が違う" in e for e in ic.check_inventory(_ENGINE, later, records))
    assert any("added がない" in e for e in ic.check_inventory(_ENGINE, [{k: v for k, v in _INV[0].items() if k != "added"}, *_INV[1:]], records))


def test_due_checks():
    assert ic.due_checks(_INV, _date(2026, 11, 10)) == []
    due = ic.due_checks(_INV, _date(2027, 1, 20))
    assert [d["fir"] for d in due] == ["FIR-2026-0001"] and due[0]["overdue"] is False
    assert all(d["overdue"] for d in ic.due_checks(_INV, _date(2027, 3, 1)))
