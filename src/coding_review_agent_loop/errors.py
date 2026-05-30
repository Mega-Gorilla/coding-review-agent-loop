"""Shared exceptions for the agent loop."""

from __future__ import annotations


class AgentLoopError(RuntimeError):
    """Raised for expected orchestration failures."""


class QuotaResetExceededError(AgentLoopError):
    """Raised when a rate-limit reset time exceeds the auto-retry threshold.

    Exit code 3 distinguishes "quota exhausted, retry later" from
    "something is broken, fix it first" (exit code 1).
    """

    EXIT_CODE = 3
