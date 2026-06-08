"""
Run an external agent (Codex or Gemini) for one review turn.

In --dry-run mode, writes a canned approved plan_review stub to --output and exits 0.
In live mode, invokes the agent CLI and writes the response to --output.

Usage:
  python -m helpers.run_external \\
    --agent codex|gemini \\
    --prompt-file PATH \\
    --output PATH \\
    --workdir PATH \\
    [--cmd PATH] \\
    [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from coding_review_agent_loop.runner import Runner

_CANNED_PLAN_REVIEW = json.dumps(
    {
        "schema_version": 1,
        "kind": "plan_review",
        "state": "approved",
        "summary": "Dry-run stub: plan looks good.",
        "blocking_plan_issues": [],
        "same_plan_followups": [],
        "future_followups": [],
        "prior_plan_item_dispositions": [],
    },
    indent=2,
)

_CANNED_PLAN_REVIEW_FOOTER = (
    "\n<!-- AGENT_PLAN_STATE: approved -->\n-- Codex (dry-run stub)\n"
)


def _build_dry_run_response() -> str:
    return _CANNED_PLAN_REVIEW + _CANNED_PLAN_REVIEW_FOOTER


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one external agent turn.")
    parser.add_argument("--agent", required=True, choices=["codex", "gemini"])
    parser.add_argument("--prompt-file", required=True, help="Path to prompt text file.")
    parser.add_argument("--output", required=True, help="Path to write the agent response.")
    parser.add_argument("--workdir", required=True, help="Working directory for the agent.")
    parser.add_argument("--cmd", default=None, help="Agent CLI command (overrides default).")
    parser.add_argument("--dry-run", action="store_true", help="Write a canned stub and exit.")
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        output_path.write_text(_build_dry_run_response(), encoding="utf-8")
        print(f"dry-run: wrote canned plan_review stub to {output_path}")
        return

    try:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"run_external: cannot read prompt file: {exc}", file=sys.stderr)
        sys.exit(1)

    workdir = Path(args.workdir)

    # Import backends lazily to avoid heavy import in dry-run path
    from coding_review_agent_loop.agents.codex import CodexBackend
    from coding_review_agent_loop.agents.gemini import GeminiBackend
    from coding_review_agent_loop.config import AgentLoopConfig

    agent_name = args.agent
    default_cmds = {"codex": "codex", "gemini": "gemini"}
    cmd = args.cmd or default_cmds[agent_name]

    # Build a minimal config sufficient for backend.run()
    import tempfile
    log_dir = Path(tempfile.gettempdir()) / "coding-review-agent-loop" / "skill-logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    config = AgentLoopConfig(
        repo="skill/run",
        claude_dir=workdir,
        codex_dir=workdir,
        gemini_dir=workdir,
        coder="claude",
        reviewer=(agent_name,),  # type: ignore[arg-type]
        base="main",
        max_rounds=1,
        auto_merge=False,
        dry_run=False,
        allow_shared_dir=True,
        claude_cmd="claude",
        codex_cmd=cmd if agent_name == "codex" else "codex",
        gemini_cmd=cmd if agent_name == "gemini" else "gemini",
        gh_cmd="gh",
        claude_args=(),
        codex_args=(),
        gemini_args=(),
        test_command=None,
        pre_review_tests=False,
        ci_check_name="",
        ci_timeout_seconds=300,
        ci_poll_interval_seconds=30,
        quiet=False,
        log_dir=log_dir,
        progress_interval_seconds=30,
        agent_max_retries=0,
        agent_retry_backoff_seconds=(30,),
        agent_memory=False,
        refresh_agent_memory=False,
        agent_memory_dir=log_dir,
        refresh_test_profile=False,
        auto_agent_dirs=(agent_name,),  # type: ignore[arg-type]
    )

    runner = Runner(dry_run=False)
    backend = CodexBackend() if agent_name == "codex" else GeminiBackend()
    try:
        result = backend.run(runner, config, prompt)
    except Exception as exc:  # noqa: BLE001
        print(f"run_external: agent invocation failed: {exc}", file=sys.stderr)
        sys.exit(1)

    output_path.write_text(result.text, encoding="utf-8")
    print(f"agent result written to {output_path}")


if __name__ == "__main__":
    main()
