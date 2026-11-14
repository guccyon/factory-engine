"""落ちた実装のチケットの自動の立て直し（recover.py）の次の一手。2026-11-13 rally #1438 が時間切れで落ちたまま人の rerun を待った"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import recover  # noqa: E402


def d(err, inc=0, tmo=0, auto=0, pr=False):
    return recover.decide(err, inc, tmo, auto, has_pr=pr, continue_max=1)


def test_timeout_first_reruns_then_resplits():
    assert d("timeout", tmo=1) == "rerun"
    assert d("timeout", tmo=2, auto=1) == "resplit"


def test_incomplete_continues_once_then_resplits():
    assert d("incomplete(claude_exit=0)", inc=1, pr=True) == "continue"
    assert d("incomplete(claude_exit=0)", inc=2, auto=1, pr=True) == "resplit"


def test_incomplete_without_pr_resplits():
    assert d("incomplete(claude_exit=1)", inc=1, pr=False) == "resplit"


def test_cap_hands_to_human():
    assert d("timeout", tmo=3, auto=recover.AUTO_MAX) is None


def test_unclassified_errors_stay_with_human():
    for e in ("", "lock_held_timeout", "unresolved_after_max_rounds", "worker_exit_1"):
        assert d(e) is None


def test_event_is_declared():
    import yaml
    from pathlib import Path
    ev = yaml.safe_load((Path(__file__).resolve().parents[3] / "definitions" / "events.yaml").read_text(encoding="utf-8"))["events"]
    assert set(ev["job.auto_recovered"]["required"]) == {"previous", "error", "action", "attempt", "max"}
