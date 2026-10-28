"""agent_runner の解決と呼び出しを、偽の claude コマンドで確かめる（ADR 0010）。

  python3 -m unittest discover -s core-gates/tests
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "core-gates" / "agent_runner.py"
spec = importlib.util.spec_from_file_location("agent_runner", RUNNER)
ar = importlib.util.module_from_spec(spec)
sys.modules["agent_runner"] = ar   # dataclass が自分のモジュールを引くため
spec.loader.exec_module(ar)

# 受け取った引数を記録し、--json-schema があれば structured_output を返す偽の claude
FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(args) + "\\n")
if os.environ.get("FAKE_FAIL_ONCE") and not os.path.exists(os.environ["FAKE_FAIL_ONCE"]):
    open(os.environ["FAKE_FAIL_ONCE"], "w").close()
    sys.exit(1)
out = {"result": "こんにちは", "total_cost_usd": 0.25, "usage": {"input_tokens": 3}, "num_turns": 2}
if "--json-schema" in args:
    out["structured_output"] = {"ok": True}
print(json.dumps(out))
sys.exit(int(os.environ.get("FAKE_RC", "0")))
"""


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.conf = ar.load_agents()

    def test_every_step_resolves_to_an_implemented_runner(self):
        for step in self.conf["steps"]:
            a = ar.resolve(step, conf=self.conf)
            self.assertEqual(a.runner, "claude-code")
            self.assertTrue(a.model)

    def test_tier_override(self):
        self.assertEqual(ar.resolve("implement", "tier-small", self.conf).model, "claude-sonnet-5-5")
        self.assertEqual(ar.resolve("implement", conf=self.conf).model, "claude-opus-5-5")

    def test_step_can_name_runner_and_model_directly(self):
        conf = {**self.conf, "steps": {**self.conf["steps"], "x": {"runner": "claude-code", "model": "m-1"}}}
        a = ar.resolve("x", conf=conf)
        self.assertEqual((a.runner, a.model, a.tier), ("claude-code", "m-1", None))

    def test_not_implemented_runner_is_refused(self):
        conf = {**self.conf, "steps": {**self.conf["steps"], "review": {"runner": "codex", "model": "gpt"}}}
        with self.assertRaisesRegex(ar.AgentError, "not implemented"):
            ar.resolve("review", conf=conf)

    def test_unknown_step_and_tier(self):
        with self.assertRaises(ar.AgentError):
            ar.resolve("nope", conf=self.conf)
        with self.assertRaises(ar.AgentError):
            ar.resolve("judge", "tier-huge", self.conf)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        fake = d / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.log = d / "log.jsonl"
        self.env = {"PATH": f"{d}{os.pathsep}{os.environ['PATH']}", "FAKE_LOG": str(self.log)}
        self._old = {k: os.environ.get(k) for k in [*self.env, "FAKE_FAIL_ONCE", "FAKE_RC"]}
        os.environ.update(self.env)
        ar.RETRY_BASE = 0

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()]

    def test_oneshot_with_schema_and_no_tools(self):
        r = ar.run("judge", "問い", schema={"type": "object"})
        self.assertEqual(r.output, {"ok": True})
        self.assertEqual((r.cost_usd, r.model, r.runner), (0.25, "claude-opus-5-5", "claude-code"))
        args = self.calls()[0]
        self.assertEqual(args[args.index("--model") + 1], "claude-opus-5-5")
        self.assertIn("--tools", args)          # judge は tools: none
        self.assertIn("--json-schema", args)

    def test_oneshot_text_keeps_default_tools(self):
        r = ar.run("clarify.persona", "答えて")
        self.assertEqual(r.output, "こんにちは")
        args = self.calls()[0]
        self.assertNotIn("--tools", args)
        self.assertEqual(args[args.index("--model") + 1], "claude-sonnet-5-5")

    def test_retries_transient_failure(self):
        os.environ["FAKE_FAIL_ONCE"] = str(Path(self.tmp.name) / "failed")
        self.assertEqual(ar.run("inspect", "x", schema={}).output, {"ok": True})
        self.assertEqual(len(self.calls()), 2)

    def test_cli_exec_passes_exit_code_and_normalizes(self):
        env = {**os.environ, "FAKE_RC": "3"}
        p = subprocess.run([sys.executable, str(RUNNER), "exec", "implement", "--tier", "tier-small", "--max-turns", "5"],
                           input="作業して", capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 3)
        out = json.loads(p.stdout)
        self.assertEqual((out["output"], out["cost_usd"], out["num_turns"], out["model"]), ("こんにちは", 0.25, 2, "claude-sonnet-5-5"))
        args = self.calls()[0]
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertEqual(args[args.index("-p") + 1], "作業して")

    def test_cli_rejects_oneshot_step_for_exec(self):
        p = subprocess.run([sys.executable, str(RUNNER), "exec", "judge"], input="x", capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
