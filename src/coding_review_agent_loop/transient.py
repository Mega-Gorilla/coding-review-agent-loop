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


_ANTIGRAVITY_ERROR_LINE_RE = re.compile(r"(?im)^\s*(?:error|fatal)\s*[:\[]")
_ANTIGRAVITY_ERROR_JSON_RE = re.compile(
    r'(?is)^\s*\{.{0,2000}?"(?:error|code|status|message)"\s*:',
)


def classify_antigravity_capacity(
    text: str,
    *,
    returncode: int | None,
    empty_response: bool,
    signatures: tuple[str, ...],
) -> AntigravityCapacityClassification:
    """Recognize framed Antigravity provider-capacity diagnostics.

    Capacity words in a normal review must not trigger fallback: this classifier
    only considers a failed/empty invocation and an error-shaped standalone
    diagnostic (or compact JSON error payload).  Authentication and billing
    retain their non-retryable precedence.
    """
    if (returncode in (None, 0) and not empty_response) or not text.strip():
        return AntigravityCapacityClassification(False)
    if NON_RETRYABLE_AGENT_OUTPUT_RE.search(text):
        return AntigravityCapacityClassification(False)
    # A provider normally prints its failure at either end; retaining a bounded
    # tail also avoids a quoted issue report in a long agent transcript.
    stripped = text.strip()
    diagnostic = stripped if len(stripped) <= 2400 else f"{stripped[:1200]}\n{stripped[-1200:]}"
    framed = bool(
        _ANTIGRAVITY_ERROR_LINE_RE.search(diagnostic)
        or _ANTIGRAVITY_ERROR_JSON_RE.search(diagnostic)
    )
    if not framed:
        return AntigravityCapacityClassification(False)
    if any(signature.lower() in diagnostic.lower() for signature in signatures):
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
