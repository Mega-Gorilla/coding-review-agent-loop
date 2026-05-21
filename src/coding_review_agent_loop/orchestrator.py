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
    create_issue,
    get_issue_context,
    get_pr_review_context,
    merge_pr,
    post_issue_comment,
    post_pr_comment,
    validate_open_issue,
    validate_open_pr,
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
)
from .protocol import (
    human_requirements_resolved,
    is_clarification_request,
    parse_agent_state,
    parse_plan_state,
    parse_pr_number,
)
from .protocol import ApprovedFollowup, parse_approved_followups
from .runner import Runner
from .usage import RunUsageContext, UsageMetadata, estimate_usage
from .workdirs import active_workdir

MAX_APPROVED_FOLLOWUP_ISSUES = 3

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


def run_optional_tests(runner: Runner, config: AgentLoopConfig) -> None:
    if not config.test_command:
        return
    log(config, f"Running local test command: {' '.join(config.test_command)}")
    runner.run(config.test_command, cwd=active_workdir(config))
    log(config, "Local test command passed")


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


def _format_same_pr_followups(followups: Sequence[ApprovedFollowup]) -> str:
    lines: list[str] = []
    for followup in followups:
        lines.append(f"{followup.reviewer} same-PR follow-up:")
        lines.append(f"- {followup.text}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_plan_approval_summary(issue_number: int, approved_plan: str) -> str:
    return "\n".join(
        [
            f"Planning complete for issue #{issue_number}.",
            "",
            "Outcome: implement",
            "",
            "Approved plan:",
            "",
            approved_plan,
            "",
            "-- coding-review-agent-loop",
        ]
    )


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
        blocking_reviews: list[tuple[str, str]] = []
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
                ),
                session_id=reviewer_session_ids.get(reviewer),
                marker_description="<!-- AGENT_PLAN_STATE: approved|blocking -->",
                validate=parse_plan_state,
                usage_context=usage_context,
            )
            review_output = review_response.text
            reviewer_session_ids[reviewer] = review_response.session_id
            review_state = str(review_response.marker_value)
            post_issue_comment(runner, config=config, issue_number=issue_number, body=review_output)
            log(config, f"Planning round {round_number}: {reviewer_name} state is {review_state}")
            if review_state == "blocking":
                blocking_reviews.append((reviewer_name, review_output))

        if not blocking_reviews:
            post_issue_comment(
                runner,
                config=config,
                issue_number=issue_number,
                body=_format_plan_approval_summary(issue_number, current_plan),
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
            post_pr_comment(runner, config=config, pr_number=pr_number, body=coder_output)
            return run_pr_loop(
                runner,
                pr_number=pr_number,
                config=config,
                coder_session_id=coder_session_id,
                issue_context=issue_context,
                workdirs_ready=True,
                usage_context=usage_context,
            )

        if round_number == config.max_rounds:
            raise AgentLoopError(
                f"One or more reviewers still reported blocking plan issues after "
                f"round {round_number}; human review required."
            )

        combined_review = "\n\n".join(
            f"{name} plan review:\n\n{review}" for name, review in blocking_reviews
        )
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

        post_pr_comment(runner, config=config, pr_number=pr_number, body=coder_output)
        return run_pr_loop(
            runner,
            pr_number=pr_number,
            config=config,
            coder_session_id=coder_session_id,
            issue_context=issue_context,
            workdirs_ready=True,
            usage_context=usage_context,
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
        if reviewer_session_id is not None and configured_reviewers:
            # Backward-compatible single-reviewer resume support: older callers
            # pass one reviewer session, so attach it to the first configured reviewer.
            reviewer_session_ids[configured_reviewers[0]] = reviewer_session_id
        for round_number in range(1, config.max_rounds + 1):
            coder_name = agent_display_name(config.coder)
            blocking_reviews: list[tuple[str, str]] = []
            same_pr_followups: list[ApprovedFollowup] = []
            round_future_followups: list[ApprovedFollowup] = []
            approved_review_outputs: list[tuple[str, str]] = []
            pr_context = get_pr_review_context(runner, config=config, pr_number=pr_number)
            pr_metadata = pr_context.metadata
            human_requirements = pr_context.human_requirements
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
                        memory=memory,
                        issue_context=issue_context,
                        human_requirements=human_requirements,
                    ),
                    session_id=reviewer_session_ids.get(reviewer),
                    marker_description="<!-- AGENT_STATE: approved|blocking -->",
                    validate=parse_agent_state,
                    usage_context=usage_context,
                )
                review_output = review_response.text
                reviewer_session_ids[reviewer] = review_response.session_id
                review_state = str(review_response.marker_value)

                post_pr_comment(runner, config=config, pr_number=pr_number, body=review_output)
                log(config, f"Round {round_number}: {reviewer_name} state is {review_state}")
                if review_state == "blocking":
                    blocking_reviews.append((reviewer_name, review_output))
                    continue

                approved_review_outputs.append((reviewer_name, review_output))
                if config.approved_followups != "ignore":
                    followups = parse_approved_followups(review_output, reviewer=reviewer_name)
                    round_future_followups.extend(followups.future)
                    if followups.same_pr:
                        if config.approved_followups.startswith("fix-and-"):
                            same_pr_followups.extend(followups.same_pr)
                        else:
                            blocking_reviews.append(
                                (
                                    reviewer_name,
                                    "\n".join(
                                        [
                                            "Approved review included Same-PR follow-ups, "
                                            f"but --approved-followups={config.approved_followups} "
                                            "does not enable a same-PR fix path.",
                                            "",
                                            _format_same_pr_followups(followups.same_pr),
                                        ]
                                    ),
                                )
                            )

            if not blocking_reviews and not same_pr_followups:
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
                    blocking_reviews.append(
                        ("Alembic migration validation", migration_validation.message or "")
                    )
                if not blocking_reviews:
                    approved_followups = round_future_followups
                    if (
                        config.approved_followups in ("summarize", "fix-and-summarize")
                        and approved_followups
                    ):
                        body = _format_approved_followup_summary(pr_number, approved_followups)
                        post_pr_comment(runner, config=config, pr_number=pr_number, body=body)
                    elif config.approved_followups in ("issue", "fix-and-issue") and approved_followups:
                        issue_urls, skipped_count = _create_approved_followup_issues(
                            runner,
                            config=config,
                            pr_number=pr_number,
                            followups=approved_followups,
                        )
                        if issue_urls:
                            body = _format_created_followup_issue_summary(
                                pr_number, issue_urls, skipped_count
                            )
                            post_pr_comment(runner, config=config, pr_number=pr_number, body=body)
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

            if same_pr_followups and not blocking_reviews:
                combined_review = _format_same_pr_followups(same_pr_followups)
                followup_prompt = build_same_pr_followup_prompt(
                    pr_number,
                    round_number,
                    combined_review,
                    config,
                    memory,
                    issue_context=issue_context,
                    human_requirements=human_requirements,
                )
            else:
                # Discard future-work suggestions from non-final rounds so reviewers
                # can restate still-relevant items after the PR has been updated.
                if same_pr_followups:
                    blocking_reviews.append(
                        (
                            "Approved same-PR follow-ups",
                            _format_same_pr_followups(same_pr_followups),
                        )
                    )
                combined_review = "\n\n".join(
                    f"{name} review:\n\n{review}" for name, review in blocking_reviews
                )
                followup_prompt = build_followup_prompt(
                    pr_number,
                    round_number,
                    combined_review,
                    config,
                    memory,
                    issue_context=issue_context,
                    human_requirements=human_requirements,
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

            post_pr_comment(runner, config=config, pr_number=pr_number, body=coder_output)
            log(config, f"Round {round_number}: {coder_name} pushed updates for re-review")

        raise AgentLoopError(
            f"Reached max rounds ({config.max_rounds}) for PR #{pr_number}; human review required."
        )
    finally:
        if owned_usage_context:
            _persist_usage_summary(config, usage_context)
