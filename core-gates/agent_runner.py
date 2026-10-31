#!/usr/bin/env python3
"""工程からエージェントを呼ぶ唯一の入口（ADR 0010）。

工程は step の名前とプロンプトだけを渡し、runner・モデルは definitions/agents.yaml で決める。
出力は runner によらず同じ形（Result）にそろえる。いま実装済みの runner は claude-code だけ。

Python から:
    from agent_runner import run
    r = run("judge", prompt, schema=SCHEMA)   # r.output は schema があれば dict、なければ文字列
シェルから:
    agent_runner.py resolve implement --tier tier-small          # {"step","tier","runner","model",...}
    agent_runner.py run clarify.asker --schema '<json>' < prompt  # {"output","cost_usd","usage","model","runner"}
    agent_runner.py exec implement --max-turns 40 < prompt        # ツールありで作業させる。終了コードは runner のもの
    agent_runner.py exec implement --state-dir /handoff/agent < prompt   # runner の会話の記録をこの下に保存し、あれば続きから再開する

--state-dir は「あれば使う近道」（ADR 0017）。続きのジョブの正は工場のボリュームの引き継ぎの記録（ticket.json・jobs/）で、
runner の記録がなくても、壊れていても、別の runner に替えても続きは作れる。中身の形は runner ごとに <state-dir>/<runner>/ の下で閉じる。

依存: python3（標準ライブラリ）。agents.yaml は PyYAML があればそれで、なければ ruby で読む。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENTS_FILE = Path(os.environ.get("FACTORY_AGENTS") or HERE.parent / "definitions" / "agents.yaml")
RETRY_BASE = float(os.environ.get("AGENT_RETRY_BASE", "20"))   # 一時的な失敗（レート制限とみられる rc=1）は 20・40・80 秒待って繰り返す


class AgentError(RuntimeError):
    pass


@dataclass
class Agent:
    step: str
    tier: str | None
    runner: str
    model: str
    mode: str = "oneshot"   # oneshot（ツールなしで 1 回答える）／ agentic（ツールありで作業する）
    tools: str = "default"  # default ／ none（ツールを渡さない）


@dataclass
class Result:
    output: object
    cost_usd: float = 0.0
    usage: dict = field(default_factory=dict)
    model: str = ""
    runner: str = ""
    num_turns: int = 0
    exit: int = 0
    resumed: bool = False   # runner の会話の記録から続きを再開した（--state-dir の近道が効いた）


def load_agents(path: Path = AGENTS_FILE) -> dict:
    try:
        import yaml  # type: ignore
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except ImportError:
        r = subprocess.run(["ruby", "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.load_file(ARGV[0]))", str(path)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise AgentError(f"cannot read {path}: install PyYAML or ruby ({r.stderr[-200:]})")
        return json.loads(r.stdout)


def resolve(step: str, tier: str | None = None, conf: dict | None = None) -> Agent:
    """step の設定から runner とモデルを決める。tier を渡すと工程の既定の tier を上書きする（MODEL_TIER・--tier）"""
    conf = conf or load_agents()
    s = (conf.get("steps") or {}).get(step)
    if s is None:
        raise AgentError(f"unknown step: {step}")
    tier = tier or s.get("tier")
    t = (conf.get("tiers") or {}).get(tier, {}) if tier else {}
    if tier and not t and not (s.get("runner") and s.get("model")):
        raise AgentError(f"unknown tier: {tier}")
    runner = s.get("runner") or t.get("runner")
    model = s.get("model") or t.get("model")
    if not runner or not model:
        raise AgentError(f"step {step} has no runner/model")
    status = ((conf.get("runners") or {}).get(runner) or {}).get("status")
    if status != "implemented":
        raise AgentError(f"runner {runner} is not implemented (step {step})")
    return Agent(step=step, tier=tier, runner=runner, model=model, mode=s.get("mode", "oneshot"), tools=s.get("tools", "default"))


def _retry(cmd: list[str], *, input: str | None, retries: int, cwd: str | None = None) -> str:
    last = None
    for i in range(retries + 1):
        r = subprocess.run(cmd, input=input, capture_output=True, text=True, cwd=cwd)
        if r.returncode == 0:
            return r.stdout
        last = r
        if i < retries:
            time.sleep(RETRY_BASE * (2 ** i))
    raise AgentError(f"{cmd[0]} failed (rc={last.returncode}): {last.stderr[-300:]}")


# --- runner: claude-code -------------------------------------------------------------------------------

def _claude_oneshot(a: Agent, prompt: str, schema: dict | None, max_turns: int, retries: int) -> Result:
    # プロンプトは標準入力で渡す。引数に載せると、長い差分を含む judge などで OS の上限（Argument list too long）に当たる
    cmd = ["claude", "-p", "--model", a.model, "--output-format", "json", "--max-turns", str(max_turns)]
    if a.tools == "none":
        cmd += ["--tools", ""]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema)]
    # --bare はキーチェーンの認証を読まないので使わない（Mac で動かす shadow_review・pairwise_judge）
    res = json.loads(_retry(cmd, input=prompt, retries=retries))
    if schema is not None:
        out = res.get("structured_output") or res.get("result")
        if isinstance(out, str):
            out = json.loads(out)
    else:
        out = res.get("result") or ""
    return Result(output=out, cost_usd=res.get("total_cost_usd") or 0.0, usage=res.get("usage") or {},
                  model=a.model, runner=a.runner, num_turns=res.get("num_turns") or 0)


def _claude_projects() -> Path:
    return Path.home() / ".claude" / "projects"   # Claude Code が会話の記録を置くところ（作業ディレクトリごとのフォルダ）


def _claude_restore(state: Path | None) -> str | None:
    """前のジョブの会話の記録を ~/.claude/projects に戻し、そのセッション id を返す。なければ None"""
    if state is None:
        return None
    try:
        meta = json.loads((state / "session.json").read_text(encoding="utf-8"))
        sid, project = str(meta["session_id"]), str(meta["project"])
        src = state / f"{sid}.jsonl"
        if not src.is_file() or "/" in project or "/" in sid:
            return None
        dst = _claude_projects() / project
        dst.mkdir(parents=True, exist_ok=True)
        (dst / src.name).write_bytes(src.read_bytes())
        return sid
    except (OSError, ValueError, KeyError):
        return None


def _claude_save(state: Path | None, sid: str | None):
    """会話の記録を state に写す（次の続きのジョブの近道）。失敗してもジョブは止めない"""
    if state is None or not sid:
        return
    try:
        found = next(iter(_claude_projects().glob(f"*/{sid}.jsonl")), None)
        if found is None:
            return
        state.mkdir(parents=True, exist_ok=True)
        (state / found.name).write_bytes(found.read_bytes())
        (state / "session.json").write_text(json.dumps({"session_id": sid, "project": found.parent.name}), encoding="utf-8")
    except OSError as e:
        print(f"[agent_runner] session save failed: {e}", file=sys.stderr)


def _claude_agentic(a: Agent, prompt: str, max_turns: int, cwd: str | None, state_dir: str | None = None) -> Result:
    # ワーカーの中だけで使う（使い捨てのコンテナなので権限の確認を省く）。stderr はそのまま流す
    state = Path(state_dir) / "claude-code" if state_dir else None
    base = ["claude", "-p", "--model", a.model, "--max-turns", str(max_turns), "--output-format", "json", "--dangerously-skip-permissions"]
    sid = _claude_restore(state)
    resumed = bool(sid)
    r = subprocess.run(base + (["--resume", sid] if sid else []), input=prompt, stdout=subprocess.PIPE, text=True, cwd=cwd)
    try:
        res = json.loads(r.stdout)
    except json.JSONDecodeError:
        res = {}
    if resumed and r.returncode != 0 and not res.get("num_turns"):
        # 記録から再開できなかった（版の違い・記録の破損）。近道をあきらめて新しい会話で続ける（引き継ぎの記録はプロンプトにある）
        print("[agent_runner] resume failed; starting a new session", file=sys.stderr)
        resumed = False
        r = subprocess.run(base, input=prompt, stdout=subprocess.PIPE, text=True, cwd=cwd)
        try:
            res = json.loads(r.stdout)
        except json.JSONDecodeError:
            res = {}
    _claude_save(state, res.get("session_id"))
    return Result(output=res.get("result") or "", cost_usd=res.get("total_cost_usd") or 0.0, usage=res.get("usage") or {},
                  model=a.model, runner=a.runner, num_turns=res.get("num_turns") or 0, exit=r.returncode, resumed=resumed)


ONESHOT = {"claude-code": _claude_oneshot}
AGENTIC = {"claude-code": _claude_agentic}


def run(step: str, prompt: str, *, schema: dict | None = None, tier: str | None = None,
        max_turns: int = 3, retries: int = 3) -> Result:
    """ツールなしで 1 回答えさせる（判定・追記・問い）"""
    a = resolve(step, tier)
    return ONESHOT[a.runner](a, prompt, schema, max_turns, retries)


def run_agentic(step: str, prompt: str, *, tier: str | None = None, max_turns: int = 40, cwd: str | None = None,
                state_dir: str | None = None) -> Result:
    """ツールありで作業させる（実装）。失敗しても例外にせず exit に runner の終了コードを入れて返す。
    state_dir があれば runner の会話の記録をそこに残し、次はそこから再開する（なくても動く近道）"""
    a = resolve(step, tier)
    if a.mode != "agentic":
        raise AgentError(f"step {step} is not agentic")
    return AGENTIC[a.runner](a, prompt, max_turns, cwd, state_dir)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("resolve", "run", "exec"):
        sp = sub.add_parser(name)
        sp.add_argument("step")
        sp.add_argument("--tier", default=os.environ.get("AGENT_TIER") or None)
        if name == "run":
            sp.add_argument("--schema", help="JSON Schema（文字列）。渡すと output は JSON")
            sp.add_argument("--max-turns", type=int, default=3)
        if name == "exec":
            sp.add_argument("--max-turns", type=int, default=40)
            sp.add_argument("--state-dir", help="runner の会話の記録を置くところ（続きのジョブの近道。ADR 0017）")
    a = p.parse_args(argv)
    try:
        if a.cmd == "resolve":
            print(json.dumps(asdict(resolve(a.step, a.tier)), ensure_ascii=False))
            return 0
        prompt = sys.stdin.read()
        if a.cmd == "run":
            r = run(a.step, prompt, schema=json.loads(a.schema) if a.schema else None, tier=a.tier, max_turns=a.max_turns)
        else:
            r = run_agentic(a.step, prompt, tier=a.tier, max_turns=a.max_turns, state_dir=a.state_dir)
    except AgentError as e:
        print(f"[agent_runner] {e}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(r), ensure_ascii=False))
    return r.exit


if __name__ == "__main__":
    sys.exit(main())
