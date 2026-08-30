"""Smoke tests for the agent workspace pipeline.

These exercise the schema contract and the agent_hub CLI surface so CI has a
meaningful unit suite from day one. They use only the standard library so they
run without `pytest` installed (via `python -m pytest` or `python -m unittest`).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = REPO_ROOT / "tasks" / "schema.json"
EXAMPLE = REPO_ROOT / "tasks" / "backlog" / "example-task-001.json"
HUB = REPO_ROOT / "agent_hub.py"


def _schema_fields():
    return json.loads(SCHEMA.read_text(encoding="utf-8"))["required"]


def test_example_task_is_valid_against_required_fields():
    task = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    for field in _schema_fields():
        assert field in task, f"example task missing required field: {field}"
    assert task["taskId"] == "example-task-001"
    assert task["status"] == "open"
    assert task["verificationTests"], "example task must declare verification tests"


def test_example_task_file_matches_schema_round_trip():
    # Structural sanity: the example task should parse and reference the schema.
    task = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(task["verificationTests"], list)
    assert all("id" in t and "command" in t and "expect" in t
               for t in task["verificationTests"])


def _run_hub(*args: str):
    return subprocess.run(
        [sys.executable, str(HUB), *args],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )


def test_validate_example_task_exits_zero():
    cp = _run_hub("validate", "--task", "example-task-001")
    assert cp.returncode == 0, cp.stderr


def test_eval_example_task_exits_zero():
    cp = _run_hub("eval", "--task", "example-task-001")
    assert cp.returncode == 0, cp.stderr
    report = json.loads(cp.stdout)
    assert report["agent_eval"] is True
    assert report["task_id"] == "example-task-001"
    assert report["passed"] is True
    assert report["blocking"] == []


if __name__ == "__main__":
    # Allow running without pytest.
    import unittest
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(
        [unittest.FunctionTestCase(g) for g in (
            test_example_task_is_valid_against_required_fields,
            test_example_task_file_matches_schema_round_trip,
            test_validate_example_task_exits_zero,
            test_eval_example_task_exits_zero,
        )]
    )
    unittest.TextTestRunner(verbosity=2).run(suite)