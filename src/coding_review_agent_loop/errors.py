"""Shared exceptions for the agent loop."""

from __future__ import annotations


class AgentLoopError(RuntimeError):
    """Raised for expected orchestration failures."""


class QuotaExhaustedError(AgentLoopError):
    """Raised when an API quota is exhausted with a reset time beyond the retry threshold."""

    def __init__(self, message: str, reset_seconds: int | None = None) -> None:
        super().__init__(message)
        self.reset_seconds = reset_seconds
