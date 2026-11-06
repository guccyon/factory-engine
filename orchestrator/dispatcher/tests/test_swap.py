"""工場の入れ替え（ADR 0031 の順番 2）: 依頼の記録、dispatcher が入れ替え役を起動すること、入れ替え役の手順（事前の確かめ・人に回す・上げ直し・確かめ・前の版に戻す）。

git と docker は偽物に差し替える。DB を使うものは空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）。
"""
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
HERE = os.path.dirname(__file__)
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
URL = os.environ.get("TEST_DATABASE_URL")
for k, v in {"DATABASE_URL": URL or "postgresql://x@localhost:1/x", "NOTIFIER": "log"}.items():
    os.environ.setdefault(k, v)

import swap  # noqa: E402
import swapper  # noqa: E402

needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")
RULES = swap.load_rules(REPO)
PREV, TARGET = "a" * 40, "b" * 40


# ------------------------------------------------------------------ 決まり（DB なし）
def test_classify_takes_the_heaviest_kind():
    assert swap.classify(["orchestrator/dispatcher/dispatcher.py", "definitions/planner.yaml"], RULES) == "code"
    assert swap.classify(["orchestrator/compose.yaml", "orchestrator/dispatcher/x.py"], RULES) == "control_plane"
    assert swap.classify(["orchestrator/compose.yaml", "orchestrator/postgres/init/006_swaps.sql"], RULES) == "db"
    assert swap.heavier("code", "control_plane") == "control_plane" and swap.heavier("db", "code") == "db"


def test_escalations_look_at_added_lines_only():
    diffs = {"orchestrator/compose.yaml": "@@\n-      - /var/run/docker.sock:/var/run/docker.sock\n+      MAX_TURNS: 40\n"}
    assert swap.escalations(list(diffs), diffs.get, RULES) == []   # ソケットのマウントを消すのは広げる変更ではない
    diffs = {"orchestrator/compose.yaml": "@@\n+    volumes:\n+      - /var/run/docker.sock:/var/run/docker.sock\n"}
    esc = swap.escalations(list(diffs), diffs.get, RULES)
    assert [e["id"] for e in esc] == ["secrets_permissions"] and esc[0]["files"] == ["orchestrator/compose.yaml"]
    assert swap.escalations(["orchestrator/dispatcher/dispatcher.py"], lambda f: "+GH_TOKEN = 1", RULES) == []   # 対象のファイルだけ
    diffs = {"orchestrator/.env.example": "@@\n+# EDGE_ALLOW_CIDRS=0.0.0.0/0 ::/0   # 説明\n+  # EDGE_COOKIE_SECRET=\n"}
    assert swap.escalations(list(diffs), diffs.get, RULES) == []   # 説明のコメント行は数えない
    diffs = {"orchestrator/.env.example": "@@\n-# EDGE_COOKIE_SECRET=\n+EDGE_COOKIE_SECRET=\n"}
    assert [e["id"] for e in swap.escalations(list(diffs), diffs.get, RULES)] == ["secrets_permissions"]   # コメントを外すのは数える


def test_git_mounts_for_worktree_and_plain_clone():
    root = "/Users/u/src/factory-engine/main"
    assert swap.git_mounts("gitdir: /Users/u/src/factory-engine/.bare/worktrees/main\n", root) == ["/Users/u/src/factory-engine/.bare"]
    assert swap.git_mounts("gitdir: /elsewhere/repo.git\n", root) == ["/elsewhere/repo.git"]
    assert swap.git_mounts("gitdir: .git/worktrees/x", root) == []   # 作業ツリーの中ならマウント済み
    assert swap.git_mounts("", root) == []


def test_launch_args_mount_the_tree_at_the_same_path_and_do_not_embed_secrets():
    args = swap.launch_args(7, "/h/fe", ["/h/.bare"], "factory-dispatcher:p0", "factory_default", "factory")
    s = " ".join(args)
    assert "--rm" in args and "--name factory-swap-7" in s and "-v /h/fe:/h/fe" in s and "-v /h/.bare:/h/.bare" in s
    assert "-w /h/fe/orchestrator" in s and "-v /var/run/docker.sock:/var/run/docker.sock" in s
    assert "-e GH_TOKEN" in s and "GH_TOKEN=" not in s and "DATABASE_URL=" not in s   # 値は dispatcher の環境から写す（引数に出さない）
    assert args[-4:] == ["python", "/app/swapper.py", "run", "7"]


def test_fetch_and_compare_urls():
    assert swapper.fetch_url("git@github.com:o/factory-engine.git") == "https://github.com/o/factory-engine.git"
    assert swapper.fetch_url("https://github.com/o/r.git") == "https://github.com/o/r.git"
    assert swapper.compare_url("git@github.com:o/r.git", "aaa", "bbb") == "https://github.com/o/r/compare/aaa...bbb"


def test_child_env_drops_secrets(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("SWAP_ROOT", "/x")
    env = swapper.child_env()
    assert "GH_TOKEN" not in env and "SWAP_ROOT" not in env and "DATABASE_URL" not in env
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"


# ------------------------------------------------------------------ 依頼と起動（DB）
@pytest.fixture()
def cur():
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


@needs_db
def test_request_is_one_at_a_time_and_cancellable(cur):
    sid = swap.request(cur, "main", "builder")
    assert swap.get(cur, sid)["status"] == "confirmed"
    with pytest.raises(swap.SwapError, match="終わっていない"):
        swap.request(cur, "main", "builder")
    assert swap.cancel(cur, sid, "builder")
    assert swap.active(cur) is None
    with pytest.raises(swap.SwapError):
        swap.request(cur, "not-a-sha", "builder")
    with pytest.raises(swap.SwapError):
        swap.request(cur, "main", "builder", kind="everything")


@needs_db
def test_rolled_back_version_is_not_retried(cur):
    sid = swap.request(cur, "main", "builder")
    swap.record(cur, sid, "swap.rolled_back", {"target_sha": "bbbbbbb", "prev": "aaaaaaa"})
    with pytest.raises(swap.SwapError, match="前の版に戻った"):
        swap.request(cur, TARGET, "builder")
    assert swap.request(cur, "c" * 40, "builder")


@needs_db
def test_tick_launches_once_and_flags_a_lost_swapper(cur):
    import stoppage
    sid = swap.request(cur, "main", "builder")
    launched = []
    swap.tick(cur, lambda *a: None, launch_fn=lambda i: launched.append(i) or swap.container(i), alive_fn=lambda i: True)
    swap.tick(cur, lambda *a: None, launch_fn=lambda i: launched.append(i) or swap.container(i), alive_fn=lambda i: True)
    assert launched == [sid] and swap.get(cur, sid)["status"] == "launched"
    # 入れ替え役が結果を書かずに消えた（コンテナがない）。起動の直後は待つ
    swap.tick(cur, lambda *a: None, alive_fn=lambda i: False)
    assert swap.get(cur, sid)["status"] == "launched"
    swap.tick(cur, lambda *a: None, alive_fn=lambda i: False, now=datetime.now(timezone.utc) + timedelta(seconds=swap.LOST_AFTER_SEC + 5))
    assert swap.get(cur, sid)["status"] == "failed"
    assert f"swap:{sid}" in stoppage.open_keys(cur, "swap:")


@needs_db
def test_tick_records_a_launch_failure(cur):
    sid = swap.request(cur, "main", "builder")

    def boom(i):
        raise swap.SwapError("dispatcher のコンテナが見つからない")
    swap.tick(cur, lambda *a: None, launch_fn=boom)
    s = swap.get(cur, sid)
    assert s["status"] == "failed" and "見つからない" in s["events"][-1]["payload"]["error"]


# ------------------------------------------------------------------ 入れ替え役の手順（DB と偽の git・docker）
class Fake:
    """偽の git・docker。イメージの今の名前が指す版（tagged）と、動いているサービスの版（live）を持つ"""
    def __init__(self, cur, files=("orchestrator/dispatcher/dispatcher.py",), diffs=None, broken=(), config_rc=0):
        self.cur, self.files, self.diffs, self.broken, self.config_rc = cur, list(files), diffs or {}, set(broken), config_rc
        self.tagged, self.live, self.head, self.calls = PREV[:7], PREV[:7], PREV, []
        self.images = {"factory-dispatcher:p0", "factory-shipper:p2", "factory-worker:p0", "factory-worker:design"}
        self.t = 0.0

    def run(self, args, cwd=None, timeout=0, check=True):
        self.calls.append(args)
        out, rc = "", 0
        a = args
        if a[:2] == ["git", "status"]:
            out = ""
        elif a[:3] == ["git", "remote", "get-url"]:
            out = "git@github.com:o/factory-engine.git"
        elif a[:3] == ["git", "rev-parse", "--verify"]:
            out = TARGET
        elif a[:3] == ["git", "rev-parse", "HEAD"]:
            out = self.head
        elif a[:3] == ["git", "diff", "--name-only"]:
            out = "\n".join(self.files)
        elif a[:2] == ["git", "diff"]:
            out = self.diffs.get(a[-1], "")
        elif a[:3] == ["git", "merge", "--ff-only"]:
            self.head = a[3]
        elif a[:3] == ["git", "reset", "--keep"]:
            self.head = a[3]
        elif a[:2] == ["docker", "compose"] and "config" in a:
            rc = self.config_rc
        elif a[:2] == ["bash", "scripts/build.sh"]:
            self.tagged = TARGET[:7]
        elif a[:3] == ["docker", "image", "inspect"]:
            rc = 0 if a[3] in self.images or a[3].startswith("sha256:") else 1
        elif a[:2] == ["docker", "tag"]:
            self.images.add(a[3])
            if a[2] in ("sha256:" + PREV[:7], "factory-dispatcher:p0") and a[3].endswith("-" + PREV[:7]):
                self.images.add(a[3])
            elif a[2].endswith("-" + PREV[:7]) and a[3] == "factory-dispatcher:p0":
                self.tagged = PREV[:7]
        elif a[:2] == ["docker", "compose"] and "up" in a:
            self.live = self.tagged
            if self.live not in self.broken:   # 壊れた版は supervisor・dispatcher が回らない
                self.cur.execute("insert into events(name, payload) values ('supervisor.heartbeat', jsonb_build_object('engine_version', %s::text)),"
                                 " ('dispatcher.started', jsonb_build_object('engine_version', %s::text))", (self.live, self.live))
        elif a[:2] == ["docker", "images"]:
            out = "\n".join(sorted(self.images))
        if check and rc:
            raise RuntimeError(f"{a} failed")
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")

    def list_running(self, project):
        state = "exited" if self.live in self.broken else "running"
        mk = lambda n, img: {"kind": "service", "name": n, "state": state, "image": img, "image_id": "sha256:" + self.live, "health": None,
                             "external": False, "version": self.live}
        return [mk("dispatcher", "factory-dispatcher:p0"), mk("console", "factory-shipper:p2"),
                {"kind": "service", "name": "postgres", "state": "running", "image": "postgres:17-alpine", "image_id": "x", "health": "healthy",
                 "external": True, "version": "-"}]

    def sleep(self, s):
        self.t += s

    def clock(self):
        return self.t


def make(cur, tmp_path, fake, kind="code"):
    root = tmp_path / "fe"
    (root / "definitions").mkdir(parents=True)
    (root / "orchestrator" / "scripts").mkdir(parents=True)
    shutil.copy(os.path.join(REPO, "definitions", "swap.yaml"), root / "definitions" / "swap.yaml")
    shutil.copy(os.path.join(REPO, "orchestrator", "scripts", "build.sh"), root / "orchestrator" / "scripts" / "build.sh")
    sid = swap.request(cur, "main", "builder", kind=kind)
    swap.record(cur, sid, "swap.launched", {"container": swap.container(sid)})
    import psycopg
    from psycopg.rows import dict_row
    return sid, swapper.Swapper(sid, str(root), "factory", run=fake.run, http=lambda u: 200 if "console" in u else 401,
                                list_running=fake.list_running, db=lambda: psycopg.connect(URL, row_factory=dict_row, autocommit=True),
                                sleep=fake.sleep, clock=fake.clock)


def names(cur, sid):
    return [e["name"] for e in swap.get(cur, sid)["events"]]


@needs_db
def test_code_swap_goes_through_without_draining(cur, tmp_path):
    import drain
    fake = Fake(cur)
    sid, sw = make(cur, tmp_path, fake)
    assert sw.main() == 0
    assert names(cur, sid)[-4:] == ["swap.started", "swap.applied", "swap.verified", "swap.completed"]
    assert fake.head == TARGET and fake.live == TARGET[:7]
    assert not drain.active(cur)
    assert "factory-dispatcher:p0-" + PREV[:7] in fake.images   # 前の版の名前を残した（戻すときに作り直さない）
    ups = [c for c in fake.calls if c[:2] == ["docker", "compose"] and "up" in c]
    assert ups and "--no-build" in ups[0] and not any(s in ups[0] for s in ("dispatcher", "console"))   # すべてのサービスを上げる
    # 同じ swap をもう一度起動しても何もしない
    assert sw.main() == 2


@needs_db
def test_control_plane_swap_drains_and_resumes(cur, tmp_path):
    import drain
    fake = Fake(cur, files=["orchestrator/compose.yaml"], diffs={"orchestrator/compose.yaml": "+      MAX_TURNS: 40\n"})
    sid, sw = make(cur, tmp_path, fake)
    assert sw.main() == 0
    evs = names(cur, sid)
    assert "swap.drained" in evs and evs[-1] == "swap.completed"
    assert swap.get(cur, sid)["events"][-1]["payload"]["kind"] == "control_plane"
    assert not drain.active(cur)   # 一時停止を解いた
    cur.execute("select payload->>'by' as by from events where name like 'factory.drain%%' order by id")
    assert [r["by"] for r in cur.fetchall()] == ["swapper", "swapper"]


@needs_db
def test_db_change_stops_before_touching_anything_and_goes_to_the_inbox(cur, tmp_path):
    import inbox
    fake = Fake(cur, files=["orchestrator/postgres/init/006_swaps.sql", "orchestrator/dispatcher/swap.py"])
    sid, sw = make(cur, tmp_path, fake, kind="code")   # 申告は code でも差分で db
    assert sw.main() == 3
    assert names(cur, sid)[-1] == "swap.escalated"
    assert fake.head == PREV and not any(c[:2] == ["bash", "scripts/build.sh"] for c in fake.calls)
    items = [i for i in inbox.items(cur) if i["key"] == f"task:swap:{sid}:db"]
    assert items and items[0]["who"] == "owner" and "006_swaps.sql" in items[0]["action"]
    assert items[0]["url"].endswith(f"/compare/{PREV[:7]}...{TARGET[:7]}")


@needs_db
def test_secret_or_permission_change_escalates(cur, tmp_path):
    import stoppage
    fake = Fake(cur, files=["orchestrator/compose.yaml"], diffs={"orchestrator/compose.yaml": "+      - /var/run/docker.sock:/var/run/docker.sock\n"})
    sid, sw = make(cur, tmp_path, fake)
    assert sw.main() == 3
    assert f"swap:{sid}" in stoppage.open_keys(cur, "swap:")
    assert fake.head == PREV


@needs_db
def test_missing_env_var_escalates_and_restores_nothing_changed(cur, tmp_path):
    fake = Fake(cur, config_rc=1)
    sid, sw = make(cur, tmp_path, fake)
    assert sw.main() == 3
    assert "compose.yaml が今の .env で通らない" in swap.get(cur, sid)["events"][-1]["payload"]["reason"]
    assert fake.head == PREV and fake.live == PREV[:7]


@needs_db
def test_broken_version_is_rolled_back(cur, tmp_path):
    import drain
    import inbox
    fake = Fake(cur, broken={TARGET[:7]})
    sid, sw = make(cur, tmp_path, fake)
    assert sw.main() == 1
    s = swap.get(cur, sid)
    assert s["status"] == "rolled_back" and "supervisor" in s["events"][-1]["payload"]["reason"]
    assert fake.head == PREV and fake.live == PREV[:7]   # 作業ツリーもイメージも前の版
    assert not drain.active(cur)
    assert any(i["key"] == f"task:swap:{sid}:rolled_back" and i["who"] == "builder" for i in inbox.items(cur))
    with pytest.raises(swap.SwapError):
        swap.request(cur, TARGET, "builder")   # 同じ版では自動で入れ替えない


@needs_db
def test_rollback_that_also_fails_keeps_the_line_drained(cur, tmp_path):
    import drain
    import stoppage
    fake = Fake(cur, broken={TARGET[:7], PREV[:7]})
    sid, sw = make(cur, tmp_path, fake)
    assert sw.main() == 3
    assert swap.get(cur, sid)["status"] == "escalated"
    assert drain.active(cur)   # 壊れた工場では流さない
    assert f"swap:{sid}" in stoppage.open_keys(cur, "swap:")


def test_build_removes_only_its_own_worktree(tmp_path):
    """入れ替え役のコンテナからはホストの他の worktree が見えない。作り終えたあと、見えない worktree の登録を消さない"""
    import subprocess
    root = tmp_path / "fe"
    g = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True, text=True).stdout
    root.mkdir()
    g("init", "-q")
    g("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "x")
    sha = g("rev-parse", "HEAD").strip()
    hidden = tmp_path / "hidden"
    g("worktree", "add", "-q", "--detach", str(hidden), sha)
    shutil.rmtree(hidden)   # このコンテナからは見えない（ホストにはある）worktree
    (root / "orchestrator").mkdir()

    def run(args, cwd=None, timeout=0, check=True):
        if args[0] == "git":
            r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
            if check and r.returncode:
                raise RuntimeError(r.stderr)
            return r
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    s = swapper.Swapper(4242, str(root), "factory", run=run)
    s.build(sha)
    listed = g("worktree", "list", "--porcelain")
    assert str(hidden) in listed
    assert "swap-src-4242" not in listed and not os.path.exists("/tmp/swap-src-4242")
