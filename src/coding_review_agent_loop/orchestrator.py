"""High-level issue, task, and PR orchestration loops."""

from __future__ import annotations

import datetime
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .agents.base import AgentName, AgentResult
from .agents.registry import agent_display_name, agent_signature, run_agent_result
from .config import (
    AgentLoopConfig,
    ensure_agent_workdirs,
    reviewers,
    sync_coder_base_before_implementation,
    sync_coder_pr_before_validation,
    sync_reviewer_pr_before_review,
)
from .errors import AgentLoopError, QuotaResetExceededError
from .github import (
    IssueContext,
    PullRequestReviewContext,
    get_issue_context,
    get_pr_checks,
    get_pr_review_context,
    merge_pr,
    post_issue_comment,
    post_pr_comment,
    validate_open_issue,
    validate_open_pr,
    validate_pr_references_issue,
    wait_for_ci,
)
from .logging import log, new_run_id, run_usage_summary_path
from .memory import prepare_agent_memory
from .migrations import validate_pr_migration_topology
from .prompts import (
    build_followup_prompt,
    build_issue_implementation_prompt,
    build_issue_plan_prompt,
    build_issue_prompt,
    build_plan_review_prompt,
    build_plan_revision_prompt,
    build_review_prompt,
    build_same_pr_followup_prompt,
    build_task_clarification_prompt,
    build_task_prompt,
    format_agent_list,
    render_coder_human_requirements_prompt_context,
)
from .protocol import (
    ParsedPlanReview,
    ParsedReview,
    ReviewItemDisposition,
    StructuredPlanRevision,
    UnresolvedReviewItem,
    human_requirements_resolved,
    is_clarification_request,
    parse_agent_state,
    parse_plan_review,
    parse_plan_state,
    parse_pr_number,
    review_freeform_summary_text,
    validate_human_requirements_acknowledgement,
    validate_structured_plan_revision,
)
from .protocol import ApprovedFollowup, parse_review
from .repair import attempt_repair
from .runner import Runner
from .usage import RunUsageContext, UsageMetadata, estimate_usage
from .workdirs import active_workdir
from .checks import (
    _format_pr_checks_comment,
    _pr_check_blocking_review,
    _pr_check_details,
    run_optional_tests,
    run_pre_review_tests,
)
from .comment_rendering import (
    ITEM_SUMMARY_LIMIT,
    PUBLIC_REVIEWER_NAME_BY_DISPLAY,
    _append_before_trailing_metadata,
    _format_unresolved_item_label,
    _extract_plan_revision_human_requirements_block,
    _item_label_status,
    _normalize_item_summary,
    _public_reviewer_name,
    _render_disposition_status,
    _render_prior_dispositions_section,
    _render_public_plan_review_comment,
    _render_public_plan_revision_comment,
    _render_public_pr_review_comment,
    _render_public_review_comment,
    _replace_structured_section,
    _review_freeform_summary_text,
    render_canonical_plan_revision,
    render_canonical_plan_steps,
)
from .followups import (
    APPROVED_FOLLOWUP_MARKER_RE,
    GroupedApprovedFollowup,
    MAX_APPROVED_FOLLOWUP_ISSUES,
    _append_approved_followups_marker,
    _approved_followup_from_unresolved_item,
    _approved_followups_marker,
    _create_approved_followup_issues,
    _dedupe_approved_followups,
    _followup_heading_key,
    _followup_issue_body,
    _followup_issue_title,
    _format_approved_followup_summary,
    _format_created_followup_issue_summary,
    _format_plan_approval_summary_with_followups,
    _format_same_pr_followups,
    _has_approved_followups_marker,
    _normalize_followup_key,
    _publish_approved_followups,
)
from .round_state import (
    PostedRoundMetadata,
    PostedRoundRecord,
    ROUND_RESUME_MARKER_RE,
    ResumedRoundSelection,
    ResumedReviewRound,
    _attach_round_metadata,
    _decode_round_metadata,
    _deserialize_disposition,
    _deserialize_unresolved_item,
    _encode_round_metadata,
    _extract_round_metadata_records,
    _max_unresolved_item_number_from_records,
    _plan_subject,
    _prior_item_ledger_signature,
    _resume_plan_round,
    _resume_pr_round,
    _select_current_round_records,
    _serialize_disposition,
    _serialize_unresolved_item,
    _strip_round_metadata,
)
from .unresolved_items import (
    ALL_RESOLVED_PROSE_RE,
    HUMAN_REQUIREMENTS_ACK_ITEM_ID,
    _apply_unresolved_item_dispositions,
    _clear_human_requirements_ack_item,
    _format_same_pr_unresolved_items,
    _format_unresolved_items_for_coder,
    _maybe_fill_resolved_dispositions_from_prose,
    _next_unresolved_item,
    _normalize_disposition_section_prose,
    _record_prior_item_disposition,
    _reconcile_human_requirements_ack_item,
    _upsert_human_requirements_ack_item,
    _validate_coder_followup_response,
    _validate_plan_review_response,
    _validate_review_response,
    _validate_structured_coder_followup_items,
)


TRANSIENT_AGENT_OUTPUT_RE = re.compile(
    r"Invalid stream|empty response|malformed tool call|"
    r"network (?:reset|timeout)|connection (?:reset|timed out|timeout)|"
    r"\btimed out\b|\btimeout\b|"
    r"\b5\d\d\b|Internal Server Error|Bad Gateway|Service Unavailable|Gateway Timeout|"
    r"\b429\b|rate.?limit(?:ed)?|"
    r"session.?limit.?exceeded|session_limit_exceeded|too many sessions|"
    r"no capacity available|capacity.*(?:unavailable|exceeded)|"
    r"resource.?exhausted|overloaded|"
    r"\bquota\b",
    re.I,
)
NON_RETRYABLE_AGENT_OUTPUT_RE = re.compile(
    r"auth(?:entication|orization)?|unauthorized|forbidden|invalid api key|"
    r"credit|billing|dirty (?:checkout|workdir|working tree)",
    re.I,
)
NEAR_MISS_AGENT_MARKER_RE = re.compile(
    r"(?m)^[ \t]*AGENT_(?:PLAN_)?STATE:[ \t]*(?:approved|blocking)[ \t.]*$",
    re.I,
)

# Threshold above which a rate-limit reset time causes an immediate exit
# rather than a silent wait (5 minutes).
LONG_RESET_THRESHOLD_SECONDS = 300

# Subset of TRANSIENT_AGENT_OUTPUT_RE patterns that specifically signal quota / rate-limit errors
# and where a reset time might be present in the error text.
_QUOTA_RATE_LIMIT_RE = re.compile(
    r"\b429\b|rate[- ]?limit(?:ed)?|"
    r"session[- ]?limit|too many sessions|"
    r"resource[- ]?exhausted|\bquota\b|"
    r"no capacity available|capacity.*(?:unavailable|exceeded)|"
    r"overloaded",
    re.I,
)
# Parse "Retry-After: N" (HTTP header) or "retry after N" or "retryDelay: Ns" (gRPC).
_RETRY_AFTER_SECONDS_RE = re.compile(
    r"\bretry[- ]after[:\s]+(\d+)\b"
    r"|\bretry[_-]?delay[:\s]+['\"]?(\d+)s['\"]?",
    re.I,
)
# Parse "try again in Xh Ym Zs".
_TRY_AGAIN_IN_RE = re.compile(
    r"\btry\s+again\s+in\s+"
    r"(?:(?P<h>\d+)\s*h(?:r|ours?)?\s*)?"
    r"(?:(?P<m>\d+)\s*m(?:in(?:utes?)?)?\s*)?"
    r"(?:(?P<s>\d+)\s*s(?:ec(?:onds?)?)?)?",
    re.I,
)
# Parse "reset in Xh Ym" / "resets in X hours".
_RESET_IN_RE = re.compile(
    r"\brese(?:t|ts)\s+in\s+"
    r"(?:(?P<h>\d+)\s*h(?:r|ours?)?\s*)?"
    r"(?:(?P<m>\d+)\s*m(?:in(?:utes?)?)?\s*)?"
    r"(?:(?P<s>\d+)\s*s(?:ec(?:onds?)?)?)?",
    re.I,
)
# Parse ISO 8601 timestamps (used to compute reset delta from now).
_ISO_TIMESTAMP_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)"
)


@dataclass(frozen=True)
class ValidatedAgentResponse:
    text: str
    session_id: str | None
    marker_value: object
    usage: UsageMetadata | None = None


def _parse_rate_limit_reset_seconds(text: str) -> int | None:
    """Extract the reset wait time in seconds from a rate-limit error message.

    Returns None if the reset time cannot be reliably parsed.
    """
    m = _RETRY_AFTER_SECONDS_RE.search(text)
    if m:
        val = m.group(1) or m.group(2)
        if val:
            return int(val)

    m = _TRY_AGAIN_IN_RE.search(text)
    if m and any(m.group(g) for g in ("h", "m", "s")):
        return (
            int(m.group("h") or 0) * 3600
            + int(m.group("m") or 0) * 60
            + int(m.group("s") or 0)
        )

    m = _RESET_IN_RE.search(text)
    if m and any(m.group(g) for g in ("h", "m", "s")):
        return (
            int(m.group("h") or 0) * 3600
            + int(m.group("m") or 0) * 60
            + int(m.group("s") or 0)
        )

    m = _ISO_TIMESTAMP_RE.search(text)
    if m:
        try:
            ts_str = m.group(1).replace(" ", "T")
            if not ts_str.endswith("Z"):
                ts_str += "Z"
            ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            delta = int((ts - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
            if delta > 0:
                return delta
        except (ValueError, OverflowError):
            pass

    return None


def _format_reset_duration(seconds: int) -> str:
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def _format_reset_at_utc(seconds: int) -> str:
    reset_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    return reset_time.strftime("%H:%M UTC")

def _agent_log_context(log_paths: Sequence[object]) -> str:
    paths = [str(path) for path in log_paths if path is not None]
    if not paths:
        return ""
    return "\nAttempt logs:\n" + "\n".join(f"- {path}" for path in paths)


def _is_transient_agent_output(text: str) -> bool:
    return bool(TRANSIENT_AGENT_OUTPUT_RE.search(text)) and not bool(
        NON_RETRYABLE_AGENT_OUTPUT_RE.search(text)
    )


def _is_retryable_marker_near_miss(text: str) -> bool:
    return bool(NEAR_MISS_AGENT_MARKER_RE.search(text)) and not bool(
        NON_RETRYABLE_AGENT_OUTPUT_RE.search(text)
    )


def _failure_category(text: str) -> str:
    """Classify a failure for logging: helps users decide whether to rerun or fix config/code."""
    if not text.strip():
        return "empty-response"
    if NON_RETRYABLE_AGENT_OUTPUT_RE.search(text):
        return "non-retryable"  # auth/billing — fix configuration
    if TRANSIENT_AGENT_OUTPUT_RE.search(text):
        return "transient"  # rate-limit/infra — rerun may help
    return "deterministic"  # no transient signal — may need code fix


def _retry_delay(config: AgentLoopConfig, retry_index: int) -> int:
    delays = config.agent_retry_backoff_seconds
    if not delays:
        return 1
    return delays[min(retry_index - 1, len(delays) - 1)]


def _format_invalid_agent_response_error(
    *,
    agent_name: str,
    marker_description: str,
    reason: str,
    result: AgentResult | None,
    log_paths: Sequence[object],
    category: str | None = None,
) -> str:
    exit_context = ""
    if result is not None and result.returncode != 0:
        exit_context = f" Agent exit code: {result.returncode}."
    log_context = _agent_log_context(log_paths)
    category_hint = ""
    if category == "transient":
        category_hint = " Failure category: transient (rerun may succeed)."
    elif category == "non-retryable":
        category_hint = " Failure category: non-retryable (check credentials or billing)."
    elif category == "deterministic":
        category_hint = " Failure category: deterministic (may require a code fix)."
    return (
        f"{agent_name} failed before producing a valid public response. "
        "No review result was recorded. "
        f"Required marker: {marker_description}. Reason: {reason}.{exit_context}"
        f"{category_hint}"
        f"{log_context}"
    )


def _new_usage_context(config: AgentLoopConfig) -> RunUsageContext:
    run_id = new_run_id()
    return RunUsageContext(run_id=run_id, summary_path=run_usage_summary_path(config, run_id))


def _resolve_usage_metadata(
    *,
    config: AgentLoopConfig,
    prompt: str,
    result: AgentResult,
) -> UsageMetadata | None:
    if result.usage is not None:
        return result.usage.with_io_sizes(prompt=prompt, response=result.text)
    if config.dry_run:
        return None
    return estimate_usage(prompt, result.text)


def _persist_usage_summary(config: AgentLoopConfig, usage_context: RunUsageContext) -> None:
    usage_context.write_summary()
    totals = usage_context.totals()
    log(
        config,
        "Usage summary written to "
        f"{usage_context.summary_path} "
        f"(calls={totals.call_count}, exact={totals.exact_calls}, "
        f"partial={totals.partial_calls}, estimated={totals.estimated_calls})",
    )


def _run_validated_agent(
    runner: Runner,
    *,
    agent: AgentName,
    config: AgentLoopConfig,
    prompt: str,
    marker_description: str,
    validate: Callable[[str], object],
    session_id: str | None = None,
    usage_context: RunUsageContext | None = None,
    use_repair: bool = False,
) -> ValidatedAgentResponse:
    agent_name = agent_display_name(agent)
    log_paths: list[object] = []
    max_attempts = config.agent_max_retries + 1
    last_error = f"{agent_name} produced no output."
    last_result: AgentResult | None = None

    for attempt in range(1, max_attempts + 1):
        result = run_agent_result(
            runner,
            agent=agent,
            config=config,
            prompt=prompt,
            session_id=session_id,
            run_id=usage_context.run_id if usage_context is not None else None,
        )
        last_result = result
        if result.log_path is not None:
            log_paths.append(result.log_path)
        text = result.text
        usage = _resolve_usage_metadata(config=config, prompt=prompt, result=result)
        usage_record = None
        if usage_context is not None and usage is not None:
            usage_record = usage_context.add_record(
                agent=agent,
                session_id=result.session_id,
                returncode=result.returncode,
                usage=usage,
                raw_backend_usage=result.raw_usage,
            )

        should_retry = False
        if result.returncode != 0:
            last_error = f"agent command exited with {result.returncode}"
            should_retry = _is_transient_agent_output(text)
        elif not text.strip():
            last_error = "agent response was empty"
            should_retry = _is_transient_agent_output(text)
        else:
            try:
                marker_value = validate(text)
            except AgentLoopError as exc:
                last_error = str(exc)
                should_retry = _is_transient_agent_output(text) or (
                    attempt == 1 and _is_retryable_marker_near_miss(text)
                )
                if use_repair and not _is_transient_agent_output(text):
                    log(config, f"{agent_name}: schema validation failed ({exc}); attempting repair pass")
                    repaired = attempt_repair(text, config.gemini_cmd)
                    if repaired is not None:
                        try:
                            marker_value = validate(repaired)
                        except AgentLoopError as repair_exc:
                            log(
                                config,
                                f"{agent_name}: repair pass produced invalid output ({repair_exc})",
                            )
                        else:
                            log(config, f"{agent_name}: repair pass recovered malformed response")
                            if usage_record is not None:
                                usage_record.validation_status = "validated"
                            return ValidatedAgentResponse(
                                text=repaired,
                                session_id=result.session_id,
                                marker_value=marker_value,
                                usage=usage,
                            )
            else:
                if usage_record is not None:
                    usage_record.validation_status = "validated"
                return ValidatedAgentResponse(
                    text=text,
                    session_id=result.session_id,
                    marker_value=marker_value,
                    usage=usage,
                )

        if should_retry:
            if _QUOTA_RATE_LIMIT_RE.search(text):
                reset_secs = _parse_rate_limit_reset_seconds(text)
                if reset_secs is not None and reset_secs > LONG_RESET_THRESHOLD_SECONDS:
                    duration_str = _format_reset_duration(reset_secs)
                    at_str = _format_reset_at_utc(reset_secs)
                    raise QuotaResetExceededError(
                        f"{agent_name} quota exhausted. Reset in {duration_str} (at {at_str}). "
                        "Rerun when quota resets, or switch to a different API key / model."
                    )
            if attempt < max_attempts:
                delay = _retry_delay(config, attempt)
                category = _failure_category(text)
                log(
                    config,
                    f"{agent_name}: {category} failure ({last_error}); "
                    f"retrying in {delay}s (attempt {attempt + 1}/{max_attempts})",
                )
                runner.run(("sleep", str(delay)), cwd=active_workdir(config))
                continue
        break

    raise AgentLoopError(
        _format_invalid_agent_response_error(
            agent_name=agent_name,
            marker_description=marker_description,
            reason=last_error,
            result=last_result,
            log_paths=log_paths,
            category=_failure_category(text),
        )
    )


def _require_pr_number(text: str) -> int:
    pr_number = parse_pr_number(text)
    if pr_number is None:
        raise AgentLoopError("Agent response did not include a PR marker or PR URL.")
    return pr_number


def _require_pr_number_or_clarification(text: str) -> int | str:
    pr_number = parse_pr_number(text)
    if pr_number is not None:
        return pr_number
    if is_clarification_request(text):
        return "clarification"
    raise AgentLoopError(
        "Agent response did not include a PR marker, PR URL, or clarification marker."
    )


def _require_plan_state_or_clarification(text: str) -> str:
    if is_clarification_request(text):
        return "clarification"
    return parse_plan_state(text)
def _validate_response_with_human_requirements(
    text: str,
    *,
    marker_validator: Callable[[str], object],
    human_requirements,
    requirement_scope: str,
    full_omission_fallback: str,
) -> object:
    marker_value = marker_validator(text)
    prompt_context = render_coder_human_requirements_prompt_context(
        human_requirements,
        requirement_scope=requirement_scope,
        full_omission_fallback=full_omission_fallback,
    )
    validate_human_requirements_acknowledgement(
        text,
        surfaced_requirement_ids=prompt_context.surfaced_requirement_ids,
        requires_direct_discussion_ack=prompt_context.requires_direct_discussion_ack,
    )
    return marker_value


def _merge_human_requirements(
    issue_context: IssueContext | None,
    pr_context: PullRequestReviewContext,
):
    combined = list(issue_context.human_requirements if issue_context is not None else ())
    combined.extend(pr_context.human_requirements)
    return tuple(sorted(combined, key=lambda requirement: requirement.created_at or ""))


def _validate_plan_revision_response(text: str) -> StructuredPlanRevision | str:
    parsed = validate_structured_plan_revision(text)
    if parsed is not None:
        return parsed
    return parse_plan_state(text)


def _should_record_new_blocking_item(summary: str, *, had_prior_items: bool, had_dispositions: bool) -> bool:
    if not summary:
        return False
    if not had_prior_items or not had_dispositions:
        return True
    non_empty_lines = [line.strip() for line in summary.splitlines() if line.strip()]
    if len(non_empty_lines) > 1:
        return True
    return len(non_empty_lines[0]) >= 80


def _describe_pr_review_outcome(parsed_review: ParsedReview, *, has_blocking_summary: bool) -> str:
    if parsed_review.state == "approved":
        return "approved"
    has_same_pr = bool(parsed_review.followups.same_pr)
    if has_blocking_summary and has_same_pr:
        return "blocking with blocking findings and same-PR follow-ups"
    if has_same_pr:
        return "blocking with same-PR follow-ups"
    return "blocking with blocking findings"


def _describe_plan_review_outcome(parsed_review: ParsedPlanReview) -> str:
    if parsed_review.state == "approved":
        return "approved"
    has_blocking = bool(parsed_review.items.blocking)
    has_same_plan = bool(parsed_review.items.same_plan)
    if has_blocking and has_same_plan:
        return "blocking with blocking plan issues and same-plan follow-ups"
    if has_same_plan:
        return "blocking with same-plan follow-ups"
    return "blocking with blocking plan issues"


def _run_plan_first_loop(
    runner: Runner,
    *,
    issue_number: int,
    config: AgentLoopConfig,
    memory,
    issue_context: IssueContext,
    implement_after_approval: bool,
    usage_context: RunUsageContext,
) -> int:
    coder_name = agent_display_name(config.coder)
    configured_reviewers = reviewers(config)
    coder_session_id: str | None = None
    reviewer_session_ids: dict[AgentName, str | None] = {}
    unresolved_items: list[UnresolvedReviewItem] = []
    approved_future_followups: list[ApprovedFollowup] = []
    next_unresolved_item_number = 1
    resume_state = _resume_plan_round(issue_context.comments, configured_reviewers=configured_reviewers)
    if resume_state is None:
        log(config, f"Planning issue #{issue_number}: invoking {coder_name}")
        plan_response = _run_validated_agent(
            runner,
            agent=config.coder,
            config=config,
            prompt=build_issue_plan_prompt(issue_number, config, memory, issue_context=issue_context),
            marker_description="<!-- AGENT_PLAN_STATE: approved|blocking --> or <!-- AGENT_CLARIFY -->",
            validate=lambda text, human_requirements=issue_context.human_requirements: _validate_response_with_human_requirements(
                text,
                marker_validator=_require_plan_state_or_clarification,
                human_requirements=human_requirements,
                requirement_scope="planning requirements",
                full_omission_fallback="Fetch the issue discussion directly before finalizing the plan.",
            ),
            usage_context=usage_context,
        )
        plan_output = plan_response.text
        coder_session_id = plan_response.session_id
        if is_clarification_request(plan_output):
            raise AgentLoopError(
                f"{coder_name} requested clarification during planning; human intervention required.\n\n"
                f"{coder_name}'s questions:\n{plan_output}"
            )
        current_plan = plan_output
        post_issue_comment(
            runner,
            config=config,
            issue_number=issue_number,
            body=_attach_round_metadata(
                plan_output,
                PostedRoundMetadata(
                    flow="plan",
                    role="coder",
                    agent=coder_name,
                    round_number=1,
                    subject=_plan_subject(current_plan),
                    prior_items=(),
                ),
            ),
        )
        start_round_number = 1
        resumed_round: ResumedReviewRound | None = None
    else:
        current_plan, resumed_round = resume_state
        unresolved_items = list(resumed_round.prior_items)
        next_unresolved_item_number = resumed_round.next_unresolved_item_number
        start_round_number = resumed_round.round_number
        log(config, f"Planning issue #{issue_number}: resuming round {start_round_number}")

    for round_number in range(start_round_number, config.max_rounds + 1):
        current_resume = resumed_round if resumed_round is not None and round_number == resumed_round.round_number else None
        prior_unresolved_items = current_resume.prior_items if current_resume is not None else tuple(unresolved_items)
        prior_dispositions: dict[str, list[ReviewItemDisposition]] = {
            item.item_id: [] for item in prior_unresolved_items
        }
        round_new_unresolved_items: list[UnresolvedReviewItem] = []
        round_approved_future_followups: list[ApprovedFollowup] = []
        blocking_reviews: list[tuple[str, str]] = []
        all_approved = True
        resumed_by_name = {
            record.metadata.agent: record for record in (current_resume.completed_reviews if current_resume is not None else ())
        }
        for reviewer in configured_reviewers:
            reviewer_name = agent_display_name(reviewer)
            resumed_record = resumed_by_name.get(reviewer_name)
            if resumed_record is not None:
                review_output = resumed_record.body
                parsed_review = ParsedPlanReview(
                    state=resumed_record.metadata.state or parse_plan_state(review_output),
                    summary=review_freeform_summary_text(review_output),
                    items=parse_plan_review(review_output, reviewer=reviewer_name).items,
                    dispositions=resumed_record.metadata.dispositions,
                )
                review_state = parsed_review.state
                log(config, f"Planning round {round_number}: resuming {reviewer_name}'s completed review")
                reviewer_new_unresolved_items = list(resumed_record.metadata.new_items)
            else:
                log(config, f"Planning round {round_number}: {reviewer_name} reviewing issue #{issue_number}")
                review_response = _run_validated_agent(
                    runner,
                    agent=reviewer,
                    config=config,
                    prompt=build_plan_review_prompt(
                        issue_number,
                        round_number,
                        current_plan,
                        config,
                        reviewer=reviewer,
                        memory=memory,
                        issue_context=issue_context,
                        unresolved_items=prior_unresolved_items,
                    ),
                    session_id=reviewer_session_ids.get(reviewer),
                    marker_description="<!-- AGENT_PLAN_STATE: approved|blocking -->",
                    validate=lambda text, reviewer_name=reviewer_name, items=prior_unresolved_items: _validate_plan_review_response(
                        text,
                        reviewer=reviewer_name,
                        unresolved_items=items,
                    ),
                    usage_context=usage_context,
                    use_repair=True,
                )
                review_output = review_response.text
                reviewer_session_ids[reviewer] = review_response.session_id
                parsed_review = review_response.marker_value
                assert isinstance(parsed_review, ParsedPlanReview)
                review_state = parsed_review.state
                reviewer_new_unresolved_items = []
            log(
                config,
                "Planning round "
                f"{round_number}: {reviewer_name} outcome is {_describe_plan_review_outcome(parsed_review)}",
            )
            for disposition in parsed_review.dispositions:
                _record_prior_item_disposition(
                    prior_dispositions,
                    disposition,
                    flow="plan",
                    round_number=round_number,
                    subject=_plan_subject(current_plan),
                    reviewer_name=reviewer_name,
                )
            if review_state == "blocking":
                all_approved = False
                blocking_reviews.append((reviewer_name, review_output))
            if resumed_record is None:
                for item in parsed_review.items.blocking:
                    tracked_item = _next_unresolved_item(
                        item_number=next_unresolved_item_number,
                        reviewer=item.reviewer,
                        source_round=round_number,
                        text=item.text,
                        status="blocking",
                    )
                    round_new_unresolved_items.append(tracked_item)
                    reviewer_new_unresolved_items.append(tracked_item)
                    next_unresolved_item_number += 1
                for item in parsed_review.items.same_plan:
                    tracked_item = _next_unresolved_item(
                        item_number=next_unresolved_item_number,
                        reviewer=item.reviewer,
                        source_round=round_number,
                        text=item.text,
                        status="same-plan",
                    )
                    round_new_unresolved_items.append(tracked_item)
                    reviewer_new_unresolved_items.append(tracked_item)
                    next_unresolved_item_number += 1
                for item in parsed_review.items.future:
                    tracked_item = _next_unresolved_item(
                        item_number=next_unresolved_item_number,
                        reviewer=item.reviewer,
                        source_round=round_number,
                        text=item.text,
                        status="future",
                    )
                    round_new_unresolved_items.append(tracked_item)
                    reviewer_new_unresolved_items.append(tracked_item)
                    next_unresolved_item_number += 1
                    round_approved_future_followups.append(item)
                post_issue_comment(
                    runner,
                    config=config,
                    issue_number=issue_number,
                    body=_attach_round_metadata(
                        _render_public_plan_review_comment(
                            parsed_review,
                            reviewer=reviewer_name,
                            prior_items=prior_unresolved_items,
                            dispositions=parsed_review.dispositions,
                        ),
                        PostedRoundMetadata(
                            flow="plan",
                            role="reviewer",
                            agent=reviewer_name,
                            round_number=round_number,
                            subject=_plan_subject(current_plan),
                            prior_items=prior_unresolved_items,
                            dispositions=parsed_review.dispositions,
                            new_items=tuple(reviewer_new_unresolved_items),
                            state=review_state,
                        ),
                    ),
                )
            else:
                round_new_unresolved_items.extend(reviewer_new_unresolved_items)
                round_approved_future_followups.extend(
                    _approved_followup_from_unresolved_item(item)
                    for item in reviewer_new_unresolved_items
                    if item.status == "future"
                )

        unresolved_items, future_from_prior_items = _apply_unresolved_item_dispositions(
            prior_unresolved_items,
            prior_dispositions,
            same_status="same-plan",
            retain_future=False,
        )
        unresolved_items = [*unresolved_items, *round_new_unresolved_items]
        must_fix_items = [item for item in unresolved_items if item.status in {"blocking", "same-plan"}]
        approved_future_followups.extend(
            _approved_followup_from_unresolved_item(item) for item in future_from_prior_items
        )
        if all_approved:
            approved_future_followups.extend(round_approved_future_followups)

        if all_approved and not must_fix_items:
            post_issue_comment(
                runner,
                config=config,
                issue_number=issue_number,
                body=_format_plan_approval_summary_with_followups(
                    issue_number,
                    current_plan,
                    approved_future_followups,
                ),
            )
            if not implement_after_approval:
                print(
                    f"Issue #{issue_number} plan approved by {format_agent_list(configured_reviewers)}."
                )
                return 0

            sync_coder_base_before_implementation(config, runner)
            log(config, f"Planning approved; invoking {coder_name} to implement issue #{issue_number}")
            coder_response = _run_validated_agent(
                runner,
                agent=config.coder,
                config=config,
                prompt=build_issue_implementation_prompt(
                    issue_number,
                    current_plan,
                    config,
                    memory,
                    issue_context=issue_context,
                ),
                session_id=coder_session_id,
                marker_description="<!-- AGENT_PR: <number> --> or PR URL",
                validate=lambda text, human_requirements=issue_context.human_requirements: _validate_response_with_human_requirements(
                    text,
                    marker_validator=_require_pr_number,
                    human_requirements=human_requirements,
                    requirement_scope="implementation requirements",
                    full_omission_fallback="Fetch the issue discussion directly before implementing.",
                ),
                usage_context=usage_context,
            )
            coder_output = coder_response.text
            coder_session_id = coder_response.session_id
            pr_number = int(coder_response.marker_value)
            log(config, f"{coder_name} reported PR #{pr_number}; validating it is open")
            validate_open_pr(runner, config=config, pr_number=pr_number)
            validate_pr_references_issue(
                runner,
                config=config,
                pr_number=pr_number,
                issue_number=issue_number,
            )
            post_pr_comment(
                runner,
                config=config,
                pr_number=pr_number,
                body=_attach_round_metadata(
                    coder_output,
                    PostedRoundMetadata(
                        flow="pr",
                        role="coder",
                        agent=coder_name,
                        round_number=1,
                        subject=str(get_pr_review_context(runner, config=config, pr_number=pr_number).metadata.head_sha or "unknown"),
                        prior_items=(),
                    ),
                ),
            )
            return run_pr_loop(
                runner,
                pr_number=pr_number,
                config=config,
                coder_session_id=coder_session_id,
                issue_context=issue_context,
                workdirs_ready=True,
                usage_context=usage_context,
                pre_review_test_pending=True,
            )

        if round_number == config.max_rounds:
            raise AgentLoopError(
                f"One or more reviewers still reported blocking plan issues after "
                f"round {round_number}; human review required."
            )

        combined_review = "\n\n".join(f"{name} plan review:\n\n{review}" for name, review in blocking_reviews)
        log(config, f"Planning round {round_number}: {coder_name} revising the plan")
        plan_response = _run_validated_agent(
            runner,
            agent=config.coder,
            config=config,
            prompt=build_plan_revision_prompt(
                issue_number,
                round_number,
                current_plan,
                combined_review,
                config,
                memory,
                issue_context=issue_context,
                unresolved_items=must_fix_items,
            ),
            session_id=coder_session_id,
            marker_description="<!-- AGENT_PLAN_STATE: approved|blocking -->",
            validate=lambda text, human_requirements=issue_context.human_requirements: _validate_response_with_human_requirements(
                text,
                marker_validator=_validate_plan_revision_response,
                human_requirements=human_requirements,
                requirement_scope="planning requirements",
                full_omission_fallback="Fetch the issue discussion directly before revising the plan.",
            ),
            usage_context=usage_context,
        )
        canonical_plan: str | None = None
        public_comment = plan_response.text
        if isinstance(plan_response.marker_value, StructuredPlanRevision):
            canonical_plan = render_canonical_plan_revision(plan_response.marker_value, must_fix_items)
            current_plan = canonical_plan
            public_comment = _render_public_plan_revision_comment(
                plan_response.marker_value,
                prior_items=must_fix_items,
                raw_text=plan_response.text,
                signature=agent_signature(config.coder),
            )
        else:
            current_plan = plan_response.text
        coder_session_id = plan_response.session_id
        post_issue_comment(
            runner,
            config=config,
            issue_number=issue_number,
            body=_attach_round_metadata(
                public_comment,
                PostedRoundMetadata(
                    flow="plan",
                    role="coder",
                    agent=coder_name,
                    round_number=round_number + 1,
                    subject=_plan_subject(current_plan),
                    prior_items=tuple(must_fix_items),
                    canonical_plan=canonical_plan,
                ),
            ),
        )
        unresolved_items = list(must_fix_items)
        resumed_round = None

    raise AgentLoopError(
        f"Reached max planning rounds ({config.max_rounds}) for issue #{issue_number}; "
        "human review required."
    )


def run_issue_loop(
    runner: Runner,
    *,
    issue_number: int,
    config: AgentLoopConfig,
    plan_first: bool = False,
    implement_after_approval: bool = False,
    usage_context: RunUsageContext | None = None,
) -> int:
    owned_usage_context = usage_context is None
    usage_context = usage_context or _new_usage_context(config)
    try:
        ensure_agent_workdirs(config, runner)
        log(config, f"Validating issue #{issue_number}")
        validate_open_issue(runner, config=config, issue_number=issue_number)
        issue_context = get_issue_context(runner, config=config, issue_number=issue_number)
        memory = prepare_agent_memory(runner, config)
        if plan_first:
            return _run_plan_first_loop(
                runner,
                issue_number=issue_number,
                config=config,
                memory=memory,
                issue_context=issue_context,
                implement_after_approval=implement_after_approval,
                usage_context=usage_context,
            )

        sync_coder_base_before_implementation(config, runner)
        coder_response = _run_validated_agent(
            runner,
            agent=config.coder,
            config=config,
            prompt=build_issue_prompt(issue_number, config, memory, issue_context=issue_context),
            marker_description="<!-- AGENT_PR: <number> --> or PR URL",
            validate=lambda text, human_requirements=issue_context.human_requirements: _validate_response_with_human_requirements(
                text,
                marker_validator=_require_pr_number,
                human_requirements=human_requirements,
                requirement_scope="implementation requirements",
                full_omission_fallback="Fetch the issue discussion directly before implementing.",
            ),
            usage_context=usage_context,
        )
        coder_output = coder_response.text
        coder_session_id = coder_response.session_id
        pr_number = int(coder_response.marker_value)
        log(config, f"{agent_display_name(config.coder)} reported PR #{pr_number}; validating it is open")
        validate_open_pr(runner, config=config, pr_number=pr_number)
        validate_pr_references_issue(
            runner,
            config=config,
            pr_number=pr_number,
            issue_number=issue_number,
        )
        initial_pr_metadata = get_pr_review_context(runner, config=config, pr_number=pr_number).metadata
        post_pr_comment(
            runner,
            config=config,
            pr_number=pr_number,
            body=_attach_round_metadata(
                coder_output,
                PostedRoundMetadata(
                    flow="pr",
                    role="coder",
                    agent=agent_display_name(config.coder),
                    round_number=1,
                    subject=str(initial_pr_metadata.head_sha or "unknown"),
                    prior_items=(),
                ),
            ),
        )
        return run_pr_loop(
            runner,
            pr_number=pr_number,
            config=config,
            coder_session_id=coder_session_id,
            issue_context=issue_context,
            workdirs_ready=True,
            usage_context=usage_context,
            pre_review_test_pending=True,
        )
    finally:
        if owned_usage_context:
            _persist_usage_summary(config, usage_context)


def _read_clarification_from_stdin() -> str:
    print(
        "\nProvide clarification (one entry per line; finish with a single '.' line or Ctrl+D):",
        file=sys.stderr,
        flush=True,
    )
    lines: list[str] = []
    try:
        while True:
            line = input()
            if line.strip() == ".":
                break
            lines.append(line)
    except EOFError:
        pass
    return "\n".join(lines)


def run_task_loop(
    runner: Runner,
    *,
    task_text: str,
    config: AgentLoopConfig,
    interactive: bool = False,
    max_clarification_rounds: int = 3,
    clarification_input=None,
    usage_context: RunUsageContext | None = None,
) -> int:
    owned_usage_context = usage_context is None
    usage_context = usage_context or _new_usage_context(config)
    try:
        if not task_text.strip():
            raise AgentLoopError("Task text is empty; provide a non-empty description.")
        if max_clarification_rounds < 0:
            raise AgentLoopError("--max-clarification-rounds must be zero or positive.")
        ensure_agent_workdirs(config, runner)
        memory = prepare_agent_memory(runner, config)

        history: list[tuple[str, str]] = []
        prompt = build_task_prompt(task_text, config, memory)
        read_clarification = clarification_input or _read_clarification_from_stdin
        coder_name = agent_display_name(config.coder)
        session_id: str | None = None

        for attempt in range(max_clarification_rounds + 1):
            if attempt == 0:
                sync_coder_base_before_implementation(config, runner)
            log(config, f"Task attempt {attempt + 1}: invoking {coder_name}")
            coder_response = _run_validated_agent(
                runner,
                agent=config.coder,
                config=config,
                prompt=prompt,
                session_id=session_id,
                marker_description="<!-- AGENT_PR: <number> -->, PR URL, or <!-- AGENT_CLARIFY -->",
                validate=_require_pr_number_or_clarification,
                usage_context=usage_context,
            )
            coder_output = coder_response.text
            session_id = coder_response.session_id

            if isinstance(coder_response.marker_value, int):
                pr_number = coder_response.marker_value
                log(config, f"{coder_name} reported PR #{pr_number}; validating it is open")
                validate_open_pr(runner, config=config, pr_number=pr_number)
                initial_pr_metadata = get_pr_review_context(runner, config=config, pr_number=pr_number).metadata
                post_pr_comment(
                    runner,
                    config=config,
                    pr_number=pr_number,
                    body=_attach_round_metadata(
                        coder_output,
                        PostedRoundMetadata(
                            flow="pr",
                            role="coder",
                            agent=coder_name,
                            round_number=1,
                            subject=str(initial_pr_metadata.head_sha or "unknown"),
                            prior_items=(),
                        ),
                    ),
                )
                return run_pr_loop(
                    runner,
                    pr_number=pr_number,
                    config=config,
                    coder_session_id=session_id,
                    workdirs_ready=True,
                    usage_context=usage_context,
                    pre_review_test_pending=True,
                )

            if not interactive:
                raise AgentLoopError(
                    f"{coder_name} requested clarification but the loop is non-interactive. "
                    "Add the missing details to the task text or rerun with --interactive.\n\n"
                    f"{coder_name}'s questions:\n{coder_output}"
                )

            if attempt >= max_clarification_rounds:
                raise AgentLoopError(
                    f"{coder_name} still requested clarification after "
                    f"{max_clarification_rounds} rounds; "
                    "human intervention required."
                )

            log(config, f"{coder_name} requested clarification (round {attempt + 1}); awaiting user input")
            print(coder_output, flush=True)
            answers = read_clarification()
            if not answers.strip():
                raise AgentLoopError("Empty clarification reply; aborting task.")
            history.append((coder_output, answers))
            prompt = build_task_clarification_prompt(task_text, history, config, memory)

        raise AgentLoopError("run_task_loop exited unexpectedly without producing a PR.")
    finally:
        if owned_usage_context:
            _persist_usage_summary(config, usage_context)


def run_pr_loop(
    runner: Runner,
    *,
    pr_number: int,
    config: AgentLoopConfig,
    coder_session_id: str | None = None,
    reviewer_session_id: str | None = None,
    issue_context: IssueContext | None = None,
    workdirs_ready: bool = False,
    usage_context: RunUsageContext | None = None,
    pre_review_test_pending: bool = False,
) -> int:
    owned_usage_context = usage_context is None
    usage_context = usage_context or _new_usage_context(config)
    try:
        if not workdirs_ready:
            ensure_agent_workdirs(config, runner)
        log(config, f"Validating PR #{pr_number}")
        validate_open_pr(runner, config=config, pr_number=pr_number)
        memory = prepare_agent_memory(runner, config)
        reviewer_session_ids: dict[AgentName, str | None] = {}
        configured_reviewers = reviewers(config)
        unresolved_items: list[UnresolvedReviewItem] = []
        latest_coder_output: str | None = None
        next_unresolved_item_number = 1
        start_round_number = 1
        resumed_round: ResumedReviewRound | None = None
        if reviewer_session_id is not None and configured_reviewers:
            # Backward-compatible single-reviewer resume support: older callers
            # pass one reviewer session, so attach it to the first configured reviewer.
            reviewer_session_ids[configured_reviewers[0]] = reviewer_session_id
        initial_pr_context = get_pr_review_context(runner, config=config, pr_number=pr_number)
        prefetched_pr_context: PullRequestReviewContext | None = None
        resumed_round = _resume_pr_round(
            initial_pr_context.comments,
            head_sha=initial_pr_context.metadata.head_sha,
            configured_reviewers=configured_reviewers,
        )
        if resumed_round is not None:
            unresolved_items = list(resumed_round.prior_items)
            latest_coder_output = resumed_round.coder_output
            next_unresolved_item_number = resumed_round.next_unresolved_item_number
            start_round_number = resumed_round.round_number
            log(config, f"PR #{pr_number}: resuming round {start_round_number}")
        for round_number in range(start_round_number, config.max_rounds + 1):
            coder_name = agent_display_name(config.coder)
            if pre_review_test_pending:
                run_pre_review_tests(runner, config)
                pre_review_test_pending = False
            if round_number == start_round_number:
                pr_context = initial_pr_context
            elif prefetched_pr_context is not None:
                pr_context = prefetched_pr_context
                prefetched_pr_context = None
            else:
                pr_context = get_pr_review_context(runner, config=config, pr_number=pr_number)
            initial_pr_context = pr_context
            pr_metadata = pr_context.metadata
            pr_comments = pr_context.comments
            human_requirements = _merge_human_requirements(issue_context, pr_context)
            current_resume = resumed_round if resumed_round is not None and round_number == resumed_round.round_number else None
            unresolved_items = _reconcile_human_requirements_ack_item(
                current_resume.prior_items if current_resume is not None else unresolved_items,
                coder_output=latest_coder_output,
                human_requirements=human_requirements,
                source_round=round_number,
            )
            prior_unresolved_items = tuple(unresolved_items)
            prior_dispositions: dict[str, list[ReviewItemDisposition]] = {
                item.item_id: [] for item in prior_unresolved_items
            }
            round_new_unresolved_items: list[UnresolvedReviewItem] = []
            approved_review_outputs: list[tuple[str, str]] = []
            pr_checks = get_pr_checks(runner, config=config, metadata=pr_metadata)
            resumed_by_name = {
                record.metadata.agent: record for record in (current_resume.completed_reviews if current_resume is not None else ())
            }
            for reviewer in configured_reviewers:
                reviewer_name = agent_display_name(reviewer)
                resumed_record = resumed_by_name.get(reviewer_name)
                if resumed_record is not None:
                    review_output = resumed_record.body
                    reparsed_review = parse_review(review_output, reviewer=reviewer_name)
                    parsed_review = ParsedReview(
                        state=resumed_record.metadata.state or parse_agent_state(review_output),
                        summary=review_freeform_summary_text(review_output),
                        blocking_items=reparsed_review.blocking_items,
                        followups=reparsed_review.followups,
                        dispositions=resumed_record.metadata.dispositions,
                    )
                    review_state = parsed_review.state
                    reviewer_new_unresolved_items = list(resumed_record.metadata.new_items)
                    log(config, f"Round {round_number}: resuming {reviewer_name}'s completed review")
                else:
                    log(config, f"Round {round_number}: {reviewer_name} reviewing PR #{pr_number}")
                    sync_reviewer_pr_before_review(config, runner, reviewer, pr_number, pr_metadata)
                    review_response = _run_validated_agent(
                        runner,
                        agent=reviewer,
                        config=config,
                        prompt=build_review_prompt(
                            pr_number,
                            round_number,
                            config,
                            reviewer=reviewer,
                            pr_metadata=pr_metadata,
                            pr_checks=pr_checks,
                            memory=memory,
                            issue_context=issue_context,
                            human_requirements=human_requirements,
                            unresolved_items=prior_unresolved_items,
                        ),
                        session_id=reviewer_session_ids.get(reviewer),
                        marker_description="<!-- AGENT_STATE: approved|blocking -->",
                        validate=lambda text, reviewer_name=reviewer_name, items=prior_unresolved_items: _validate_review_response(
                            text,
                            reviewer=reviewer_name,
                            unresolved_items=items,
                        ),
                        usage_context=usage_context,
                        use_repair=True,
                    )
                    review_output = review_response.text
                    reviewer_session_ids[reviewer] = review_response.session_id
                    parsed_review = review_response.marker_value
                    assert isinstance(parsed_review, ParsedReview)
                    review_state = parsed_review.state
                    reviewer_new_unresolved_items = []

                for disposition in parsed_review.dispositions:
                    _record_prior_item_disposition(
                        prior_dispositions,
                        disposition,
                        flow="pr",
                        round_number=round_number,
                        subject=str(pr_metadata.head_sha or "unknown"),
                        reviewer_name=reviewer_name,
                    )
                blocking_summary = parsed_review.summary
                has_blocking_summary = _should_record_new_blocking_item(
                    blocking_summary,
                    had_prior_items=bool(prior_unresolved_items),
                    had_dispositions=bool(parsed_review.dispositions),
                )
                log(
                    config,
                    f"Round {round_number}: {reviewer_name} outcome is "
                    f"{_describe_pr_review_outcome(parsed_review, has_blocking_summary=has_blocking_summary)}",
                )
                if review_state == "blocking":
                    if resumed_record is None:
                        if has_blocking_summary:
                            tracked_item = _next_unresolved_item(
                                item_number=next_unresolved_item_number,
                                reviewer=reviewer_name,
                                source_round=round_number,
                                text=blocking_summary,
                                status="blocking",
                            )
                            round_new_unresolved_items.append(tracked_item)
                            reviewer_new_unresolved_items.append(tracked_item)
                            next_unresolved_item_number += 1
                        if parsed_review.followups.same_pr:
                            if config.approved_followups.startswith("fix-and-"):
                                for followup in parsed_review.followups.same_pr:
                                    tracked_item = _next_unresolved_item(
                                        item_number=next_unresolved_item_number,
                                        reviewer=followup.reviewer,
                                        source_round=round_number,
                                        text=followup.text,
                                        status="same-pr",
                                    )
                                    round_new_unresolved_items.append(tracked_item)
                                    reviewer_new_unresolved_items.append(tracked_item)
                                    next_unresolved_item_number += 1
                            else:
                                tracked_item = _next_unresolved_item(
                                    item_number=next_unresolved_item_number,
                                    reviewer=reviewer_name,
                                    source_round=round_number,
                                    text="\n".join(
                                        [
                                            "Blocking review included Same-PR follow-ups, "
                                            f"but --approved-followups={config.approved_followups} "
                                            "does not enable a same-PR fix path.",
                                            "",
                                            _format_same_pr_followups(parsed_review.followups.same_pr),
                                        ]
                                    ),
                                    status="blocking",
                                )
                                round_new_unresolved_items.append(tracked_item)
                                reviewer_new_unresolved_items.append(tracked_item)
                                next_unresolved_item_number += 1
                        post_pr_comment(
                            runner,
                            config=config,
                            pr_number=pr_number,
                            body=_attach_round_metadata(
                                _render_public_pr_review_comment(
                                    parsed_review,
                                    reviewer=reviewer_name,
                                    human_requirements_resolved_flag=human_requirements_resolved(
                                        review_output
                                    ),
                                    prior_items=prior_unresolved_items,
                                    dispositions=parsed_review.dispositions,
                                ),
                                PostedRoundMetadata(
                                    flow="pr",
                                    role="reviewer",
                                    agent=reviewer_name,
                                    round_number=round_number,
                                    subject=str(pr_metadata.head_sha or "unknown"),
                                    prior_items=prior_unresolved_items,
                                    dispositions=parsed_review.dispositions,
                                    new_items=tuple(reviewer_new_unresolved_items),
                                    state=review_state,
                                ),
                            ),
                        )
                    else:
                        round_new_unresolved_items.extend(reviewer_new_unresolved_items)
                    continue

                approved_review_outputs.append((reviewer_name, review_output))
                if resumed_record is None:
                    if config.approved_followups != "ignore":
                        for followup in parsed_review.followups.future:
                            tracked_item = _next_unresolved_item(
                                item_number=next_unresolved_item_number,
                                reviewer=followup.reviewer,
                                source_round=round_number,
                                text=followup.text,
                                status="future",
                            )
                            round_new_unresolved_items.append(tracked_item)
                            reviewer_new_unresolved_items.append(tracked_item)
                            next_unresolved_item_number += 1
                    post_pr_comment(
                        runner,
                        config=config,
                        pr_number=pr_number,
                        body=_attach_round_metadata(
                            _render_public_pr_review_comment(
                                parsed_review,
                                reviewer=reviewer_name,
                                human_requirements_resolved_flag=human_requirements_resolved(
                                    review_output
                                ),
                                prior_items=prior_unresolved_items,
                                dispositions=parsed_review.dispositions,
                            ),
                            PostedRoundMetadata(
                                flow="pr",
                                role="reviewer",
                                agent=reviewer_name,
                                round_number=round_number,
                                subject=str(pr_metadata.head_sha or "unknown"),
                                prior_items=prior_unresolved_items,
                                dispositions=parsed_review.dispositions,
                                new_items=tuple(reviewer_new_unresolved_items),
                                state=review_state,
                            ),
                        ),
                    )
                else:
                    round_new_unresolved_items.extend(reviewer_new_unresolved_items)

            unresolved_items, _future_items = _apply_unresolved_item_dispositions(
                prior_unresolved_items,
                prior_dispositions,
            )
            unresolved_items = [*unresolved_items, *round_new_unresolved_items]
            future_followups = [
                _approved_followup_from_unresolved_item(item)
                for item in unresolved_items
                if item.status == "future"
            ]
            must_fix_items = [item for item in unresolved_items if item.status in {"blocking", "same-pr"}]

            if not must_fix_items:
                if human_requirements:
                    missing_acknowledgements = [
                        reviewer_name
                        for reviewer_name, review_output in approved_review_outputs
                        if not human_requirements_resolved(review_output)
                    ]
                    if missing_acknowledgements:
                        log(
                            config,
                            f"Round {round_number}: reviewer(s) {', '.join(missing_acknowledgements)} "
                            "approved without acknowledging signed human requirements; "
                            "re-injecting as blocking item",
                        )
                        unresolved_items.append(
                            _next_unresolved_item(
                                item_number=next_unresolved_item_number,
                                reviewer="Orchestrator",
                                source_round=round_number,
                                text=(
                                    f"Reviewer(s) {', '.join(missing_acknowledgements)} approved without "
                                    "acknowledging the signed human requirements. Coder must address the "
                                    "human requirements and ensure the reviewer explicitly resolves them "
                                    "before approval."
                                ),
                                status="blocking",
                            )
                        )
                        next_unresolved_item_number += 1
                        must_fix_items = [
                            item for item in unresolved_items if item.status in {"blocking", "same-pr"}
                        ]
                sync_coder_pr_before_validation(config, runner, pr_number, pr_metadata)
                migration_validation = validate_pr_migration_topology(
                    runner,
                    config=config,
                    checkout=active_workdir(config),
                    pr_metadata=pr_metadata,
                )
                if not migration_validation.ok:
                    log(config, f"Round {round_number}: Alembic migration validation blocked approval")
                    unresolved_items.append(
                        _next_unresolved_item(
                            item_number=next_unresolved_item_number,
                            reviewer="Alembic migration validation",
                            source_round=round_number,
                            text=migration_validation.message or "Migration validation failed.",
                            status="blocking",
                        )
                    )
                    next_unresolved_item_number += 1
                    must_fix_items = [item for item in unresolved_items if item.status in {"blocking", "same-pr"}]
                if not must_fix_items:
                    if pr_checks.state in {"pending", "unavailable"}:
                        _publish_approved_followups(
                            runner,
                            config=config,
                            pr_number=pr_number,
                            head_sha=pr_metadata.head_sha,
                            pr_comments=pr_comments,
                            followups=future_followups,
                        )
                    if pr_checks.state in {"failing", "pending", "unavailable"}:
                        details = _pr_check_details(pr_checks)
                        post_pr_comment(
                            runner,
                            config=config,
                            pr_number=pr_number,
                            body=_format_pr_checks_comment(pr_number, pr_checks.state, details),
                        )
                        if pr_checks.state == "failing":
                            log(
                                config,
                                f"Round {round_number}: GitHub PR checks blocked approval ({pr_checks.state})",
                            )
                            unresolved_items.append(
                                _next_unresolved_item(
                                    item_number=next_unresolved_item_number,
                                    reviewer="GitHub PR checks",
                                    source_round=round_number,
                                    text=_pr_check_blocking_review(pr_number, pr_checks.state, details),
                                    status="blocking",
                                )
                            )
                            next_unresolved_item_number += 1
                            must_fix_items = [
                                item for item in unresolved_items if item.status in {"blocking", "same-pr"}
                            ]
                        else:
                            raise AgentLoopError(
                                f"GitHub PR checks for PR #{pr_number} are {pr_checks.state}; "
                                "wait for CI or investigate GitHub API access before treating the PR as approved."
                            )
                if not must_fix_items:
                    _publish_approved_followups(
                        runner,
                        config=config,
                        pr_number=pr_number,
                        head_sha=pr_metadata.head_sha,
                        pr_comments=pr_comments,
                        followups=future_followups,
                    )
                    run_optional_tests(runner, config)
                    if config.auto_merge:
                        wait_for_ci(runner, config, pr_number)
                        merge_pr(runner, config, pr_number)
                    print(f"PR #{pr_number} approved by {format_agent_list(configured_reviewers)}.")
                    return 0
            if round_number == config.max_rounds:
                raise AgentLoopError(
                    f"One or more reviewers still reported blocking issues after round {round_number}; "
                    "human review required."
                )

            same_pr_items = [item for item in unresolved_items if item.status == "same-pr"]
            blocking_items = [item for item in unresolved_items if item.status == "blocking"]
            if same_pr_items and not blocking_items:
                combined_review = _format_same_pr_unresolved_items(same_pr_items)
                coder_human_requirements_context = render_coder_human_requirements_prompt_context(
                    human_requirements
                )
                followup_prompt = build_same_pr_followup_prompt(
                    pr_number,
                    round_number,
                    combined_review,
                    config,
                    memory,
                    issue_context=issue_context,
                    human_requirements=human_requirements,
                    human_requirements_context=coder_human_requirements_context,
                )
            else:
                combined_review = _format_unresolved_items_for_coder(unresolved_items)
                coder_human_requirements_context = render_coder_human_requirements_prompt_context(
                    human_requirements
                )
                followup_prompt = build_followup_prompt(
                    pr_number,
                    round_number,
                    combined_review,
                    config,
                    memory,
                    issue_context=issue_context,
                    human_requirements=human_requirements,
                    human_requirements_context=coder_human_requirements_context,
                )
            log(config, f"Round {round_number}: {coder_name} addressing reviewer feedback")
            coder_response = _run_validated_agent(
                runner,
                agent=config.coder,
                config=config,
                prompt=followup_prompt,
                session_id=coder_session_id,
                marker_description="<!-- AGENT_STATE: approved|blocking -->",
                validate=lambda text, items=tuple(unresolved_items), human_requirements=human_requirements: _validate_coder_followup_response(
                    text,
                    unresolved_items=items,
                    human_requirements=human_requirements,
                ),
                usage_context=usage_context,
                use_repair=True,
            )
            coder_output = coder_response.text
            coder_session_id = coder_response.session_id
            latest_coder_output = coder_output

            unresolved_items = _reconcile_human_requirements_ack_item(
                unresolved_items,
                coder_output=coder_output,
                human_requirements=human_requirements,
                source_round=round_number,
            )
            updated_pr_context = get_pr_review_context(runner, config=config, pr_number=pr_number)

            post_pr_comment(
                runner,
                config=config,
                pr_number=pr_number,
                body=_attach_round_metadata(
                    coder_output,
                    PostedRoundMetadata(
                        flow="pr",
                        role="coder",
                        agent=coder_name,
                        round_number=round_number + 1,
                        subject=str(updated_pr_context.metadata.head_sha or "unknown"),
                        prior_items=tuple(unresolved_items),
                    ),
                ),
            )
            log(config, f"Round {round_number}: {coder_name} pushed updates for re-review")
            pre_review_test_pending = True
            resumed_round = None
            prefetched_pr_context = updated_pr_context

        raise AgentLoopError(
            f"Reached max rounds ({config.max_rounds}) for PR #{pr_number}; human review required."
        )
    finally:
        if owned_usage_context:
            _persist_usage_summary(config, usage_context)
