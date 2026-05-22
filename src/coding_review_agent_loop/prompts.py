"""Prompt builders for coder and reviewer agent turns."""

from __future__ import annotations

from textwrap import indent
from typing import Sequence

from .agents.base import AgentName
from .agents.registry import agent_display_name, agent_signature
from .config import AgentLoopConfig, reviewers
from .github import HumanReviewRequirement, IssueContext, PullRequestChecks, PullRequestMetadata
from .memory import AgentMemoryContext, format_agent_memory_context
from .protocol import UnresolvedReviewItem


def format_agent_list(agents: Sequence[AgentName]) -> str:
    names = [agent_display_name(agent) for agent in agents]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def _memory_block(memory: AgentMemoryContext | None) -> str:
    text = format_agent_memory_context(memory)
    if not text:
        return ""
    return f"Agent memory context:\n{text}\n"


def _scratch_file_guidance() -> str:
    return (
        "If you need temporary scratch files while inspecting diffs or command output, "
        "write them outside the repository checkout, for example under "
        "/tmp/coding-review-agent-loop/scratch/. Do not create temporary files in the "
        "repo worktree unless they are intended project changes.\n"
    )


def _coder_test_reporting_guidance() -> str:
    return (
        "Before opening or updating the PR, run the relevant tests you can run "
        "locally. In your final response, include a short `Tests:` line listing "
        "the exact commands run and whether they passed. If you cannot run tests, "
        "explain why in that `Tests:` line.\n"
    )


def _truncate_issue_text(text: str, *, max_chars: int, label: str) -> str:
    if len(text) <= max_chars:
        return text
    suffix = f"\n\n[{label} truncated to keep this prompt bounded.]"
    return text[: max(0, max_chars - len(suffix))].rstrip() + suffix


def format_issue_context(issue_context: IssueContext, *, max_chars: int = 24_000) -> str:
    raw_body = issue_context.body if issue_context.body else "(none)"
    body = _truncate_issue_text(raw_body, max_chars=max_chars // 3, label="Issue body")
    title = issue_context.title if issue_context.title else "(unknown)"
    lines = [
        f"GitHub issue #{issue_context.number}",
        "",
        "Title:",
        title,
        "",
        "Body:",
        body,
        "",
        "Comments, oldest to newest:",
    ]
    if issue_context.comments:
        comments = [
            "\n".join(
                [
                    "",
                    (
                        f"Comment by {comment.author or '(unknown)'} "
                        f"at {comment.created_at or '(unknown time)'}:"
                    ),
                    comment.body if comment.body else "(none)",
                ]
            )
            for comment in issue_context.comments
        ]
        issue_header = "\n".join(lines)
        full_text = issue_header + "\n".join(comments)
        if len(full_text) <= max_chars:
            return full_text

        kept_comments: list[str] = []
        omitted_count = 0
        for comment_text in reversed(comments):
            candidate_comments = [comment_text, *kept_comments]
            notice = (
                "\n\n"
                f"Older comments omitted: {len(comments) - len(candidate_comments)} "
                "comment(s) were omitted to keep this prompt bounded. "
                "Newest comments were kept preferentially."
            )
            candidate = issue_header + notice + "".join(candidate_comments)
            if len(candidate) <= max_chars:
                kept_comments = candidate_comments
                omitted_count = len(comments) - len(candidate_comments)
            elif not kept_comments:
                omitted_count = len(comments) - 1
                notice = (
                    "\n\n"
                    f"Older comments omitted: {omitted_count} comment(s) were omitted to keep "
                    "this prompt bounded. Newest comments were kept preferentially."
                )
                prefix = issue_header + notice
                remaining_chars = max_chars - len(prefix)
                if remaining_chars > 0:
                    truncated_comment = _truncate_issue_text(
                        comment_text,
                        max_chars=remaining_chars,
                        label="Newest comment",
                    )
                    if len(prefix) + len(truncated_comment) <= max_chars:
                        return prefix + truncated_comment
                omitted_count = len(comments)
                break
            else:
                break
        if kept_comments:
            return (
                issue_header
                + "\n\n"
                + f"Older comments omitted: {omitted_count} comment(s) were omitted to keep "
                "this prompt bounded. Newest comments were kept preferentially."
                + "".join(kept_comments)
            )
        return (
            issue_header
            + "\n\n"
            + f"All {omitted_count} comment(s) were omitted to keep this prompt bounded. "
            "Fetch the issue discussion directly if those details are needed."
        )
    return "\n".join([*lines, "", "(none)"])


def format_human_requirements(
    human_requirements: Sequence[HumanReviewRequirement],
    *,
    max_chars: int = 12_000,
) -> str:
    if not human_requirements:
        return ""

    header = "\n".join(
        [
            "Signed Human Reviewer Requirements",
            "",
            "Treat these signed human reviewer comments as high-priority PR requirements. "
            "Later human comments may supersede earlier ones; the latest human instruction wins. "
            "If a requirement is unsafe, impossible, or contradicted by a later human instruction, "
            "explain that explicitly instead of ignoring it.",
        ]
    )
    entries = [
        "\n".join(
            [
                "",
                f"Requirement {index}:",
                f"- Source: {requirement.source_type}",
                f"- Author: {requirement.author or '(unknown)'}",
                f"- Created: {requirement.created_at or '(unknown time)'}",
                f"- URL: {requirement.url or '(unavailable)'}",
                "",
                requirement.body,
            ]
        )
        for index, requirement in enumerate(human_requirements, start=1)
    ]
    full_text = header + "\n".join(entries)
    if len(full_text) <= max_chars:
        return full_text

    kept_entries: list[str] = []
    omitted_count = 0
    for entry in reversed(entries):
        candidate_entries = [entry, *kept_entries]
        notice = (
            "\n\n"
            f"Older signed human requirement(s) omitted: {len(entries) - len(candidate_entries)}. "
            "Newest requirements were kept preferentially."
        )
        candidate = header + notice + "\n".join(candidate_entries)
        if len(candidate) <= max_chars:
            kept_entries = candidate_entries
            omitted_count = len(entries) - len(candidate_entries)
        elif not kept_entries:
            omitted_count = len(entries) - 1
            notice = (
                "\n\n"
                f"Older signed human requirement(s) omitted: {omitted_count}. "
                "Newest requirements were kept preferentially."
            )
            prefix = header + notice
            remaining_chars = max_chars - len(prefix)
            if remaining_chars > 0:
                truncated_entry = _truncate_issue_text(
                    entry,
                    max_chars=remaining_chars,
                    label="Newest signed human requirement",
                )
                if len(prefix) + len(truncated_entry) <= max_chars:
                    return prefix + truncated_entry
            omitted_count = len(entries)
            break
        else:
            break
    if kept_entries:
        return (
            header
            + "\n\n"
            + f"Older signed human requirement(s) omitted: {omitted_count}. "
            "Newest requirements were kept preferentially."
            + "\n".join(kept_entries)
        )
    return (
        header
        + "\n\n"
        + f"All {omitted_count} signed human requirement(s) were omitted to keep this prompt bounded. "
        "Fetch the PR discussion directly before approving."
    )


def _human_requirements_block(
    human_requirements: Sequence[HumanReviewRequirement] | None,
) -> str:
    if not human_requirements:
        return ""
    return f"{format_human_requirements(human_requirements)}\n"


def _human_requirements_review_guidance(
    human_requirements: Sequence[HumanReviewRequirement] | None,
) -> str:
    if not human_requirements:
        return ""
    return """Signed human reviewer requirements override AI reviewer preferences unless they
are unsafe, impossible, or contradicted by a later signed human instruction.
Verify every signed human reviewer requirement before approving. If all signed
human reviewer requirements are addressed or explicitly resolved, an approved
review must include exactly:

<!-- HUMAN_REQUIREMENTS_RESOLVED -->

If any signed human reviewer requirement is unresolved, return blocking.
"""


def _format_unresolved_review_items(unresolved_items: Sequence[UnresolvedReviewItem] | None) -> str:
    if not unresolved_items:
        return ""
    lines = [
        "Prior unresolved review items from earlier rounds",
        "",
        "Explicitly evaluate every item below before approving. Use the item IDs exactly as written.",
        "",
    ]
    for item in unresolved_items:
        details = [indent(item.text, "  ")]
        if item.notes:
            details.append("  Latest reviewer updates:")
            details.extend(f"  - {note}" for note in item.notes)
        lines.extend(
            [
                f"- [{item.item_id}] {item.status} from {item.reviewer} in round {item.source_round}",
                *details,
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _format_unresolved_plan_items(unresolved_items: Sequence[UnresolvedReviewItem] | None) -> str:
    if not unresolved_items:
        return ""
    lines = [
        "Prior unresolved plan items from earlier rounds",
        "",
        "Explicitly evaluate every item below before approving. Use the item IDs exactly as written.",
        "",
    ]
    for item in unresolved_items:
        details = [indent(item.text, "  ")]
        if item.notes:
            details.append("  Latest reviewer updates:")
            details.extend(f"  - {note}" for note in item.notes)
        lines.extend(
            [
                f"- [{item.item_id}] {item.status} from {item.reviewer} in round {item.source_round}",
                *details,
            ]
        )
    lines.append("")
    return "\n".join(lines)


def format_pr_checks(checks: PullRequestChecks) -> str:
    lines = [
        "GitHub PR checks:",
        f"- Overall state: {checks.state}",
    ]
    if checks.required_checks:
        lines.append(f"- Required checks: {', '.join(checks.required_checks)}")
    if checks.failing:
        lines.append(
            "- Failing checks: "
            + ", ".join(f"{check.name} ({check.status})" for check in checks.failing)
        )
    if checks.pending:
        lines.append(
            "- Pending checks: "
            + ", ".join(f"{check.name} ({check.status})" for check in checks.pending)
        )
    if checks.missing_required:
        lines.append(f"- Required checks not yet reporting: {', '.join(checks.missing_required)}")
    if checks.branch_protection_note:
        lines.append(f"- Branch protection: {checks.branch_protection_note}")
    return "\n".join(lines)


def _issue_context_block(issue_context: IssueContext | None) -> str:
    if issue_context is None:
        return ""
    return (
        "Issue context from GitHub. Later comments may refine or supersede the "
        "original issue body:\n"
        f"{format_issue_context(issue_context)}\n"
    )


def build_issue_prompt(
    issue_number: int,
    config: AgentLoopConfig,
    memory: AgentMemoryContext | None = None,
    issue_context: IssueContext | None = None,
) -> str:
    reviewer_name = format_agent_list(reviewers(config))
    coder_signature = agent_signature(config.coder)
    return f"""Fix GitHub issue #{issue_number} in {config.repo}.

Use this local checkout as your workspace. Create a branch, implement the fix,
run relevant tests, commit, push, and open a pull request against {config.base}.
{_scratch_file_guidance()}
{_coder_test_reporting_guidance()}
{_issue_context_block(issue_context)}
{_memory_block(memory)}

Do not wait for {reviewer_name} yourself; this local orchestrator will run {reviewer_name} after
you create the PR. In your final response, include the PR number using exactly
this marker:

<!-- AGENT_PR: <number> -->

Also include exactly one state marker:

<!-- AGENT_STATE: blocking -->

Use blocking here to hand the PR to {reviewer_name} for review. Sign the response as:
-- {coder_signature}
"""


def build_issue_plan_prompt(
    issue_number: int,
    config: AgentLoopConfig,
    memory: AgentMemoryContext | None = None,
    issue_context: IssueContext | None = None,
) -> str:
    reviewer_name = format_agent_list(reviewers(config))
    coder_signature = agent_signature(config.coder)
    return f"""Plan GitHub issue #{issue_number} in {config.repo}.

Use this local checkout only to inspect context. Do not edit files, create a
branch, commit, push, or open a pull request during this planning stage.
Write a concise implementation plan covering the intended approach, key files or
areas to change, edge cases, and test strategy.
{_scratch_file_guidance()}
{_issue_context_block(issue_context)}
{_memory_block(memory)}

Do not wait for {reviewer_name} yourself; this local orchestrator will run
{reviewer_name} to review the plan. End your final response with exactly one
planning marker:

<!-- AGENT_PLAN_STATE: blocking -->

Use blocking here to hand the plan to {reviewer_name} for review. If the issue
is materially ambiguous before a useful plan can be written, ask focused
clarifying questions and end with exactly this marker:

<!-- AGENT_CLARIFY -->

Sign the response as:
-- {coder_signature}
"""


def build_plan_review_prompt(
    issue_number: int,
    round_number: int,
    plan: str,
    config: AgentLoopConfig,
    *,
    reviewer: AgentName,
    memory: AgentMemoryContext | None = None,
    issue_context: IssueContext | None = None,
    unresolved_items: Sequence[UnresolvedReviewItem] = (),
) -> str:
    coder_name = agent_display_name(config.coder)
    reviewer_signature = agent_signature(reviewer)
    reviewer_group = format_agent_list(reviewers(config))
    unresolved_items_block = _format_unresolved_plan_items(unresolved_items)
    if unresolved_items:
        unresolved_items_guidance = """Because prior unresolved plan items exist, include this exact section in your review:

### Prior unresolved plan item dispositions

Use one bullet per listed item and cover every item exactly once. Allowed forms:
- [item-id] resolved
- [item-id] still blocking
- [item-id] same-plan
- [item-id] future follow-up: brief reason

Only use `future follow-up` when returning `approved`. If a current-plan item still needs to be fixed before implementation starts, keep it as `still blocking` or `same-plan` instead of downgrading it.
"""
    else:
        unresolved_items_guidance = ""
    return f"""Review the implementation plan for GitHub issue #{issue_number} in {config.repo} (planning round {round_number}).

Use this local checkout only to inspect context. Do not edit files, create a
branch, commit, push, or open a pull request during this planning review.
{_scratch_file_guidance()}
{_issue_context_block(issue_context)}
{unresolved_items_block}{_memory_block(memory)}

Plan from {coder_name}:

{plan}

Review the plan for correctness, architecture fit, missing edge cases, test
strategy, and ambiguity. Use this exact structured format in your response body:

### Blocking plan issues
- Required corrections that must be resolved before the plan can be approved.

### Same-plan follow-ups
- Required current-plan refinements that should be folded into the next plan revision before approval.

### Future follow-ups
- Ideas worth tracking separately that are not required for the current implementation plan.

Blocking plan issues and Same-plan follow-ups both prevent approval. Future
follow-ups are allowed only in approved plan reviews.
{unresolved_items_guidance}
Use blocking only when the current plan still has blocking plan issues or
same-plan follow-ups. All configured reviewers ({reviewer_group}) must approve
in the same planning round before implementation can proceed.

End your final response with exactly one planning marker:

<!-- AGENT_PLAN_STATE: approved -->

or:

<!-- AGENT_PLAN_STATE: blocking -->

Use approved only if there are no blocking plan issues. Always sign your response:
-- {reviewer_signature}
"""


def build_plan_revision_prompt(
    issue_number: int,
    round_number: int,
    previous_plan: str,
    review: str,
    config: AgentLoopConfig,
    memory: AgentMemoryContext | None = None,
    issue_context: IssueContext | None = None,
    unresolved_items: Sequence[UnresolvedReviewItem] = (),
) -> str:
    reviewer_name = format_agent_list(reviewers(config))
    coder_signature = agent_signature(config.coder)
    unresolved_items_block = _format_unresolved_plan_items(unresolved_items)
    return f"""{reviewer_name} reviewed the implementation plan for GitHub issue #{issue_number} in {config.repo} and found blocking issues.

Revise the plan in this local checkout without editing code. Do not create a
branch, commit, push, or open a pull request during this planning stage.
{_scratch_file_guidance()}
{_issue_context_block(issue_context)}
{unresolved_items_block}{_memory_block(memory)}

Previous plan:

{previous_plan}

{reviewer_name} plan review:

{review}

Revise the plan item by item instead of replying only with free-form prose. If
prior unresolved plan items are listed above, include this exact section in
your response and address each item ID exactly once:

### Prior plan review item dispositions
- [item-id] resolved: brief explanation of how the revised plan addresses it
- [item-id] still blocking: brief explanation if you cannot resolve it yet
- [item-id] same-plan: brief explanation of the remaining current-plan refinement
- [item-id] future: brief explanation only if a reviewer explicitly asked to move it to future work

Then provide the revised implementation plan with concrete file areas, edge
cases, and tests. Use `same-plan`, never `same-pr`, when describing plan-only
current-round refinements.

This is planning round {round_number}. End your final response with exactly one
planning marker:

<!-- AGENT_PLAN_STATE: blocking -->

Use blocking to hand the revised plan back to {reviewer_name}. Sign the response as:
-- {coder_signature}
"""


def build_issue_implementation_prompt(
    issue_number: int,
    approved_plan: str,
    config: AgentLoopConfig,
    memory: AgentMemoryContext | None = None,
    issue_context: IssueContext | None = None,
) -> str:
    reviewer_name = format_agent_list(reviewers(config))
    coder_signature = agent_signature(config.coder)
    return f"""Implement the approved plan for GitHub issue #{issue_number} in {config.repo}.

Use this local checkout as your workspace. Create a branch, implement the
approved plan, run relevant tests, commit, push, and open a pull request against
{config.base}.
{_scratch_file_guidance()}
{_coder_test_reporting_guidance()}
{_issue_context_block(issue_context)}
{_memory_block(memory)}

Approved implementation plan:

{approved_plan}

Do not wait for {reviewer_name} yourself; this local orchestrator will run
{reviewer_name} after you create the PR. In your final response, include the PR
number using exactly this marker:

<!-- AGENT_PR: <number> -->

Also include exactly one state marker:

<!-- AGENT_STATE: blocking -->

Use blocking here to hand the PR to {reviewer_name} for review. Sign the response as:
-- {coder_signature}
"""


def build_task_prompt(
    task_text: str,
    config: AgentLoopConfig,
    memory: AgentMemoryContext | None = None,
) -> str:
    reviewer_name = format_agent_list(reviewers(config))
    coder_signature = agent_signature(config.coder)
    return f"""You have been given a free-form task to implement in {config.repo}.

Task:
{task_text}
{_memory_block(memory)}

Use this local checkout as your workspace. Decide between two paths:

(a) If the task is clear enough to implement, create a branch, implement the
    change, run relevant tests, commit, push, and open a pull request against
    {config.base}. Do not wait for {reviewer_name}; this local orchestrator
    will run {reviewer_name} after you create the PR. End your final response
    with both markers:
{_scratch_file_guidance()}
{_coder_test_reporting_guidance()}

    <!-- AGENT_PR: <number> -->
    <!-- AGENT_STATE: blocking -->

(b) If the task is genuinely ambiguous or missing information that would change
    the implementation, do NOT write code. Instead, ask focused clarifying
    questions and end your final response with exactly this marker:

    <!-- AGENT_CLARIFY -->

Prefer (a) when reasonable assumptions can be documented in the PR description;
choose (b) only for material ambiguity. Sign your response as:
-- {coder_signature}
"""


def build_task_clarification_prompt(
    task_text: str,
    history: Sequence[tuple[str, str]],
    config: AgentLoopConfig,
    memory: AgentMemoryContext | None = None,
) -> str:
    coder_signature = agent_signature(config.coder)
    qa_blocks = "\n\n".join(
        f"Round {idx + 1} questions from you:\n{questions}\n\n"
        f"Round {idx + 1} answers from the user:\n{answers}"
        for idx, (questions, answers) in enumerate(history)
    )
    return f"""Continuing the previous free-form task in {config.repo}.

Original task:
{task_text}
{_memory_block(memory)}

Clarification so far:

{qa_blocks}

Now proceed. Strongly prefer to implement the task and open a PR. Only ask
again if a critical detail is still missing. Use the same response markers as
before:
{_scratch_file_guidance()}
{_coder_test_reporting_guidance()}

- For implementation: include both <!-- AGENT_PR: <number> --> and
  <!-- AGENT_STATE: blocking --> at the end of your final response.
- For another clarification round: end your final response with exactly
  <!-- AGENT_CLARIFY -->.

Sign your response as:
-- {coder_signature}
"""


def build_review_prompt(
    pr_number: int,
    round_number: int,
    config: AgentLoopConfig,
    *,
    reviewer: AgentName,
    pr_metadata: PullRequestMetadata | None = None,
    pr_checks: PullRequestChecks | None = None,
    memory: AgentMemoryContext | None = None,
    issue_context: IssueContext | None = None,
    human_requirements: Sequence[HumanReviewRequirement] | None = None,
    unresolved_items: Sequence[UnresolvedReviewItem] | None = None,
) -> str:
    coder_name = agent_display_name(config.coder)
    reviewer_signature = agent_signature(reviewer)
    reviewer_group = format_agent_list(reviewers(config))
    metadata = pr_metadata or PullRequestMetadata(
        number=pr_number,
        repo=config.repo,
        title=None,
        head_branch=None,
        base_branch=None,
        head_sha=None,
        url=None,
    )
    title = metadata.title or "(unknown)"
    head_branch = metadata.head_branch or "(unknown)"
    base_branch = metadata.base_branch or "(unknown)"
    head_sha = metadata.head_sha or "(unknown)"
    url_line = f"- URL: {metadata.url}\n" if metadata.url else ""
    checks_block = f"{format_pr_checks(pr_checks)}\n" if pr_checks is not None else ""
    human_requirements_guidance = _human_requirements_review_guidance(human_requirements)
    unresolved_items_block = _format_unresolved_review_items(unresolved_items)
    if unresolved_items:
        unresolved_items_guidance = """Because prior unresolved items exist, include this exact section in your review:

### Prior unresolved item dispositions

Use one bullet per listed item and cover every item exactly once. Allowed forms:
- [item-id] resolved
- [item-id] still blocking
- [item-id] same-pr
- [item-id] future follow-up: brief reason

Only use `future follow-up` when returning `approved`. If an item should still be fixed before merge, keep it as `still blocking` or `same-pr` instead of downgrading it.
"""
    else:
        unresolved_items_guidance = ""
    if config.approved_followups == "ignore":
        followup_guidance = """Do not include Same-PR follow-ups, Future follow-ups, or legacy
Non-blocking follow-ups sections in approved reviews; this run is configured to
ignore approved-review follow-up sections. Mark the review blocking instead
when cleanup should be fixed before merge.
"""
    elif config.approved_followups.startswith("fix-and-"):
        followup_guidance = f"""If you approve but notice small, localized, low-risk cleanup worth fixing
before merge, list those items under this exact heading:

### Same-PR follow-ups

Use Same-PR follow-ups only for narrow current-PR cleanup in files already
touched by this PR or directly adjacent code. Do not use this section for
larger redesigns, broad refactors, or independent future work.

If you approve but notice substantial work that is better handled separately in
a future issue or PR, list at most three highest-value items under this exact
heading:

### Future follow-ups

Same-PR follow-ups will be sent back to {coder_name} and require another review
round before final approval. Do not put trivial style nits in either follow-up
section.
"""
    else:
        followup_guidance = """If you approve but notice substantial work that is better handled separately in
a future issue or PR, list at most three highest-value items under this exact
heading:

### Future follow-ups

Do not use the Same-PR follow-ups section in this mode; mark the review blocking
instead when small or local cleanup should be fixed before merge.
The legacy heading `### Non-blocking follow-ups` is still accepted as future
follow-ups for compatibility, but prefer `### Future follow-ups`.
"""
    return f"""Review pull request #{pr_number} in {config.repo} (round {round_number}).

PR metadata:
- Repo: {metadata.repo}
- PR: #{metadata.number}
- Title: {title}
- Head branch: {head_branch}
- Base branch: {base_branch}
- Head SHA: {head_sha}
{url_line}
Use this PR metadata as authoritative. The Head SHA above is the PR head this
review round is about. If local files do not match that SHA, refresh/fetch the
checkout before reviewing. Do not spend time discovering the PR
branch. Do not report findings based on untracked files unless those files are
present in the PR diff.
{_scratch_file_guidance()}
{checks_block}{_issue_context_block(issue_context)}
{_human_requirements_block(human_requirements)}
{unresolved_items_block}{_memory_block(memory)}

Suggested commands:
- {config.gh_cmd} pr view {metadata.number} --repo {metadata.repo} --json title,body,headRefName,baseRefName,headRefOid,comments,reviews
- {config.gh_cmd} pr diff {metadata.number} --repo {metadata.repo}

If a shell/tool command requires confirmation in non-interactive mode, do not
retry repeatedly. Use the PR metadata above and the suggested GitHub CLI
commands, or produce a blocking review explaining the limitation.

Focus on correctness, security, test coverage, and maintainability. Review the
full diff and any existing PR discussion. Do not make code changes in this
review step; report blocking findings if {coder_name} needs to fix anything.
Treat the GitHub PR checks block above as authoritative for current CI state.
Do not say or imply that tests passed globally unless the GitHub PR checks
state is `passing` or `no_checks`. If only a local subset passed while GitHub
checks are `failing`, `pending`, or `unavailable`, say that explicitly.
When the PR changes files under `alembic/versions/`, verify migration topology:
new revisions should descend from the current head unless the PR intentionally
adds a merge migration.
{human_requirements_guidance}
{unresolved_items_guidance}
{followup_guidance}
Use blocking only for issues that should prevent merge.
All configured reviewers ({reviewer_group}) must approve in the same round for
the pull request to be considered approved.

End your final response with exactly one marker:

<!-- AGENT_STATE: approved -->

or:

<!-- AGENT_STATE: blocking -->

Use approved only if there are no blocking issues. Always sign your response:
-- {reviewer_signature}
"""


def build_followup_prompt(
    pr_number: int,
    round_number: int,
    review: str,
    config: AgentLoopConfig,
    memory: AgentMemoryContext | None = None,
    issue_context: IssueContext | None = None,
    human_requirements: Sequence[HumanReviewRequirement] | None = None,
) -> str:
    reviewer_name = format_agent_list(reviewers(config))
    coder_signature = agent_signature(config.coder)
    return f"""{reviewer_name} reviewed pull request #{pr_number} in {config.repo} and found blocking issues.

Address the review below in this local checkout. Pull/sync the PR branch if
needed, implement fixes, run relevant tests, commit, and push to the same PR.
Do not create a new PR.
{_scratch_file_guidance()}
{_coder_test_reporting_guidance()}
{_issue_context_block(issue_context)}
{_human_requirements_block(human_requirements)}
{_memory_block(memory)}

{reviewer_name} review:

{review}

This is round {round_number}. End your final response with exactly one marker:

<!-- AGENT_STATE: blocking -->

Use blocking to hand the updated PR back to {reviewer_name}. If you cannot safely address
the review, explain why and still use the blocking marker so a human can
intervene. Sign the response as:
-- {coder_signature}
"""


def build_same_pr_followup_prompt(
    pr_number: int,
    round_number: int,
    review: str,
    config: AgentLoopConfig,
    memory: AgentMemoryContext | None = None,
    issue_context: IssueContext | None = None,
    human_requirements: Sequence[HumanReviewRequirement] | None = None,
) -> str:
    reviewer_name = format_agent_list(reviewers(config))
    coder_signature = agent_signature(config.coder)
    return f"""{reviewer_name} approved pull request #{pr_number} in {config.repo} with same-PR follow-ups.

Address the follow-up items below in this local checkout. Pull/sync the PR
branch if needed, implement fixes, run relevant tests, commit, and push to the
same PR. Do not create a new PR.
These same-PR follow-ups are intended to be small, localized cleanup for the
current PR. Keep the change narrowly scoped to the listed items. Do not take on
larger redesigns or unrelated future work; call that out instead.
{_scratch_file_guidance()}
{_coder_test_reporting_guidance()}
{_issue_context_block(issue_context)}
{_human_requirements_block(human_requirements)}
{_memory_block(memory)}

Same-PR follow-ups:

{review}

This is round {round_number}. End your final response with exactly one marker:

<!-- AGENT_STATE: blocking -->

Use blocking to hand the updated PR back to {reviewer_name}. If you cannot safely address
the follow-ups, explain why and still use the blocking marker so a human can
intervene. Sign the response as:
-- {coder_signature}
"""
