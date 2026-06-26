import base64
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import coding_review_agent_loop.orchestrator as orchestrator
from coding_review_agent_loop.agents.base import with_public_response_file_instruction
from coding_review_agent_loop.agents.claude import (
    BACKEND as CLAUDE_BACKEND,
    _normalize_claude_usage,
    _parse_claude_output,
)
from coding_review_agent_loop.agents.codex import (
    BACKEND as CODEX_BACKEND,
    _extract_codex_usage,
    _normalize_codex_usage,
)
from coding_review_agent_loop.agents.gemini import (
    BACKEND as GEMINI_BACKEND,
    PUBLIC_RESPONSE_MARKER,
    _normalize_gemini_usage,
    _parse_gemini_payload,
)
from coding_review_agent_loop.cli import (
    AgentLoopConfig,
    AgentLoopError,
    CommandResult,
    Runner,
    build_parser,
    config_from_args,
    ensure_log_dir_ignored,
    is_clarification_request,
    parse_agent_state,
    parse_pr_number,
    run_issue_loop,
    run_pr_loop,
    run_task_loop,
)
from coding_review_agent_loop.errors import QuotaResetExceededError, UnknownPriorItemDispositionError
from coding_review_agent_loop.orchestrator import (
    _decode_public_response_json_prefix,
    _format_reset_duration,
    _failure_category,
    _HumanRequirementsRecoveryContext,
    _is_transient_agent_output,
    _is_transient_public_response,
    _parse_rate_limit_reset_seconds,
    _recover_plan_revision_human_requirements_acknowledgement,
    _run_validated_agent,
    _split_reconstructable_plan_revision_response,
)
from coding_review_agent_loop.config import (
    default_agent_memory_dir,
    default_agent_workdir,
    default_cache_root,
    resolve_base_branch,
)
from coding_review_agent_loop.comment_rendering import (
    _render_public_coder_followup_comment,
    _render_public_plan_review_comment,
    _render_public_plan_revision_comment,
    _render_public_pr_review_comment,
    normalize_freeform_signature,
)
from coding_review_agent_loop.decomposition import (
    CreatedPhaseIssue,
    MAX_DECOMPOSITION_PHASES,
    RecordedPhase,
    approved_plan_hash,
    find_existing_phase_implementation_handoff,
    format_decomposition_parent_summary,
    format_one_shot_impl_handoff_comment,
    format_phase_implementation_handoff_comment,
    parse_plan_decomposition,
)
from coding_review_agent_loop.github import (
    HumanReviewRequirement,
    IssueComment,
    IssueContext,
    PullRequestReviewContext,
    PullRequestMetadata,
    get_issue_context,
    get_pr_checks,
)
from coding_review_agent_loop.followups import (
    MAX_APPROVED_FOLLOWUP_ISSUES,
    reconcile_approved_followups,
)
from coding_review_agent_loop.memory import AgentMemoryContext
from coding_review_agent_loop.migrations import MigrationValidationResult, validate_pr_migration_topology
from coding_review_agent_loop.orchestrator import (
    ITEM_SUMMARY_LIMIT,
    HUMAN_REQUIREMENTS_ACK_ITEM_ID,
    PostedRoundMetadata,
    ValidatedAgentResponse,
    _apply_unresolved_item_dispositions,
    _attach_round_metadata,
    _collect_prior_compact_summaries,
    _decode_round_metadata,
    _encode_round_metadata,
    _format_unresolved_item_label,
    _plan_subject,
    _render_public_review_comment,
    _reconcile_human_requirements_ack_item,
    _review_freeform_summary_text,
    _resume_pr_round,
    _resume_plan_round,
    _strip_round_metadata,
    _validate_coder_followup_response,
    _validate_plan_revision_response,
    _validate_review_response,
    _validate_plan_review_response,
    render_public_agent_comment,
    render_canonical_plan_revision,
    render_canonical_plan_steps,
)
from coding_review_agent_loop.prompts import (
    COMPACT_PLANNING_VOLATILE_TAIL_MARKER,
    COMPACT_PR_REVIEW_VOLATILE_TAIL_MARKER,
    CompactPlanTailContext,
    CompactPrReviewTailContext,
    CompactPriorContext,
    HUMAN_REQUIREMENTS_ADDRESSED_MARKER,
    HUMAN_REQUIREMENTS_DIRECT_DISCUSSION_ACK,
    _build_followup_guidance,
    _build_unresolved_items_guidance,
    build_followup_prompt,
    build_issue_implementation_prompt,
    build_issue_plan_prompt,
    build_issue_prompt,
    build_task_prompt,
    build_same_pr_followup_prompt,
    build_plan_review_prompt,
    build_plan_revision_prompt,
    build_review_prompt,
    format_human_requirements,
    format_issue_context,
    render_coder_human_requirements_prompt_context,
)
from coding_review_agent_loop.protocol import (
    ApprovedFollowup,
    _expect_string_list,
    _extract_structured_coder_followup_payload,
    _extract_structured_plan_review_payload,
    _extract_structured_plan_revision_payload,
    _extract_structured_pr_review_payload,
    normalize_response_file_structured_text,
    parse_approved_followups,
    parse_human_requirements_acknowledgement,
    parse_pr_review,
    parse_plan_item_dispositions,
    parse_plan_review,
    parse_plan_review_items,
    parse_plan_state,
    parse_structured_plan_review,
    parse_structured_pr_review,
    parse_review,
    parse_non_blocking_followups,
    parse_signed_human_requirement_body,
    parse_unresolved_item_dispositions,
    ReviewItemDisposition,
    UnresolvedReviewItem,
    validate_human_requirements_acknowledgement,
    validate_structured_coder_followup,
    validate_structured_human_requirements_acknowledgement,
    validate_structured_plan_state,
    validate_structured_plan_revision,
)
from coding_review_agent_loop.workdir_guard import (
    extract_reported_tests_from_response,
    validate_response_tests_within_workdir,
    validate_test_commands_within_workdir,
)

from unittest.mock import MagicMock, patch



from agent_loop_helpers import (
    FakeRunner,
    json_dumps,
    command_index,
    read_usage_summary,
    prior_item_dispositions,
    blocking_issues,
    prior_plan_item_dispositions,
    structured_pr_review,
    structured_plan_review,
    structured_plan_revision,
    structured_plan_state,
    structured_coder_followup,
    make_config,
    plan_decomposition_json,
)

@pytest.fixture(autouse=True)
def _no_real_repair():
    """Prevent attempt_repair from calling the real Gemini CLI in all tests.

    Tests that explicitly test repair behaviour patch the orchestrator-level
    import themselves, which takes precedence over this fixture.  Unit tests
    for attempt_repair itself patch subprocess.run directly, so they are
    unaffected here.
    """
    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _agent_commands_available(monkeypatch):
    """Keep config tests independent of agent CLIs installed on the test host."""
    import coding_review_agent_loop.config as config_module

    real_which = config_module.shutil.which

    def which(command):
        resolved = real_which(command)
        if resolved is not None:
            return resolved
        if command in {"claude", "codex", "gemini", "agy"}:
            return f"/mock/bin/{command}"
        return None

    monkeypatch.setattr(config_module.shutil, "which", which)

def test_task_loop_creates_pr_then_alternates_until_codex_approval(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Implemented.\n<!-- AGENT_PR: 91 -->\n<!-- AGENT_STATE: blocking -->",
            "Fixed review.\n<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=[
            "One nit.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        pr_payload={
            "number": 91,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/91",
        },
    )
    config = make_config(tmp_path)

    assert (
        run_task_loop(
            runner,
            task_text="Add a /healthz endpoint that returns 200 OK.",
            config=config,
        )
        == 0
    )

    command_names = [cmd[:2] for cmd, _cwd in runner.commands]
    assert ["claude", "--print"] in command_names
    assert ["codex", "exec"] in command_names
    assert len(runner.comments) == 4
    assert runner.comments[0].startswith("Implemented.")
    assert runner.comments[-1].startswith("**Review verdict:** Approved\n\nLGTM.")

def test_task_loop_syncs_coder_base_before_first_implementation_attempt(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Implemented.\n<!-- AGENT_PR: 91 -->\n<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        pr_payload={
            "number": 91,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/91",
        },
    )
    config = make_config(tmp_path)
    config.agent_memory_dir.mkdir(parents=True)
    (config.agent_memory_dir / "last-analyzed-commit").write_text("base123\n", encoding="utf-8")

    assert run_task_loop(runner, task_text="Add a /healthz endpoint.", config=config) == 0

    commands = runner.commands
    memory_index = command_index(commands, ["git", "diff", "--name-only"])
    fetch_index = command_index(commands, ["git", "fetch", "origin"])
    switch_index = command_index(commands, ["git", "switch", "main"])
    pull_index = command_index(commands, ["git", "pull", "--ff-only", "origin", "main"])
    coder_index = command_index(commands, ["claude", "--print"])

    assert memory_index < fetch_index < switch_index < pull_index < coder_index

def test_task_loop_picks_up_pr_url_when_marker_missing(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Opened https://github.com/OWNER/REPO/pull/77\n"
            "<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    config = make_config(tmp_path)

    assert (
        run_task_loop(
            runner,
            task_text="Tighten the rate limiter to 5 rps.",
            config=config,
        )
        == 0
    )

def test_task_loop_non_interactive_fails_on_clarification_request(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "I need to know which endpoint.\n<!-- AGENT_CLARIFY -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="non-interactive"):
        run_task_loop(
            runner,
            task_text="Add caching",
            config=config,
        )

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)
    assert runner.comments == []

def test_task_loop_interactive_supplies_clarification_then_creates_pr(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Which endpoint and how long?\n<!-- AGENT_CLARIFY -->\n-- Anthropic Claude",
            "Implemented.\n<!-- AGENT_PR: 99 -->\n<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        pr_payload={
            "number": 99,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/99",
        },
    )
    config = make_config(tmp_path)
    answers = iter(["recent-debates endpoint, 60s TTL"])

    assert (
        run_task_loop(
            runner,
            task_text="Add caching",
            config=config,
            interactive=True,
            clarification_input=lambda: next(answers),
        )
        == 0
    )

    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(claude_calls) == 2
    assert "recent-debates endpoint, 60s TTL" in claude_calls[1][-1]

def test_task_loop_interactive_aborts_after_max_clarification_rounds(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Q1?\n<!-- AGENT_CLARIFY -->",
            "Q2?\n<!-- AGENT_CLARIFY -->",
        ],
    )
    config = make_config(tmp_path)
    answers = iter(["a1", "a2"])

    with pytest.raises(AgentLoopError, match="after 1 rounds"):
        run_task_loop(
            runner,
            task_text="Refactor everything",
            config=config,
            interactive=True,
            max_clarification_rounds=1,
            clarification_input=lambda: next(answers),
        )

def test_task_loop_rejects_empty_task_text(tmp_path):
    runner = FakeRunner()
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="empty"):
        run_task_loop(runner, task_text="   ", config=config)

    assert runner.commands == []

def test_task_loop_requires_pr_or_clarification_marker(tmp_path):
    runner = FakeRunner(
        claude_outputs=["I just wrote some prose without any markers."],
    )
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="PR marker"):
        run_task_loop(runner, task_text="Do something", config=config)

def test_codex_task_loop_creates_pr_then_claude_approves(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Implemented task.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "Ship it.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    assert run_task_loop(runner, task_text="Add /healthz endpoint.", config=config) == 0

    assert len(runner.comments) == 2
    assert runner.comments[0].startswith("Implemented task.")
    assert runner.comments[1].startswith("**Review verdict:** Approved\n\nShip it.")

def test_codex_task_loop_picks_up_pr_url_when_marker_missing(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Opened https://github.com/OWNER/REPO/pull/77\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    assert run_task_loop(runner, task_text="Tighten rate limiter.", config=config) == 0

def test_codex_task_loop_rejects_empty_task_text(tmp_path):
    runner = FakeRunner()
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    with pytest.raises(AgentLoopError, match="empty"):
        run_task_loop(runner, task_text="   ", config=config)

    assert runner.commands == []

