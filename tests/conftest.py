"""Shared pytest fixtures."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _disable_repair_pass():
    """Prevent attempt_repair from making real subprocess calls during tests.

    Tests that specifically exercise the repair pass override this by patching
    coding_review_agent_loop.orchestrator.attempt_repair in their own body.
    """
    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value=None):
        yield
