"""Materialize discuss-mode split proposals and plan-first deferred stages into
concrete follow-up GitHub issues (#476).

A discuss `split` consensus or a plan-first plan that declares deferred stages
otherwise leaves its follow-up scope only as prose. This module files one
child issue per remaining stage, links parent and children in both
directions, and is idempotent across reruns via a durable parent marker plus a
`gh issue list --search` recovery pass for the create-then-crash window.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from .config import AgentLoopConfig
from .decomposition import _decode_json_payload, _encode_json_payload
from .errors import AgentLoopError
from .followups import _normalize_followup_key
from .github import create_issue, post_issue_comment, search_issues
from .runner import Runner

MAX_SPLIT_CHILDREN = 8

SPLIT_MATERIALIZATION_MARKER_RE = re.compile(
    r"<!--\s*AGENT_DISCUSS_SPLIT:\s*(?P<payload>[A-Za-z0-9+/=_-]+)\s*-->",
    re.I,
)
SPLIT_CHILD_MARKER_RE = re.compile(
    r"<!--\s*AGENT_SPLIT_CHILD:\s*parent=(?P<parent>\d+)\s+key=(?P<key>[0-9a-f]+)\s*-->",
    re.I,
)
ISSUE_NUMBER_RE = re.compile(r"/issues/(\d+)(?:\b|$)|#(\d+)\b")


@dataclass(frozen=True)
class SplitChild:
    title: str
    key: str
    url: str | None
    number: int | None
    origin: str  # "created" or "adopted"


@dataclass(frozen=True)
class SplitMaterializationMetadata:
    parent_issue: int
    subject: str
    proposal_titles: tuple[str, ...]
    children: tuple[SplitChild, ...]


def _proposal_key(title: str) -> str:
    return _normalize_followup_key(title)


def _child_key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _child_issue_title(parent_issue: int, proposal_title: str) -> str:
    return f"[#{parent_issue} stage] {proposal_title}"[:120]


def _issue_number_from_url(issue_url: str | None) -> int | None:
    if not issue_url:
        return None
    match = ISSUE_NUMBER_RE.search(issue_url)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _encode_metadata(metadata: SplitMaterializationMetadata) -> str:
    return _encode_json_payload(
        {
            "parent_issue": metadata.parent_issue,
            "subject": metadata.subject,
            "proposal_titles": list(metadata.proposal_titles),
            "children": [
                {
                    "title": child.title,
                    "key": child.key,
                    "url": child.url,
                    "number": child.number,
                    "origin": child.origin,
                }
                for child in metadata.children
            ],
        }
    )


def _decode_metadata(encoded: str) -> SplitMaterializationMetadata:
    payload = _decode_json_payload(encoded, marker_name="AGENT_DISCUSS_SPLIT")
    children_payload = payload.get("children")
    if not isinstance(children_payload, list):
        raise AgentLoopError("Invalid AGENT_DISCUSS_SPLIT payload.")
    children: list[SplitChild] = []
    for child in children_payload:
        if not isinstance(child, dict) or not isinstance(child.get("title"), str):
            raise AgentLoopError("Invalid AGENT_DISCUSS_SPLIT payload.")
        url = child.get("url")
        number = child.get("number")
        children.append(
            SplitChild(
                title=child["title"],
                key=str(child.get("key") or _proposal_key(child["title"])),
                url=url if isinstance(url, str) else None,
                number=number if isinstance(number, int) else None,
                origin=str(child.get("origin") or "created"),
            )
        )
    try:
        return SplitMaterializationMetadata(
            parent_issue=int(payload["parent_issue"]),
            subject=str(payload.get("subject") or ""),
            proposal_titles=tuple(str(value) for value in payload.get("proposal_titles", [])),
            children=tuple(children),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentLoopError("Invalid AGENT_DISCUSS_SPLIT payload.") from exc


def find_existing_split_materialization(
    comments: Sequence[object],
    *,
    parent_issue: int,
) -> SplitMaterializationMetadata | None:
    """Return the latest recorded split materialization for `parent_issue`, if any.

    Matching is by parent issue number only (not subject): a merged proposal's
    normalized key is stable across subject-hash drift between discuss/plan
    reruns, so keying off the parent alone is what actually prevents refiling
    identical proposals after the subject changes.
    """
    found: SplitMaterializationMetadata | None = None
    for comment in comments:
        body = getattr(comment, "body", None)
        if not isinstance(body, str):
            continue
        for match in SPLIT_MATERIALIZATION_MARKER_RE.finditer(body):
            metadata = _decode_metadata(match.group("payload"))
            if metadata.parent_issue == parent_issue:
                found = metadata
    return found


def _dependency_lines(children: Sequence[SplitChild]) -> list[str]:
    if not children:
        return ["- None yet."]
    lines: list[str] = []
    for child in children:
        ref = child.url or (f"#{child.number}" if child.number is not None else None)
        if ref:
            lines.append(f"- {child.title}: {ref}")
        else:
            lines.append(f"- {child.title}: (issue URL unavailable from GitHub CLI output)")
    return lines


def _format_child_issue_body(
    *,
    repo: str,
    parent_issue: int,
    proposal_title: str,
    key: str,
    rationale_lines: Sequence[str],
    siblings: Sequence[SplitChild],
) -> str:
    parent_url = f"https://github.com/{repo}/issues/{parent_issue}"
    lines = [
        f"Part of #{parent_issue}: {parent_url}",
        "",
        "## Proposed scope",
        proposal_title,
    ]
    if rationale_lines:
        lines.extend(["", "## Split rationale", *[f"- {line}" for line in rationale_lines]])
    lines.extend(
        [
            "",
            "## Sibling stages",
            *_dependency_lines(siblings),
            "",
            "## Execution instructions",
            f"Run `agent-loop issue <this issue number>` to implement this stage in its own PR. "
            f"Keep the PR scoped to this stage; reference #{parent_issue} but never use closing "
            f"keywords (Fixes/Closes/Resolves) against #{parent_issue} — it tracks other stages too.",
            "",
            f"<!-- AGENT_SPLIT_CHILD: parent={parent_issue} key={_child_key_hash(key)} -->",
        ]
    )
    return "\n".join(lines)


def _format_parent_summary(
    metadata: SplitMaterializationMetadata,
    *,
    skipped_by_cap: int,
) -> str:
    lines = [
        f"Split consensus for issue #{metadata.parent_issue} materialized into child issues.",
        "",
        "| Stage | Origin | Child issue |",
        "| --- | --- | --- |",
    ]
    for child in metadata.children:
        ref = child.url or (f"#{child.number}" if child.number is not None else "unavailable")
        lines.append(f"| {child.title} | {child.origin} | {ref} |")
    if skipped_by_cap:
        lines.append("")
        lines.append(
            f"{skipped_by_cap} additional proposal(s) were skipped by the "
            f"MAX_SPLIT_CHILDREN={MAX_SPLIT_CHILDREN} cap; consolidate proposals before rerunning."
        )
    lines.extend(
        [
            "",
            "Continue implementation with `agent-loop issue <child issue number>` for the stage "
            "you want to work on next; this parent issue is not automatically closed by any "
            "single child's PR.",
            "",
            f"<!-- AGENT_DISCUSS_SPLIT: {_encode_metadata(metadata)} -->",
            "-- coding-review-agent-loop",
        ]
    )
    return "\n".join(lines)


def materialize_split_proposals(
    runner: Runner,
    *,
    config: AgentLoopConfig,
    parent_issue: int,
    subject: str,
    proposals: Sequence[str],
    issue_comments: Sequence[object],
    rationale_lines: Sequence[str] = (),
) -> SplitMaterializationMetadata:
    """File remaining split-proposal stages as child GitHub issues, idempotently.

    `proposals` must already be exactly the stages this call should cover (the
    selected/current stage excluded); callers decide what "remaining" means
    for their flow. Proposals whose normalized key already appears in a prior
    materialization for this parent are treated as already filed and are
    neither recreated nor re-adopted.
    """
    existing = find_existing_split_materialization(issue_comments, parent_issue=parent_issue)
    existing_children = list(existing.children) if existing is not None else []
    seen_keys = {child.key for child in existing_children}

    deduped: list[tuple[str, str]] = []
    for proposal in proposals:
        key = _proposal_key(proposal)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append((proposal, key))

    all_proposal_titles = tuple(
        dict.fromkeys([*(existing.proposal_titles if existing is not None else ()), *proposals])
    )

    if not deduped:
        if existing is not None:
            return existing
        return SplitMaterializationMetadata(
            parent_issue=parent_issue,
            subject=subject,
            proposal_titles=all_proposal_titles,
            children=(),
        )

    capacity = max(MAX_SPLIT_CHILDREN - len(existing_children), 0)
    capped = deduped[:capacity]
    skipped_by_cap = len(deduped) - len(capped)

    adopted: list[SplitChild] = []
    remaining_to_create: list[tuple[str, str]] = []
    if capped:
        search_query = f'"[#{parent_issue} stage]" in:title'
        found_issues = search_issues(runner, config=config, search=search_query)
        found_by_key: dict[str, tuple[str | None, int | None]] = {}
        for found in found_issues:
            match = SPLIT_CHILD_MARKER_RE.search(found.body or "")
            if match is not None and int(match.group("parent")) == parent_issue:
                found_by_key.setdefault(match.group("key"), (found.url, found.number))
                continue
            # Body unavailable or marker missing: fall back to normalized-title
            # matching against the deterministic child title format.
            title_key = _proposal_key(found.title)
            found_by_key.setdefault(f"title:{title_key}", (found.url, found.number))
        for title, key in capped:
            hashed = _child_key_hash(key)
            match = found_by_key.get(hashed) or found_by_key.get(f"title:{key}")
            if match is not None:
                url, number = match
                adopted.append(SplitChild(title=title, key=key, url=url, number=number, origin="adopted"))
            else:
                remaining_to_create.append((title, key))
    else:
        remaining_to_create = []

    created: list[SplitChild] = []
    siblings_so_far = [*existing_children, *adopted]
    for title, key in remaining_to_create:
        body = _format_child_issue_body(
            repo=config.repo,
            parent_issue=parent_issue,
            proposal_title=title,
            key=key,
            rationale_lines=rationale_lines,
            siblings=siblings_so_far,
        )
        issue_url = create_issue(
            runner,
            config=config,
            title=_child_issue_title(parent_issue, title),
            body=body,
        )
        child = SplitChild(
            title=title,
            key=key,
            url=issue_url,
            number=_issue_number_from_url(issue_url),
            origin="created",
        )
        created.append(child)
        siblings_so_far = [*siblings_so_far, child]

    metadata = SplitMaterializationMetadata(
        parent_issue=parent_issue,
        subject=subject,
        proposal_titles=all_proposal_titles,
        children=tuple([*existing_children, *adopted, *created]),
    )
    if adopted or created:
        post_issue_comment(
            runner,
            config=config,
            issue_number=parent_issue,
            body=_format_parent_summary(metadata, skipped_by_cap=skipped_by_cap),
        )
    return metadata


SPLIT_STAGE_HANDOFF_MARKER_RE = re.compile(
    r"<!--\s*AGENT_SPLIT_STAGE_HANDOFF:\s*(?P<payload>[A-Za-z0-9+/=_-]+)\s*-->",
    re.I,
)


@dataclass(frozen=True)
class SplitStageHandoffMetadata:
    parent_issue: int
    plan_hash: str
    child_issue_number: int
    child_issue_url: str | None


def _encode_stage_handoff_metadata(metadata: SplitStageHandoffMetadata) -> str:
    return _encode_json_payload(
        {
            "parent_issue": metadata.parent_issue,
            "plan_hash": metadata.plan_hash,
            "child_issue_number": metadata.child_issue_number,
            "child_issue_url": metadata.child_issue_url,
        }
    )


def _decode_stage_handoff_metadata(encoded: str) -> SplitStageHandoffMetadata:
    payload = _decode_json_payload(encoded, marker_name="AGENT_SPLIT_STAGE_HANDOFF")
    try:
        child_issue_url = payload.get("child_issue_url")
        return SplitStageHandoffMetadata(
            parent_issue=int(payload["parent_issue"]),
            plan_hash=str(payload["plan_hash"]),
            child_issue_number=int(payload["child_issue_number"]),
            child_issue_url=child_issue_url if isinstance(child_issue_url, str) else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentLoopError("Invalid AGENT_SPLIT_STAGE_HANDOFF payload.") from exc


def find_existing_split_stage_handoff(
    comments: Sequence[object],
    *,
    parent_issue: int,
    plan_hash: str,
) -> SplitStageHandoffMetadata | None:
    found: SplitStageHandoffMetadata | None = None
    for comment in comments:
        body = getattr(comment, "body", None)
        if not isinstance(body, str):
            continue
        for match in SPLIT_STAGE_HANDOFF_MARKER_RE.finditer(body):
            metadata = _decode_stage_handoff_metadata(match.group("payload"))
            if metadata.parent_issue == parent_issue and metadata.plan_hash == plan_hash:
                found = metadata
    return found


def format_split_stage_handoff_comment(
    *,
    parent_issue: int,
    plan_hash: str,
    child_issue_number: int,
    child_issue_url: str | None,
) -> str:
    metadata = SplitStageHandoffMetadata(
        parent_issue=parent_issue,
        plan_hash=plan_hash,
        child_issue_number=child_issue_number,
        child_issue_url=child_issue_url,
    )
    child = child_issue_url or f"#{child_issue_number}"
    lines = [
        f"Approved plan for issue #{parent_issue} implements stage {child}.",
        "",
        f"Plan hash: {plan_hash}",
        "",
        "The implementation PR will close this stage's child issue and only reference "
        f"(not close) #{parent_issue}, since other stages remain tracked separately.",
        "",
        f"<!-- AGENT_SPLIT_STAGE_HANDOFF: {_encode_stage_handoff_metadata(metadata)} -->",
        "-- coding-review-agent-loop",
    ]
    return "\n".join(lines)


def post_split_stage_handoff_comment(
    runner: Runner,
    *,
    config: AgentLoopConfig,
    parent_issue: int,
    plan_hash: str,
    child_issue_number: int,
    child_issue_url: str | None,
) -> None:
    post_issue_comment(
        runner,
        config=config,
        issue_number=parent_issue,
        body=format_split_stage_handoff_comment(
            parent_issue=parent_issue,
            plan_hash=plan_hash,
            child_issue_number=child_issue_number,
            child_issue_url=child_issue_url,
        ),
    )


def resolve_selected_stage(
    *,
    existing: SplitMaterializationMetadata,
    split_stage: int | None,
) -> SplitChild:
    """Resolve which materialized child the current plan-first run implements (#476).

    Auto-resolves only when exactly one child was ever materialized for this
    parent; otherwise `--split-stage` must disambiguate. This is deliberately
    stricter than fuzzy title matching so an implementation run never guesses
    which stage a plan covers.
    """
    if split_stage is not None:
        for child in existing.children:
            if child.number == split_stage:
                return child
        raise AgentLoopError(
            f"--split-stage {split_stage} does not match any child issue materialized for "
            f"parent #{existing.parent_issue}."
        )
    if len(existing.children) == 1:
        return existing.children[0]
    raise AgentLoopError(
        f"Issue #{existing.parent_issue} has {len(existing.children)} materialized split "
        "stages. Run `agent-loop issue <child issue number>` for the stage you want, or "
        "rerun this issue with --split-stage <child issue number> to select one."
    )
