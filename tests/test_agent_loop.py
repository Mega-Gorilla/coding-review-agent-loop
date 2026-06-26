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




















































































































def test_issue_loop_creates_pr_then_alternates_until_codex_approval(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Created PR.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->",
            "Fixed review.\n<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=[
            "Finding: bug remains.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    command_names = [cmd[:2] for cmd, _cwd in runner.commands]
    assert ["claude", "--print"] in command_names
    assert ["codex", "exec"] in command_names
    assert len(runner.comments) == 4
    assert runner.comments[-1].startswith("**Review verdict:** Approved\n\nLGTM.")
    assert list((tmp_path / "logs").glob("*-claude.log"))
    assert list((tmp_path / "logs").glob("*-codex.log"))
    assert (tmp_path / "logs" / ".gitignore").read_text(encoding="utf-8") == "*\n!.gitignore\n"


def test_issue_loop_syncs_coder_base_after_memory_before_coder(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Created PR.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)
    config.agent_memory_dir.mkdir(parents=True)
    (config.agent_memory_dir / "last-analyzed-commit").write_text("base123\n", encoding="utf-8")

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    commands = runner.commands
    issue_context_index = command_index(commands, ["gh", "issue", "view"])
    memory_index = command_index(commands, ["git", "diff", "--name-only"])
    fetch_index = command_index(commands, ["git", "fetch", "origin"])
    switch_index = command_index(commands, ["git", "switch", "main"])
    pull_index = command_index(commands, ["git", "pull", "--ff-only", "origin", "main"])
    coder_index = command_index(commands, ["claude", "--print"])

    assert issue_context_index < memory_index < fetch_index < switch_index < pull_index < coder_index






def test_get_issue_context_parses_signed_issue_body_and_comments(tmp_path):
    runner = FakeRunner(
        issue_payload={
            "number": 56,
            "title": "Support signed issue requirements",
            "body": "Use the stable API path.\n\n-- Human Reviewer",
            "url": "https://github.com/OWNER/REPO/issues/56",
            "author": {"login": "issue-author"},
            "createdAt": "2026-05-17T08:00:00Z",
        },
        issue_comments=[
            {
                "author": {"login": "maintainer"},
                "createdAt": "2026-05-17T09:00:00Z",
                "url": "https://github.com/OWNER/REPO/issues/56#issuecomment-1",
                "body": "Unsigned discussion remains normal context.",
            },
            {
                "author": {"login": "lead"},
                "createdAt": "2026-05-17T10:00:00Z",
                "url": "https://github.com/OWNER/REPO/issues/56#issuecomment-2",
                "body": "Add a regression test.\n\n-- Human Reviewer",
            },
        ],
    )
    config = make_config(tmp_path)

    issue_context = get_issue_context(runner, config=config, issue_number=56)

    assert [item.source_type for item in issue_context.human_requirements] == [
        "Issue body",
        "Issue comment",
    ]
    assert [item.author for item in issue_context.human_requirements] == ["issue-author", "lead"]
    assert [item.created_at for item in issue_context.human_requirements] == [
        "2026-05-17T08:00:00Z",
        "2026-05-17T10:00:00Z",
    ]
    assert issue_context.human_requirements[0].body == "Use the stable API path."
    assert issue_context.human_requirements[1].body == "Add a regression test."
    assert issue_context.comments[0].body == "Unsigned discussion remains normal context."






























def test_issue_loop_can_use_codex_as_coder_and_claude_as_reviewer(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Created PR.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->",
            "Fixed review.\n<!-- AGENT_STATE: blocking -->",
        ],
        claude_outputs=[
            "Finding: bug remains.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    agent_commands = [cmd[:2] for cmd, _cwd in runner.commands if cmd[:1] in (["claude"], ["codex"])]
    assert agent_commands == [
        ["codex", "exec"],
        ["claude", "--print"],
        ["codex", "exec"],
        ["claude", "--print"],
    ]
    assert len(runner.comments) == 4
    assert runner.comments[-1].startswith("**Review verdict:** Approved\n\nLGTM.")


def test_issue_loop_runs_pre_review_tests_after_coder_changes(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Created PR.\nTests: pytest passed.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->",
            "Fixed review.\nTests: pytest passed.\n<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=[
            "Finding: bug remains.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, test_command=("pytest", "tests/test_agent_loop.py"))

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    first_coder = command_index(runner.commands, ["claude", "--print"])
    first_test = commands.index(["pytest", "tests/test_agent_loop.py"])
    first_review = command_index(runner.commands, ["codex", "exec"])
    assert first_coder < first_test < first_review
    assert commands.count(["pytest", "tests/test_agent_loop.py"]) == 3


def test_pre_review_tests_can_be_disabled(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Created PR.\nTests: pytest passed.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    config = make_config(
        tmp_path,
        test_command=("pytest", "tests/test_agent_loop.py"),
        pre_review_tests=False,
    )

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    first_test = commands.index(["pytest", "tests/test_agent_loop.py"])
    first_review = command_index(runner.commands, ["codex", "exec"])
    assert first_review < first_test
    assert commands.count(["pytest", "tests/test_agent_loop.py"]) == 1










def test_ensure_log_dir_ignored_does_not_overwrite_existing_file(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    gitignore = log_dir / ".gitignore"
    gitignore.write_text("custom\n", encoding="utf-8")

    ensure_log_dir_ignored(log_dir)

    assert gitignore.read_text(encoding="utf-8") == "custom\n"


def test_pr_loop_runs_tests_and_merge_only_after_codex_approval(tmp_path):
    runner = FakeRunner(codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"])
    config = make_config(
        tmp_path,
        auto_merge=True,
        test_command=("pytest", "tests/test_agent_loop.py"),
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    assert ["pytest", "tests/test_agent_loop.py"] in commands
    assert [
        "gh",
        "api",
        "repos/OWNER/REPO/commits/abc123/check-runs",
    ] in commands
    assert [
        "gh",
        "api",
        "repos/OWNER/REPO/commits/abc123/status",
    ] in commands
    assert [
        "gh",
        "api",
        "repos/OWNER/REPO/branches/main/protection/required_status_checks",
    ] in commands
    assert ["gh", "pr", "merge", "77", "--repo", "OWNER/REPO", "--merge"] in commands


def test_pr_loop_does_not_post_gemini_diagnostics_without_agent_state(tmp_path):
    diagnostic = "[ERROR] Invalid stream: The model returned an empty response or malformed tool call."
    runner = FakeRunner(gemini_outputs=[diagnostic, diagnostic, diagnostic])
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(AgentLoopError, match="No review result was recorded"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert runner.comments == []
    assert not any(diagnostic in comment for comment in runner.comments)
    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert sleep_commands == [["sleep", "1"], ["sleep", "1"]]


@pytest.mark.parametrize(
    "text",
    [
        "orchestrator.py lines 577-581: it currently falls back to parse_plan_state(text)",
        "orchestrator.py:577-581: it currently falls back to parse_plan_state(text)",
        "A bare 500 in diagnostic prose without HTTP context.",
    ],
)
def test_source_line_references_with_5xx_numbers_are_not_transient(text):
    assert not _is_transient_agent_output(text)
    assert _failure_category(text) == "deterministic"


@pytest.mark.parametrize(
    "text",
    [
        "Internal Server Error",
        "Bad Gateway",
        "Service Unavailable",
        "Gateway Timeout",
    ],
)
def test_explicit_server_error_phrases_remain_transient(text):
    assert _is_transient_agent_output(text)
    assert _failure_category(text) == "transient"


@pytest.mark.parametrize(
    "text",
    [
        "The authoritative PR diff shows no regressions.",
        "Authoritative source confirms the change.",
        "The author of this commit fixed the bug.",
        "This is an authoritative reference.",
    ],
)
def test_auth_prefix_words_are_not_non_retryable(text):
    """Words starting with 'auth' that are not auth-failure keywords must not match."""
    from coding_review_agent_loop.transient import NON_RETRYABLE_AGENT_OUTPUT_RE

    assert not NON_RETRYABLE_AGENT_OUTPUT_RE.search(text), (
        f"NON_RETRYABLE_AGENT_OUTPUT_RE unexpectedly matched: {text!r}"
    )
    assert _is_transient_agent_output(text) is False or True  # no crash; classification is unrelated
    assert _failure_category(text) != "non-retryable"


@pytest.mark.parametrize(
    "text",
    [
        "authentication failed",
        "Authorization error",
        "auth failed",
        "unauthorized",
        "forbidden",
        "Invalid API Key",
        "billing issue",
        "credit limit exceeded",
        "dirty checkout",
    ],
)
def test_genuine_auth_and_billing_terms_remain_non_retryable(text):
    """Real auth/billing/dirty-checkout diagnostics must still be non-retryable."""
    from coding_review_agent_loop.transient import NON_RETRYABLE_AGENT_OUTPUT_RE

    assert NON_RETRYABLE_AGENT_OUTPUT_RE.search(text), (
        f"NON_RETRYABLE_AGENT_OUTPUT_RE did not match: {text!r}"
    )
    assert _failure_category(text) == "non-retryable"


def test_plan_review_does_not_post_diagnostics_without_plan_state(tmp_path):
    diagnostic = "[ERROR] Invalid stream: The model returned an empty response or malformed tool call."
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Update the CLI.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        gemini_outputs=[diagnostic, diagnostic, diagnostic],
    )
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(AgentLoopError, match="AGENT_PLAN_STATE"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)

    assert len(runner.comments) == 1
    assert runner.comments[0].startswith("Plan:")
    assert not any(diagnostic in comment for comment in runner.comments)


def test_pr_loop_retries_transient_gemini_diagnostic_and_posts_only_valid_response(tmp_path):
    diagnostic = "[ERROR] Invalid stream: The model returned an empty response or malformed tool call."
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[diagnostic, valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{valid}"]
    assert diagnostic not in runner.comments[0]
    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert sleep_commands == [["sleep", "1"]]


@pytest.mark.parametrize("terminator", ["", "."])
def test_pr_loop_retries_plain_agent_state_near_miss_once(tmp_path, terminator):
    near_miss = f"LGTM.\nAGENT_STATE: approved{terminator}\n-- Google Gemini"
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[near_miss, valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{valid}"]
    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert sleep_commands == [["sleep", "1"]]


def test_plan_loop_retries_plain_agent_plan_state_near_miss_once(tmp_path):
    near_miss = "Plan looks sound.\nAGENT_PLAN_STATE: approved.\n-- Google Gemini"
    valid = "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Update the CLI.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        gemini_outputs=[near_miss, valid],
    )
    config = make_config(tmp_path, reviewer="gemini")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert near_miss not in runner.comments
    assert any(comment == f"**Review verdict:** Approved\n\n{valid}" for comment in runner.comments)
    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert sleep_commands == [["sleep", "1"]]


def test_gemini_public_response_file_is_inside_git_dir(tmp_path):
    valid = "LGTM from response file.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=["stdout should be ignored"], public_response_outputs=[valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{valid}"]
    gemini_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["gemini"]]
    assert len(gemini_commands) == 1
    prompt = "\n".join(gemini_commands[0])
    expected_prefix = str(config.gemini_dir / ".git" / "agent-loop" / "responses" / "gemini")
    assert expected_prefix in prompt
    assert "/tmp/coding-review-agent-loop/responses/" not in prompt


def test_gemini_public_response_file_resolves_worktree_git_dir(tmp_path):
    valid = "LGTM from response file.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=["stdout should be ignored"], public_response_outputs=[valid])
    config = make_config(tmp_path, reviewer="gemini")
    git_dir = tmp_path / "main-repo" / ".git" / "worktrees" / "gemini"
    git_dir.mkdir(parents=True)
    (config.gemini_dir / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{valid}"]
    gemini_call = next(cmd for cmd, _cwd in runner.commands if cmd[:1] == ["gemini"])
    assert str(git_dir / "agent-loop" / "responses" / "gemini") in gemini_call[2]
    assert str(config.gemini_dir / ".git" / "agent-loop") not in gemini_call[2]


def test_gemini_pre_marker_429_does_not_suppress_structured_review_repair(tmp_path):
    malformed_public_review = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "approved",
                "summary": "Found one issue.",
                "blocking_items": [],
                "same_pr_followups": [],
                "future_followups": [],
                "prior_item_dispositions": [],
            }
        )
        + "\nProse between JSON and footer should be repaired.\n"
        "<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    )
    raw_stdout = (
        "Attempt 1 failed with status 429. Retrying with backoff... "
        "No capacity available for model gemini-3-flash-preview on the server.\n"
        f"{PUBLIC_RESPONSE_MARKER}\n"
        f"{malformed_public_review}"
    )
    repaired_review = structured_pr_review(
        state="approved",
        summary="Review passed after repair.",
        reviewer="Google Gemini",
    )
    runner = FakeRunner(gemini_outputs=[{"stdout": raw_stdout}])
    config = make_config(tmp_path, reviewer="gemini", agent_max_retries=0)
    captured_repairs = []

    def fake_attempt_repair(raw: str, gemini_cmd: str, *, expected_kind: str | None = None, unresolved_item_ids=None, surfaced_requirement_ids=None, allowed_prior_item_ids=None, unknown_prior_item_ids=None, same_round_context=None) -> str | None:
        captured_repairs.append(raw)
        assert expected_kind == "pr_review"
        return repaired_review

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", fake_attempt_repair):
        assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert captured_repairs == [malformed_public_review]
    assert "429" not in captured_repairs[0]
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)
    assert any("Review passed after repair." in comment for comment in runner.comments)


def test_gemini_response_file_repair_ignores_raw_stdout_transient_diagnostics(tmp_path):
    malformed_public_review = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "approved",
                "summary": "Found one issue.",
                "blocking_items": ["Approved reviews cannot have blocking items."],
                "same_pr_followups": [],
                "future_followups": [],
                "prior_item_dispositions": [],
            }
        )
        + "\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    )
    repaired_review = structured_pr_review(
        state="approved",
        summary="Response file review passed after repair.",
        reviewer="Google Gemini",
    )
    runner = FakeRunner(
        gemini_outputs=[
            {"stdout": "Attempt 1 failed with status 429. No capacity available, then recovered."}
        ],
        public_response_outputs=[{"text": malformed_public_review}],
    )
    config = make_config(tmp_path, reviewer="gemini", agent_max_retries=0)
    captured_repairs = []

    def fake_attempt_repair(raw: str, gemini_cmd: str, *, expected_kind: str | None = None, unresolved_item_ids=None, surfaced_requirement_ids=None, allowed_prior_item_ids=None, unknown_prior_item_ids=None, same_round_context=None) -> str | None:
        captured_repairs.append(raw)
        return repaired_review

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", fake_attempt_repair):
        assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert captured_repairs == [malformed_public_review]
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)
    assert any("Response file review passed after repair." in comment for comment in runner.comments)


def test_pr_loop_exhausted_transient_retry_reports_attempt_logs(tmp_path):
    diagnostic = "[ERROR] Invalid stream: The model returned an empty response or malformed tool call."
    runner = FakeRunner(gemini_outputs=[(diagnostic, 1), (diagnostic, 1), (diagnostic, 1)])
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(AgentLoopError) as exc_info:
        run_pr_loop(runner, pr_number=77, config=config)

    message = str(exc_info.value)
    assert "No review result was recorded" in message
    assert "Failure category: transient" in message
    assert "Attempt logs:" in message
    assert "gemini.log" in message
    assert runner.comments == []


def test_pr_loop_retries_quota_error(tmp_path):
    quota_output = "Quota exceeded for this project."
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[(quota_output, 1), valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{valid}"]
    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert len(sleep_commands) == 1


def test_pr_loop_does_not_retry_normal_missing_marker_response(tmp_path):
    output = "I reviewed the PR and it looks fine."
    runner = FakeRunner(gemini_outputs=[output])
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(AgentLoopError, match="AGENT_STATE"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert runner.comments == []
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)


def test_pr_loop_retries_rate_limit_429(tmp_path):
    rate_limit_output = "HTTP 429 Too Many Requests: rate limit exceeded."
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[(rate_limit_output, 1), valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{valid}"]
    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert len(sleep_commands) == 1


def test_pr_loop_retries_claude_session_limit(tmp_path):
    session_limit_output = "Error: session_limit_exceeded — too many sessions for this project."
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[(session_limit_output, 1), valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{valid}"]
    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert len(sleep_commands) == 1


def test_pr_loop_retries_gemini_no_capacity(tmp_path):
    no_capacity_output = "No capacity available for model gemini-flash on the server."
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[(no_capacity_output, 1), valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{valid}"]
    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert len(sleep_commands) == 1


def test_diagnostic_shaped_public_response_remains_transient(tmp_path):
    public_response = (
        f"{PUBLIC_RESPONSE_MARKER}\n"
        "HTTP 429 Too Many Requests: rate limit exceeded.\n"
        "<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    )
    runner = FakeRunner(gemini_outputs=[{"stdout": public_response}])
    config = make_config(tmp_path, reviewer="gemini", agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair") as repair_mock:
        with pytest.raises(AgentLoopError) as exc_info:
            run_pr_loop(runner, pr_number=77, config=config)

    repair_mock.assert_not_called()
    assert "Failure category: transient" in str(exc_info.value)


def test_public_response_error_payload_remains_transient():
    assert _is_transient_public_response(
        json.dumps(
            {
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Quota exceeded. Retry-After: 60",
                }
            }
        )
    )


def test_public_response_structured_json_after_known_artifact_is_not_transient():
    text = (
        f"{PUBLIC_RESPONSE_MARKER}\n"
        + structured_pr_review(
            summary="Wrong structured kind discusses 429, quota, capacity, and transient behavior.",
            reviewer="Google Gemini",
        )
    )

    assert not _is_transient_public_response(text, repair_expected_kind="coder_followup")


def test_structured_plan_review_transient_terms_with_trailing_prose_normalizes(tmp_path):
    malformed_review = (
        structured_plan_review(
            state="approved",
            summary=(
                "The plan discusses 429, quota, resource exhausted, timeout, capacity, "
                "and transient retry handling as domain text."
            ),
            reviewer="Google Gemini",
        )
        + "\nTrailing prose after the signature should be repaired."
    )
    runner = FakeRunner(gemini_outputs=[malformed_review])
    config = make_config(tmp_path, reviewer="gemini", agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair") as repair_mock:
        response = _run_validated_agent(
            runner,
            agent="gemini",
            config=config,
            prompt="Review the plan.",
            marker_description="<!-- AGENT_PLAN_STATE: approved|blocking -->",
            validate=lambda text: _validate_plan_review_response(
                text,
                reviewer="Google Gemini",
                unresolved_items=(),
            ),
            use_repair=True,
            repair_expected_kind="plan_review",
        )

    assert response.text == structured_plan_review(
        state="approved",
        summary=(
            "The plan discusses 429, quota, resource exhausted, timeout, capacity, "
            "and transient retry handling as domain text."
        ),
        reviewer="Google Gemini",
    )
    repair_mock.assert_not_called()
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)


def test_structured_pr_review_transient_terms_duplicate_footer_normalizes(tmp_path):
    malformed_review = (
        structured_pr_review(
            state="approved",
            summary=(
                "The review covers capacity, timeout, 429, quota, resource-exhausted, "
                "and transient classifier behavior."
            ),
            reviewer="Google Gemini",
        )
        + "\n\n<!-- AGENT_STATE: approved -->"
    )
    runner = FakeRunner(gemini_outputs=[malformed_review])
    config = make_config(tmp_path, reviewer="gemini", agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair") as repair_mock:
        response = _run_validated_agent(
            runner,
            agent="gemini",
            config=config,
            prompt="Review the PR.",
            marker_description="<!-- AGENT_STATE: approved|blocking -->",
            validate=lambda text: _validate_review_response(
                text,
                reviewer="Google Gemini",
                unresolved_items=(),
            ),
            use_repair=True,
            repair_expected_kind="pr_review",
        )

    assert response.text == structured_pr_review(
        state="approved",
        summary=(
            "The review covers capacity, timeout, 429, quota, resource-exhausted, "
            "and transient classifier behavior."
        ),
        reviewer="Google Gemini",
    )
    repair_mock.assert_not_called()
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)


def test_structured_coder_followup_transient_terms_before_footer_runs_repair(tmp_path):
    unresolved_items = (
        UnresolvedReviewItem(
            item_id="item-1",
            reviewer="OpenAI Codex",
            source_round=1,
            text="Add timeout regression coverage.",
            status="blocking",
        ),
    )
    malformed_followup = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "approved",
                "summary": "Updated timeout and capacity handling without treating prose as transient.",
                "addressed_items": ["item-1"],
                "remaining_items": [],
                "human_requirements": {
                    "addressed_ids": [],
                    "checked_discussion_directly": False,
                },
            }
        )
        + "\n## Changes made\nMentioned timeout and capacity in prose before the footer.\n"
        "<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"
    )
    repaired_followup = structured_coder_followup(
        state="approved",
        summary="Updated timeout and capacity handling.",
        addressed_items=["item-1"],
        remaining_items=[],
        reviewer="Anthropic Claude",
    )
    runner = FakeRunner(claude_outputs=[malformed_followup])
    config = make_config(tmp_path, coder="claude", agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value=repaired_followup) as repair_mock:
        response = _run_validated_agent(
            runner,
            agent="claude",
            config=config,
            prompt="Address review feedback.",
            marker_description="<!-- AGENT_STATE: approved|blocking -->",
            validate=lambda text: _validate_coder_followup_response(
                text,
                unresolved_items=unresolved_items,
                human_requirements=(),
            ),
            use_repair=True,
            repair_expected_kind="coder_followup",
        )

    assert response.text == repaired_followup
    repair_mock.assert_called_once_with(
        malformed_followup,
        config.gemini_cmd,
        expected_kind="coder_followup",
    )
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)


def test_run_validated_agent_recovers_coder_followup_from_message_text_when_response_file_markdown(tmp_path):
    unresolved_items = (
        UnresolvedReviewItem(
            item_id="item-8",
            reviewer="Orchestrator",
            source_round=4,
            text="Acknowledge signed human requirements.",
            status="blocking",
        ),
    )
    valid_followup = structured_coder_followup(
        state="approved",
        summary="Acknowledged the signed human requirements.",
        addressed_items=["item-8"],
        remaining_items=[],
        reviewer="OpenAI Codex",
    )
    markdown_response_file = (
        "### Human requirements\n\n"
        "Acknowledged.\n\n"
        "<!-- HUMAN_REQUIREMENTS_ADDRESSED -->\n"
        "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    )
    runner = FakeRunner(
        codex_outputs=[{"public_response": valid_followup, "stdout": "diagnostic output"}],
        public_response_outputs=[{"text": markdown_response_file}],
    )
    config = make_config(tmp_path, coder="codex", agent_max_retries=0)

    response = _run_validated_agent(
        runner,
        agent="codex",
        config=config,
        prompt="Address feedback.",
        marker_description="<!-- AGENT_STATE: approved|blocking -->",
        validate=lambda text: _validate_coder_followup_response(
            text,
            unresolved_items=unresolved_items,
            human_requirements=(),
        ),
        repair_expected_kind="coder_followup",
    )

    assert response.text == valid_followup
    assert response.marker_value.addressed_items == ("item-8",)


def test_run_validated_agent_recovers_fenced_coder_followup_from_raw_stdout(tmp_path):
    unresolved_items = (
        UnresolvedReviewItem(
            item_id="item-1",
            reviewer="OpenAI Codex",
            source_round=1,
            text="Fix the bug.",
            status="blocking",
        ),
    )
    valid_followup = structured_coder_followup(
        state="approved",
        addressed_items=["item-1"],
        remaining_items=[],
        reviewer="OpenAI Codex",
    )
    json_part, footer = valid_followup.split("\n<!-- AGENT_STATE:", 1)
    fenced_stdout = f"tool diagnostic\n```json\n{json_part}\n```\n<!-- AGENT_STATE:{footer}"
    runner = FakeRunner(
        codex_outputs=[{"public_response": "legacy markdown", "stdout": fenced_stdout}],
        public_response_outputs=[{"text": "### Update\nFixed it.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"}],
    )
    config = make_config(tmp_path, coder="codex", agent_max_retries=0)

    response = _run_validated_agent(
        runner,
        agent="codex",
        config=config,
        prompt="Address feedback.",
        marker_description="<!-- AGENT_STATE: approved|blocking -->",
        validate=lambda text: _validate_coder_followup_response(
            text,
            unresolved_items=unresolved_items,
            human_requirements=(),
        ),
        repair_expected_kind="coder_followup",
    )

    assert response.text == valid_followup


























# ---------------------------------------------------------------------------
# Issue #271: coder_followup path through attempt_envelope_normalization
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# Issue #275: strip_unknown_prior_item_dispositions with tightly-packed input
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Issue #274: combined envelope+disposition strip via _run_validated_agent
# ---------------------------------------------------------------------------











def test_gemini_duplicate_trailing_agent_state_marker_normalizes_without_repair(tmp_path):
    malformed_public_review = (
        structured_pr_review(
            state="approved",
            summary="Found one issue.",
            reviewer="Google Gemini",
        )
        + "\n\n<!-- AGENT_STATE: approved -->"
    )
    raw_stdout = f"{PUBLIC_RESPONSE_MARKER}\n{malformed_public_review}"
    runner = FakeRunner(gemini_outputs=[{"stdout": raw_stdout}])
    config = make_config(tmp_path, reviewer="gemini", agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair") as repair_mock:
        assert run_pr_loop(runner, pr_number=77, config=config) == 0

    repair_mock.assert_not_called()
    assert any("Found one issue." in comment for comment in runner.comments)
    assert all(comment.count("<!-- AGENT_STATE: approved -->") == 1 for comment in runner.comments)


def test_gemini_pre_marker_429_malformed_public_response_fails_deterministically(tmp_path):
    malformed_public_review = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "approved",
                "summary": "Found one issue.",
                "blocking_items": [],
                "same_pr_followups": [],
                "future_followups": [],
                "prior_item_dispositions": [],
            }
        )
        + "\nExtra prose before the footer.\n"
        "<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    )
    raw_stdout = (
        "Attempt 1 failed with status 429. No capacity available for model gemini.\n"
        f"{PUBLIC_RESPONSE_MARKER}\n"
        f"{malformed_public_review}"
    )
    runner = FakeRunner(gemini_outputs=[{"stdout": raw_stdout}])
    config = make_config(tmp_path, reviewer="gemini", agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value="still invalid"):
        with pytest.raises(AgentLoopError) as exc_info:
            run_pr_loop(runner, pr_number=77, config=config)

    message = str(exc_info.value)
    assert "Failure category: deterministic" in message
    assert "Failure category: transient" not in message


def test_pr_loop_does_not_retry_billing_credit_exhaustion(tmp_path):
    output = "Quota exceeded: billing credits are exhausted."
    runner = FakeRunner(gemini_outputs=[output])
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(AgentLoopError, match="No review result was recorded"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert runner.comments == []
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)


def test_pr_loop_does_not_retry_auth_failure(tmp_path):
    output = "Unauthorized: invalid api key provided."
    runner = FakeRunner(gemini_outputs=[output])
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(AgentLoopError, match="No review result was recorded"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert runner.comments == []
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)


def test_pr_loop_failure_log_distinguishes_transient_failure(tmp_path):
    rate_limit_output = "HTTP 429: rate limit exceeded."
    runner = FakeRunner(gemini_outputs=[(rate_limit_output, 1)] * 3)
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(AgentLoopError) as exc_info:
        run_pr_loop(runner, pr_number=77, config=config)

    message = str(exc_info.value)
    assert "transient" in message
    assert "rerun may succeed" in message


def test_pr_loop_failure_log_identifies_non_retryable(tmp_path):
    billing_output = "Your billing account has no credits remaining."
    runner = FakeRunner(gemini_outputs=[billing_output])
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(AgentLoopError) as exc_info:
        run_pr_loop(runner, pr_number=77, config=config)

    message = str(exc_info.value)
    assert "non-retryable" in message
    assert "credentials or billing" in message


def test_pr_loop_exits_immediately_on_long_reset_rate_limit(tmp_path):
    # "Retry-After: 3600" → 3600 s reset > 300 s threshold → must exit, not retry.
    rate_limit_output = "HTTP 429: rate limit exceeded. Retry-After: 3600"
    runner = FakeRunner(gemini_outputs=[(rate_limit_output, 1)])
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(QuotaResetExceededError) as exc_info:
        run_pr_loop(runner, pr_number=77, config=config)

    message = str(exc_info.value)
    assert "quota exhausted" in message.lower()
    assert "1h" in message  # 3600 s = 1h
    assert "Rerun when quota resets" in message
    # Must not have slept / retried.
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)


def test_pr_loop_exits_immediately_on_claude_session_limit_reset(tmp_path, monkeypatch):
    class FixedDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = cls(2026, 6, 3, 5, 33, 48, tzinfo=datetime.timezone.utc)
            if tz is None:
                return fixed.replace(tzinfo=None)
            return fixed.astimezone(tz)

    monkeypatch.setattr(orchestrator.datetime, "datetime", FixedDateTime)
    session_limit_output = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 429,
            "result": "You've hit your session limit · resets 1:30am (America/Los_Angeles)",
        }
    )
    runner = FakeRunner(claude_outputs=[(session_limit_output, 1)])
    config = make_config(tmp_path, reviewer="claude")

    with pytest.raises(QuotaResetExceededError) as exc_info:
        run_pr_loop(runner, pr_number=77, config=config)

    message = str(exc_info.value)
    assert "Claude quota exhausted" in message
    assert "2h 56m" in message
    assert "Rerun when quota resets" in message
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)


def test_pr_loop_retries_on_short_reset_rate_limit(tmp_path):
    # "Retry-After: 60" → 60 s reset ≤ 300 s threshold → retry automatically.
    rate_limit_output = "HTTP 429: rate limit exceeded. Retry-After: 60"
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[(rate_limit_output, 1), valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert len(sleep_commands) == 1


def test_pr_loop_retries_on_rate_limit_without_reset_time(tmp_path):
    # No parseable reset time → fall back to normal retry behavior.
    rate_limit_output = "HTTP 429: rate limit exceeded."
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[(rate_limit_output, 1), valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert len(sleep_commands) == 1


@pytest.mark.parametrize("text,expected_secs", [
    ("Retry-After: 3600", 3600),
    ("retry after 1800", 1800),
    ("retryDelay: '7200s'", 7200),
    ("try again in 2h 30m", 9000),
    ("try again in 45m", 2700),
    ("resets in 1h", 3600),
    ("reset in 5m", 300),
])
def test_parse_rate_limit_reset_seconds(text, expected_secs):
    assert _parse_rate_limit_reset_seconds(text) == expected_secs


def test_parse_rate_limit_reset_seconds_claude_absolute_time():
    now = datetime.datetime(2026, 6, 3, 5, 33, 48, tzinfo=datetime.timezone.utc)
    text = "You've hit your session limit · resets 1:30am (America/Los_Angeles)"

    assert _parse_rate_limit_reset_seconds(text, now_utc=now) == 10572


@pytest.mark.parametrize("text", [
    "HTTP 429: rate limit exceeded.",
    "Too many requests.",
    "quota exceeded",
])
def test_parse_rate_limit_reset_seconds_returns_none_when_unparseable(text):
    assert _parse_rate_limit_reset_seconds(text) is None


@pytest.mark.parametrize("seconds,expected", [
    (3600, "1h"),
    (7200, "2h"),
    (9000, "2h 30m"),
    (300, "5m"),
    (45, "45s"),
    (3660, "1h 1m"),
])
def test_format_reset_duration(seconds, expected):
    assert _format_reset_duration(seconds) == expected


def test_quota_reset_exceeded_error_exit_code():
    assert QuotaResetExceededError.EXIT_CODE == 3


def test_pr_loop_reinjects_blocking_item_when_human_requirement_marker_missing(tmp_path):
    # Reviewer approves without HUMAN_REQUIREMENTS_RESOLVED → synthetic blocking item,
    # loop hits max_rounds (set to 1) instead of a terminal deadlock.
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        pr_payload={
            "number": 77,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/77",
            "title": "Improve review prompt context",
            "headRefName": "feature/review-context",
            "baseRefName": "main",
            "headRefOid": "abc123",
            "comments": [
                {
                    "author": {"login": "maintainer"},
                    "createdAt": "2026-05-18T10:00:00Z",
                    "url": "https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                    "body": "Please use the absolute URL.\n\n-- Human Reviewer",
                }
            ],
            "reviews": [],
        },
    )
    config = make_config(
        tmp_path,
        auto_merge=True,
        test_command=("pytest", "tests/test_agent_loop.py"),
        approved_followups="summarize",
        max_rounds=1,
    )

    # The old behaviour was a terminal deadlock; now the loop continues and hits max_rounds.
    with pytest.raises(AgentLoopError, match="blocking issues after round 1"):
        run_pr_loop(runner, pr_number=77, config=config)

    commands = [cmd for cmd, _cwd in runner.commands]
    assert ["pytest", "tests/test_agent_loop.py"] not in commands
    assert ["gh", "pr", "merge", "77", "--repo", "OWNER/REPO", "--merge"] not in commands
    assert not any(comment.startswith("Approved-review future follow-ups") for comment in runner.comments)


def test_pr_loop_recovers_when_second_reviewer_includes_human_requirement_marker(tmp_path):
    # Round 1: reviewer approves without HUMAN_REQUIREMENTS_RESOLVED → blocking item injected.
    # Round 2: coder addresses it; reviewer approves with the marker → success.
    pr_payload = {
        "number": 77,
        "state": "OPEN",
        "url": "https://github.com/OWNER/REPO/pull/77",
        "title": "Improve review prompt context",
        "headRefName": "feature/review-context",
        "baseRefName": "main",
        "headRefOid": "abc123",
        "comments": [
            {
                "author": {"login": "maintainer"},
                "createdAt": "2026-05-18T10:00:00Z",
                "url": "https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                "body": "Please use the absolute URL.\n\n-- Human Reviewer",
            }
        ],
        "reviews": [],
    }
    runner = FakeRunner(
        claude_outputs=[
            # Round 2: coder addresses the re-injected blocking item and acknowledges human requirements
            "Addressed human requirements.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: used the absolute URL.\n"
            "<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            # Round 1: approves but forgets the marker
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
            # Round 2: resolves the synthetic blocking item and acknowledges human requirements
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- HUMAN_REQUIREMENTS_RESOLVED -->\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        pr_payload=pr_payload,
    )
    config = make_config(tmp_path, max_rounds=2)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0


def test_pr_loop_allows_approval_with_human_requirement_resolution_marker(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "LGTM.\n<!-- HUMAN_REQUIREMENTS_RESOLVED -->\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        pr_payload={
            "number": 77,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/77",
            "title": "Improve review prompt context",
            "headRefName": "feature/review-context",
            "baseRefName": "main",
            "headRefOid": "abc123",
            "comments": [
                {
                    "author": {"login": "maintainer"},
                    "createdAt": "2026-05-18T10:00:00Z",
                    "url": "https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                    "body": "Please use the absolute URL.\n\n-- Human Reviewer",
                }
            ],
            "reviews": [],
        },
    )
    config = make_config(tmp_path, test_command=("pytest", "tests/test_agent_loop.py"))

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    assert ["pytest", "tests/test_agent_loop.py"] in commands


def test_pr_loop_accepts_structured_coder_followup_in_pr_round(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "coder_followup",
                    "state": "blocking",
                    "summary": "Added the requested regression test.",
                    "addressed_items": ["item-1"],
                    "remaining_items": [],
                    "addressed_item_notes": {
                        "item-1": "Added the structured coder follow-up regression case."
                    },
                    "human_requirements": {
                        "addressed_ids": [],
                        "checked_discussion_directly": False,
                    },
                    "tests_run": ["pytest tests/test_agent_loop.py -k structured_coder_followup"],
                }
            )
            + "\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
        ],
        codex_outputs=[
            "Need one more regression test before merge."
            + blocking_issues("Add the structured coder follow-up regression case.")
            + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", max_rounds=2)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    followup_comments = [comment for comment in runner.comments if "## Coder follow-up" in comment]
    assert len(followup_comments) == 1
    visible_followup = _strip_round_metadata(followup_comments[0])
    assert "Added the requested regression test." in visible_followup
    assert "### Addressed items\n- item-1: Blocking issue from OpenAI Codex" in visible_followup
    assert "  - Resolution: Added the structured coder follow-up regression case." in visible_followup
    assert "### Remaining items\n- None." in visible_followup
    assert (
        "### Tests run\n- pytest tests/test_agent_loop.py -k structured_coder_followup"
        in visible_followup
    )
    assert '"kind": "coder_followup"' not in visible_followup


def test_pr_loop_rejects_malformed_structured_coder_followup_before_re_review(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "coder_followup",
                    "state": "blocking",
                    "summary": "Tried to handle the feedback.",
                    "addressed_items": ["item-9"],
                    "remaining_items": [],
                    "human_requirements": {
                        "addressed_ids": [],
                        "checked_discussion_directly": False,
                    },
                }
            )
            + "\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
        ],
        codex_outputs=[
            "Need one more regression test before merge."
            + blocking_issues("Add the structured coder follow-up regression case.")
            + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(
        tmp_path,
        coder="claude",
        reviewer="codex",
        max_rounds=2,
        agent_max_retries=0,
    )

    with pytest.raises(
        AgentLoopError,
        match="Coder follow-up referenced unknown unresolved reviewer item IDs: item-9",
    ):
        run_pr_loop(runner, pr_number=77, config=config)


def test_reconcile_human_requirements_ack_item_surfaces_markdown_ack_blocker():
    human_requirements = (
        HumanReviewRequirement(
            source_type="PR comment",
            author="reviewer",
            created_at="2026-05-18T10:00:00Z",
            url="https://github.com/OWNER/REPO/pull/77#issuecomment-1",
            body="Please use the absolute URL.",
        ),
    )

    reconciled = _reconcile_human_requirements_ack_item(
        (),
        coder_output="Implemented fix without the extra acknowledgement.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        human_requirements=human_requirements,
        source_round=2,
    )

    assert [item.item_id for item in reconciled] == [HUMAN_REQUIREMENTS_ACK_ITEM_ID]
    assert "missing required signed human requirements marker" in reconciled[0].text


def test_reconcile_human_requirements_ack_item_clears_markdown_ack_blocker():
    human_requirements = (
        HumanReviewRequirement(
            source_type="PR comment",
            author="reviewer",
            created_at="2026-05-18T10:00:00Z",
            url="https://github.com/OWNER/REPO/pull/77#issuecomment-1",
            body="Please use the absolute URL.",
        ),
    )

    reconciled = _reconcile_human_requirements_ack_item(
        (
            UnresolvedReviewItem(
                item_id=HUMAN_REQUIREMENTS_ACK_ITEM_ID,
                reviewer="Orchestrator",
                source_round=1,
                text="Ack missing.",
                status="blocking",
            ),
        ),
        coder_output=(
            "Implemented follow-up.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: updated the URL handling.\n"
            "<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
        ),
        human_requirements=human_requirements,
        source_round=2,
    )

    assert reconciled == []


def test_pr_loop_revalidates_latest_coder_output_against_refreshed_human_requirements(
    tmp_path, monkeypatch
):
    runner = FakeRunner(
        claude_outputs=[
            "Implemented fix with the required acknowledgement.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: updated the URL handling.\n"
            "<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Blocking issue.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        pr_payload={
            "number": 77,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/77",
            "title": "Improve review prompt context",
            "headRefName": "feature/review-context",
            "baseRefName": "main",
            "headRefOid": "abc123",
            "comments": [
                {
                    "author": {"login": "maintainer"},
                    "createdAt": "2026-05-18T10:00:00Z",
                    "url": "https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                    "body": "Please use the absolute URL.\n\n-- Human Reviewer",
                }
            ],
            "reviews": [],
        },
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", max_rounds=2)
    metadata = PullRequestMetadata(
        number=77,
        repo="OWNER/REPO",
        title="Improve review prompt context",
        head_branch="feature/review-context",
        base_branch="main",
        head_sha="abc123",
        url="https://github.com/OWNER/REPO/pull/77",
    )
    contexts = iter(
        [
            PullRequestReviewContext(
                metadata=metadata,
                comments=(),
                human_requirements=(
                    HumanReviewRequirement(
                        source_type="PR comment",
                        author="maintainer",
                        created_at="2026-05-18T10:00:00Z",
                        url="https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                        body="Please use the absolute URL.",
                    ),
                ),
            ),
            PullRequestReviewContext(
                metadata=metadata,
                comments=(),
                human_requirements=(),
            ),
        ]
    )

    monkeypatch.setattr(
        "coding_review_agent_loop.orchestrator.get_pr_review_context",
        lambda *args, **kwargs: next(contexts),
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    review_prompts = [cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"]]
    assert len(review_prompts) == 2
    assert HUMAN_REQUIREMENTS_ACK_ITEM_ID not in review_prompts[1]


def test_pr_loop_routes_migration_validation_failure_through_coder_followup(tmp_path, monkeypatch):
    runner = FakeRunner(
        claude_outputs=["Fixed migration.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
        codex_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
            "LGTM again."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, test_command=("pytest", "tests/test_agent_loop.py"), max_rounds=2)
    validations = iter(
        [
            MigrationValidationResult(
                ok=False,
                message=(
                    "alembic/versions/e4f5a6b7c8d9_add_pricing.py declares `down_revision = '5d5f0e1a2b3c'`; "
                    "expected current head `402b9e8af79b`."
                ),
            ),
            MigrationValidationResult(ok=True),
        ]
    )

    monkeypatch.setattr(
        "coding_review_agent_loop.orchestrator.validate_pr_migration_topology",
        lambda *args, **kwargs: next(validations),
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    coder_prompts = [cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(coder_prompts) == 1
    assert "Alembic migration validation unresolved blocking item [item-1]" in coder_prompts[0]
    assert "expected current head `402b9e8af79b`" in coder_prompts[0]

    commands = runner.commands
    pytest_index = command_index(commands, ["pytest", "tests/test_agent_loop.py"])
    first_review_index = [
        index for index, (cmd, _cwd) in enumerate(commands) if cmd[:2] == ["codex", "exec"]
    ][0]
    second_review_index = [
        index for index, (cmd, _cwd) in enumerate(commands) if cmd[:2] == ["codex", "exec"]
    ][1]
    assert first_review_index < pytest_index < second_review_index






def test_pr_loop_routes_failing_github_checks_through_coder_followup(tmp_path, monkeypatch):
    runner = FakeRunner(
        codex_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
            "Still failing upstream."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=["Investigated CI.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(tmp_path, max_rounds=2)
    check_states = iter(
        [
            {
                "check_runs": [
                    {"name": "tests/test_server.py", "status": "completed", "conclusion": "success"},
                    {"name": "tests/test_security.py", "status": "completed", "conclusion": "failure"},
                ]
            },
            {"check_runs": [{"name": "test", "status": "completed", "conclusion": "success"}]},
        ]
    )

    def advance_checks(*_args, **_kwargs):
        runner.pr_check_runs_payload = next(check_states)
        return original_get_pr_checks(*_args, **_kwargs)

    from coding_review_agent_loop import orchestrator as orchestrator_module

    original_get_pr_checks = orchestrator_module.get_pr_checks
    monkeypatch.setattr(orchestrator_module, "get_pr_checks", advance_checks)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert any(
        comment.startswith("GitHub PR checks are failing for PR #77.") for comment in runner.comments
    )
    followup_prompt = next(
        cmd[-1]
        for cmd, _cwd in runner.commands
        if cmd[:1] == ["claude"]
        and "GitHub PR checks unresolved blocking item [item-1] from round 1:" in cmd[-1]
    )
    assert "Failing checks: tests/test_security.py (failure)" in followup_prompt
    assert "Do not claim global test success unless GitHub PR checks are green." in followup_prompt


def test_pr_loop_blocks_final_approval_when_github_checks_pending(tmp_path):
    runner = FakeRunner(
        codex_outputs=["Looks good locally.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        pr_check_runs_payload={"check_runs": []},
        pr_status_payload={"state": "pending", "statuses": []},
        pr_branch_protection_payload={"contexts": ["test"]},
    )
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="GitHub PR checks for PR #77 are pending"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert any(
        comment.startswith("GitHub PR checks are still pending for PR #77.")
        for comment in runner.comments
    )


def test_pr_loop_summarizes_approved_followups_before_pending_check_exit(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Looks good locally.\n\n### Future follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        pr_check_runs_payload={"check_runs": []},
        pr_status_payload={"state": "pending", "statuses": []},
        pr_branch_protection_payload={"contexts": ["test"]},
    )
    config = make_config(tmp_path, approved_followups="summarize")

    with pytest.raises(AgentLoopError, match="GitHub PR checks for PR #77 are pending"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert len(runner.comments) == 3
    assert runner.comments[1].startswith("Approved-review future follow-ups for PR #77:")
    assert "- Add cleanup docs. (Codex)" in runner.comments[1]
    assert "<!-- AGENT_APPROVED_FOLLOWUPS: pr=77 head=abc123 mode=summarize -->" in runner.comments[1]
    assert runner.comments[2].startswith("GitHub PR checks are still pending for PR #77.")


def test_pr_loop_summary_marker_has_single_blank_line_before_footer_marker(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Looks good locally.\n\n### Future follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
    )
    config = make_config(tmp_path, approved_followups="summarize")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    summary = runner.comments[-1]
    assert (
        "These were mentioned in approved reviews as future work and did not block merge readiness.\n\n"
        "<!-- AGENT_APPROVED_FOLLOWUPS: pr=77 head=abc123 mode=summarize -->\n"
        "-- coding-review-agent-loop"
    ) in summary


def test_pr_loop_creates_approved_followup_issues_before_unavailable_check_exit(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Looks good locally.\n\n### Future follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        pr_check_runs_payload={"check_runs": []},
        pr_branch_protection_returncode=1,
        pr_branch_protection_stderr="500 Internal Server Error",
        pr_check_runs_returncode=1,
        pr_check_runs_stderr="500 Internal Server Error",
        pr_status_returncode=1,
        pr_status_stderr="500 Internal Server Error",
    )
    config = make_config(tmp_path, approved_followups="issue")

    with pytest.raises(AgentLoopError, match="GitHub PR checks for PR #77 are unavailable"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert len(runner.issues) == 1
    assert runner.issues[0]["title"] == "Follow up future review note: Add cleanup docs."
    assert len(runner.comments) == 3
    assert runner.comments[1].startswith("Created approved-review future follow-up issues for PR #77:")
    assert "<!-- AGENT_APPROVED_FOLLOWUPS: pr=77 head=abc123 mode=issue -->" in runner.comments[1]
    assert runner.comments[2].startswith("GitHub PR check status is unavailable for PR #77.")


def test_pr_loop_skips_duplicate_approved_followup_issue_creation_when_marker_exists(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex approves.\n\n### Future follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        pr_payload={
            "comments": [
                {
                    "author": {"login": "coding-review-agent-loop"},
                    "createdAt": "2026-05-22T10:00:00Z",
                    "body": (
                        "Created approved-review future follow-up issues for PR #77:\n\n"
                        "- https://github.com/OWNER/REPO/issues/99\n\n"
                        "These were mentioned in approved reviews as future work and did not block merge readiness.\n\n"
                        "<!-- AGENT_APPROVED_FOLLOWUPS: pr=77 head=abc123 mode=issue -->\n"
                        "-- coding-review-agent-loop"
                    ),
                }
            ]
        },
    )
    config = make_config(tmp_path, approved_followups="issue")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.issues == []
    assert runner.comments == [
        "**Review verdict:** Approved\n\n"
        "Codex approves.\n\n### Future follow-ups\n- Add cleanup docs.\n"
        "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    ]


def test_pr_loop_allows_repos_without_github_checks_when_branch_protection_404(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        pr_check_runs_payload={"check_runs": []},
        pr_status_payload={"state": "pending", "statuses": []},
        pr_branch_protection_returncode=1,
        pr_branch_protection_stderr="404 Not Found",
    )
    config = make_config(tmp_path)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0
    assert not any(comment.startswith("GitHub PR checks are") for comment in runner.comments)


def test_pr_loop_allows_repos_without_github_checks_when_branch_protection_403(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        pr_check_runs_payload={"check_runs": []},
        pr_status_payload={"state": "pending", "statuses": []},
        pr_branch_protection_returncode=1,
        pr_branch_protection_stderr="403 Forbidden",
    )
    config = make_config(tmp_path)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0
    assert not any(comment.startswith("GitHub PR checks are") for comment in runner.comments)










def test_get_pr_checks_returns_no_checks_in_dry_run(tmp_path):
    runner = FakeRunner()
    config = make_config(tmp_path, dry_run=True)

    pr_checks = get_pr_checks(
        runner,
        config=config,
        metadata=PullRequestMetadata(
            number=77,
            repo="OWNER/REPO",
            title="Improve review prompt context",
            head_branch="feature/review-context",
            base_branch="main",
            head_sha="abc123",
            url="https://github.com/OWNER/REPO/pull/77",
        ),
    )

    assert pr_checks.state == "no_checks"
    assert pr_checks.branch_protection_status == "unavailable"
    assert pr_checks.branch_protection_note == "Dry run mode does not query live GitHub PR checks."






def test_pr_loop_combines_issue_and_pr_signed_human_requirements(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "LGTM.\n<!-- HUMAN_REQUIREMENTS_RESOLVED -->\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        pr_payload={
            "comments": [
                {
                    "author": {"login": "maintainer"},
                    "createdAt": "2026-05-18T10:00:00Z",
                    "url": "https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                    "body": "Use the absolute URL in the PR path.\n\n-- Human Reviewer",
                }
            ],
            "reviews": [],
        },
    )
    config = make_config(tmp_path, reviewer="codex")
    issue_context = IssueContext(
        number=56,
        repo="OWNER/REPO",
        title="Support issue comments",
        body="Original request.",
        url="https://github.com/OWNER/REPO/issues/56",
        comments=(),
        human_requirements=(
            HumanReviewRequirement(
                source_type="Issue body",
                author="issue-author",
                created_at="2026-05-17T08:00:00Z",
                url="https://github.com/OWNER/REPO/issues/56",
                body="Preserve backward compatibility.",
            ),
        ),
    )

    assert run_pr_loop(runner, pr_number=77, config=config, issue_context=issue_context) == 0

    prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    assert "Preserve backward compatibility." in prompt
    assert "Use the absolute URL in the PR path." in prompt
    assert prompt.index("Preserve backward compatibility.") < prompt.index(
        "Use the absolute URL in the PR path."
    )














def test_pr_loop_keeps_blocking_review_when_future_followups_are_misclassified(tmp_path):
    runner = FakeRunner(
        gemini_outputs=[
            "Still blocked.\n\n"
            "### Same-PR follow-ups\n"
            "- Tighten the reset helper.\n\n"
            "### Future follow-ups\n"
            "- Consider a broader cleanup later.\n\n"
            "<!-- AGENT_STATE: blocking -->\n"
            "-- Google Gemini",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved", "[item-2] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- Google Gemini",
        ],
        codex_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved", "[item-2] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=["Fixed review.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(
        tmp_path,
        reviewer=("gemini", "codex"),
        approved_followups="fix-and-issue",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments[0].startswith("**Review verdict:** Blocking\n\nStill blocked.")
    assert "Consider a broader cleanup later." not in runner.comments[0]
    followup_prompt = next(
        cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["claude"] and "Address the review below" in cmd[-1]
    )
    assert "Still blocked." in followup_prompt




def test_agent_memory_default_parent_ignores_generated_contents(tmp_path):
    runner = FakeRunner(codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"])
    config = make_config(tmp_path)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    gitignore = tmp_path / "claude" / ".agent-loop" / ".gitignore"
    assert gitignore.read_text(encoding="utf-8") == "*\n!.gitignore\n"


def test_agent_memory_does_not_ignore_custom_parent_directory(tmp_path):
    runner = FakeRunner(codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"])
    memory_dir = tmp_path / "custom-memory"
    config = make_config(tmp_path, agent_memory_dir=memory_dir)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert not (tmp_path / ".gitignore").exists()


def test_agent_memory_detects_changed_files_since_previous_commit(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        git_head="def456",
        changed_files=["src/coding_review_agent_loop/prompts.py", "tests/test_agent_loop.py"],
    )
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "last-analyzed-commit").write_text("abc123\n", encoding="utf-8")
    config = make_config(tmp_path, agent_memory_dir=memory_dir)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    diff_commands = [cmd for cmd, _cwd in runner.commands if cmd[:3] == ["git", "diff", "--name-only"]]
    assert ["git", "diff", "--name-only", "abc123..def456"] in diff_commands
    prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    assert "src/coding_review_agent_loop/prompts.py" in prompt
    assert "tests/test_agent_loop.py" in prompt
    assert (memory_dir / "last-analyzed-commit").read_text(encoding="utf-8") == "def456\n"


def test_agent_memory_logs_when_changed_file_diff_falls_back(tmp_path, capsys):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        git_head="def456",
        diff_returncode=128,
        diff_stderr="fatal: bad revision 'abc123..def456'",
    )
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "last-analyzed-commit").write_text("abc123\n", encoding="utf-8")
    config = make_config(tmp_path, agent_memory_dir=memory_dir, quiet=False)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    captured = capsys.readouterr()
    assert "Could not diff agent memory baseline abc123..def456" in captured.err
    assert "treating all tracked files as changed" in captured.err
    prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    assert "README.md" in prompt


def test_test_profile_records_provided_test_command(tmp_path):
    runner = FakeRunner(codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"])
    memory_dir = tmp_path / "memory"
    config = make_config(
        tmp_path,
        agent_memory_dir=memory_dir,
        test_command=("python", "-m", "pytest", "-q"),
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    profile = (memory_dir / "test-profile.md").read_text(encoding="utf-8")
    assert "`python -m pytest -q`" in profile
    prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    assert "prefer verified test commands from the execution profile" in prompt


def test_agent_memory_can_be_disabled(tmp_path):
    runner = FakeRunner(codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"])
    memory_dir = tmp_path / "memory"
    config = make_config(tmp_path, agent_memory=False, agent_memory_dir=memory_dir)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert not memory_dir.exists()
    prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    assert "Agent memory context:" not in prompt


def test_pr_loop_requires_all_reviewers_to_approve(tmp_path):
    runner = FakeRunner(
        codex_outputs=["Codex approves.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        claude_outputs=["Claude approves.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude"),
        auto_merge=True,
        test_command=("pytest", "tests/test_agent_loop.py"),
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    agent_commands = [cmd[:2] for cmd, _cwd in runner.commands if cmd[:1] in (["claude"], ["codex"])]
    assert agent_commands == [["codex", "exec"], ["claude", "--print"]]
    assert len(runner.comments) == 2
    commands = [cmd for cmd, _cwd in runner.commands]
    metadata_fetches = [
        cmd
        for cmd in commands
        if cmd[:3] == ["gh", "pr", "view"]
        and "--json" in cmd
        and cmd[cmd.index("--json") + 1]
        == "number,title,headRefName,baseRefName,headRefOid,url,body,comments,reviews"
    ]
    assert len(metadata_fetches) == 1
    assert ["pytest", "tests/test_agent_loop.py"] in commands
    assert ["gh", "pr", "merge", "77", "--repo", "OWNER/REPO", "--merge"] in commands


def test_pr_loop_ignores_approved_followups_by_default(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "LGTM.\n\n### Non-blocking follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
    )
    config = make_config(tmp_path)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [
        "**Review verdict:** Approved\n\n"
        "LGTM.\n\n### Future follow-ups\n- Add cleanup docs.\n"
        "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    ]


def test_pr_loop_summarizes_approved_followups_from_multiple_reviewers(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex approves.\n\n### Non-blocking follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        claude_outputs=[
            "Claude approves.\n\n### Non-blocking follow-ups\n- Add regression coverage.\n"
            "<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude"),
        approved_followups="summarize",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.comments) == 3
    summary = runner.comments[-1]
    assert summary.startswith("Approved-review future follow-ups for PR #77:")
    assert "- Add cleanup docs. (Codex)" in summary
    assert "- Add regression coverage. (Claude)" in summary
    assert "future work and did not block merge readiness" in summary
    assert summary.endswith("-- coding-review-agent-loop")


def test_pr_loop_creates_issues_for_approved_followups(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex approves.\n\n### Non-blocking follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        claude_outputs=[
            "Claude approves.\n\n### Non-blocking follow-ups\n- Add regression coverage.\n"
            "<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude"),
        approved_followups="issue",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.comments) == 3
    assert runner.issues == [
        {
            "title": "Follow up future review note: Add cleanup docs.",
            "body": (
                "Future follow-up from approved review on PR #77.\n\n"
                "Reviewer: Codex\n\n"
                "Follow-up:\n"
                "- Add cleanup docs.\n\n"
                "Original reviewer notes:\n"
                "- Codex: Add cleanup docs.\n\n"
                "This was mentioned in an approved review as future work and did not block merge readiness."
            ),
        },
        {
            "title": "Follow up future review note: Add regression coverage.",
            "body": (
                "Future follow-up from approved review on PR #77.\n\n"
                "Reviewer: Claude\n\n"
                "Follow-up:\n"
                "- Add regression coverage.\n\n"
                "Original reviewer notes:\n"
                "- Claude: Add regression coverage.\n\n"
                "This was mentioned in an approved review as future work and did not block merge readiness."
            ),
        },
    ]
    issue_summary = runner.comments[-1]
    assert issue_summary.startswith("Created approved-review future follow-up issues for PR #77:")
    assert "- https://github.com/OWNER/REPO/issues/99" in issue_summary
    assert issue_summary.count("https://github.com/OWNER/REPO/issues/99") == 1
    assert "future work and did not block merge readiness" in issue_summary
    assert issue_summary.endswith("-- coding-review-agent-loop")


def test_pr_loop_deduplicates_approved_followup_issues_across_reviewers(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex approves.\n\n### Future follow-ups\n"
            "- **Remote validation**: Validate explicit workdir git remotes against the target repo.\n"
            "- Add a distinct dry-run smoke test.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        claude_outputs=[
            "Claude approves.\n\n### Future follow-ups\n"
            "- **Remote validation**: Validate explicit workdir git remotes against the target repo.\n"
            "- Document cache cleanup behavior.\n"
            "<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"
        ],
        issue_urls=[
            "https://github.com/OWNER/REPO/issues/99",
            "https://github.com/OWNER/REPO/issues/100",
            "https://github.com/OWNER/REPO/issues/101",
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude"),
        approved_followups="issue",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert [issue["title"] for issue in runner.issues] == [
        "Follow up future review note: **Remote validation**: Validate explicit workdir git remotes against the target repo.",
        "Follow up future review note: Add a distinct dry-run smoke test.",
        "Follow up future review note: Document cache cleanup behavior.",
    ]
    remote_body = runner.issues[0]["body"]
    assert "Reviewers:\n- Codex\n- Claude" in remote_body
    assert "Original reviewer notes:" in remote_body
    assert "- Codex: **Remote validation**" in remote_body
    assert "- Claude: **Remote validation**" in remote_body
    issue_summary = runner.comments[-1]
    assert "- https://github.com/OWNER/REPO/issues/99" in issue_summary
    assert "- https://github.com/OWNER/REPO/issues/100" in issue_summary
    assert "- https://github.com/OWNER/REPO/issues/101" in issue_summary


def test_reconcile_approved_followups_groups_semantic_duplicates_and_preserves_distinct_items():
    reconciliation = reconcile_approved_followups(
        [
            ApprovedFollowup(
                reviewer="Claude",
                text="Clarify repair-pass ownership across the flowchart and sequence diagram.",
            ),
            ApprovedFollowup(
                reviewer="Gemini",
                text="Document repair pass ownership in the flowchart and sequence diagram so the handoff is clear.",
            ),
            ApprovedFollowup(
                reviewer="Codex",
                text="Add memory freshness checks before planning starts.",
            ),
            ApprovedFollowup(
                reviewer="Claude",
                text="Add sync-before-planning coverage for reviewer workdirs.",
            ),
        ],
        issue_limit=MAX_APPROVED_FOLLOWUP_ISSUES,
    )

    assert len(reconciliation.groups) == 3
    assert reconciliation.deduplicated_count == 1
    assert reconciliation.skipped_by_cap == 0
    grouped_reviewers = [group.reviewers for group in reconciliation.groups]
    assert ("Claude", "Gemini") in grouped_reviewers
    assert any("memory freshness" in group.text for group in reconciliation.groups)
    assert any("sync-before-planning" in group.text for group in reconciliation.groups)


def test_reconcile_approved_followups_selects_more_specific_canonical_wording_and_caps():
    reconciliation = reconcile_approved_followups(
        [
            ApprovedFollowup(reviewer="Claude", text="Clarify repair-pass ownership."),
            ApprovedFollowup(
                reviewer="Gemini",
                text="Clarify repair-pass ownership in `docs/local_agent_loop.md` and the sequence diagram.",
            ),
            ApprovedFollowup(reviewer="Codex", text="Follow up two."),
            ApprovedFollowup(reviewer="Claude", text="Follow up three."),
            ApprovedFollowup(reviewer="Gemini", text="Follow up four."),
        ],
        issue_limit=3,
    )

    assert reconciliation.groups[0].text == (
        "Clarify repair-pass ownership in `docs/local_agent_loop.md` and the sequence diagram."
    )
    assert len(reconciliation.selected_groups) == 3
    assert reconciliation.skipped_by_cap == 1
    assert reconciliation.deduplicated_count == 1


def test_pr_loop_files_earlier_future_followup_not_repeated_in_final_round(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            structured_pr_review(
                state="approved",
                summary="Codex approves with a later cleanup.",
                future_followups=["Add memory freshness checks before planning starts."],
                reviewer="OpenAI Codex",
            ),
            structured_pr_review(
                state="approved",
                summary="Codex final approval.",
                prior_item_dispositions=[
                    {
                        "item_id": "item-1",
                        "disposition": "future",
                        "note": "Still useful as separate tracking.",
                    },
                    {"item_id": "item-2", "disposition": "resolved"},
                ],
                reviewer="OpenAI Codex",
            ),
        ],
        claude_outputs=[
            structured_pr_review(
                state="blocking",
                summary="Need one current-PR fix.",
                blocking_items=["Fix the current sync regression."],
                reviewer="Anthropic Claude",
            ),
            structured_coder_followup(
                addressed_items=["item-2"],
                remaining_items=["item-1"],
                reviewer="Anthropic Claude",
            ),
            structured_pr_review(
                state="approved",
                summary="Claude final approval.",
                prior_item_dispositions=[
                    {"item_id": "item-1", "disposition": "future", "note": "Still valid."},
                    {"item_id": "item-2", "disposition": "resolved"},
                ],
                reviewer="Anthropic Claude",
            ),
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude"),
        approved_followups="issue",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.issues) == 1
    assert runner.issues[0]["title"] == (
        "Follow up future review note: Add memory freshness checks before planning starts."
    )
    assert "Update from Codex: Still useful as separate tracking." in runner.issues[0]["body"]


def test_pr_loop_does_not_file_resolved_earlier_future_followup(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            structured_pr_review(
                state="approved",
                summary="Codex approves with a later cleanup.",
                future_followups=["Remove stale final-round-only follow-up handling."],
                reviewer="OpenAI Codex",
            ),
            structured_pr_review(
                state="approved",
                summary="Codex final approval.",
                prior_item_dispositions=[
                    {
                        "item_id": "item-1",
                        "disposition": "resolved",
                        "note": "Fixed in the second commit.",
                    },
                    {"item_id": "item-2", "disposition": "resolved"},
                ],
                reviewer="OpenAI Codex",
            ),
        ],
        claude_outputs=[
            structured_pr_review(
                state="blocking",
                summary="Need one current-PR fix.",
                blocking_items=["Fix the current sync regression."],
                reviewer="Anthropic Claude",
            ),
            structured_coder_followup(
                addressed_items=["item-2"],
                remaining_items=["item-1"],
                reviewer="Anthropic Claude",
            ),
            structured_pr_review(
                state="approved",
                summary="Claude final approval.",
                prior_item_dispositions=[
                    {"item_id": "item-1", "disposition": "resolved", "note": "Fixed."},
                    {"item_id": "item-2", "disposition": "resolved"},
                ],
                reviewer="Anthropic Claude",
            ),
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude"),
        approved_followups="issue",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.issues == []
    assert not any(comment.startswith("Created approved-review future follow-up issues") for comment in runner.comments)


def test_pr_loop_semantically_deduplicates_followup_issues_and_keeps_provenance(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            structured_pr_review(
                state="approved",
                summary="Codex approves.",
                reviewer="OpenAI Codex",
            )
        ],
        claude_outputs=[
            structured_pr_review(
                state="approved",
                summary="Claude approves.",
                future_followups=[
                    "Clarify repair-pass ownership across the flowchart and sequence diagram."
                ],
                reviewer="Anthropic Claude",
            )
        ],
        gemini_outputs=[
            structured_pr_review(
                state="approved",
                summary="Gemini approves.",
                future_followups=[
                    "Document repair pass ownership in the flowchart and sequence diagram so the handoff is clear."
                ],
                reviewer="Google Gemini",
            )
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude", "gemini"),
        approved_followups="issue",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.issues) == 1
    body = runner.issues[0]["body"]
    assert "Reviewers:\n- Claude\n- Gemini" in body
    assert "Original reviewer notes:" in body
    assert "- Claude: Clarify repair-pass ownership" in body
    assert "- Gemini: Document repair pass ownership" in body
    assert "Reconciliation: 1 filed, 1 deduplicated, 0 skipped by cap." in runner.comments[-1]


def test_pr_loop_suppresses_followup_issue_summary_when_no_urls_returned(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex approves.\n\n### Future follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        issue_urls=[None],
    )
    config = make_config(tmp_path, approved_followups="issue")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.comments) == 1
    assert len(runner.issues) == 1


def test_pr_loop_creates_no_issues_without_approved_followups(tmp_path):
    runner = FakeRunner(
        codex_outputs=["Codex approves.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    config = make_config(tmp_path, approved_followups="issue")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.comments) == 1
    assert runner.issues == []


def test_pr_loop_logs_created_followup_issue_url(tmp_path, capsys):
    runner = FakeRunner(
        codex_outputs=[
            "Codex approves.\n\n### Future follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
    )
    config = make_config(tmp_path, approved_followups="issue", quiet=False)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    captured = capsys.readouterr()
    assert "Created GitHub issue: https://github.com/OWNER/REPO/issues/99" in captured.err


@pytest.mark.parametrize("mode", ["summarize", "issue"])
def test_pr_loop_treats_same_pr_followups_as_blocking_without_fix_mode(tmp_path, mode):
    runner = FakeRunner(
        codex_outputs=[
            "Codex found cleanup.\n\n"
            "### Same-PR follow-ups\n"
            "- Rename the helper before merge.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
        ],
    )
    config = make_config(tmp_path, approved_followups=mode, max_rounds=1)

    with pytest.raises(AgentLoopError, match="still reported blocking"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert len(runner.comments) == 1
    assert not runner.issues
    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


@pytest.mark.parametrize("mode", ["summarize", "issue"])
def test_pr_loop_treats_same_pr_prose_followups_as_blocking_without_fix_mode(tmp_path, mode):
    runner = FakeRunner(
        codex_outputs=[
            "Codex found cleanup.\n\n"
            "### Same-PR follow-ups\n"
            "Rename the helper before merge.\n"
            "Keep the behavior unchanged.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
        ],
    )
    config = make_config(tmp_path, approved_followups=mode, max_rounds=1)

    with pytest.raises(AgentLoopError, match="still reported blocking"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert len(runner.comments) == 1
    assert not runner.issues
    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


def test_pr_loop_caps_approved_followup_issues(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex approves.\n\n### Non-blocking follow-ups\n"
            "- Follow up one.\n"
            "- Follow up two.\n"
            "- Follow up three.\n"
            "- Follow up four.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
    )
    config = make_config(tmp_path, approved_followups="issue")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert [issue["title"] for issue in runner.issues] == [
        "Follow up future review note: Follow up one.",
        "Follow up future review note: Follow up two.",
        "Follow up future review note: Follow up three.",
    ]
    assert len(runner.comments) == 2
    issue_summary = runner.comments[-1]
    assert "- https://github.com/OWNER/REPO/issues/99" in issue_summary
    assert "Skipped 1 additional item(s) to avoid issue noise" in issue_summary
    assert issue_summary.endswith("-- coding-review-agent-loop")


def test_pr_loop_fix_and_summarize_sends_same_pr_followups_to_coder_then_rereviews(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "### Same-PR follow-ups\n"
            "- Rename the helper before merge.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Codex approves final pass.\n\n"
            "### Future follow-ups\n"
            "- Add broader integration coverage later.\n"
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=["Renamed helper.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(tmp_path, approved_followups="fix-and-summarize")
    issue_context = IssueContext(
        number=56,
        repo="OWNER/REPO",
        title="Support issue comments",
        body="Original request.",
        url="https://github.com/OWNER/REPO/issues/56",
        comments=(
            IssueComment(
                author="commenter",
                created_at="2026-05-17T10:00:00Z",
                body="Clarifying issue comment.",
            ),
        ),
    )

    assert run_pr_loop(runner, pr_number=77, config=config, issue_context=issue_context) == 0

    agent_commands = [cmd[:2] for cmd, _cwd in runner.commands if cmd[:1] in (["claude"], ["codex"])]
    assert agent_commands == [["codex", "exec"], ["claude", "--print"], ["codex", "exec"]]
    assert len(runner.comments) == 4
    followup_prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["claude"])
    assert "requested same-PR follow-ups" in followup_prompt
    assert "remains blocked pending another review round" in followup_prompt
    assert "Rename the helper before merge." in followup_prompt
    assert "[item-1]" in followup_prompt
    assert "Issue context from GitHub" in followup_prompt
    assert "Title:\nSupport issue comments" in followup_prompt
    assert "Clarifying issue comment." in followup_prompt
    assert "small, localized cleanup for the\ncurrent PR" in followup_prompt
    assert "Keep the change narrowly scoped to the listed items" in followup_prompt
    assert "Do not take on\nlarger redesigns or unrelated future work" in followup_prompt
    assert "Add broader integration coverage later." in runner.comments[-1]


def test_pr_loop_fix_and_issue_uses_final_round_future_followups_after_same_pr_cleanup(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "### Same-PR follow-ups\n"
            "- Tighten the validation message.\n\n"
            "### Future follow-ups\n"
            "- Stale future item from the blocking round.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Codex approves final pass.\n\n"
            "### Future follow-ups\n"
            "- Add a separate migration dry-run command.\n"
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=["Tightened message.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(tmp_path, approved_followups="fix-and-issue")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.issues) == 1
    assert runner.issues[0]["title"] == "Follow up future review note: Add a separate migration dry-run command."
    assert "Stale future item from the blocking round." not in runner.issues[0]["body"]
    commands = [cmd[:3] for cmd, _cwd in runner.commands]
    assert commands.count(["gh", "issue", "create"]) == 1


def test_pr_loop_fix_and_issue_drops_blocking_round_future_followups(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "### Same-PR follow-ups\n"
            "- Tighten the validation message.\n\n"
            "### Future follow-ups\n"
            "- Stale future item from the blocking round.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Codex approves final pass.\n\n"
            "### Future follow-ups\n"
            "- Add a separate migration dry-run command.\n"
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=["Tightened message.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(tmp_path, approved_followups="fix-and-issue")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.issues) == 1
    assert "Stale future item from the blocking round." not in runner.issues[0]["body"]


def test_pr_loop_fix_and_issue_uses_only_final_round_future_followups(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex found cleanup.\n\n"
            "### Same-PR follow-ups\n"
            "- Tighten the validation message.\n\n"
            "### Future follow-ups\n"
            "- Stale item fixed by the same-PR pass.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Codex approves final pass.\n\n"
            "### Future follow-ups\n"
            "- Add a separate migration dry-run command.\n"
            + prior_item_dispositions(
                "[item-1] resolved",
                "[item-2] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=["Tightened message.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(tmp_path, approved_followups="fix-and-issue")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.issues) == 1
    assert runner.issues[0]["title"] == "Follow up future review note: Add a separate migration dry-run command."
    assert "Stale item fixed by the same-PR pass." not in runner.issues[0]["body"]
    assert "- https://github.com/OWNER/REPO/issues/99" in runner.comments[-1]
    commands = [cmd[:3] for cmd, _cwd in runner.commands]
    assert commands.count(["gh", "issue", "create"]) == 1


def test_pr_loop_fix_and_summarize_uses_only_final_round_future_followups(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex found cleanup.\n\n"
            "### Same-PR follow-ups\n"
            "- Add a small assertion before merge.\n\n"
            "### Future follow-ups\n"
            "- Add Codex's larger follow-up later.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Codex approves final pass.\n\n"
            "### Future follow-ups\n"
            "- Add Codex's final follow-up later.\n"
            + prior_item_dispositions(
                "[item-1] resolved",
                "[item-2] resolved",
                "[item-3] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "Claude approves.\n\n"
            "### Future follow-ups\n"
            "- Add Claude's larger follow-up later.\n"
            "<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
            "Claude approves final pass.\n\n"
            "### Future follow-ups\n"
            "- Add Claude's final follow-up later.\n"
            + prior_item_dispositions(
                "[item-1] resolved",
                "[item-2] resolved",
                "[item-3] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
        gemini_outputs=["Added assertion.\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini"],
    )
    config = make_config(
        tmp_path,
        coder="gemini",
        reviewer=("codex", "claude"),
        approved_followups="fix-and-summarize",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    agent_commands = [cmd[:2] for cmd, _cwd in runner.commands if cmd[:1] in (["claude"], ["codex"], ["gemini"])]
    assert agent_commands == [
        ["codex", "exec"],
        ["claude", "--print"],
        ["gemini", "--prompt"],
        ["codex", "exec"],
        ["claude", "--print"],
    ]
    summary = runner.comments[-1]
    assert "- Add Codex's final follow-up later. (Codex)" in summary
    assert "- Add Claude's final follow-up later. (Claude)" in summary
    assert "Add Codex's larger follow-up later." not in summary
    assert "Add Claude's larger follow-up later." not in summary


def test_pr_loop_fix_and_issue_extracts_final_round_bullet_and_prose_future_followups(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex found cleanup.\n\n"
            "### Same-PR follow-ups\n"
            "- Tighten the validation message.\n\n"
            "### Future follow-ups\n"
            "- Stale Codex item fixed by the same-PR pass.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Codex approves final pass.\n\n"
            "### Future follow-ups\n"
            "- Refine token estimation for large review prompts.\n"
            + prior_item_dispositions(
                "[item-1] resolved",
                "[item-2] resolved",
                "[item-3] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "Claude approves with cleanup.\n\n"
            "### Future follow-ups\n"
            "- Stale Claude item fixed by the same-PR pass.\n"
            "<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
            "Claude approves final pass.\n\n"
            "### Future follow-ups\n"
            "The `_parse_gemini_output` helper is dead production code and could be removed\n"
            "in a future cleanup.\n\n"
            "### Same-PR follow-ups\n"
            "No same-PR follow-ups.\n"
            + prior_item_dispositions(
                "[item-1] resolved",
                "[item-2] resolved",
                "[item-3] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
        gemini_outputs=["Tightened message.\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini"],
        issue_urls=[
            "https://github.com/OWNER/REPO/issues/99",
            "https://github.com/OWNER/REPO/issues/100",
        ],
    )
    config = make_config(
        tmp_path,
        coder="gemini",
        reviewer=("codex", "claude"),
        approved_followups="fix-and-issue",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.issues[0]["title"] == (
        "Follow up future review note: Refine token estimation for large review prompts."
    )
    assert runner.issues[1]["title"].startswith(
        "Follow up future review note: The `_parse_gemini_output` helper is dead production code"
    )
    assert "could be removed in a future cleanup." in runner.issues[1]["body"]
    assert "Stale Codex item fixed by the same-PR pass." not in runner.issues[0]["body"]
    assert "Stale Claude item fixed by the same-PR pass." not in runner.issues[1]["body"]
    issue_summary = runner.comments[-1]
    assert "- https://github.com/OWNER/REPO/issues/99" in issue_summary
    assert "- https://github.com/OWNER/REPO/issues/100" in issue_summary
    assert "Stale Codex item fixed by the same-PR pass." not in issue_summary


def test_pr_loop_reruns_all_reviewers_when_any_reviewer_blocks(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Needs a regression test.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "Addressed review.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "Claude approves."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Codex approves first pass.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
            "Codex approves second pass."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, coder="claude", reviewer=("claude", "codex"))

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.comments) == 5
    followup_prompt = next(
        cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["claude"] and "Address the review below" in cmd[-1]
    )
    assert "Needs a regression test." in followup_prompt
    assert "Codex approves first pass." not in followup_prompt
    commands = [cmd for cmd, _cwd in runner.commands]
    metadata_fetches = [
        cmd
        for cmd in commands
        if cmd[:3] == ["gh", "pr", "view"]
        and "--json" in cmd
        and cmd[cmd.index("--json") + 1]
        == "number,title,headRefName,baseRefName,headRefOid,url,body,comments,reviews"
    ]
    assert len(metadata_fetches) == 2


def test_pr_loop_rejects_cross_reviewer_approval_without_prior_item_disposition(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Needs a regression test.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "Claude resolves it."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Codex approves first pass.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
            "Codex approves second pass.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        gemini_outputs=["Implemented fix.\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini"],
    )
    config = make_config(tmp_path, coder="gemini", reviewer=("claude", "codex"), max_rounds=2)

    with pytest.raises(AgentLoopError, match="did not evaluate all prior unresolved items: item-1"):
        run_pr_loop(runner, pr_number=77, config=config)

    second_codex_prompt = [
        cmd[-1]
        for cmd, _cwd in runner.commands
        if cmd[:2] == ["codex", "exec"] and "round 2" in cmd[-1]
    ][0]
    assert "Prior unresolved review items from earlier rounds" in second_codex_prompt
    assert "[item-1] blocking from Claude in round 1" in second_codex_prompt


def test_pr_loop_can_downgrade_prior_blocker_to_future_followup_only_in_approved_review(tmp_path):
    runner = FakeRunner(
        claude_outputs=["Addressed review.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
        codex_outputs=[
            "Missing docs cleanup.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM now."
            + prior_item_dispositions("[item-1] future follow-up: cleanup can wait")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, approved_followups="summarize", max_rounds=2)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    summary = runner.comments[-1]
    assert summary.startswith("Approved-review future follow-ups for PR #77:")
    assert "Missing docs cleanup." in summary


def test_pr_loop_persists_downgraded_future_followup_across_later_blocking_rounds(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Still blocked.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "LGTM."
            + prior_item_dispositions("[item-1] future follow-up: cleanup can wait", "[item-2] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Missing docs cleanup.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Implemented fix for Claude.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Looks good."
            + prior_item_dispositions("[item-1] future follow-up: cleanup can wait", "[item-2] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude"),
        coder="codex",
        approved_followups="summarize",
        max_rounds=2,
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    summary = runner.comments[-1]
    assert summary.startswith("Approved-review future follow-ups for PR #77:")
    assert "Missing docs cleanup." in summary


def test_pr_loop_finalized_future_followup_summary_preserves_disposition_notes(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Still blocked.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "Claude approves final pass."
            + prior_item_dispositions(
                "[item-1] future follow-up: cleanup can wait until after rollout",
                "[item-2] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Missing docs cleanup.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Implemented blocker.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Codex approves final pass."
            + prior_item_dispositions(
                "[item-1] future follow-up: cleanup can wait until after rollout",
                "[item-2] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude"),
        coder="codex",
        approved_followups="summarize",
        max_rounds=2,
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    summary = runner.comments[-1]
    assert "Missing docs cleanup." in summary
    assert "Update from Codex: cleanup can wait until after rollout" in summary




def test_pr_loop_compact_review_mode_uses_fresh_sessions_and_compact_prior_ledger(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex approves with future work.\n\n"
            "### Future follow-ups\n"
            "- Document cache cleanup behavior.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
            "Codex approves final pass."
            + prior_item_dispositions(
                "[item-1] future follow-up: still future work",
                "[item-2] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "Claude still blocks.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "Claude approves final pass."
            + prior_item_dispositions(
                "[item-1] future follow-up: still future work",
                "[item-2] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
        gemini_outputs=[
            structured_coder_followup(
                state="blocking",
                summary="Implemented blocker and ran focused tests.",
                addressed_items=["item-1", "item-2"],
                remaining_items=[],
                tests_run=["python -m pytest tests/test_agent_loop.py -k compact_pr"],
                reviewer="Google Gemini",
            )
        ],
        pr_payload={"body": "PR body used by compact review mode."},
    )
    config = make_config(
        tmp_path,
        coder="gemini",
        reviewer=("codex", "claude"),
        approved_followups="summarize",
        max_rounds=2,
        pr_review_context_mode="compact",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    codex_prompts = [cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"]]
    assert len(codex_prompts) == 2
    second_codex_prompt = codex_prompts[1]
    assert COMPACT_PR_REVIEW_VOLATILE_TAIL_MARKER in second_codex_prompt
    assert "PR body used by compact review mode." in second_codex_prompt
    assert "Implemented blocker and ran focused tests." in second_codex_prompt
    assert "python -m pytest tests/test_agent_loop.py -k compact_pr" in second_codex_prompt
    assert "Document cache cleanup behavior." not in second_codex_prompt
    assert "[item-1] future" not in second_codex_prompt
    assert "Claude still blocks." in second_codex_prompt
    assert not any("--resume" in cmd for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])

    assert runner.comments[-1].startswith("Approved-review future follow-ups for PR #77:")
    assert "Document cache cleanup behavior." in runner.comments[-1]


def test_pr_loop_carries_prior_item_notes_without_creating_duplicate_blocker_items(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Needs regression coverage.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Still blocked."
            + prior_item_dispositions("[item-1] still blocking: include API error path too")
            + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Looks good."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "Added coverage.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "Expanded coverage.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path, max_rounds=3)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    second_coder_prompt = [cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]][1]
    assert "Latest reviewer updates:" in second_coder_prompt
    assert "Codex: include API error path too" in second_coder_prompt
    assert "[item-2]" not in second_coder_prompt


def test_pr_loop_posts_human_readable_item_labels_in_new_and_prior_sections(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Implemented the requested PR body change.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "### Same-PR follow-ups\n"
            "- Require source issue reference in PR body.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Looks good."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", approved_followups="fix-and-summarize", max_rounds=2)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments[0] == (
        "**Review verdict:** Blocking\n\n"
        "### Same-PR follow-ups\n"
        "- Require source issue reference in PR body.\n"
        "<!-- AGENT_STATE: blocking -->\n"
        "-- OpenAI Codex"
    )
    assert runner.comments[2] == (
        "**Review verdict:** Approved\n\n"
        "Looks good.\n\n"
        "### Prior unresolved item dispositions\n"
        "- [item-1] Same-PR follow-up from OpenAI Codex, round 1: Require source issue reference in PR body. -> resolved\n"
        "<!-- AGENT_STATE: approved -->\n"
        "-- OpenAI Codex"
    )


def test_pr_loop_tracks_only_summary_when_blocking_items_phrase_the_issue_differently(tmp_path):
    runner = FakeRunner(
        claude_outputs=["Implemented fixes.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
        codex_outputs=[
            "Needs one more regression test before merge."
            + blocking_issues("Add the mixed-history resume case to `tests/test_agent_loop.py`.")
            + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Looks good."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", max_rounds=2)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    second_coder_prompt = [cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]][0]
    assert "Needs one more regression test before merge." in second_coder_prompt
    assert "Add the mixed-history resume case" not in second_coder_prompt
    assert runner.comments[0] == (
        "**Review verdict:** Blocking\n\n"
        "Needs one more regression test before merge.\n\n"
        "### Blocking issues\n"
        "- Add the mixed-history resume case to `tests/test_agent_loop.py`.\n"
        "<!-- AGENT_STATE: blocking -->\n"
        "-- OpenAI Codex"
    )


def test_resume_pr_round_reparses_orchestrator_rendered_blocking_issues_comment():
    carried_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Need one more regression test before merge.",
        status="blocking",
        source_status="blocking",
    )
    rendered_review = _render_public_pr_review_comment(
        parse_review(
            "Need one more regression test before merge."
            + blocking_issues("Exercise the structured-resume path.")
            + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            reviewer="OpenAI Codex",
        ),
        reviewer="Codex",
        human_requirements_resolved_flag=False,
        prior_items=(),
        dispositions=(),
    )
    review_comment = _attach_round_metadata(
        rendered_review,
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Codex",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
            dispositions=(),
            new_items=(),
            state="blocking",
        ),
    )
    coder_comment = _attach_round_metadata(
        "Addressed the review.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
        ),
    )

    resumed = _resume_pr_round(
        [
            IssueComment(author="bot", created_at="2026-05-25T00:00:00Z", body=coder_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:01:00Z", body=review_comment),
        ],
        head_sha="abc123",
        configured_reviewers=("codex",),
    )

    assert resumed is not None
    resumed_review = parse_review(resumed.completed_reviews[0].body, reviewer="Codex")
    assert [item.text for item in resumed_review.blocking_items] == [
        "Exercise the structured-resume path."
    ]
    assert resumed_review.summary == "Need one more regression test before merge."


def test_resume_pr_round_prefers_structured_coder_followup_metadata():
    carried_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Need one more regression test before merge.",
        status="blocking",
        source_status="blocking",
    )
    raw_structured_followup = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Added the requested regression test.",
                "addressed_items": ["item-1"],
                "remaining_items": [],
                "human_requirements": {
                    "addressed_ids": ["Requirement 1"],
                    "checked_discussion_directly": False,
                },
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
    )
    parsed = validate_structured_coder_followup(raw_structured_followup)
    assert parsed is not None
    public_comment = _render_public_coder_followup_comment(parsed, agent="Claude")
    coder_comment = _attach_round_metadata(
        public_comment,
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
            raw_structured_coder_response=raw_structured_followup,
        ),
    )

    resumed = _resume_pr_round(
        [IssueComment(author="bot", created_at="2026-05-25T00:00:00Z", body=coder_comment)],
        head_sha="abc123",
        configured_reviewers=("codex",),
    )

    assert resumed is not None
    assert resumed.coder_output == raw_structured_followup
    resumed_followup = validate_structured_coder_followup(resumed.coder_output)
    assert resumed_followup is not None
    assert resumed_followup.human_requirements.addressed_ids == ("Requirement 1",)
    assert '"kind": "coder_followup"' not in _strip_round_metadata(coder_comment)


def test_resume_pr_round_marks_empty_ledger_incomplete_after_same_subject_prior_new_items():
    prior_new_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Prior same-head item.",
        status="blocking",
    )
    prior_review_comment = _attach_round_metadata(
        "Prior review.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Codex",
            round_number=1,
            subject="abc123",
            prior_items=(),
            new_items=(prior_new_item,),
            state="blocking",
        ),
    )
    current_coder_comment = _attach_round_metadata(
        "Current coder output.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=2,
            subject="abc123",
            prior_items=(),
        ),
    )

    resumed = _resume_pr_round(
        [
            IssueComment(author="bot", created_at="2026-05-25T00:00:00Z", body=prior_review_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:01:00Z", body=current_coder_comment),
        ],
        head_sha="abc123",
        configured_reviewers=("codex",),
    )

    assert resumed is not None
    assert resumed.ledger_may_be_incomplete is True


def test_resume_pr_round_does_not_mark_ledger_incomplete_for_cross_subject_prior_new_items():
    prior_new_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Prior other-head item.",
        status="blocking",
    )
    prior_review_comment = _attach_round_metadata(
        "Prior review.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Codex",
            round_number=1,
            subject="old-sha",
            prior_items=(),
            new_items=(prior_new_item,),
            state="blocking",
        ),
    )
    current_coder_comment = _attach_round_metadata(
        "Current coder output.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=1,
            subject="new-sha",
            prior_items=(),
        ),
    )

    resumed = _resume_pr_round(
        [
            IssueComment(author="bot", created_at="2026-05-25T00:00:00Z", body=prior_review_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:01:00Z", body=current_coder_comment),
        ],
        head_sha="new-sha",
        configured_reviewers=("codex",),
    )

    assert resumed is not None
    assert resumed.ledger_may_be_incomplete is False


def test_resume_pr_round_recovers_unrecorded_head_advance_reviewer_new_item():
    active_item = UnresolvedReviewItem(
        item_id="item-2",
        reviewer="Google Gemini",
        source_round=1,
        text="Fix the regression before merge.",
        status="blocking",
    )
    coder_comment = _attach_round_metadata(
        "Initial PR handoff.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=1,
            subject="old-sha",
            prior_items=(),
        ),
    )
    review_comment = _attach_round_metadata(
        "Blocked.\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Gemini",
            round_number=1,
            subject="old-sha",
            prior_items=(),
            new_items=(active_item,),
            state="blocking",
        ),
    )

    resumed = _resume_pr_round(
        [
            IssueComment(author="bot", created_at="2026-05-25T00:00:00Z", body=coder_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:01:00Z", body=review_comment),
        ],
        head_sha="new-sha",
        configured_reviewers=("gemini",),
    )

    assert resumed is not None
    assert resumed.unrecorded_head_advance is True
    assert resumed.ledger_may_be_incomplete is True
    assert resumed.round_number == 1
    assert resumed.completed_reviews == ()
    assert [item.item_id for item in resumed.prior_items] == ["item-2"]
    assert resumed.next_unresolved_item_number == 3


def test_resume_pr_round_recovers_coder_only_unrecorded_head_advance():
    carried_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Still needs a targeted test.",
        status="same-pr",
    )
    future_item = UnresolvedReviewItem(
        item_id="item-2",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Document this later.",
        status="future",
    )
    coder_comment = _attach_round_metadata(
        "Addressed prior feedback.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=2,
            subject="old-sha",
            prior_items=(carried_item, future_item),
            compact_prior_summaries=("Older summary.",),
        ),
    )

    resumed = _resume_pr_round(
        [IssueComment(author="bot", created_at="2026-05-25T00:00:00Z", body=coder_comment)],
        head_sha="new-sha",
        configured_reviewers=("codex",),
    )

    assert resumed is not None
    assert resumed.unrecorded_head_advance is True
    assert resumed.round_number == 2
    assert [item.item_id for item in resumed.prior_items] == ["item-1"]
    assert resumed.compact_prior_summaries == ("Older summary.",)


def test_resume_pr_round_recovers_reviewer_only_with_aggregated_dispositions():
    prior_blocking = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Fix the flaky test.",
        status="blocking",
    )
    prior_same_pr = UnresolvedReviewItem(
        item_id="item-2",
        reviewer="Google Gemini",
        source_round=1,
        text="Tighten the docs.",
        status="same-pr",
    )
    future_new_item = UnresolvedReviewItem(
        item_id="item-3",
        reviewer="OpenAI Codex",
        source_round=2,
        text="Follow up in another PR.",
        status="future",
    )
    active_new_item = UnresolvedReviewItem(
        item_id="item-4",
        reviewer="Google Gemini",
        source_round=2,
        text="Add one same-PR assertion.",
        status="same-pr",
    )
    codex_resolution = ReviewItemDisposition(
        item_id="item-1",
        reviewer="OpenAI Codex",
        disposition="resolved",
        note=None,
    )
    gemini_same_pr = ReviewItemDisposition(
        item_id="item-2",
        reviewer="Google Gemini",
        disposition="same-pr",
        note="Still needed before merge.",
    )
    codex_comment = _attach_round_metadata(
        "Codex review.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Codex",
            round_number=2,
            subject="old-sha",
            prior_items=(prior_blocking, prior_same_pr),
            dispositions=(codex_resolution,),
            new_items=(future_new_item,),
            state="approved",
        ),
    )
    gemini_comment = _attach_round_metadata(
        "Gemini review.\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Gemini",
            round_number=2,
            subject="old-sha",
            prior_items=(prior_blocking, prior_same_pr),
            dispositions=(gemini_same_pr,),
            new_items=(active_new_item,),
            state="blocking",
        ),
    )

    resumed = _resume_pr_round(
        [
            IssueComment(author="bot", created_at="2026-05-25T00:00:00Z", body=codex_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:01:00Z", body=gemini_comment),
        ],
        head_sha="new-sha",
        configured_reviewers=("codex", "gemini"),
    )

    assert resumed is not None
    assert resumed.unrecorded_head_advance is True
    assert [item.item_id for item in resumed.prior_items] == ["item-2", "item-4"]
    assert resumed.prior_items[0].status == "same-pr"
    assert "Still needed before merge." in resumed.prior_items[0].text


def test_resume_pr_round_ignores_unrecorded_head_advance_with_no_active_items():
    future_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Future cleanup.",
        status="future",
    )
    review_comment = _attach_round_metadata(
        "Approved with future follow-up.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Codex",
            round_number=1,
            subject="old-sha",
            prior_items=(),
            new_items=(future_item,),
            state="approved",
        ),
    )

    assert (
        _resume_pr_round(
            [IssueComment(author="bot", created_at="2026-05-25T00:00:00Z", body=review_comment)],
            head_sha="new-sha",
            configured_reviewers=("codex",),
        )
        is None
    )


def test_resume_pr_round_fails_early_for_incoherent_unrecorded_head_advance():
    bad_comment = _attach_round_metadata(
        "Bad metadata.\n<!-- AGENT_STATE: blocking -->\n-- Bot",
        PostedRoundMetadata(
            flow="pr",
            role="observer",
            agent="Bot",
            round_number=1,
            subject="old-sha",
        ),
    )

    with pytest.raises(
        AgentLoopError,
        match=(
            "PR head advanced without a recorded coder follow-up.*"
            "Current head: new-sha.*Latest recorded metadata subject: old-sha"
        ),
    ):
        _resume_pr_round(
            [IssueComment(author="bot", created_at="2026-05-25T00:00:00Z", body=bad_comment)],
            head_sha="new-sha",
            configured_reviewers=("codex",),
        )


def test_resume_plan_round_marks_empty_ledger_incomplete_after_same_subject_prior_new_items():
    plan = "Plan text."
    subject = _plan_subject(plan)
    prior_new_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Prior same-plan item.",
        status="blocking",
    )
    prior_review_comment = _attach_round_metadata(
        "Prior plan review.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="plan",
            role="reviewer",
            agent="Codex",
            round_number=1,
            subject=subject,
            prior_items=(),
            new_items=(prior_new_item,),
            state="blocking",
        ),
    )
    current_coder_comment = _attach_round_metadata(
        plan + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=subject,
            prior_items=(),
            canonical_plan=plan,
        ),
    )

    resumed = _resume_plan_round(
        [
            IssueComment(author="bot", created_at="2026-05-25T00:00:00Z", body=prior_review_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:01:00Z", body=current_coder_comment),
        ],
        configured_reviewers=("codex",),
    )

    assert resumed is not None
    assert resumed[1].ledger_may_be_incomplete is True


def test_resume_pr_round_prefers_latest_metadata_ledger_for_same_head_replay():
    stale_item = UnresolvedReviewItem(
        item_id="item-3",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Stale replay item.",
        status="blocking",
        source_status="blocking",
    )
    active_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Active replay item.",
        status="blocking",
        source_status="blocking",
    )
    stale_coder_comment = _attach_round_metadata(
        "Stale replay.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=2,
            subject="abc123",
            prior_items=(stale_item,),
        ),
    )
    stale_reviewer_comment = _attach_round_metadata(
        "Still blocked."
        + prior_item_dispositions("[item-3] still blocking: stale replay")
        + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Codex",
            round_number=2,
            subject="abc123",
            prior_items=(stale_item,),
            dispositions=(
                parse_unresolved_item_dispositions(
                    prior_item_dispositions("[item-3] still blocking: stale replay"),
                    reviewer="OpenAI Codex",
                )[0],
            ),
            state="blocking",
        ),
    )
    active_coder_comment = _attach_round_metadata(
        "Current replay.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=2,
            subject="abc123",
            prior_items=(active_item,),
        ),
    )
    active_reviewer_comment = _attach_round_metadata(
        "Looks good."
        + prior_item_dispositions("[item-1] resolved")
        + "\n<!-- AGENT_STATE: approved -->\n-- Google Gemini",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Gemini",
            round_number=2,
            subject="abc123",
            prior_items=(active_item,),
            dispositions=(
                parse_unresolved_item_dispositions(
                    prior_item_dispositions("[item-1] resolved"),
                    reviewer="Google Gemini",
                )[0],
            ),
            state="approved",
        ),
    )
    previous_head_comment = _attach_round_metadata(
        "Older head.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=4,
            subject="old-head",
            prior_items=(
                UnresolvedReviewItem(
                    item_id="item-9",
                    reviewer="OpenAI Codex",
                    source_round=3,
                    text="Older head item.",
                    status="blocking",
                ),
            ),
        ),
    )

    resumed = _resume_pr_round(
        [
            IssueComment(author="bot", created_at="2026-05-25T00:00:00Z", body=previous_head_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:01:00Z", body=stale_coder_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:02:00Z", body=stale_reviewer_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:03:00Z", body=active_coder_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:04:00Z", body=active_reviewer_comment),
        ],
        head_sha="abc123",
        configured_reviewers=("codex", "gemini"),
    )

    assert resumed is not None
    assert [item.item_id for item in resumed.prior_items] == ["item-1"]
    assert resumed.next_unresolved_item_number == 4
    assert [record.metadata.agent for record in resumed.completed_reviews] == ["Gemini"]


def test_pr_loop_resume_hybrid_history_prefers_metadata_ledger_over_legacy_markdown(tmp_path):
    carried_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Add a regression test before merge.",
        status="blocking",
        source_status="blocking",
    )
    legacy_comment = (
        "Legacy raw markdown review.\n\n"
        "### Blocking issues\n"
        "- Keep the legacy fallback path.\n"
        "<!-- AGENT_STATE: blocking -->\n"
        "-- OpenAI Codex"
    )
    coder_comment = _attach_round_metadata(
        "Updated the PR.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
        ),
    )
    codex_comment = _attach_round_metadata(
        "Looks good."
        + prior_item_dispositions("[item-1] resolved")
        + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Codex",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
            dispositions=(
                parse_unresolved_item_dispositions(
                    prior_item_dispositions("[item-1] resolved"),
                    reviewer="OpenAI Codex",
                )[0],
            ),
            state="approved",
        ),
    )
    runner = FakeRunner(
        gemini_outputs=[
            "Ship it."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
        ],
        pr_payload={
            "comments": [
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:00:00Z", "body": legacy_comment},
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:05:00Z", "body": coder_comment},
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:06:00Z", "body": codex_comment},
            ],
        },
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"))

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    gemini_prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["gemini"])
    assert "[item-1]" in gemini_prompt
    assert "Add a regression test before merge." in gemini_prompt
    assert "Keep the legacy fallback path." not in gemini_prompt


def test_pr_loop_routes_unrecorded_head_advance_through_coder_before_reviewers(tmp_path):
    old_item = UnresolvedReviewItem(
        item_id="item-2",
        reviewer="Google Gemini",
        source_round=1,
        text="Preserve the metadata-backed unresolved item on rerun.",
        status="blocking",
    )
    old_coder_comment = _attach_round_metadata(
        "Opened the PR.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=1,
            subject="old-sha",
            prior_items=(),
        ),
    )
    old_review_comment = _attach_round_metadata(
        "Blocked.\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Gemini",
            round_number=1,
            subject="old-sha",
            prior_items=(),
            new_items=(old_item,),
            state="blocking",
        ),
    )
    runner = FakeRunner(
        claude_outputs=[
            structured_coder_followup(
                summary="Addressed the recovered prior item.",
                addressed_items=["item-2"],
                tests_run=["python -m pytest tests/test_agent_loop.py -k unrecorded_head"],
            )
        ],
        codex_outputs=[
            structured_pr_review(
                state="approved",
                summary="Recovered item is resolved.",
                prior_item_dispositions=[
                    {"item_id": "item-2", "disposition": "resolved"},
                ],
            )
        ],
        pr_payload={
            "headRefOid": "new-sha",
            "comments": [
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:00:00Z", "body": old_coder_comment},
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:01:00Z", "body": old_review_comment},
            ],
        },
    )
    config = make_config(tmp_path, reviewer="codex")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    first_coder = command_index(runner.commands, ["claude"])
    first_reviewer = command_index(runner.commands, ["codex", "exec"])
    assert first_coder < first_reviewer
    reviewer_prompt = runner.commands[first_reviewer][0][-1]
    assert "[item-2]" in reviewer_prompt
    assert "Preserve the metadata-backed unresolved item on rerun." in reviewer_prompt
    posted_coder_comment = next(
        comment["body"]
        for comment in runner.pr_payload["comments"]
        if "## Coder follow-up" in comment["body"]
    )
    match = re.search(r"<!--\s*AGENT_LOOP_META:\s*(?P<payload>[A-Za-z0-9+/=_-]+)\s*-->", posted_coder_comment)
    assert match is not None
    metadata = _decode_round_metadata(match.group("payload"))
    assert metadata.subject == "new-sha"
    assert metadata.round_number == 2
    assert [item.item_id for item in metadata.prior_items] == ["item-2"]


def test_pr_loop_unrecorded_head_advance_prevents_empty_ledger_unknown_item_abort(tmp_path):
    old_item = UnresolvedReviewItem(
        item_id="item-2",
        reviewer="Google Gemini",
        source_round=1,
        text="Carry this item instead of starting an empty ledger.",
        status="blocking",
    )
    old_review_comment = _attach_round_metadata(
        "Blocked.\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Gemini",
            round_number=1,
            subject="old-sha",
            prior_items=(),
            new_items=(old_item,),
            state="blocking",
        ),
    )
    runner = FakeRunner(
        claude_outputs=[
            structured_coder_followup(
                summary="Classified the recovered item.",
                addressed_items=["item-2"],
            )
        ],
        codex_outputs=[
            structured_pr_review(
                state="approved",
                summary="Old item is resolved.",
                prior_item_dispositions=[
                    {"item_id": "item-2", "disposition": "resolved"},
                ],
            )
        ],
        pr_payload={
            "headRefOid": "new-sha",
            "comments": [
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:01:00Z", "body": old_review_comment},
            ],
        },
    )
    config = make_config(tmp_path, reviewer="codex")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0
    assert runner.claude_outputs == []
    assert runner.codex_outputs == []
    assert not any("unknown item" in comment.lower() for comment in runner.comments)


def test_reconcile_human_requirements_ack_item_accepts_stored_structured_coder_followup():
    human_requirements = (
        HumanReviewRequirement(
            source_type="PR comment",
            author="reviewer",
            created_at="2026-05-18T10:00:00Z",
            url="https://github.com/OWNER/REPO/pull/77#issuecomment-1",
            body="Please use the absolute URL.",
        ),
    )
    structured_followup = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Implemented the requested URL fix.",
                "addressed_items": ["item-1"],
                "remaining_items": [],
                "human_requirements": {
                    "addressed_ids": ["Requirement 1"],
                    "checked_discussion_directly": False,
                },
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
    )

    reconciled = _reconcile_human_requirements_ack_item(
        (
            UnresolvedReviewItem(
                item_id=HUMAN_REQUIREMENTS_ACK_ITEM_ID,
                reviewer="Orchestrator",
                source_round=1,
                text="Ack missing.",
                status="blocking",
            ),
        ),
        coder_output=structured_followup,
        human_requirements=human_requirements,
        source_round=2,
    )

    assert reconciled == []


def test_pr_loop_does_not_expose_same_round_item_ids_to_later_reviewers(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "### Same-PR follow-ups\n"
            "- Require source issue reference in PR body.\n"
            "<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Still blocked.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(
        tmp_path,
        coder="gemini",
        reviewer=("claude", "codex"),
        approved_followups="fix-and-summarize",
        max_rounds=1,
    )

    with pytest.raises(AgentLoopError, match="still reported blocking issues after round 1"):
        run_pr_loop(runner, pr_number=77, config=config)

    second_reviewer_prompt = [
        cmd[-1]
        for cmd, _cwd in runner.commands
        if cmd[:2] == ["codex", "exec"] and "round 1" in cmd[-1]
    ][0]
    assert "Only items listed under `Prior unresolved review items from earlier rounds`" in second_reviewer_prompt
    assert "[item-1]" not in second_reviewer_prompt
    assert "### New tracked unresolved items" not in runner.comments[0]


def test_pr_loop_same_pr_items_remain_blocking_until_explicitly_resolved(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "### Same-PR follow-ups\n"
            "- Rename the helper before merge.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Codex still wants the rename."
            + prior_item_dispositions("[item-1] same-pr")
            + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
        claude_outputs=["Tried a partial fix.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(tmp_path, approved_followups="fix-and-summarize", max_rounds=2)

    with pytest.raises(AgentLoopError, match="still reported blocking issues after round 2"):
        run_pr_loop(runner, pr_number=77, config=config)


def test_pr_loop_resumes_with_only_missing_reviewer_for_current_head(tmp_path):
    carried_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="Codex",
        source_round=1,
        text="Add a regression test before merge.",
        status="blocking",
        source_status="blocking",
    )
    coder_comment = _attach_round_metadata(
        "Updated the PR with the requested fix.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
        ),
    )
    codex_comment = _attach_round_metadata(
        "Looks good."
        + prior_item_dispositions("[item-1] resolved")
        + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Codex",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
            dispositions=(
                parse_unresolved_item_dispositions(
                    prior_item_dispositions("[item-1] resolved"),
                    reviewer="OpenAI Codex",
                )[0],
            ),
            state="approved",
        ),
    )
    runner = FakeRunner(
        gemini_outputs=[
            "Ship it."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
        ],
        pr_payload={
            "comments": [
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T10:00:00Z", "body": coder_comment},
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T10:05:00Z", "body": codex_comment},
            ],
        },
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"))

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    agent_commands = [cmd[0] for cmd, _cwd in runner.commands if cmd[:1] in (["claude"], ["codex"], ["gemini"])]
    assert agent_commands == ["gemini"]
    gemini_prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["gemini"])
    assert "[item-1]" in gemini_prompt
    assert "Add a regression test before merge." in gemini_prompt


def test_pr_loop_resume_raises_agent_loop_error_for_missing_reconstructed_prior_item(tmp_path):
    carried_item = UnresolvedReviewItem(
        item_id="item-2",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Actual active carried item.",
        status="blocking",
        source_status="blocking",
    )
    coder_comment = _attach_round_metadata(
        "Updated the PR.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
        ),
    )
    invalid_disposition = ReviewItemDisposition(
        item_id="item-1",
        reviewer="OpenAI Codex",
        disposition="resolved",
    )
    codex_comment = _attach_round_metadata(
        "Looks good."
        + prior_item_dispositions("[item-1] resolved")
        + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Codex",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
            dispositions=(invalid_disposition,),
            state="approved",
        ),
    )
    runner = FakeRunner(
        pr_payload={
            "comments": [
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T10:00:00Z", "body": coder_comment},
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T10:05:00Z", "body": codex_comment},
            ],
        },
    )
    config = make_config(tmp_path, reviewer=("codex",))

    with pytest.raises(
        AgentLoopError,
        match=r"Resumed pr round 2 reconstructed prior items item-2, but Codex dispositioned unknown item `item-1`",
    ):
        run_pr_loop(runner, pr_number=77, config=config)


@pytest.mark.parametrize(
    "line",
    [
        "[item-1] same-pr: none",
        "[item-1] still blocking: none",
        "[item-1] future follow-up: none",
    ],
)
def test_pr_loop_rejects_contradictory_disposition_before_extra_coder_round(tmp_path, line):
    runner = FakeRunner(
        codex_outputs=[
            "Needs regression coverage.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Looks good overall."
            + prior_item_dispositions(line)
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=["Added coverage.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(tmp_path, approved_followups="fix-and-summarize", max_rounds=3)

    with pytest.raises(AgentLoopError, match="use `resolved` when nothing remains"):
        run_pr_loop(runner, pr_number=77, config=config)

    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(claude_calls) == 1


def test_pr_loop_does_not_run_claude_after_final_blocking_round(tmp_path):
    runner = FakeRunner(codex_outputs=["Still blocked.\n<!-- AGENT_STATE: blocking -->"])
    config = make_config(tmp_path, max_rounds=1)

    with pytest.raises(AgentLoopError, match="still reported blocking"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


def test_shared_workdir_requires_explicit_override(tmp_path):
    runner = FakeRunner()
    shared = tmp_path / "repo"
    shared.mkdir()
    config = make_config(tmp_path, claude_dir=shared, codex_dir=shared)

    with pytest.raises(AgentLoopError, match="same directory"):
        run_pr_loop(runner, pr_number=77, config=config)


def test_gemini_shared_workdir_requires_explicit_override(tmp_path):
    runner = FakeRunner()
    shared = tmp_path / "repo"
    shared.mkdir()
    config = make_config(
        tmp_path,
        reviewer=("codex", "gemini"),
        codex_dir=shared,
        gemini_dir=shared,
    )

    with pytest.raises(AgentLoopError, match="same directory"):
        run_pr_loop(runner, pr_number=77, config=config)


def test_missing_agent_workdirs_are_created(tmp_path):
    runner = FakeRunner(
        claude_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"],
    )
    claude_dir = tmp_path / "missing" / "claude"
    codex_dir = tmp_path / "missing" / "codex"
    config = make_config(
        tmp_path,
        claude_dir=claude_dir,
        codex_dir=codex_dir,
        coder="codex",
        reviewer="claude",
        create_dirs=False,
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0
    assert claude_dir.is_dir()
    assert codex_dir.is_dir()


def test_missing_gemini_workdir_is_created_when_configured(tmp_path):
    runner = FakeRunner(
        gemini_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"],
    )
    gemini_dir = tmp_path / "missing" / "gemini"
    config = make_config(
        tmp_path,
        reviewer="gemini",
        gemini_dir=gemini_dir,
        create_dirs=False,
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0
    assert gemini_dir.is_dir()


def test_non_codex_loop_uses_active_workdir_for_github_and_tests(tmp_path):
    runner = FakeRunner(
        gemini_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"],
    )
    codex_dir = tmp_path / "inactive" / "codex"
    config = make_config(
        tmp_path,
        claude_dir=tmp_path / "missing" / "claude",
        codex_dir=codex_dir,
        gemini_dir=tmp_path / "missing" / "gemini",
        coder="claude",
        reviewer="gemini",
        test_command=("pytest", "tests/test_agent_loop.py"),
        create_dirs=False,
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert not codex_dir.exists()
    github_or_test_cwds = [
        cwd
        for cmd, cwd in runner.commands
        if cmd[:1] == ["gh"] or cmd == ["pytest", "tests/test_agent_loop.py"]
    ]
    assert github_or_test_cwds
    bootstrap_pr_queries = [
        cwd
        for cmd, cwd in runner.commands
        if cmd[:3] == ["gh", "pr", "view"]
        and "number,title,headRefName,baseRefName,headRefOid,url,body,comments,reviews" in cmd
        and cwd != config.claude_dir
    ]
    assert bootstrap_pr_queries == [Path.cwd()]
    assert set(github_or_test_cwds) == {Path.cwd(), config.claude_dir}


def test_omitted_agent_dirs_default_to_repo_scoped_temp_checkouts(monkeypatch, tmp_path):
    parser = build_parser()
    cache_home = tmp_path / "cache"
    monkeypatch.setattr("coding_review_agent_loop.config.sys.platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    args = parser.parse_args([
        "task",
        "Fix the bug",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "codex",
        "--reviewer",
        "claude",
    ])

    config = config_from_args(args, FakeRunner())

    assert config.codex_dir == default_agent_workdir("OWNER/REPO", "codex").resolve()
    assert config.claude_dir == default_agent_workdir("OWNER/REPO", "claude").resolve()
    assert config.gemini_dir == default_agent_workdir("OWNER/REPO", "gemini").resolve()
    assert config.antigravity_dir == default_agent_workdir("OWNER/REPO", "antigravity").resolve()
    assert set(config.auto_agent_dirs) == {"claude", "codex", "gemini", "antigravity"}
    assert config.agent_memory_dir == (
        cache_home / "coding-review-agent-loop" / "repos" / "OWNER-REPO" / "memory"
    ).resolve()


@pytest.mark.parametrize(
    ("coder", "reviewer", "missing_command", "override_flag"),
    [
        ("claude", "codex", "missing-claude", "--claude-cmd"),
        ("claude", "gemini", "missing-gemini", "--gemini-cmd"),
    ],
)
def test_config_preflight_rejects_missing_agent_before_repo_detection(
    monkeypatch,
    coder,
    reviewer,
    missing_command,
    override_flag,
):
    parser = build_parser()
    args = parser.parse_args([
        "task",
        "Fix the bug",
        "--coder",
        coder,
        "--reviewer",
        reviewer,
        f"--{coder}-cmd",
        missing_command if override_flag == f"--{coder}-cmd" else coder,
        f"--{reviewer}-cmd",
        missing_command if override_flag == f"--{reviewer}-cmd" else reviewer,
    ])
    detection_calls = []
    monkeypatch.setattr(
        "coding_review_agent_loop.config.detect_repo",
        lambda *call_args: detection_calls.append(call_args),
    )
    monkeypatch.setattr(
        "coding_review_agent_loop.config.shutil.which",
        lambda command: None if command == missing_command else f"/bin/{command}",
    )

    with pytest.raises(
        AgentLoopError,
        match=rf"{missing_command} CLI not found on PATH.*{override_flag}",
    ):
        config_from_args(args, Runner())

    assert detection_calls == []


def test_config_preflight_checks_only_unique_configured_agents(monkeypatch, tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "task",
        "Fix the bug",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "codex",
        "--reviewer",
        "codex",
        "--codex-dir",
        str(tmp_path / "codex"),
    ])
    checked = []

    def fake_which(command):
        checked.append(command)
        return f"/bin/{command}"

    monkeypatch.setattr("coding_review_agent_loop.config.shutil.which", fake_which)

    config = config_from_args(args, Runner())

    assert config.coder == "codex"
    assert checked == ["codex", "agy"]


def test_config_preflight_accepts_custom_absolute_command(tmp_path):
    command = tmp_path / "custom-codex"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o755)
    parser = build_parser()
    args = parser.parse_args([
        "task",
        "Fix the bug",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "codex",
        "--reviewer",
        "codex",
        "--codex-cmd",
        str(command),
    ])

    config = config_from_args(args, Runner())

    assert config.codex_cmd == str(command)


def test_config_preflight_skips_dry_run_command_preview(monkeypatch):
    parser = build_parser()
    args = parser.parse_args([
        "task",
        "Fix the bug",
        "--repo",
        "OWNER/REPO",
        "--dry-run",
        "--claude-cmd",
        "missing-claude",
        "--codex-cmd",
        "missing-codex",
    ])
    monkeypatch.setattr(
        "coding_review_agent_loop.config.shutil.which",
        lambda command: pytest.fail(f"unexpected preflight for {command}"),
    )

    config = config_from_args(args, Runner(dry_run=True))

    assert config.dry_run is True


def test_preflight_absolute_path_valid(tmp_path):
    """Absolute path to an existing executable passes preflight and is stored."""
    from coding_review_agent_loop.config import preflight_agent_commands

    parser = build_parser()
    args = parser.parse_args([
        "pr", "77", "--repo", "OWNER/REPO",
        "--claude-cmd", sys.executable,
        "--codex-cmd", "codex",
    ])
    args.coder = "claude"
    runner = Runner()
    preflight_agent_commands(args, runner, ())
    assert runner._resolved_commands[sys.executable] == sys.executable


def test_preflight_absolute_path_not_found_gives_path_error(tmp_path):
    """Nonexistent absolute path gives 'not found or not executable', not 'not found on PATH'."""
    from coding_review_agent_loop.config import preflight_agent_commands

    nonexistent = str(tmp_path / "no-such-binary")
    parser = build_parser()
    args = parser.parse_args([
        "pr", "77", "--repo", "OWNER/REPO",
        "--claude-cmd", nonexistent,
    ])
    args.coder = "claude"
    with pytest.raises(AgentLoopError, match="not found or not executable"):
        preflight_agent_commands(args, Runner(), ())


def test_preflight_absolute_path_dangling_symlink_gives_path_error(tmp_path):
    """Dangling absolute-path symlink gives 'not found or not executable', not 'not found on PATH'."""
    from coding_review_agent_loop.config import preflight_agent_commands

    dangling = tmp_path / "dangling-claude"
    dangling.symlink_to(tmp_path / "missing-target")
    parser = build_parser()
    args = parser.parse_args([
        "pr", "77", "--repo", "OWNER/REPO",
        "--claude-cmd", str(dangling),
    ])
    args.coder = "claude"
    with pytest.raises(AgentLoopError, match="not found or not executable"):
        preflight_agent_commands(args, Runner(), ())


def test_preflight_bare_name_not_found_gives_path_message(monkeypatch):
    """Bare name not on PATH still gives 'not found on PATH' message."""
    from coding_review_agent_loop.config import preflight_agent_commands

    monkeypatch.setattr("coding_review_agent_loop.config.shutil.which", lambda cmd: None)
    parser = build_parser()
    args = parser.parse_args([
        "pr", "77", "--repo", "OWNER/REPO",
        "--claude-cmd", "missing-bare-name",
    ])
    args.coder = "claude"
    with pytest.raises(AgentLoopError, match="not found on PATH"):
        preflight_agent_commands(args, Runner(), ())


def test_omitted_cli_base_is_preserved_for_runtime_resolution(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
    ])

    assert config_from_args(args, FakeRunner()).base is None


def test_pre_review_tests_cli_defaults_on_and_can_be_disabled(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "task",
        "Fix the bug",
        "--repo",
        "OWNER/REPO",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
    ])
    config = config_from_args(args, FakeRunner())
    assert config.pre_review_tests is True

    args = parser.parse_args([
        "task",
        "Fix the bug",
        "--repo",
        "OWNER/REPO",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
        "--no-pre-review-tests",
    ])
    config = config_from_args(args, FakeRunner())
    assert config.pre_review_tests is False


@pytest.mark.parametrize("repo", ["OWNER", "OWNER/", "/REPO", "OWNER/REPO/EXTRA"])
def test_default_agent_workdir_rejects_invalid_repo_formats(repo):
    with pytest.raises(AgentLoopError, match="OWNER/REPO"):
        default_agent_workdir(repo, "codex")


def test_default_agent_memory_dir_uses_xdg_cache_and_repo_scope(monkeypatch, tmp_path):
    cache_home = tmp_path / "xdg-cache"
    monkeypatch.setattr("coding_review_agent_loop.config.sys.platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))

    assert default_agent_memory_dir("OWNER/REPO") == (
        cache_home / "coding-review-agent-loop" / "repos" / "OWNER-REPO" / "memory"
    )


def test_default_cache_root_uses_posix_home_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr("coding_review_agent_loop.config.sys.platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    assert default_cache_root() == tmp_path / ".cache" / "coding-review-agent-loop"


@pytest.mark.parametrize(
    ("platform", "home_parts"),
    [
        ("darwin", ("Library", "Caches", "coding-review-agent-loop")),
        ("win32", ("AppData", "Local", "coding-review-agent-loop", "Cache")),
    ],
)
def test_default_cache_root_uses_platform_home_fallbacks(
    monkeypatch,
    tmp_path,
    platform,
    home_parts,
):
    monkeypatch.setattr("coding_review_agent_loop.config.sys.platform", platform)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert default_cache_root() == tmp_path.joinpath(*home_parts)


def test_default_cache_root_uses_windows_local_app_data(monkeypatch, tmp_path):
    local_app_data = tmp_path / "local-app-data"
    monkeypatch.setattr("coding_review_agent_loop.config.sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    assert default_cache_root() == local_app_data / "coding-review-agent-loop" / "Cache"


@pytest.mark.parametrize("repo", ["OWNER", "OWNER/", "/REPO", "OWNER/REPO/EXTRA"])
def test_default_agent_memory_dir_rejects_invalid_repo_formats(repo):
    with pytest.raises(AgentLoopError, match="OWNER/REPO"):
        default_agent_memory_dir(repo)


@pytest.mark.parametrize("mode", ["ignore", "summarize", "issue", "fix-and-summarize", "fix-and-issue"])
def test_approved_followups_cli_mode_is_configurable(tmp_path, mode):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--approved-followups",
        mode,
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
        "--gemini-dir",
        str(tmp_path / "gemini"),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.approved_followups == mode


@pytest.mark.parametrize(
    "mode",
    ["plan-only", "decompose-only", "implement-one-shot", "implement-by-phase"],
)
def test_plan_execution_mode_cli_is_configurable(tmp_path, mode):
    parser = build_parser()
    args = parser.parse_args([
        "issue",
        "56",
        "--repo",
        "OWNER/REPO",
        "--plan-first",
        "--plan-execution-mode",
        mode,
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
        "--gemini-dir",
        str(tmp_path / "gemini"),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.plan_execution_mode == mode


def test_explicit_agent_dirs_are_preserved_when_others_default(tmp_path):
    parser = build_parser()
    codex_dir = tmp_path / "codex"
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "codex",
        "--reviewer",
        "claude",
        "--codex-dir",
        str(codex_dir),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.codex_dir == codex_dir
    assert config.claude_dir == default_agent_workdir("OWNER/REPO", "claude").resolve()
    assert set(config.auto_agent_dirs) == {"claude", "gemini", "antigravity"}


def test_relative_log_dir_defaults_under_active_coder_workdir(tmp_path):
    parser = build_parser()
    claude_dir = tmp_path / "claude"
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "claude",
        "--reviewer",
        "gemini",
        "--claude-dir",
        str(claude_dir),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.log_dir == claude_dir / ".agent-loop-logs"


def test_agent_memory_flags_configure_memory_dir_and_refresh(tmp_path):
    parser = build_parser()
    codex_dir = tmp_path / "codex"
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "codex",
        "--reviewer",
        "claude",
        "--codex-dir",
        str(codex_dir),
        "--no-agent-memory",
        "--refresh-agent-memory",
        "--refresh-test-profile",
        "--agent-memory-dir",
        "custom-memory",
    ])

    config = config_from_args(args, FakeRunner())

    assert config.agent_memory is False
    assert config.refresh_agent_memory is True
    assert config.refresh_test_profile is True
    assert config.agent_memory_dir == codex_dir / "custom-memory"


def test_agent_memory_explicit_absolute_dir_is_resolved(tmp_path):
    parser = build_parser()
    memory_dir = tmp_path / "memory-parent" / ".." / "agent-memory"
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--agent-memory-dir",
        str(memory_dir),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.agent_memory_dir == memory_dir.resolve()


def test_agent_memory_default_ignores_active_coder_workdir(tmp_path, monkeypatch):
    parser = build_parser()
    cache_home = tmp_path / "cache"
    codex_dir = tmp_path / "codex"
    monkeypatch.setattr("coding_review_agent_loop.config.sys.platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "codex",
        "--reviewer",
        "claude",
        "--codex-dir",
        str(codex_dir),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.agent_memory_dir == (
        cache_home / "coding-review-agent-loop" / "repos" / "OWNER-REPO" / "memory"
    ).resolve()
    assert codex_dir not in config.agent_memory_dir.parents


def test_auto_created_agent_dir_is_cloned_before_use(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    codex_dir = tmp_path / "tmp-root" / "owner-repo" / "codex" / "repo"
    config = make_config(
        tmp_path,
        claude_dir=tmp_path / "explicit-claude",
        codex_dir=codex_dir,
        reviewer="codex",
        auto_agent_dirs=("codex",),
        create_dirs=False,
    )
    config.claude_dir.mkdir(parents=True)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert ["gh", "repo", "clone", "OWNER/REPO", str(codex_dir)] in [
        cmd for cmd, _cwd in runner.commands
    ]
    assert codex_dir.is_dir()


def test_clean_existing_auto_agent_dir_is_synced(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        reviewer="codex",
        auto_agent_dirs=("codex",),
        create_dirs=False,
    )
    config.claude_dir.mkdir(parents=True)
    config.gemini_dir.mkdir(parents=True)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    assert ["git", "fetch", "origin"] in commands
    assert ["git", "switch", "main"] in commands
    assert ["git", "pull", "--ff-only", "origin", "main"] in commands


def test_pr_loop_resolves_pr_base_before_workdir_setup(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        pr_payload={"baseRefName": "develop"},
    )
    config = make_config(
        tmp_path,
        base=None,
        reviewer="codex",
        auto_agent_dirs=("codex",),
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    pr_context_index = command_index(runner.commands, ["gh", "pr", "view"])
    switch_index = command_index(runner.commands, ["git", "switch", "develop"])
    assert pr_context_index < switch_index
    assert ["git", "pull", "--ff-only", "origin", "develop"] in commands
    assert not any("origin/main" in arg for cmd in commands for arg in cmd)


def test_pr_loop_explicit_base_overrides_pr_base_without_repo_default_query(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        pr_payload={"baseRefName": "develop"},
    )
    config = make_config(
        tmp_path,
        base="release",
        reviewer="codex",
        auto_agent_dirs=("codex",),
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    assert ["git", "switch", "release"] in commands
    assert ["git", "switch", "develop"] not in commands
    assert not any(
        cmd[:3] == ["gh", "repo", "view"] and "defaultBranchRef" in cmd
        for cmd in commands
    )


@pytest.mark.parametrize("pr_base", [None, "", "   "])
def test_pr_loop_falls_back_to_repo_default_when_pr_base_is_missing(tmp_path, pr_base):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        pr_payload={"baseRefName": pr_base},
        repo_default_branch="develop",
    )
    config = make_config(
        tmp_path,
        base=None,
        reviewer="codex",
        auto_agent_dirs=("codex",),
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    repo_query_index = command_index(runner.commands, ["gh", "repo", "view"])
    switch_index = command_index(runner.commands, ["git", "switch", "develop"])
    assert repo_query_index < switch_index


@pytest.mark.parametrize("mode", ["issue", "task"])
def test_issue_and_task_loops_use_repo_default_when_base_is_omitted(tmp_path, mode):
    runner = FakeRunner(
        claude_outputs=[
            "Implemented.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        pr_payload={"baseRefName": "develop"},
        repo_default_branch="develop",
    )
    config = make_config(
        tmp_path,
        base=None,
        reviewer="codex",
        auto_agent_dirs=("claude", "codex"),
    )

    if mode == "issue":
        assert run_issue_loop(runner, issue_number=56, config=config) == 0
    else:
        assert run_task_loop(runner, task_text="Add /healthz endpoint.", config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    assert ["git", "switch", "develop"] in commands
    assert not any("origin/main" in arg for cmd in commands for arg in cmd)


def test_unresolved_base_metadata_produces_targeted_override_error(tmp_path):
    runner = FakeRunner(
        pr_payload={"baseRefName": None},
        repo_default_branch=None,
        repo_default_branch_returncode=1,
    )
    config = make_config(tmp_path, base=None, reviewer="codex")

    with pytest.raises(
        AgentLoopError,
        match=r"Unable to resolve a base branch for OWNER/REPO.*--base <branch>",
    ):
        run_pr_loop(runner, pr_number=77, config=config)

    assert not any(cmd[:2] == ["git", "switch"] for cmd, _cwd in runner.commands)


def test_dry_run_base_resolution_defaults_to_main_without_github_query(tmp_path):
    runner = FakeRunner()
    config = make_config(tmp_path, base=None, dry_run=True)

    resolved = resolve_base_branch(config, runner)

    assert resolved.base == "main"
    assert not any(cmd[:1] == ["gh"] for cmd, _cwd in runner.commands)


def test_reviewer_checkout_is_refreshed_to_pr_head_before_review(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    config = make_config(tmp_path, reviewer="codex")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    review_index = command_index(runner.commands, ["codex", "exec"])
    fetch_index = command_index(runner.commands, ["git", "fetch", "origin"], start=0)
    pr_fetch_index = command_index(
        runner.commands,
        ["git", "fetch", "origin", "+pull/77/head:refs/remotes/origin/pr/77"],
    )
    checkout_index = command_index(
        runner.commands,
        ["git", "checkout", "--detach", "refs/remotes/origin/pr/77"],
    )
    head_index = command_index(runner.commands, ["git", "rev-parse", "HEAD"], start=checkout_index)

    assert commands[pr_fetch_index] == ["git", "fetch", "origin", "+pull/77/head:refs/remotes/origin/pr/77"]
    assert fetch_index < pr_fetch_index < checkout_index < head_index < review_index




def test_reviewer_checkout_refreshes_each_round_before_review(tmp_path):
    runner = FakeRunner(
        claude_outputs=["Fixed.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
        codex_outputs=[
            "Please fix it.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    pr_fetches = [
        index
        for index, cmd in enumerate(commands)
        if cmd == ["git", "fetch", "origin", "+pull/77/head:refs/remotes/origin/pr/77"]
    ]
    review_indices = [index for index, cmd in enumerate(commands) if cmd[:2] == ["codex", "exec"]]

    assert len(pr_fetches) == 3
    assert len(review_indices) == 2
    assert pr_fetches[0] < review_indices[0]
    assert pr_fetches[1] < review_indices[1]


def test_dirty_default_reviewer_checkout_is_cleaned_before_review(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        git_status=" M stale.py\n",
    )
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        coder="claude",
        reviewer="codex",
        auto_agent_dirs=("claude", "codex"),
        create_dirs=False,
    )
    config.claude_dir.mkdir(parents=True)
    config.gemini_dir.mkdir(parents=True)

    assert run_pr_loop(runner, pr_number=77, config=config, workdirs_ready=True) == 0

    reset_index = command_index(runner.commands, ["git", "reset", "--hard"])
    clean_index = command_index(runner.commands, ["git", "clean", "-fd"])
    review_index = command_index(runner.commands, ["codex", "exec"])

    assert reset_index < clean_index < review_index


def test_dirty_explicit_reviewer_checkout_fails_before_review_invocation(tmp_path):
    runner = FakeRunner(
        codex_outputs=["This should not run.\n<!-- AGENT_STATE: approved -->"],
        git_status=" M stale.py\n",
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", agent_memory=False)

    with pytest.raises(AgentLoopError, match="--codex-dir is dirty"):
        run_pr_loop(runner, pr_number=77, config=config, workdirs_ready=True)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)




def test_dirty_existing_auto_agent_dir_is_cleaned_before_sync(tmp_path, capsys):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        git_status=" M file.py\n",
    )
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        coder="codex",
        reviewer="codex",
        auto_agent_dirs=("codex",),
        create_dirs=False,
        quiet=False,
    )
    config.gemini_dir.mkdir(parents=True)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    assert ["git", "reset", "--hard"] in commands
    assert ["git", "clean", "-fd"] in commands
    assert ["git", "pull", "--ff-only", "origin", "main"] in commands
    captured = capsys.readouterr()
    assert f"Cleaning dirty default codex workdir: {codex_dir}" in captured.err


def test_dirty_explicit_agent_dir_fails_clearly(tmp_path):
    runner = FakeRunner(git_status=" M file.py\n")
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        coder="codex",
        reviewer="codex",
        create_dirs=False,
    )
    config.gemini_dir.mkdir(parents=True)

    with pytest.raises(AgentLoopError, match="--codex-dir is dirty"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


@pytest.mark.parametrize("loop_name", ["issue", "task"])
def test_dirty_explicit_coder_dir_fails_before_issue_or_task_coder_invocation(tmp_path, loop_name):
    runner = FakeRunner(
        git_status=" M file.py\n",
        codex_outputs=[
            "Implemented.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
    )
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        coder="codex",
        reviewer="codex",
        create_dirs=False,
    )
    config.claude_dir.mkdir(parents=True)
    config.gemini_dir.mkdir(parents=True)

    with pytest.raises(AgentLoopError, match="--codex-dir is dirty"):
        if loop_name == "issue":
            run_issue_loop(runner, issue_number=56, config=config)
        else:
            run_task_loop(runner, task_text="Add /healthz endpoint.", config=config)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


def test_explicit_agent_dir_must_match_requested_repo(tmp_path):
    runner = FakeRunner(git_remote="git@github.com:OTHER/REPO.git")
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        coder="codex",
        reviewer="codex",
        create_dirs=False,
    )
    config.gemini_dir.mkdir(parents=True)

    with pytest.raises(AgentLoopError, match="not 'OWNER/REPO'"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


def test_existing_auto_agent_dir_must_be_git_checkout(tmp_path):
    runner = FakeRunner(git_inside=False)
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        reviewer="codex",
        auto_agent_dirs=("codex",),
        create_dirs=False,
    )
    config.claude_dir.mkdir(parents=True)
    config.gemini_dir.mkdir(parents=True)

    with pytest.raises(AgentLoopError, match="not a git checkout"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


def test_stale_default_workdir_only_logs_is_recreated(tmp_path, capsys):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    (codex_dir / ".agent-loop-logs").mkdir()

    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        reviewer="codex",
        auto_agent_dirs=("codex",),
        create_dirs=False,
        quiet=False,
    )
    config.claude_dir.mkdir(parents=True)
    config.gemini_dir.mkdir(parents=True)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    clone_cmds = [cmd for cmd, _cwd in runner.commands if cmd[:3] == ["gh", "repo", "clone"]]
    assert any(cmd[4] == str(codex_dir) for cmd in clone_cmds), "Expected fresh clone of stale workdir"

    captured = capsys.readouterr()
    assert "Stale default codex workdir detected" in captured.err
    assert "recreating" in captured.err


def test_stale_default_workdir_with_unknown_files_still_fails(tmp_path):
    runner = FakeRunner(git_inside=False)
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    (codex_dir / ".agent-loop-logs").mkdir()
    (codex_dir / "some-user-file.py").write_text("# user work", encoding="utf-8")

    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        reviewer="codex",
        auto_agent_dirs=("codex",),
        create_dirs=False,
    )
    config.claude_dir.mkdir(parents=True)
    config.gemini_dir.mkdir(parents=True)

    with pytest.raises(AgentLoopError, match="not a git checkout"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert not any(cmd[:3] == ["gh", "repo", "clone"] for cmd, _cwd in runner.commands)


def test_stale_default_workdir_empty_is_recreated(tmp_path, capsys):
    """An empty workdir (no .git, no files) is treated as stale and re-cloned."""
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()  # exists but empty

    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        reviewer="codex",
        auto_agent_dirs=("codex",),
        create_dirs=False,
        quiet=False,
    )
    config.claude_dir.mkdir(parents=True)
    config.gemini_dir.mkdir(parents=True)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    clone_cmds = [cmd for cmd, _cwd in runner.commands if cmd[:3] == ["gh", "repo", "clone"]]
    assert any(cmd[4] == str(codex_dir) for cmd in clone_cmds), "Expected fresh clone of empty stale workdir"

    captured = capsys.readouterr()
    assert "Stale default codex workdir detected" in captured.err
    assert "recreating" in captured.err


def test_stale_default_workdir_git_only_is_recreated(tmp_path, capsys):
    """A workdir with only a .git dir (no working tree) is treated as stale and re-cloned."""
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    (codex_dir / ".git").mkdir()  # .git present, but no source files

    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        reviewer="codex",
        auto_agent_dirs=("codex",),
        create_dirs=False,
        quiet=False,
    )
    config.claude_dir.mkdir(parents=True)
    config.gemini_dir.mkdir(parents=True)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    clone_cmds = [cmd for cmd, _cwd in runner.commands if cmd[:3] == ["gh", "repo", "clone"]]
    assert any(cmd[4] == str(codex_dir) for cmd in clone_cmds), "Expected fresh clone of git-only stale workdir"

    captured = capsys.readouterr()
    assert "Stale default codex workdir detected" in captured.err
    assert "recreating" in captured.err


def test_explicit_dir_not_git_checkout_is_not_recreated(tmp_path):
    runner = FakeRunner(git_inside=False)
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    (codex_dir / ".agent-loop-logs").mkdir()

    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        reviewer="codex",
        create_dirs=False,
    )
    config.claude_dir.mkdir(parents=True)
    config.gemini_dir.mkdir(parents=True)

    with pytest.raises(AgentLoopError, match="not a git checkout"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert not any(cmd[:3] == ["gh", "repo", "clone"] for cmd, _cwd in runner.commands)


def test_existing_auto_agent_dir_must_match_requested_repo(tmp_path):
    runner = FakeRunner(git_remote="git@github.com:OTHER/REPO.git")
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        reviewer="codex",
        auto_agent_dirs=("codex",),
        create_dirs=False,
    )
    config.claude_dir.mkdir(parents=True)
    config.gemini_dir.mkdir(parents=True)

    with pytest.raises(AgentLoopError, match="not 'OWNER/REPO'"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


def test_agent_workdir_existing_file_fails_clearly(tmp_path):
    runner = FakeRunner()
    claude_path = tmp_path / "claude-file"
    claude_path.write_text("not a dir", encoding="utf-8")
    config = make_config(tmp_path, claude_dir=claude_path, create_dirs=False)

    with pytest.raises(AgentLoopError, match="not a directory"):
        run_pr_loop(runner, pr_number=77, config=config)


def test_gemini_workdir_existing_file_fails_clearly(tmp_path):
    runner = FakeRunner()
    gemini_path = tmp_path / "gemini-file"
    gemini_path.write_text("not a dir", encoding="utf-8")
    config = make_config(
        tmp_path,
        reviewer="gemini",
        gemini_dir=gemini_path,
        create_dirs=False,
    )

    with pytest.raises(AgentLoopError, match="not a directory"):
        run_pr_loop(runner, pr_number=77, config=config)


def test_config_allows_same_coder_and_reviewer(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "codex",
        "--reviewer",
        "codex",
        "--codex-dir",
        str(tmp_path / "codex"),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.coder == "codex"
    assert config.reviewer == ("codex",)


def test_config_allows_coder_in_multiple_reviewers(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "codex",
        "--reviewer",
        "claude",
        "--reviewer",
        "codex",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.coder == "codex"
    assert config.reviewer == ("claude", "codex")


def test_config_accepts_gemini_as_coder_and_reviewer(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "gemini",
        "--reviewer",
        "claude",
        "--reviewer",
        "gemini",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--gemini-dir",
        str(tmp_path / "gemini"),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.coder == "gemini"
    assert config.reviewer == ("claude", "gemini")
    assert config.gemini_dir == tmp_path / "gemini"


@pytest.mark.parametrize(
    ("coder", "reviewer"),
    [
        ("agy", "codex"),
        ("codex", "agy"),
        ("antigravity", "codex"),
    ],
)
def test_config_normalizes_antigravity_agent_names(tmp_path, coder, reviewer):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--coder",
        coder,
        "--reviewer",
        reviewer,
        "--codex-dir",
        str(tmp_path / "codex"),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.coder == ("antigravity" if coder == "agy" else coder)
    assert config.reviewer == (
        "antigravity" if reviewer == "agy" else reviewer,
    )


def test_config_rejects_duplicate_reviewers(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--reviewer",
        "codex",
        "--reviewer",
        "codex",
        "--codex-dir",
        str(tmp_path / "codex"),
    ])

    with pytest.raises(AgentLoopError, match="same agent more than once"):
        config_from_args(args, FakeRunner())


def test_config_rejects_alias_and_canonical_duplicate_reviewers(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--reviewer",
        "agy",
        "--reviewer",
        "antigravity",
        "--codex-dir",
        str(tmp_path / "codex"),
    ])

    with pytest.raises(AgentLoopError, match="same agent more than once"):
        config_from_args(args, FakeRunner())


@pytest.mark.parametrize("max_rounds", ["0", "-1"])
def test_config_rejects_non_positive_max_rounds(tmp_path, max_rounds):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--max-rounds",
        max_rounds,
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
    ])

    with pytest.raises(AgentLoopError, match="--max-rounds must be greater than zero"):
        config_from_args(args, FakeRunner())


def test_config_defaults_do_not_bypass_agent_permissions(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
    ])
    config = config_from_args(args, FakeRunner())

    assert config.claude_args == ()
    assert config.codex_args == ()
    assert config.gemini_args == ()


def test_config_can_opt_into_dangerous_agent_permissions(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
        "--dangerous-agent-permissions",
    ])
    config = config_from_args(args, FakeRunner())

    assert config.claude_args == ("--dangerously-skip-permissions",)
    assert config.codex_args == ("--dangerously-bypass-approvals-and-sandbox",)
    assert config.gemini_args == ("--yolo", "--skip-trust")


def test_explicit_agent_args_replace_dangerous_profile(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
        "--dangerous-agent-permissions",
        "--claude-arg=--permission-mode",
        "--claude-arg=acceptEdits",
        "--codex-arg=--sandbox",
        "--codex-arg=workspace-write",
        "--gemini-arg=--approval-mode",
        "--gemini-arg=auto_edit",
    ])
    config = config_from_args(args, FakeRunner())

    assert config.claude_args == ("--permission-mode", "acceptEdits")
    assert config.codex_args == ("--sandbox", "workspace-write")
    assert config.gemini_args == ("--approval-mode", "auto_edit")


def test_issue_loop_requires_claude_to_report_pr_number(tmp_path):
    runner = FakeRunner(claude_outputs=["Created something.\n<!-- AGENT_STATE: blocking -->"])
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="PR marker"):
        run_issue_loop(runner, issue_number=56, config=config)


def test_issue_loop_rejects_missing_initial_issue_human_requirements_acknowledgement(tmp_path):
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Keep the legacy flag.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Created PR.\nTests: python -m pytest passed.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->"
        ],
    )
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="missing required signed human requirements marker"):
        run_issue_loop(runner, issue_number=56, config=config)


def test_issue_loop_accepts_initial_issue_human_requirements_acknowledgement(tmp_path):
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Keep the legacy flag.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Created PR.\nTests: python3 -m pytest passed.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: kept the legacy flag path.\n"
            "<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->"
        ],
        codex_outputs=[
            "LGTM.\n<!-- HUMAN_REQUIREMENTS_RESOLVED -->\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config) == 0


def test_issue_loop_rejects_pr_number_before_running_claude(tmp_path):
    runner = FakeRunner(issue_payload={
        "number": 62,
        "state": "closed",
        "is_pr": True,
        "url": "https://github.com/OWNER/REPO/pull/62",
    })
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="pull request, not an issue"):
        run_issue_loop(runner, issue_number=62, config=config)

    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


def test_issue_loop_plan_first_stops_after_approved_plan(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Update the CLI.\n- Add tests.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert any(cmd[:3] == ["claude", "--print", "--output-format"] for cmd, _cwd in runner.commands)
    assert any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)
    assert not any(cmd[:3] == ["gh", "pr", "view"] for cmd, _cwd in runner.commands)
    assert len(runner.comments) == 3
    assert runner.comments[0].startswith("Plan:")
    assert runner.comments[1].startswith("**Review verdict:** Approved\n\nPlan looks sound.")
    assert "Outcome: implement" in runner.comments[2]
    assert not any(cmd[:2] == ["git", "fetch"] for cmd, _cwd in runner.commands)
    assert not any(cmd[:2] == ["git", "switch"] for cmd, _cwd in runner.commands)


def test_parse_plan_decomposition_accepts_agent_and_human_phases():
    parsed = parse_plan_decomposition(
        plan_decomposition_json(
            {
                "title": "Internal schema utilities",
                "scope": "Add helpers.",
                "non_goals": "No live switch.",
                "dependency_notes": "First phase.",
                "rollout_risk": "low - internal only.",
                "validation": "Run python -m pytest.",
                "parent_context": "Approved plan slice and invariant details.",
                "automation": "agent-pr",
                "depends_on": [],
            },
            {
                "title": "Manual rollout checkpoint",
                "scope": "Human validates the deployed behavior.",
                "non_goals": "No code changes.",
                "dependency_notes": "After Internal schema utilities.",
                "rollout_risk": "medium - live checkpoint.",
                "validation": "Human remark and closure required.",
                "parent_context": "Approved plan slice for the manual checkpoint.",
                "automation": "human-action",
                "depends_on": ["Internal schema utilities"],
            },
        )
    )

    assert [phase.title for phase in parsed.phases] == [
        "Internal schema utilities",
        "Manual rollout checkpoint",
    ]
    assert parsed.phases[1].automation == "human-action"
    assert parsed.phases[1].depends_on == ("Internal schema utilities",)


def test_parse_plan_decomposition_accepts_normalized_earlier_phase_dependency():
    parsed = parse_plan_decomposition(
        plan_decomposition_json(
            {
                "title": "Internal schema utilities",
                "scope": "Add helpers.",
                "non_goals": "No live switch.",
                "dependency_notes": "First phase.",
                "rollout_risk": "low - internal only.",
                "validation": "Run python -m pytest.",
                "parent_context": "Approved plan slice and invariant details.",
                "automation": "agent-pr",
                "depends_on": [],
            },
            {
                "title": "Manual rollout checkpoint",
                "scope": "Human validates the deployed behavior.",
                "non_goals": "No code changes.",
                "dependency_notes": "After Internal schema utilities.",
                "rollout_risk": "medium - live checkpoint.",
                "validation": "Human remark and closure required.",
                "parent_context": "Approved plan slice for the manual checkpoint.",
                "automation": "human-action",
                "depends_on": ["  internal   SCHEMA utilities  "],
            },
        )
    )

    assert parsed.phases[1].depends_on == ("internal   SCHEMA utilities",)


def test_parse_plan_decomposition_rejects_self_dependency():
    phase = {
        "title": "Internal schema utilities",
        "scope": "Add helpers.",
        "non_goals": "No live switch.",
        "dependency_notes": "First phase.",
        "rollout_risk": "low - internal only.",
        "validation": "Run python -m pytest.",
        "parent_context": "Approved plan slice and invariant details.",
        "automation": "agent-pr",
        "depends_on": ["Internal schema utilities"],
    }

    with pytest.raises(AgentLoopError, match="cannot depend on itself"):
        parse_plan_decomposition(plan_decomposition_json(phase))


def test_parse_plan_decomposition_rejects_forward_dependency():
    first_phase = {
        "title": "Internal schema utilities",
        "scope": "Add helpers.",
        "non_goals": "No live switch.",
        "dependency_notes": "First phase.",
        "rollout_risk": "low - internal only.",
        "validation": "Run python -m pytest.",
        "parent_context": "Approved plan slice and invariant details.",
        "automation": "agent-pr",
        "depends_on": ["Manual rollout checkpoint"],
    }
    second_phase = {
        "title": "Manual rollout checkpoint",
        "scope": "Human validates the deployed behavior.",
        "non_goals": "No code changes.",
        "dependency_notes": "After Internal schema utilities.",
        "rollout_risk": "medium - live checkpoint.",
        "validation": "Human remark and closure required.",
        "parent_context": "Approved plan slice for the manual checkpoint.",
        "automation": "human-action",
        "depends_on": [],
    }

    with pytest.raises(AgentLoopError, match="dependencies must reference an earlier phase"):
        parse_plan_decomposition(plan_decomposition_json(first_phase, second_phase))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda phase: phase.pop("parent_context"), "parent_context"),
        (lambda phase: phase.pop("rollout_risk"), "rollout_risk"),
        (lambda phase: phase.pop("validation"), "validation"),
        (lambda phase: phase.__setitem__("automation", "robot"), "invalid automation"),
        (lambda phase: phase.__setitem__("depends_on", ["Missing phase"]), "unknown phase"),
    ],
)
def test_parse_plan_decomposition_rejects_invalid_phase_fields(mutate, message):
    phase = {
        "title": "Internal schema utilities",
        "scope": "Add helpers.",
        "non_goals": "No live switch.",
        "dependency_notes": "First phase.",
        "rollout_risk": "low - internal only.",
        "validation": "Run python -m pytest.",
        "parent_context": "Approved plan slice and invariant details.",
        "automation": "agent-pr",
        "depends_on": [],
    }
    mutate(phase)

    with pytest.raises(AgentLoopError, match=message):
        parse_plan_decomposition(plan_decomposition_json(phase))


def test_parse_plan_decomposition_rejects_duplicates_and_over_cap():
    phase = {
        "title": "Repeated phase",
        "scope": "Add helpers.",
        "non_goals": "No live switch.",
        "dependency_notes": "First phase.",
        "rollout_risk": "low - internal only.",
        "validation": "Run python -m pytest.",
        "parent_context": "Approved plan slice and invariant details.",
        "automation": "agent-pr",
        "depends_on": [],
    }
    with pytest.raises(AgentLoopError, match="duplicate phase title"):
        parse_plan_decomposition(plan_decomposition_json(phase, dict(phase)))

    phases = [dict(phase, title=f"Phase {index}") for index in range(MAX_DECOMPOSITION_PHASES + 1)]
    with pytest.raises(AgentLoopError, match="MAX_DECOMPOSITION_PHASES"):
        parse_plan_decomposition(plan_decomposition_json(*phases))


def test_issue_loop_plan_first_rejects_missing_initial_plan_human_requirements_acknowledgement(
    tmp_path,
):
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Keep the public API unchanged.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Plan:\n- Update the parser.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
        ],
    )
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="missing required signed human requirements marker"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)


def test_issue_loop_plan_first_accepts_initial_plan_human_requirements_acknowledgement(tmp_path):
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Keep the public API unchanged.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Plan:\n- Update the parser.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: the plan keeps the public API unchanged.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
        ],
        codex_outputs=[structured_plan_review(summary="Plan looks sound.", human_requirements_resolved=True)],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0


def test_issue_loop_plan_first_revises_until_all_reviewers_approve(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            structured_plan_revision(summary="Revised plan with tests."),
        ],
        codex_outputs=[
            structured_plan_review(
                state="blocking",
                summary="Missing test strategy.",
                blocking_plan_issues=["Missing test strategy."],
            ),
            structured_plan_review(
                summary="Plan looks sound.",
                prior_plan_item_dispositions=[{"item_id": "item-1", "disposition": "resolved"}],
            ),
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0


def test_issue_loop_structured_plan_state_public_comment_renders_markdown_and_preserves_metadata(tmp_path):
    raw_structured_plan = structured_plan_state(
        summary="Plan the issue fix.",
        plan_steps=["Update the renderer.", "Add regression tests."],
        reviewer="Google Antigravity",
    )
    runner = FakeRunner(
        antigravity_outputs=[raw_structured_plan],
        codex_outputs=[structured_plan_review(summary="Plan looks sound.")],
    )
    config = make_config(tmp_path, coder="antigravity", reviewer="codex")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    public_comment = runner.comments[0]
    assert public_comment.startswith("## Plan")
    assert "### Plan steps\n1. Update the renderer.\n2. Add regression tests." in public_comment
    assert '"kind": "plan_state"' not in _strip_round_metadata(public_comment)

    raw_comment = runner.issue_comments[0]["body"]
    match = re.search(r"<!--\s*AGENT_LOOP_META:\s*(?P<payload>[A-Za-z0-9+/=_-]+)\s*-->", raw_comment)
    assert match is not None
    metadata = _decode_round_metadata(match.group("payload"))
    assert metadata.canonical_plan == raw_structured_plan
    assert metadata.raw_structured_coder_response == raw_structured_plan


def test_issue_loop_markdown_plan_state_public_comment_passes_through(tmp_path):
    markdown_plan = "Initial markdown plan.\n<!-- AGENT_PLAN_STATE: approved -->\n-- Anthropic Claude"
    runner = FakeRunner(
        claude_outputs=[markdown_plan],
        codex_outputs=[structured_plan_review(summary="Plan looks sound.")],
    )
    config = make_config(tmp_path, reviewer="codex")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    raw_comment = runner.issue_comments[0]["body"]
    metadata_match = re.search(
        r"\n?<!--\s*AGENT_LOOP_META:\s*[A-Za-z0-9+/=_-]+\s*-->\n?",
        raw_comment,
    )
    assert metadata_match is not None
    assert raw_comment.replace(metadata_match.group(0), "\n").strip() == markdown_plan
    metadata = _decode_round_metadata(
        re.search(r"<!--\s*AGENT_LOOP_META:\s*(?P<payload>[A-Za-z0-9+/=_-]+)\s*-->", raw_comment)
        .group("payload")
    )
    assert metadata.canonical_plan is None
    assert metadata.raw_structured_coder_response is None


def test_issue_loop_plan_revision_stores_raw_structured_metadata(tmp_path):
    raw_structured_revision = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_revision",
                "state": "blocking",
                "summary": "Revised plan with tests.",
                "prior_plan_item_dispositions": [
                    {"item_id": "item-1", "disposition": "resolved", "note": "Added the missing test step."}
                ],
                "plan_steps": ["Add the regression test.", "Run the focused suite."],
            }
        )
        + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            raw_structured_revision,
        ],
        codex_outputs=[
            structured_plan_review(
                state="blocking",
                summary="Missing test strategy.",
                blocking_plan_issues=["Missing test strategy."],
            ),
            structured_plan_review(
                summary="Plan looks sound.",
                prior_plan_item_dispositions=[{"item_id": "item-1", "disposition": "resolved"}],
            ),
        ],
    )
    config = make_config(tmp_path, reviewer="codex")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert runner.comments[2].startswith("## Revised plan")
    assert '"kind": "plan_revision"' not in _strip_round_metadata(runner.comments[2])
    raw_comment = runner.issue_comments[2]["body"]
    match = re.search(r"<!--\s*AGENT_LOOP_META:\s*(?P<payload>[A-Za-z0-9+/=_-]+)\s*-->", raw_comment)
    assert match is not None
    metadata = _decode_round_metadata(match.group("payload"))
    assert metadata.raw_structured_coder_response == raw_structured_revision


def test_issue_loop_plan_revision_rejects_missing_human_requirements_acknowledgement(tmp_path):
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Preserve backward compatibility.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Initial plan.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: the plan preserves backward compatibility.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            structured_plan_revision(summary="Revised plan."),
            "Revised plan.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: the revised plan still preserves backward compatibility.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            structured_plan_review(
                state="blocking",
                summary="Missing a regression test.",
                blocking_plan_issues=["Missing a regression test."],
            )
        ],
    )
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="missing required signed human requirements marker"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)


def test_issue_loop_plan_revision_accepts_human_requirements_acknowledgement(tmp_path):
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Preserve backward compatibility.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Initial plan.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: the plan preserves backward compatibility.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            structured_plan_revision(
                summary="Revised plan.",
                human_requirements=(
                    f"\n{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
                    "### Human requirements\n"
                    "- Requirement 1: the revised plan still preserves backward compatibility.\n"
                ),
            ),
        ],
        codex_outputs=[
            structured_plan_review(
                state="blocking",
                summary="Missing a regression test.",
                blocking_plan_issues=["Missing a regression test."],
            ),
            structured_plan_review(
                summary="Plan looks sound.",
                prior_plan_item_dispositions=[{"item_id": "item-1", "disposition": "resolved"}],
                human_requirements_resolved=True,
            ),
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(claude_calls) == 2
    assert "Missing a regression test." in claude_calls[1][-1]
    assert len(runner.comments) == 5
    assert runner.comments[2].startswith("## Revised plan")


def test_issue_loop_plan_revision_repair_preserves_signed_human_requirements(tmp_path):
    malformed_revision = (
        "### Prior plan review item dispositions\n"
        "- item-1: resolved by adding compatibility tests.\n\n"
        "### Revised plan\n"
        "- Preserve backward compatibility.\n"
        "- Add regression tests.\n\n"
        f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
        "### Human requirements\n"
        "- Requirement 1: the revised plan preserves backward compatibility.\n\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    repaired_revision = structured_plan_revision(
        summary="Revised plan with compatibility tests.",
        prior_plan_item_dispositions=[
            {
                "item_id": "item-1",
                "disposition": "resolved",
                "note": "Added compatibility tests.",
            }
        ],
        plan_steps=["Preserve backward compatibility.", "Add regression tests."],
        human_requirements=(
            f"\n{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: the revised plan preserves backward compatibility.\n"
        ),
    )
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Preserve backward compatibility.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Initial plan.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: the plan preserves backward compatibility.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            malformed_revision,
        ],
        codex_outputs=[
            structured_plan_review(
                state="blocking",
                summary="Missing a regression test.",
                blocking_plan_issues=["Missing a regression test."],
            ),
            structured_plan_review(
                summary="Plan looks sound.",
                prior_plan_item_dispositions=[{"item_id": "item-1", "disposition": "resolved"}],
                human_requirements_resolved=True,
            ),
        ],
    )
    config = make_config(tmp_path, agent_max_retries=0)
    captured_repairs = []

    def fake_attempt_repair(raw: str, gemini_cmd: str, *, expected_kind: str | None = None, unresolved_item_ids=None, surfaced_requirement_ids=None, allowed_prior_item_ids=None, unknown_prior_item_ids=None, same_round_context=None) -> str | None:
        captured_repairs.append((raw, expected_kind))
        return repaired_revision

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", fake_attempt_repair):
        assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert len(captured_repairs) == 1
    assert captured_repairs[0][1] == "plan_revision"
    assert HUMAN_REQUIREMENTS_ADDRESSED_MARKER in captured_repairs[0][0]
    public_revision = _strip_round_metadata(runner.comments[2])
    assert '"kind": "plan_revision"' not in public_revision
    assert HUMAN_REQUIREMENTS_ADDRESSED_MARKER in public_revision
    assert "### Human requirements" in public_revision


def test_issue_loop_plan_revision_repair_rejects_wrong_kind_from_human_requirements_text(tmp_path):
    malformed_revision = (
        "### Revised plan\n"
        "- Preserve backward compatibility.\n\n"
        f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
        "### Human requirements\n"
        "- Requirement 1: the revised plan preserves backward compatibility.\n\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    wrong_kind_repair = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Revised the plan.",
                "addressed_items": [],
                "remaining_items": [],
                "human_requirements": {
                    "addressed_ids": ["Requirement 1"],
                    "checked_discussion_directly": False,
                },
            }
        )
        + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Preserve backward compatibility.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Initial plan.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: the plan preserves backward compatibility.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            malformed_revision,
        ],
        codex_outputs=[
            structured_plan_review(
                state="blocking",
                summary="Missing a regression test.",
                blocking_plan_issues=["Missing a regression test."],
            )
        ],
    )
    config = make_config(tmp_path, agent_max_retries=0)
    captured_kinds = []

    def fake_attempt_repair(raw: str, gemini_cmd: str, *, expected_kind: str | None = None, unresolved_item_ids=None, surfaced_requirement_ids=None, allowed_prior_item_ids=None, unknown_prior_item_ids=None, same_round_context=None) -> str | None:
        captured_kinds.append(expected_kind)
        return wrong_kind_repair

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", fake_attempt_repair):
        with pytest.raises(AgentLoopError, match="expected `plan_revision`"):
            run_issue_loop(runner, issue_number=56, config=config, plan_first=True)

    assert captured_kinds == ["plan_revision"]


def test_issue_loop_plan_revision_repair_without_human_ack_fails_clearly(tmp_path):
    malformed_revision = (
        "### Revised plan\n"
        "- Preserve backward compatibility.\n\n"
        f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
        "### Human requirements\n"
        "- Requirement 1: the revised plan preserves backward compatibility.\n\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    repaired_without_ack = structured_plan_revision(
        summary="Revised plan with compatibility tests.",
        plan_steps=["Preserve backward compatibility.", "Add regression tests."],
    )
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Preserve backward compatibility.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Initial plan.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: the plan preserves backward compatibility.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            malformed_revision,
        ],
        codex_outputs=[
            structured_plan_review(
                state="blocking",
                summary="Missing a regression test.",
                blocking_plan_issues=["Missing a regression test."],
            )
        ],
    )
    config = make_config(tmp_path, agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value=repaired_without_ack):
        with pytest.raises(AgentLoopError, match="missing required signed human requirements marker"):
            run_issue_loop(runner, issue_number=56, config=config, plan_first=True)


def test_issue_loop_plan_first_requires_reviewers_to_disposition_prior_items(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Revised plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Second revised plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "### Blocking plan issues\n- Add parser validation tests.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
            "Still needs the test.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, max_rounds=2)

    with pytest.raises(AgentLoopError, match="did not evaluate all prior unresolved plan items"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)


def test_issue_loop_plan_first_carries_same_plan_item_across_reviewers_and_rounds(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Revised plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Second revised plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "### Same-plan follow-ups\n- Add the carry-forward orchestration test.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
            "Still needs one plan refinement."
            + prior_plan_item_dispositions("[item-1] same-plan: still need the mixed-reviewer case")
            + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
            "Plan looks sound."
            + prior_plan_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
        gemini_outputs=[
            "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- Google Gemini",
            "Plan looks sound now."
            + prior_plan_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- Google Gemini",
            "Final pass."
            + prior_plan_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- Google Gemini",
        ],
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"), max_rounds=3)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert any("item-1" in call[-1] for call in claude_calls[1:])
    assert "Approved plan:" in runner.comments[-1]


def test_issue_loop_plan_first_posts_human_readable_item_labels_in_new_and_prior_sections(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Revised plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "### Blocking plan issues\n"
            "- Keep plan-review wording distinct from PR wording.\n"
            "### Same-plan follow-ups\n"
            "- Add one carry-forward plan test.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
            "Plan looks sound."
            + prior_plan_item_dispositions(
                "[item-1] resolved",
                "[item-2] resolved",
            )
            + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, reviewer="codex", max_rounds=2)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert runner.comments[1] == (
        "**Review verdict:** Blocking\n\n"
        "### Blocking plan issues\n"
        "- Keep plan-review wording distinct from PR wording.\n"
        "\n"
        "### Same-plan follow-ups\n"
        "- Add one carry-forward plan test.\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n"
        "-- OpenAI Codex"
    )
    assert runner.comments[3] == (
        "**Review verdict:** Approved\n\n"
        "Plan looks sound.\n\n"
        "### Prior unresolved plan item dispositions\n"
        "- [item-1] Blocking issue from OpenAI Codex, round 1: Keep plan-review wording distinct from PR wording. -> resolved\n"
        "- [item-2] Same-plan follow-up from OpenAI Codex, round 1: Add one carry-forward plan test. -> resolved\n"
        "<!-- AGENT_PLAN_STATE: approved -->\n"
        "-- OpenAI Codex"
    )


def test_issue_loop_plan_first_does_not_expose_same_round_item_ids_to_later_reviewers(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
        ],
        gemini_outputs=[
            "### Same-plan follow-ups\n"
            "- Add the carry-forward orchestration test.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Google Gemini",
        ],
        claude_outputs=[
            "Still blocked.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(
        tmp_path,
        coder="codex",
        reviewer=("gemini", "claude"),
        max_rounds=1,
    )

    with pytest.raises(AgentLoopError, match="still reported blocking plan issues after round 1"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)

    second_reviewer_prompt = [
        cmd[-1]
        for cmd, _cwd in runner.commands
        if cmd[:1] == ["claude"] and "planning round 1" in cmd[-1]
    ][0]
    assert "Only items listed under `Prior unresolved plan items from earlier rounds`" in second_reviewer_prompt
    assert "[item-1]" not in second_reviewer_prompt
    assert "### New tracked unresolved items" not in runner.comments[1]


def test_issue_loop_plan_first_uses_compact_context_after_round_one(tmp_path, capsys):
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            structured_plan_revision(
                summary="Resolve item one.",
                prior_plan_item_dispositions=[
                    {"item_id": "item-1", "disposition": "resolved", "note": "Covered."}
                ],
                plan_steps=["Revised plan after round one."],
            ),
            structured_plan_revision(
                summary="Resolve item two.",
                prior_plan_item_dispositions=[
                    {"item_id": "item-2", "disposition": "resolved", "note": "Covered."}
                ],
                plan_steps=["Revised plan after round two."],
            ),
        ],
        codex_outputs=[
            structured_plan_review(
                state="blocking",
                blocking_plan_issues=["Round one issue."],
            ),
            structured_plan_review(
                state="blocking",
                blocking_plan_issues=["Round two issue."],
                prior_plan_item_dispositions=[
                    {"item_id": "item-1", "disposition": "resolved", "note": "Covered."}
                ],
            ),
            structured_plan_review(
                state="approved",
                prior_plan_item_dispositions=[
                    {"item_id": "item-2", "disposition": "resolved", "note": "Covered."}
                ],
            ),
        ],
    )
    config = make_config(
        tmp_path,
        coder="claude",
        reviewer=("codex",),
        max_rounds=3,
        quiet=False,
    )

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    prompts = [cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] in (["claude"], ["codex"])]
    round_one_review = next(prompt for prompt in prompts if "planning round 1" in prompt)
    round_two_review = next(prompt for prompt in prompts if "Planning round: 2" in prompt and "Role: reviewer" in prompt)
    round_two_revision = next(prompt for prompt in prompts if "Planning round: 2" in prompt and "Role: coder" in prompt)
    assert COMPACT_PLANNING_VOLATILE_TAIL_MARKER not in round_one_review
    assert COMPACT_PLANNING_VOLATILE_TAIL_MARKER in round_two_review
    assert COMPACT_PLANNING_VOLATILE_TAIL_MARKER in round_two_revision

    captured = capsys.readouterr()
    assert "Planning issue #56: invoking Claude (context mode: full)" in captured.err
    assert "Planning round 2: Codex reviewing issue #56 (context mode: compact)" in captured.err
    assert "Planning round 2: Claude revising the plan (context mode: compact)" in captured.err


def test_issue_loop_plan_first_requires_reviewer_human_requirements_resolution(tmp_path, capsys):
    runner = FakeRunner(
        issue_payload={
            "body": "Keep compact context cache-aware.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Initial plan covers cache-aware compact context.\n"
            "<!-- HUMAN_REQUIREMENTS_ADDRESSED -->\n\n"
            "### Human requirements\n"
            "- Requirement 1: The plan keeps compact context cache-aware.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            structured_plan_revision(
                summary="Revised plan requires explicit reviewer acknowledgement.",
                prior_plan_item_dispositions=[
                    {"item_id": "item-1", "disposition": "resolved", "note": "Reviewer must acknowledge."}
                ],
                plan_steps=["Keep the compact context cache-aware and require reviewer acknowledgement."],
                human_requirements=(
                    "\n<!-- HUMAN_REQUIREMENTS_ADDRESSED -->\n\n"
                    "### Human requirements\n"
                    "- Requirement 1: The revised plan covers the cache-aware compact context requirement."
                ),
            ),
        ],
        codex_outputs=[
            structured_plan_review(state="approved"),
            structured_plan_review(
                state="approved",
                prior_plan_item_dispositions=[
                    {"item_id": "item-1", "disposition": "resolved", "note": "Acknowledged."}
                ],
                human_requirements_resolved=True,
            ),
        ],
    )
    config = make_config(
        tmp_path,
        coder="claude",
        reviewer=("codex",),
        max_rounds=2,
        plan_execution_mode="plan-only",
        quiet=False,
    )

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert "Approved plan:" in runner.comments[-1]
    assert any(
        "approved without acknowledging the signed human requirements" in comment
        for comment in runner.comments
    )
    assert "<!-- HUMAN_REQUIREMENTS_RESOLVED -->" in runner.comments[-2]
    captured = capsys.readouterr()
    assert "approved without acknowledging signed human requirements" in captured.err


def test_issue_loop_plan_first_uses_full_context_when_plan_ledger_incomplete(tmp_path, capsys):
    old_plan = "Old plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    new_plan = "New plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    old_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Old subject item that could be missed.",
        status="blocking",
        source_status="blocking",
    )
    old_reviewer_comment = _attach_round_metadata(
        structured_plan_review(
            state="blocking",
            blocking_plan_issues=["Old subject item that could be missed."],
        ),
        PostedRoundMetadata(
            flow="plan",
            role="reviewer",
            agent="Codex",
            round_number=1,
            subject=_plan_subject(old_plan),
            new_items=(old_item,),
            state="blocking",
        ),
    )
    latest_coder_comment = _attach_round_metadata(
        new_plan,
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=_plan_subject(new_plan),
            prior_items=(),
        ),
    )
    runner = FakeRunner(
        issue_comments=[
            {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:00:00Z", "body": old_reviewer_comment},
            {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:10:00Z", "body": latest_coder_comment},
        ],
        codex_outputs=[structured_plan_review(state="approved")],
    )
    config = make_config(
        tmp_path,
        coder="claude",
        reviewer=("codex",),
        quiet=False,
    )

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    review_prompt = [
        cmd[-1]
        for cmd, _cwd in runner.commands
        if cmd[:2] == ["codex", "exec"] and "planning round 2" in cmd[-1]
    ][0]
    assert COMPACT_PLANNING_VOLATILE_TAIL_MARKER not in review_prompt
    captured = capsys.readouterr()
    assert "Planning round 2: Codex reviewing issue #56 (context mode: full (ledger incomplete))" in captured.err


def test_issue_loop_plan_first_resumes_with_only_missing_reviewer_for_current_plan(tmp_path):
    current_plan = "Revised plan.\n- Add state reconstruction.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    coder_comment = _attach_round_metadata(
        current_plan,
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=_plan_subject(current_plan),
            prior_items=(),
        ),
    )
    codex_comment = _attach_round_metadata(
        "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="plan",
            role="reviewer",
            agent="Codex",
            round_number=2,
            subject=_plan_subject(current_plan),
            state="approved",
        ),
    )
    runner = FakeRunner(
        issue_comments=[
            {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:00:00Z", "body": coder_comment},
            {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:05:00Z", "body": codex_comment},
        ],
        gemini_outputs=["Plan looks sound too.\n<!-- AGENT_PLAN_STATE: approved -->\n-- Google Gemini"],
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"))

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    agent_commands = [cmd[0] for cmd, _cwd in runner.commands if cmd[:1] in (["claude"], ["codex"], ["gemini"])]
    assert agent_commands == ["gemini"]
    assert runner.comments[-1].startswith("Planning complete for issue #56.")


def test_resume_plan_round_prefers_latest_metadata_ledger_for_same_plan_replay():
    current_plan = "Revised plan.\n- Add the active-ledger replay test.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    subject = _plan_subject(current_plan)
    stale_item = UnresolvedReviewItem(
        item_id="item-3",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Stale plan replay item.",
        status="same-plan",
        source_status="same-plan",
    )
    active_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Active plan replay item.",
        status="same-plan",
        source_status="same-plan",
    )
    stale_coder_comment = _attach_round_metadata(
        current_plan,
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=subject,
            prior_items=(stale_item,),
            canonical_plan=current_plan,
        ),
    )
    stale_reviewer_comment = _attach_round_metadata(
        "Still needs work."
        + prior_plan_item_dispositions("[item-3] same-plan: stale replay")
        + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="plan",
            role="reviewer",
            agent="Codex",
            round_number=2,
            subject=subject,
            prior_items=(stale_item,),
            dispositions=(
                parse_plan_item_dispositions(
                    prior_plan_item_dispositions("[item-3] same-plan: stale replay"),
                    reviewer="OpenAI Codex",
                )[0],
            ),
            state="blocking",
        ),
    )
    active_coder_comment = _attach_round_metadata(
        current_plan,
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=subject,
            prior_items=(active_item,),
            canonical_plan=current_plan,
        ),
    )
    active_reviewer_comment = _attach_round_metadata(
        "Plan looks sound."
        + prior_plan_item_dispositions("[item-1] resolved")
        + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- Google Gemini",
        PostedRoundMetadata(
            flow="plan",
            role="reviewer",
            agent="Gemini",
            round_number=2,
            subject=subject,
            prior_items=(active_item,),
            dispositions=(
                parse_plan_item_dispositions(
                    prior_plan_item_dispositions("[item-1] resolved"),
                    reviewer="Google Gemini",
                )[0],
            ),
            state="approved",
        ),
    )

    resumed = _resume_plan_round(
        [
            IssueComment(author="bot", created_at="2026-05-25T00:00:00Z", body=stale_coder_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:01:00Z", body=stale_reviewer_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:02:00Z", body=active_coder_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:03:00Z", body=active_reviewer_comment),
        ],
        configured_reviewers=("codex", "gemini"),
    )

    assert resumed is not None
    current_plan_text, resumed_state = resumed
    assert current_plan_text == current_plan
    assert [item.item_id for item in resumed_state.prior_items] == ["item-1"]
    assert resumed_state.next_unresolved_item_number == 4
    assert [record.metadata.agent for record in resumed_state.completed_reviews] == ["Gemini"]


def test_resume_plan_round_prefers_canonical_plan_metadata():
    public_body = (
        "Revised plan summary.\n\n### Plan steps\n1. Public body copy.\n\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    canonical_plan = (
        "Revised plan summary.\n\n### Prior plan review item dispositions\n- None.\n\n"
        "### Plan steps\n1. Canonical copy."
    )
    coder_comment = _attach_round_metadata(
        public_body,
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=_plan_subject(canonical_plan),
            prior_items=(),
            canonical_plan=canonical_plan,
        ),
    )

    resumed = _resume_plan_round(
        [IssueComment(author="bot", created_at="2026-05-20T09:00:00Z", body=coder_comment)],
        configured_reviewers=("codex", "gemini"),
    )

    assert resumed is not None
    current_plan, state = resumed
    assert current_plan == canonical_plan
    assert state.coder_output == canonical_plan


def test_resume_plan_round_prefers_structured_plan_revision_metadata_for_coder_output():
    public_body = (
        "## Revised plan\n\nRevised plan summary.\n\n### Plan steps\n1. Public body copy.\n\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    raw_structured_revision = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_revision",
                "state": "blocking",
                "summary": "Revised plan summary.",
                "prior_plan_item_dispositions": [],
                "plan_steps": ["Canonical copy."],
            }
        )
        + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    parsed = validate_structured_plan_revision(raw_structured_revision)
    assert parsed is not None
    canonical_plan = render_canonical_plan_revision(parsed, ())
    coder_comment = _attach_round_metadata(
        public_body,
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=_plan_subject(canonical_plan),
            prior_items=(),
            canonical_plan=canonical_plan,
            raw_structured_coder_response=raw_structured_revision,
        ),
    )

    resumed = _resume_plan_round(
        [IssueComment(author="bot", created_at="2026-05-20T09:00:00Z", body=coder_comment)],
        configured_reviewers=("codex", "gemini"),
    )

    assert resumed is not None
    current_plan, state = resumed
    assert current_plan == canonical_plan
    assert state.coder_output == raw_structured_revision
    assert validate_structured_plan_revision(state.coder_output) is not None
    assert '"kind": "plan_revision"' not in _strip_round_metadata(coder_comment)


def test_resume_plan_round_falls_back_to_raw_body_for_markdown_plan():
    plan = "Revised plan.\n- Add state reconstruction.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    coder_comment = _attach_round_metadata(
        plan,
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=_plan_subject(plan),
            prior_items=(),
        ),
    )

    resumed = _resume_plan_round(
        [IssueComment(author="bot", created_at="2026-05-20T09:00:00Z", body=coder_comment)],
        configured_reviewers=("codex",),
    )

    assert resumed is not None
    current_plan, state = resumed
    assert current_plan == plan
    assert state.coder_output == plan


def test_plan_subject_ignores_trailing_whitespace_added_by_metadata_round_trip():
    plan = "Revised plan.\n- Add state reconstruction.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"

    attached = _attach_round_metadata(
        f"{plan}\n",
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=_plan_subject(f"{plan}\n"),
            prior_items=(),
        ),
    )

    assert _plan_subject(f"{plan}\n") == _plan_subject(_strip_round_metadata(attached))


def test_round_metadata_round_trip_preserves_canonical_plan():
    metadata = PostedRoundMetadata(
        flow="plan",
        role="coder",
        agent="Claude",
        round_number=2,
        subject="abc",
        canonical_plan="Summary\n\n### Plan steps\n1. Canonical step.",
    )

    assert _decode_round_metadata(_encode_round_metadata(metadata)).canonical_plan == metadata.canonical_plan


def test_round_metadata_round_trip_preserves_compact_prior_summaries():
    metadata = PostedRoundMetadata(
        flow="plan",
        role="coder",
        agent="Claude",
        round_number=2,
        subject="abc",
        compact_prior_summaries=("[item-1] resolved: full prior text",),
    )

    decoded = _decode_round_metadata(_encode_round_metadata(metadata))

    assert decoded.compact_prior_summaries == metadata.compact_prior_summaries


def test_decode_old_round_metadata_defaults_compact_prior_summaries_to_empty():
    payload = {
        "flow": "plan",
        "role": "coder",
        "agent": "Claude",
        "round_number": 2,
        "subject": "abc",
        "prior_items": [],
        "dispositions": [],
        "new_items": [],
        "state": None,
        "canonical_plan": None,
        "raw_structured_coder_response": None,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")

    assert _decode_round_metadata(encoded).compact_prior_summaries == ()


def test_resume_plan_round_restores_compact_prior_summaries_across_subject_change():
    old_plan = "Old plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    new_plan = "New plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    old_subject = _plan_subject(old_plan)
    new_subject = _plan_subject(new_plan)
    old_summary = "[item-1] resolved: old-subject resolved summary"
    old_coder_comment = _attach_round_metadata(
        old_plan,
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=old_subject,
            compact_prior_summaries=(old_summary,),
        ),
    )
    new_coder_comment = _attach_round_metadata(
        new_plan,
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=3,
            subject=new_subject,
            compact_prior_summaries=(old_summary,),
        ),
    )

    resumed = _resume_plan_round(
        [
            IssueComment(author="bot", created_at="2026-05-20T09:00:00Z", body=old_coder_comment),
            IssueComment(author="bot", created_at="2026-05-20T09:10:00Z", body=new_coder_comment),
        ],
        configured_reviewers=("codex",),
    )

    assert resumed is not None
    _current_plan, state = resumed
    assert state.compact_prior_summaries == (old_summary,)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"flow": "plan"},
        {
            "flow": "plan",
            "role": "coder",
            "agent": "Claude",
            "round_number": "not-an-int",
            "subject": "abc",
        },
    ],
)
def test_decode_round_metadata_rejects_missing_or_invalid_required_fields(payload):
    encoded = json.dumps(payload).encode("utf-8")

    with pytest.raises(AgentLoopError, match="Invalid AGENT_LOOP_META payload"):
        _decode_round_metadata(encoded=base64.urlsafe_b64encode(encoded).decode("ascii"))


@pytest.mark.parametrize(
    "line",
    [
        "[item-1] same-plan: none",
        "[item-1] still blocking: none",
        "[item-1] future follow-up: none",
    ],
)
def test_issue_loop_plan_first_rejects_contradictory_disposition_before_extra_revision(
    tmp_path, line
):
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Revised plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "### Same-plan follow-ups\n- Add the carry-forward orchestration test.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
            "Plan looks sound now."
            + prior_plan_item_dispositions(line)
            + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, reviewer="codex", max_rounds=3)

    with pytest.raises(AgentLoopError, match="use `resolved` when nothing remains"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)

    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(claude_calls) == 2


def test_issue_loop_plan_first_plan_only_does_not_publish_approved_future_followups(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Revised plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "### Same-plan follow-ups\n- Tighten the prompt wording.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
            "Looks good."
            + prior_plan_item_dispositions("[item-1] future follow-up: document parser helper reuse separately")
            + "\n### Future follow-ups\n- Add a later cleanup to dedupe shared prompt rendering.\n"
            + "<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert runner.issues == []
    summary = runner.comments[-1]
    assert summary.startswith("Planning complete for issue #56.")
    assert "Approved plan future follow-ups:" in summary
    assert "document parser helper reuse separately" in summary
    assert "Add a later cleanup to dedupe shared prompt rendering." in summary
    assert "not carried into PR review" in summary
    assert "not PR prior review items" in summary
    assert "Filed future follow-up issues:" not in summary
    assert "<!-- AGENT_PLAN_APPROVED_FOLLOWUPS:" in summary
    assert "mode=summarize" in summary


def test_issue_loop_plan_first_decompose_only_summarizes_instead_of_filing_plan_followups(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Split the implementation into phases.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            plan_decomposition_json(
                {
                    "title": "Schema helpers",
                    "scope": "Add parser dataclasses and tests.",
                    "non_goals": "No live orchestrator switch.",
                    "dependency_notes": "First phase; no dependencies.",
                    "rollout_risk": "low - internal only.",
                    "validation": "Run python -m pytest tests/test_agent_loop.py.",
                    "parent_context": "Approved plan slice: add schema helpers and preserve behavior.",
                    "automation": "agent-pr",
                    "depends_on": [],
                }
            ),
        ],
        codex_outputs=[
            structured_plan_review(
                state="approved",
                future_followups=["Add a later cleanup to dedupe shared prompt rendering."],
            ),
        ],
        issue_urls=["https://github.com/OWNER/REPO/issues/101"],
    )
    config = make_config(
        tmp_path,
        approved_followups="issue",
        plan_execution_mode="decompose-only",
    )

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert len(runner.issues) == 1
    assert runner.issues[0]["title"] == "Phase 1: Schema helpers (from #56)"
    assert not any(
        issue["title"].startswith("Follow up future plan-review note:")
        for issue in runner.issues
    )
    planning_summary = runner.comments[2]
    assert planning_summary.startswith("Planning complete for issue #56.")
    assert "Approved plan future follow-ups:" in planning_summary
    assert "Add a later cleanup to dedupe shared prompt rendering." in planning_summary
    assert "Filed future follow-up issues:" not in planning_summary
    assert "mode=summarize" in planning_summary
    assert "mode=issue" not in planning_summary


def test_issue_loop_plan_first_files_approved_future_followups_before_implementation(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Make the change.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Implemented approved plan.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            structured_plan_review(
                state="approved",
                future_followups=["Add a later cleanup to dedupe shared prompt rendering."],
            ),
            structured_pr_review(state="approved", summary="LGTM."),
        ],
        issue_urls=["https://github.com/OWNER/REPO/issues/99"],
    )
    config = make_config(tmp_path, approved_followups="issue")

    assert (
        run_issue_loop(
            runner,
            issue_number=56,
            config=config,
            plan_first=True,
            implement_after_approval=True,
        )
        == 0
    )

    assert len(runner.issues) == 1
    assert runner.issues[0]["title"] == (
        "Follow up future plan-review note: Add a later cleanup to dedupe shared prompt rendering."
    )
    issue_body = runner.issues[0]["body"]
    assert "Parent issue: #56" in issue_body
    assert "Approved plan hash:" in issue_body
    assert "Planning round(s): 1" in issue_body
    assert "Original plan item ID(s): item-1" in issue_body
    assert "Codex" in issue_body
    assert "outside the current implementation scope" in issue_body
    assert "not a PR-review prior item" in issue_body
    summary = runner.comments[2]
    assert summary.startswith("Planning complete for issue #56.")
    assert "Filed future follow-up issues:" in summary
    assert "https://github.com/OWNER/REPO/issues/99" in summary
    assert "Approved plan future follow-ups:" not in summary
    assert "<!-- AGENT_PLAN_APPROVED_FOLLOWUPS:" in summary

    issue_create_index = command_index(runner.commands, ["gh", "issue", "create"])
    second_claude_index = command_index(
        runner.commands,
        ["claude", "--print"],
        start=command_index(runner.commands, ["claude", "--print"]) + 1,
    )
    assert issue_create_index < second_claude_index


def test_issue_loop_plan_first_files_surviving_future_from_mixed_outcome_round(tmp_path):
    future_text = "Factor the shared follow-up guidance into a reusable helper."
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Make the change.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            structured_plan_revision(
                summary="Address the blocking test gap.",
                prior_plan_item_dispositions=[
                    {"item_id": "item-2", "disposition": "resolved", "note": "Added the test."}
                ],
                plan_steps=["Make the change.", "Add the missing regression test."],
            ),
            "Implemented approved plan.\n<!-- AGENT_PR: 77 -->\n"
            "<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            structured_plan_review(
                state="approved",
                future_followups=[future_text],
            ),
            structured_plan_review(
                state="approved",
                prior_plan_item_dispositions=[
                    {
                        "item_id": "item-1",
                        "disposition": "future",
                        "note": "Keep this as confirmed post-plan cleanup.",
                    },
                    {"item_id": "item-2", "disposition": "resolved"},
                ],
            ),
            structured_pr_review(state="approved", summary="LGTM."),
        ],
        gemini_outputs=[
            structured_plan_review(
                state="blocking",
                blocking_plan_issues=["Add a regression test for the plan-review ledger."],
                reviewer="Google Gemini",
            ),
            structured_plan_review(
                state="approved",
                future_followups=[future_text],
                prior_plan_item_dispositions=[
                    {"item_id": "item-1", "disposition": "future"},
                    {"item_id": "item-2", "disposition": "resolved"},
                ],
                reviewer="Google Gemini",
            ),
            structured_pr_review(
                state="approved",
                summary="LGTM.",
                reviewer="Google Gemini",
            ),
        ],
        issue_urls=["https://github.com/OWNER/REPO/issues/99"],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "gemini"),
        approved_followups="issue",
        max_rounds=2,
    )

    assert (
        run_issue_loop(
            runner,
            issue_number=56,
            config=config,
            plan_first=True,
            implement_after_approval=True,
        )
        == 0
    )

    coder_revision_prompt = next(
        cmd[-1]
        for cmd, _cwd in runner.commands
        if cmd[:1] == ["claude"] and '"kind": "plan_revision"' in cmd[-1]
    )
    assert "item-2" in coder_revision_prompt
    assert "item-1" not in coder_revision_prompt
    assert future_text not in coder_revision_prompt

    round_two_reviewer_prompts = [
        cmd[-1]
        for cmd, _cwd in runner.commands
        if "Planning round: 2" in cmd[-1] and "Role: reviewer" in cmd[-1]
    ]
    assert len(round_two_reviewer_prompts) == 2
    assert all("item-1" in prompt for prompt in round_two_reviewer_prompts)

    assert len(runner.issues) == 1
    issue_body = runner.issues[0]["body"]
    assert "Planning round(s): 1, 2" in issue_body
    assert "Reviewers: Codex, Gemini" in issue_body
    assert "Original plan item ID(s): item-1, item-3" in issue_body
    assert "Keep this as confirmed post-plan cleanup." in issue_body
    assert any(
        "Reconciliation: 1 filed, 1 deduplicated, 0 skipped by cap." in comment
        for comment in runner.comments
    )

    issue_create_index = command_index(runner.commands, ["gh", "issue", "create"])
    round_two_review_indexes = [
        index
        for index, (cmd, _cwd) in enumerate(runner.commands)
        if "Planning round: 2" in cmd[-1] and "Role: reviewer" in cmd[-1]
    ]
    assert issue_create_index > max(round_two_review_indexes)


@pytest.mark.parametrize("later_disposition", ["resolved", "same-plan", "blocking"])
def test_issue_loop_plan_first_does_not_file_future_item_after_later_lifecycle_change(
    tmp_path, later_disposition
):
    future_text = "Extract the shared plan-review formatting helper."
    promoted = later_disposition in {"same-plan", "blocking"}
    promotion_state = "blocking" if promoted else "approved"
    final_plan_dispositions = [
        {"item_id": "item-1", "disposition": later_disposition},
        {"item_id": "item-2", "disposition": "resolved"},
    ]
    claude_outputs = [
        "Plan:\n- Make the change.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        structured_plan_revision(
            summary="Address the original blocker.",
            prior_plan_item_dispositions=[{"item_id": "item-2", "disposition": "resolved"}],
        ),
    ]
    if promoted:
        claude_outputs.append(
            structured_plan_revision(
                summary="Address the promoted future item.",
                prior_plan_item_dispositions=[
                    {"item_id": "item-1", "disposition": "resolved"}
                ],
            )
        )
    claude_outputs.append(
        "Implemented approved plan.\n<!-- AGENT_PR: 77 -->\n"
        "<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
    )

    def reviewer_outputs(reviewer):
        outputs = [
            structured_plan_review(
                state="approved",
                future_followups=[future_text],
                reviewer=reviewer,
            ),
            structured_plan_review(
                state=promotion_state,
                prior_plan_item_dispositions=final_plan_dispositions,
                reviewer=reviewer,
            ),
        ]
        if promoted:
            outputs.append(
                structured_plan_review(
                    state="approved",
                    prior_plan_item_dispositions=[
                        {"item_id": "item-1", "disposition": "resolved"}
                    ],
                    reviewer=reviewer,
                )
            )
        outputs.append(structured_pr_review(state="approved", reviewer=reviewer))
        return outputs

    runner = FakeRunner(
        claude_outputs=claude_outputs,
        codex_outputs=reviewer_outputs("OpenAI Codex"),
        gemini_outputs=[
            structured_plan_review(
                state="blocking",
                blocking_plan_issues=["Add the initial ledger regression."],
                reviewer="Google Gemini",
            ),
            *reviewer_outputs("Google Gemini")[1:],
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "gemini"),
        approved_followups="issue",
        max_rounds=3,
    )

    assert (
        run_issue_loop(
            runner,
            issue_number=56,
            config=config,
            plan_first=True,
            implement_after_approval=True,
        )
        == 0
    )

    assert runner.issues == []
    assert not any(future_text in comment for comment in runner.comments[-2:])
    if promoted:
        coder_revision_prompts = [
            cmd[-1]
            for cmd, _cwd in runner.commands
            if cmd[:1] == ["claude"] and '"kind": "plan_revision"' in cmd[-1]
        ]
        assert len(coder_revision_prompts) == 2
        assert "item-1" in coder_revision_prompts[1]
        assert future_text in coder_revision_prompts[1]
        final_round_review_indexes = [
            index
            for index, (cmd, _cwd) in enumerate(runner.commands)
            if "Planning round: 3" in cmd[-1] and "Role: reviewer" in cmd[-1]
        ]
        implementation_index = next(
            index
            for index, (cmd, _cwd) in enumerate(runner.commands)
            if cmd[:1] == ["claude"] and "Implement the approved plan" in cmd[-1]
        )
        assert implementation_index > max(final_round_review_indexes)


def test_issue_loop_plan_first_ignore_mode_keeps_pr_prior_ledger_clean(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Make the change.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Implemented approved plan.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            structured_plan_review(
                state="approved",
                future_followups=["Track a separate planning cleanup later."],
            ),
            structured_pr_review(state="approved", summary="LGTM."),
        ],
    )
    config = make_config(tmp_path, approved_followups="ignore")

    assert (
        run_issue_loop(
            runner,
            issue_number=56,
            config=config,
            plan_first=True,
            implement_after_approval=True,
        )
        == 0
    )

    assert runner.issues == []
    planning_summary = runner.comments[2]
    assert "Approved plan future follow-ups:" in planning_summary
    assert "Track a separate planning cleanup later." in planning_summary
    assert "not carried into PR review" in planning_summary
    pr_review_prompt = [
        cmd[-1]
        for cmd, _cwd in runner.commands
        if cmd[:2] == ["codex", "exec"] and '"kind": "pr_review"' in cmd[-1]
    ][0]
    assert "Only items listed under `Prior unresolved review items from earlier rounds`" in pr_review_prompt
    assert "Track a separate planning cleanup later." not in pr_review_prompt
    assert "planning-stage `item-*` IDs and approved\nplan future follow-ups" in pr_review_prompt
    assert "prior_plan_item_dispositions" in pr_review_prompt


def test_issue_loop_plan_first_deduplicates_plan_followup_issues_across_reviewers(tmp_path):
    runner = FakeRunner(
        gemini_outputs=[
            "Plan:\n- Make the change.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Google Gemini",
            "Implemented approved plan.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini",
        ],
        codex_outputs=[
            structured_plan_review(
                state="approved",
                future_followups=[
                    "**Remote validation**: Validate explicit workdir git remotes against the target repo.",
                ],
            ),
            structured_pr_review(state="approved", summary="LGTM."),
        ],
        claude_outputs=[
            structured_plan_review(
                state="approved",
                future_followups=[
                    "**Remote validation**: Validate explicit workdir git remotes against the target repo.",
                ],
                reviewer="Anthropic Claude",
            ),
            structured_pr_review(state="approved", summary="LGTM.", reviewer="Anthropic Claude"),
        ],
        issue_urls=["https://github.com/OWNER/REPO/issues/99"],
    )
    config = make_config(
        tmp_path,
        coder="gemini",
        reviewer=("codex", "claude"),
        approved_followups="issue",
    )

    assert (
        run_issue_loop(
            runner,
            issue_number=56,
            config=config,
            plan_first=True,
            implement_after_approval=True,
        )
        == 0
    )

    assert len(runner.issues) == 1
    body = runner.issues[0]["body"]
    assert "Reviewers: Codex, Claude" in body
    assert "Original plan item ID(s): item-1, item-2" in body
    assert body.count("**Remote validation**") == 3
    assert any(
        "Reconciliation: 1 filed, 1 deduplicated, 0 skipped by cap." in comment
        for comment in runner.comments
    )


def test_issue_loop_plan_first_plan_followup_marker_prevents_duplicate_issue_creation(tmp_path):
    plan = "Plan:\n- Make the change.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    plan_hash = approved_plan_hash(plan)
    future_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="Codex",
        source_round=1,
        text="Add a later cleanup to dedupe shared prompt rendering.",
        status="future",
        source_status="future",
    )
    runner = FakeRunner(
        claude_outputs=[
            "Implemented approved plan.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            structured_pr_review(state="approved", summary="LGTM."),
        ],
        issue_comments=[
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:00Z",
                "body": _attach_round_metadata(
                    plan,
                    PostedRoundMetadata(
                        flow="plan",
                        role="coder",
                        agent="Claude",
                        round_number=1,
                        subject=_plan_subject(plan),
                    ),
                ),
            },
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:01Z",
                "body": _attach_round_metadata(
                    structured_plan_review(
                        state="approved",
                        future_followups=["Add a later cleanup to dedupe shared prompt rendering."],
                    ),
                    PostedRoundMetadata(
                        flow="plan",
                        role="reviewer",
                        agent="Codex",
                        round_number=1,
                        subject=_plan_subject(plan),
                        new_items=(future_item,),
                        state="approved",
                    ),
                ),
            },
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:02Z",
                "body": (
                    "Planning complete for issue #56.\n\n"
                    "<!-- AGENT_PLAN_APPROVED_FOLLOWUPS: "
                    f"issue=56 plan={plan_hash} mode=issue -->\n"
                    "-- coding-review-agent-loop"
                ),
            },
        ],
    )
    config = make_config(tmp_path, approved_followups="issue")

    assert (
        run_issue_loop(
            runner,
            issue_number=56,
            config=config,
            plan_first=True,
            implement_after_approval=True,
        )
        == 0
    )

    assert runner.issues == []
    assert not any(
        "Filed future follow-up issues:" in comment or "Approved plan future follow-ups:" in comment
        for comment in runner.comments
    )


def test_issue_loop_plan_first_decompose_only_creates_child_issues(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Add schema helpers.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            plan_decomposition_json(
                {
                    "title": "Schema helpers",
                    "scope": "Add parser dataclasses and tests.",
                    "non_goals": "No live orchestrator switch.",
                    "dependency_notes": "First phase; no dependencies.",
                    "rollout_risk": "low - internal only.",
                    "validation": "Run python -m pytest tests/test_agent_loop.py.",
                    "parent_context": "Approved plan slice: add schema helpers and preserve behavior.",
                    "automation": "agent-pr",
                    "depends_on": [],
                },
                {
                    "title": "Human rollout checkpoint",
                    "scope": "Human validates rollout readiness.",
                    "non_goals": "No code changes.",
                    "dependency_notes": "Depends on Schema helpers.",
                    "rollout_risk": "medium - manual checkpoint.",
                    "validation": "Human must add a remark and close the issue.",
                    "parent_context": "Approved plan slice: stop for human validation.",
                    "automation": "manual-close",
                    "depends_on": ["Schema helpers"],
                },
            ),
        ],
        codex_outputs=[
            "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
        issue_urls=[
            "https://github.com/OWNER/REPO/issues/101",
            "https://github.com/OWNER/REPO/issues/102",
        ],
    )
    config = make_config(tmp_path, plan_execution_mode="decompose-only")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert len(runner.issues) == 2
    assert runner.issues[0]["title"] == "Phase 1: Schema helpers (from #56)"
    assert "Run `agent-loop issue <this issue number>`" in runner.issues[0]["body"]
    assert "Approved plan slice: add schema helpers" in runner.issues[0]["body"]
    assert runner.issues[1]["title"] == "[Human] Phase 2: Human rollout checkpoint (from #56)"
    assert "depends on #101: Schema helpers" in runner.issues[1]["body"]
    assert "human should add the required remark/update and close this issue" in runner.issues[1]["body"]
    summary = runner.comments[-1]
    assert summary.startswith("Approved plan decomposed for issue #56.")
    assert "Every phase above has a GitHub child issue" in summary
    assert "<!-- AGENT_PLAN_DECOMPOSITION:" in summary
    assert not any(cmd[:3] == ["gh", "pr", "view"] for cmd, _cwd in runner.commands)


def test_issue_loop_plan_first_decompose_only_is_idempotent(tmp_path):
    plan = "Plan:\n- Add schema helpers.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    summary = format_decomposition_parent_summary(
        parent_issue=56,
        mode="decompose-only",
        plan_hash=approved_plan_hash(plan),
        created=(),
    )
    runner = FakeRunner(
        issue_comments=[
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:00Z",
                "body": _attach_round_metadata(
                    plan,
                    PostedRoundMetadata(
                        flow="plan",
                        role="coder",
                        agent="Claude",
                        round_number=1,
                        subject=_plan_subject(plan),
                    ),
                ),
            },
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:01Z",
                "body": _attach_round_metadata(
                    "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
                    PostedRoundMetadata(
                        flow="plan",
                        role="reviewer",
                        agent="Codex",
                        round_number=1,
                        subject=_plan_subject(plan),
                        state="approved",
                    ),
                ),
            },
            {"author": {"login": "bot"}, "createdAt": "2026-05-23T00:00:02Z", "body": summary},
        ],
    )
    config = make_config(tmp_path, plan_execution_mode="decompose-only")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert runner.issues == []
    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


def test_issue_loop_plan_first_implement_by_phase_rerun_without_handoff_implements_once(tmp_path):
    plan = "Plan:\n- Add schema helpers.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    child = CreatedPhaseIssue(
        phase=RecordedPhase(title="Schema helpers", automation="agent-pr"),
        issue_url="https://github.com/OWNER/REPO/issues/99",
        issue_number=99,
    )
    summary = format_decomposition_parent_summary(
        parent_issue=56,
        mode="implement-by-phase",
        plan_hash=approved_plan_hash(plan),
        created=(child,),
    )
    runner = FakeRunner(
        claude_outputs=[
            "Implemented first phase.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        issue_comments=[
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:00Z",
                "body": _attach_round_metadata(
                    plan,
                    PostedRoundMetadata(
                        flow="plan",
                        role="coder",
                        agent="Claude",
                        round_number=1,
                        subject=_plan_subject(plan),
                    ),
                ),
            },
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:01Z",
                "body": _attach_round_metadata(
                    "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
                    PostedRoundMetadata(
                        flow="plan",
                        role="reviewer",
                        agent="Codex",
                        round_number=1,
                        subject=_plan_subject(plan),
                        state="approved",
                    ),
                ),
            },
            {"author": {"login": "bot"}, "createdAt": "2026-05-23T00:00:02Z", "body": summary},
        ],
        pr_payload={"body": "Fixes #99"},
    )
    config = make_config(tmp_path, plan_execution_mode="implement-by-phase")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert runner.issues == []
    assert any("<!-- AGENT_PLAN_PHASE_IMPLEMENTATION:" in comment for comment in runner.comments)
    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(claude_calls) == 1
    assert "GitHub issue #99" in claude_calls[0][-1]


def test_issue_loop_plan_first_implement_by_phase_rerun_with_handoff_stops(tmp_path, capsys):
    plan = "Plan:\n- Add schema helpers.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    child = CreatedPhaseIssue(
        phase=RecordedPhase(title="Schema helpers", automation="agent-pr"),
        issue_url="https://github.com/OWNER/REPO/issues/99",
        issue_number=99,
    )
    summary = format_decomposition_parent_summary(
        parent_issue=56,
        mode="implement-by-phase",
        plan_hash=approved_plan_hash(plan),
        created=(child,),
    )
    handoff = format_phase_implementation_handoff_comment(
        parent_issue=56,
        mode="implement-by-phase",
        plan_hash=approved_plan_hash(plan),
        phase_index=1,
        created=child,
    )
    runner = FakeRunner(
        issue_comments=[
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:00Z",
                "body": _attach_round_metadata(
                    plan,
                    PostedRoundMetadata(
                        flow="plan",
                        role="coder",
                        agent="Claude",
                        round_number=1,
                        subject=_plan_subject(plan),
                    ),
                ),
            },
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:01Z",
                "body": _attach_round_metadata(
                    "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
                    PostedRoundMetadata(
                        flow="plan",
                        role="reviewer",
                        agent="Codex",
                        round_number=1,
                        subject=_plan_subject(plan),
                        state="approved",
                    ),
                ),
            },
            {"author": {"login": "bot"}, "createdAt": "2026-05-23T00:00:02Z", "body": summary},
            {"author": {"login": "bot"}, "createdAt": "2026-05-23T00:00:03Z", "body": handoff},
        ],
    )
    config = make_config(tmp_path, plan_execution_mode="implement-by-phase")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    output = capsys.readouterr().out
    assert "already handed off to child issue #99" in output
    assert "agent-loop issue 99" in output
    assert runner.issues == []
    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)
    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


def test_issue_loop_plan_first_implement_by_phase_human_first_rerun_does_not_handoff(tmp_path):
    plan = "Plan:\n- Validate migration manually first.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    child = CreatedPhaseIssue(
        phase=RecordedPhase(title="Manual readiness check", automation="human-action"),
        issue_url="https://github.com/OWNER/REPO/issues/99",
        issue_number=99,
    )
    summary = format_decomposition_parent_summary(
        parent_issue=56,
        mode="implement-by-phase",
        plan_hash=approved_plan_hash(plan),
        created=(child,),
    )
    runner = FakeRunner(
        issue_comments=[
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:00Z",
                "body": _attach_round_metadata(
                    plan,
                    PostedRoundMetadata(
                        flow="plan",
                        role="coder",
                        agent="Claude",
                        round_number=1,
                        subject=_plan_subject(plan),
                    ),
                ),
            },
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:01Z",
                "body": _attach_round_metadata(
                    "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
                    PostedRoundMetadata(
                        flow="plan",
                        role="reviewer",
                        agent="Codex",
                        round_number=1,
                        subject=_plan_subject(plan),
                        state="approved",
                    ),
                ),
            },
            {"author": {"login": "bot"}, "createdAt": "2026-05-23T00:00:02Z", "body": summary},
        ],
    )
    config = make_config(tmp_path, plan_execution_mode="implement-by-phase")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert runner.issues == []
    assert not any("<!-- AGENT_PLAN_PHASE_IMPLEMENTATION:" in comment for comment in runner.comments)
    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


def test_issue_loop_plan_first_implement_by_phase_stops_on_human_first_phase(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Validate migration manually first.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            plan_decomposition_json(
                {
                    "title": "Manual readiness check",
                    "scope": "Human validates external readiness.",
                    "non_goals": "No agent PR.",
                    "dependency_notes": "First phase; no dependencies.",
                    "rollout_risk": "medium - manual readiness gate.",
                    "validation": "Human remark and closure required.",
                    "parent_context": "Approved plan slice: manual readiness gate.",
                    "automation": "human-action",
                    "depends_on": [],
                }
            ),
        ],
        codex_outputs=["Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex"],
    )
    config = make_config(tmp_path, plan_execution_mode="implement-by-phase")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert len(runner.issues) == 1
    assert runner.issues[0]["title"].startswith("[Human] Phase 1")
    assert not any("<!-- AGENT_PLAN_PHASE_IMPLEMENTATION:" in comment for comment in runner.comments)
    assert not any(cmd[:3] == ["gh", "pr", "view"] for cmd, _cwd in runner.commands)


def test_issue_loop_plan_first_implement_by_phase_implements_first_agent_phase(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Add schema helpers.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            plan_decomposition_json(),
            "Implemented first phase.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        issue_urls=["https://github.com/OWNER/REPO/issues/99"],
        pr_payload={"body": "Fixes #99"},
    )
    config = make_config(tmp_path, plan_execution_mode="implement-by-phase")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert len(runner.issues) == 1
    decomposition_index = next(
        index for index, comment in enumerate(runner.comments) if "<!-- AGENT_PLAN_DECOMPOSITION:" in comment
    )
    handoff_index = next(
        index for index, comment in enumerate(runner.comments) if "<!-- AGENT_PLAN_PHASE_IMPLEMENTATION:" in comment
    )
    implementation_index = next(
        index for index, comment in enumerate(runner.comments) if comment.startswith("Implemented first phase.")
    )
    assert decomposition_index < handoff_index < implementation_index
    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(claude_calls) == 3
    assert "GitHub issue #99" in claude_calls[2][-1]
    assert "Approved implementation plan" in claude_calls[2][-1]


def test_issue_loop_plan_first_implement_by_phase_missing_child_number_does_not_handoff(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Add schema helpers.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            plan_decomposition_json(),
        ],
        codex_outputs=[
            "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
        issue_urls=[None],
    )
    config = make_config(tmp_path, plan_execution_mode="implement-by-phase")

    with pytest.raises(AgentLoopError, match="child issue number"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)

    assert not any("<!-- AGENT_PLAN_PHASE_IMPLEMENTATION:" in comment for comment in runner.comments)


def test_phase_implementation_handoff_rejects_malformed_marker():
    comment = IssueComment(
        author="bot",
        created_at="2026-05-23T00:00:00Z",
        body="<!-- AGENT_PLAN_PHASE_IMPLEMENTATION: not-valid-base64 -->",
    )

    with pytest.raises(AgentLoopError, match="Invalid AGENT_PLAN_PHASE_IMPLEMENTATION payload"):
        find_existing_phase_implementation_handoff(
            (comment,),
            parent_issue=56,
            plan_hash="abc123",
            mode="implement-by-phase",
            phase_index=1,
            child_issue_number=99,
        )


def test_issue_loop_plan_first_keeps_blocking_review_when_future_followups_are_misclassified(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Revised plan with focused tests.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Still blocked.\n\n"
            "### Blocking plan issues\n"
            "- Add parser coverage for blocking reviews with stray future follow-ups.\n\n"
            "### Same-plan follow-ups\n"
            "- Tighten the plan-review prompt wording.\n\n"
            "### Future follow-ups\n"
            "- Consider a later prompt dedupe cleanup.\n\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n"
            "-- OpenAI Codex",
            "Plan looks sound."
            + prior_plan_item_dispositions("[item-1] resolved", "[item-2] resolved")
            + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert "Add parser coverage for blocking reviews with stray future follow-ups." in claude_calls[1][-1]
    assert "Tighten the plan-review prompt wording." in claude_calls[1][-1]
    assert runner.comments[1].startswith("**Review verdict:** Blocking\n\nStill blocked.")
    assert "### Future follow-ups" not in runner.comments[1]


def test_issue_loop_plan_first_can_implement_after_approval(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Make the change.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Implemented approved plan.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)

    assert (
        run_issue_loop(
            runner,
            issue_number=56,
            config=config,
            plan_first=True,
            implement_after_approval=True,
        )
        == 0
    )

    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(claude_calls) == 2
    assert "Approved implementation plan" in claude_calls[1][-1]
    assert "include `Fixes #56` or another direct reference to issue #56" in claude_calls[1][-1]
    first_claude_index = command_index(runner.commands, ["claude", "--print"])
    fetch_index = command_index(runner.commands, ["git", "fetch", "origin"])
    switch_index = command_index(runner.commands, ["git", "switch", "main"])
    second_claude_index = command_index(runner.commands, ["claude", "--print"], start=first_claude_index + 1)
    assert first_claude_index < fetch_index < switch_index < second_claude_index
    assert len(runner.comments) == 6
    assert "<!-- AGENT_PLAN_ONE_SHOT_IMPL:" in runner.comments[3]
    assert runner.comments[4].startswith("Implemented approved plan.")
    assert runner.comments[5].startswith("**Review verdict:** Approved\n\nLGTM.")


def test_issue_loop_rejects_pr_without_issue_reference_in_body(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Fixed issue.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        pr_payload={
            "number": 77,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/77",
            "body": "Summary only.",
        },
    )
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="does not reference issue #56") as excinfo:
        run_issue_loop(runner, issue_number=56, config=config)

    assert "Edit the PR description on GitHub" in str(excinfo.value)
    assert "rerun the orchestrator as `agent-loop pr 77` to continue the review" in str(excinfo.value)


def test_issue_loop_plan_first_implementation_rejects_pr_without_issue_reference_in_body(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Make the change.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Implemented approved plan.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
        pr_payload={
            "number": 77,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/77",
            "body": "Summary only.",
        },
    )
    config = make_config(tmp_path, reviewer=("codex",))

    with pytest.raises(AgentLoopError, match="does not reference issue #56") as excinfo:
        run_issue_loop(
            runner,
            issue_number=56,
            config=config,
            plan_first=True,
            implement_after_approval=True,
        )

    assert "Edit the PR description on GitHub" in str(excinfo.value)
    assert "rerun the orchestrator as `agent-loop pr 77` to continue the review" in str(excinfo.value)


def test_issue_loop_plan_first_one_shot_posts_handoff_after_pr_creation(tmp_path):
    plan = "Plan:\n- Make the change.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    runner = FakeRunner(
        claude_outputs=[
            plan,
            "Implemented approved plan.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)

    assert (
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True, implement_after_approval=True)
        == 0
    )

    handoff_comments = [c for c in runner.comments if "<!-- AGENT_PLAN_ONE_SHOT_IMPL:" in c]
    assert len(handoff_comments) == 1
    assert f"Plan hash: {approved_plan_hash(plan)}" in handoff_comments[0]
    assert "Plan subject:" in handoff_comments[0]
    assert "PR #77" in handoff_comments[0]


def test_issue_loop_plan_first_one_shot_rerun_resumes_pr_loop(tmp_path):
    plan = "Plan:\n- Make the change.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    handoff = format_one_shot_impl_handoff_comment(
        parent_issue=56,
        mode="implement-one-shot",
        plan_hash=approved_plan_hash(plan),
        plan_subject=_plan_subject(plan),
        pr_number=77,
        pr_head_sha="abc123",
    )
    runner = FakeRunner(
        issue_comments=[
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:00Z",
                "body": _attach_round_metadata(
                    plan,
                    PostedRoundMetadata(
                        flow="plan",
                        role="coder",
                        agent="Claude",
                        round_number=1,
                        subject=_plan_subject(plan),
                    ),
                ),
            },
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:01Z",
                "body": _attach_round_metadata(
                    "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
                    PostedRoundMetadata(
                        flow="plan",
                        role="reviewer",
                        agent="Codex",
                        round_number=1,
                        subject=_plan_subject(plan),
                        state="approved",
                    ),
                ),
            },
            {"author": {"login": "bot"}, "createdAt": "2026-05-23T00:00:02Z", "body": handoff},
        ],
        codex_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True, implement_after_approval=True) == 0

    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(claude_calls) == 0
    assert any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


def test_issue_loop_plan_first_one_shot_rerun_with_closed_pr_stops(tmp_path, capsys):
    plan = "Plan:\n- Make the change.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    handoff = format_one_shot_impl_handoff_comment(
        parent_issue=56,
        mode="implement-one-shot",
        plan_hash=approved_plan_hash(plan),
        plan_subject=_plan_subject(plan),
        pr_number=77,
        pr_head_sha="abc123",
    )
    runner = FakeRunner(
        issue_comments=[
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:00Z",
                "body": _attach_round_metadata(
                    plan,
                    PostedRoundMetadata(
                        flow="plan",
                        role="coder",
                        agent="Claude",
                        round_number=1,
                        subject=_plan_subject(plan),
                    ),
                ),
            },
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:01Z",
                "body": _attach_round_metadata(
                    "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
                    PostedRoundMetadata(
                        flow="plan",
                        role="reviewer",
                        agent="Codex",
                        round_number=1,
                        subject=_plan_subject(plan),
                        state="approved",
                    ),
                ),
            },
            {"author": {"login": "bot"}, "createdAt": "2026-05-23T00:00:02Z", "body": handoff},
        ],
        pr_payload={"state": "CLOSED"},
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True, implement_after_approval=True) == 0

    output = capsys.readouterr().out
    assert "PR #77" in output
    assert "closed" in output
    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)
    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


def test_issue_loop_plan_first_one_shot_rerun_hash_mismatch_reimplements(tmp_path):
    plan = "Plan:\n- Make the change.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    old_plan = "Plan:\n- Old approach that was replaced.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    old_handoff = format_one_shot_impl_handoff_comment(
        parent_issue=56,
        mode="implement-one-shot",
        plan_hash=approved_plan_hash(old_plan),
        plan_subject=_plan_subject(old_plan),
        pr_number=99,
        pr_head_sha=None,
    )
    runner = FakeRunner(
        issue_comments=[
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:00Z",
                "body": _attach_round_metadata(
                    plan,
                    PostedRoundMetadata(
                        flow="plan",
                        role="coder",
                        agent="Claude",
                        round_number=1,
                        subject=_plan_subject(plan),
                    ),
                ),
            },
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:01Z",
                "body": _attach_round_metadata(
                    "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
                    PostedRoundMetadata(
                        flow="plan",
                        role="reviewer",
                        agent="Codex",
                        round_number=1,
                        subject=_plan_subject(plan),
                        state="approved",
                    ),
                ),
            },
            {"author": {"login": "bot"}, "createdAt": "2026-05-23T00:00:02Z", "body": old_handoff},
        ],
        claude_outputs=[
            "Implemented approved plan.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True, implement_after_approval=True) == 0

    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(claude_calls) == 1
    assert "Approved implementation plan" in claude_calls[0][-1]


def test_issue_loop_plan_first_one_shot_rerun_pr_missing_issue_reference(tmp_path):
    plan = "Plan:\n- Make the change.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    handoff = format_one_shot_impl_handoff_comment(
        parent_issue=56,
        mode="implement-one-shot",
        plan_hash=approved_plan_hash(plan),
        plan_subject=_plan_subject(plan),
        pr_number=77,
        pr_head_sha="abc123",
    )
    runner = FakeRunner(
        issue_comments=[
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:00Z",
                "body": _attach_round_metadata(
                    plan,
                    PostedRoundMetadata(
                        flow="plan",
                        role="coder",
                        agent="Claude",
                        round_number=1,
                        subject=_plan_subject(plan),
                    ),
                ),
            },
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:01Z",
                "body": _attach_round_metadata(
                    "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
                    PostedRoundMetadata(
                        flow="plan",
                        role="reviewer",
                        agent="Codex",
                        round_number=1,
                        subject=_plan_subject(plan),
                        state="approved",
                    ),
                ),
            },
            {"author": {"login": "bot"}, "createdAt": "2026-05-23T00:00:02Z", "body": handoff},
        ],
        pr_payload={
            "number": 77,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/77",
            "body": "No issue reference here.",
        },
    )
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="does not reference issue #56") as excinfo:
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True, implement_after_approval=True)

    assert "Edit the PR description on GitHub" in str(excinfo.value)
    assert "rerun the orchestrator as `agent-loop pr 77` to continue the review" in str(excinfo.value)
    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


def test_is_clarification_request_detects_marker():
    assert is_clarification_request("need more info\n<!-- AGENT_CLARIFY -->")
    assert is_clarification_request("<!-- agent_clarify -->")
    assert not is_clarification_request("done\n<!-- AGENT_STATE: blocking -->")


def test_is_clarification_request_state_marker_after_clarify_takes_precedence():
    # AGENT_PLAN_STATE after inline AGENT_CLARIFY example: issue #216 / #278 shape.
    # Inline (non-standalone) AGENT_CLARIFY never triggers, regardless of state markers.
    plan_with_embedded_clarify = (
        "Here is my plan.\n\n"
        "If I needed clarification I would emit <!-- AGENT_CLARIFY --> as a marker.\n\n"
        "But I have enough information, so here is the full plan:\n\n"
        "1. Do step one\n2. Do step two\n\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    assert not is_clarification_request(plan_with_embedded_clarify)

    # Inline AGENT_CLARIFY without any state marker: still not clarification.
    inline_only = (
        "The protocol supports <!-- AGENT_CLARIFY --> for clarification requests.\n\n"
        "Here is my fix."
    )
    assert not is_clarification_request(inline_only)

    # AGENT_STATE after inline AGENT_CLARIFY example: PR/coder blocking response.
    pr_response_with_embedded_clarify = (
        "The protocol supports <!-- AGENT_CLARIFY --> for clarification requests.\n\n"
        "Here is my fix.\n\n"
        "<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
    )
    assert not is_clarification_request(pr_response_with_embedded_clarify)

    # AGENT_PR after inline AGENT_CLARIFY example: coder PR-creation response.
    pr_created_with_embedded_clarify = (
        "Use <!-- AGENT_CLARIFY --> if you need more info.\n\n"
        "Implemented the fix.\n\n"
        "<!-- AGENT_PR: 42 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
    )
    assert not is_clarification_request(pr_created_with_embedded_clarify)

    # PR URL after inline AGENT_CLARIFY: treated as final state marker.
    pr_url_with_embedded_clarify = (
        "Use <!-- AGENT_CLARIFY --> for questions.\n\n"
        "See https://github.com/OWNER/REPO/pull/99 for the PR."
    )
    assert not is_clarification_request(pr_url_with_embedded_clarify)

    # Real clarification request: standalone AGENT_CLARIFY is the final marker.
    real_clarify = "Which endpoint should I use?\n<!-- AGENT_CLARIFY -->\n-- Anthropic Claude"
    assert is_clarification_request(real_clarify)

    # Standalone AGENT_CLARIFY on its own line, after a state marker in prose.
    clarify_after_state = (
        "Round 1 ended with <!-- AGENT_STATE: blocking -->, but I still need more info.\n"
        "<!-- AGENT_CLARIFY -->"
    )
    assert is_clarification_request(clarify_after_state)

    # Standalone AGENT_CLARIFY on its own line, appearing after AGENT_PLAN_STATE in prose.
    plan_state_in_prose_clarify_last = (
        "The previous round used <!-- AGENT_PLAN_STATE: blocking --> to signal issues,\n"
        "but now I have a question:\n"
        "<!-- AGENT_CLARIFY -->"
    )
    assert is_clarification_request(plan_state_in_prose_clarify_last)


def test_is_clarification_request_standalone_marker_positional_semantics():
    # Standalone AGENT_PLAN_STATE footer appearing BEFORE a standalone AGENT_CLARIFY
    # does NOT suppress it — AGENT_CLARIFY is the final marker and wins.
    plan_footer_then_clarify_appendix = (
        "Plan content.\n\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n"
        "-- Anthropic Claude\n\n"
        "Appendix:\n"
        "<!-- AGENT_CLARIFY -->"
    )
    assert is_clarification_request(plan_footer_then_clarify_appendix)

    # Standalone AGENT_STATE appearing BEFORE AGENT_CLARIFY also does not suppress.
    state_then_clarify = (
        "<!-- AGENT_STATE: blocking -->\n\n"
        "Note:\n"
        "<!-- AGENT_CLARIFY -->"
    )
    assert is_clarification_request(state_then_clarify)

    # Standalone AGENT_STATE appearing AFTER AGENT_CLARIFY does suppress it.
    clarify_then_state = (
        "<!-- AGENT_CLARIFY -->\n"
        "<!-- AGENT_STATE: blocking -->"
    )
    assert not is_clarification_request(clarify_then_state)

    # Inline (non-standalone) AGENT_STATE in prose does NOT suppress AGENT_CLARIFY —
    # it may be a quoted reference to a previous round's state.
    inline_state_then_clarify = (
        "Round 1 ended with <!-- AGENT_STATE: blocking -->, but I still need more info.\n"
        "<!-- AGENT_CLARIFY -->"
    )
    assert is_clarification_request(inline_state_then_clarify)

    # Inline AGENT_PLAN_STATE in prose also does not suppress.
    inline_plan_state_then_clarify = (
        "The previous round used <!-- AGENT_PLAN_STATE: blocking --> to signal issues,\n"
        "but now I have a question:\n"
        "<!-- AGENT_CLARIFY -->"
    )
    assert is_clarification_request(inline_plan_state_then_clarify)


def test_is_clarification_request_pr_marker_takes_precedence():
    # AGENT_PR: N standalone marker appearing AFTER AGENT_CLARIFY suppresses it.
    pr_after_clarify = (
        "<!-- AGENT_CLARIFY -->\n"
        "Actually I have enough info.\n"
        "<!-- AGENT_PR: 55 -->"
    )
    assert not is_clarification_request(pr_after_clarify)

    # AGENT_PR: N standalone marker appearing BEFORE AGENT_CLARIFY does NOT suppress —
    # AGENT_CLARIFY is the final marker and wins.
    pr_before_clarify = (
        "<!-- AGENT_PR: 55 -->\n"
        "<!-- AGENT_STATE: blocking -->\n\n"
        "Note:\n"
        "<!-- AGENT_CLARIFY -->"
    )
    assert is_clarification_request(pr_before_clarify)


def test_is_clarification_request_ignores_fenced_code_block_examples():
    # AGENT_CLARIFY on its own line inside a backtick fence: not clarification.
    fenced_no_state = (
        "Here's how the marker looks:\n\n"
        "```\n"
        "<!-- AGENT_CLARIFY -->\n"
        "```\n\n"
        "That's all."
    )
    assert not is_clarification_request(fenced_no_state)

    # Fenced example with AGENT_PLAN_STATE after the block: still not clarification.
    fenced_with_plan_state = (
        "Protocol example:\n\n"
        "```\n"
        "<!-- AGENT_CLARIFY -->\n"
        "```\n\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    assert not is_clarification_request(fenced_with_plan_state)

    # Fenced example where the code block appears AFTER a state marker: not clarification.
    state_then_fenced = (
        "<!-- AGENT_PLAN_STATE: blocking -->\n\n"
        "Appendix:\n\n"
        "```\n"
        "<!-- AGENT_CLARIFY -->\n"
        "```"
    )
    assert not is_clarification_request(state_then_fenced)

    # Tilde fence also excluded.
    tilde_fenced = (
        "~~~\n"
        "<!-- AGENT_CLARIFY -->\n"
        "~~~"
    )
    assert not is_clarification_request(tilde_fenced)

    # Real standalone AGENT_CLARIFY outside a fence: still detected.
    outside_fence = (
        "```\n"
        "some code\n"
        "```\n\n"
        "<!-- AGENT_CLARIFY -->"
    )
    assert is_clarification_request(outside_fence)

    # AGENT_CLARIFY both inside and outside a fence: outside occurrence is active.
    inside_and_outside = (
        "```\n"
        "<!-- AGENT_CLARIFY -->\n"
        "```\n\n"
        "<!-- AGENT_CLARIFY -->"
    )
    assert is_clarification_request(inside_and_outside)


def test_is_clarification_request_requires_clarify_at_end():
    # Non-blank, non-signature content after AGENT_CLARIFY means it's embedded.
    embedded_with_trailing = (
        "<!-- AGENT_CLARIFY -->\n\n"
        "Some trailing prose that isn't a signature.\n"
        "<!-- AGENT_STATE: blocking -->"
    )
    # AGENT_STATE suppresses it via the presence-based check above.
    assert not is_clarification_request(embedded_with_trailing)

    # Standalone AGENT_CLARIFY with only blank lines after it: valid.
    clarify_then_blank = "<!-- AGENT_CLARIFY -->\n\n"
    assert is_clarification_request(clarify_then_blank)

    # Standalone AGENT_CLARIFY with only a signature after it: valid.
    clarify_then_sig = "<!-- AGENT_CLARIFY -->\n-- Anthropic Claude\n"
    assert is_clarification_request(clarify_then_sig)

    # Standalone AGENT_CLARIFY with real prose content after it (no state marker):
    # should NOT be treated as an active clarification.
    clarify_then_prose = (
        "<!-- AGENT_CLARIFY -->\n\n"
        "Continuing thoughts about the plan.\n"
    )
    assert not is_clarification_request(clarify_then_prose)

    # AGENT_CLARIFY in plan body with plan footer after: suppressed by state marker
    # (presence-based check catches it before trailing-content check).
    in_plan_body = (
        "Here are my questions:\n\n"
        "<!-- AGENT_CLARIFY -->\n\n"
        "More explanation here.\n\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n"
    )
    assert not is_clarification_request(in_plan_body)

    # Multiple AGENT_CLARIFY; last one has only signature trailing: valid.
    multi_clarify = (
        "First question set:\n<!-- AGENT_CLARIFY -->\n\nOther text.\n\n"
        "<!-- AGENT_CLARIFY -->\n-- Anthropic Claude\n"
    )
    assert is_clarification_request(multi_clarify)

    # Multiple AGENT_CLARIFY; last one has prose trailing: not valid.
    multi_clarify_bad = (
        "<!-- AGENT_CLARIFY -->\n\n"
        "<!-- AGENT_CLARIFY -->\n\n"
        "But wait, there's more content.\n"
    )
    assert not is_clarification_request(multi_clarify_bad)


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


def test_pr_loop_rejects_non_open_pr_before_running_codex(tmp_path):
    runner = FakeRunner(pr_payload={
        "number": 62,
        "state": "MERGED",
        "url": "https://github.com/OWNER/REPO/pull/62",
    })
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="provide an open PR"):
        run_pr_loop(runner, pr_number=62, config=config)


def test_pr_loop_refreshes_pr_head_without_just_in_time_base_sync(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    config = make_config(tmp_path)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    assert ["git", "fetch", "origin"] in commands
    assert ["git", "fetch", "origin", "+pull/77/head:refs/remotes/origin/pr/77"] in commands
    assert ["git", "switch", "main"] not in commands
    assert ["git", "pull", "--ff-only", "origin", "main"] not in commands


# ---------------------------------------------------------------------------
# Reverse flow: Codex creates PR, Claude reviews
# ---------------------------------------------------------------------------


def test_codex_issue_loop_creates_pr_then_claude_approves(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Fixed issue.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "Looks good.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    command_names = [cmd[:2] for cmd, _cwd in runner.commands]
    assert ["codex", "exec"] in command_names
    assert ["claude", "--print"] in command_names
    assert len(runner.comments) == 2
    assert runner.comments[0].startswith("Fixed issue.")
    assert runner.comments[1].startswith("**Review verdict:** Approved\n\nLooks good.")


def test_codex_issue_loop_alternates_until_claude_approval(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Implemented fix.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Addressed Claude's review.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "Missing test.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    assert len(runner.comments) == 4
    assert runner.comments[-1].startswith("**Review verdict:** Approved\n\nLGTM.")


def test_codex_issue_loop_requires_codex_to_report_pr_number(tmp_path):
    runner = FakeRunner(
        codex_outputs=["Did some work.\n<!-- AGENT_STATE: blocking -->"],
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    with pytest.raises(AgentLoopError, match="PR marker"):
        run_issue_loop(runner, issue_number=56, config=config)

    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


def test_issue_loop_rejects_outside_workdir_tests_before_posting_pr_comment(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Fixed issue.\n"
            "Tests: cd ~/llm-dialectic && python -m pytest\n"
            "<!-- AGENT_PR: 77 -->\n"
            "<!-- AGENT_STATE: blocking -->\n"
            "-- OpenAI Codex",
        ],
        claude_outputs=[
            "Looks good.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    with pytest.raises(AgentLoopError, match="outside the assigned checkout"):
        run_issue_loop(runner, issue_number=56, config=config)

    assert runner.comments == []
    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


def test_issue_loop_rejects_reported_pr_when_assigned_head_unchanged(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Fixed issue.\n"
            "Tests: python -m pytest passed.\n"
            "<!-- AGENT_PR: 77 -->\n"
            "<!-- AGENT_STATE: blocking -->\n"
            "-- OpenAI Codex",
        ],
        claude_outputs=[
            "Looks good.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
        advance_git_head_on_pr=False,
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    with pytest.raises(AgentLoopError, match="HEAD did not advance"):
        run_issue_loop(runner, issue_number=56, config=config)

    assert runner.comments == []
    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


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


def test_pr_loop_rejects_structured_followup_outside_workdir_tests_before_posting(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            structured_pr_review(
                state="blocking",
                summary="Needs a test.",
                blocking_items=["Add a regression test."],
                reviewer="Anthropic Claude",
            ),
            "Looks good.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            structured_coder_followup(
                summary="Added the test.",
                addressed_items=["item-1"],
                tests_run=["cd ~/llm-dialectic && python -m pytest"],
                reviewer="OpenAI Codex",
            ),
        ],
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    with pytest.raises(AgentLoopError, match="outside the assigned checkout"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert len(runner.comments) == 1
    assert runner.comments[0].startswith("**Review verdict:** Blocking")
    assert not any("Added the test." in comment for comment in runner.comments)


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


def test_gemini_issue_loop_creates_pr_then_codex_approves(tmp_path):
    runner = FakeRunner(
        gemini_outputs=[
            "Fixed issue.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini",
        ],
        codex_outputs=[
            "Looks good.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, coder="gemini", reviewer="codex")

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    agent_commands = [cmd[:2] for cmd, _cwd in runner.commands if cmd[:1] in (["gemini"], ["codex"])]
    assert agent_commands == [["gemini", "--prompt"], ["codex", "exec"]]
    assert len(runner.comments) == 2
    assert runner.comments[0].startswith("Fixed issue.")
    assert runner.comments[1].startswith("**Review verdict:** Approved\n\nLooks good.")


def test_gemini_issue_loop_resumes_session_for_followup(tmp_path):
    runner = FakeRunner(
        gemini_outputs=[
            json.dumps({
                "response": "Fixed issue.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini",
                "session_id": "gemini-session-1",
            }),
            # Plain-text output intentionally clears the tracked session; a third
            # Gemini turn would start without --resume.
            "Addressed review.\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini",
        ],
        codex_outputs=[
            "Needs a regression test.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Looks good."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(
        tmp_path,
        coder="gemini",
        reviewer="codex",
        gemini_args=("--output-format", "json"),
    )

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    gemini_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["gemini"]]
    assert len(gemini_calls) == 2
    assert "--resume" not in gemini_calls[0]
    assert gemini_calls[1][-2:] == ["--resume", "gemini-session-1"]




def test_gemini_review_loop_prefers_public_response_file_over_stdout(tmp_path):
    runner = FakeRunner(
        gemini_outputs=[
            "Warning: True color (24-bit) support not detected.\n"
            "YOLO mode is enabled. All tool calls will be automatically approved.\n"
            "I will fetch the PR and inspect the diff.\n"
            "Error executing tool run_shell_command: confirmation required.\n"
            "This stdout chatter should not be posted.\n",
        ],
        public_response_outputs=[
            "LGTM from response file.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini",
        ],
    )
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    gemini_call = next(cmd for cmd, _cwd in runner.commands if cmd[:1] == ["gemini"])
    assert "PUBLIC RESPONSE FILE:" in gemini_call[2]
    assert str(config.gemini_dir / ".git" / "agent-loop" / "responses" / "gemini") in gemini_call[2]
    assert runner.comments == ["**Review verdict:** Approved\n\nLGTM from response file.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"]


def test_claude_review_loop_prefers_public_response_file_over_stdout(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            json.dumps(
                {
                    "result": (
                        "I will inspect the PR diff.\n"
                        "Tool output chatter should not be posted.\n"
                    ),
                    "session_id": "claude-session-1",
                }
            ),
        ],
        public_response_outputs=[
            "LGTM from response file.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path, reviewer="claude")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    claude_call = next(cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"])
    assert "PUBLIC RESPONSE FILE:" in claude_call[-1]
    assert "/coding-review-agent-loop/responses/OWNER-REPO/claude/" in claude_call[-1]
    assert runner.comments == ["**Review verdict:** Approved\n\nLGTM from response file.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"]


def test_public_response_file_instruction_mentions_plan_revision_human_ack_exception(tmp_path):
    prompt = with_public_response_file_instruction(
        "Review the PR.",
        tmp_path / "response.md",
    )

    assert "For structured plan revisions only" in prompt
    assert "<!-- HUMAN_REQUIREMENTS_ADDRESSED -->" in prompt
    assert "`### Human requirements` section after the JSON object" in prompt
    assert "before the\n`AGENT_PLAN_STATE` footer" in prompt


def test_codex_task_loop_rejects_empty_task_text(tmp_path):
    runner = FakeRunner()
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    with pytest.raises(AgentLoopError, match="empty"):
        run_task_loop(runner, task_text="   ", config=config)

    assert runner.commands == []


def test_claude_review_loop_runs_tests_and_merge_only_after_approval(tmp_path):
    runner = FakeRunner(
        claude_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"],
    )
    config = make_config(
        tmp_path,
        coder="codex",
        reviewer="claude",
        auto_merge=True,
        test_command=("pytest", "tests/test_agent_loop.py"),
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    assert ["pytest", "tests/test_agent_loop.py"] in commands
    assert ["gh", "pr", "merge", "77", "--repo", "OWNER/REPO", "--merge"] in commands


def test_claude_review_loop_does_not_run_codex_after_final_blocking_round(tmp_path):
    runner = FakeRunner(
        claude_outputs=["Still blocked.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude", max_rounds=1)

    with pytest.raises(AgentLoopError, match="still reported blocking"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


def test_claude_review_loop_rejects_non_open_pr(tmp_path):
    runner = FakeRunner(pr_payload={
        "number": 62,
        "state": "CLOSED",
        "url": "https://github.com/OWNER/REPO/pull/62",
    })
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    with pytest.raises(AgentLoopError, match="provide an open PR"):
        run_pr_loop(runner, pr_number=62, config=config)

    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


# ---------------------------------------------------------------------------
# Repair pass tests
# ---------------------------------------------------------------------------



















































































































# ---------------------------------------------------------------------------
# New tests for issue #246: repair approved reviews with active prior dispositions
# ---------------------------------------------------------------------------



# --- repair.py prompt content tests ---













# --- _reviewer_human_requirements_instruction tests ---















# --- _surfaced_reviewer_requirement_ids tests ---







# --- PR loop repair-first tests ---










# --- Plan loop repair-first tests ---









# --- Protocol regression tests ---









# ---------------------------------------------------------------------------
# Round 2 follow-up tests: same-pr/same-plan followup recording and FORMAT fix
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# Tests for issue #273: deterministic recovery of same-round prior-item dispositions
# ---------------------------------------------------------------------------



# --- Unit tests for strip_unknown_prior_item_dispositions ---

















# --- Integration tests via _run_validated_agent ---















# ---------------------------------------------------------------------------
# Antigravity (agy) backend + Gemini retirement guidance (#215)
# ---------------------------------------------------------------------------






























def test_runner_pty_reports_tty_and_strips_ansi(tmp_path):
    """The real PTY path: the child sees a TTY and ANSI codes are stripped."""
    import sys
    from coding_review_agent_loop.runner import Runner, strip_ansi

    assert strip_ansi("\x1b[31mred\x1b[0m\r\ndone") == "red\ndone"

    program = (
        "import sys\n"
        "sys.stdout.write('istty=%s\\n' % sys.stdout.isatty())\n"
        "sys.stdout.write('\\x1b[32mGREEN\\x1b[0m\\n')\n"
    )
    log_path = tmp_path / "logs" / "pty.log"
    result = Runner().run_with_log(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        log_path=log_path,
        label="PtyProbe",
        progress_interval_seconds=999,
        check=True,
        use_pty=True,
    )
    assert "istty=True" in result.stdout
    assert "GREEN" in result.stdout
    assert "\x1b[" not in result.stdout  # ANSI stripped from captured output
    assert result.returncode == 0


@pytest.mark.parametrize("use_pty", [False, True])
def test_runner_retries_dangling_symlink_spawn_and_recovers(
    monkeypatch,
    tmp_path,
    use_pty,
):
    import coding_review_agent_loop.runner as runner_module

    command_name = "bare-agent"
    missing_target = tmp_path / "updating-agent-target"
    command = tmp_path / command_name
    command.symlink_to(missing_target)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    runner = Runner()
    runner.remember_agent_command(command_name, str(command), "--codex-cmd")
    original_popen = runner_module.subprocess.Popen
    popen_calls = []
    sleep_calls = []

    def flaky_popen(*args, **kwargs):
        popen_calls.append(args[0])
        if len(popen_calls) == 1:
            raise FileNotFoundError(command_name)
        return original_popen(*args, **kwargs)

    def restore_command(delay):
        sleep_calls.append(delay)
        command.unlink()
        command.symlink_to(sys.executable)

    monkeypatch.setattr(runner_module.subprocess, "Popen", flaky_popen)
    monkeypatch.setattr(runner_module.time, "sleep", restore_command)

    result = runner.run_with_log(
        [command_name, "-c", "print('recovered')"],
        cwd=tmp_path,
        log_path=tmp_path / "logs" / f"retry-{use_pty}.log",
        label="Retry probe",
        progress_interval_seconds=999,
        use_pty=use_pty,
    )

    assert result.returncode == 0
    assert "recovered" in result.stdout
    assert len(popen_calls) == 2
    assert sleep_calls[0] == 2
    assert all(delay == 1 for delay in sleep_calls[1:])


@pytest.mark.parametrize("use_pty", [False, True])
def test_runner_dangling_symlink_spawn_retry_is_bounded(
    monkeypatch,
    tmp_path,
    use_pty,
):
    import coding_review_agent_loop.runner as runner_module

    command_name = "bare-agent"
    command = tmp_path / command_name
    command.symlink_to(tmp_path / "missing-target")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    runner = Runner()
    runner.remember_agent_command(command_name, str(command), "--codex-cmd")
    popen_calls = []
    sleep_calls = []

    def missing_popen(*args, **kwargs):
        popen_calls.append(args[0])
        raise FileNotFoundError(command_name)

    monkeypatch.setattr(runner_module.subprocess, "Popen", missing_popen)
    monkeypatch.setattr(
        runner_module.time,
        "sleep",
        lambda delay: sleep_calls.append(delay),
    )

    with pytest.raises(
        AgentLoopError,
        match=r"CLI not found on PATH.*--codex-cmd",
    ):
        runner.run_with_log(
            [command_name, "--version"],
            cwd=tmp_path,
            log_path=tmp_path / "logs" / f"bounded-{use_pty}.log",
            label="Bounded retry probe",
            progress_interval_seconds=999,
            use_pty=use_pty,
        )

    assert len(popen_calls) == 3
    assert sleep_calls == [2, 2]


def test_runner_missing_command_without_dangling_evidence_does_not_retry(
    monkeypatch,
    tmp_path,
):
    import coding_review_agent_loop.runner as runner_module

    popen_calls = []
    sleep_calls = []

    def missing_popen(*args, **kwargs):
        popen_calls.append(args[0])
        raise FileNotFoundError("missing-agent")

    monkeypatch.setattr(runner_module.shutil, "which", lambda command: None)
    monkeypatch.setattr(runner_module.subprocess, "Popen", missing_popen)
    monkeypatch.setattr(
        runner_module.time,
        "sleep",
        lambda delay: sleep_calls.append(delay),
    )

    with pytest.raises(AgentLoopError, match="missing-agent CLI not found on PATH"):
        Runner().run_with_log(
            ["missing-agent", "--version"],
            cwd=tmp_path,
            log_path=tmp_path / "logs" / "missing.log",
            label="Missing probe",
            progress_interval_seconds=999,
        )

    assert len(popen_calls) == 1
    assert sleep_calls == []


@pytest.mark.parametrize("use_pty", [False, True])
def test_runner_absolute_path_spawn_does_not_retry(monkeypatch, tmp_path, use_pty):
    """Absolute-path FileNotFoundError raises immediately (no retry, no sleep)."""
    import coding_review_agent_loop.runner as runner_module

    abs_cmd = str(tmp_path / "no-such-binary")
    popen_calls = []
    sleep_calls = []

    def missing_popen(*args, **kwargs):
        popen_calls.append(args[0])
        raise FileNotFoundError(abs_cmd)

    monkeypatch.setattr(runner_module.subprocess, "Popen", missing_popen)
    monkeypatch.setattr(
        runner_module.time, "sleep", lambda delay: sleep_calls.append(delay),
    )

    with pytest.raises(AgentLoopError, match="not found or not executable"):
        Runner().run_with_log(
            [abs_cmd, "--version"],
            cwd=tmp_path,
            log_path=tmp_path / "logs" / f"abs-no-retry-{use_pty}.log",
            label="Absolute-path no-retry probe",
            progress_interval_seconds=999,
            use_pty=use_pty,
        )

    assert len(popen_calls) == 1
    assert sleep_calls == []




























# ---------------------------------------------------------------------------
# Dynamic model-specific signatures (#332)
# ---------------------------------------------------------------------------


def test_agent_signature_generic_without_config():
    from coding_review_agent_loop.agents.registry import agent_signature
    assert agent_signature("codex") == "OpenAI Codex"
    assert agent_signature("antigravity") == "Google Antigravity"


def test_agent_signature_uses_configured_model(tmp_path):
    from coding_review_agent_loop.agents.registry import agent_signature
    config = make_config(tmp_path, codex_model="gpt-5.2-codex", codex_reasoning_effort="high")
    assert agent_signature("codex", config) == "OpenAI Codex: gpt-5.2-codex (high)"
    # antigravity model is always declared (effort already embedded).
    assert agent_signature("antigravity", config) == "Google Antigravity: Gemini 3.5 Flash (High)"
    # gemini with no declared model falls back to the generic signature.
    assert agent_signature("gemini", make_config(tmp_path)) == "Google Gemini"


def test_agent_signature_model_used_overrides_config(tmp_path):
    from coding_review_agent_loop.agents.registry import agent_signature
    config = make_config(tmp_path, antigravity_model="Gemini 3.1 Pro (High)")
    # #333 fallback: the model that actually ran wins over the configured one.
    assert (
        agent_signature("antigravity", config, "Gemini 3.5 Flash (High)")
        == "Google Antigravity: Gemini 3.5 Flash (High)"
    )


def test_config_rejects_model_arg_conflicts(tmp_path):
    for kwargs in (
        {"codex_model": "gpt-5", "codex_args": ("--model", "other")},
        {"codex_reasoning_effort": "high", "codex_args": ("-c", 'model_reasoning_effort="low"')},
        {"gemini_model": "g", "gemini_args": ("--model", "other")},
        {"claude_model": "c", "claude_args": ("--model", "other")},
        {"antigravity_args": ("--model", "x")},
    ):
        with pytest.raises(AgentLoopError, match="conflicts with"):
            make_config(tmp_path, **kwargs)


def test_config_rejects_codex_effort_without_model(tmp_path):
    # Rollout model detection is best-effort, so effort alone cannot be labeled
    # reliably and requires an explicit --codex-model.
    with pytest.raises(AgentLoopError, match="requires --codex-model"):
        make_config(tmp_path, codex_reasoning_effort="high")
    # With a model it's accepted.
    config = make_config(tmp_path, codex_model="gpt-5", codex_reasoning_effort="high")
    assert config.codex_reasoning_effort == "high"


def test_config_allows_declared_model_without_conflict(tmp_path):
    config = make_config(tmp_path, codex_model="gpt-5", gemini_model="g", claude_model="c")
    assert config.codex_model == "gpt-5"
    assert config.gemini_model == "g"
    assert config.claude_model == "c"


def test_codex_backend_passes_model_and_effort(tmp_path):
    from coding_review_agent_loop.agents.codex import CodexBackend
    runner = FakeRunner(codex_outputs=[("STATE: approved\n\nok", 0)])
    config = make_config(tmp_path, codex_model="gpt-5.2-codex", codex_reasoning_effort="high")
    result = CodexBackend().run(runner, config, "Review", run_id="r")
    cmd = runner.commands[-1][0]
    assert cmd[cmd.index("--model") + 1] == "gpt-5.2-codex"
    assert 'model_reasoning_effort="high"' in cmd
    assert result.model_used == "gpt-5.2-codex (high)"


def test_gemini_backend_passes_model_and_sets_model_used(tmp_path):
    import coding_review_agent_loop.agents.gemini as gm
    runner = FakeRunner(gemini_outputs=[("STATE: approved\n\nok", 0)])
    config = make_config(tmp_path, gemini_model="gemini-3.5-flash")
    result = gm.BACKEND.run(runner, config, "Review", run_id="r")
    cmd = runner.commands[-1][0]
    assert cmd[cmd.index("--model") + 1] == "gemini-3.5-flash"
    assert result.model_used == "gemini-3.5-flash"


def test_claude_backend_passes_model_when_declared(tmp_path):
    from coding_review_agent_loop.agents.claude import ClaudeBackend
    runner = FakeRunner(claude_outputs=[("STATE: approved\n\nok", 0)])
    config = make_config(tmp_path, claude_model="opus")
    ClaudeBackend().run(runner, config, "Review", run_id="r")
    cmd = runner.commands[-1][0]
    assert cmd[cmd.index("--model") + 1] == "opus"


def test_public_reviewer_name_config_aware_no_leakage(tmp_path):
    from coding_review_agent_loop.comment_rendering import _public_reviewer_name
    config = make_config(tmp_path, codex_model="gpt-5", antigravity_model="Gemini 3.1 Pro (High)")
    assert _public_reviewer_name("Codex", config) == "OpenAI Codex: gpt-5"
    assert _public_reviewer_name("Antigravity", config) == "Google Antigravity: Gemini 3.1 Pro (High)"
    # No declared model → generic; unknown display name → passthrough.
    assert _public_reviewer_name("Claude", config) == "Anthropic Claude"
    assert _public_reviewer_name("Codex") == "OpenAI Codex"
    assert _public_reviewer_name("Somebody") == "Somebody"


def test_render_public_agent_comment_stamps_model_for_every_kind():
    model = "Gemini 3.1 Pro (High)"

    pr_review = parse_pr_review(
        structured_pr_review(state="approved", reviewer="Google Antigravity"),
        reviewer="Google Antigravity",
    )
    plan_review = parse_plan_review(
        structured_plan_review(state="approved", reviewer="Google Antigravity"),
        reviewer="Google Antigravity",
    )
    coder_followup = validate_structured_coder_followup(
        structured_coder_followup(state="approved", reviewer="Google Antigravity")
    )
    plan_revision = validate_structured_plan_revision(
        structured_plan_revision(reviewer="Google Antigravity")
    )
    assert coder_followup is not None
    assert plan_revision is not None

    rendered = [
        render_public_agent_comment(
            kind="pr_review",
            parsed=pr_review,
            agent="Antigravity",
            dispositions=pr_review.dispositions,
            model_used=model,
        ),
        render_public_agent_comment(
            kind="plan_review",
            parsed=plan_review,
            agent="Antigravity",
            dispositions=plan_review.dispositions,
            model_used=model,
        ),
        render_public_agent_comment(
            kind="coder_followup",
            parsed=coder_followup,
            agent="antigravity",
            model_used=model,
        ),
        render_public_agent_comment(
            kind="plan_revision",
            parsed=plan_revision,
            agent="antigravity",
            raw_text=structured_plan_revision(reviewer="Google Antigravity"),
            model_used=model,
        ),
    ]

    assert all(comment.endswith(f"-- Google Antigravity: {model}") for comment in rendered)


# ---------------------------------------------------------------------------
# Antigravity prompt — turn-end requirement (#385)
# ---------------------------------------------------------------------------






def test_base_response_file_instruction_includes_must_write_before_turn_ends(tmp_path):
    from coding_review_agent_loop.agents.base import with_public_response_file_instruction
    composed = with_public_response_file_instruction("BASE PROMPT", tmp_path / "response.md")
    assert "before your turn ends" in composed


# ── Tests: issue #400 – toolPermission: "strict" injection for reviewer ────────


























def test_reviewer_and_coder_call_sites_pass_correct_role(tmp_path):
    """_run_validated_agent propagates role= to run_agent_result correctly."""
    from unittest.mock import patch
    from coding_review_agent_loop.agents.base import AgentResult

    config = make_config(tmp_path, reviewer="gemini", agent_max_retries=0)
    captured_roles: list = []

    def mock_run(runner, *, agent, config, prompt, session_id=None, run_id=None, role=None):
        captured_roles.append(role)
        return AgentResult(text="ok")

    with patch("coding_review_agent_loop.orchestrator.run_agent_result", mock_run):
        _run_validated_agent(
            FakeRunner(),
            agent="gemini",
            config=config,
            prompt="Review the plan.",
            marker_description="test",
            validate=lambda text: text,
            role="reviewer",
        )

    assert captured_roles == ["reviewer"]
    captured_roles.clear()

    with patch("coding_review_agent_loop.orchestrator.run_agent_result", mock_run):
        _run_validated_agent(
            FakeRunner(),
            agent="gemini",
            config=config,
            prompt="Implement.",
            marker_description="test",
            validate=lambda text: text,
        )

    assert captured_roles == [None]


def test_run_agent_result_passes_role_to_backend(tmp_path, monkeypatch):
    """run_agent_result threads role= through to the backend's run() method."""
    from coding_review_agent_loop.agents.registry import run_agent_result
    from coding_review_agent_loop.agents.base import AgentResult
    from coding_review_agent_loop.agents import registry as reg_mod

    captured: dict = {}

    class TrackingBackend:
        name = "gemini"
        display_name = "Gemini"
        signature = "Google Gemini"

        def workdir(self, config):
            return tmp_path

        def default_args(self, *, dangerous):
            return ()

        def run(self, runner, config, prompt, session_id=None, run_id=None, role=None):
            captured["role"] = role
            return AgentResult(text="ok")

    monkeypatch.setitem(reg_mod.BACKENDS, "gemini", TrackingBackend())
    config = make_config(tmp_path, reviewer="gemini")
    run_agent_result(FakeRunner(), agent="gemini", config=config, prompt="Test", role="reviewer")
    assert captured["role"] == "reviewer"


# ---------------------------------------------------------------------------
# Shared PR review guidance unit and integration tests (#413, #417)
# ---------------------------------------------------------------------------















# ---------------------------------------------------------------------------
# PostedRoundMetadata.model_used and normalize_freeform_signature (#416)
# ---------------------------------------------------------------------------


def test_posted_round_metadata_model_used_round_trip():
    metadata = PostedRoundMetadata(
        flow="plan",
        role="coder",
        agent="Claude",
        round_number=1,
        subject="abc",
        model_used="gpt-5.5 (medium)",
    )
    decoded = _decode_round_metadata(_encode_round_metadata(metadata))
    assert decoded.model_used == "gpt-5.5 (medium)"


def test_posted_round_metadata_model_used_none_round_trip():
    metadata = PostedRoundMetadata(
        flow="plan",
        role="coder",
        agent="Claude",
        round_number=1,
        subject="abc",
        model_used=None,
    )
    decoded = _decode_round_metadata(_encode_round_metadata(metadata))
    assert decoded.model_used is None


def test_posted_round_metadata_model_used_backward_compat():
    payload = {
        "flow": "plan",
        "role": "coder",
        "agent": "Claude",
        "round_number": 1,
        "subject": "abc",
        "prior_items": [],
        "dispositions": [],
        "new_items": [],
        "state": None,
        "canonical_plan": None,
        "raw_structured_coder_response": None,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")
    decoded = _decode_round_metadata(encoded)
    assert decoded.model_used is None


def test_resume_plan_round_preserves_stored_model_used():
    plan = "Plan content.\n<!-- AGENT_PLAN_STATE: approved -->\n-- Anthropic Claude"
    coder_comment = _attach_round_metadata(
        plan,
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=1,
            subject=_plan_subject(plan),
        ),
    )
    review_text = structured_plan_review(state="approved", summary="LGTM.")
    reviewer_comment = _attach_round_metadata(
        review_text,
        PostedRoundMetadata(
            flow="plan",
            role="reviewer",
            agent="Codex",
            round_number=1,
            subject=_plan_subject(plan),
            state="approved",
            model_used="gpt-5.5 (medium)",
        ),
    )
    resumed = _resume_plan_round(
        [
            IssueComment(author="bot", created_at="2026-05-20T09:00:00Z", body=coder_comment),
            IssueComment(author="bot", created_at="2026-05-20T09:01:00Z", body=reviewer_comment),
        ],
        configured_reviewers=("codex",),
    )
    assert resumed is not None
    _current_plan, state = resumed
    assert state.completed_reviews[0].metadata.model_used == "gpt-5.5 (medium)"


def test_resume_pr_round_preserves_stored_model_used():
    coder_text = "Implemented the fix.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"
    coder_comment = _attach_round_metadata(
        coder_text,
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=1,
            subject="sha123",
        ),
    )
    review_text = structured_pr_review(state="approved", summary="LGTM.")
    reviewer_comment = _attach_round_metadata(
        review_text,
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Codex",
            round_number=1,
            subject="sha123",
            state="approved",
            model_used="gpt-5.5 (medium)",
        ),
    )
    resumed = _resume_pr_round(
        [
            IssueComment(author="bot", created_at="2026-05-20T09:00:00Z", body=coder_comment),
            IssueComment(author="bot", created_at="2026-05-20T09:01:00Z", body=reviewer_comment),
        ],
        head_sha="sha123",
        configured_reviewers=("codex",),
    )
    assert resumed is not None
    assert resumed.completed_reviews[0].metadata.model_used == "gpt-5.5 (medium)"


def test_normalize_freeform_signature_replaces_existing(tmp_path):
    config = make_config(tmp_path)
    result = normalize_freeform_signature(
        "Plan text.\n-- OpenAI Codex",
        agent="codex",
        config=config,
        model_used="gpt-5.5 (medium)",
    )
    assert result.endswith("-- OpenAI Codex: gpt-5.5 (medium)")
    assert "Plan text." in result


def test_normalize_freeform_signature_appends_when_absent(tmp_path):
    config = make_config(tmp_path)
    result = normalize_freeform_signature(
        "Plan text without a signature.",
        agent="codex",
        config=config,
        model_used="gpt-5.5 (medium)",
    )
    assert result.endswith("-- OpenAI Codex: gpt-5.5 (medium)")
    assert "Plan text without a signature." in result


def test_normalize_freeform_signature_skips_html_comments(tmp_path):
    config = make_config(tmp_path)
    text = "Plan text.\n-- OpenAI Codex\n<!-- AGENT_PLAN_STATE: approved -->"
    result = normalize_freeform_signature(
        text,
        agent="codex",
        config=config,
        model_used="gpt-5.5 (medium)",
    )
    assert "-- OpenAI Codex: gpt-5.5 (medium)" in result
    assert "<!-- AGENT_PLAN_STATE: approved -->" in result
    assert result.endswith("<!-- AGENT_PLAN_STATE: approved -->")


def test_normalize_freeform_signature_no_duplicate_when_already_canonical(tmp_path):
    config = make_config(tmp_path)
    canonical = "Plan text.\n-- OpenAI Codex: gpt-5.5 (medium)"
    result = normalize_freeform_signature(
        canonical,
        agent="codex",
        config=config,
        model_used="gpt-5.5 (medium)",
    )
    assert result == canonical


def test_run_plan_loop_freeform_initial_plan_includes_model(tmp_path):
    plan_text = "My initial plan.\n<!-- AGENT_PLAN_STATE: approved -->\n-- Anthropic Claude"
    reviewer_text = structured_plan_review(summary="LGTM.")
    reviewer_marker = parse_plan_review(reviewer_text, reviewer="OpenAI Codex")

    def fake_run_validated_agent(runner, *, agent, **kwargs):
        if agent == "claude":
            return ValidatedAgentResponse(
                text=plan_text,
                model_used="gpt-5.5 (medium)",
                session_id=None,
                marker_value=None,
            )
        return ValidatedAgentResponse(
            text=reviewer_text,
            model_used=None,
            session_id=None,
            marker_value=reviewer_marker,
        )

    runner = FakeRunner()
    config = make_config(tmp_path, coder="claude", reviewer="codex")
    with patch(
        "coding_review_agent_loop.orchestrator._run_validated_agent",
        side_effect=fake_run_validated_agent,
    ):
        assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    posted_body = runner.issue_comments[0]["body"]
    stripped = _strip_round_metadata(posted_body)
    assert stripped.endswith("-- Anthropic Claude: gpt-5.5 (medium)")


def test_run_plan_loop_freeform_revision_includes_model(tmp_path):
    initial_plan = "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    blocking_review_text = structured_plan_review(
        state="blocking",
        summary="Missing test strategy.",
        blocking_plan_issues=["Missing test strategy."],
    )
    blocking_review_marker = parse_plan_review(blocking_review_text, reviewer="OpenAI Codex")
    revised_plan = "Revised plan.\n<!-- AGENT_PLAN_STATE: approved -->\n-- Anthropic Claude"
    approved_review_text = structured_plan_review(
        state="approved",
        summary="LGTM.",
        prior_plan_item_dispositions=[{"item_id": "item-1", "disposition": "resolved"}],
    )
    approved_review_marker = parse_plan_review(approved_review_text, reviewer="OpenAI Codex")

    call_count = [0]

    def fake_run_validated_agent(runner, *, agent, **kwargs):
        call_count[0] += 1
        if agent == "claude":
            if call_count[0] == 1:
                return ValidatedAgentResponse(
                    text=initial_plan, model_used=None, session_id=None, marker_value=None
                )
            return ValidatedAgentResponse(
                text=revised_plan,
                model_used="gpt-5.5 (medium)",
                session_id=None,
                marker_value=None,
            )
        if call_count[0] == 2:
            return ValidatedAgentResponse(
                text=blocking_review_text,
                model_used=None,
                session_id=None,
                marker_value=blocking_review_marker,
            )
        return ValidatedAgentResponse(
            text=approved_review_text,
            model_used=None,
            session_id=None,
            marker_value=approved_review_marker,
        )

    runner = FakeRunner()
    config = make_config(tmp_path, coder="claude", reviewer="codex")
    with patch(
        "coding_review_agent_loop.orchestrator._run_validated_agent",
        side_effect=fake_run_validated_agent,
    ):
        assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    # Second issue_comments entry is the plan revision (index 1: reviewer, index 2: revision)
    revision_body = runner.issue_comments[2]["body"]
    stripped = _strip_round_metadata(revision_body)
    assert stripped.endswith("-- Anthropic Claude: gpt-5.5 (medium)")


def test_run_pr_loop_freeform_coder_followup_includes_model(tmp_path):
    blocking_review_text = structured_pr_review(
        state="blocking",
        summary="Add a regression test.",
    )
    blocking_review_marker = parse_pr_review(blocking_review_text, reviewer="OpenAI Codex")
    coder_followup_text = (
        "Added the regression test.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"
    )
    approved_review_text = structured_pr_review(
        state="approved",
        summary="LGTM.",
        prior_item_dispositions=[{"item_id": "item-1", "disposition": "resolved"}],
    )
    approved_review_marker = parse_pr_review(approved_review_text, reviewer="OpenAI Codex")

    call_count = [0]

    def fake_run_validated_agent(runner, *, agent, **kwargs):
        call_count[0] += 1
        if agent == "codex":
            if call_count[0] == 1:
                return ValidatedAgentResponse(
                    text=blocking_review_text,
                    model_used=None,
                    session_id=None,
                    marker_value=blocking_review_marker,
                )
            return ValidatedAgentResponse(
                text=approved_review_text,
                model_used=None,
                session_id=None,
                marker_value=approved_review_marker,
            )
        return ValidatedAgentResponse(
            text=coder_followup_text,
            model_used="gpt-5.5 (medium)",
            session_id=None,
            marker_value=None,
        )

    runner = FakeRunner()
    config = make_config(tmp_path, coder="claude", reviewer="codex")
    with patch(
        "coding_review_agent_loop.orchestrator._run_validated_agent",
        side_effect=fake_run_validated_agent,
    ):
        assert run_pr_loop(runner, pr_number=77, config=config) == 0

    # First PR comment is the reviewer blocking; second is the coder followup
    followup_body = runner.pr_payload["comments"][1]["body"]
    stripped = _strip_round_metadata(followup_body)
    assert stripped.endswith("-- Anthropic Claude: gpt-5.5 (medium)")


def test_pr_initial_coder_post_includes_model(tmp_path):
    plan_text = "Plan content.\n<!-- AGENT_PLAN_STATE: approved -->\n-- Anthropic Claude"
    plan_review_text = structured_plan_review(summary="Approved.")
    plan_review_marker = parse_plan_review(plan_review_text, reviewer="OpenAI Codex")
    pr_coder_text = (
        "Implemented the feature.\n"
        "<!-- AGENT_PR: 77 -->\n"
        "<!-- AGENT_STATE: blocking -->\n"
        "-- Anthropic Claude"
    )
    pr_review_text = structured_pr_review(state="approved", summary="LGTM.")
    pr_review_marker = parse_pr_review(pr_review_text, reviewer="OpenAI Codex")

    call_count = [0]

    def fake_run_validated_agent(runner, *, agent, **kwargs):
        call_count[0] += 1
        if agent == "claude":
            if call_count[0] == 1:
                return ValidatedAgentResponse(
                    text=plan_text, model_used=None, session_id=None, marker_value=None
                )
            # PR host-coder call: advance git head so validate_assigned_head_advanced passes
            before_head = runner.git_head
            runner.git_head = before_head + "-coder"
            return ValidatedAgentResponse(
                text=pr_coder_text,
                model_used="gpt-5.5 (medium)",
                session_id=None,
                marker_value=77,
            )
        if call_count[0] == 2:
            return ValidatedAgentResponse(
                text=plan_review_text,
                model_used=None,
                session_id=None,
                marker_value=plan_review_marker,
            )
        return ValidatedAgentResponse(
            text=pr_review_text,
            model_used=None,
            session_id=None,
            marker_value=pr_review_marker,
        )

    runner = FakeRunner()
    config = make_config(tmp_path, coder="claude", reviewer="codex")
    with patch(
        "coding_review_agent_loop.orchestrator._run_validated_agent",
        side_effect=fake_run_validated_agent,
    ):
        assert (
            run_issue_loop(
                runner,
                issue_number=56,
                config=config,
                plan_first=True,
                implement_after_approval=True,
            )
            == 0
        )

    # First PR comment is from the host-coder initial post
    pr_initial_body = runner.pr_payload["comments"][0]["body"]
    stripped = _strip_round_metadata(pr_initial_body)
    assert stripped.endswith("-- Anthropic Claude: gpt-5.5 (medium)")
