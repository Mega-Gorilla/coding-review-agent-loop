"""Integration test: invokes demo_loop.py as a subprocess."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_demo_loop_dry_run() -> None:
    """
    Run helpers/demo_loop.py and verify it:
    - exits 0
    - prints "validation passed: plan_state"
    - prints "validation passed: plan_review"
    - writes a session file with last_completed_step=post_review
    """
    repo = "demo/skill-loop-test"
    issue = 88888

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "helpers.demo_loop",
            "--issue",
            str(issue),
            "--repo",
            repo,
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
        check=False,
    )

    assert result.returncode == 0, (
        f"demo_loop failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )

    assert "validation passed: plan_state" in result.stdout, result.stdout
    assert "validation passed: plan_review" in result.stdout, result.stdout

    # Verify session state was written with last_completed_step=post_review
    slug = repo.replace("/", "-").replace(":", "-")
    state_home = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    )
    session_path = (
        state_home
        / "coding-review-agent-loop"
        / "skill-sessions"
        / slug
        / f"{issue}.json"
    )
    assert session_path.exists(), f"session file not found: {session_path}"
    data = json.loads(session_path.read_text(encoding="utf-8"))
    assert data.get("last_completed_step") == "post_review", data
