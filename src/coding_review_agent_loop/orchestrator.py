"""High-level issue, task, and PR orchestration loops."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .agents.base import AgentName, AgentResult
from .agents.registry import agent_display_name, run_agent_result
from .config import (
    AgentLoopConfig,
    ensure_agent_workdirs,
    reviewers,
    sync_coder_base_before_implementation,
    sync_coder_pr_before_validation,
    sync_reviewer_pr_before_review,
)
from .errors import AgentLoopError
from .github import (
    IssueContext,
    PullRequestChecks,
    create_issue,
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
    FUTURE_FOLLOWUP_HEADING_RE,
    HUMAN_REQUIREMENTS_HEADING_RE,
    LEGACY_FOLLOWUP_HEADING_RE,
    ParsedPlanReview,
    PRIOR_UNRESOLVED_ITEM_DISPOSITIONS_HEADING_RE,
    PRIOR_UNRESOLVED_PLAN_ITEM_DISPOSITIONS_HEADING_RE,
    SAME_PR_FOLLOWUP_HEADING_RE,
    ParsedReview,
    ReviewItemDisposition,
    UnresolvedReviewItem,
    human_requirements_resolved,
    is_clarification_request,
    parse_agent_state,
    parse_plan_review,
    parse_plan_state,
    parse_pr_number,
    validate_human_requirements_acknowledgement,
)
from .protocol import ApprovedFollowup, parse_review
from .runner import Runner
from .usage import RunUsageContext, UsageMetadata, estimate_usage
from .workdirs import active_workdir

MAX_APPROVED_FOLLOWUP_ISSUES = 3
HUMAN_REQUIREMENTS_ACK_ITEM_ID = "item-human-requirements-acknowledgement"
APPROVED_FOLLOWUP_MARKER_RE = re.compile(
    r"<!--\s*AGENT_APPROVED_FOLLOWUPS:\s*pr=(?P<pr>\d+)\s+head=(?P<head>\S+)\s+mode=(?P<mode>[a-z-]+)\s*-->",
    re.I,
)

TRANSIENT_AGENT_OUTPUT_RE = re.compile(
    r"Invalid stream|empty response|malformed tool call|"
    r"network (?:reset|timeout)|connection (?:reset|timed out|timeout)|"
    r"\btimed out\b|\btimeout\b|"
    r"\b5\d\d\b|Internal Server Error|Bad Gateway|Service Unavailable|Gateway Timeout",
    re.I,
)
NON_RETRYABLE_AGENT_OUTPUT_RE = re.compile(
    r"auth(?:entication|orization)?|unauthorized|forbidden|invalid api key|"
    r"credit|quota|rate limit|billing|dirty (?:checkout|workdir|working tree)",
    re.I,
)
NEAR_MISS_AGENT_MARKER_RE = re.compile(
    r"(?m)^[ \t]*AGENT_(?:PLAN_)?STATE:[ \t]*(?:approved|blocking)[ \t.]*$",
    re.I,
)


@dataclass(frozen=True)
class ValidatedAgentResponse:
    text: str
    session_id: str | None
    marker_value: object
    usage: UsageMetadata | None = None


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
) -> str:
    exit_context = ""
    if result is not None and result.returncode != 0:
        exit_context = f" Agent exit code: {result.returncode}."
    log_context = _agent_log_context(log_paths)
    return (
        f"{agent_name} failed before producing a valid public response. "
        "No review result was recorded. "
        f"Required marker: {marker_description}. Reason: {reason}.{exit_context}"
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
            else:
                if usage_record is not None:
                    usage_record.validation_status = "validated"
                return ValidatedAgentResponse(
                    text=text,
                    session_id=result.session_id,
                    marker_value=marker_value,
                    usage=usage,
                )

        if should_retry and attempt < max_attempts:
            delay = _retry_delay(config, attempt)
            log(
                config,
                f"{agent_name} produced a transient invalid response; "
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


@dataclass(frozen=True)
class GroupedApprovedFollowup:
    text: str
    items: tuple[ApprovedFollowup, ...]

    @property
    def reviewers(self) -> tuple[str, ...]:
        reviewers: list[str] = []
        for item in self.items:
            if item.reviewer not in reviewers:
                reviewers.append(item.reviewer)
        return tuple(reviewers)


def _next_unresolved_item(
    *,
    item_number: int,
    reviewer: str,
    source_round: int,
    text: str,
    status: str,
    notes: Sequence[str] = (),
) -> UnresolvedReviewItem:
    return UnresolvedReviewItem(
        item_id=f"item-{item_number}",
        reviewer=reviewer,
        source_round=source_round,
        text=text,
        status=status,
        notes=tuple(notes),
    )


def _validate_review_response(
    text: str,
    *,
    reviewer: str,
    unresolved_items: Sequence[UnresolvedReviewItem],
) -> ParsedReview:
    parsed = parse_review(text, reviewer=reviewer)
    if not unresolved_items:
        return parsed

    unresolved_by_id = {item.item_id: item for item in unresolved_items}
    disposition_ids = [item.item_id for item in parsed.dispositions]
    duplicates = sorted({item_id for item_id in disposition_ids if disposition_ids.count(item_id) > 1})
    if duplicates:
        raise AgentLoopError(
            "Review listed prior unresolved items more than once: " + ", ".join(duplicates)
        )
    unknown = sorted(set(disposition_ids) - set(unresolved_by_id))
    if unknown:
        raise AgentLoopError(
            "Review referenced unknown prior unresolved item IDs: " + ", ".join(unknown)
        )
    missing = sorted(set(unresolved_by_id) - set(disposition_ids))
    if missing:
        raise AgentLoopError(
            "Review did not evaluate all prior unresolved items: " + ", ".join(missing)
        )
    return parsed


def _upsert_human_requirements_ack_item(
    unresolved_items: Sequence[UnresolvedReviewItem],
    *,
    source_round: int,
    text: str,
) -> list[UnresolvedReviewItem]:
    retained = [
        item for item in unresolved_items if item.item_id != HUMAN_REQUIREMENTS_ACK_ITEM_ID
    ]
    retained.append(
        UnresolvedReviewItem(
            item_id=HUMAN_REQUIREMENTS_ACK_ITEM_ID,
            reviewer="Orchestrator",
            source_round=source_round,
            text=text,
            status="blocking",
        )
    )
    return retained


def _clear_human_requirements_ack_item(
    unresolved_items: Sequence[UnresolvedReviewItem],
) -> list[UnresolvedReviewItem]:
    return [item for item in unresolved_items if item.item_id != HUMAN_REQUIREMENTS_ACK_ITEM_ID]


def _reconcile_human_requirements_ack_item(
    unresolved_items: Sequence[UnresolvedReviewItem],
    *,
    coder_output: str | None,
    human_requirements,
    source_round: int,
) -> list[UnresolvedReviewItem]:
    if coder_output is None:
        return list(unresolved_items)

    prompt_context = render_coder_human_requirements_prompt_context(human_requirements)
    try:
        validate_human_requirements_acknowledgement(
            coder_output,
            surfaced_requirement_ids=prompt_context.surfaced_requirement_ids,
            requires_direct_discussion_ack=prompt_context.requires_direct_discussion_ack,
        )
    except AgentLoopError as exc:
        return _upsert_human_requirements_ack_item(
            unresolved_items,
            source_round=source_round,
            text=str(exc),
        )
    return _clear_human_requirements_ack_item(unresolved_items)


def _apply_unresolved_item_dispositions(
    unresolved_items: Sequence[UnresolvedReviewItem],
    dispositions_by_item: dict[str, list[ReviewItemDisposition]],
    *,
    same_status: str = "same-pr",
    retain_future: bool = True,
) -> tuple[list[UnresolvedReviewItem], list[UnresolvedReviewItem]]:
    next_unresolved: list[UnresolvedReviewItem] = []
    future_items: list[UnresolvedReviewItem] = []
    for item in unresolved_items:
        dispositions = dispositions_by_item.get(item.item_id, [])
        if not dispositions:
            next_unresolved.append(item)
            continue
        text = item.text
        notes = list(item.notes)
        outcomes = {disposition.disposition for disposition in dispositions}
        preserve_note_in_text = bool({"blocking", same_status} & outcomes) or (
            "future" in outcomes and not retain_future
        )
        for disposition in dispositions:
            if disposition.note:
                note_text = f"{disposition.reviewer}: {disposition.note}"
                if preserve_note_in_text:
                    update_line = f"Update from {note_text}"
                    if update_line not in text:
                        text = f"{text.rstrip()}\n\n{update_line}"
                if note_text not in notes:
                    notes.append(note_text)
        if "blocking" in outcomes:
            next_unresolved.append(
                UnresolvedReviewItem(
                    item_id=item.item_id,
                    reviewer=item.reviewer,
                    source_round=item.source_round,
                    text=text,
                    status="blocking",
                    notes=tuple(notes),
                )
            )
            continue
        if same_status in outcomes:
            next_unresolved.append(
                UnresolvedReviewItem(
                    item_id=item.item_id,
                    reviewer=item.reviewer,
                    source_round=item.source_round,
                    text=text,
                    status=same_status,
                    notes=tuple(notes),
                )
            )
            continue
        if "future" in outcomes:
            future_item = UnresolvedReviewItem(
                item_id=item.item_id,
                reviewer=item.reviewer,
                source_round=item.source_round,
                text=text,
                status="future",
                notes=tuple(notes),
            )
            if retain_future:
                next_unresolved.append(future_item)
            else:
                future_items.append(future_item)
    return next_unresolved, future_items


def _approved_followup_from_unresolved_item(item: UnresolvedReviewItem) -> ApprovedFollowup:
    text = item.text
    for note in item.notes:
        update_line = f"Update from {note}"
        if update_line not in text:
            text = f"{text.rstrip()}\n\n{update_line}"
    return ApprovedFollowup(reviewer=item.reviewer, text=text)


def _validate_plan_review_response(
    text: str,
    *,
    reviewer: str,
    unresolved_items: Sequence[UnresolvedReviewItem],
) -> ParsedPlanReview:
    parsed = parse_plan_review(text, reviewer=reviewer)
    if not unresolved_items:
        return parsed

    unresolved_by_id = {item.item_id: item for item in unresolved_items}
    disposition_ids = [item.item_id for item in parsed.dispositions]
    duplicates = sorted({item_id for item_id in disposition_ids if disposition_ids.count(item_id) > 1})
    if duplicates:
        raise AgentLoopError(
            "Plan review listed prior unresolved plan items more than once: "
            + ", ".join(duplicates)
        )
    unknown = sorted(set(disposition_ids) - set(unresolved_by_id))
    if unknown:
        raise AgentLoopError(
            "Plan review referenced unknown prior unresolved plan item IDs: "
            + ", ".join(unknown)
        )
    missing = sorted(set(unresolved_by_id) - set(disposition_ids))
    if missing:
        raise AgentLoopError(
            "Plan review did not evaluate all prior unresolved plan items: "
            + ", ".join(missing)
        )
    return parsed


def _review_freeform_summary_text(text: str) -> str:
    lines: list[str] = []
    skip_structured_section = False
    structured_heading_res = (
        SAME_PR_FOLLOWUP_HEADING_RE,
        FUTURE_FOLLOWUP_HEADING_RE,
        LEGACY_FOLLOWUP_HEADING_RE,
        HUMAN_REQUIREMENTS_HEADING_RE,
        PRIOR_UNRESOLVED_ITEM_DISPOSITIONS_HEADING_RE,
        PRIOR_UNRESOLVED_PLAN_ITEM_DISPOSITIONS_HEADING_RE,
    )
    for line in text.splitlines():
        stripped = line.strip()
        if any(pattern.match(line) for pattern in structured_heading_res):
            skip_structured_section = True
            continue
        if skip_structured_section and stripped.startswith("### "):
            skip_structured_section = False
        if skip_structured_section:
            continue
        if not stripped:
            lines.append("")
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        if stripped.startswith("-- "):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _should_record_new_blocking_item(summary: str, *, had_prior_items: bool, had_dispositions: bool) -> bool:
    if not summary:
        return False
    if not had_prior_items or not had_dispositions:
        return True
    non_empty_lines = [line.strip() for line in summary.splitlines() if line.strip()]
    if len(non_empty_lines) > 1:
        return True
    return len(non_empty_lines[0]) >= 80


def run_optional_tests(runner: Runner, config: AgentLoopConfig) -> None:
    if not config.test_command:
        return
    log(config, f"Running local test command: {' '.join(config.test_command)}")
    runner.run(config.test_command, cwd=active_workdir(config))
    log(config, "Local test command passed")


def run_pre_review_tests(runner: Runner, config: AgentLoopConfig) -> None:
    if not config.pre_review_tests or not config.test_command:
        return
    log(config, f"Running pre-review test command: {' '.join(config.test_command)}")
    runner.run(config.test_command, cwd=active_workdir(config))
    log(config, "Pre-review test command passed")


def _format_approved_followup_summary(pr_number: int, followups: list[ApprovedFollowup]) -> str:
    lines = [
        f"Approved-review future follow-ups for PR #{pr_number}:",
        "",
    ]
    for followup in followups:
        lines.append(f"- {followup.text} ({followup.reviewer})")
    lines.extend(
        [
            "",
            "These were mentioned in approved reviews as future work and did not block merge readiness.",
            "",
            "-- coding-review-agent-loop",
        ]
    )
    return "\n".join(lines)


def _approved_followups_marker(pr_number: int, head_sha: str | None, mode: str) -> str:
    head = head_sha or "unknown"
    return f"<!-- AGENT_APPROVED_FOLLOWUPS: pr={pr_number} head={head} mode={mode} -->"


def _append_approved_followups_marker(
    body: str,
    *,
    pr_number: int,
    head_sha: str | None,
    mode: str,
) -> str:
    footer = "\n-- coding-review-agent-loop"
    prefix, found, _suffix = body.rpartition(footer)
    if not found:
        return body
    prefix = prefix.rstrip()
    return "\n".join(
        [
            prefix,
            "",
            _approved_followups_marker(pr_number, head_sha, mode),
            "-- coding-review-agent-loop",
        ]
    )


def _has_approved_followups_marker(
    comments: Sequence[object],
    *,
    pr_number: int,
    head_sha: str | None,
    mode: str,
) -> bool:
    target_head = head_sha or "unknown"
    for comment in comments:
        body = getattr(comment, "body", None)
        if not isinstance(body, str):
            continue
        for match in APPROVED_FOLLOWUP_MARKER_RE.finditer(body):
            if (
                int(match.group("pr")) == pr_number
                and match.group("head") == target_head
                and match.group("mode").lower() == mode.lower()
            ):
                return True
    return False


def _followup_issue_title(followup: ApprovedFollowup) -> str:
    text = " ".join(followup.text.split())
    title = f"Follow up future review note: {text}"
    return title[:120]


def _normalize_followup_key(text: str) -> str:
    key = re.sub(r"`([^`]+)`", r"\1", text)
    key = re.sub(r"\*\*([^*]+)\*\*", r"\1", key)
    key = re.sub(r"[_*#>]+", " ", key)
    key = re.sub(r"[^\w\s]+", " ", key.lower())
    return " ".join(key.split())


def _followup_heading_key(text: str) -> str | None:
    heading_match = re.match(r"^\s*\*\*(?P<title>[^*]+)\*\*\s*:?", text)
    if heading_match:
        return _normalize_followup_key(heading_match.group("title"))
    first_clause = re.split(r"\s+-\s+|:\s+", text, maxsplit=1)[0]
    if first_clause != text and 3 <= len(first_clause.split()) <= 12:
        return _normalize_followup_key(first_clause)
    return None


def _dedupe_approved_followups(followups: Sequence[ApprovedFollowup]) -> list[GroupedApprovedFollowup]:
    grouped: list[GroupedApprovedFollowup] = []
    indexes: dict[str, int] = {}
    for followup in followups:
        keys = [_normalize_followup_key(followup.text)]
        heading_key = _followup_heading_key(followup.text)
        if heading_key:
            keys.append(heading_key)
        existing_index = next((indexes[key] for key in keys if key in indexes), None)
        if existing_index is None:
            indexes.update((key, len(grouped)) for key in keys if key)
            grouped.append(GroupedApprovedFollowup(text=followup.text, items=(followup,)))
            continue
        existing = grouped[existing_index]
        grouped[existing_index] = GroupedApprovedFollowup(
            text=existing.text,
            items=(*existing.items, followup),
        )
        indexes.update((key, existing_index) for key in keys if key)
    return grouped


def _followup_issue_body(pr_number: int, followup: GroupedApprovedFollowup) -> str:
    lines = [
        f"Future follow-up from approved review on PR #{pr_number}.",
        "",
    ]
    reviewers = followup.reviewers
    if len(reviewers) == 1:
        lines.append(f"Reviewer: {reviewers[0]}")
    else:
        lines.append("Reviewers:")
        lines.extend(f"- {reviewer}" for reviewer in reviewers)
    lines.extend(
        [
            "",
            "Follow-up:",
            f"- {followup.text}",
        ]
    )
    if len(followup.items) > 1:
        lines.extend(["", "Original reviewer notes:"])
        lines.extend(f"- {item.reviewer}: {item.text}" for item in followup.items)
    lines.extend(
        [
            "",
            "This was mentioned in an approved review as future work and did not block merge readiness.",
        ]
    )
    return "\n".join(lines)


def _create_approved_followup_issues(
    runner: Runner,
    *,
    config: AgentLoopConfig,
    pr_number: int,
    followups: list[ApprovedFollowup],
) -> tuple[list[str], int]:
    issue_urls: list[str] = []
    deduped_followups = _dedupe_approved_followups(followups)
    selected_followups = deduped_followups[:MAX_APPROVED_FOLLOWUP_ISSUES]
    for followup in selected_followups:
        issue_url = create_issue(
            runner,
            config=config,
            title=_followup_issue_title(
                ApprovedFollowup(reviewer=followup.reviewers[0], text=followup.text)
            ),
            body=_followup_issue_body(pr_number, followup),
        )
        if issue_url is not None:
            issue_urls.append(issue_url)
    skipped_count = len(deduped_followups) - len(selected_followups)
    return issue_urls, skipped_count


def _format_created_followup_issue_summary(
    pr_number: int,
    issue_urls: list[str],
    skipped_count: int,
) -> str:
    unique_issue_urls = list(dict.fromkeys(issue_urls))
    lines = [
        f"Created approved-review future follow-up issues for PR #{pr_number}:",
        "",
    ]
    if unique_issue_urls:
        lines.extend(f"- {issue_url}" for issue_url in unique_issue_urls)
    else:
        lines.append("- Created issue URL unavailable from GitHub CLI output.")
    lines.extend(
        [
            "",
            "These were mentioned in approved reviews as future work and did not block merge readiness.",
        ]
    )
    if skipped_count > 0:
        lines.extend(
            [
                "",
                f"Skipped {skipped_count} additional item(s) to avoid issue noise; reviewers should reserve "
                "this section for substantial independent follow-up work.",
            ]
        )
    lines.extend(["", "-- coding-review-agent-loop"])
    return "\n".join(lines)


def _publish_approved_followups(
    runner: Runner,
    *,
    config: AgentLoopConfig,
    pr_number: int,
    head_sha: str | None,
    pr_comments: Sequence[object],
    followups: list[ApprovedFollowup],
) -> bool:
    if not followups or config.approved_followups == "ignore":
        return False

    if config.approved_followups in ("summarize", "fix-and-summarize"):
        mode = "summarize"
        if _has_approved_followups_marker(
            pr_comments,
            pr_number=pr_number,
            head_sha=head_sha,
            mode=mode,
        ):
            log(
                config,
                f"Approved-review future follow-ups already recorded for PR #{pr_number} at {head_sha or 'unknown'} ({mode})",
            )
            return False
        body = _format_approved_followup_summary(pr_number, followups)
        body = _append_approved_followups_marker(
            body,
            pr_number=pr_number,
            head_sha=head_sha,
            mode=mode,
        )
        post_pr_comment(runner, config=config, pr_number=pr_number, body=body)
        return True

    if config.approved_followups in ("issue", "fix-and-issue"):
        mode = "issue"
        if _has_approved_followups_marker(
            pr_comments,
            pr_number=pr_number,
            head_sha=head_sha,
            mode=mode,
        ):
            log(
                config,
                f"Approved-review future follow-ups already recorded for PR #{pr_number} at {head_sha or 'unknown'} ({mode})",
            )
            return False
        issue_urls, skipped_count = _create_approved_followup_issues(
            runner,
            config=config,
            pr_number=pr_number,
            followups=followups,
        )
        if issue_urls:
            body = _format_created_followup_issue_summary(pr_number, issue_urls, skipped_count)
            body = _append_approved_followups_marker(
                body,
                pr_number=pr_number,
                head_sha=head_sha,
                mode=mode,
            )
            post_pr_comment(runner, config=config, pr_number=pr_number, body=body)
            return True
    return False


def _format_same_pr_followups(followups: Sequence[ApprovedFollowup]) -> str:
    lines: list[str] = []
    for followup in followups:
        lines.append(f"{followup.reviewer} same-PR follow-up:")
        lines.append(f"- {followup.text}")
        lines.append("")
    return "\n".join(lines).strip()


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


def _format_same_pr_unresolved_items(items: Sequence[UnresolvedReviewItem]) -> str:
    lines: list[str] = []
    for item in items:
        lines.append(
            f"{item.reviewer} same-PR follow-up [{item.item_id}] from round {item.source_round}:"
        )
        lines.append(f"- {item.text}")
        if item.notes:
            lines.append("Latest reviewer updates:")
            lines.extend(f"- {note}" for note in item.notes)
        lines.append("")
    return "\n".join(lines).strip()


def _format_unresolved_items_for_coder(items: Sequence[UnresolvedReviewItem]) -> str:
    lines: list[str] = []
    for item in items:
        lines.append(
            f"{item.reviewer} unresolved {item.status} item [{item.item_id}] from round {item.source_round}:"
        )
        lines.append(f"- {item.text}")
        if item.notes:
            lines.append("Latest reviewer updates:")
            lines.extend(f"- {note}" for note in item.notes)
        lines.append("")
    return "\n".join(lines).strip()


def _format_plan_approval_summary_with_followups(
    issue_number: int,
    approved_plan: str,
    future_followups: Sequence[ApprovedFollowup],
) -> str:
    lines = [
        f"Planning complete for issue #{issue_number}.",
        "",
        "Outcome: implement",
        "",
        "Approved plan:",
        "",
        approved_plan,
    ]
    if future_followups:
        lines.extend(["", "Approved plan future follow-ups:", ""])
        lines.extend(f"- {followup.text} ({followup.reviewer})" for followup in future_followups)
    lines.extend(["", "-- coding-review-agent-loop"])
    return "\n".join(lines)


def _format_pr_checks_comment(pr_number: int, state: str, details: list[str]) -> str:
    headline = {
        "failing": f"GitHub PR checks are failing for PR #{pr_number}.",
        "pending": f"GitHub PR checks are still pending for PR #{pr_number}.",
        "unavailable": f"GitHub PR check status is unavailable for PR #{pr_number}.",
    }[state]
    lines = [
        headline,
        "",
        "Reviewer approvals do not make this PR merge-ready until GitHub PR checks are green, or the PR explicitly states that only a local subset passed.",
        "",
    ]
    lines.extend(f"- {detail}" for detail in details)
    lines.extend(["", "-- coding-review-agent-loop"])
    return "\n".join(lines)


def _pr_check_blocking_review(pr_number: int, state: str, details: list[str]) -> str:
    headline = {
        "failing": "GitHub PR checks are failing and must be resolved before approval.",
        "pending": "GitHub PR checks are still pending, so this PR cannot be treated as merge-ready yet.",
        "unavailable": "GitHub PR check status is unavailable, so merge readiness cannot be confirmed yet.",
    }[state]
    lines = [headline, ""]
    lines.extend(f"- {detail}" for detail in details)
    lines.extend(
        [
            "",
            "Do not claim global test success unless GitHub PR checks are green. If only local tests passed, say that explicitly.",
        ]
    )
    return "\n".join(lines)


def _pr_check_details(pr_checks: PullRequestChecks) -> list[str]:
    details: list[str] = []
    if pr_checks.required_checks:
        details.append(f"Required checks: {', '.join(pr_checks.required_checks)}")
    if pr_checks.failing:
        details.append(
            "Failing checks: "
            + ", ".join(f"{check.name} ({check.status})" for check in pr_checks.failing)
        )
    if pr_checks.pending:
        details.append(
            "Pending checks: "
            + ", ".join(f"{check.name} ({check.status})" for check in pr_checks.pending)
        )
    if pr_checks.missing_required:
        details.append(
            "Required checks not yet reporting: " + ", ".join(pr_checks.missing_required)
        )
    if pr_checks.branch_protection_note:
        details.append(pr_checks.branch_protection_note)
    if not details:
        details.append("No individual check names were available from the GitHub API.")
    return details


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

    log(config, f"Planning issue #{issue_number}: invoking {coder_name}")
    plan_response = _run_validated_agent(
        runner,
        agent=config.coder,
        config=config,
        prompt=build_issue_plan_prompt(issue_number, config, memory, issue_context=issue_context),
        marker_description="<!-- AGENT_PLAN_STATE: approved|blocking --> or <!-- AGENT_CLARIFY -->",
        validate=_require_plan_state_or_clarification,
        usage_context=usage_context,
    )
    plan_output = plan_response.text
    coder_session_id = plan_response.session_id
    if is_clarification_request(plan_output):
        raise AgentLoopError(
            f"{coder_name} requested clarification during planning; human intervention required.\n\n"
            f"{coder_name}'s questions:\n{plan_output}"
        )
    post_issue_comment(runner, config=config, issue_number=issue_number, body=plan_output)
    current_plan = plan_output

    for round_number in range(1, config.max_rounds + 1):
        prior_unresolved_items = tuple(unresolved_items)
        prior_dispositions: dict[str, list[ReviewItemDisposition]] = {
            item.item_id: [] for item in prior_unresolved_items
        }
        round_new_unresolved_items: list[UnresolvedReviewItem] = []
        round_approved_future_followups: list[ApprovedFollowup] = []
        blocking_reviews: list[tuple[str, str]] = []
        all_approved = True
        for reviewer in configured_reviewers:
            reviewer_name = agent_display_name(reviewer)
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
            )
            review_output = review_response.text
            reviewer_session_ids[reviewer] = review_response.session_id
            parsed_review = review_response.marker_value
            assert isinstance(parsed_review, ParsedPlanReview)
            review_state = parsed_review.state
            post_issue_comment(runner, config=config, issue_number=issue_number, body=review_output)
            log(
                config,
                "Planning round "
                f"{round_number}: {reviewer_name} outcome is {_describe_plan_review_outcome(parsed_review)}",
            )
            for disposition in parsed_review.dispositions:
                prior_dispositions[disposition.item_id].append(disposition)
            if review_state == "blocking":
                all_approved = False
                blocking_reviews.append((reviewer_name, review_output))
            for item in parsed_review.items.blocking:
                round_new_unresolved_items.append(
                    _next_unresolved_item(
                        item_number=next_unresolved_item_number,
                        reviewer=item.reviewer,
                        source_round=round_number,
                        text=item.text,
                        status="blocking",
                    )
                )
                next_unresolved_item_number += 1
            for item in parsed_review.items.same_plan:
                round_new_unresolved_items.append(
                    _next_unresolved_item(
                        item_number=next_unresolved_item_number,
                        reviewer=item.reviewer,
                        source_round=round_number,
                        text=item.text,
                        status="same-plan",
                    )
                )
                next_unresolved_item_number += 1
            for item in parsed_review.items.future:
                round_approved_future_followups.append(item)

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
                validate=_require_pr_number,
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
            post_pr_comment(runner, config=config, pr_number=pr_number, body=coder_output)
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
            validate=parse_plan_state,
            usage_context=usage_context,
        )
        current_plan = plan_response.text
        coder_session_id = plan_response.session_id
        post_issue_comment(runner, config=config, issue_number=issue_number, body=current_plan)

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
            validate=_require_pr_number,
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

        post_pr_comment(runner, config=config, pr_number=pr_number, body=coder_output)
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
                post_pr_comment(runner, config=config, pr_number=pr_number, body=coder_output)
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
        if reviewer_session_id is not None and configured_reviewers:
            # Backward-compatible single-reviewer resume support: older callers
            # pass one reviewer session, so attach it to the first configured reviewer.
            reviewer_session_ids[configured_reviewers[0]] = reviewer_session_id
        for round_number in range(1, config.max_rounds + 1):
            coder_name = agent_display_name(config.coder)
            if pre_review_test_pending:
                run_pre_review_tests(runner, config)
                pre_review_test_pending = False
            pr_context = get_pr_review_context(runner, config=config, pr_number=pr_number)
            pr_metadata = pr_context.metadata
            pr_comments = pr_context.comments
            human_requirements = pr_context.human_requirements
            unresolved_items = _reconcile_human_requirements_ack_item(
                unresolved_items,
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
            for reviewer in configured_reviewers:
                reviewer_name = agent_display_name(reviewer)
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
                )
                review_output = review_response.text
                reviewer_session_ids[reviewer] = review_response.session_id
                parsed_review = review_response.marker_value
                assert isinstance(parsed_review, ParsedReview)
                review_state = parsed_review.state

                post_pr_comment(runner, config=config, pr_number=pr_number, body=review_output)
                for disposition in parsed_review.dispositions:
                    prior_dispositions[disposition.item_id].append(disposition)
                blocking_summary = _review_freeform_summary_text(review_output)
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
                    if has_blocking_summary:
                        round_new_unresolved_items.append(
                            _next_unresolved_item(
                                item_number=next_unresolved_item_number,
                                reviewer=reviewer_name,
                                source_round=round_number,
                                text=blocking_summary,
                                status="blocking",
                            )
                        )
                        next_unresolved_item_number += 1
                    if parsed_review.followups.same_pr:
                        if config.approved_followups.startswith("fix-and-"):
                            for followup in parsed_review.followups.same_pr:
                                round_new_unresolved_items.append(
                                    _next_unresolved_item(
                                        item_number=next_unresolved_item_number,
                                        reviewer=followup.reviewer,
                                        source_round=round_number,
                                        text=followup.text,
                                        status="same-pr",
                                    )
                                )
                                next_unresolved_item_number += 1
                        else:
                            round_new_unresolved_items.append(
                                _next_unresolved_item(
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
                            )
                            next_unresolved_item_number += 1
                    continue

                approved_review_outputs.append((reviewer_name, review_output))
                if config.approved_followups != "ignore":
                    for followup in parsed_review.followups.future:
                        round_new_unresolved_items.append(
                            _next_unresolved_item(
                                item_number=next_unresolved_item_number,
                                reviewer=followup.reviewer,
                                source_round=round_number,
                                text=followup.text,
                                status="future",
                            )
                        )
                        next_unresolved_item_number += 1

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
                        raise AgentLoopError(
                            "Signed human reviewer requirements remain unresolved or "
                            "unacknowledged by approved reviewer response(s): "
                            f"{', '.join(missing_acknowledgements)}. "
                            "Human intervention is required."
                        )
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
                validate=parse_agent_state,
                usage_context=usage_context,
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

            post_pr_comment(runner, config=config, pr_number=pr_number, body=coder_output)
            log(config, f"Round {round_number}: {coder_name} pushed updates for re-review")
            pre_review_test_pending = True

        raise AgentLoopError(
            f"Reached max rounds ({config.max_rounds}) for PR #{pr_number}; human review required."
        )
    finally:
        if owned_usage_context:
            _persist_usage_summary(config, usage_context)
