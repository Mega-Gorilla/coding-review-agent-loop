"""Parsing for agent response markers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import AgentLoopError

STATE_RE = re.compile(r"<!--\s*AGENT_STATE:\s*(approved|blocking)\s*-->", re.I)
PLAN_STATE_RE = re.compile(r"<!--\s*AGENT_PLAN_STATE:\s*(approved|blocking)\s*-->", re.I)
PR_RE = re.compile(r"<!--\s*AGENT_PR:\s*(\d+)\s*-->", re.I)
GH_PR_URL_RE = re.compile(r"/pull/(\d+)(?:\b|$)")
CLARIFY_RE = re.compile(r"<!--\s*AGENT_CLARIFY\s*-->", re.I)
HUMAN_REVIEWER_SIGNATURE_RE = re.compile(r"^\s*--\s*Human Reviewer\s*$", re.I | re.M)
HUMAN_REQUIREMENTS_RESOLVED_RE = re.compile(
    r"<!--\s*HUMAN_REQUIREMENTS_RESOLVED\s*-->",
    re.I,
)


def _followup_heading_re(title: str) -> re.Pattern[str]:
    title_with_punctuation = rf"{title}[:.]?"
    return re.compile(
        rf"^\s*#{{2,6}}\s+"
        rf"(?:{title_with_punctuation}|\*\*{title}\*\*[:.]?|\*\*{title_with_punctuation}\*\*)"
        rf"\s*$",
        re.I,
    )


SAME_PR_FOLLOWUP_HEADING_RE = _followup_heading_re(r"same[- ]pr follow[- ]ups")
FUTURE_FOLLOWUP_HEADING_RE = _followup_heading_re(r"future follow[- ]ups")
LEGACY_FOLLOWUP_HEADING_RE = _followup_heading_re(r"non[- ]blocking follow[- ]ups")
ANY_HEADING_RE = re.compile(r"^\s*#{1,6}\s+\S")
HTML_COMMENT_RE = re.compile(r"^\s*<!--.*-->\s*$")
SIGNATURE_RE = re.compile(r"^\s*--\s+\S")
BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(?P<text>.+?)\s*$")
EMPTY_FOLLOWUP_RE = re.compile(
    r"^(?:none|n/a|no follow[- ]?ups?|no same[- ]pr follow[- ]?ups?|no future follow[- ]?ups?)\.?$",
    re.I,
)


@dataclass(frozen=True)
class ApprovedFollowup:
    reviewer: str
    text: str


@dataclass(frozen=True)
class ApprovedFollowups:
    same_pr: tuple[ApprovedFollowup, ...]
    future: tuple[ApprovedFollowup, ...]


def parse_agent_state(text: str) -> str:
    matches = STATE_RE.findall(text)
    if not matches:
        raise AgentLoopError("Agent response did not include <!-- AGENT_STATE: approved|blocking -->")
    # Use the final marker as authoritative; responses may quote earlier review markers.
    return matches[-1].lower()


def parse_plan_state(text: str) -> str:
    matches = PLAN_STATE_RE.findall(text)
    if not matches:
        raise AgentLoopError(
            "Agent response did not include <!-- AGENT_PLAN_STATE: approved|blocking -->"
        )
    return matches[-1].lower()


def parse_pr_number(text: str) -> int | None:
    marker = PR_RE.search(text)
    if marker:
        return int(marker.group(1))
    url = GH_PR_URL_RE.search(text)
    if url:
        return int(url.group(1))
    return None


def is_clarification_request(text: str) -> bool:
    return bool(CLARIFY_RE.search(text))


def parse_signed_human_requirement_body(text: str | None) -> str | None:
    """Return comment body before a standalone ``-- Human Reviewer`` signature."""
    if not text:
        return None
    match = HUMAN_REVIEWER_SIGNATURE_RE.search(text)
    if not match:
        return None
    body = text[: match.start()].strip()
    return body or None


def human_requirements_resolved(text: str) -> bool:
    return bool(HUMAN_REQUIREMENTS_RESOLVED_RE.search(text))


def parse_approved_followups(text: str, *, reviewer: str) -> ApprovedFollowups:
    """Extract same-PR and future follow-up bullets from an approved review."""
    same_pr: list[ApprovedFollowup] = []
    future: list[ApprovedFollowup] = []
    active: list[ApprovedFollowup] | None = None
    current: list[str] = []

    def flush_current() -> None:
        if active is not None and current:
            item = " ".join(part.strip() for part in current if part.strip()).strip()
            if item and not EMPTY_FOLLOWUP_RE.match(item):
                active.append(ApprovedFollowup(reviewer=reviewer, text=item))
            current.clear()

    for line in text.splitlines():
        if SAME_PR_FOLLOWUP_HEADING_RE.match(line):
            flush_current()
            active = same_pr
            continue
        if FUTURE_FOLLOWUP_HEADING_RE.match(line) or LEGACY_FOLLOWUP_HEADING_RE.match(line):
            flush_current()
            active = future
            continue
        if active is None:
            continue
        if ANY_HEADING_RE.match(line):
            flush_current()
            active = None
            continue
        if HTML_COMMENT_RE.match(line) or SIGNATURE_RE.match(line):
            flush_current()
            active = None
            continue
        bullet = BULLET_RE.match(line)
        if bullet:
            flush_current()
            current.append(bullet.group("text"))
            continue
        if current and line.strip():
            current.append(line)

    flush_current()
    return ApprovedFollowups(same_pr=tuple(same_pr), future=tuple(future))


def parse_non_blocking_followups(text: str, *, reviewer: str) -> list[ApprovedFollowup]:
    """Extract legacy non-blocking follow-ups as future follow-ups."""
    return list(parse_approved_followups(text, reviewer=reviewer).future)
