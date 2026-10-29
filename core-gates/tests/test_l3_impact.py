"""l3_impact の割り当てと再現性を、手で作った小さなリポジトリで確かめる。

  python3 -m unittest discover -s core-gates/tests
"""
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("l3_impact", Path(__file__).resolve().parent.parent / "l3_impact.py")
l3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(l3)

CONTEXT_MAP = """contexts:
  - { id: identity }
  - { id: scheduling }
  - { id: entitlement }
  - { id: live-lesson }
"""
GLOSSARY = """terms:
  - { id: coach, context: identity }
  - { id: booking, context: scheduling }
  - { id: slot, context: scheduling }
  - { id: live_lesson, context: live-lesson }
"""
SCENARIOS = """scenarios:
  - { id: cs-001, expected_contexts: [scheduling] }
"""
CONFIG = """max_contexts: 1
keywords:
  quota: entitlement
weak: [coach]
paths:
  "app/interface/message.rs": shared
module_roots: [app/domain]
"""


def sh(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


def write(repo, path, text="x\n"):
    p = Path(repo) / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class L3ImpactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        sh(self.repo, "init", "-q", "-b", "main")
        sh(self.repo, "config", "user.email", "t@example.com")
        sh(self.repo, "config", "user.name", "t")
        write(self.repo, "specs/context-map.yaml", CONTEXT_MAP)
        write(self.repo, "specs/glossary.yaml", GLOSSARY)
        write(self.repo, "specs/change-scenarios.yaml", SCENARIOS)
        write(self.repo, ".factory-config/l3.yaml", CONFIG)
        write(self.repo, "app/domain/scheduling/booking.rs")
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-q", "-m", "base")
        sh(self.repo, "checkout", "-q", "-b", "pr")

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, files: dict):
        for path, text in files.items():
            write(self.repo, path, text)
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-q", "-m", "change")

    def measure(self, **kw):
        return l3.measure(self.repo, "main", "HEAD", **kw)

    def test_tokens_split_camel_and_plural(self):
        self.assertEqual(l3.tokens("apps/admin-web/coachBookings.test"), ["app", "admin", "web", "coach", "booking", "test"])

    def test_directory_owns_the_file(self):
        self.commit({"app/domain/entitlement/live_lesson_quota.rs": "x\n"})
        r = self.measure(force=True)
        self.assertEqual(r["by_file"]["app/domain/entitlement/live_lesson_quota.rs"], ["entitlement"])

    def test_name_is_used_in_flat_directories_with_longest_match(self):
        self.commit({"app/infra/pg_live_lesson_slots.rs": "x\n", "app/infra/quota_values.rs": "x\n"})
        r = self.measure(force=True)
        self.assertEqual(r["by_file"]["app/infra/pg_live_lesson_slots.rs"], ["live-lesson", "scheduling"])
        self.assertEqual(r["by_file"]["app/infra/quota_values.rs"], ["entitlement"])

    def test_weak_word_only_when_nothing_else_matches(self):
        self.commit({"app/interface/coach_bookings.rs": "x\n", "app/interface/coaches.rs": "x\n",
                     "app/student-coach/lib.rs": "x\n"})
        r = self.measure(force=True)
        self.assertEqual(r["by_file"]["app/interface/coach_bookings.rs"], ["scheduling"])
        self.assertEqual(r["by_file"]["app/interface/coaches.rs"], ["identity"])
        self.assertEqual(r["by_file"]["app/student-coach/lib.rs"], [l3.SHARED])   # ディレクトリでは weak を使わない

    def test_paths_override_and_shared(self):
        self.commit({"app/interface/message.rs": "x\n", "README.md": "x\n"})
        r = self.measure(force=True)
        self.assertEqual(r["by_file"]["app/interface/message.rs"], [l3.SHARED])
        self.assertEqual(r["contexts"], [])

    def test_migration_tables_but_not_references(self):
        self.commit({"app/infra/migrations/0002_expand.sql":
                     "-- slot は触らない\nALTER TABLE bookings ADD COLUMN coach_id uuid REFERENCES coaches(id);\n"})
        r = self.measure()
        self.assertIn("schema_diff", r["trigger"])
        self.assertEqual(r["by_file"]["app/infra/migrations/0002_expand.sql"], ["scheduling"])

    def test_tests_are_counted_apart(self):
        self.commit({"app/domain/scheduling/slot.rs": "x\n", "app/interface/tests/quota.rs": "x\n"})
        r = self.measure(force=True)
        self.assertEqual(r["contexts"], ["scheduling"])
        self.assertEqual(r["test_contexts"], ["entitlement"])
        self.assertEqual(r["test_files"], 1)

    def test_skip_when_no_trigger(self):
        self.commit({"app/domain/scheduling/slot.rs": "x\n"})
        self.assertEqual(self.measure()["verdict"], "skip")

    def test_new_module_fires(self):
        self.commit({"app/domain/billing/payout.rs": "x\n"})
        r = self.measure()
        self.assertEqual(r["new_modules"], ["app/domain/billing"])
        self.assertIn("new_module", r["trigger"])

    def test_scenario_flags_unexpected_context(self):
        self.commit({"app/domain/scheduling/slot.rs": "x\n", "app/domain/entitlement/quota.rs": "x\n"})
        r = self.measure(scenario_id="cs-001")
        self.assertEqual(r["verdict"], "flag")
        self.assertEqual(r["scenario"]["unexpected"], ["entitlement"])

    def test_max_contexts_without_scenario(self):
        self.commit({"app/domain/scheduling/slot.rs": "x\n", "app/domain/entitlement/quota.rs": "x\n"})
        self.assertEqual(self.measure(force=True)["verdict"], "flag")

    def test_same_diff_same_digest(self):
        self.commit({"app/domain/scheduling/slot.rs": "x\n", "app/infra/quota_values.rs": "x\n"})
        a, b = self.measure(force=True), self.measure(force=True)
        self.assertEqual(a, b)
        self.assertTrue(a["digest"].startswith("sha256:"))

    def test_unknown_context_in_config_is_an_error(self):
        write(self.repo, ".factory-config/l3.yaml", "keywords:\n  payout: billing\n")
        sh(self.repo, "commit", "-q", "-am", "bad")
        with self.assertRaises(ValueError):
            self.measure(force=True)


if __name__ == "__main__":
    unittest.main()
