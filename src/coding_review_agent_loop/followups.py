"""Approved follow-up publishing and formatting helpers."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .config import AgentLoopConfig
from .github import create_issue, post_pr_comment
from .logging import log
from .protocol import ApprovedFollowup, UnresolvedReviewItem
from .runner import Runner

MAX_APPROVED_FOLLOWUP_ISSUES = 3
APPROVED_FOLLOWUP_MARKER_RE = re.compile(
    r"<!--\s*AGENT_APPROVED_FOLLOWUPS:\s*pr=(?P<pr>\d+)\s+head=(?P<head>\S+)\s+mode=(?P<mode>[a-z-]+)\s*-->",
    re.I,
)


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


def _approved_followup_from_unresolved_item(item: UnresolvedReviewItem) -> ApprovedFollowup:
    text = item.text
    for note in item.notes:
        update_line = f"Update from {note}"
        if update_line not in text:
            text = f"{text.rstrip()}\n\n{update_line}"
    return ApprovedFollowup(reviewer=item.reviewer, text=text)


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
