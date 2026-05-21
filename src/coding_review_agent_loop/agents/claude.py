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
from ..usage import UsageMetadata

if TYPE_CHECKING:
    from ..config import AgentLoopConfig


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _normalize_claude_usage(payload: object) -> UsageMetadata | None:
    if not isinstance(payload, dict):
        return None
    input_tokens = _coerce_int(payload.get("input_tokens") or payload.get("inputTokens"))
    cached_input_tokens = _coerce_int(
        payload.get("cached_input_tokens") or payload.get("cache_read_input_tokens")
    )
    output_tokens = _coerce_int(payload.get("output_tokens") or payload.get("outputTokens"))
    total_tokens = _coerce_int(payload.get("total_tokens"))
    if total_tokens is None and any(value is not None for value in (input_tokens, output_tokens)):
        total_tokens = sum(value or 0 for value in (input_tokens, output_tokens))
    if not any(
        value is not None for value in (input_tokens, cached_input_tokens, output_tokens, total_tokens)
    ):
        return None
    return UsageMetadata(
        mode="partial" if cached_input_tokens is None else "exact",
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _parse_claude_output(raw: str) -> tuple[str, str | None, UsageMetadata | None, object | None]:
    """Extract (text, session_id) from Claude's --output-format json response."""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            text = data.get("result", raw)
            if not isinstance(text, str):
                text = raw
            raw_usage = data.get("usage")
            if raw_usage is None and isinstance(data.get("result_message"), dict):
                raw_usage = data["result_message"].get("usage")
            return text, data.get("session_id"), _normalize_claude_usage(raw_usage), raw_usage
    except (json.JSONDecodeError, ValueError):
        pass
    return raw, None, None, None


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
        run_id: str | None = None,
    ) -> AgentResult:
        response_path = public_response_path(config, "claude")
        args = [config.claude_cmd, "--print", "--output-format", "json", *config.claude_args]
        if session_id:
            args += ["--resume", session_id]
        args.append(with_public_response_file_instruction(prompt, response_path))
        log_path = agent_log_path(config, "claude", run_id=run_id)
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
        text, new_session_id, usage, raw_usage = _parse_claude_output(result.stdout)
        return AgentResult(
            text=read_public_response_file(response_path) or text,
            session_id=new_session_id,
            log_path=log_path,
            returncode=result.returncode,
            usage=usage,
            raw_usage=raw_usage,
        )


BACKEND = ClaudeBackend()
