"""Validation helpers for keeping coder-reported work inside the assigned checkout."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Iterable, Sequence

from .errors import AgentLoopError


TEST_SECTION_RE = re.compile(r"(?im)^\s*tests(?:\s+run)?\s*:\s*(?P<body>.*)$")
ABSOLUTE_PATH_RE = re.compile(r"(?<![\w.-])/(?:[^\s`'\"|;&)<>]+)")
HOME_PATH_RE = re.compile(r"(?<![\w.-])(?:~|\$HOME)/(?:[^\s`'\"|;&)<>]+)")
WINDOWS_PATH_RE = re.compile(r"(?<![\w.-])[A-Za-z]:\\[^\s`'\"|;&)<>]+")


def _canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_inside(path: Path, assigned_workdir: Path) -> bool:
    try:
        path.relative_to(assigned_workdir)
        return True
    except ValueError:
        return False


def _normalize_reported_path(raw_path: str) -> Path | None:
    cleaned = raw_path.strip().strip("`'\".,")
    if not cleaned:
        return None
    if cleaned.startswith("$HOME/"):
        cleaned = str(Path.home()) + cleaned[len("$HOME") :]
    if cleaned.startswith("~/") or cleaned.startswith("/"):
        return _canonical(Path(cleaned))
    if WINDOWS_PATH_RE.fullmatch(cleaned):
        return Path(cleaned)
    return None


def _reported_paths(command: str) -> Iterable[str]:
    yield from HOME_PATH_RE.findall(command)
    yield from ABSOLUTE_PATH_RE.findall(command)
    yield from WINDOWS_PATH_RE.findall(command)

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    for index, token in enumerate(tokens):
        if token in {"cd", "-C", "--directory"} and index + 1 < len(tokens):
            yield tokens[index + 1]
        elif token.startswith("--directory="):
            yield token.split("=", 1)[1]
        elif token.startswith("$HOME/") or token.startswith("~/") or token.startswith("/"):
            yield token


def validate_test_commands_within_workdir(
    tests_run: Sequence[str] | None,
    *,
    assigned_workdir: Path,
) -> None:
    if not tests_run:
        return
    assigned = _canonical(assigned_workdir)
    for command in tests_run:
        for raw_path in _reported_paths(command):
            path = _normalize_reported_path(raw_path)
            if path is None:
                continue
            if WINDOWS_PATH_RE.fullmatch(raw_path.strip().strip("`'\".,")):
                raise AgentLoopError(
                    "Coder reported tests from a Windows-style path outside the assigned "
                    f"checkout: {raw_path!r} in command {command!r}. Assigned checkout: {assigned}"
                )
            if path == assigned or _is_inside(path, assigned):
                continue
            raise AgentLoopError(
                "Coder reported tests from outside the assigned checkout: "
                f"{raw_path!r} in command {command!r}. Assigned checkout: {assigned}"
            )


def extract_reported_tests_from_response(text: str) -> tuple[str, ...]:
    """Return the coder's public test-report lines, avoiding quoted issue context."""
    lines = text.splitlines()
    reports: list[str] = []
    for index, line in enumerate(lines):
        match = TEST_SECTION_RE.match(line)
        if not match:
            continue
        body = match.group("body").strip()
        if body:
            reports.append(body)
            continue
        continuation: list[str] = []
        for next_line in lines[index + 1 :]:
            stripped = next_line.strip()
            if not stripped:
                if continuation:
                    break
                continue
            if stripped.startswith("<!-- AGENT_") or stripped.startswith("-- "):
                break
            if re.match(r"^#{1,6}\s+", stripped):
                break
            continuation.append(stripped.removeprefix("- ").strip())
        if continuation:
            reports.extend(continuation)
    return tuple(reports)


def validate_response_tests_within_workdir(text: str, *, assigned_workdir: Path) -> None:
    validate_test_commands_within_workdir(
        extract_reported_tests_from_response(text),
        assigned_workdir=assigned_workdir,
    )


def validate_assigned_head_advanced(
    *,
    before_head: str | None,
    after_head: str | None,
    assigned_workdir: Path,
) -> None:
    if not before_head or not after_head:
        return
    if before_head == after_head:
        raise AgentLoopError(
            "Coder reported a PR, but the assigned checkout HEAD did not advance. "
            f"Assigned checkout: {_canonical(assigned_workdir)}; HEAD: {after_head}"
        )
