"""Unit tests for the lightweight transient-output classifier."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from coding_review_agent_loop import orchestrator, transient


def test_transient_signals_are_retryable() -> None:
    assert transient.is_transient_agent_output("Error: 429 Too Many Requests")
    assert transient.is_transient_agent_output("the model is overloaded")
    assert transient.is_transient_agent_output("connection timed out")
    assert transient.is_transient_agent_output("503 Service Unavailable")
    assert transient.is_transient_agent_output("resource exhausted")


def test_non_retryable_and_clean_output_are_not_transient() -> None:
    assert not transient.is_transient_agent_output("invalid api key")
    assert not transient.is_transient_agent_output("billing problem on your account")
    assert not transient.is_transient_agent_output("the plan looks good to me")
    assert not transient.is_transient_agent_output("")


def test_non_retryable_overrides_transient_signal() -> None:
    # A non-retryable signal (auth/billing) wins even when a transient term is present.
    assert not transient.is_transient_agent_output("got 429 but the api key is unauthorized")


def test_antigravity_capacity_requires_framed_provider_failure() -> None:
    signatures = ("high traffic", "try again in a minute", "429", "overload", "no capacity")
    for text in (
        "Error: Our servers are experiencing high traffic right now, please try again in a minute.",
        "Fatal: 429 Too Many Requests; temporarily at capacity",
        '{"error": {"code": "RESOURCE_EXHAUSTED", "message": "overload: no capacity"}}',
    ):
        assert transient.classify_antigravity_capacity(
            text, returncode=1, empty_response=False, signatures=signatures
        ).is_capacity
    assert not transient.classify_antigravity_capacity(
        "The review quotes: Error: Our servers are experiencing high traffic right now, please try again in a minute.",
        returncode=1,
        empty_response=False,
        signatures=signatures,
    ).is_capacity
    assert not transient.classify_antigravity_capacity(
        "Error: high traffic but invalid API key", returncode=1, empty_response=False, signatures=signatures
    ).is_capacity


def test_orchestrator_alias_preserves_identity() -> None:
    # orchestrator keeps the old private name as an alias to the moved implementation.
    assert orchestrator._is_transient_agent_output is transient.is_transient_agent_output


def test_backgrounded_completion_phrases_match() -> None:
    assert transient.looks_like_backgrounded_completion(
        "I'll wait for the background test run to finish."
    )
    assert transient.looks_like_backgrounded_completion(
        "Waiting for background tests to complete."
    )
    assert transient.looks_like_backgrounded_completion(
        "Waiting on the background test run and the exit-monitor; I'll continue once results arrive."
    )
    assert transient.looks_like_backgrounded_completion(
        "I started the full suite in the background; will get notified when it finishes."
    )
    assert transient.looks_like_backgrounded_completion(
        "You will be notified once the background build finishes."
    )
    assert transient.looks_like_backgrounded_completion(
        "Let me wait for the tests in the background to complete before finishing this."
    )
    assert transient.looks_like_backgrounded_completion(
        "Running the test suite in the background now."
    )


def test_unrelated_failure_text_does_not_match() -> None:
    assert not transient.looks_like_backgrounded_completion(
        "I do not have enough information to proceed."
    )
    assert not transient.looks_like_backgrounded_completion(
        "Please wait while I review the diff."
    )
    assert not transient.looks_like_backgrounded_completion("The tests passed and the PR is ready.")
    assert not transient.looks_like_backgrounded_completion("")


def test_backgrounded_completion_phrase_matches_regardless_of_embedded_markers() -> None:
    # The phrase heuristic is purely textual (#588): eligibility for a
    # completion-recovery attempt is gated separately, by the caller's own
    # terminal-result validator having already rejected the response -- not
    # by whether some other (possibly invalid-for-that-validator) marker is
    # present. An embedded AGENT_STATE: approved or a quoted AGENT_PLAN_STATE
    # marker must never exempt matching text from this detector.
    assert transient.looks_like_backgrounded_completion(
        "I'll wait for the background test run to finish.\n<!-- AGENT_STATE: approved -->"
    )
    assert transient.looks_like_backgrounded_completion(
        "As discussed in the prior round (<!-- AGENT_PLAN_STATE: blocking -->), "
        "I'll wait for the background build to finish before continuing."
    )
