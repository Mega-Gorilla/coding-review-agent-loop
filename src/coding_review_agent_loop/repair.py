"""LLM repair pass for malformed review outputs."""

from __future__ import annotations

import os

REPAIR_MODEL = "gemini-3.1-flash-lite"

# v7 prompt — 34/34 repair rate on historical format failures across both repos.
# Use str.replace("{raw_response}", raw) to substitute — do NOT use .format()
# because the prompt contains literal { } characters in the JSON examples.
_REPAIR_PROMPT_TEMPLATE = """\
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


def _build_repair_prompt(raw_text: str) -> str:
    return _REPAIR_PROMPT_TEMPLATE.replace("{raw_response}", raw_text)


def repair_review_response(raw_text: str) -> str | None:
    """Attempt to repair a malformed review response using gemini-3.1-flash-lite.

    Returns the repaired text on success, or None if the SDK is unavailable,
    no API key is configured, or the repair call fails.
    """
    try:
        from google import genai  # type: ignore[import]
    except ImportError:
        return None

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return None

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=REPAIR_MODEL,
            contents=_build_repair_prompt(raw_text),
        )
        text = response.text
        if not isinstance(text, str) or not text.strip():
            return None
        return text
    except Exception:
        return None
