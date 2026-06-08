"""
Minimal standalone demo of the Claude Code skill loop.

Demonstrates:
  1. Claude (host) writes a stub plan.
  2. validate_response validates it as plan_state.
  3. state_manager attach-metadata adds AGENT_LOOP_META to the plan comment.
  4. gh_ops post-issue-comment --dry-run records the plan (with metadata).
  5. Codex (dry-run) writes a canned approved plan_review stub.
  6. validate_response validates it as plan_review.
  7. state_manager attach-metadata adds AGENT_LOOP_META to the reviewer comment.
  8. gh_ops post-issue-comment --dry-run records the reviewer comment.
  9. state_manager write-session records last_completed_step=post_review.
 10. Verify _resume_plan_round can find the round from the metadata-tagged bodies.

Usage:
  python -m helpers.demo_loop --issue 123 [--repo OWNER/REPO]
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

    # Step 3: attach AGENT_LOOP_META to the plan comment (coder, round 1)
    plan_with_meta = tmpdir / "plan_with_meta.md"
    _run(
        _py(
            [
                "state_manager",
                "attach-metadata",
                "--body-file",
                str(plan_file),
                "--output",
                str(plan_with_meta),
                "--flow",
                "plan",
                "--role",
                "coder",
                "--agent",
                "Claude",
                "--round-number",
                "1",
                "--state",
                "approved",
                "--subject-plan-file",
                str(plan_file),
                "--canonical-plan-file",
                str(plan_file),
            ]
        )
    )
    print(f"demo_loop: plan comment with AGENT_LOOP_META: {plan_with_meta}")
    assert "AGENT_LOOP_META" in plan_with_meta.read_text(encoding="utf-8")

    # Step 4: dry-run post the plan comment (with metadata)
    _run(
        _py(
            [
                "gh_ops",
                "post-issue-comment",
                "--issue",
                str(args.issue),
                "--file",
                str(plan_with_meta),
                "--repo",
                args.repo,
                "--dry-run",
            ]
        )
    )

    # Step 5: Codex dry-run produces approved plan_review stub
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

    # Step 6: validate plan_review
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

    # Step 7: attach AGENT_LOOP_META to the reviewer comment
    # Compute subject from the plan file (same subject as the coder comment)
    reviewer_with_meta = tmpdir / "codex_review_with_meta.md"
    _run(
        _py(
            [
                "state_manager",
                "attach-metadata",
                "--body-file",
                str(reviewer_output),
                "--output",
                str(reviewer_with_meta),
                "--flow",
                "plan",
                "--role",
                "reviewer",
                "--agent",
                "Codex",
                "--round-number",
                "1",
                "--state",
                "approved",
                "--subject-plan-file",
                str(plan_file),
            ]
        )
    )
    print(f"demo_loop: reviewer comment with AGENT_LOOP_META: {reviewer_with_meta}")
    assert "AGENT_LOOP_META" in reviewer_with_meta.read_text(encoding="utf-8")

    # Step 8: dry-run post the reviewer comment
    _run(
        _py(
            [
                "gh_ops",
                "post-issue-comment",
                "--issue",
                str(args.issue),
                "--file",
                str(reviewer_with_meta),
                "--repo",
                args.repo,
                "--dry-run",
            ]
        )
    )

    # Step 9: record session state
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

    # Step 10: Verify _resume_plan_round finds the round from the metadata-tagged comments
    # This directly tests that attach-metadata produces valid AGENT_LOOP_META.
    from coding_review_agent_loop.round_state import _resume_plan_round

    class _FakeComment:
        def __init__(self, body: str) -> None:
            self.body = body

    fake_comments = [
        _FakeComment(plan_with_meta.read_text(encoding="utf-8")),
        _FakeComment(reviewer_with_meta.read_text(encoding="utf-8")),
    ]
    resume_result = _resume_plan_round(fake_comments, configured_reviewers=["codex"])
    assert resume_result is not None, (
        "build-resume could not find the skill-posted round — AGENT_LOOP_META not recognized"
    )
    _plan_text, resumed = resume_result
    assert resumed.round_number == 1, f"Expected round 1, got {resumed.round_number}"
    assert len(resumed.completed_reviews) == 1, (
        f"Expected 1 completed reviewer (Codex), got {len(resumed.completed_reviews)}"
    )
    print(f"demo_loop: _resume_plan_round found round {resumed.round_number} with "
          f"{len(resumed.completed_reviews)} completed reviewer(s)")

    print("demo_loop: all steps completed successfully")
    print(f"session state: {json.dumps(session_data, indent=2)}")


if __name__ == "__main__":
    main()
