"""LLM repair pass for malformed review outputs.

When an agent produces a review that fails strict schema validation, this module
attempts to recover it by calling gemini-3.1-flash-lite as a format-repair
assistant.  If the repair also fails validation, the caller treats the result
as blocking per the issue guardrails.
"""

from __future__ import annotations

import logging
import os

_logger = logging.getLogger(__name__)

# v7 prompt — tested against all 34 historical format-failure samples (100%).
# Uses str.replace("{raw_response}", raw) for substitution because the prompt
# itself contains literal { } characters in the JSON examples.
_REPAIR_PROMPT = """\
You are a format-repair assistant. An AI agent produced a code review that failed strict schema validation. Extract its intent and reformat it into one of these two valid formats.

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

## ARRAY FIELD TYPES:
- blocking_items, same_pr_followups, future_followups, blocking_plan_issues, same_plan_followups -> STRINGS
- prior_item_dispositions, prior_plan_item_dispositions -> OBJECTS {"item_id":..., "disposition":..., "note":...}
- disposition values: "resolved", "blocking", "same-pr"/"same-plan", or "future" ONLY

## STATE RULES:
### APPROVED: blocking_items=[], same_pr_followups=[], prior dispositions only "resolved"/"future"
### BLOCKING: future_followups=[], prior dispositions MUST NOT use "future"

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

## FORMAT:
1. Start DIRECTLY with { — no prose, no markdown fences.
2. After }: <!-- AGENT_STATE: X --> (pr_review) OR <!-- AGENT_PLAN_STATE: X --> (plan_review). DIFFERENT MARKERS.
3. JSON "state" matches X. Then: -- Reviewer Name. STOP.
4. plan_review if original has AGENT_PLAN_STATE / blocking_plan_issues / same_plan_followups / prior_plan_item_dispositions.

Output ONLY the repaired response. No explanations.

## Original (malformed) response:

{raw_response}"""

_REPAIR_MODEL = "gemini-3.1-flash-lite"


def attempt_repair(raw: str) -> str | None:
    """Call gemini-3.1-flash-lite to reformat a malformed review response.

    Returns the repaired text on success, or None when the repair service is
    unavailable (missing SDK, missing API key) or returns an error.
    The caller is responsible for re-validating the returned text.
    """
    try:
        from google import genai as _genai  # type: ignore[import-untyped]
    except ImportError:
        _logger.debug("google-genai SDK not available; skipping repair pass")
        return None

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        _logger.debug("No GEMINI_API_KEY or GOOGLE_API_KEY found; skipping repair pass")
        return None

    prompt = _REPAIR_PROMPT.replace("{raw_response}", raw)
    try:
        client = _genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=_REPAIR_MODEL,
            contents=prompt,
        )
        text = response.text
        if not isinstance(text, str) or not text.strip():
            _logger.debug("repair model returned empty response")
            return None
        return text
    except Exception as exc:
        _logger.debug("repair pass call failed: %s", exc)
        return None
