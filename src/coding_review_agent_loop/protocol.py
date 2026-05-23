"""Parsing for agent response markers."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .errors import AgentLoopError

HUMAN_REQUIREMENTS_ADDRESSED_MARKER = "<!-- HUMAN_REQUIREMENTS_ADDRESSED -->"
HUMAN_REQUIREMENTS_DIRECT_DISCUSSION_ACK = "checked the PR discussion directly before responding"

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
HUMAN_REQUIREMENTS_ADDRESSED_RE = re.compile(
    re.escape(HUMAN_REQUIREMENTS_ADDRESSED_MARKER),
    re.I,
)
HUMAN_REQUIREMENTS_DIRECT_DISCUSSION_ACK_RE = re.compile(
    re.escape(HUMAN_REQUIREMENTS_DIRECT_DISCUSSION_ACK),
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
PRIOR_UNRESOLVED_ITEM_DISPOSITIONS_HEADING_RE = _followup_heading_re(
    r"prior unresolved item dispositions"
)
BLOCKING_PLAN_ISSUES_HEADING_RE = _followup_heading_re(r"blocking plan issues")
SAME_PLAN_FOLLOWUP_HEADING_RE = _followup_heading_re(r"same[- ]plan follow[- ]ups")
HUMAN_REQUIREMENTS_HEADING_RE = _followup_heading_re(r"human requirements")
PRIOR_UNRESOLVED_PLAN_ITEM_DISPOSITIONS_HEADING_RE = _followup_heading_re(
    r"prior unresolved plan item dispositions"
)
ANY_HEADING_RE = re.compile(r"^\s*#{1,6}\s+\S")
HTML_COMMENT_RE = re.compile(r"^\s*<!--.*-->\s*$")
SIGNATURE_RE = re.compile(r"^\s*--\s+\S")
BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(?P<text>.+?)\s*$")


def _empty_placeholder_re(*phrases: str) -> re.Pattern[str]:
    joined = "|".join(phrases)
    return re.compile(rf"^(?:none|n/a|{joined})\.?$", re.I)


EMPTY_FOLLOWUP_RE = _empty_placeholder_re(
    r"no follow[- ]?ups?",
    r"no same[- ]pr follow[- ]?ups?",
    r"no future follow[- ]?ups?",
)
EMPTY_PLAN_SECTION_RE = _empty_placeholder_re(
    r"no blocking plan issues?",
    r"no same[- ]plan follow[- ]?ups?",
    r"no future follow[- ]?ups?",
)


@dataclass(frozen=True)
class ApprovedFollowup:
    reviewer: str
    text: str


@dataclass(frozen=True)
class ApprovedFollowups:
    same_pr: tuple[ApprovedFollowup, ...]
    future: tuple[ApprovedFollowup, ...]


@dataclass(frozen=True)
class ReviewItemDisposition:
    item_id: str
    reviewer: str
    disposition: str
    note: str | None = None


@dataclass(frozen=True)
class UnresolvedReviewItem:
    item_id: str
    reviewer: str
    source_round: int
    text: str
    status: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedReview:
    state: str
    followups: ApprovedFollowups
    dispositions: tuple[ReviewItemDisposition, ...]


@dataclass(frozen=True)
class PlanReviewItems:
    blocking: tuple[ApprovedFollowup, ...]
    same_plan: tuple[ApprovedFollowup, ...]
    future: tuple[ApprovedFollowup, ...]


@dataclass(frozen=True)
class ParsedPlanReview:
    state: str
    items: PlanReviewItems
    dispositions: tuple[ReviewItemDisposition, ...]


@dataclass(frozen=True)
class ParsedHumanRequirementsAcknowledgement:
    marker_present: bool
    section_present: bool
    addressed_ids: tuple[str, ...]
    section_text: str


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


def _normalize_requirement_label(text: str) -> str:
    match = re.fullmatch(r"\s*Requirement\s+(\d+)\s*", text, re.I)
    if not match:
        raise AgentLoopError(f"Invalid human requirement label: {text}")
    return f"Requirement {match.group(1)}"


def parse_human_requirements_acknowledgement(text: str) -> ParsedHumanRequirementsAcknowledgement:
    marker_present = bool(HUMAN_REQUIREMENTS_ADDRESSED_RE.search(text))
    section_present = False
    section_lines: list[str] = []
    addressed_ids: list[str] = []
    active = False

    for line in text.splitlines():
        if HUMAN_REQUIREMENTS_HEADING_RE.match(line):
            section_present = True
            active = True
            continue
        if not active:
            continue
        if ANY_HEADING_RE.match(line) or HTML_COMMENT_RE.match(line) or SIGNATURE_RE.match(line):
            active = False
            continue
        section_lines.append(line)
        bullet = BULLET_RE.match(line)
        if not bullet:
            continue
        for match in re.finditer(r"\bRequirement\s+\d+\b", bullet.group("text"), re.I):
            addressed_ids.append(_normalize_requirement_label(match.group(0)))

    return ParsedHumanRequirementsAcknowledgement(
        marker_present=marker_present,
        section_present=section_present,
        addressed_ids=tuple(addressed_ids),
        section_text="\n".join(section_lines).strip(),
    )


def validate_human_requirements_acknowledgement(
    text: str,
    *,
    surfaced_requirement_ids: Sequence[str],
    requires_direct_discussion_ack: bool,
) -> None:
    if not surfaced_requirement_ids and not requires_direct_discussion_ack:
        return

    parsed = parse_human_requirements_acknowledgement(text)
    if not parsed.marker_present:
        raise AgentLoopError(
            "Coder response missing required signed human requirements marker "
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}."
        )
    if not parsed.section_present:
        raise AgentLoopError("Coder response missing required `### Human requirements` section.")

    addressed_ids = list(parsed.addressed_ids)
    duplicates = sorted({item_id for item_id in addressed_ids if addressed_ids.count(item_id) > 1})
    if duplicates:
        raise AgentLoopError(
            "Coder response listed signed human requirement IDs more than once: "
            + ", ".join(duplicates)
        )

    expected_ids = tuple(_normalize_requirement_label(item_id) for item_id in surfaced_requirement_ids)
    unknown = sorted(set(addressed_ids) - set(expected_ids))
    if unknown:
        raise AgentLoopError(
            "Coder response referenced unknown signed human requirement IDs: "
            + ", ".join(unknown)
        )

    if expected_ids:
        missing = sorted(set(expected_ids) - set(addressed_ids))
        if missing:
            raise AgentLoopError(
                "Coder response did not address all surfaced signed human requirement IDs: "
                + ", ".join(missing)
            )
        return

    if not HUMAN_REQUIREMENTS_DIRECT_DISCUSSION_ACK_RE.search(parsed.section_text):
        raise AgentLoopError(
            "Coder response must acknowledge that the prompt omitted the detailed signed human requirements "
            f"and that it {HUMAN_REQUIREMENTS_DIRECT_DISCUSSION_ACK}."
        )


def _collect_section_items(
    text: str,
    *,
    sections: Sequence[tuple[re.Pattern[str], list[ApprovedFollowup]]],
    empty_item_re: re.Pattern[str],
    reviewer: str,
) -> None:
    active: list[ApprovedFollowup] | None = None
    current: list[str] = []
    current_is_prose = False

    def flush_current() -> None:
        nonlocal current_is_prose
        if active is not None and current:
            item = " ".join(part.strip() for part in current if part.strip()).strip()
            if item and not empty_item_re.match(item):
                active.append(ApprovedFollowup(reviewer=reviewer, text=item))
            current.clear()
        current_is_prose = False

    for line in text.splitlines():
        next_active = None
        for pattern, bucket in sections:
            if pattern.match(line):
                next_active = bucket
                break
        if next_active is not None:
            flush_current()
            active = next_active
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
        if not line.strip():
            if current_is_prose:
                flush_current()
            continue
        bullet = BULLET_RE.match(line)
        if bullet:
            flush_current()
            current.append(bullet.group("text"))
            current_is_prose = False
            continue
        if current and line.strip():
            current.append(line)
            continue
        current.append(line)
        current_is_prose = True

    flush_current()


def parse_approved_followups(text: str, *, reviewer: str) -> ApprovedFollowups:
    """Extract same-PR and future follow-ups from an approved review."""
    same_pr: list[ApprovedFollowup] = []
    future: list[ApprovedFollowup] = []
    _collect_section_items(
        text,
        sections=(
            (SAME_PR_FOLLOWUP_HEADING_RE, same_pr),
            (FUTURE_FOLLOWUP_HEADING_RE, future),
            (LEGACY_FOLLOWUP_HEADING_RE, future),
        ),
        empty_item_re=EMPTY_FOLLOWUP_RE,
        reviewer=reviewer,
    )
    return ApprovedFollowups(same_pr=tuple(same_pr), future=tuple(future))


def parse_plan_review_items(text: str, *, reviewer: str) -> PlanReviewItems:
    blocking: list[ApprovedFollowup] = []
    same_plan: list[ApprovedFollowup] = []
    future: list[ApprovedFollowup] = []
    _collect_section_items(
        text,
        sections=(
            (BLOCKING_PLAN_ISSUES_HEADING_RE, blocking),
            (SAME_PLAN_FOLLOWUP_HEADING_RE, same_plan),
            (FUTURE_FOLLOWUP_HEADING_RE, future),
        ),
        empty_item_re=EMPTY_PLAN_SECTION_RE,
        reviewer=reviewer,
    )
    return PlanReviewItems(
        blocking=tuple(blocking),
        same_plan=tuple(same_plan),
        future=tuple(future),
    )


def _disposition_re(*same_statuses: str) -> re.Pattern[str]:
    same_pattern = "|".join(same_statuses)
    return re.compile(
        r"^\s*\[?(?P<item_id>[A-Za-z0-9][A-Za-z0-9._-]*)\]?\s*"
        r"(?:->|:)?\s*"
        r"(?P<status>"
        r"resolved|"
        r"(?:still\s+)?blocking|"
        rf"(?:still\s+)?(?:{same_pattern})|"
        r"(?:downgraded\s+to\s+)?future follow[- ]up"
        r")"
        r"(?:\s*:\s*(?P<note>.+))?\s*$",
        re.I,
    )


def _normalize_disposition(status: str, *, same_status: str) -> str:
    normalized = " ".join(status.lower().split())
    normalized = normalized.replace("same pr", "same-pr").replace("same plan", "same-plan")
    normalized = normalized.replace("follow up", "follow-up")
    if normalized == "resolved":
        return "resolved"
    if normalized.endswith("blocking"):
        return "blocking"
    if normalized.endswith(same_status):
        return same_status
    if normalized.endswith("future follow-up"):
        return "future"
    raise AgentLoopError(f"Unsupported unresolved item disposition: {status}")


def _active_disposition_has_empty_note(
    disposition: str,
    note: str | None,
    *,
    same_status: str,
    is_plan_review: bool,
) -> bool:
    if disposition == "resolved" or not note:
        return False

    same_status_pattern = re.escape(same_status).replace(r"\-", "[- ]")
    blocking_phrases = [r"no blocking issues?"]
    if is_plan_review:
        blocking_phrases.append(r"no blocking plan issues?")

    empty_note_res = {
        "blocking": _empty_placeholder_re(*blocking_phrases),
        same_status: _empty_placeholder_re(rf"no {same_status_pattern} follow[- ]?ups?"),
        "future": _empty_placeholder_re(r"no future follow[- ]?ups?", r"no follow[- ]?ups?"),
    }
    empty_note_re = empty_note_res.get(disposition)
    return bool(empty_note_re and empty_note_re.match(note))


def _parse_unresolved_item_dispositions(
    text: str,
    *,
    reviewer: str,
    heading_re: re.Pattern[str],
    empty_item_re: re.Pattern[str],
    disposition_re: re.Pattern[str],
    same_status: str,
    error_message: str,
    is_plan_review: bool,
) -> tuple[ReviewItemDisposition, ...]:
    dispositions: list[ReviewItemDisposition] = []
    active = False

    for line in text.splitlines():
        if heading_re.match(line):
            active = True
            continue
        if not active:
            continue
        if ANY_HEADING_RE.match(line) or HTML_COMMENT_RE.match(line) or SIGNATURE_RE.match(line):
            active = False
            continue
        if not line.strip():
            continue
        bullet = BULLET_RE.match(line)
        if not bullet:
            continue
        entry = bullet.group("text")
        if empty_item_re.match(entry):
            continue
        match = disposition_re.match(entry)
        if not match:
            raise AgentLoopError(error_message)
        note = match.group("note")
        normalized_note = note.strip() if note else None
        disposition = _normalize_disposition(match.group("status"), same_status=same_status)
        if _active_disposition_has_empty_note(
            disposition,
            normalized_note,
            same_status=same_status,
            is_plan_review=is_plan_review,
        ):
            raise AgentLoopError(error_message)
        dispositions.append(
            ReviewItemDisposition(
                item_id=match.group("item_id"),
                reviewer=reviewer,
                disposition=disposition,
                note=normalized_note,
            )
        )

    return tuple(dispositions)


def parse_unresolved_item_dispositions(text: str, *, reviewer: str) -> tuple[ReviewItemDisposition, ...]:
    """Extract structured prior-item dispositions from a review."""
    return _parse_unresolved_item_dispositions(
        text,
        reviewer=reviewer,
        heading_re=PRIOR_UNRESOLVED_ITEM_DISPOSITIONS_HEADING_RE,
        empty_item_re=EMPTY_FOLLOWUP_RE,
        disposition_re=_disposition_re(r"same[- ]pr"),
        same_status="same-pr",
        error_message=(
            "Invalid prior unresolved item disposition. Use bullets like "
            "`- [item-1] resolved`, `- [item-2] still blocking`, "
            "`- [item-3] same-pr`, or `- [item-4] future follow-up: reason`. "
            "Active unresolved dispositions must describe remaining work; use "
            "`resolved` when nothing remains."
        ),
        is_plan_review=False,
    )


def parse_plan_item_dispositions(text: str, *, reviewer: str) -> tuple[ReviewItemDisposition, ...]:
    """Extract structured prior plan-item dispositions from a plan review."""
    return _parse_unresolved_item_dispositions(
        text,
        reviewer=reviewer,
        heading_re=PRIOR_UNRESOLVED_PLAN_ITEM_DISPOSITIONS_HEADING_RE,
        empty_item_re=EMPTY_PLAN_SECTION_RE,
        disposition_re=_disposition_re(r"same[- ]plan"),
        same_status="same-plan",
        error_message=(
            "Invalid prior unresolved plan item disposition. Use bullets like "
            "`- [item-1] resolved`, `- [item-2] still blocking`, "
            "`- [item-3] same-plan`, or `- [item-4] future follow-up: reason`. "
            "Active unresolved dispositions must describe remaining work; use "
            "`resolved` when nothing remains."
        ),
        is_plan_review=True,
    )


def parse_review(text: str, *, reviewer: str) -> ParsedReview:
    """Parse a review, including state, follow-ups, and prior-item dispositions."""
    state = parse_agent_state(text)
    followups = parse_approved_followups(text, reviewer=reviewer)
    dispositions = parse_unresolved_item_dispositions(text, reviewer=reviewer)
    if state == "blocking" and followups.future:
        # Some agents still append speculative future work to otherwise valid
        # blocking reviews. Preserve the blocking findings and ignore that
        # lower-priority section rather than aborting the whole round.
        followups = ApprovedFollowups(same_pr=followups.same_pr, future=())
    if state == "blocking" and any(item.disposition == "future" for item in dispositions):
        raise AgentLoopError(
            "Blocking reviews may not downgrade prior unresolved items to Future follow-ups."
        )
    if state == "approved":
        active_dispositions = [
            item.disposition for item in dispositions if item.disposition in {"blocking", "same-pr"}
        ]
        if followups.same_pr or active_dispositions:
            raise AgentLoopError(
                "Approved reviews must be fully complete for this round. Do not use "
                "`approved` when Same-PR follow-ups remain or when any prior unresolved "
                "item stays `still blocking` or `same-pr`."
            )
    return ParsedReview(state=state, followups=followups, dispositions=dispositions)


def parse_plan_review(text: str, *, reviewer: str) -> ParsedPlanReview:
    """Parse a plan review, including state, structured plan items, and dispositions."""
    state = parse_plan_state(text)
    items = parse_plan_review_items(text, reviewer=reviewer)
    dispositions = parse_plan_item_dispositions(text, reviewer=reviewer)
    if state == "blocking" and items.future:
        # Some agents still append speculative future work to otherwise valid
        # blocking plan reviews. Preserve the blocking findings and ignore that
        # lower-priority section rather than aborting the whole planning round.
        items = PlanReviewItems(blocking=items.blocking, same_plan=items.same_plan, future=())
    if state == "blocking" and any(item.disposition == "future" for item in dispositions):
        raise AgentLoopError(
            "Blocking plan reviews may not downgrade prior unresolved plan items to Future follow-ups."
        )
    if state == "approved":
        active_dispositions = [
            item.disposition for item in dispositions if item.disposition in {"blocking", "same-plan"}
        ]
        if items.blocking or items.same_plan or active_dispositions:
            raise AgentLoopError(
                "Approved plan reviews must be fully complete for this planning round. "
                "Do not use `approved` when blocking plan issues, Same-plan follow-ups, "
                "or carried-forward plan items remain active."
            )
    return ParsedPlanReview(state=state, items=items, dispositions=dispositions)


def parse_non_blocking_followups(text: str, *, reviewer: str) -> list[ApprovedFollowup]:
    """Extract legacy non-blocking follow-ups as future follow-ups."""
    return list(parse_approved_followups(text, reviewer=reviewer).future)
