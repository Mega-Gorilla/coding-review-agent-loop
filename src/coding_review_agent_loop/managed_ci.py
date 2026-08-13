"""External driver for repositories that implement managed exact-head CI."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Literal

from .ci_health import (
    CiInfrastructureStall,
    PullRequestCheck,
    PullRequestChecks,
    is_wholly_infrastructure_blocked,
)
from .config import AgentLoopConfig
from .errors import AgentLoopError
from .github import (
    PullRequestMergeability,
    PullRequestMetadata,
    get_pr_checks,
    get_pr_head_sha,
    get_pr_mergeability,
)
from .logging import log
from .runner import Runner
from .workdirs import active_workdir


MANAGED_LABEL = "agent-loop-managed"
FINAL_CONTEXT = "final-ci/exact-head"
READINESS_CONTEXT = "agent-loop/round-readiness"
WORKFLOW_FILE = "ci.yml"
_CONTRACT_MARKERS = (MANAGED_LABEL, FINAL_CONTEXT, "expected_head_sha")


@dataclass(frozen=True)
class ManagedCiContract:
    workflow_file: str = WORKFLOW_FILE


@dataclass(frozen=True)
class ManagedCiOutcome:
    status: Literal[
        "passed",
        "failed",
        "timeout",
        "head_changed",
        "merge_conflict",
        "infrastructure_stall",
    ]
    checks: PullRequestChecks | None = None
    mergeability: PullRequestMergeability | None = None
    head_sha: str | None = None
    stall: CiInfrastructureStall | None = None


def activate_managed_ci(
    runner: Runner,
    *,
    config: AgentLoopConfig,
    pr_number: int,
    metadata: PullRequestMetadata,
) -> ManagedCiContract | None:
    """Activate a repository-advertised managed-CI contract for auto-merge runs.

    Repositories without any contract markers retain legacy behavior. A partial
    contract fails closed because applying its suppression label could otherwise
    disable hosted tests without a usable final qualification path.
    """
    if not config.auto_merge or config.dry_run:
        return None

    result = runner.run(
        [
            config.gh_cmd,
            "api",
            f"repos/{config.repo}/contents/.github/workflows/{WORKFLOW_FILE}",
            "-H",
            "Accept: application/vnd.github.raw+json",
        ],
        cwd=active_workdir(config),
        check=False,
    )
    if result.returncode != 0:
        return None
    workflow = result.stdout or ""
    present = tuple(marker for marker in _CONTRACT_MARKERS if marker in workflow)
    if not present:
        return None
    missing = tuple(marker for marker in _CONTRACT_MARKERS if marker not in workflow)
    if missing:
        raise AgentLoopError(
            "Repository CI advertises an incomplete managed-CI contract; missing marker(s): "
            + ", ".join(missing)
        )

    pr_result = runner.run(
        [config.gh_cmd, "api", f"repos/{config.repo}/pulls/{pr_number}"],
        cwd=active_workdir(config),
        check=False,
    )
    if pr_result.returncode != 0:
        raise AgentLoopError(f"Unable to validate managed-CI eligibility for PR #{pr_number}.")
    try:
        pr_data = json.loads(pr_result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AgentLoopError(
            f"Managed-CI eligibility response for PR #{pr_number} was invalid JSON."
        ) from exc
    head = pr_data.get("head") or {}
    head_repo = (head.get("repo") or {}).get("full_name")
    live_head_sha = head.get("sha")
    live_head_ref = head.get("ref")
    base_ref = (pr_data.get("base") or {}).get("ref")
    if not isinstance(head_repo, str) or head_repo.casefold() != config.repo.casefold():
        return None
    if not metadata.base_branch or base_ref != metadata.base_branch:
        return None
    if not metadata.head_sha:
        raise AgentLoopError(f"PR #{pr_number} has no head SHA; managed CI cannot be activated.")
    if not metadata.head_branch or live_head_ref != metadata.head_branch:
        raise AgentLoopError(
            f"PR #{pr_number} has no stable same-repository head branch for managed CI."
        )
    if live_head_sha != metadata.head_sha:
        raise AgentLoopError(
            f"PR #{pr_number} moved from {metadata.head_sha} to "
            f"{live_head_sha or 'an unknown head'} "
            "while managed CI was being activated; rerun against the live head."
        )

    labels = {
        label.get("name")
        for label in pr_data.get("labels") or []
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    }
    label_result = runner.run(
        [config.gh_cmd, "api", f"repos/{config.repo}/labels/{MANAGED_LABEL}"],
        cwd=active_workdir(config),
        check=False,
    )
    if label_result.returncode != 0:
        create_result = runner.run(
            [
                config.gh_cmd,
                "api",
                "--method",
                "POST",
                f"repos/{config.repo}/labels",
                "-f",
                f"name={MANAGED_LABEL}",
                "-f",
                "color=1f6feb",
                "-f",
                "description=Suppress intermediate CI; agent-loop dispatches exact-head final CI",
            ],
            cwd=active_workdir(config),
            check=False,
        )
        if create_result.returncode != 0:
            raise AgentLoopError(f"Unable to create the `{MANAGED_LABEL}` label.")
    if MANAGED_LABEL not in labels:
        prior_workflow_run_ids = _workflow_run_ids(
            runner,
            config=config,
            head_sha=metadata.head_sha,
        )
        apply_result = runner.run(
            [
                config.gh_cmd,
                "api",
                "--method",
                "POST",
                f"repos/{config.repo}/issues/{pr_number}/labels",
                "-f",
                f"labels[]={MANAGED_LABEL}",
            ],
            cwd=active_workdir(config),
            check=False,
        )
        if apply_result.returncode != 0:
            raise AgentLoopError(f"Unable to apply `{MANAGED_LABEL}` to PR #{pr_number}.")
        try:
            _wait_for_label_handoff(
                runner,
                config=config,
                pr_number=pr_number,
                metadata=metadata,
                prior_run_ids=prior_workflow_run_ids,
            )
        except AgentLoopError as exc:
            remove_result = runner.run(
                [
                    config.gh_cmd,
                    "api",
                    "--method",
                    "DELETE",
                    f"repos/{config.repo}/issues/{pr_number}/labels/{MANAGED_LABEL}",
                ],
                cwd=active_workdir(config),
                check=False,
            )
            if remove_result.returncode != 0:
                raise AgentLoopError(
                    f"{exc} Cleanup also failed: `{MANAGED_LABEL}` remains applied and "
                    "suppresses hosted CI until it is removed."
                ) from exc
            raise
    log(config, f"PR #{pr_number}: activated managed exact-head CI")
    return ManagedCiContract()


def intermediate_managed_checks(checks: PullRequestChecks) -> PullRequestChecks:
    """Hide contract-controlled absence while reviewers inspect an intermediate head.

    The managed route intentionally does not create the repository's test
    matrix contexts, so branch protection reports those contexts as missing
    until the exact-head workflow is dispatched. Actually observed non-final
    checks remain visible, especially failures from independent integrations.
    """
    pending = tuple(check for check in checks.pending if check.name != FINAL_CONTEXT)
    failing = tuple(check for check in checks.failing if check.name != FINAL_CONTEXT)
    passing = tuple(check for check in checks.passing if check.name != FINAL_CONTEXT)
    required = tuple(name for name in checks.required_checks if name != FINAL_CONTEXT)
    if failing:
        state = "failing"
    elif pending:
        state = "pending"
    elif passing:
        state = "passing"
    elif checks.check_query_status == "unavailable":
        state = "unavailable"
    else:
        state = "no_checks"
    return replace(
        checks,
        state=state,
        required_checks=required,
        passing=passing,
        pending=pending,
        failing=failing,
        missing_required=(),
        infrastructure_stalls=tuple(
            stall for stall in checks.infrastructure_stalls if stall.name != FINAL_CONTEXT
        ),
    )


def publish_round_readiness(
    runner: Runner,
    *,
    config: AgentLoopConfig,
    head_sha: str,
) -> None:
    result = runner.run(
        [
            config.gh_cmd,
            "api",
            "--method",
            "POST",
            f"repos/{config.repo}/statuses/{head_sha}",
            "-f",
            "state=success",
            "-f",
            f"context={READINESS_CONTEXT}",
            "-f",
            "description=Configured local pre-review verification passed",
        ],
        cwd=active_workdir(config),
        check=False,
    )
    if result.returncode != 0:
        raise AgentLoopError(f"Unable to publish `{READINESS_CONTEXT}` for {head_sha}.")


def dispatch_final_qualification(
    runner: Runner,
    *,
    config: AgentLoopConfig,
    pr_number: int,
    expected_head_sha: str,
    head_ref: str,
    contract: ManagedCiContract,
) -> None:
    live_head = get_pr_head_sha(runner, config, pr_number)
    if live_head != expected_head_sha:
        raise AgentLoopError(
            f"PR #{pr_number} head moved from approved SHA {expected_head_sha} "
            f"to {live_head} before CI dispatch."
        )
    result = runner.run(
        [
            config.gh_cmd,
            "api",
            "--method",
            "POST",
            f"repos/{config.repo}/actions/workflows/{contract.workflow_file}/dispatches",
            "-f",
            f"ref={head_ref}",
            "-f",
            f"inputs[pr_number]={pr_number}",
            "-f",
            f"inputs[expected_head_sha]={expected_head_sha}",
        ],
        cwd=active_workdir(config),
        check=False,
    )
    if result.returncode != 0:
        raise AgentLoopError(
            f"Unable to dispatch managed final CI for PR #{pr_number} at {expected_head_sha}."
        )
    log(config, f"PR #{pr_number}: dispatched managed final CI at {expected_head_sha}")


def wait_for_final_qualification(
    runner: Runner,
    *,
    config: AgentLoopConfig,
    pr_number: int,
    metadata: PullRequestMetadata,
) -> ManagedCiOutcome:
    expected_head = metadata.head_sha
    if not expected_head:
        raise AgentLoopError(f"PR #{pr_number} has no approved head SHA.")
    attempts = max(1, config.ci_timeout_seconds // config.ci_poll_interval_seconds)
    latest: PullRequestChecks | None = None
    for attempt in range(attempts):
        live_head = get_pr_head_sha(runner, config, pr_number)
        if live_head != expected_head:
            return ManagedCiOutcome(status="head_changed", checks=latest, head_sha=live_head)
        mergeability = get_pr_mergeability(runner, config=config, pr_number=pr_number)
        if mergeability.state == "conflicted":
            return ManagedCiOutcome(
                status="merge_conflict",
                checks=latest,
                mergeability=mergeability,
                head_sha=live_head,
            )
        latest = get_pr_checks(runner, config=config, metadata=metadata)
        final = _find_context(latest, FINAL_CONTEXT)
        status = final.status.lower() if final is not None else "pending"
        log(config, f"Managed CI context '{FINAL_CONTEXT}' status: {status}")
        if status == "success":
            return ManagedCiOutcome(status="passed", checks=latest, head_sha=live_head)
        if status in {
            "failure",
            "error",
            "cancelled",
            "timed_out",
            "action_required",
            "startup_failure",
            "stale",
        }:
            return ManagedCiOutcome(status="failed", checks=latest, head_sha=live_head)
        stall_checks = intermediate_managed_checks(latest)
        if is_wholly_infrastructure_blocked(stall_checks):
            stall = CiInfrastructureStall(checks=stall_checks.infrastructure_stalls)
            return ManagedCiOutcome(
                status="infrastructure_stall",
                checks=latest,
                head_sha=live_head,
                stall=stall,
            )
        if attempt < attempts - 1:
            runner.run(["sleep", str(config.ci_poll_interval_seconds)], cwd=active_workdir(config))
    return ManagedCiOutcome(status="timeout", checks=latest, head_sha=expected_head)


def _find_context(checks: PullRequestChecks, name: str) -> PullRequestCheck | None:
    for check in (*checks.failing, *checks.pending, *checks.passing):
        if check.name == name:
            return check
    return None


def _workflow_run_ids(
    runner: Runner,
    *,
    config: AgentLoopConfig,
    head_sha: str,
) -> set[int]:
    payload = _workflow_runs_payload(runner, config=config, head_sha=head_sha)
    return {
        run["id"]
        for run in payload
        if isinstance(run, dict) and isinstance(run.get("id"), int)
    }


def _workflow_runs_payload(
    runner: Runner,
    *,
    config: AgentLoopConfig,
    head_sha: str,
) -> list[dict[str, object]]:
    result = runner.run(
        [
            config.gh_cmd,
            "api",
            f"repos/{config.repo}/actions/workflows/{WORKFLOW_FILE}/runs"
            f"?event=pull_request&head_sha={head_sha}&per_page=20",
        ],
        cwd=active_workdir(config),
        check=False,
    )
    if result.returncode != 0:
        raise AgentLoopError("Unable to inspect managed-CI workflow runs.")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AgentLoopError("Managed-CI workflow-run response was invalid JSON.") from exc
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise AgentLoopError("Managed-CI workflow-run response omitted `workflow_runs`.")
    return runs


def _wait_for_label_handoff(
    runner: Runner,
    *,
    config: AgentLoopConfig,
    pr_number: int,
    metadata: PullRequestMetadata,
    prior_run_ids: set[int],
) -> None:
    assert metadata.head_sha is not None
    attempts = max(1, config.ci_timeout_seconds // config.ci_poll_interval_seconds)
    for attempt in range(attempts):
        live_head = get_pr_head_sha(runner, config, pr_number)
        if live_head != metadata.head_sha:
            raise AgentLoopError(
                f"PR #{pr_number} head moved while waiting for the managed-label CI handoff."
            )
        runs = _workflow_runs_payload(runner, config=config, head_sha=metadata.head_sha)
        new_runs = [
            run
            for run in runs
            if isinstance(run.get("id"), int) and run["id"] not in prior_run_ids
        ]
        if any(run.get("status") == "completed" for run in new_runs):
            checks = get_pr_checks(runner, config=config, metadata=metadata)
            final = _find_context(checks, FINAL_CONTEXT)
            if final is None or final.status.lower() not in {"pending", "queued", "in_progress"}:
                raise AgentLoopError(
                    f"Managed-label handoff for PR #{pr_number} completed without publishing "
                    f"`{FINAL_CONTEXT}=pending`."
                )
            return
        if attempt < attempts - 1:
            runner.run(["sleep", str(config.ci_poll_interval_seconds)], cwd=active_workdir(config))
    raise AgentLoopError(
        f"Managed-label handoff for PR #{pr_number} did not complete within "
        f"{config.ci_timeout_seconds}s."
    )
