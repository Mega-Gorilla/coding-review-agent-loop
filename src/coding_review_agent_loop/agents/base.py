"""Agent backend protocol and shared types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
import uuid
from typing import TYPE_CHECKING, Literal, Protocol

from ..runner import Runner
from ..usage import UsageMetadata

if TYPE_CHECKING:
    from ..config import AgentLoopConfig

AgentName = Literal["claude", "codex", "gemini"]


@dataclass(frozen=True)
class AgentResult:
    text: str
    response_file_text: str | None = None
    message_text: str | None = None
    session_id: str | None = None
    log_path: Path | None = None
    returncode: int = 0
    usage: UsageMetadata | None = None
    raw_usage: object | None = None


class AgentBackend(Protocol):
    name: AgentName
    display_name: str
    signature: str

    def workdir(self, config: AgentLoopConfig) -> Path: ...

    def default_args(self, *, dangerous: bool) -> tuple[str, ...]: ...

    def run(
        self,
        runner: Runner,
        config: AgentLoopConfig,
        prompt: str,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> AgentResult: ...


def _safe_repo_slug(repo: str) -> str:
    return repo.replace("/", "-").replace(":", "-")


def public_response_path(
    config: AgentLoopConfig,
    agent: AgentName,
    *,
    root: Path | None = None,
) -> Path:
    base_dir = (
        root
        if root is not None
        else Path(tempfile.gettempdir())
        / "coding-review-agent-loop"
        / "responses"
        / _safe_repo_slug(config.repo)
    )
    path = (
        base_dir
        / agent
        / f"{uuid.uuid4().hex}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def with_public_response_file_instruction(prompt: str, response_path: Path) -> str:
    return f"""{prompt}

PUBLIC RESPONSE FILE:

Write the final public response that should be posted to GitHub to this file:

{response_path}

The orchestrator will post only that file's contents when it exists and is
non-empty. Keep internal tool narration, planning notes, diagnostics, and
scratch output out of that file. Include the required AGENT_STATE / AGENT_PR /
AGENT_CLARIFY markers in the file, as requested above.
"""


def read_public_response_file(response_path: Path) -> str | None:
    try:
        text = response_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    text = text.strip()
    return text or None
