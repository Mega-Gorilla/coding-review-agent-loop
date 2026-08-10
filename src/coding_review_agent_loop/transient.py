"""Lightweight transient/non-retryable agent-output classification.

Extracted from ``orchestrator`` so callers that must stay dependency-light
(e.g. the skill's ``helpers.run_external`` subprocess launcher) can decide
whether to retry an agent invocation without importing the full orchestrator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TRANSIENT_AGENT_OUTPUT_RE = re.compile(
    r"Invalid stream|empty response|malformed tool call|"
    r"network (?:reset|timeout)|connection (?:reset|timed out|timeout)|"
    r"\btimed out\b|\btimeout\b|"
    r"Internal Server Error|Bad Gateway|Service Unavailable|Gateway Timeout|"
    r"\b429\b|rate.?limit(?:ed)?|"
    r"session.?limit.?exceeded|session_limit_exceeded|too many sessions|"
    r"no capacity available|capacity.*(?:unavailable|exceeded)|"
    r"resource.?exhausted|overloaded|"
    r"\bquota\b",
    re.I,
)
NON_RETRYABLE_AGENT_OUTPUT_RE = re.compile(
    r"\bauth(?:entication|orization)?\b|unauthorized|forbidden|invalid api key|"
    r"credit|billing|dirty (?:checkout|workdir|working tree)",
    re.I,
)


def is_transient_agent_output(text: str) -> bool:
    """Return True if ``text`` looks like a transient failure worth retrying.

    A match on a transient pattern is overridden by any non-retryable signal
    (auth/billing/dirty-checkout), which should never be retried blindly.
    """
    return bool(TRANSIENT_AGENT_OUTPUT_RE.search(text)) and not bool(
        NON_RETRYABLE_AGENT_OUTPUT_RE.search(text)
    )


@dataclass(frozen=True)
class AntigravityCapacityClassification:
    """Provider-scoped capacity result used to decide model fallback."""

    is_capacity: bool
    diagnostic: str = ""


_ANTIGRAVITY_ERROR_LINE_RE = re.compile(r"(?i)^\s*(?:error|fatal)\s*[:\[]")
_ANTIGRAVITY_ERROR_JSON_RE = re.compile(
    r'^\s*\{.{0,2000}?"(?:error|code|status|message)"\s*:', re.I
)


def classify_antigravity_capacity(
    text: str,
    *,
    returncode: int | None,
    empty_response: bool,
    signatures: tuple[str, ...],
) -> AntigravityCapacityClassification:
    """Recognize framed Antigravity provider-capacity diagnostics.

    Only failed or empty invocations are eligible. A signature on a provider
    error line (or its adjacent detail line), a compact JSON error line, or an
    unframed standalone provider diagnostic qualifies. Framed provider errors
    must be terminal output so unrelated transcript wording cannot suppress a
    real capacity failure; authentication/billing retain non-retryable precedence.
    """
    if (returncode in (None, 0) and not empty_response) or not text.strip():
        return AntigravityCapacityClassification(False)
    if NON_RETRYABLE_AGENT_OUTPUT_RE.search(text):
        return AntigravityCapacityClassification(False)
    # A provider normally prints its failure at either end; retaining a bounded
    # tail avoids accepting a capacity phrase from an unrelated long transcript.
    stripped = text.strip()
    diagnostic = stripped if len(stripped) <= 2400 else f"{stripped[:1200]}\n{stripped[-1200:]}"
    lines = [line.strip() for line in diagnostic.splitlines() if line.strip()]
    lowered_signatures = tuple(signature.lower() for signature in signatures)

    def has_signature(value: str) -> bool:
        return any(signature in value.lower() for signature in lowered_signatures)

    for index, line in enumerate(lines):
        if _ANTIGRAVITY_ERROR_LINE_RE.match(line) or _ANTIGRAVITY_ERROR_JSON_RE.match(line):
            adjacent = "\n".join(lines[max(0, index - 1): index + 2])
            # agy emits an error immediately before exiting. Limit the frame to
            # the last few non-empty lines rather than inspecting normal reviewer
            # transcript text that may precede it.
            if has_signature(adjacent) and index >= len(lines) - 3:
                return AntigravityCapacityClassification(True, diagnostic)

    # Older agy versions emitted bare provider diagnostics (for example,
    # "quota exceeded please try again") on a non-zero exit. Preserve that
    # fallback trigger. A bare diagnostic must be a single line beginning with
    # a configured capacity signature, so an incidental phrase embedded in a
    # review transcript cannot qualify.
    if len(lines) == 1 and any(
        lines[0].lower().startswith(signature) for signature in lowered_signatures
    ):
        return AntigravityCapacityClassification(True, diagnostic)
    return AntigravityCapacityClassification(False)


# Phrases an agent uses when it has started required work (tests, builds) in
# the background and ends its turn waiting on it instead of finishing in the
# foreground (#588). This is purely textual: it makes no assumption about
# marker presence/absence. Eligibility for completion recovery is gated
# separately by the caller's own terminal-result validator having already
# rejected the response, so an embedded (but invalid-for-that-validator)
# marker never exempts a response from matching here.
BACKGROUNDED_COMPLETION_RE = re.compile(
    r"(?i)"
    r"\bi(?:'|')?ll wait\b|"
    r"\bwait(?:ing)? (?:for|on) (?:the )?background|"
    r"\brun(?:s|ning)? (?:it |them )?in the background\b|"
    r"\b(?:test|build|suite)\w*\b.{0,60}\bin the background\b|"
    r"\bin the background\b.{0,60}\b(?:test|build|suite)|"
    r"\b(?:you(?:'|')?ll|i(?:'|')?ll|we(?:'|')?ll) (?:get|be) notified\b|"
    r"\bwill (?:get|be) notified\b|"
    r"\blet me wait for\b|"
    r"\bonce (?:the |it )?(?:background )?(?:test|build|suite).{0,40}finish"
)


def looks_like_backgrounded_completion(text: str) -> bool:
    """Return True if ``text`` reads like the agent deferred to background work.

    Purely a phrase match; callers must independently confirm the response
    failed the relevant terminal-result validator before treating this as
    grounds for a completion-recovery attempt (#588).
    """
    return bool(BACKGROUNDED_COMPLETION_RE.search(text))
