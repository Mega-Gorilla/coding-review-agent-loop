"""Claude Code backend."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from .base import (
    AgentName,
    AgentResult,
    public_response_path,
    read_public_response_file,
    with_public_response_file_instruction,
)
from ..logging import agent_log_path, log
from ..runner import Runner

if TYPE_CHECKING:
    from ..config import AgentLoopConfig


def _parse_claude_output(raw: str) -> tuple[str, str | None]:
    """Extract (text, session_id) from Claude's --output-format json response."""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            text = data.get("result", raw)
            if not isinstance(text, str):
                text = raw
            return text, data.get("session_id")
    except (json.JSONDecodeError, ValueError):
        pass
    return raw, None


class ClaudeBackend:
    name: AgentName = "claude"
    display_name = "Claude"
    signature = "Anthropic Claude"

    def workdir(self, config: AgentLoopConfig) -> Path:
        return config.claude_dir

    def default_args(self, *, dangerous: bool) -> tuple[str, ...]:
        return ("--dangerously-skip-permissions",) if dangerous else ()

    def run(
        self,
        runner: Runner,
        config: AgentLoopConfig,
        prompt: str,
        session_id: str | None = None,
    ) -> AgentResult:
        response_path = public_response_path(config, "claude")
        args = [config.claude_cmd, "--print", "--output-format", "json", *config.claude_args]
        if session_id:
            args += ["--resume", session_id]
        args.append(with_public_response_file_instruction(prompt, response_path))
        log_path = agent_log_path(config, "claude")
        log(config, f"Starting Claude in {config.claude_dir}; log: {log_path}; response: {response_path}")
        result = runner.run_with_log(
            args,
            cwd=config.claude_dir,
            log_path=log_path,
            label="Claude",
            progress_interval_seconds=config.progress_interval_seconds,
            check=False,
        )
        log(config, f"Claude finished; log: {log_path}")
        text, new_session_id = _parse_claude_output(result.stdout)
        return AgentResult(
            text=read_public_response_file(response_path) or text,
            session_id=new_session_id,
            log_path=log_path,
            returncode=result.returncode,
        )


BACKEND = ClaudeBackend()
