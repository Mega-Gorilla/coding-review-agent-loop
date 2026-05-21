"""OpenAI Codex CLI backend."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .base import AgentName, AgentResult
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


def _normalize_codex_usage(payload: object) -> UsageMetadata | None:
    if not isinstance(payload, dict):
        return None
    input_tokens = _coerce_int(payload.get("input_tokens"))
    cached_input_tokens = _coerce_int(payload.get("cached_input_tokens"))
    output_tokens = _coerce_int(payload.get("output_tokens"))
    reasoning_tokens = _coerce_int(
        payload.get("reasoning_tokens") or payload.get("reasoning_output_tokens")
    )
    total_tokens = _coerce_int(payload.get("total_tokens"))
    if total_tokens is None and any(
        value is not None for value in (input_tokens, output_tokens, reasoning_tokens)
    ):
        total_tokens = sum(value or 0 for value in (input_tokens, output_tokens, reasoning_tokens))
    if not any(
        value is not None
        for value in (
            input_tokens,
            cached_input_tokens,
            output_tokens,
            reasoning_tokens,
            total_tokens,
        )
    ):
        return None
    return UsageMetadata(
        mode="exact",
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
    )


def _extract_codex_usage(raw: str) -> tuple[UsageMetadata | None, object | None]:
    last_usage: object | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type") or event.get("event")
        if event_type != "turn.completed":
            continue
        usage_payload = event.get("usage")
        if usage_payload is None and isinstance(event.get("turn"), dict):
            usage_payload = event["turn"].get("usage")
        usage = _normalize_codex_usage(usage_payload)
        if usage is not None:
            last_usage = usage_payload
            return usage, usage_payload
    return None, last_usage


class CodexBackend:
    name: AgentName = "codex"
    display_name = "Codex"
    signature = "OpenAI Codex"

    def workdir(self, config: AgentLoopConfig) -> Path:
        return config.codex_dir

    def default_args(self, *, dangerous: bool) -> tuple[str, ...]:
        return ("--dangerously-bypass-approvals-and-sandbox",) if dangerous else ()

    def run(
        self,
        runner: Runner,
        config: AgentLoopConfig,
        prompt: str,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> AgentResult:
        log_path = agent_log_path(config, "codex", run_id=run_id)
        log(config, f"Starting Codex in {config.codex_dir}; log: {log_path}")
        if config.dry_run:
            result = runner.run(
                [
                    config.codex_cmd,
                    "exec",
                    "--cd",
                    str(config.codex_dir),
                    *config.codex_args,
                    prompt,
                ],
                cwd=config.codex_dir,
            )
            log(config, f"Codex finished; log: {log_path}")
            return AgentResult(text=result.stdout, log_path=log_path, returncode=result.returncode)

        with tempfile.NamedTemporaryFile("r", encoding="utf-8", delete=False) as handle:
            output_path = handle.name
        try:
            result = runner.run_with_log(
                [
                    config.codex_cmd,
                    "exec",
                    "--cd",
                    str(config.codex_dir),
                    "--json",
                    "--output-last-message",
                    output_path,
                    *config.codex_args,
                    prompt,
                ],
                cwd=config.codex_dir,
                log_path=log_path,
                label="Codex",
                progress_interval_seconds=config.progress_interval_seconds,
                check=False,
            )
            output = Path(output_path).read_text(encoding="utf-8") if Path(output_path).exists() else ""
            if not output:
                output = result.stdout
            usage, raw_usage = _extract_codex_usage(result.stdout)
            log(config, f"Codex finished; log: {log_path}")
            return AgentResult(
                text=output,
                log_path=log_path,
                returncode=result.returncode,
                usage=usage,
                raw_usage=raw_usage,
            )
        finally:
            try:
                os.unlink(output_path)
            except FileNotFoundError:
                pass


BACKEND = CodexBackend()
