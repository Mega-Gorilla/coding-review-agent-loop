"""
Minimal standalone demo of the Claude Code skill loop.

Demonstrates:
  1. Claude (host) writes a stub plan.
  2. validate_response validates it as plan_state.
  3. Codex (dry-run) writes a canned approved plan_review stub.
  4. validate_response validates it as plan_review.
  5. gh_ops post-issue-comment --dry-run records the review.
  6. state_manager write-session records last_completed_step=post_review.

Usage:
  python -m helpers.demo_loop --issue 123 [--dry-run] [--repo OWNER/REPO]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

# Make src importable when run from the repo root
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

_HELPERS = Path(__file__).parent

_HOST_STUB_PLAN = """\
## Plan

1. Create helpers/validate_response.py
2. Create helpers/state_manager.py
3. Create helpers/run_external.py
4. Create helpers/gh_ops.py
5. Create helpers/demo_loop.py
6. Create SKILL.md
7. Add tests

<!-- AGENT_PLAN_STATE: approved -->
-- Anthropic Claude (skill demo stub)
"""


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        print(f"demo_loop: command failed: {' '.join(cmd)}", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)
    return result


def _py(module_args: list[str]) -> list[str]:
    return [sys.executable, "-m", f"helpers.{module_args[0]}", *module_args[1:]]


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal skill loop demo.")
    parser.add_argument("--issue", type=int, default=123)
    parser.add_argument("--repo", default="demo/repo")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Always dry-run for demo (default: True).")
    args = parser.parse_args()

    session_id = uuid.uuid4().hex[:8]
    tmpdir = Path(tempfile.mkdtemp(prefix=f"skill-demo-{session_id}-"))
    print(f"demo_loop: session {session_id}, tmpdir {tmpdir}")

    # Step 1: write host stub plan
    plan_file = tmpdir / "plan.md"
    plan_file.write_text(_HOST_STUB_PLAN, encoding="utf-8")
    print(f"demo_loop: wrote host plan stub to {plan_file}")

    # Step 2: validate plan_state
    result = _run(_py(["validate_response", "--file", str(plan_file), "--kind", "plan_state"]))
    print(result.stdout.strip())
    assert "validation passed: plan_state" in result.stdout, result.stdout

    # Step 3: Codex dry-run produces approved plan_review stub
    reviewer_output = tmpdir / "codex_review.md"
    _run(
        _py(
            [
                "run_external",
                "--agent",
                "codex",
                "--prompt-file",
                str(plan_file),
                "--output",
                str(reviewer_output),
                "--workdir",
                str(tmpdir),
                "--dry-run",
            ]
        )
    )
    print(f"demo_loop: Codex dry-run output written to {reviewer_output}")

    # Step 4: validate plan_review
    context_file = tmpdir / "context.json"
    context_file.write_text(
        json.dumps({"reviewer": "Codex", "prior_items": [], "current_round_items": []}),
        encoding="utf-8",
    )
    result = _run(
        _py(
            [
                "validate_response",
                "--file",
                str(reviewer_output),
                "--kind",
                "plan_review",
                "--context-file",
                str(context_file),
            ]
        )
    )
    print(result.stdout.strip())
    assert "validation passed: plan_review" in result.stdout, result.stdout

    # Step 5: dry-run post issue comment
    _run(
        _py(
            [
                "gh_ops",
                "post-issue-comment",
                "--issue",
                str(args.issue),
                "--file",
                str(reviewer_output),
                "--repo",
                args.repo,
                "--dry-run",
            ]
        )
    )

    # Step 6: record session state
    _run(
        _py(
            [
                "state_manager",
                "write-session",
                "--issue",
                str(args.issue),
                "--repo",
                args.repo,
                "--fields",
                json.dumps({"last_completed_step": "post_review", "session_id": session_id}),
            ]
        )
    )

    # Verify session was written
    result = _run(
        _py(
            [
                "state_manager",
                "read-session",
                "--issue",
                str(args.issue),
                "--repo",
                args.repo,
            ]
        )
    )
    session_data = json.loads(result.stdout)
    assert session_data.get("last_completed_step") == "post_review", session_data

    print("demo_loop: all steps completed successfully")
    print(f"session state: {json.dumps(session_data, indent=2)}")


if __name__ == "__main__":
    main()
