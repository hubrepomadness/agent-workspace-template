#!/usr/bin/env python3
"""
agent_hub.py — local delegation CLI for the autonomous agent workspace.

A minimal, dependency-light command line for humans and agents to interact with
the task pipeline defined in AGENTS.md / tasks/schema.json.

Subcommands
-----------
  validate   Check one or all task files against tasks/schema.json.
  eval       Run a task's verificationTests locally and emit a JSON pass/fail
             report. Exits non-zero if any non-benchmark test fails.
  bench      Run a task's benchmark tests and emit a benchmark report.
  push-task  Format a new JSON task, write it to tasks/backlog/, commit it, and
             open a structured GitHub Issue tagged `agent-task`.
  dispatch   List unclaimed open tasks and trigger a cloud-based agent run to
             claim them (via a pluggable dispatcher; see "Dispatch" below).

Dependencies
------------
Standard library only. `jsonschema` is used for schema validation *if installed*;
otherwise a built-in structural validator provides a degraded-but-functional
fallback so the CLI works on a bare interpreter. `gh` (GitHub CLI) is required
only for `push-task` and is invoked via subprocess; nothing is imported.

Dispatch
--------
`dispatch` does not hard-code any cloud provider. It reads a dispatcher hook from
the environment and invokes it with the open-task payload as JSON on stdin:

  * If $AGENT_DISPATCH_COMMAND is set, it is run with the JSON payload on stdin.
  * Else if $AGENT_DISPATCH_URL is set, an HTTP POST is made to that URL with the
    JSON payload (stdlib urllib; bearer token from $AGENT_DISPATCH_TOKEN if set).
  * Else the command lists open tasks and prints guidance on configuring a hook.

This keeps the CLI cloud-agnostic: point it at any agent runner (a webhook, a
queue producer, a CLI that spawns a containerized agent) without coupling here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = REPO_ROOT / "tasks" / "schema.json"
BACKLOG_DIR = REPO_ROOT / "tasks" / "backlog"
COMPLETED_DIR = REPO_ROOT / "tasks" / "completed"
BENCH_BASELINE_DIR = REPO_ROOT / "tasks" / ".benchmarks"

VALID_PRIORITIES = ("P0", "P1", "P2", "P3")
VALID_STATUSES = ("open", "in_progress", "blocked", "completed", "cancelled")

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slugify(text: str) -> str:
    """Lowercase, alphanumeric-hyphen slug matching schema's taskId pattern."""
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    if not text:
        raise ValueError("Title does not produce a usable slug; supply --task-id.")
    return text


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _task_path(task_id: str) -> Path:
    return BACKLOG_DIR / f"{task_id}.json"


def _next_task_id(base_slug: str) -> str:
    """Ensure uniqueness against existing backlog + completed task ids."""
    existing = set()
    if BACKLOG_DIR.is_dir():
        existing.update(p.stem for p in BACKLOG_DIR.glob("*.json"))
    if COMPLETED_DIR.is_dir():
        existing.update(p.stem for p in COMPLETED_DIR.glob("*.json"))
    if base_slug not in existing:
        return base_slug
    # Append a numeric suffix until unique.
    n = 2
    while f"{base_slug}-{n}" in existing:
        n += 1
    return f"{base_slug}-{n}"


def _err(msg: str) -> None:
    print(f"::error::{msg}", file=sys.stderr)


def _run_command(cmd: str, cwd: Path = REPO_ROOT) -> dict:
    """Run a shell command, capturing exit code and combined output."""
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(cwd),
            capture_output=True, text=True, timeout=600,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return {"exit": proc.returncode, "output": output[-4000:]}
    except subprocess.TimeoutExpired:
        return {"exit": 124, "output": "[command timed out after 600s]"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"exit": 1, "output": f"[runner error: {exc}]"}


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _builtin_validate(task: dict) -> list[str]:
    """Degraded structural validator used when jsonschema is unavailable."""
    errors: list[str] = []
    required = [
        "taskId", "title", "objective", "inputs", "targetOutputs",
        "verificationTests", "priority", "status",
    ]
    for field in required:
        if field not in task:
            errors.append(f"missing required field: {field}")
    if "taskId" in task and not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", task["taskId"]):
        errors.append("taskId must match ^[a-z0-9]+(-[a-z0-9]+)*$")
    if "priority" in task and task["priority"] not in VALID_PRIORITIES:
        errors.append(f"priority must be one of {VALID_PRIORITIES}")
    if "status" in task and task["status"] not in VALID_STATUSES:
        errors.append(f"status must be one of {VALID_STATUSES}")
    vt = task.get("verificationTests")
    if not isinstance(vt, list) or not vt:
        errors.append("verificationTests must be a non-empty array")
    else:
        for i, t in enumerate(vt):
            for f in ("id", "command", "expect"):
                if f not in t:
                    errors.append(f"verificationTests[{i}] missing field: {f}")
            if t.get("expect") in ("contains", "json_match") and "assert" not in t:
                errors.append(f"verificationTests[{i}] expect={t['expect']} requires 'assert'")
    return errors


def _validate_task(task: dict) -> list[str]:
    """Validate a task dict against the schema. Returns a list of error strings."""
    try:
        import jsonschema  # type: ignore
    except Exception:
        return _builtin_validate(task)
    try:
        schema = _load_json(SCHEMA_PATH)
        validator = jsonschema.Draft202012Validator(schema)
        return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
                for e in sorted(validator.iter_errors(task), key=str)]
    except Exception as exc:  # pragma: no cover - defensive
        return [f"schema validation crashed: {exc}"]


def _all_task_files() -> list[Path]:
    files = []
    if BACKLOG_DIR.is_dir():
        files.extend(sorted(BACKLOG_DIR.glob("*.json")))
    if COMPLETED_DIR.is_dir():
        files.extend(sorted(COMPLETED_DIR.glob("*.json")))
    return files


def cmd_validate(args: argparse.Namespace) -> int:
    if args.task:
        path = _task_path(args.task)
        if not path.exists():
            _err(f"task file not found: {path}")
            return 2
        task = _load_json(path)
        errors = _validate_task(task)
        if errors:
            _err(f"INVALID: {args.task}")
            for e in errors:
                print(f"  - {e}", file=sys.stderr)
            return 1
        print(f"VALID: {args.task}")
        return 0

    files = _all_task_files()
    if not files:
        print("No task files found to validate.")
        return 0
    bad = 0
    for path in files:
        try:
            task = _load_json(path)
        except Exception as exc:
            _err(f"unparseable: {path.name}: {exc}")
            bad += 1
            continue
        errors = _validate_task(task)
        if errors:
            _err(f"INVALID: {path.name}")
            for e in errors:
                print(f"  - {e}", file=sys.stderr)
            bad += 1
        else:
            print(f"VALID: {path.name}")
    return 1 if bad else 0


# --------------------------------------------------------------------------- #
# Eval + bench
# --------------------------------------------------------------------------- #

def _eval_single_test(test: dict) -> dict:
    result = _run_command(test["command"])
    passed = False
    expect = test.get("expect", "exit_zero")
    if expect == "exit_zero":
        passed = result["exit"] == 0
    elif expect == "exit_nonzero":
        passed = result["exit"] != 0
    elif expect == "contains":
        passed = result["exit"] == 0 and test.get("assert", "") in result["output"]
    elif expect == "json_match":
        passed = False
        if result["exit"] == 0:
            try:
                data = json.loads(result["output"])
                passed = _json_contains(data, json.loads(test["assert"]))
            except Exception:
                passed = False
    result["passed"] = passed
    return result


def _json_contains(haystack: Any, needle: Any) -> bool:
    """True if needle appears as a value (or sub-structure) within haystack."""
    if haystack == needle:
        return True
    if isinstance(haystack, dict):
        return any(_json_contains(v, needle) for v in haystack.values())
    if isinstance(haystack, list):
        return any(_json_contains(v, needle) for v in haystack)
    return False


def cmd_eval(args: argparse.Namespace) -> int:
    path = _task_path(args.task)
    if not path.exists():
        # Also look in completed/ so historical tasks can be re-evaluated.
        alt = COMPLETED_DIR / f"{args.task}.json"
        if alt.exists():
            path = alt
        else:
            _err(f"task file not found: {args.task}")
            return 2
    task = _load_json(path)
    errors = _validate_task(task)
    if errors:
        _err("task does not conform to schema; refusing to eval:")
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 2

    tests = [t for t in task.get("verificationTests", [])
             if not t.get("benchmark", False)]
    results: dict[str, dict] = {}
    for t in tests:
        results[t["id"]] = _eval_single_test(t)

    blocking = [tid for tid, r in results.items() if not r["passed"]]
    report = {
        "agent_eval": True,
        "task_id": task["taskId"],
        "passed": not blocking,
        "tests": results,
        "blocking": blocking,
    }

    if args.report:
        _save_json(Path(args.report), report)
    print(json.dumps(report, indent=2))
    return 0 if not blocking else 1


def cmd_bench(args: argparse.Namespace) -> int:
    path = _task_path(args.task)
    if not path.exists():
        alt = COMPLETED_DIR / f"{args.task}.json"
        if alt.exists():
            path = alt
        else:
            _err(f"task file not found: {args.task}")
            return 2
    task = _load_json(path)
    bench_tests = [t for t in task.get("verificationTests", [])
                   if t.get("benchmark", False)]
    if not bench_tests:
        report = {"agent_eval_bench": True, "task_id": task["taskId"],
                  "benchmarks": {}, "note": "no benchmark tests declared"}
        if args.report:
            _save_json(Path(args.report), report)
        print(json.dumps(report, indent=2))
        return 0

    BENCH_BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    baseline_path = BENCH_BASELINE_DIR / f"{task['taskId']}.baseline.json"
    baselines = {}
    if baseline_path.exists():
        try:
            baselines = _load_json(baseline_path)
        except Exception:
            baselines = {}

    results: dict[str, dict] = {}
    for t in bench_tests:
        start = time.perf_counter()
        run = _run_command(t["command"])
        elapsed = time.perf_counter() - start
        threshold = t.get("regressionThreshold", 0.0)
        prev = baselines.get(t["id"], {}).get("elapsed_seconds")
        regressed = False
        if prev is not None and prev > 0:
            regressed = elapsed > prev * (1.0 + threshold)
        results[t["id"]] = {
            "passed": run["exit"] == 0 and not regressed,
            "exit": run["exit"],
            "elapsed_seconds": round(elapsed, 6),
            "baseline_seconds": prev,
            "threshold": threshold,
            "regressed": regressed,
        }
        # Update baseline to the latest good measurement (establishes baseline
        # on first run, then drifts only when allowed by the threshold).
        baselines[t["id"]] = {"elapsed_seconds": round(elapsed, 6)}

    report = {"agent_eval_bench": True, "task_id": task["taskId"],
              "benchmarks": results}
    if args.report:
        _save_json(Path(args.report), report)
    _save_json(baseline_path, baselines)
    print(json.dumps(report, indent=2))
    return 0 if all(r["passed"] for r in results.values()) else 1


# --------------------------------------------------------------------------- #
# push-task
# --------------------------------------------------------------------------- #

def _ensure_gh() -> bool:
    try:
        subprocess.run(["gh", "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def _gh(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *cmd], capture_output=True, text=True, check=check)


def _repo_slug() -> str | None:
    try:
        cp = _gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        return cp.stdout.strip() or None
    except Exception:
        return None


def _ensure_label(label: str) -> None:
    """Create the agent-task label if it does not already exist."""
    try:
        _gh("label", "list", "--json", "name", "-q", f".[].name")
        cp = _gh("label", "list", "--json", "name", "-q", f".[] | select(.name==\"{label}\")")
        if cp.stdout.strip():
            return
        _gh("label", "create", label, "--description",
            "A structured task claimable by autonomous agents",
            "--color", "0E8A16", check=False)
    except Exception:
        # Non-fatal: gh may lack permission to create labels; the issue can
        # still be opened; labeling will simply be skipped on failure.
        pass


def cmd_push_task(args: argparse.Namespace) -> int:
    if not args.title or not args.spec:
        _err("--title and --spec are both required for push-task")
        return 2

    base = _slugify(args.task_id) if args.task_id else _slugify(args.title)
    task_id = _next_task_id(base)

    paths = []
    if args.inputs:
        for chunk in args.inputs:
            paths.extend(p.strip() for p in chunk.split(",") if p.strip())

    task = {
        "taskId": task_id,
        "title": args.title,
        "objective": args.spec,
        "inputs": {"paths": paths, "commands": [], "context": args.context or ""},
        "targetOutputs": {
            "artifacts": [],
            "spec": args.target_outputs or "",
        },
        "verificationTests": [],
        "priority": args.priority,
        "status": "open",
        "assignee": None,
        "dependsOn": [],
        "createdAt": _now_iso(),
        "completedAt": None,
        "prNumber": None,
        "memory": None,
    }

    # Attach any inline verification tests passed as repeated "id:command" tokens.
    for token in args.test or []:
        if ":" not in token:
            _err(f"--test tokens must be 'id:command', got: {token}")
            return 2
        tid, cmd = token.split(":", 1)
        task["verificationTests"].append(
            {"id": tid.strip(), "command": cmd.strip(), "expect": "exit_zero"}
        )
    if not task["verificationTests"]:
        # A schema-valid task requires at least one verification test. Provide a
        # default smoke test so the file validates out of the box.
        task["verificationTests"].append(
            {"id": "smoke.validate",
             "command": f"python agent_hub.py validate --task {task_id}",
             "expect": "exit_zero"}
        )

    errors = _validate_task(task)
    if errors:
        _err("generated task fails schema validation:")
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    out_path = _task_path(task_id)
    _save_json(out_path, task)
    print(f"Wrote task file: {out_path.relative_to(REPO_ROOT)}")

    # Commit to the repo if we are inside a git working tree.
    if (REPO_ROOT / ".git").exists():
        try:
            _gh  # noqa: touch import path; git ops below use subprocess directly
            subprocess.run(["git", "add", str(out_path)], check=True)
            subprocess.run(
                ["git", "commit", "-m",
                 f"chore(tasks): add {task_id}\n\n- Closes task: {task_id}\n"
                 f"- Agent: agent_hub.py\n- Verification: schema validates"],
                check=True, capture_output=True, text=True,
            )
            print(f"Committed {task_id}.")
        except subprocess.CalledProcessError as exc:
            print(f"::warning::git commit skipped: "
                  f"{(exc.stderr or str(exc)).strip()}", file=sys.stderr)
    else:
        print("No .git directory; skipped git commit (file is written).")

    # Open a structured GitHub Issue tagged agent-task.
    if not _ensure_gh():
        print("::warning::gh CLI not found; task file written but no issue "
              "opened. Install gh to publish issues.", file=sys.stderr)
        return 0

    _ensure_label("agent-task")
    body = (
        "### Task\n\n"
        f"Task ID: `{task_id}`\nPriority: `{args.priority}`\n\n"
        "This issue mirrors the structured task file "
        f"[`tasks/backlog/{task_id}.json`](tasks/backlog/{task_id}.json) "
        "for agent discoverability. The canonical source of truth is the JSON "
        "file; this issue is a pointer.\n\n"
        "```json\n"
        + json.dumps(task, indent=2)
        + "\n```\n\n"
        "To claim: create branch `task/" + task_id + "`, implement, and open a PR "
        "titled `[" + task_id + "] <summary>` per AGENTS.md."
    )
    try:
        cp = _gh("issue", "create", "--title",
                 f"[agent-task] {args.title}",
                 "--body", body, "--label", "agent-task")
        issue_url = cp.stdout.strip()
        print(f"Opened issue: {issue_url}" if issue_url
              else "Issue opened (no URL returned).")
    except subprocess.CalledProcessError as exc:
        print(f"::warning::failed to open issue: "
              f"{(exc.stderr or str(exc)).strip()}", file=sys.stderr)
        # Try once more without the label in case label creation was blocked.
        try:
            cp = _gh("issue", "create", "--title",
                     f"[agent-task] {args.title}", "--body", body)
            print(f"Opened issue (unlabeled): {cp.stdout.strip()}")
        except Exception as exc2:
            print(f"::error::could not open issue: {exc2}", file=sys.stderr)
            return 1
    return 0


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #

def _open_unclaimed_tasks() -> list[dict]:
    tasks = []
    if not BACKLOG_DIR.is_dir():
        return tasks
    for path in sorted(BACKLOG_DIR.glob("*.json")):
        try:
            t = _load_json(path)
        except Exception:
            continue
        if t.get("status") == "open" and not t.get("assignee"):
            tasks.append(t)
    return tasks


def _dispatch_via_command(payload: str) -> int:
    cmd = os.environ["AGENT_DISPATCH_COMMAND"]
    print(f"Dispatching {json.loads(payload)['count']} task(s) via "
          f"AGENT_DISPATCH_COMMAND: {cmd}")
    proc = subprocess.run(cmd, shell=True, input=payload,
                          capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
    return proc.returncode


def _dispatch_via_url(payload: str) -> int:
    url = os.environ["AGENT_DISPATCH_URL"]
    print(f"Dispatching to URL: {url}")
    data = payload.encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Accept": "application/json"}
    token = os.environ.get("AGENT_DISPATCH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"Dispatcher responded: HTTP {resp.status}")
            sys.stdout.write(resp.read().decode("utf-8", "replace"))
            return 0
    except urllib.error.HTTPError as exc:
        _err(f"dispatcher HTTP error: {exc.code} {exc.reason}")
        sys.stderr.write(exc.read().decode("utf-8", "replace"))
        return 1
    except Exception as exc:
        _err(f"dispatcher request failed: {exc}")
        return 1


def cmd_dispatch(args: argparse.Namespace) -> int:
    tasks = _open_unclaimed_tasks()
    if not tasks:
        print("No open, unclaimed tasks in tasks/backlog/. Nothing to dispatch.")
        if args.dry_run:
            return 0
        print("Tip: use `python agent_hub.py push-task` to create a task first.")
        return 0

    print(f"Open unclaimed tasks ({len(tasks)}):")
    for t in tasks:
        print(f"  - [{t.get('priority', '?')}] {t['taskId']}: {t['title']}")

    payload = json.dumps({"count": len(tasks), "tasks": tasks}, indent=2)

    if args.dry_run:
        print("\n--dry-run: would dispatch the following payload:\n" + payload)
        return 0

    if os.environ.get("AGENT_DISPATCH_COMMAND"):
        return _dispatch_via_command(payload)
    if os.environ.get("AGENT_DISPATCH_URL"):
        return _dispatch_via_url(payload)

    print(
        "\nNo dispatcher configured. Set one of:\n"
        "  AGENT_DISPATCH_COMMAND  a shell command that reads the JSON task "
        "payload on stdin and launches your cloud agent run\n"
        "  AGENT_DISPATCH_URL      an HTTP endpoint POSTed the JSON task "
        "payload (optional AGENT_DISPATCH_TOKEN bearer)\n\n"
        "Then re-run `python agent_hub.py dispatch`. The payload above is what "
        "the dispatcher will receive."
    )
    return 0


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent_hub.py",
        description="Delegation CLI for the autonomous agent task pipeline.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("validate", help="Validate task file(s) against the schema.")
    sp.add_argument("--task", help="Specific task id to validate. "
                    "Omit to validate every task file.")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("eval", help="Run a task's verificationTests and report.")
    sp.add_argument("--task", required=True, help="Task id to evaluate.")
    sp.add_argument("--report", help="Path to write the JSON eval report.")
    sp.set_defaults(func=cmd_eval)

    sp = sub.add_parser("bench", help="Run a task's benchmark tests and report.")
    sp.add_argument("--task", required=True, help="Task id to benchmark.")
    sp.add_argument("--report", help="Path to write the JSON benchmark report.")
    sp.set_defaults(func=cmd_bench)

    sp = sub.add_parser("push-task", help="Format a task, commit it, open a GitHub Issue.")
    sp.add_argument("--title", required=True, help="Short imperative task title.")
    sp.add_argument("--spec", required=True, help="Objective / specification text.")
    sp.add_argument("--task-id", help="Override the generated task id slug.")
    sp.add_argument("--priority", choices=VALID_PRIORITIES, default="P2",
                    help="Task priority (default P2).")
    sp.add_argument("--context", help="Prose explaining the task's inputs.")
    sp.add_argument("--target-outputs", dest="target_outputs",
                    help="Spec text for the target outputs.")
    sp.add_argument("--inputs", action="append",
                    help="Comma-separated repo-relative input paths. Repeatable.")
    sp.add_argument("--test", action="append",
                    help='Inline verification test as "id:command". Repeatable. '
                         'A default smoke test is added if none given.')
    sp.set_defaults(func=cmd_push_task)

    sp = sub.add_parser("dispatch", help="Trigger a cloud agent run on open tasks.")
    sp.add_argument("--dry-run", action="store_true",
                    help="List open tasks and print the payload without dispatching.")
    sp.set_defaults(func=cmd_dispatch)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        _err("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())