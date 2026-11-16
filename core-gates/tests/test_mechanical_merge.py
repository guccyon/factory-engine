"""続きのジョブの衝突を機械的に解く（工程の改善案 #504・#524）"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import mechanical_merge as mm  # noqa: E402

K = """id: K-0001
statement: 知見
enforcement:
  level: deterministic
  solutions:
    - kind: ci_check
      issue: 1
"""
README = "# 道具\n\n## 検査\n\n- A を見る\n\n## 使い方\n\n手順。\n"
DECL = """conflict:
  mechanical:
    paths: ["CHANGELOG.md", "changelog.d/", ".factory-config/knowledge/K-*.yaml", "tools/*/README.md"]
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def conflicted(tmp: Path, base: dict, main: dict, pr: dict, decl: str | None = DECL) -> Path:
    """main と pr が base から別々に変えて、pr に main を取り込んで衝突させたリポジトリ"""
    repo = tmp / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")

    def put(files: dict):
        for rel, text in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "c")

    put({**base, **({DECLARATION: decl} if decl else {})})
    git(repo, "checkout", "-q", "-b", "pr")
    put(pr)
    git(repo, "checkout", "-q", "main")
    put(main)
    git(repo, "checkout", "-q", "pr")
    subprocess.run(["git", "merge", "--no-edit", "-q", "main"], cwd=repo, capture_output=True)
    return repo


DECLARATION = mm.DECLARATION
KP = ".factory-config/knowledge/K-0001.yaml"
RP = "tools/pr-body-check/README.md"


def unmerged(repo: Path) -> list[str]:
    return git(repo, "diff", "--name-only", "--diff-filter=U").split()


def test_knowledge_solutions_appended_on_both_sides_are_kept(tmp_path):
    repo = conflicted(tmp_path, {KP: K}, {KP: K + "    - kind: test\n      issue: 2\n"}, {KP: K + "    - kind: ci_check\n      issue: 3\n"})
    assert unmerged(repo) == [KP]
    assert mm.main(["--repo", str(repo), KP]) == 0
    assert unmerged(repo) == []
    import yaml
    sols = yaml.safe_load((repo / KP).read_text(encoding="utf-8"))["enforcement"]["solutions"]
    assert [s["issue"] for s in sols] == [1, 3, 2]


def test_readme_sections_appended_on_both_sides_are_kept(tmp_path):
    repo = conflicted(tmp_path, {RP: README}, {RP: README.replace("- A を見る\n", "- A を見る\n- B を見る\n")},
                      {RP: README.replace("- A を見る\n", "- A を見る\n- C を見る\n")})
    assert mm.main(["--repo", str(repo), RP]) == 0
    text = (repo / RP).read_text(encoding="utf-8")
    assert "- B を見る" in text and "- C を見る" in text and "<<<<<<<" not in text


def test_rewritten_lines_are_left_to_the_agent(tmp_path):
    repo = conflicted(tmp_path, {RP: README}, {RP: README.replace("- A を見る", "- A を厳しく見る")},
                      {RP: README.replace("- A を見る", "- A をゆるく見る")})
    assert mm.main(["--repo", str(repo), RP]) == 1
    assert unmerged(repo) == [RP]   # 何も書き換えない


def test_files_outside_the_declaration_are_left_to_the_agent(tmp_path):
    repo = conflicted(tmp_path, {KP: K, "src/a.py": "x = 1\n"},
                      {KP: K + "    - kind: test\n      issue: 2\n", "src/a.py": "x = 1\ny = 2\n"},
                      {KP: K + "    - kind: test\n      issue: 3\n", "src/a.py": "x = 1\nz = 3\n"})
    assert mm.main(["--repo", str(repo), KP, "src/a.py"]) == 3
    assert sorted(unmerged(repo)) == sorted([KP, "src/a.py"])


def test_without_declaration_only_changelog(tmp_path):
    cl = "# 変更履歴\n\n## [Unreleased]\n\n- 既存\n"
    repo = conflicted(tmp_path, {"CHANGELOG.md": cl, KP: K}, {"CHANGELOG.md": cl + "- 本線\n", KP: K + "    - kind: test\n      issue: 2\n"},
                      {"CHANGELOG.md": cl + "- PR\n", KP: K + "    - kind: test\n      issue: 3\n"}, decl=None)
    assert mm.main(["--repo", str(repo), "CHANGELOG.md", KP]) == 3
    assert mm.main(["--repo", str(repo), "CHANGELOG.md"]) == 0
    assert "- 本線" in (repo / "CHANGELOG.md").read_text(encoding="utf-8")


def test_broken_yaml_is_left_to_the_agent(tmp_path):
    repo = conflicted(tmp_path, {KP: K}, {KP: K + "    - kind: test\n      issue: 2\n"}, {KP: K + "  extra: [\n"})
    assert mm.main(["--repo", str(repo), KP]) == 1


def test_patterns():
    pats = ["CHANGELOG.md", "changelog.d/", ".factory-config/knowledge/K-*.yaml", "tools/*/README.md"]
    assert mm.matches("changelog.d/12.md", pats) and mm.matches(KP, pats) and mm.matches(RP, pats)
    assert not mm.matches(".factory-config/knowledge/INDEX.md", pats) and not mm.matches("README.md", pats)
