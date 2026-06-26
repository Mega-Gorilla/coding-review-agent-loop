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

