"""Unresolved review item ledger and response validation helpers."""

from __future__ import annotations

import re
from collections.abc import Sequence

from .errors import AgentLoopError
from .prompts import render_coder_human_requirements_prompt_context
from .protocol import (
    ParsedPlanReview,
    ParsedReview,
    ReviewItemDisposition,
    StructuredCoderFollowup,
    UnresolvedReviewItem,
    parse_plan_review,
    parse_pr_review,
    validate_human_requirements_acknowledgement,
    validate_structured_coder_followup,
    validate_structured_human_requirements_acknowledgement,
)

HUMAN_REQUIREMENTS_ACK_ITEM_ID = "item-human-requirements-acknowledgement"
ALL_RESOLVED_PROSE_RE = re.compile(
    r"^all (?:prior items|listed items|carried-forward items) are resolved\.?$"
    r"|^all prior unresolved items have been resolved\.?$",
    re.I,
)


def _normalize_disposition_section_prose(text: str) -> str:
    return " ".join(text.strip().split())


def _maybe_fill_resolved_dispositions_from_prose(
    parsed: ParsedReview | ParsedPlanReview,
    *,
    reviewer: str,
    unresolved_items: Sequence[UnresolvedReviewItem],
) -> tuple[ReviewItemDisposition, ...]:
    if not unresolved_items or parsed.dispositions:
        return parsed.dispositions
    normalized = _normalize_disposition_section_prose(parsed.raw_dispositions_text)
    if not normalized or not ALL_RESOLVED_PROSE_RE.fullmatch(normalized):
        return parsed.dispositions
    return tuple(
        ReviewItemDisposition(
            item_id=item.item_id,
            reviewer=reviewer,
            disposition="resolved",
        )
        for item in unresolved_items
    )


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
        source_status=status,
        notes=tuple(notes),
    )


def _validate_review_response(
    text: str,
    *,
    reviewer: str,
    unresolved_items: Sequence[UnresolvedReviewItem],
) -> ParsedReview:
    parsed = parse_pr_review(text, reviewer=reviewer)
    if not unresolved_items:
        return parsed

    unresolved_by_id = {item.item_id: item for item in unresolved_items}
    dispositions = _maybe_fill_resolved_dispositions_from_prose(
        parsed,
        reviewer=reviewer,
        unresolved_items=unresolved_items,
    )
    disposition_ids = [item.item_id for item in dispositions]
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
    return ParsedReview(
        state=parsed.state,
        summary=parsed.summary,
        blocking_items=parsed.blocking_items,
        followups=parsed.followups,
        dispositions=dispositions,
        raw_dispositions_text=parsed.raw_dispositions_text,
    )


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
            source_status="blocking",
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
    if not prompt_context.surfaced_requirement_ids and not prompt_context.requires_direct_discussion_ack:
        return _clear_human_requirements_ack_item(unresolved_items)
    try:
        structured_followup = validate_structured_coder_followup(coder_output)
        if structured_followup is not None:
            validate_structured_human_requirements_acknowledgement(
                structured_followup.human_requirements.addressed_ids,
                checked_discussion_directly=structured_followup.human_requirements.checked_discussion_directly,
                surfaced_requirement_ids=prompt_context.surfaced_requirement_ids,
                requires_direct_discussion_ack=prompt_context.requires_direct_discussion_ack,
            )
        else:
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


def _validate_structured_coder_followup_items(
    parsed: StructuredCoderFollowup,
    *,
    unresolved_items: Sequence[UnresolvedReviewItem],
) -> None:
    expected_ids = [
        item.item_id for item in unresolved_items if item.item_id != HUMAN_REQUIREMENTS_ACK_ITEM_ID
    ]
    listed_ids = [*parsed.addressed_items, *parsed.remaining_items]
    duplicates = sorted({item_id for item_id in listed_ids if listed_ids.count(item_id) > 1})
    if duplicates:
        raise AgentLoopError(
            "Coder follow-up listed unresolved reviewer item IDs more than once: "
            + ", ".join(duplicates)
        )
    unknown = sorted(set(listed_ids) - set(expected_ids))
    if unknown:
        raise AgentLoopError(
            "Coder follow-up referenced unknown unresolved reviewer item IDs: "
            + ", ".join(unknown)
        )
    missing = sorted(set(expected_ids) - set(listed_ids))
    if missing:
        raise AgentLoopError(
            "Coder follow-up did not classify all unresolved reviewer items into addressed or remaining: "
            + ", ".join(missing)
        )


def _validate_coder_followup_response(
    text: str,
    *,
    unresolved_items: Sequence[UnresolvedReviewItem],
    human_requirements,
) -> StructuredCoderFollowup | str:
    prompt_context = render_coder_human_requirements_prompt_context(human_requirements)
    structured_followup = validate_structured_coder_followup(text)
    if structured_followup is not None:
        _validate_structured_coder_followup_items(
            structured_followup,
            unresolved_items=unresolved_items,
        )
        validate_structured_human_requirements_acknowledgement(
            structured_followup.human_requirements.addressed_ids,
            checked_discussion_directly=structured_followup.human_requirements.checked_discussion_directly,
            surfaced_requirement_ids=prompt_context.surfaced_requirement_ids,
            requires_direct_discussion_ack=prompt_context.requires_direct_discussion_ack,
        )
        return structured_followup

    raise AgentLoopError("Coder response did not use the required structured format.")


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
                    source_status=item.source_status,
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
                    source_status=item.source_status,
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
                source_status=item.source_status,
                notes=tuple(notes),
            )
            if retain_future:
                next_unresolved.append(future_item)
            else:
                future_items.append(future_item)
    return next_unresolved, future_items


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
    dispositions = _maybe_fill_resolved_dispositions_from_prose(
        parsed,
        reviewer=reviewer,
        unresolved_items=unresolved_items,
    )
    disposition_ids = [item.item_id for item in dispositions]
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
    return ParsedPlanReview(
        state=parsed.state,
        summary=parsed.summary,
        items=parsed.items,
        dispositions=dispositions,
        raw_dispositions_text=parsed.raw_dispositions_text,
    )


def _record_prior_item_disposition(
    prior_dispositions: dict[str, list[ReviewItemDisposition]],
    disposition: ReviewItemDisposition,
    *,
    flow: str,
    round_number: int,
    subject: str,
    reviewer_name: str,
) -> None:
    if disposition.item_id not in prior_dispositions:
        raise AgentLoopError(
            "Resumed "
            f"{flow} round {round_number} reconstructed prior items "
            f"{', '.join(sorted(prior_dispositions)) or '(none)'}, but {reviewer_name} "
            f"dispositioned unknown item `{disposition.item_id}` for subject `{subject}`."
        )
    prior_dispositions[disposition.item_id].append(disposition)


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
