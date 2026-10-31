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

# 受け取った引数と標準入力（プロンプト）を記録し、--json-schema があれば structured_output を返す偽の claude
FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
stdin = sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(args + ["STDIN:" + stdin]) + "\\n")
if os.environ.get("FAKE_FAIL_ONCE") and not os.path.exists(os.environ["FAKE_FAIL_ONCE"]):
    open(os.environ["FAKE_FAIL_ONCE"], "w").close()
    sys.exit(1)
if "--resume" in args and os.environ.get("FAKE_RESUME_FAIL"):
    print("{}"); sys.exit(1)
out = {"result": "こんにちは", "total_cost_usd": 0.25, "usage": {"input_tokens": 3}, "num_turns": 2}
if "--dangerously-skip-permissions" in args and os.environ.get("HOME"):
    # 会話の記録を Claude Code と同じところに書く（作業ディレクトリごとのフォルダ）
    sid = "s-" + str(sum(1 for _ in open(os.environ["FAKE_LOG"])))
    d = os.path.join(os.environ["HOME"], ".claude", "projects", "-work-repo")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, sid + ".jsonl"), "w") as f:
        f.write(json.dumps({"resumed_from": args[args.index("--resume") + 1] if "--resume" in args else None}) + "\\n")
    out["session_id"] = sid
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
        self._old = {k: os.environ.get(k) for k in [*self.env, "FAKE_FAIL_ONCE", "FAKE_RC", "FAKE_RESUME_FAIL", "HOME"]}
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
        self.assertEqual(args[-1], "STDIN:問い")
        self.assertNotIn("問い", args[:-1])

    def test_long_prompt_does_not_hit_argument_limit(self):
        # 引数に載せると OS の上限（Argument list too long）に当たる長さ。標準入力なら通る
        r = ar.run("judge", "差分" * 200_000, schema={"type": "object"})
        self.assertEqual(r.output, {"ok": True})

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
        self.assertEqual(args[-1], "STDIN:作業して")   # プロンプトは標準入力で渡す
        self.assertNotIn("作業して", args[:-1])

    def test_state_dir_saves_and_resumes_session(self):
        home, state = Path(self.tmp.name) / "home", Path(self.tmp.name) / "handoff" / "agent"
        os.environ["HOME"] = str(home)
        r1 = ar.run_agentic("implement", "作業して", state_dir=str(state))
        self.assertFalse(r1.resumed)
        meta = json.loads((state / "claude-code" / "session.json").read_text())
        self.assertEqual(meta, {"session_id": "s-1", "project": "-work-repo"})
        # 別のコンテナ（記録のない HOME）でも、ボリュームの記録から続きを再開する
        os.environ["HOME"] = str(Path(self.tmp.name) / "home2")
        r2 = ar.run_agentic("implement", "続きを", state_dir=str(state))
        self.assertTrue(r2.resumed)
        args = self.calls()[1]
        self.assertEqual(args[args.index("--resume") + 1], "s-1")
        self.assertEqual(json.loads((state / "claude-code" / "session.json").read_text())["session_id"], "s-2")

    def test_resume_failure_falls_back_to_new_session(self):
        os.environ["HOME"] = str(Path(self.tmp.name) / "home")
        state = Path(self.tmp.name) / "agent"
        ar.run_agentic("implement", "作業して", state_dir=str(state))
        os.environ["FAKE_RESUME_FAIL"] = "1"
        r = ar.run_agentic("implement", "続きを", state_dir=str(state))
        self.assertEqual((r.exit, r.resumed, r.output), (0, False, "こんにちは"))
        self.assertIn("--resume", self.calls()[1])
        self.assertNotIn("--resume", self.calls()[2])

    def test_no_state_dir_never_resumes(self):
        os.environ["HOME"] = str(Path(self.tmp.name) / "home")
        ar.run_agentic("implement", "作業して")
        ar.run_agentic("implement", "作業して")
        self.assertNotIn("--resume", self.calls()[1])

    def test_cli_rejects_oneshot_step_for_exec(self):
        p = subprocess.run([sys.executable, str(RUNNER), "exec", "judge"], input="x", capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
