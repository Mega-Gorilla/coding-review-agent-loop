"""Tests for opt-in parallel plan/PR reviewer execution (#594)."""
import threading
from unittest.mock import patch

import pytest

import coding_review_agent_loop.orchestrator as orchestrator
from coding_review_agent_loop.cli import AgentLoopError, build_parser, run_issue_loop, run_pr_loop
from coding_review_agent_loop.errors import QuotaResetExceededError
from agent_loop_helpers import (
    FakeRunner,
    make_config,
    structured_plan_review,
    structured_plan_state,
    structured_pr_review,
)


def _initial_plan() -> str:
    return structured_plan_state(
        state="blocking", summary="Initial plan.", plan_steps=["Make the change."]
    )


# ---------------------------------------------------------------------------
# CLI / config plumbing
# ---------------------------------------------------------------------------

def test_review_parallel_flag_scoped_to_issue_pr_task_not_discuss():
    for command, extra_args in (
        ("issue", ["56"]),
        ("pr", ["77"]),
        ("task", ["do the thing"]),
    ):
        args = build_parser().parse_args(
            [command, *extra_args, "--repo", "OWNER/REPO", "--review-parallel"]
        )
        assert args.review_parallel is True
        plain = build_parser().parse_args([command, *extra_args, "--repo", "OWNER/REPO"])
        assert plain.review_parallel is False

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["discuss", "56", "--repo", "OWNER/REPO", "--review-parallel"]
        )


# ---------------------------------------------------------------------------
# Concurrency probes
# ---------------------------------------------------------------------------

class _ReviewConcurrencyProbeRunner(FakeRunner):
    """Each reviewer blocks until the other has started, so a timeout means
    same-round reviewers did not truly overlap."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.codex_started = threading.Event()
        self.gemini_started = threading.Event()
        self.overlap_confirmed = True

    def run_with_log(self, args, *, cwd, **kwargs):
        cmd = [str(arg) for arg in args]
        if cmd[:2] == ["codex", "exec"]:
            self.codex_started.set()
            if not self.gemini_started.wait(timeout=10):
                self.overlap_confirmed = False
        elif cmd[:1] == ["gemini"]:
            self.gemini_started.set()
            if not self.codex_started.wait(timeout=10):
                self.overlap_confirmed = False
        return super().run_with_log(args, cwd=cwd, **kwargs)


def test_plan_first_parallel_runs_same_round_reviewers_concurrently(tmp_path):
    runner = _ReviewConcurrencyProbeRunner(
        claude_outputs=[_initial_plan()],
        codex_outputs=[structured_plan_review(summary="Codex plan review complete.")],
        gemini_outputs=[
            structured_plan_review(summary="Gemini plan review complete.", reviewer="Google Gemini")
        ],
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"), review_parallel=True)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert runner.overlap_confirmed, "same-round plan reviewers did not run concurrently"
    assert "Codex plan review complete." in runner.comments[1]
    assert "-- OpenAI Codex" in runner.comments[1]
    assert "Gemini plan review complete." in runner.comments[2]
    assert "-- Google Gemini" in runner.comments[2]


def test_pr_loop_parallel_runs_same_round_reviewers_concurrently(tmp_path):
    runner = _ReviewConcurrencyProbeRunner(
        codex_outputs=[structured_pr_review(summary="Codex PR review complete.")],
        gemini_outputs=[
            structured_pr_review(summary="Gemini PR review complete.", reviewer="Google Gemini")
        ],
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"), review_parallel=True)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.overlap_confirmed, "same-round PR reviewers did not run concurrently"
    assert "Codex PR review complete." in runner.comments[0]
    assert "-- OpenAI Codex" in runner.comments[0]
    assert "Gemini PR review complete." in runner.comments[1]
    assert "-- Google Gemini" in runner.comments[1]


# ---------------------------------------------------------------------------
# Parity with sequential
# ---------------------------------------------------------------------------

def test_plan_first_parallel_matches_sequential_comments(tmp_path):
    claude_outputs = [_initial_plan()]
    codex_outputs = [structured_plan_review(summary="Codex approves the plan.")]
    gemini_outputs = [
        structured_plan_review(summary="Gemini approves the plan.", reviewer="Google Gemini")
    ]

    sequential_runner = FakeRunner(
        claude_outputs=list(claude_outputs),
        codex_outputs=list(codex_outputs),
        gemini_outputs=list(gemini_outputs),
    )
    sequential_config = make_config(
        tmp_path / "seq", reviewer=("codex", "gemini"), log_dir=tmp_path / "seq" / "logs"
    )
    parallel_runner = FakeRunner(
        claude_outputs=list(claude_outputs),
        codex_outputs=list(codex_outputs),
        gemini_outputs=list(gemini_outputs),
    )
    parallel_config = make_config(
        tmp_path / "par",
        reviewer=("codex", "gemini"),
        log_dir=tmp_path / "par" / "logs",
        review_parallel=True,
    )

    assert run_issue_loop(sequential_runner, issue_number=56, config=sequential_config, plan_first=True) == 0
    assert run_issue_loop(parallel_runner, issue_number=56, config=parallel_config, plan_first=True) == 0

    assert parallel_runner.comments == sequential_runner.comments


def test_pr_loop_parallel_matches_sequential_comments(tmp_path):
    codex_outputs = [structured_pr_review(summary="Codex approves the PR.")]
    gemini_outputs = [
        structured_pr_review(summary="Gemini approves the PR.", reviewer="Google Gemini")
    ]

    sequential_runner = FakeRunner(codex_outputs=list(codex_outputs), gemini_outputs=list(gemini_outputs))
    sequential_config = make_config(
        tmp_path / "seq", reviewer=("codex", "gemini"), log_dir=tmp_path / "seq" / "logs"
    )
    parallel_runner = FakeRunner(codex_outputs=list(codex_outputs), gemini_outputs=list(gemini_outputs))
    parallel_config = make_config(
        tmp_path / "par",
        reviewer=("codex", "gemini"),
        log_dir=tmp_path / "par" / "logs",
        review_parallel=True,
    )

    assert run_pr_loop(sequential_runner, pr_number=77, config=sequential_config) == 0
    assert run_pr_loop(parallel_runner, pr_number=77, config=parallel_config) == 0

    assert parallel_runner.comments == sequential_runner.comments


# ---------------------------------------------------------------------------
# Collect-then-apply-then-raise, with resume
# ---------------------------------------------------------------------------

def test_plan_first_parallel_collects_healthy_review_before_raising_then_resumes(tmp_path):
    runner = FakeRunner(
        claude_outputs=[_initial_plan()],
        codex_outputs=[
            structured_plan_review(
                state="blocking",
                summary="Review incomplete: could not confirm the prior finding.",
            )
        ],
        gemini_outputs=[
            structured_plan_review(summary="Gemini approves the plan.", reviewer="Google Gemini")
        ],
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"), review_parallel=True)

    with pytest.raises(AgentLoopError, match="Codex"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)

    # Gemini's healthy review is posted (comment[0] is the coder's plan) even
    # though Codex's failure aborts the round afterward.
    assert len(runner.comments) == 2
    assert "Gemini approves the plan." in runner.comments[1]

    # A rerun resumes Gemini's posted review instead of re-invoking it.
    commands_before_rerun = len(runner.commands)
    runner.codex_outputs.append(
        structured_plan_review(summary="Codex approves after rerun.")
    )
    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0
    new_commands = runner.commands[commands_before_rerun:]
    gemini_calls_after = [cmd for cmd, _cwd in new_commands if cmd[:1] == ["gemini"]]
    assert len(gemini_calls_after) == 0


def test_pr_loop_parallel_collects_healthy_review_before_raising_then_resumes(tmp_path):
    runner = FakeRunner(
        codex_outputs=[("codex exploded", 1)],
        gemini_outputs=[
            structured_pr_review(summary="Gemini approves the PR.", reviewer="Google Gemini")
        ],
    )
    config = make_config(
        tmp_path, reviewer=("codex", "gemini"), review_parallel=True, agent_max_retries=0
    )

    with pytest.raises(AgentLoopError, match="Codex"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert len(runner.comments) == 1
    assert "Gemini approves the PR." in runner.comments[0]

    commands_before_rerun = len(runner.commands)
    runner.codex_outputs.append(structured_pr_review(summary="Codex approves after rerun."))
    assert run_pr_loop(runner, pr_number=77, config=config) == 0
    new_commands = runner.commands[commands_before_rerun:]
    gemini_calls_after = [cmd for cmd, _cwd in new_commands if cmd[:1] == ["gemini"]]
    assert len(gemini_calls_after) == 0


# ---------------------------------------------------------------------------
# PR pre-launch sync failure isolation
# ---------------------------------------------------------------------------

def test_pr_loop_parallel_sync_failure_isolated_from_healthy_reviewer(tmp_path):
    real_sync = orchestrator.sync_reviewer_pr_before_review

    def fake_sync(config, runner, reviewer, pr_number, pr_metadata):
        if reviewer == "codex":
            raise AgentLoopError("Codex checkout is desynced from the PR head.")
        return real_sync(config, runner, reviewer, pr_number, pr_metadata)

    runner = FakeRunner(
        gemini_outputs=[
            structured_pr_review(summary="Gemini approves the PR.", reviewer="Google Gemini")
        ],
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"), review_parallel=True)

    with patch("coding_review_agent_loop.orchestrator.sync_reviewer_pr_before_review", fake_sync):
        with pytest.raises(AgentLoopError, match="desynced"):
            run_pr_loop(runner, pr_number=77, config=config)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)
    assert len(runner.comments) == 1
    assert "Gemini approves the PR." in runner.comments[0]


# ---------------------------------------------------------------------------
# Unavailable (non-deterministic) reviewer failure policy
# ---------------------------------------------------------------------------

def test_pr_loop_parallel_unavailable_reviewer_alongside_healthy_reviewer(tmp_path):
    import json

    unavailable = json.dumps(
        {
            "schema_version": 1,
            "kind": "agent_unavailable",
            "retryable": False,
            "category": "environment",
            "summary": "The review checkout cannot access the diff.",
            "suggested_action": "Repair the reviewer sandbox before retrying it.",
        }
    ) + "\n<!-- AGENT_UNAVAILABLE -->\n-- OpenAI Codex"
    runner = FakeRunner(
        codex_outputs=[unavailable],
        gemini_outputs=[
            structured_pr_review(summary="Gemini approves the PR.", reviewer="Google Gemini")
        ],
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"), review_parallel=True)

    with pytest.raises(AgentLoopError, match="missing required input from Codex"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert "**Review status: Incomplete**" in runner.comments[-1]
    assert "Codex" in runner.comments[-1]
    # Gemini's healthy approval is recorded even though the round ultimately
    # reports an incomplete review because of Codex.
    assert any("Gemini approves the PR." in comment for comment in runner.comments)


# ---------------------------------------------------------------------------
# Quota precedence
# ---------------------------------------------------------------------------

class _OtherFatalError(AgentLoopError):
    pass


def test_pr_loop_parallel_quota_error_takes_precedence_over_other_fatal_error(tmp_path):
    # Configured order puts the plain fatal failure (Gemini) BEFORE the quota
    # failure (Codex), so this only passes if the raise phase scans every
    # captured failure for a quota error instead of just raising the first
    # one encountered in configured order.
    config = make_config(
        tmp_path, reviewer=("gemini", "codex"), review_parallel=True, agent_max_retries=0
    )
    runner = FakeRunner()

    def fake_run_validated_agent(runner_arg, *, agent, **kwargs):
        if agent == "codex":
            raise QuotaResetExceededError("Codex quota exhausted; resets in 2h.")
        raise _OtherFatalError("Gemini failed deterministically.")

    with patch.object(orchestrator, "_run_validated_agent", side_effect=fake_run_validated_agent):
        with pytest.raises(QuotaResetExceededError):
            run_pr_loop(runner, pr_number=77, config=config)


# ---------------------------------------------------------------------------
# Mixed resumed/pending: zero-pending resume constructs no executor
# ---------------------------------------------------------------------------

def test_plan_first_parallel_zero_pending_resume_constructs_no_executor(tmp_path):
    from coding_review_agent_loop.orchestrator import (
        PostedRoundMetadata,
        _attach_round_metadata,
        _plan_subject,
    )

    current_plan = "Revised plan.\n- Add state reconstruction.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    coder_comment = _attach_round_metadata(
        current_plan,
        PostedRoundMetadata(
            flow="plan", role="coder", agent="Claude", round_number=2,
            subject=_plan_subject(current_plan), prior_items=(),
        ),
    )
    codex_comment = _attach_round_metadata(
        "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="plan", role="reviewer", agent="Codex", round_number=2,
            subject=_plan_subject(current_plan), state="approved",
        ),
    )
    gemini_comment = _attach_round_metadata(
        "Plan looks sound too.\n<!-- AGENT_PLAN_STATE: approved -->\n-- Google Gemini",
        PostedRoundMetadata(
            flow="plan", role="reviewer", agent="Gemini", round_number=2,
            subject=_plan_subject(current_plan), state="approved",
        ),
    )
    runner = FakeRunner(
        issue_comments=[
            {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:00:00Z", "body": coder_comment},
            {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:05:00Z", "body": codex_comment},
            {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:06:00Z", "body": gemini_comment},
        ],
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"), review_parallel=True)

    def _boom(*args, **kwargs):
        raise AssertionError("no reviewer turn was pending; a thread pool should not be constructed")

    with patch("coding_review_agent_loop.orchestrator.ThreadPoolExecutor", side_effect=_boom):
        assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    agent_commands = [cmd[0] for cmd, _cwd in runner.commands if cmd[:1] in (["claude"], ["codex"], ["gemini"])]
    assert agent_commands == []


# ---------------------------------------------------------------------------
# Shared reviewer workdir rejection
# ---------------------------------------------------------------------------

def test_plan_first_parallel_rejects_shared_reviewer_workdirs(tmp_path):
    shared = tmp_path / "shared"
    config = make_config(
        tmp_path,
        reviewer=("codex", "gemini"),
        codex_dir=shared,
        gemini_dir=shared,
        allow_shared_dir=True,
        review_parallel=True,
    )
    runner = FakeRunner(claude_outputs=[], codex_outputs=[], gemini_outputs=[])

    with pytest.raises(AgentLoopError, match="distinct workdir per reviewer"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)

    assert len(runner.comments) == 0


def test_pr_loop_parallel_rejects_shared_reviewer_workdirs(tmp_path):
    shared = tmp_path / "shared"
    config = make_config(
        tmp_path,
        reviewer=("codex", "gemini"),
        codex_dir=shared,
        gemini_dir=shared,
        allow_shared_dir=True,
        review_parallel=True,
    )
    runner = FakeRunner(codex_outputs=[], gemini_outputs=[])

    with pytest.raises(AgentLoopError, match="distinct workdir per reviewer"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert len(runner.comments) == 0


def test_pr_loop_parallel_rejects_shared_reviewer_workdirs_with_workdirs_ready_handoff(tmp_path):
    shared = tmp_path / "shared"
    config = make_config(
        tmp_path,
        reviewer=("codex", "gemini"),
        codex_dir=shared,
        gemini_dir=shared,
        allow_shared_dir=True,
        review_parallel=True,
        create_dirs=False,
    )
    shared.mkdir(parents=True)
    config.claude_dir.mkdir(parents=True)
    runner = FakeRunner(codex_outputs=[], gemini_outputs=[])

    with pytest.raises(AgentLoopError, match="distinct workdir per reviewer"):
        run_pr_loop(runner, pr_number=77, config=config, workdirs_ready=True)

    assert len(runner.comments) == 0


# ---------------------------------------------------------------------------
# Repair/retry isolation
# ---------------------------------------------------------------------------

def test_plan_first_parallel_repair_isolated_between_reviewers(tmp_path):
    malformed_codex = "Plan looks fine but this response is missing the state marker."
    repaired_codex = structured_plan_review(summary="Codex approves after repair.")
    gemini_ok = structured_plan_review(summary="Gemini approves the plan.", reviewer="Google Gemini")

    lock = threading.Lock()
    repair_calls = []

    def fake_attempt_repair(raw, gemini_cmd, *, expected_kind=None, **kwargs):
        with lock:
            repair_calls.append(raw)
        if raw == malformed_codex:
            return repaired_codex
        return None

    runner = FakeRunner(
        claude_outputs=[_initial_plan()],
        codex_outputs=[malformed_codex],
        gemini_outputs=[gemini_ok],
    )
    config = make_config(
        tmp_path, reviewer=("codex", "gemini"), review_parallel=True, agent_max_retries=0
    )

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", fake_attempt_repair):
        assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert len(repair_calls) == 1
    assert "Codex approves after repair." in runner.comments[1]
    assert "Gemini approves the plan." in runner.comments[2]
