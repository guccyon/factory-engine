"""知見の番号の付け直し（core-gates/knowledge_renumber.py）"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import knowledge_renumber as kr  # noqa: E402

R = kr.ROOT


def sh(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def write(repo: Path, files: dict[str, str]):
    for name, text in files.items():
        f = repo / R / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")


def commit(repo, msg):
    sh(repo, "add", "-A")
    sh(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", msg)


def setup(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    sh(repo, "init", "-q", "-b", "main")
    # 付け直しのコミットは本体が打つので、身元はリポジトリに持たせる（CI の runner には git の身元が無い）
    sh(repo, "config", "user.name", "t")
    sh(repo, "config", "user.email", "t@example.invalid")
    write(repo, {"README.md": "x\n", "INDEX.md": "まだない\n"})
    commit(repo, "init")
    sh(repo, "checkout", "-q", "-b", "topic")
    write(repo, {"K-0001.yaml": "id: K-0001\nevidence:\n  - case: C-0001\nrelated: [K-0002]\ndecided_by: committee:D-0001\n",
                 "K-0002.yaml": "id: K-0002\nevidence:\n  - case: C-0001\nrelated: [K-0001]\n",
                 "cases/C-0001.md": "# C-0001\nK-0001 と K-0002 を足した。K-00010 は別物\n",
                 "committee/D-0001.yaml": "id: D-0001\nknowledge: [K-0001, K-0002]\n",
                 "INDEX.md": "K-0001 K-0002\n"})
    commit(repo, "topic")
    sh(repo, "checkout", "-q", "main")
    write(repo, {"K-0001.yaml": "id: K-0001\n", "cases/C-0001.md": "# main\n", "committee/D-0001.yaml": "id: D-0001\n",
                 "committee/D-0002.yaml": "id: D-0002\n", "INDEX.md": "K-0001\n"})
    commit(repo, "main")
    sh(repo, "checkout", "-q", "topic")
    return repo


def test_renumbers_only_colliding_ids_and_rewrites_references(tmp_path):
    repo = setup(tmp_path)
    m = kr.renumber(repo, "main", commit=True)
    # K-0001 は main にもある → K-0003（main の 1 とこのブランチの 2 の次）。K-0002 はぶつからないのでそのまま
    assert m == {"K-0001": "K-0003", "C-0001": "C-0002", "D-0001": "D-0003"}
    root = repo / R
    assert (root / "K-0003.yaml").read_text() == "id: K-0003\nevidence:\n  - case: C-0002\nrelated: [K-0002]\ndecided_by: committee:D-0003\n"
    assert (root / "K-0002.yaml").read_text() == "id: K-0002\nevidence:\n  - case: C-0002\nrelated: [K-0003]\n"
    assert "K-0003 と K-0002 を足した。K-00010 は別物" in (root / "cases" / "C-0002.md").read_text()
    assert (root / "committee" / "D-0003.yaml").read_text() == "id: D-0003\nknowledge: [K-0003, K-0002]\n"
    assert not (root / "K-0001.yaml").exists()
    # 取り込むと、残る衝突は INDEX.md だけ（作り直す）
    r = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "merge", "-q", "--no-edit", "main"],
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert sh(repo, "diff", "--name-only", "--diff-filter=U").split() == [f"{R}/INDEX.md"]


def test_nothing_to_do_when_no_collision(tmp_path):
    repo = setup(tmp_path)
    kr.renumber(repo, "main", commit=True)
    assert kr.renumber(repo, "main", commit=True) == {}


def test_rewrites_only_added_lines_in_existing_files(tmp_path):
    # 既存の知見を直す変更セット（amend）: このブランチが既存の K-0001 に足した事例の参照だけを付け直す（rally #677）
    repo = tmp_path / "r"
    repo.mkdir()
    sh(repo, "init", "-q", "-b", "main")
    # 付け直しのコミットは本体が打つので、身元はリポジトリに持たせる（CI の runner には git の身元が無い）
    sh(repo, "config", "user.name", "t")
    sh(repo, "config", "user.email", "t@example.invalid")
    write(repo, {"K-0001.yaml": "id: K-0001\nevidence:\n  - case: C-0001\n", "cases/C-0001.md": "# C-0001\n"})
    commit(repo, "init")
    sh(repo, "checkout", "-q", "-b", "topic")
    write(repo, {"K-0001.yaml": "id: K-0001\nevidence:\n  - case: C-0001\n  - case: C-0002\n", "cases/C-0002.md": "# C-0002\n"})
    commit(repo, "topic")
    sh(repo, "checkout", "-q", "main")
    write(repo, {"cases/C-0002.md": "# main\n"})
    commit(repo, "main")
    sh(repo, "checkout", "-q", "topic")
    assert kr.renumber(repo, "main", commit=True) == {"C-0002": "C-0003"}
    # 足した行は C-0003 に、もとからある行（id: K-0001、case: C-0001）はそのまま
    assert (repo / R / "K-0001.yaml").read_text() == "id: K-0001\nevidence:\n  - case: C-0001\n  - case: C-0003\n"
