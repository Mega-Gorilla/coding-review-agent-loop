"""LLM repair pass for malformed review/coder-followup outputs.

When an agent produces a review or coder follow-up that fails strict schema
validation, this module attempts to recover it by calling gemini-3.1-flash-lite
as a format-repair assistant.  If the repair also fails validation, the caller
treats the result as blocking per the issue guardrails.
"""

from __future__ import annotations

import logging
import subprocess

from .agents.gemini import _parse_gemini_payload

_logger = logging.getLogger(__name__)

# v10 prompt — adds plan_revision repair and keeps coder_followup repair bugs fixed:
#   - fenced JSON (```json ... ```) now explicitly handled
#   - addressed_items vs human_requirements.addressed_ids distinction clarified
#   - plan_revision / plan_steps inputs choose the plan revision schema
# Uses str.replace("{raw_response}", raw, 1) for substitution because the prompt
# itself contains literal { } characters in the JSON examples.
_REPAIR_PROMPT = """\
You are a format-repair assistant. An AI agent produced a code review, plan review, plan revision, or coder follow-up that failed strict schema validation. Extract its intent and reformat it into one of these four valid formats.

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
- Every reviewer item ID from the original must appear in EITHER addressed_items OR remaining_items, not both.
- Do NOT include human requirement labels in addressed_items or remaining_items.

## STATE RULES (Format A/B):
### APPROVED: blocking_items=[], same_pr_followups=[], prior dispositions only "resolved"/"future"
### BLOCKING: future_followups=[], prior dispositions MUST NOT use "future"

## STATE RULES (Format C):
### APPROVED: remaining_items=[]
### BLOCKING: remaining_items is non-empty

## STATE RULES (Format D):
### BLOCKING only. plan_revision.state must be "blocking" and must include at least one plan_steps string.

## FORMAT SELECTION:
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

Original (malformed): blocking review, prior item-1 was already "future" in a previous round.

CORRECT repair: OMIT item-1 from prior_item_dispositions entirely. Do not include it at all.
{
  "schema_version": 1, "kind": "pr_review", "state": "blocking",
  "summary": "...",
  "blocking_items": [],
  "same_pr_followups": ["Fix the CLI flag in error message"],
  "future_followups": [],
  "prior_item_dispositions": [
    {"item_id": "item-2", "disposition": "same-pr", "note": "CLI flag still wrong"}
  ]
}
<!-- AGENT_STATE: blocking -->
-- Reviewer

item-1 is omitted because it was already "future" — it stays future without needing to be re-dispositioned.

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

## FORMAT:
1. Start DIRECTLY with { — no prose, no markdown fences.
2. After }: <!-- AGENT_STATE: X --> (pr_review or coder_followup) OR <!-- AGENT_PLAN_STATE: X --> (plan_review or plan_revision). DIFFERENT MARKERS.
3. JSON "state" matches X. Then: -- Agent Name. STOP. Nothing else.

Output ONLY the repaired response. No explanations.

## Original (malformed) response:

{raw_response}"""

_REPAIR_MODEL = "gemini-3.1-flash-lite"


def attempt_repair(raw: str, gemini_cmd: str) -> str | None:
    """Call gemini-3.1-flash-lite via the Gemini CLI to reformat a malformed review response.

    Uses the same CLI invocation path as the reviewer so no extra auth is needed.
    Returns the repaired text on success, or None when the CLI fails or returns empty output.
    The caller is responsible for re-validating the returned text.
    """
    prompt = _REPAIR_PROMPT.replace("{raw_response}", raw, 1)
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
    text, _, _, _ = _parse_gemini_payload(result.stdout.strip())
    return text or None
