"""Parsing for agent response markers."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from .errors import AgentLoopError

HUMAN_REQUIREMENTS_ADDRESSED_MARKER = "<!-- HUMAN_REQUIREMENTS_ADDRESSED -->"
HUMAN_REQUIREMENTS_DIRECT_DISCUSSION_ACK = (
    "checked the relevant GitHub discussion directly before responding"
)

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
HEADING_LEVEL_RE = re.compile(r"^\s*(#{1,6})\s+\S")
THEMATIC_BREAK_RE = re.compile(r"^\s*(?:([-*_])\s*){3,}\s*$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
    source_status: str | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedReview:
    state: str
    summary: str
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
    summary: str
    items: PlanReviewItems
    dispositions: tuple[ReviewItemDisposition, ...]


@dataclass(frozen=True)
class StructuredPrReview:
    schema_version: int
    kind: str
    state: str
    summary: str
    blocking_items: tuple[str, ...]
    same_pr_followups: tuple[str, ...]
    future_followups: tuple[str, ...]
    prior_item_dispositions: tuple[ReviewItemDisposition, ...]


@dataclass(frozen=True)
class StructuredPlanReview:
    schema_version: int
    kind: str
    state: str
    summary: str
    blocking_plan_issues: tuple[str, ...]
    same_plan_followups: tuple[str, ...]
    future_followups: tuple[str, ...]
    prior_plan_item_dispositions: tuple[ReviewItemDisposition, ...]


@dataclass(frozen=True)
class StructuredHumanRequirementsPayload:
    addressed_ids: tuple[str, ...]
    checked_discussion_directly: bool


@dataclass(frozen=True)
class StructuredCoderFollowup:
    schema_version: int
    kind: str
    state: str
    summary: str
    addressed_items: tuple[str, ...]
    remaining_items: tuple[str, ...]
    human_requirements: StructuredHumanRequirementsPayload
    tests_run: tuple[str, ...] | None = None


@dataclass(frozen=True)
class StructuredPlanRevision:
    schema_version: int
    kind: str
    state: str
    summary: str
    plan_steps: tuple[str, ...]


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


def review_freeform_summary_text(text: str) -> str:
    lines: list[str] = []
    skip_structured_section = False
    structured_heading_res = (
        BLOCKING_PLAN_ISSUES_HEADING_RE,
        SAME_PLAN_FOLLOWUP_HEADING_RE,
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


def _expect_object(value: object, *, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AgentLoopError(f"{context} must be a JSON object.")
    return value


def _expect_exact_keys(
    value: dict[str, object],
    *,
    context: str,
    required: set[str],
    optional: set[str] = frozenset(),
) -> None:
    keys = set(value)
    missing = sorted(required - keys)
    if missing:
        raise AgentLoopError(f"{context} is missing required field(s): {', '.join(missing)}")
    unknown = sorted(keys - required - optional)
    if unknown:
        raise AgentLoopError(f"{context} has unknown field(s): {', '.join(unknown)}")


def _expect_int(value: object, *, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentLoopError(f"{context} must be an integer.")
    return value


def _expect_bool(value: object, *, context: str) -> bool:
    if not isinstance(value, bool):
        raise AgentLoopError(f"{context} must be a boolean.")
    return value


def _expect_non_empty_string(value: object, *, context: str) -> str:
    if not isinstance(value, str):
        raise AgentLoopError(f"{context} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise AgentLoopError(f"{context} must be a non-empty string.")
    return normalized


def _expect_string_list(
    value: object,
    *,
    context: str,
    item_context: str,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AgentLoopError(f"{context} must be a JSON array.")
    return tuple(
        _expect_non_empty_string(item, context=f"{item_context} at index {index}")
        for index, item in enumerate(value)
    )


def _expect_state(value: object, *, context: str) -> str:
    state = _expect_non_empty_string(value, context=context)
    if state not in {"approved", "blocking"}:
        raise AgentLoopError(f"{context} must be `approved` or `blocking`.")
    return state


def _expect_item_id(value: object, *, context: str) -> str:
    item_id = _expect_non_empty_string(value, context=context)
    if not ITEM_ID_RE.fullmatch(item_id):
        raise AgentLoopError(
            f"{context} must match `[A-Za-z0-9][A-Za-z0-9._-]*`."
        )
    return item_id


def _expect_item_id_list(value: object, *, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AgentLoopError(f"{context} must be a JSON array.")
    return tuple(
        _expect_item_id(item, context=f"{context} item at index {index}")
        for index, item in enumerate(value)
    )


def _expect_requirement_id_list(value: object, *, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AgentLoopError(f"{context} must be a JSON array.")
    rendered: list[str] = []
    for index, item in enumerate(value):
        label = _expect_non_empty_string(item, context=f"{context} item at index {index}")
        rendered.append(_normalize_requirement_label(label))
    return tuple(rendered)


def _extract_structured_response_object(text: str) -> dict[str, object] | None:
    stripped = text.strip()
    if not stripped:
        return None
    decoder = json.JSONDecoder()
    try:
        payload, end = decoder.raw_decode(stripped)
    except json.JSONDecodeError:
        return None
    if stripped[end:].strip():
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _require_supported_schema_version(payload: dict[str, object]) -> None:
    if "schema_version" not in payload:
        raise AgentLoopError("Structured response is missing required field: schema_version")
    version = _expect_int(payload["schema_version"], context="schema_version")
    if version != 1:
        raise AgentLoopError(f"Unsupported structured response schema_version: {version}")


def _parse_review_item_disposition_payload(
    value: object,
    *,
    field_name: str,
    reviewer: str,
    allowed_same_status: str,
    is_plan_review: bool,
) -> ReviewItemDisposition:
    payload = _expect_object(value, context=field_name)
    _expect_exact_keys(
        payload,
        context=field_name,
        required={"item_id", "disposition"},
        optional={"note"},
    )
    item_id = _expect_item_id(payload["item_id"], context=f"{field_name}.item_id")
    disposition = _expect_non_empty_string(payload["disposition"], context=f"{field_name}.disposition")
    allowed_statuses = {"resolved", "blocking", allowed_same_status, "future"}
    if disposition not in allowed_statuses:
        rendered = ", ".join(sorted(allowed_statuses))
        raise AgentLoopError(f"{field_name}.disposition must be one of: {rendered}")
    note_value = payload.get("note")
    note = None
    if note_value is not None:
        note = _expect_non_empty_string(note_value, context=f"{field_name}.note")
    if _active_disposition_has_empty_note(
        disposition,
        note,
        same_status=allowed_same_status,
        is_plan_review=is_plan_review,
    ):
        raise AgentLoopError(
            f"{field_name}.note cannot be an empty placeholder for active disposition `{disposition}`."
        )
    return ReviewItemDisposition(
        item_id=item_id,
        reviewer=reviewer,
        disposition=disposition,
        note=note,
    )


def _expect_disposition_list(
    value: object,
    *,
    context: str,
    reviewer: str,
    allowed_same_status: str,
    is_plan_review: bool,
) -> tuple[ReviewItemDisposition, ...]:
    if not isinstance(value, list):
        raise AgentLoopError(f"{context} must be a JSON array.")
    return tuple(
        _parse_review_item_disposition_payload(
            item,
            field_name=f"{context}[{index}]",
            reviewer=reviewer,
            allowed_same_status=allowed_same_status,
            is_plan_review=is_plan_review,
        )
        for index, item in enumerate(value)
    )


def _finalize_parsed_review(
    *,
    state: str,
    summary: str,
    followups: ApprovedFollowups,
    dispositions: tuple[ReviewItemDisposition, ...],
) -> ParsedReview:
    if state == "blocking" and followups.future:
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
    return ParsedReview(
        state=state,
        summary=summary,
        followups=followups,
        dispositions=dispositions,
    )


def _finalize_parsed_plan_review(
    *,
    state: str,
    summary: str,
    items: PlanReviewItems,
    dispositions: tuple[ReviewItemDisposition, ...],
) -> ParsedPlanReview:
    if state == "blocking" and items.future:
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
    return ParsedPlanReview(
        state=state,
        summary=summary,
        items=items,
        dispositions=dispositions,
    )


def _structured_followups(items: tuple[str, ...], *, reviewer: str) -> tuple[ApprovedFollowup, ...]:
    return tuple(ApprovedFollowup(reviewer=reviewer, text=item) for item in items)


def parse_structured_pr_review(text: str, *, reviewer: str) -> ParsedReview | None:
    payload = _extract_structured_response_object(text)
    if payload is None:
        return None
    _require_supported_schema_version(payload)
    kind = payload.get("kind")
    if isinstance(kind, str) and kind != "pr_review":
        raise AgentLoopError("Structured response kind mismatch: expected `pr_review`.")
    try:
        _expect_exact_keys(
            payload,
            context="pr_review",
            required={
                "schema_version",
                "kind",
                "state",
                "summary",
                "blocking_items",
                "same_pr_followups",
                "future_followups",
                "prior_item_dispositions",
            },
        )
        state = _expect_state(payload["state"], context="pr_review.state")
        summary = _expect_non_empty_string(payload["summary"], context="pr_review.summary")
        blocking_items = _expect_string_list(
            payload["blocking_items"],
            context="pr_review.blocking_items",
            item_context="pr_review.blocking_items",
        )
        same_pr_followups = _expect_string_list(
            payload["same_pr_followups"],
            context="pr_review.same_pr_followups",
            item_context="pr_review.same_pr_followups",
        )
        future_followups = _expect_string_list(
            payload["future_followups"],
            context="pr_review.future_followups",
            item_context="pr_review.future_followups",
        )
        dispositions = _expect_disposition_list(
            payload["prior_item_dispositions"],
            context="pr_review.prior_item_dispositions",
            reviewer=reviewer,
            allowed_same_status="same-pr",
            is_plan_review=False,
        )
    except AgentLoopError:
        return None
    if state == "blocking" and future_followups:
        raise AgentLoopError("Blocking structured reviews may not include future follow-ups.")
    return _finalize_parsed_review(
        state=state,
        summary=summary,
        followups=ApprovedFollowups(
            same_pr=_structured_followups(same_pr_followups, reviewer=reviewer),
            future=_structured_followups(future_followups, reviewer=reviewer),
        ),
        dispositions=dispositions,
    )


def parse_structured_plan_review(text: str, *, reviewer: str) -> ParsedPlanReview | None:
    payload = _extract_structured_response_object(text)
    if payload is None:
        return None
    _require_supported_schema_version(payload)
    kind = payload.get("kind")
    if isinstance(kind, str) and kind != "plan_review":
        raise AgentLoopError("Structured response kind mismatch: expected `plan_review`.")
    try:
        _expect_exact_keys(
            payload,
            context="plan_review",
            required={
                "schema_version",
                "kind",
                "state",
                "summary",
                "blocking_plan_issues",
                "same_plan_followups",
                "future_followups",
                "prior_plan_item_dispositions",
            },
        )
        state = _expect_state(payload["state"], context="plan_review.state")
        summary = _expect_non_empty_string(payload["summary"], context="plan_review.summary")
        blocking_items = _expect_string_list(
            payload["blocking_plan_issues"],
            context="plan_review.blocking_plan_issues",
            item_context="plan_review.blocking_plan_issues",
        )
        same_plan_followups = _expect_string_list(
            payload["same_plan_followups"],
            context="plan_review.same_plan_followups",
            item_context="plan_review.same_plan_followups",
        )
        future_followups = _expect_string_list(
            payload["future_followups"],
            context="plan_review.future_followups",
            item_context="plan_review.future_followups",
        )
        dispositions = _expect_disposition_list(
            payload["prior_plan_item_dispositions"],
            context="plan_review.prior_plan_item_dispositions",
            reviewer=reviewer,
            allowed_same_status="same-plan",
            is_plan_review=True,
        )
    except AgentLoopError:
        return None
    if state == "blocking" and future_followups:
        raise AgentLoopError("Blocking structured plan reviews may not include future follow-ups.")
    return _finalize_parsed_plan_review(
        state=state,
        summary=summary,
        items=PlanReviewItems(
            blocking=_structured_followups(blocking_items, reviewer=reviewer),
            same_plan=_structured_followups(same_plan_followups, reviewer=reviewer),
            future=_structured_followups(future_followups, reviewer=reviewer),
        ),
        dispositions=dispositions,
    )


def validate_structured_coder_followup(text: str) -> StructuredCoderFollowup | None:
    payload = _extract_structured_response_object(text)
    if payload is None:
        return None
    _require_supported_schema_version(payload)
    kind = payload.get("kind")
    if isinstance(kind, str) and kind != "coder_followup":
        raise AgentLoopError("Structured response kind mismatch: expected `coder_followup`.")
    try:
        _expect_exact_keys(
            payload,
            context="coder_followup",
            required={
                "schema_version",
                "kind",
                "state",
                "summary",
                "addressed_items",
                "remaining_items",
                "human_requirements",
            },
            optional={"tests_run"},
        )
        human_requirements_payload = _expect_object(
            payload["human_requirements"],
            context="coder_followup.human_requirements",
        )
        _expect_exact_keys(
            human_requirements_payload,
            context="coder_followup.human_requirements",
            required={"addressed_ids", "checked_discussion_directly"},
        )
        tests_run_value = payload.get("tests_run")
        tests_run = (
            _expect_string_list(
                tests_run_value,
                context="coder_followup.tests_run",
                item_context="coder_followup.tests_run",
            )
            if tests_run_value is not None
            else None
        )
        return StructuredCoderFollowup(
            schema_version=1,
            kind="coder_followup",
            state=_expect_state(payload["state"], context="coder_followup.state"),
            summary=_expect_non_empty_string(payload["summary"], context="coder_followup.summary"),
            addressed_items=_expect_item_id_list(
                payload["addressed_items"],
                context="coder_followup.addressed_items",
            ),
            remaining_items=_expect_item_id_list(
                payload["remaining_items"],
                context="coder_followup.remaining_items",
            ),
            human_requirements=StructuredHumanRequirementsPayload(
                addressed_ids=_expect_requirement_id_list(
                    human_requirements_payload["addressed_ids"],
                    context="coder_followup.human_requirements.addressed_ids",
                ),
                checked_discussion_directly=_expect_bool(
                    human_requirements_payload["checked_discussion_directly"],
                    context="coder_followup.human_requirements.checked_discussion_directly",
                ),
            ),
            tests_run=tests_run,
        )
    except AgentLoopError:
        return None


def validate_structured_plan_revision(text: str) -> StructuredPlanRevision | None:
    payload = _extract_structured_response_object(text)
    if payload is None:
        return None
    _require_supported_schema_version(payload)
    kind = payload.get("kind")
    if isinstance(kind, str) and kind != "plan_revision":
        raise AgentLoopError("Structured response kind mismatch: expected `plan_revision`.")
    try:
        _expect_exact_keys(
            payload,
            context="plan_revision",
            required={"schema_version", "kind", "state", "summary", "plan_steps"},
        )
        state = _expect_non_empty_string(payload["state"], context="plan_revision.state")
        if state != "blocking":
            raise AgentLoopError("plan_revision.state must be `blocking`.")
        summary = _expect_non_empty_string(payload["summary"], context="plan_revision.summary")
        plan_steps = _expect_string_list(
            payload["plan_steps"],
            context="plan_revision.plan_steps",
            item_context="plan_revision.plan_steps",
        )
    except AgentLoopError:
        return None
    return StructuredPlanRevision(
        schema_version=1,
        kind="plan_revision",
        state="blocking",
        summary=summary,
        plan_steps=plan_steps,
    )


def _collect_section_items(
    text: str,
    *,
    sections: Sequence[tuple[re.Pattern[str], list[ApprovedFollowup]]],
    empty_item_re: re.Pattern[str],
    reviewer: str,
) -> None:
    def heading_level(line: str) -> int | None:
        match = HEADING_LEVEL_RE.match(line)
        if not match:
            return None
        return len(match.group(1))

    def normalize_item_text(lines: list[str]) -> str:
        trimmed = list(lines)
        while trimmed and not trimmed[0].strip():
            trimmed.pop(0)
        while trimmed and not trimmed[-1].strip():
            trimmed.pop()
        if not trimmed:
            return ""
        common_indent = min(
            len(line) - len(line.lstrip(" "))
            for line in trimmed
            if line.strip()
        )
        if common_indent:
            trimmed = [line[common_indent:] if line.strip() else "" for line in trimmed]

        rendered: list[str] = []
        paragraph: list[str] = []
        fence_marker: str | None = None

        def flush_paragraph() -> None:
            if paragraph:
                rendered.append(" ".join(part.strip() for part in paragraph))
                paragraph.clear()

        for raw_line in trimmed:
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped:
                flush_paragraph()
                if rendered and rendered[-1] != "":
                    rendered.append("")
                continue

            fence_match = re.match(r"^\s*(```+|~~~+)", line)
            if fence_match:
                flush_paragraph()
                rendered.append(line)
                marker = fence_match.group(1)
                if fence_marker == marker:
                    fence_marker = None
                elif fence_marker is None:
                    fence_marker = marker
                continue
            if fence_marker is not None:
                rendered.append(line)
                continue

            if HEADING_LEVEL_RE.match(line):
                flush_paragraph()
                rendered.append(line)
                continue

            if re.match(r"^\s{2,}(?:[-*+]\s+|\d+[.)]\s+)", line) or stripped.startswith(">"):
                flush_paragraph()
                rendered.append(line)
                continue

            paragraph.append(stripped)

        flush_paragraph()
        while rendered and rendered[-1] == "":
            rendered.pop()
        return "\n".join(rendered).strip()

    def section_bucket(line: str) -> list[ApprovedFollowup] | None:
        for pattern, bucket in sections:
            if pattern.match(line):
                return bucket
        return None

    active: list[ApprovedFollowup] | None = None
    current: list[str] = []
    active_heading_level: int | None = None

    def flush_current() -> None:
        if active is not None and current:
            item = normalize_item_text(current)
            if item and not empty_item_re.match(item):
                active.append(ApprovedFollowup(reviewer=reviewer, text=item))
            current.clear()

    for line in text.splitlines():
        next_active = section_bucket(line)
        if next_active is not None:
            flush_current()
            active = next_active
            active_heading_level = heading_level(line)
            continue
        if active is None:
            continue

        next_heading_level = heading_level(line)
        if next_heading_level is not None and active_heading_level is not None and next_heading_level <= active_heading_level:
            flush_current()
            active = None
            active_heading_level = None
            continue
        if HTML_COMMENT_RE.match(line) or SIGNATURE_RE.match(line):
            flush_current()
            active = None
            active_heading_level = None
            continue
        if THEMATIC_BREAK_RE.match(line):
            flush_current()
            continue
        bullet = BULLET_RE.match(line)
        if bullet:
            flush_current()
            current.append(bullet.group("text"))
            continue
        if next_heading_level is not None:
            flush_current()
            current.append(line.rstrip())
            continue
        if current or line.strip():
            current.append(line.rstrip())

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
    status_pattern = (
        r"resolved|"
        r"(?:still\s+)?blocking|"
        rf"(?:still\s+)?(?:{same_pattern})|"
        r"(?:downgraded\s+to\s+)?future follow[- ]up"
    )
    return re.compile(
        r"^\s*\[?(?P<item_id>[A-Za-z0-9][A-Za-z0-9._-]*)\]?\s*"
        r"(?:"
        r"(?:(?P<label>.+?)\s*->\s*)"
        r"|"
        r"(?:(?:->|:)?\s*)"
        r")"
        rf"(?P<status>{status_pattern})"
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
    summary = review_freeform_summary_text(text)
    followups = parse_approved_followups(text, reviewer=reviewer)
    dispositions = parse_unresolved_item_dispositions(text, reviewer=reviewer)
    return _finalize_parsed_review(
        state=state,
        summary=summary,
        followups=followups,
        dispositions=dispositions,
    )


def parse_plan_review(text: str, *, reviewer: str) -> ParsedPlanReview:
    """Parse a plan review, including state, structured plan items, and dispositions."""
    state = parse_plan_state(text)
    summary = review_freeform_summary_text(text)
    items = parse_plan_review_items(text, reviewer=reviewer)
    dispositions = parse_plan_item_dispositions(text, reviewer=reviewer)
    return _finalize_parsed_plan_review(
        state=state,
        summary=summary,
        items=items,
        dispositions=dispositions,
    )


def parse_non_blocking_followups(text: str, *, reviewer: str) -> list[ApprovedFollowup]:
    """Extract legacy non-blocking follow-ups as future follow-ups."""
    return list(parse_approved_followups(text, reviewer=reviewer).future)
