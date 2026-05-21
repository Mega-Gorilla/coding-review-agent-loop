"""Alembic migration topology validation helpers."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from .config import AgentLoopConfig, _run_git
from .errors import AgentLoopError
from .github import PullRequestMetadata
from .runner import Runner

ALEMBIC_VERSIONS_PREFIX = "alembic/versions/"


@dataclass(frozen=True)
class RevisionMetadata:
    path: str
    revision: str
    down_revisions: tuple[str, ...]


@dataclass(frozen=True)
class RevisionIndex:
    by_revision: dict[str, RevisionMetadata]
    by_path: dict[str, RevisionMetadata]


@dataclass(frozen=True)
class MigrationValidationResult:
    ok: bool
    message: str | None = None


def validate_pr_migration_topology(
    runner: Runner,
    *,
    config: AgentLoopConfig,
    checkout: Path,
    pr_metadata: PullRequestMetadata,
) -> MigrationValidationResult:
    base_branch = (pr_metadata.base_branch or config.base).strip() or config.base
    base_ref = f"origin/{base_branch}"
    changed_files = _changed_files_against_base(runner, checkout=checkout, base_ref=base_ref)
    changed_migrations = [path for path in changed_files if is_alembic_version_path(path)]
    if not changed_migrations:
        return MigrationValidationResult(ok=True)

    try:
        base_index = _build_revision_index(
            _load_git_revision_sources(runner, checkout=checkout, ref=base_ref)
        )
        pr_index = _build_revision_index(_load_worktree_revision_sources(checkout))
    except AgentLoopError as exc:
        return MigrationValidationResult(ok=False, message=str(exc))

    base_heads = _head_revisions(base_index)
    if len(base_heads) != 1:
        return MigrationValidationResult(ok=True)

    pr_heads = _head_revisions(pr_index)
    if len(pr_heads) == 1:
        return MigrationValidationResult(ok=True)

    expected_head = base_heads[0]
    offending = _find_offending_revision(
        changed_paths=changed_migrations,
        pr_index=pr_index,
        expected_head=expected_head,
        pr_heads=set(pr_heads),
    )
    if offending is None:
        return MigrationValidationResult(
            ok=False,
            message=(
                "PR changes Alembic revisions under `alembic/versions/`, but the resulting "
                f"migration graph has multiple heads: {', '.join(f'`{head}`' for head in pr_heads)}. "
                f"Base branch `{base_branch}` currently has single head `{expected_head}`. "
                "Update the new revision chain to descend from the current head or add an intentional "
                "merge migration."
            ),
        )

    declared_parent = _format_down_revisions(offending.down_revisions)
    return MigrationValidationResult(
        ok=False,
        message=(
            "PR changes Alembic revisions under `alembic/versions/`, but the resulting migration "
            f"graph branches unexpectedly. `{offending.path}` declares `down_revision = {declared_parent}`, "
            f"while base branch `{base_branch}` currently has single head `{expected_head}`. "
            f"Resulting PR heads: {', '.join(f'`{head}`' for head in pr_heads)}. "
            f"Update `{offending.path}` to descend from `{expected_head}` or add an intentional merge migration."
        ),
    )


def is_alembic_version_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.startswith(ALEMBIC_VERSIONS_PREFIX) and normalized.endswith(".py")


def _changed_files_against_base(runner: Runner, *, checkout: Path, base_ref: str) -> tuple[str, ...]:
    result = _run_git(runner, checkout, ("diff", "--name-only", f"{base_ref}...HEAD"))
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _load_git_revision_sources(
    runner: Runner,
    *,
    checkout: Path,
    ref: str,
) -> dict[str, str]:
    listing = _run_git(
        runner,
        checkout,
        ("ls-tree", "-r", "--name-only", ref, "--", "alembic/versions"),
    ).stdout
    paths = [line.strip() for line in listing.splitlines() if is_alembic_version_path(line.strip())]
    sources: dict[str, str] = {}
    for rel_path in paths:
        show = _run_git(runner, checkout, ("show", f"{ref}:{rel_path}"))
        sources[rel_path] = show.stdout
    return sources


def _load_worktree_revision_sources(checkout: Path) -> dict[str, str]:
    versions_dir = checkout / "alembic" / "versions"
    if not versions_dir.is_dir():
        return {}

    sources: dict[str, str] = {}
    for path in sorted(versions_dir.rglob("*.py")):
        rel_path = path.relative_to(checkout).as_posix()
        sources[rel_path] = path.read_text(encoding="utf-8")
    return sources


def _build_revision_index(sources: dict[str, str]) -> RevisionIndex:
    by_revision: dict[str, RevisionMetadata] = {}
    by_path: dict[str, RevisionMetadata] = {}
    for rel_path, source in sorted(sources.items()):
        metadata = parse_revision_metadata(source, path=rel_path)
        other = by_revision.get(metadata.revision)
        if other is not None:
            raise AgentLoopError(
                f"Could not validate Alembic revision metadata: duplicate revision "
                f"`{metadata.revision}` in `{other.path}` and `{metadata.path}`."
            )
        by_revision[metadata.revision] = metadata
        by_path[metadata.path] = metadata
    return RevisionIndex(by_revision=by_revision, by_path=by_path)


def parse_revision_metadata(source: str, *, path: str) -> RevisionMetadata:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise AgentLoopError(
            f"Could not validate Alembic revision metadata in `{path}`: {exc.msg}."
        ) from exc

    revision_value: str | None = None
    down_revision_value: tuple[str, ...] | None = None
    for node in tree.body:
        target_name, value_node = _assignment_target(node)
        if target_name == "revision":
            revision_value = _parse_string_literal(value_node, field="revision", path=path)
        elif target_name == "down_revision":
            down_revision_value = _parse_down_revision_literal(value_node, path=path)

    if not revision_value:
        raise AgentLoopError(f"Could not validate Alembic revision metadata in `{path}`: missing `revision`.")
    if down_revision_value is None:
        raise AgentLoopError(
            f"Could not validate Alembic revision metadata in `{path}`: missing `down_revision`."
        )
    return RevisionMetadata(path=path, revision=revision_value, down_revisions=down_revision_value)


def _assignment_target(node: ast.stmt) -> tuple[str | None, ast.AST | None]:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                return target.id, node.value
        return None, None
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id, node.value
    return None, None


def _parse_string_literal(node: ast.AST | None, *, field: str, path: str) -> str:
    if node is None:
        raise AgentLoopError(f"Could not validate Alembic revision metadata in `{path}`: missing `{field}`.")
    try:
        value = ast.literal_eval(node)
    except Exception as exc:
        raise AgentLoopError(
            f"Could not validate Alembic revision metadata in `{path}`: `{field}` must be a literal string."
        ) from exc
    if not isinstance(value, str) or not value:
        raise AgentLoopError(
            f"Could not validate Alembic revision metadata in `{path}`: `{field}` must be a literal string."
        )
    return value


def _parse_down_revision_literal(node: ast.AST | None, *, path: str) -> tuple[str, ...]:
    if node is None:
        return ()
    try:
        value = ast.literal_eval(node)
    except Exception as exc:
        raise AgentLoopError(
            f"Could not validate Alembic revision metadata in `{path}`: `down_revision` must be a "
            "literal string, tuple of strings, list of strings, or None."
        ) from exc
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) and item for item in value):
        return tuple(value)
    raise AgentLoopError(
        f"Could not validate Alembic revision metadata in `{path}`: `down_revision` must be a "
        "literal string, tuple of strings, list of strings, or None."
    )


def _head_revisions(index: RevisionIndex) -> tuple[str, ...]:
    referenced = {
        parent
        for metadata in index.by_revision.values()
        for parent in metadata.down_revisions
        if parent in index.by_revision
    }
    return tuple(sorted(revision for revision in index.by_revision if revision not in referenced))


def _find_offending_revision(
    *,
    changed_paths: list[str],
    pr_index: RevisionIndex,
    expected_head: str,
    pr_heads: set[str],
) -> RevisionMetadata | None:
    for path in changed_paths:
        metadata = pr_index.by_path.get(path)
        if metadata is None:
            continue
        if metadata.revision in pr_heads and expected_head not in metadata.down_revisions:
            return metadata
    for path in changed_paths:
        metadata = pr_index.by_path.get(path)
        if metadata is None:
            continue
        if metadata.revision in pr_heads:
            return metadata
    return None


def _format_down_revisions(down_revisions: tuple[str, ...]) -> str:
    if not down_revisions:
        return "None"
    if len(down_revisions) == 1:
        return repr(down_revisions[0])
    return repr(down_revisions)
