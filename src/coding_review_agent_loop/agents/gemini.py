"""Gemini CLI backend."""

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
from ..protocol import CLARIFY_RE, STATE_RE
from ..runner import Runner
from ..usage import UsageMetadata, coerce_int, first_present

if TYPE_CHECKING:
    from ..config import AgentLoopConfig


PUBLIC_RESPONSE_MARKER = "=== AGENT_LOOP_PUBLIC_RESPONSE_BELOW ==="


def _with_public_response_marker_instruction(prompt: str) -> str:
    return f"""{prompt}

IMPORTANT FOR GEMINI CLI OUTPUT FILTERING:

Gemini CLI may print tool-use narration, diagnostics, or internal status text
before your final answer.

When you are ready to provide the response that should be posted publicly to
GitHub, print this exact line immediately before it:

{PUBLIC_RESPONSE_MARKER}

Only content after that line will be posted to GitHub. Do not print the marker
until you are done with all internal reasoning, tool use, and review work.
"""


def _strip_public_response_marker(raw: str) -> str:
    if PUBLIC_RESPONSE_MARKER not in raw:
        return raw
    return raw.rsplit(PUBLIC_RESPONSE_MARKER, 1)[1].lstrip("\n")


def _strip_gemini_preamble(raw: str) -> str:
    """Drop Gemini CLI diagnostics that can appear before the final response."""
    marker_stripped = _strip_public_response_marker(raw)
    if marker_stripped != raw:
        return marker_stripped

    marker_matches = [*STATE_RE.finditer(raw), *CLARIFY_RE.finditer(raw)]
    if not marker_matches:
        return raw

    public_end = max(match.start() for match in marker_matches)
    separator = "\n---\n"
    separator_at = raw.find(separator, 0, public_end)
    if separator_at == -1:
        return raw

    return raw[separator_at + len(separator) :].lstrip("\n")

def _normalize_gemini_usage(payload: object) -> UsageMetadata | None:
    if not isinstance(payload, dict):
        return None
    input_tokens = coerce_int(
        first_present(payload, "input_tokens", "inputTokenCount", "promptTokenCount")
    )
    cached_input_tokens = coerce_int(
        first_present(payload, "cached_input_tokens", "cachedInputTokenCount")
    )
    output_tokens = coerce_int(
        first_present(payload, "output_tokens", "outputTokenCount", "candidatesTokenCount")
    )
    total_tokens = coerce_int(first_present(payload, "total_tokens", "totalTokenCount"))
    if total_tokens is None and any(value is not None for value in (input_tokens, output_tokens)):
        total_tokens = sum(value or 0 for value in (input_tokens, output_tokens))
    if not any(
        value is not None for value in (input_tokens, cached_input_tokens, output_tokens, total_tokens)
    ):
        return None
    mode = "exact" if all(value is not None for value in (input_tokens, output_tokens, total_tokens)) else "partial"
    return UsageMetadata(
        mode=mode,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _parse_gemini_payload(
    raw: str,
) -> tuple[str, str | None, UsageMetadata | None, object | None]:
    """Extract (text, session_id, usage, raw_usage) from Gemini output."""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            text = data.get("response", raw)
            if not isinstance(text, str):
                text = raw
            session_id = data.get("session_id")
            raw_usage = first_present(data, "stats", "usage", "usageMetadata")
            return (
                _strip_gemini_preamble(text),
                session_id if isinstance(session_id, str) else None,
                _normalize_gemini_usage(raw_usage),
                raw_usage,
            )
    except (json.JSONDecodeError, ValueError):
        pass
    return _strip_gemini_preamble(raw), None, None, None


def _gemini_public_response_root(gemini_dir: Path) -> Path:
    git_marker = gemini_dir / ".git"
    if git_marker.is_file():
        gitdir_prefix = "gitdir:"
        try:
            gitdir_text = git_marker.read_text(encoding="utf-8").strip()
        except OSError:
            gitdir_text = ""
        if gitdir_text.lower().startswith(gitdir_prefix):
            git_dir = Path(gitdir_text[len(gitdir_prefix) :].strip())
            if not git_dir.is_absolute():
                git_dir = gemini_dir / git_dir
            return git_dir.resolve() / "agent-loop" / "responses"
        return gemini_dir / ".agent-loop-responses"

    return git_marker / "agent-loop" / "responses"


class GeminiBackend:
    name: AgentName = "gemini"
    display_name = "Gemini"
    signature = "Google Gemini"

    def workdir(self, config: AgentLoopConfig) -> Path:
        return config.gemini_dir

    def default_args(self, *, dangerous: bool) -> tuple[str, ...]:
        return ("--yolo", "--skip-trust") if dangerous else ()

    def run(
        self,
        runner: Runner,
        config: AgentLoopConfig,
        prompt: str,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> AgentResult:
        # Gemini CLI only allows file writes inside the trusted workspace (or
        # its own private temp dir, whose path we do not know ahead of time).
        # Keep the response file inside the git dir so it is writable but never
        # dirties the reviewed worktree. In linked worktrees, .git is a pointer
        # file, so resolve it before creating children beneath it.
        response_path = public_response_path(
            config,
            "gemini",
            root=_gemini_public_response_root(config.gemini_dir),
        )
        log_path = agent_log_path(config, "gemini", run_id=run_id)
        log(config, f"Starting Gemini in {config.gemini_dir}; log: {log_path}; response: {response_path}")
        args = [
            config.gemini_cmd,
            "--prompt",
            _with_public_response_marker_instruction(
                with_public_response_file_instruction(prompt, response_path)
            ),
            *config.gemini_args,
        ]
        if session_id:
            args += ["--resume", session_id]
        result = runner.run_with_log(
            args,
            cwd=config.gemini_dir,
            log_path=log_path,
            label="Gemini",
            progress_interval_seconds=config.progress_interval_seconds,
            check=False,
        )
        log(config, f"Gemini finished; log: {log_path}")
        message_text, new_session_id, usage, raw_usage = _parse_gemini_payload(result.stdout)
        response_file_text = read_public_response_file(response_path)
        return AgentResult(
            text=response_file_text or message_text,
            response_file_text=response_file_text,
            message_text=message_text,
            session_id=new_session_id,
            log_path=log_path,
            returncode=result.returncode,
            usage=usage,
            raw_usage=raw_usage,
        )


BACKEND = GeminiBackend()
