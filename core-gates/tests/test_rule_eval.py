"""rule_eval の形の検査・eval ケースの実行・発火を、手で作った小さなリポジトリで確かめる。

  python3 -m unittest discover -s core-gates/tests
"""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("rule_eval", Path(__file__).resolve().parent.parent / "rule_eval.py")
re_ = importlib.util.module_from_spec(spec)
spec.loader.exec_module(re_)

POSITIVE = """diff --git a/apps/backend/src/interface/http/coach.rs b/apps/backend/src/interface/http/coach.rs
--- a/apps/backend/src/interface/http/coach.rs
+++ b/apps/backend/src/interface/http/coach.rs
@@ -10,2 +10,3 @@
 fn list() {
+    let rows = repo.all_accounts();
 }
"""
NEGATIVE = """diff --git a/apps/backend/src/interface/http/coach.rs b/apps/backend/src/interface/http/coach.rs
--- a/apps/backend/src/interface/http/coach.rs
+++ b/apps/backend/src/interface/http/coach.rs
@@ -10,2 +10,3 @@
 fn list() {
+    let rows = repo.coaches();
 }
"""


def rule_yaml(rid="OV-001", state="shadow", stage="crystallized", kind="diff_regex", pattern=r"all_accounts\\(", neg_expect="silent",
              confidentiality="product", paths='["apps/backend/**"]'):
    return f"""id: {rid}
title: "コーチの一覧はアカウント全件から取らない"
state: {state}
knowledge_stage: {stage}
confidentiality: {confidentiality}
severity: must
lessons: ["github:guccyon/rally#117"]
signature: "rubric:ARCH-002"
check:
  kind: {kind}
  pattern: "{pattern}"
  paths: {paths}
eval_cases:
  - {{ id: p1, polarity: positive, expect: fire, diff_ref: "file:rules/{rid}/positive-1.diff" }}
  - {{ id: n1, polarity: negative, expect: {neg_expect}, diff_ref: "file:rules/{rid}/negative-1.diff" }}
"""


class RuleEvalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / re_.RULES_DIR).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, text, rid="OV-001", pos=POSITIVE, neg=NEGATIVE):
        (self.repo / re_.RULES_DIR / f"{rid}.yaml").write_text(text, encoding="utf-8")
        d = self.repo / re_.EVALS_DIR / "rules" / rid
        d.mkdir(parents=True, exist_ok=True)
        (d / "positive-1.diff").write_text(pos, encoding="utf-8")
        (d / "negative-1.diff").write_text(neg, encoding="utf-8")

    def test_no_rules_is_green(self):
        rep = re_.evaluate(self.repo)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["rules"], [])

    def test_regex_rule_passes_its_cases(self):
        self.put(rule_yaml())
        rep = re_.evaluate(self.repo)
        self.assertTrue(rep["ok"], rep)
        r = rep["rules"][0]
        self.assertEqual([c["result"] for c in r["cases"]], ["fire", "silent"])
        self.assertEqual(r["cases"][0]["hits"][0]["line"], 11)

    def test_case_mismatch_fails(self):
        self.put(rule_yaml(neg_expect="fire"))
        rep = re_.evaluate(self.repo)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["rules"][0]["failed_cases"], ["n1"])

    def test_out_of_scope_path_is_silent(self):
        self.put(rule_yaml(paths='["apps/web/**"]', neg_expect="silent"))
        rep = re_.evaluate(self.repo)
        self.assertEqual(rep["rules"][0]["failed_cases"], ["p1"])

    def test_shape_errors(self):
        self.put(rule_yaml(confidentiality="secret", stage="crystallized", kind="rubric"))
        errs = re_.evaluate(self.repo)["rules"][0]["errors"]
        self.assertTrue(any("confidentiality" in e for e in errs))
        self.assertTrue(any("check.kind" in e for e in errs))

    def test_missing_negative_case(self):
        text = rule_yaml().split("  - { id: n1")[0]
        self.put(text)
        errs = re_.evaluate(self.repo)["rules"][0]["errors"]
        self.assertTrue(any("negative がない" in e for e in errs))

    def test_file_name_must_match_id(self):
        (self.repo / re_.RULES_DIR / "OV-002.yaml").write_text(rule_yaml(rid="OV-001"), encoding="utf-8")
        errs = re_.evaluate(self.repo)["rules"][0]["errors"]
        self.assertTrue(any("ファイル名" in e for e in errs))

    def test_diff_ref_cannot_escape_evals(self):
        self.put(rule_yaml().replace("file:rules/OV-001/positive-1.diff", "file:../../etc/passwd"))
        rep = re_.evaluate(self.repo)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("外を指して" in e for e in rep["rules"][0]["errors"]))

    def test_rubric_rule_is_judged_by_factory(self):
        self.put(rule_yaml(stage="rubric", kind="rubric"))
        r = re_.evaluate(self.repo)["rules"][0]
        self.assertTrue(r["ok"], r)
        self.assertEqual({c["result"] for c in r["cases"]}, {"factory"})

    def test_retired_rule_is_not_run(self):
        self.put(rule_yaml(state="retired", neg_expect="fire"))
        self.assertTrue(re_.evaluate(self.repo)["ok"])

    def test_digest_is_stable_and_tracks_check(self):
        a = {"id": "OV-001", "check": {"kind": "diff_regex", "pattern": "x"}, "eval_cases": []}
        b = dict(a, state="adopted", history=["moved"])
        self.assertEqual(re_.digest(a), re_.digest(b))
        self.assertNotEqual(re_.digest(a), re_.digest(dict(a, check={"kind": "diff_regex", "pattern": "y"})))

    def test_added_lines_ignores_plusplus_content(self):
        diff = "--- a/x.sql\n+++ b/x.sql\n@@ -1 +1,2 @@\n a\n+++ b\n"
        self.assertEqual(re_.added_lines(diff), [("x.sql", 2, "++ b")])

    def test_fire_on_pr(self):
        self.put(rule_yaml())
        self.put(rule_yaml(rid="OV-002", state="candidate"), rid="OV-002")
        sh = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)
        sh("init", "-q", "-b", "main")
        sh("-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
        sh("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "base")
        f = self.repo / "apps/backend/src/interface/http/coach.rs"
        f.parent.mkdir(parents=True)
        f.write_text("fn list() {\n    let rows = repo.all_accounts();\n}\n", encoding="utf-8")
        sh("add", ".")
        sh("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "head")
        rep = re_.fire_on_pr(self.repo, "HEAD~1", "HEAD")
        self.assertEqual([x["id"] for x in rep["fires"]], ["OV-001"])   # candidate の OV-002 は発火させない
        self.assertEqual(rep["fires"][0]["hits"][0]["path"], "apps/backend/src/interface/http/coach.rs")
        json.dumps(rep)


if __name__ == "__main__":
    unittest.main()
