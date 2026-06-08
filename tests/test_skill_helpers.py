"""Unit tests for the Claude Code skill helper CLIs."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HELPERS = Path(__file__).parent.parent / "helpers"


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", *args],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"Command {args!r} failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
    return result


# ---------------------------------------------------------------------------
# helpers/validate_response.py
# ---------------------------------------------------------------------------

_VALID_PLAN_STATE = """\
## Plan

1. Step one

<!-- AGENT_PLAN_STATE: approved -->
-- Anthropic Claude
"""

_INVALID_PLAN_STATE = "This has no marker at all."

_VALID_PLAN_REVIEW = json.dumps(
    {
        "schema_version": 1,
        "kind": "plan_review",
        "state": "approved",
        "summary": "Plan looks good.",
        "blocking_plan_issues": [],
        "same_plan_followups": [],
        "future_followups": [],
        "prior_plan_item_dispositions": [],
    }
) + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- Codex\n"


def _write_tmp(content: str, suffix: str = ".md") -> str:
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8") as f:
        f.write(content)
        return f.name


class TestValidateResponse:
    def test_valid_plan_state_accepted(self) -> None:
        path = _write_tmp(_VALID_PLAN_STATE)
        result = _run("helpers.validate_response", "--file", path, "--kind", "plan_state")
        assert "validation passed: plan_state" in result.stdout

    def test_missing_plan_state_marker_rejected(self) -> None:
        path = _write_tmp(_INVALID_PLAN_STATE)
        result = _run("helpers.validate_response", "--file", path, "--kind", "plan_state", check=False)
        assert result.returncode != 0
        assert "validation failed: plan_state" in result.stderr

    def test_valid_plan_review_accepted(self) -> None:
        path = _write_tmp(_VALID_PLAN_REVIEW)
        ctx_path = _write_tmp(
            json.dumps({"reviewer": "Codex", "prior_items": [], "current_round_items": []}),
            suffix=".json",
        )
        result = _run(
            "helpers.validate_response",
            "--file",
            path,
            "--kind",
            "plan_review",
            "--context-file",
            ctx_path,
        )
        assert "validation passed: plan_review" in result.stdout

    def test_plan_review_with_unknown_prior_item_rejected(self) -> None:
        # A review that disposes unknown prior item IDs must be rejected.
        review = json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_review",
                "state": "blocking",
                "summary": "Blocking.",
                "blocking_plan_issues": ["Something bad."],
                "same_plan_followups": [],
                "future_followups": [],
                "prior_plan_item_dispositions": [
                    {"item_id": "item-999", "disposition": "resolved"}
                ],
            }
        ) + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Codex\n"

        path = _write_tmp(review)
        # Empty prior items — item-999 is unknown
        ctx_path = _write_tmp(
            json.dumps({"reviewer": "Codex", "prior_items": [], "current_round_items": []}),
            suffix=".json",
        )
        result = _run(
            "helpers.validate_response",
            "--file",
            path,
            "--kind",
            "plan_review",
            "--context-file",
            ctx_path,
            check=False,
        )
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# helpers/state_manager.py  (session round-trip, no live gh required)
# ---------------------------------------------------------------------------

class TestStateManager:
    def _session_path(self, repo: str, issue: int) -> Path:
        import os
        slug = repo.replace("/", "-").replace(":", "-")
        state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        return state_home / "coding-review-agent-loop" / "skill-sessions" / slug / f"{issue}.json"

    def test_write_and_read_session(self) -> None:
        repo = "test/skill-repo"
        issue = 9999
        fields = {"last_completed_step": "post_review", "session_id": "abc123"}
        _run(
            "helpers.state_manager",
            "write-session",
            "--issue",
            str(issue),
            "--repo",
            repo,
            "--fields",
            json.dumps(fields),
        )
        result = _run("helpers.state_manager", "read-session", "--issue", str(issue), "--repo", repo)
        data = json.loads(result.stdout)
        assert data["last_completed_step"] == "post_review"
        assert data["session_id"] == "abc123"

    def test_write_and_clear_pending_comment(self) -> None:
        repo = "test/skill-repo"
        issue = 9999
        body_path = "/tmp/pending-comment-body.md"
        _run(
            "helpers.state_manager",
            "write-pending-comment",
            "--issue",
            str(issue),
            "--repo",
            repo,
            "--body",
            body_path,
        )
        result = _run("helpers.state_manager", "read-session", "--issue", str(issue), "--repo", repo)
        data = json.loads(result.stdout)
        assert data.get("pending_comment_body") == body_path

        _run(
            "helpers.state_manager",
            "clear-pending-comment",
            "--issue",
            str(issue),
            "--repo",
            repo,
        )
        result = _run("helpers.state_manager", "read-session", "--issue", str(issue), "--repo", repo)
        data = json.loads(result.stdout)
        assert "pending_comment_body" not in data


# ---------------------------------------------------------------------------
# helpers/run_external.py  (dry-run only)
# ---------------------------------------------------------------------------

class TestRunExternal:
    def test_dry_run_exits_zero_and_writes_valid_stub(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as pf:
            pf.write("Prompt text.")
            prompt_path = pf.name
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as of:
            output_path = of.name

        result = _run(
            "helpers.run_external",
            "--agent",
            "codex",
            "--prompt-file",
            prompt_path,
            "--output",
            output_path,
            "--workdir",
            "/tmp",
            "--dry-run",
        )
        assert result.returncode == 0
        content = Path(output_path).read_text(encoding="utf-8")
        # The dry-run stub must contain a valid plan_review JSON and AGENT_PLAN_STATE marker
        assert "AGENT_PLAN_STATE: approved" in content
        assert '"state": "approved"' in content
