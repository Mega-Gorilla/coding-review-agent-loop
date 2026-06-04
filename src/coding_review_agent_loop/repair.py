"""LLM repair pass for malformed review/coder-followup outputs.

When an agent produces a review or coder follow-up that fails strict schema
validation, this module attempts to recover it by calling gemini-3.1-flash-lite
as a format-repair assistant.  If the repair also fails validation, the caller
treats the result as blocking per the issue guardrails.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence

from .agents.gemini import _parse_gemini_payload

_logger = logging.getLogger(__name__)

# v13 prompt — adds repair guidance for human-requirements marker, active approved dispositions,
# blocking+future dispositions, approved+current-plan future_followups, and same-round confusion:
#   - _reviewer_human_requirements_instruction for pr_review/plan_review missing HUMAN_REQUIREMENTS_RESOLVED
#   - STATE RULES updated: blocking reviews must explicitly disposition ALL allowed prior items; future forbidden
#   - Worked Example 2 updated: shows explicit non-future disposition instead of omission
#   - New state rules for approved+active dispositions, blocking+future, approved+current-plan future_followups
#   - New Examples 6-12 covering all new repair scenarios
# v12 prompt — adds explicit prior-item disposition repair context:
#   - lists carried prior IDs that may be dispositioned
#   - lists unknown prior-item disposition IDs that must be removed
#   - reminds agents that same-round findings are informational only
# v11 prompt — constrains repair to the caller's expected response kind when supplied
# and preserves signed human-requirements acknowledgements on plan revisions:
#   - expected_kind prevents plan_revision repair from drifting into coder_followup
#   - plan_revision repair may include the signed human requirements marker/section before AGENT_PLAN_STATE
# v10 prompt — adds plan_revision repair and keeps coder_followup repair bugs fixed:
#   - fenced JSON (```json ... ```) now explicitly handled
#   - addressed_items vs human_requirements.addressed_ids distinction clarified
#   - plan_revision / plan_steps inputs choose the plan revision schema
# Uses str.replace("{raw_response}", raw, 1) for substitution because the prompt
# itself contains literal { } characters in the JSON examples.
_REPAIR_PROMPT = """\
You are a format-repair assistant. An AI agent produced a code review, plan review, plan revision, or coder follow-up that failed strict schema validation. Extract its intent and reformat it into one of these four valid formats.

{expected_kind_instruction}

{coder_followup_required_items_instruction}

{coder_followup_human_requirements_instruction}

{reviewer_human_requirements_instruction}

{prior_item_dispositions_instruction}

## Valid Format A — PR Review:

{
  "schema_version": 1,
  "kind": "pr_review",
  "state": "approved" | "blocking",
  "summary": "<short summary>",
  "blocking_items": [],
  "same_pr_followups": [],
  "future_followups": [],
  "prior_item_dispositions": [
    {"item_id": "item-1", "disposition": "resolved"},
    {"item_id": "item-2", "disposition": "same-pr", "note": "reason still needed"}
  ]
}
<!-- AGENT_STATE: approved -->
-- <Reviewer Name>

## Valid Format B — Plan Review:

{
  "schema_version": 1,
  "kind": "plan_review",
  "state": "approved" | "blocking",
  "summary": "<short summary>",
  "blocking_plan_issues": [],
  "same_plan_followups": [],
  "future_followups": [],
  "prior_plan_item_dispositions": [
    {"item_id": "item-1", "disposition": "resolved"}
  ]
}
<!-- AGENT_PLAN_STATE: approved -->
-- <Reviewer Name>

## Valid Format C — Coder Follow-up:

{
  "schema_version": 1,
  "kind": "coder_followup",
  "state": "approved" | "blocking",
  "summary": "<short summary>",
  "addressed_items": ["item-1"],
  "remaining_items": ["item-2"],
  "addressed_item_notes": {"item-1": "<how it was resolved>"},
  "remaining_item_notes": {"item-2": "<why it remains>"},
  "human_requirements": {
    "addressed_ids": ["Requirement 1"],
    "checked_discussion_directly": false
  }
}
<!-- AGENT_STATE: approved -->
-- <Coder Name>

## Valid Format D — Plan Revision:

{
  "schema_version": 1,
  "kind": "plan_revision",
  "state": "blocking",
  "summary": "<short summary>",
  "prior_plan_item_dispositions": [
    {"item_id": "item-1", "disposition": "resolved", "note": "covered by the revised plan"}
  ],
  "plan_steps": ["Update the parser.", "Add regression tests."]
}
<!-- HUMAN_REQUIREMENTS_ADDRESSED -->

### Human requirements

- Requirement 1: <how the revised plan addresses this signed human requirement, if present in the original>
<!-- AGENT_PLAN_STATE: blocking -->
-- <Coder Name>

## ARRAY FIELD TYPES (Format A/B/D):
- blocking_items, same_pr_followups, future_followups, blocking_plan_issues, same_plan_followups, plan_steps -> STRINGS
- prior_item_dispositions, prior_plan_item_dispositions -> OBJECTS {"item_id":..., "disposition":..., "note":...}
- disposition values: "resolved", "blocking", "same-pr"/"same-plan", or "future" ONLY

## ARRAY FIELD TYPES (Format C) — TWO DIFFERENT ID TYPES, DO NOT CONFUSE THEM:
- addressed_items, remaining_items -> reviewer ITEM IDs only: short slugs matching [A-Za-z0-9][A-Za-z0-9._-]*
  Examples: "item-1", "item-2"
  NEVER put human requirement labels ("Requirement 1") here — they contain spaces and will fail.
- human_requirements.addressed_ids -> human REQUIREMENT LABELS like "Requirement 1", "Requirement 2"
  These are different from item IDs and may contain spaces.
- human_requirements.addressed_ids may contain only exact signed labels surfaced in the original prompt/response context.
- If no signed human requirement labels are surfaced in the repair context, use "addressed_ids": [].
- Never convert issue numbers, issue acceptance criteria, reviewer item IDs, reviewer comments, summaries, or arbitrary labels into signed human requirements.
- In mixed cases, keep valid surfaced Requirement N labels and drop invalid extras.
- Every reviewer item ID from the original must appear in EITHER addressed_items OR remaining_items, not both.
- If a "Required coder follow-up item IDs" block is provided above, every listed ID must appear in exactly one of addressed_items or remaining_items even if the malformed markdown omitted it.
- Legacy markdown markers like <!-- HUMAN_REQUIREMENTS_ADDRESSED --> and a ### Human requirements section are evidence for human_requirements.addressed_ids and checked_discussion_directly only; they do not classify regular reviewer or orchestrator-injected item-N records.
- Do NOT include human requirement labels in addressed_items or remaining_items.

## STATE RULES (Format A/B):
### APPROVED: blocking_items=[], same_pr_followups=[], prior dispositions only "resolved" or "future"
### APPROVED + active same-pr/same-plan/blocking prior dispositions:
  - If the note clearly says the current PR/plan already covers the item: change disposition to "resolved"
  - If the item is genuinely still open: change state to "blocking" and keep the active disposition
  - Never return "approved" with active same-pr, same-plan, or blocking prior dispositions
### APPROVED with future_followups that are actually current-plan/PR concerns:
  - Evaluate each entry in future_followups — genuinely deferred independent work may stay
  - If any entry is actually required for the current plan/PR to be correct (not independent future work):
    move it to blocking_plan_issues/blocking_items and change state to "blocking"
  - Only genuinely independent later work may remain in future_followups on an approved review
### BLOCKING: future_followups=[], ALL prior items in the allowed list must appear in prior_item_dispositions
  - No item may be omitted, including items that were "future" in a prior round
  - "future" disposition is forbidden in blocking reviews
  - Only "blocking"/"same-pr"/"same-plan"/"resolved" are valid prior dispositions in blocking reviews
### Invalid enum values: "still blocking" → "blocking", "still same-pr" → "same-pr",
  "still same-plan" → "same-plan" — normalize without dropping any other items

## DEDUPE RULES (Format B):
- Same-plan follow-ups and Future follow-ups are mutually exclusive.
- If the same plan-review concern or paraphrase appears in blocking_plan_issues and same_plan_followups, keep blocking_plan_issues and drop the duplicate same_plan_followups entry.
- If the same plan-review concern or paraphrase appears in same_plan_followups and future_followups, keep same_plan_followups/current-plan work and drop the duplicate future_followups entry.
- If the same plan-review concern or paraphrase appears in blocking_plan_issues and future_followups, keep blocking_plan_issues and drop the duplicate future_followups entry.

## STATE RULES (Format C):
### APPROVED: remaining_items=[]
### BLOCKING: remaining_items is non-empty

## STATE RULES (Format D):
### BLOCKING only. plan_revision.state must be "blocking" and must include at least one plan_steps string.

## FORMAT SELECTION:
- If an expected response kind is provided above, use ONLY that format. Do not infer a different kind from keywords in the malformed response.
- Use Format C if the original contains "coder_followup" or "addressed_items" or "remaining_items".
- Use Format D if the original contains "plan_revision" or "plan_steps".
- Use Format B if the original contains AGENT_PLAN_STATE / blocking_plan_issues / same_plan_followups / prior_plan_item_dispositions.
- Otherwise use Format A.

## WORKED EXAMPLE 1 — approved + same-PR items (change to blocking, DISCARD futures):

Original (malformed): approved state, has same-PR items AND future items.

CORRECT repair: change state to blocking, keep same-PR items, DISCARD future items entirely.
{
  "schema_version": 1, "kind": "pr_review", "state": "blocking",
  "summary": "...",
  "blocking_items": [],
  "same_pr_followups": ["Fix the CSS class name"],
  "future_followups": [],
  "prior_item_dispositions": []
}
<!-- AGENT_STATE: blocking -->
-- Reviewer

The future item ("Consider improving contrast ratio") is DISCARDED. It can be raised again in a later round.

## WORKED EXAMPLE 2 — prior item already filed as "future", now in a blocking review:

Original (malformed): blocking review, prior item-1 was already "future" in a previous round,
but item-1 is in the allowed carried prior item IDs list.

CORRECT repair: include item-1 explicitly with a valid non-future disposition. Never omit it.
{
  "schema_version": 1, "kind": "pr_review", "state": "blocking",
  "summary": "...",
  "blocking_items": [],
  "same_pr_followups": ["Fix the CLI flag in error message"],
  "future_followups": [],
  "prior_item_dispositions": [
    {"item_id": "item-1", "disposition": "resolved", "note": "No longer relevant given current changes"},
    {"item_id": "item-2", "disposition": "same-pr", "note": "CLI flag still wrong"}
  ]
}
<!-- AGENT_STATE: blocking -->
-- Reviewer

item-1 must appear because all allowed prior items must be explicitly dispositioned in blocking reviews.
"future" is forbidden in blocking reviews. Use "resolved" if no longer relevant, "blocking" if it must
be fixed now, or "same-pr"/"same-plan" if it still needs attention.

## WORKED EXAMPLE 3 — coder follow-up with JSON wrapped in fences and human requirements:

Original (malformed): coder_followup JSON wrapped in ```json fences, with a ### Human requirements
section and missing <!-- HUMAN_REQUIREMENTS_ADDRESSED --> marker.

```json
{
  "schema_version": 1,
  "kind": "coder_followup",
  "state": "blocking",
  "summary": "Implemented the quota exit policy.",
  "addressed_items": ["item-1"],
  "remaining_items": [],
  "human_requirements": {
    "addressed_ids": ["Requirement 1"],
    "checked_discussion_directly": false
  }
}
```

<!-- AGENT_STATE: blocking -->

### Human requirements

- **Requirement 1**: Implemented.

-- Anthropic Claude

CORRECT repair — strip the fences, remove the prose, output bare JSON + footer + signature:
{
  "schema_version": 1,
  "kind": "coder_followup",
  "state": "blocking",
  "summary": "Implemented the quota exit policy.",
  "addressed_items": ["item-1"],
  "remaining_items": [],
  "human_requirements": {
    "addressed_ids": ["Requirement 1"],
    "checked_discussion_directly": false
  }
}
<!-- AGENT_STATE: blocking -->
-- Anthropic Claude

Notes:
- addressed_items contains real reviewer item IDs (like "item-1"), never the human-requirements ack pseudo-item
- "Requirement 1" stays in human_requirements.addressed_ids (it is a human requirement label, not an item ID)
- <!-- HUMAN_REQUIREMENTS_ADDRESSED --> is NOT needed in the structured path
- No prose, no ### sections after the JSON

## WORKED EXAMPLE 4 — plan revision with signed human requirements:

Original (malformed): markdown revised plan, with a signed human-requirements acknowledgement.

### Prior plan review item dispositions

- item-1: resolved by adding API compatibility checks.

### Revised plan

- Keep the public API unchanged.
- Add regression tests for compatibility.

<!-- HUMAN_REQUIREMENTS_ADDRESSED -->

### Human requirements

- Requirement 1: The revised plan preserves backward compatibility.

<!-- AGENT_PLAN_STATE: blocking -->
-- Anthropic Claude

CORRECT repair — output plan_revision JSON first, preserve the human requirements marker and section before AGENT_PLAN_STATE:
{
  "schema_version": 1,
  "kind": "plan_revision",
  "state": "blocking",
  "summary": "Revised the plan to preserve backward compatibility.",
  "prior_plan_item_dispositions": [
    {"item_id": "item-1", "disposition": "resolved", "note": "Added API compatibility checks."}
  ],
  "plan_steps": ["Keep the public API unchanged.", "Add regression tests for compatibility."]
}
<!-- HUMAN_REQUIREMENTS_ADDRESSED -->

### Human requirements

- Requirement 1: The revised plan preserves backward compatibility.
<!-- AGENT_PLAN_STATE: blocking -->
-- Anthropic Claude

Notes:
- When repairing plan_revision, do not output coder_followup even if the original contains a Human requirements section.
- If the original plan revision includes <!-- HUMAN_REQUIREMENTS_ADDRESSED --> and a ### Human requirements section, preserve both after the JSON and before <!-- AGENT_PLAN_STATE: blocking -->.

## WORKED EXAMPLE 5 — coder follow-up with no signed human requirements:

Original (malformed): coder_followup includes "Issue #221 acceptance criteria" in human_requirements.addressed_ids, but the repair context has no surfaced signed human requirement labels.

CORRECT repair — keep reviewer items separate and rewrite human_requirements.addressed_ids to []:
{
  "schema_version": 1,
  "kind": "coder_followup",
  "state": "blocking",
  "summary": "Updated the implementation and left one reviewer item pending.",
  "addressed_items": ["item-1"],
  "remaining_items": ["item-2"],
  "human_requirements": {
    "addressed_ids": [],
    "checked_discussion_directly": false
  }
}
<!-- AGENT_STATE: blocking -->
-- OpenAI Codex

Notes:
- Issue acceptance criteria are not signed human requirements.
- Reviewer items belong in addressed_items / remaining_items, never in human_requirements.addressed_ids.
- If surfaced signed labels include "Requirement 1" and the malformed response has ["Requirement 1", "Issue #221 acceptance criteria"], keep ["Requirement 1"] and drop "Issue #221 acceptance criteria".

## WORKED EXAMPLE 6 — approved plan_review with same-plan disposition whose note says plan covers it:

Original (malformed): approved plan_review, item-13 has disposition "same-plan" but note says "Plan now covers this".

CORRECT repair: change disposition to "resolved", keep state "approved".
{
  "schema_version": 1, "kind": "plan_review", "state": "approved",
  "summary": "...",
  "blocking_plan_issues": [], "same_plan_followups": [], "future_followups": [],
  "prior_plan_item_dispositions": [
    {"item_id": "item-13", "disposition": "resolved", "note": "Plan now covers this"}
  ]
}
<!-- AGENT_PLAN_STATE: approved -->
-- Reviewer

## WORKED EXAMPLE 7 — approved plan_review with same-plan disposition, note says work remains:

Original (malformed): approved plan_review, item-14 has disposition "same-plan" and note says work remains.

CORRECT repair: change state to "blocking", keep the same-plan disposition.
{
  "schema_version": 1, "kind": "plan_review", "state": "blocking",
  "summary": "...",
  "blocking_plan_issues": [],
  "same_plan_followups": [],
  "future_followups": [],
  "prior_plan_item_dispositions": [
    {"item_id": "item-14", "disposition": "same-plan", "note": "Test count still not updated"}
  ]
}
<!-- AGENT_PLAN_STATE: blocking -->
-- Reviewer

## WORKED EXAMPLE 8 — blocking pr_review with future_followups and a formerly-future allowed prior item:

Original (malformed): blocking pr_review, has future_followups, prior item-1 was previously "future"
and is in the allowed prior item IDs.

CORRECT repair: remove future_followups, include item-1 with explicit non-future disposition.
{
  "schema_version": 1, "kind": "pr_review", "state": "blocking",
  "summary": "...",
  "blocking_items": ["Fix the memory leak"],
  "same_pr_followups": [],
  "future_followups": [],
  "prior_item_dispositions": [
    {"item_id": "item-1", "disposition": "resolved", "note": "No longer applicable"},
    {"item_id": "item-2", "disposition": "blocking", "note": "Memory leak is still present"}
  ]
}
<!-- AGENT_STATE: blocking -->
-- Reviewer

item-1 must appear explicitly; omitting it would fail validation.
The future_followup entry is discarded; it can be raised again in a later round.

## WORKED EXAMPLE 9 — "still blocking" invalid enum, all other items preserved:

Original (malformed): pr_review with disposition "still blocking" instead of "blocking".

CORRECT repair: normalize "still blocking" → "blocking"; preserve all other items unchanged.
{
  "schema_version": 1, "kind": "pr_review", "state": "blocking",
  "summary": "...",
  "blocking_items": [],
  "same_pr_followups": [],
  "future_followups": [],
  "prior_item_dispositions": [
    {"item_id": "item-1", "disposition": "blocking", "note": "Still needs attention"},
    {"item_id": "item-2", "disposition": "resolved"}
  ]
}
<!-- AGENT_STATE: blocking -->
-- Reviewer

Only the enum value is changed; no other items are dropped.

## WORKED EXAMPLE 10 — approved review missing HUMAN_REQUIREMENTS_RESOLVED, requirements satisfied:

Original (malformed): approved pr_review with all requirements satisfied but missing the marker.
The repair context surfaced: Requirement 1.

CORRECT repair: keep state "approved", add <!-- HUMAN_REQUIREMENTS_RESOLVED --> after the JSON.
{
  "schema_version": 1, "kind": "pr_review", "state": "approved",
  "summary": "...",
  "blocking_items": [], "same_pr_followups": [], "future_followups": [],
  "prior_item_dispositions": []
}
<!-- HUMAN_REQUIREMENTS_RESOLVED -->
<!-- AGENT_STATE: approved -->
-- Reviewer

## WORKED EXAMPLE 11 — approved review missing HUMAN_REQUIREMENTS_RESOLVED, requirement unresolved:

Original (malformed): approved pr_review but Requirement 1 is not satisfied by the current PR.
The repair context surfaced: Requirement 1.

CORRECT repair: change state to "blocking", add a concrete blocking_item naming the requirement.
{
  "schema_version": 1, "kind": "pr_review", "state": "blocking",
  "summary": "...",
  "blocking_items": ["Requirement 1 is not satisfied: the PR must use absolute URLs as required."],
  "same_pr_followups": [],
  "future_followups": [],
  "prior_item_dispositions": []
}
<!-- AGENT_STATE: blocking -->
-- Reviewer

Do NOT add <!-- HUMAN_REQUIREMENTS_RESOLVED --> when blocking.

## WORKED EXAMPLE 12 — approved plan_review with same-round item in prior_plan_item_dispositions
and current-plan concerns in future_followups:

Original (malformed): approved plan_review with prior_plan_item_dispositions containing item-1
(which is a same-round finding, not a carried prior item — allowed_prior_item_ids is empty),
and future_followups containing current-plan validation details that are not genuinely deferred.

CORRECT repair: remove the invalid same-round disposition, promote current-plan concerns to
blocking_plan_issues, change state to "blocking".
{
  "schema_version": 1, "kind": "plan_review", "state": "blocking",
  "summary": "...",
  "blocking_plan_issues": [
    "The repair examples and validators for already-future prior items must be reconciled."
  ],
  "same_plan_followups": [],
  "future_followups": [],
  "prior_plan_item_dispositions": []
}
<!-- AGENT_PLAN_STATE: blocking -->
-- Reviewer

item-1 is removed because it was a same-round finding, not an eligible carried prior item.
The future_followups entry is moved to blocking_plan_issues because it concerns current-plan correctness.
Only genuinely independent later work should remain in future_followups on an approved review.

## FORMAT:
1. Start DIRECTLY with { — no prose, no markdown fences.
2. After }: For approved `pr_review` or `plan_review` that now includes `<!-- HUMAN_REQUIREMENTS_RESOLVED -->`,
   place that marker immediately after the JSON and before the AGENT_STATE/AGENT_PLAN_STATE footer.
   For `plan_revision`, place the optional signed human requirements acknowledgement before the footer.
   Then: <!-- AGENT_STATE: X --> (pr_review or coder_followup) OR <!-- AGENT_PLAN_STATE: X --> (plan_review or plan_revision). DIFFERENT MARKERS.
3. JSON "state" matches X. Then: -- Agent Name. STOP. Nothing else.

Output ONLY the repaired response. No explanations.

## Original (malformed) response:

{raw_response}"""

_REPAIR_MODEL = "gemini-3.1-flash-lite"
_SUPPORTED_EXPECTED_KINDS = {"pr_review", "plan_review", "coder_followup", "plan_revision"}


def _coder_followup_required_items_instruction(
    expected_kind: str | None,
    unresolved_item_ids: Sequence[str] | None,
) -> str:
    if unresolved_item_ids is None:
        return ""
    if expected_kind != "coder_followup":
        raise ValueError("unresolved_item_ids may only be used for coder_followup repair")
    rendered_ids = "\n".join(f"- `{item_id}`" for item_id in unresolved_item_ids)
    return (
        "## Required coder follow-up item IDs:\n"
        "The repaired coder_followup must classify every ID below in exactly one of "
        "`addressed_items` or `remaining_items`, even if the malformed response does "
        "not mention the ID:\n"
        f"{rendered_ids or '- (none)'}\n"
        "Do not put human requirement labels such as `Requirement 1` in these arrays.\n"
    )


def _coder_followup_human_requirements_instruction(
    expected_kind: str | None,
    surfaced_requirement_ids: Sequence[str] | None,
) -> str:
    if surfaced_requirement_ids is None:
        return ""
    if expected_kind != "coder_followup":
        raise ValueError("surfaced_requirement_ids may only be used for coder_followup repair")
    rendered_ids = "\n".join(f"- `{item_id}`" for item_id in surfaced_requirement_ids)
    if not surfaced_requirement_ids:
        rendered_ids = "- (none)"
    return (
        "## Surfaced signed human requirement labels for coder follow-up:\n"
        f"{rendered_ids}\n"
        "Only the exact labels above may appear in `human_requirements.addressed_ids`.\n"
        "When the list is `(none)`, set `human_requirements.addressed_ids` to `[]`. "
        "Do not use issue numbers, issue acceptance criteria, reviewer item IDs, reviewer comments, "
        "summaries, or arbitrary labels as human requirement IDs.\n"
    )


def _reviewer_human_requirements_instruction(
    expected_kind: str | None,
    reviewer_requirement_ids: Sequence[str] | None,
) -> str:
    if reviewer_requirement_ids is None:
        return ""
    if expected_kind not in {"pr_review", "plan_review"}:
        raise ValueError("reviewer_requirement_ids may only be used for pr_review or plan_review repair")
    rendered_ids = "\n".join(f"- `{req_id}`" for req_id in reviewer_requirement_ids)
    if not reviewer_requirement_ids:
        rendered_ids = "- (none)"
    state_marker = "AGENT_STATE" if expected_kind == "pr_review" else "AGENT_PLAN_STATE"
    blocking_field = "blocking_items" if expected_kind == "pr_review" else "blocking_plan_issues"
    return (
        "## Signed human requirements missing acknowledgement:\n"
        "This approved review is missing <!-- HUMAN_REQUIREMENTS_RESOLVED -->.\n"
        f"Surfaced signed human requirements:\n{rendered_ids}\n"
        "For each listed requirement, confirm whether the current plan/PR satisfies it.\n"
        "If ALL requirements are satisfied: keep state `approved` and add "
        f"`<!-- HUMAN_REQUIREMENTS_RESOLVED -->` after the JSON and before the `<!-- {state_marker}: approved -->` footer.\n"
        "If ANY requirement is NOT satisfied: change state to `blocking` and add a concrete "
        f"`{blocking_field}` entry naming the unresolved requirement. "
        "Do NOT add `<!-- HUMAN_REQUIREMENTS_RESOLVED -->` when blocking.\n"
    )


def _repair_prior_item_ids_instruction(
    allowed_prior_item_ids: Sequence[str] | None,
    unknown_prior_item_ids: Sequence[str] | None,
    same_round_context: str | None,
) -> str:
    if (
        allowed_prior_item_ids is None
        and unknown_prior_item_ids is None
        and same_round_context is None
    ):
        return ""
    allowed = ", ".join(sorted(allowed_prior_item_ids or ())) or "(none)"
    unknown = ", ".join(sorted(unknown_prior_item_ids or ())) or "(none)"
    context = (
        same_round_context
        or "Same-round findings are informational only and must not be dispositioned as prior carried items."
    )
    return (
        "## Prior item disposition repair:\n"
        "Same-round findings are informational only and must not be dispositioned as prior carried items.\n"
        f"Allowed carried prior item IDs: {allowed}\n"
        f"Unknown prior item disposition IDs to remove: {unknown}\n"
        f"Context: {context}\n"
        "Preserve valid dispositions for allowed IDs. Remove only unknown prior-item disposition entries.\n"
    )


def attempt_repair(
    raw: str,
    gemini_cmd: str,
    *,
    expected_kind: str | None = None,
    unresolved_item_ids: Sequence[str] | None = None,
    surfaced_requirement_ids: Sequence[str] | None = None,
    reviewer_requirement_ids: Sequence[str] | None = None,
    allowed_prior_item_ids: Sequence[str] | None = None,
    unknown_prior_item_ids: Sequence[str] | None = None,
    same_round_context: str | None = None,
) -> str | None:
    """Call gemini-3.1-flash-lite via the Gemini CLI to reformat a malformed review response.

    Uses the same CLI invocation path as the reviewer so no extra auth is needed.
    Returns the repaired text on success, or None when the CLI fails or returns empty output.
    The caller is responsible for re-validating the returned text.
    """
    if expected_kind is not None and expected_kind not in _SUPPORTED_EXPECTED_KINDS:
        raise ValueError(f"Unsupported expected repair kind: {expected_kind}")
    coder_followup_required_items_instruction = _coder_followup_required_items_instruction(
        expected_kind,
        unresolved_item_ids,
    )
    coder_followup_human_requirements_instruction = _coder_followup_human_requirements_instruction(
        expected_kind,
        surfaced_requirement_ids,
    )
    reviewer_human_requirements_instr = _reviewer_human_requirements_instruction(
        expected_kind,
        reviewer_requirement_ids,
    )
    prior_item_dispositions_instruction = _repair_prior_item_ids_instruction(
        allowed_prior_item_ids,
        unknown_prior_item_ids,
        same_round_context,
    )
    expected_kind_instruction = (
        "## Expected response kind:\n"
        f"You MUST repair this response as `{expected_kind}`. Output no other `kind` value.\n"
        if expected_kind is not None
        else "## Expected response kind:\nNo expected response kind was provided; choose from the format-selection rules.\n"
    )
    prompt = _REPAIR_PROMPT.replace("{expected_kind_instruction}", expected_kind_instruction, 1)
    prompt = prompt.replace(
        "{coder_followup_required_items_instruction}",
        coder_followup_required_items_instruction,
        1,
    )
    prompt = prompt.replace(
        "{coder_followup_human_requirements_instruction}",
        coder_followup_human_requirements_instruction,
        1,
    )
    prompt = prompt.replace(
        "{reviewer_human_requirements_instruction}",
        reviewer_human_requirements_instr,
        1,
    )
    prompt = prompt.replace(
        "{prior_item_dispositions_instruction}",
        prior_item_dispositions_instruction,
        1,
    )
    prompt = prompt.replace("{raw_response}", raw, 1)
    try:
        result = subprocess.run(
            [gemini_cmd, "--model", _REPAIR_MODEL, "--skip-trust", "--prompt", prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        _logger.debug("repair pass CLI invocation failed: %s", exc)
        return None
    if result.returncode != 0:
        _logger.debug("repair pass CLI exited with code %d", result.returncode)
        return None
    text, _, _, _, _ = _parse_gemini_payload(result.stdout.strip())
    return text or None
