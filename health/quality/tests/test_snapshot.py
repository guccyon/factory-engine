"""内部品質の定期診断（health/quality/snapshot.py）。小さな製品の木を作って測る。

  python -m pytest health/quality/tests
"""
import json
import os
import subprocess
import sys

import pytest
import yaml

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import snapshot  # noqa: E402

DEFS = yaml.safe_load(open(os.path.join(HERE, "..", "..", "..", "definitions", "quality.yaml"), encoding="utf-8"))
CONFIG = {
    "languages": {
        "rust": {"src": ["src/**/*.rs"], "tests": ["tests/**/*.rs"], "inline_test_marker": "#[cfg(test)]"},
        "dart": {"src": ["lib/**/*.dart"], "tests": ["test/**/*.dart"]},
    },
    "exclude": ["^src/generated\\.rs$"],
}

SIMPLE = "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n"
BRANCHY = "pub fn f(x: i32) -> i32 {\n" + "".join(f"    if x == {i} {{ return {i}; }}\n" for i in range(12)) + "    0\n}\n"
INLINE_TESTS = "\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() {\n" + "".join(f"        if 1 == {i} {{}}\n" for i in range(12)) + "    }\n}\n"


def tree(tmp_path, files: dict[str, str]):
    for p, text in files.items():
        f = tmp_path / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init"], check=True)
    return str(tmp_path)


def run(root, **kw):
    from datetime import datetime, timezone
    return snapshot.measure(root, CONFIG, DEFS, kw.get("clippy"), kw.get("extra", {}), kw.get("history", True), datetime.now(timezone.utc))


@pytest.mark.skipif(snapshot.lizard is None, reason="lizard がない")
def test_counts_complex_functions_and_ignores_inline_tests(tmp_path):
    root = tree(tmp_path, {"src/a.rs": SIMPLE + BRANCHY + INLINE_TESTS, "tests/it.rs": "#[tokio::test]\nasync fn x() {}\n",
                           "src/generated.rs": BRANCHY, "lib/w.dart": "int f() => 1;\n", "test/w_test.dart": "  test('x', () {});\n"})
    s = run(root)
    assert s["counts"]["functions"] == 2                 # add と f。inline のテストと除外したファイルは数えない
    assert s["counts"]["complex_functions"] == 1
    assert s["metrics"]["complex_function_ratio"] == 0.5
    assert s["counts"]["tests"] == 3                     # inline の #[test]・tests/ の #[tokio::test]・Dart の test(
    assert s["measured_languages"] == ["rust"]           # Dart は複雑さを測らない
    assert s["hotspots"][0]["file"] == "src/a.rs" and s["hotspots"][0]["changes"] == 1


def test_duplication_counts_repeated_windows(tmp_path):
    body = "".join(f"    let v{i} = compute({i});\n" for i in range(8))
    root = tree(tmp_path, {"src/a.rs": "fn a() {\n" + body + "}\n", "src/b.rs": "fn b() {\n" + body + "}\n"})
    s = run(root, history=False)
    assert s["counts"]["duplicated_lines"] == 16
    assert s["metrics"]["duplication_ratio"] == pytest.approx(16 / 18, abs=1e-3)


def test_clippy_json_dedupes_by_location(tmp_path):
    msg = {"reason": "compiler-message", "message": {"level": "warning", "code": {"code": "clippy::too_many_lines"},
                                                      "spans": [{"is_primary": True, "file_name": "src/a.rs", "line_start": 1}]}}
    p = tmp_path / "clippy.json"
    p.write_text("\n".join([json.dumps(msg), json.dumps(msg), json.dumps({"reason": "build-finished"}), "not json"]))
    assert snapshot.clippy_warnings(str(p)) == 1


def test_score_is_weighted_and_skips_nulls():
    best = {k: c["good"] for k, c in DEFS["score"]["components"].items()}
    assert snapshot.score(best, DEFS) == 100
    worst = {k: c["bad"] for k, c in DEFS["score"]["components"].items()}
    assert snapshot.score(worst, DEFS) == 0
    assert snapshot.score({"layer_violations": 0, "duplication_ratio": None}, DEFS) == 100
    assert snapshot.score({}, DEFS) is None


def test_floor_flags_only_increase():
    base = {"counts": {"complex_functions": 3}, "score": 90}
    results, md = snapshot.floor(DEFS, base, {"counts": {"complex_functions": 4}, "score": 89})
    assert results[0]["broken"] and "下限割れ" in md
    results, _ = snapshot.floor(DEFS, base, {"counts": {"complex_functions": 3}, "score": 90})
    assert not results[0]["broken"]
    results, md = snapshot.floor(DEFS, base, {"counts": {"complex_functions": None}, "score": None})
    assert not results[0]["broken"] and "測れない" in md
