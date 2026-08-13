import json

import pytest

from coding_review_agent_loop.errors import AgentLoopError
from coding_review_agent_loop.github import (
    PullRequestCheck,
    PullRequestChecks,
    PullRequestMetadata,
    merge_pr,
)
from coding_review_agent_loop.managed_ci import (
    FINAL_CONTEXT,
    MANAGED_LABEL,
    ManagedCiContract,
    activate_managed_ci,
    dispatch_final_qualification,
    intermediate_managed_checks,
    wait_for_final_qualification,
)
from coding_review_agent_loop.runner import CommandResult

from agent_loop_helpers import FakeRunner, make_config


WORKFLOW = """
name: CI
on:
  workflow_dispatch:
    inputs:
      expected_head_sha: {required: true}
jobs:
  route:
    if: contains(github.event.pull_request.labels.*.name, 'agent-loop-managed')
  aggregate:
    name: final-ci/exact-head
"""


class ManagedRunner(FakeRunner):
    def __init__(self, *, workflow=WORKFLOW, **kwargs):
        super().__init__(**kwargs)
        self.workflow = workflow
        self.label_applied = False

    def _run_locked(self, args, *, cwd, check):
        cmd = list(args)
        if cmd[:3] == ["gh", "api", "repos/OWNER/REPO/contents/.github/workflows/ci.yml"]:
            cmd, cwd_path = self._record_command(args, cwd)
            return CommandResult(cmd, cwd_path, self.workflow, "", 0)
        if cmd[:3] == ["gh", "api", "repos/OWNER/REPO/pulls/7"]:
            cmd, cwd_path = self._record_command(args, cwd)
            payload = {
                "head": {
                    "repo": {"full_name": "OWNER/REPO"},
                    "sha": "abc123",
                    "ref": "feature",
                },
                "base": {"ref": "main"},
                "labels": [],
            }
            return CommandResult(cmd, cwd_path, json.dumps(payload), "", 0)
        if cmd[:3] == ["gh", "api", f"repos/OWNER/REPO/labels/{MANAGED_LABEL}"]:
            cmd, cwd_path = self._record_command(args, cwd)
            return CommandResult(cmd, cwd_path, "{}", "", 0)
        if "repos/OWNER/REPO/actions/workflows/ci.yml/runs?" in " ".join(cmd):
            cmd, cwd_path = self._record_command(args, cwd)
            runs = (
                [{"id": 2, "status": "completed", "conclusion": "success"}]
                if self.label_applied
                else [{"id": 1, "status": "completed", "conclusion": "success"}]
            )
            return CommandResult(cmd, cwd_path, json.dumps({"workflow_runs": runs}), "", 0)
        if "repos/OWNER/REPO/issues/7/labels" in cmd:
            self.label_applied = True
        return super()._run_locked(args, cwd=cwd, check=check)


def metadata():
    return PullRequestMetadata(
        number=7,
        repo="OWNER/REPO",
        title="Managed CI",
        head_branch="feature",
        base_branch="main",
        head_sha="abc123",
        url="https://github.com/OWNER/REPO/pull/7",
    )


def checks(*, pending=(), passing=(), failing=(), required=(FINAL_CONTEXT,), missing=()):
    return PullRequestChecks(
        state="failing" if failing else "pending" if pending else "passing",
        required_checks=required,
        passing=passing,
        pending=pending,
        failing=failing,
        missing_required=missing,
        branch_protection_status="configured",
    )


def test_activate_managed_ci_only_for_complete_supported_contract(tmp_path):
    config = make_config(tmp_path, auto_merge=True)
    runner = ManagedRunner(
        pr_payload={"headRefOid": "abc123"},
        pr_status_payload={
            "statuses": [{"context": FINAL_CONTEXT, "state": "pending"}]
        },
    )

    contract = activate_managed_ci(
        runner, config=config, pr_number=7, metadata=metadata()
    )

    assert contract == ManagedCiContract()
    assert any(
        cmd[:5] == ["gh", "api", "--method", "POST", "repos/OWNER/REPO/issues/7/labels"]
        for cmd, _cwd in runner.commands
    )


def test_activate_managed_ci_preserves_legacy_behavior_without_markers(tmp_path):
    config = make_config(tmp_path, auto_merge=True)
    runner = ManagedRunner(workflow="name: CI\n")

    assert activate_managed_ci(
        runner, config=config, pr_number=7, metadata=metadata()
    ) is None


def test_activate_managed_ci_fails_closed_for_partial_contract(tmp_path):
    config = make_config(tmp_path, auto_merge=True)
    runner = ManagedRunner(workflow=f"name: CI\n# {MANAGED_LABEL}\n")

    with pytest.raises(AgentLoopError, match="incomplete managed-CI contract"):
        activate_managed_ci(runner, config=config, pr_number=7, metadata=metadata())


def test_intermediate_checks_remove_only_expected_final_pending_context():
    final = PullRequestCheck(FINAL_CONTEXT, "status_context", "pending")
    lint = PullRequestCheck("lint", "check_run", "failure")

    filtered = intermediate_managed_checks(
        checks(
            pending=(final,),
            failing=(lint,),
            required=(FINAL_CONTEXT, "test (pr-inline)"),
            missing=("test (pr-inline)",),
        )
    )

    assert filtered.state == "failing"
    assert filtered.required_checks == ("test (pr-inline)",)
    assert filtered.pending == ()
    assert filtered.failing == (lint,)
    assert filtered.missing_required == ()


def test_dispatch_and_wait_bind_final_qualification_to_exact_head(tmp_path):
    config = make_config(tmp_path, auto_merge=True, ci_poll_interval_seconds=1)
    final = {"context": FINAL_CONTEXT, "state": "success", "target_url": None}
    runner = ManagedRunner(
        pr_payload={"headRefOid": "abc123", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"},
        pr_status_payload={"statuses": [final]},
        pr_branch_protection_payload={"contexts": [FINAL_CONTEXT], "checks": []},
    )

    dispatch_final_qualification(
        runner,
        config=config,
        pr_number=7,
        expected_head_sha="abc123",
        head_ref="feature",
        contract=ManagedCiContract(),
    )
    outcome = wait_for_final_qualification(
        runner, config=config, pr_number=7, metadata=metadata()
    )

    assert outcome.status == "passed"
    dispatch = next(
        cmd for cmd, _cwd in runner.commands
        if "repos/OWNER/REPO/actions/workflows/ci.yml/dispatches" in cmd
    )
    assert "ref=feature" in dispatch
    assert "inputs[expected_head_sha]=abc123" in dispatch


def test_merge_pr_uses_expected_head_guard(tmp_path):
    config = make_config(tmp_path, auto_merge=True)
    runner = FakeRunner()

    merge_pr(runner, config, 7, expected_head_sha="abc123")

    command = runner.commands[-1][0]
    assert command[-2:] == ["--match-head-commit", "abc123"]
