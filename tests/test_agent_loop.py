import base64
import json
import re
import subprocess
from pathlib import Path

import pytest

from coding_review_agent_loop.agents.claude import (
    BACKEND as CLAUDE_BACKEND,
    _normalize_claude_usage,
    _parse_claude_output,
)
from coding_review_agent_loop.agents.codex import (
    BACKEND as CODEX_BACKEND,
    _extract_codex_usage,
    _normalize_codex_usage,
)
from coding_review_agent_loop.agents.gemini import (
    BACKEND as GEMINI_BACKEND,
    PUBLIC_RESPONSE_MARKER,
    _normalize_gemini_usage,
    _parse_gemini_payload,
)
from coding_review_agent_loop.cli import (
    AgentLoopConfig,
    AgentLoopError,
    CommandResult,
    Runner,
    build_parser,
    config_from_args,
    ensure_log_dir_ignored,
    is_clarification_request,
    parse_agent_state,
    parse_pr_number,
    run_issue_loop,
    run_pr_loop,
    run_task_loop,
)
from coding_review_agent_loop.errors import QuotaResetExceededError
from coding_review_agent_loop.orchestrator import (
    _format_reset_duration,
    _failure_category,
    _is_transient_agent_output,
    _is_transient_public_response,
    _parse_rate_limit_reset_seconds,
    _run_validated_agent,
)
from coding_review_agent_loop.config import (
    default_agent_memory_dir,
    default_agent_workdir,
    default_cache_root,
)
from coding_review_agent_loop.decomposition import (
    CreatedPhaseIssue,
    MAX_DECOMPOSITION_PHASES,
    RecordedPhase,
    approved_plan_hash,
    find_existing_phase_implementation_handoff,
    format_decomposition_parent_summary,
    format_phase_implementation_handoff_comment,
    parse_plan_decomposition,
)
from coding_review_agent_loop.github import (
    HumanReviewRequirement,
    IssueComment,
    IssueContext,
    PullRequestReviewContext,
    PullRequestMetadata,
    get_issue_context,
    get_pr_checks,
)
from coding_review_agent_loop.followups import (
    MAX_APPROVED_FOLLOWUP_ISSUES,
    reconcile_approved_followups,
)
from coding_review_agent_loop.migrations import MigrationValidationResult, validate_pr_migration_topology
from coding_review_agent_loop.orchestrator import (
    ITEM_SUMMARY_LIMIT,
    HUMAN_REQUIREMENTS_ACK_ITEM_ID,
    PostedRoundMetadata,
    _apply_unresolved_item_dispositions,
    _attach_round_metadata,
    _decode_round_metadata,
    _encode_round_metadata,
    _format_unresolved_item_label,
    _plan_subject,
    _render_public_coder_followup_comment,
    _render_public_plan_review_comment,
    _render_public_plan_revision_comment,
    _render_public_pr_review_comment,
    _render_public_review_comment,
    _reconcile_human_requirements_ack_item,
    _review_freeform_summary_text,
    _resume_pr_round,
    _resume_plan_round,
    _strip_round_metadata,
    _validate_coder_followup_response,
    _validate_plan_revision_response,
    _validate_review_response,
    _validate_plan_review_response,
    render_canonical_plan_revision,
    render_canonical_plan_steps,
)
from coding_review_agent_loop.prompts import (
    HUMAN_REQUIREMENTS_ADDRESSED_MARKER,
    HUMAN_REQUIREMENTS_DIRECT_DISCUSSION_ACK,
    build_followup_prompt,
    build_issue_implementation_prompt,
    build_issue_plan_prompt,
    build_issue_prompt,
    build_same_pr_followup_prompt,
    build_plan_review_prompt,
    build_plan_revision_prompt,
    build_review_prompt,
    format_human_requirements,
    format_issue_context,
    render_coder_human_requirements_prompt_context,
)
from coding_review_agent_loop.protocol import (
    ApprovedFollowup,
    _expect_string_list,
    _extract_structured_coder_followup_payload,
    _extract_structured_plan_review_payload,
    _extract_structured_plan_revision_payload,
    _extract_structured_pr_review_payload,
    normalize_response_file_structured_text,
    parse_approved_followups,
    parse_human_requirements_acknowledgement,
    parse_pr_review,
    parse_plan_item_dispositions,
    parse_plan_review,
    parse_plan_review_items,
    parse_plan_state,
    parse_structured_plan_review,
    parse_structured_pr_review,
    parse_review,
    parse_non_blocking_followups,
    parse_signed_human_requirement_body,
    parse_unresolved_item_dispositions,
    ReviewItemDisposition,
    UnresolvedReviewItem,
    validate_human_requirements_acknowledgement,
    validate_structured_coder_followup,
    validate_structured_human_requirements_acknowledgement,
    validate_structured_plan_revision,
)

from unittest.mock import MagicMock, patch


@pytest.fixture(autouse=True)
def _no_real_repair(request):
    """Prevent attempt_repair from calling the real Gemini CLI in all tests.

    Tests that explicitly test repair behaviour patch the orchestrator-level
    import themselves, which takes precedence over this fixture.  Unit tests
    for attempt_repair itself patch subprocess.run directly, so they are
    unaffected here.
    """
    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _no_real_repair(request):
    """Prevent attempt_repair from calling the real Gemini CLI in all tests.

    Tests that explicitly test repair behaviour patch the orchestrator-level
    import themselves, which takes precedence over this fixture.  Unit tests
    for attempt_repair itself patch subprocess.run directly, so they are
    unaffected here.
    """
    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value=None):
        yield


class FakeRunner(Runner):
    def __init__(
        self,
        *,
        claude_outputs=None,
        codex_outputs=None,
        gemini_outputs=None,
        issue_payload=None,
        issue_comments=None,
        pr_payload=None,
        pr_check_runs_payload=None,
        pr_status_payload=None,
        pr_branch_protection_payload=None,
        pr_branch_protection_returncode=0,
        pr_branch_protection_stderr="",
        pr_check_runs_returncode=0,
        pr_check_runs_stderr="",
        pr_status_returncode=0,
        pr_status_stderr="",
        git_status="",
        git_remote="git@github.com:OWNER/REPO.git",
        git_inside=True,
        git_head="abc123",
        tracked_files=None,
        changed_files=None,
        diff_returncode=0,
        diff_stderr="",
        issue_urls=None,
        public_response_outputs=None,
    ):
        super().__init__(dry_run=False)
        self.claude_outputs = list(claude_outputs or [])
        self.codex_outputs = list(codex_outputs or [])
        self.gemini_outputs = list(gemini_outputs or [])
        self.issue_payload = {
            "number": 56,
            "state": "open",
            "is_pr": False,
            "url": "https://github.com/OWNER/REPO/issues/56",
            "title": "Fix issue-mode context",
            "body": "Original issue body.",
        }
        if issue_payload:
            self.issue_payload.update(issue_payload)
        self.issue_comments = list(issue_comments or [])
        self.pr_payload = {
            "number": 77,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/77",
            "title": "Improve review prompt context",
            "body": "Fixes #56",
            "headRefName": "feature/review-context",
            "baseRefName": "main",
            "headRefOid": "abc123",
            "comments": [],
            "reviews": [],
        }
        if pr_payload:
            self.pr_payload.update(pr_payload)
        self.pr_check_runs_payload = pr_check_runs_payload or {
            "check_runs": [{"name": "test", "status": "completed", "conclusion": "success"}]
        }
        self.pr_status_payload = pr_status_payload or {"state": "success", "statuses": []}
        self.pr_branch_protection_payload = pr_branch_protection_payload or {"contexts": ["test"]}
        self.pr_branch_protection_returncode = pr_branch_protection_returncode
        self.pr_branch_protection_stderr = pr_branch_protection_stderr
        self.pr_check_runs_returncode = pr_check_runs_returncode
        self.pr_check_runs_stderr = pr_check_runs_stderr
        self.pr_status_returncode = pr_status_returncode
        self.pr_status_stderr = pr_status_stderr
        self.commands = []
        self.comments = []
        self.issues = []
        self.git_status = git_status
        self.git_remote = git_remote
        self.git_inside = git_inside
        self.git_head = git_head
        self.tracked_files = tracked_files or [
            "pyproject.toml",
            "README.md",
            "src/coding_review_agent_loop/cli.py",
            "tests/test_agent_loop.py",
        ]
        self.changed_files = changed_files or ["src/coding_review_agent_loop/cli.py"]
        self.diff_returncode = diff_returncode
        self.diff_stderr = diff_stderr
        self.issue_urls = list(issue_urls) if issue_urls is not None else None
        self.public_response_outputs = list(public_response_outputs or [])

    def _normalize_legacy_agent_output(self, output: str, prompt: str) -> str:
        stripped = output.lstrip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                for key in ("response", "result"):
                    value = payload.get(key)
                    if isinstance(value, str):
                        normalized_value = self._normalize_legacy_agent_output(value, prompt)
                        if normalized_value != value:
                            payload[key] = normalized_value
                            return json.dumps(payload)
            return output
        signature_matches = re.findall(r"-- (OpenAI Codex|Google Gemini|Anthropic Claude)", prompt)
        signature = signature_matches[-1] if signature_matches else "OpenAI Codex"
        if (
            '"kind": "pr_review"' in prompt
            and '"kind": "coder_followup"' not in prompt
            and "<!-- AGENT_STATE:" in output
        ):
            parsed = parse_review(output, reviewer="OpenAI Codex")
            return structured_pr_review(
                state=parsed.state,
                summary=parsed.summary or "Review complete.",
                blocking_items=[item.text for item in parsed.blocking_items],
                same_pr_followups=[item.text for item in parsed.followups.same_pr],
                future_followups=[item.text for item in parsed.followups.future],
                prior_item_dispositions=[
                    {
                        key: value
                        for key, value in {
                            "item_id": item.item_id,
                            "disposition": item.disposition,
                            "note": item.note,
                        }.items()
                        if value is not None
                    }
                    for item in parsed.dispositions
                ],
                reviewer=signature,
                human_requirements_resolved="<!-- HUMAN_REQUIREMENTS_RESOLVED -->" in output,
            )
        if (
            '"kind": "plan_review"' in prompt
            and '"kind": "plan_revision"' not in prompt
            and "<!-- AGENT_PLAN_STATE:" in output
        ):
            state = parse_plan_state(output)
            items = parse_plan_review_items(output, reviewer="OpenAI Codex")
            future = [item.text for item in items.future] if state == "approved" else []
            blocking = [item.text for item in items.blocking]
            return structured_plan_review(
                state=state,
                summary=_review_freeform_summary_text(output) or "Plan review complete.",
                blocking_plan_issues=blocking,
                same_plan_followups=[item.text for item in items.same_plan],
                future_followups=future,
                prior_plan_item_dispositions=[
                    {
                        key: value
                        for key, value in {
                            "item_id": item.item_id,
                            "disposition": item.disposition,
                            "note": item.note,
                        }.items()
                        if value is not None
                    }
                    for item in parse_plan_item_dispositions(output, reviewer="OpenAI Codex")
                ],
                reviewer=signature,
            )
        if '"kind": "plan_revision"' in prompt and "<!-- AGENT_PLAN_STATE:" in output:
            return structured_plan_revision(
                summary=_review_freeform_summary_text(output) or "Revised the plan.",
                plan_steps=[
                    line.strip("- ").strip()
                    for line in output.splitlines()
                    if line.strip().startswith("- ")
                    and "Requirement " not in line
                    and not line.strip().startswith("--")
                ]
                or [_review_freeform_summary_text(output) or "Revised the plan."],
                human_requirements=(
                    "\n" + HUMAN_REQUIREMENTS_ADDRESSED_MARKER
                    if HUMAN_REQUIREMENTS_ADDRESSED_MARKER in output
                    else ""
                ),
            )
        if '"kind": "coder_followup"' in prompt and "<!-- AGENT_STATE:" in output:
            item_ids = sorted(set(re.findall(r"\[(item-[A-Za-z0-9._-]+)\]", prompt)))
            item_ids = [item_id for item_id in item_ids if item_id != HUMAN_REQUIREMENTS_ACK_ITEM_ID]
            human_ids = sorted(set(re.findall(r"`(Requirement \d+)`|(?:^|\s)(Requirement \d+):", output)))
            flattened_human_ids = [first or second for first, second in human_ids]
            return structured_coder_followup(
                state=parse_agent_state(output),
                summary=_review_freeform_summary_text(output) or "Updated the PR.",
                addressed_items=item_ids,
                remaining_items=[],
                human_requirement_ids=flattened_human_ids,
            )
        return output

    def _next_agent_output(self, outputs):
        output = outputs.pop(0)
        if isinstance(output, dict):
            return output
        if isinstance(output, tuple):
            return output
        return output, 0

    def _record_command(self, args, cwd):
        cmd = [str(arg) for arg in args]
        cwd_path = Path(cwd)
        if not cwd_path.is_dir():
            raise FileNotFoundError(cwd_path)
        self.commands.append((cmd, cwd_path))
        return cmd, cwd_path

    def _maybe_write_public_response_file(self, cmd):
        if not self.public_response_outputs:
            return
        prompt = "\n".join(cmd)
        match = re.search(r"Write the final public response.*?\n\n([^\n]+/responses/[^\n]+\.md)", prompt, re.S)
        if not match:
            return
        response_path = Path(match.group(1))
        response_path.parent.mkdir(parents=True, exist_ok=True)
        response = self.public_response_outputs.pop(0)
        if isinstance(response, dict):
            response = response.get("text", "")
        elif isinstance(response, str):
            response = self._normalize_legacy_agent_output(response, prompt)
        response_path.write_text(response, encoding="utf-8")

    def run_with_log(
        self,
        args,
        *,
        cwd,
        log_path,
        label,
        progress_interval_seconds,
        check=True,
    ):
        cmd, cwd_path = self._record_command(args, cwd)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_log_dir_ignored(log_path.parent)

        if cmd[:1] == ["claude"]:
            output, returncode = self._next_agent_output(self.claude_outputs)
            if isinstance(output, str):
                output = self._normalize_legacy_agent_output(output, "\n".join(cmd))
            self._maybe_write_public_response_file(cmd)
            log_path.write_text(f"$ {' '.join(cmd)}\n\n{output}", encoding="utf-8")
            return CommandResult(cmd, cwd_path, output, "", returncode)

        if cmd[:2] == ["codex", "exec"]:
            output = self._next_agent_output(self.codex_outputs)
            if isinstance(output, dict):
                public_response = output.get("public_response", "")
                stdout = output.get("stdout", "")
                returncode = output.get("returncode", 0)
            else:
                public_response, returncode = output
                stdout = public_response
            if isinstance(public_response, str):
                normalized = self._normalize_legacy_agent_output(public_response, "\n".join(cmd))
                if normalized != public_response:
                    public_response = normalized
            self._maybe_write_public_response_file(cmd)
            if "--output-last-message" in cmd:
                out_path = Path(cmd[cmd.index("--output-last-message") + 1])
                out_path.write_text(public_response, encoding="utf-8")
            log_path.write_text(f"$ {' '.join(cmd)}\n\ncodex completed", encoding="utf-8")
            return CommandResult(cmd, cwd_path, stdout, "", returncode)

        if cmd[:1] == ["gemini"]:
            output = self._next_agent_output(self.gemini_outputs)
            explicit_stdout = False
            if isinstance(output, dict):
                stdout = output.get("stdout", "")
                returncode = output.get("returncode", 0)
                explicit_stdout = True
            else:
                stdout, returncode = output
            output = stdout
            if isinstance(output, str) and not explicit_stdout:
                output = self._normalize_legacy_agent_output(output, "\n".join(cmd))
            self._maybe_write_public_response_file(cmd)
            log_path.write_text(f"$ {' '.join(cmd)}\n\n{output}", encoding="utf-8")
            return CommandResult(cmd, cwd_path, output, "", returncode)

        return self.run(args, cwd=cwd, check=check)

    def run(self, args, *, cwd, input_text=None, check=True):
        cmd, cwd_path = self._record_command(args, cwd)

        if cmd[:1] == ["claude"]:
            output, returncode = self._next_agent_output(self.claude_outputs)
            if isinstance(output, str):
                output = self._normalize_legacy_agent_output(output, "\n".join(cmd))
            return CommandResult(cmd, cwd_path, output, "", returncode)

        if cmd[:2] == ["codex", "exec"]:
            output = self._next_agent_output(self.codex_outputs)
            if isinstance(output, dict):
                public_response = output.get("public_response", "")
                stdout = output.get("stdout", "")
                returncode = output.get("returncode", 0)
            else:
                public_response, returncode = output
                stdout = public_response
            if isinstance(public_response, str):
                normalized = self._normalize_legacy_agent_output(public_response, "\n".join(cmd))
                if normalized != public_response:
                    public_response = normalized
            if "--output-last-message" in cmd:
                out_path = Path(cmd[cmd.index("--output-last-message") + 1])
                out_path.write_text(public_response, encoding="utf-8")
            return CommandResult(cmd, cwd_path, stdout, "", returncode)

        if cmd[:3] == ["gh", "pr", "comment"]:
            if "--body-file" in cmd:
                body_path = Path(cmd[cmd.index("--body-file") + 1])
                raw_body = body_path.read_text(encoding="utf-8")
            elif "--body" in cmd:
                raw_body = cmd[cmd.index("--body") + 1]
            else:
                raw_body = ""
            self.comments.append(_strip_round_metadata(raw_body))
            self.pr_payload.setdefault("comments", []).append(
                {
                    "author": {"login": "coding-review-agent-loop"},
                    "createdAt": f"2026-05-23T00:00:{len(self.pr_payload.get('comments', [])):02d}Z",
                    "body": raw_body,
                }
            )
            return CommandResult(cmd, cwd_path, "", "", 0)

        if cmd[:3] == ["gh", "issue", "comment"]:
            if "--body-file" in cmd:
                body_path = Path(cmd[cmd.index("--body-file") + 1])
                raw_body = body_path.read_text(encoding="utf-8")
            elif "--body" in cmd:
                raw_body = cmd[cmd.index("--body") + 1]
            else:
                raw_body = ""
            self.comments.append(_strip_round_metadata(raw_body))
            self.issue_comments.append(
                {
                    "author": {"login": "coding-review-agent-loop"},
                    "createdAt": f"2026-05-23T00:00:{len(self.issue_comments):02d}Z",
                    "body": raw_body,
                }
            )
            return CommandResult(cmd, cwd_path, "", "", 0)

        if cmd[:3] == ["gh", "issue", "create"]:
            title = cmd[cmd.index("--title") + 1]
            if "--body-file" in cmd:
                body_path = Path(cmd[cmd.index("--body-file") + 1])
                body = body_path.read_text(encoding="utf-8")
            else:
                body = cmd[cmd.index("--body") + 1]
            self.issues.append({"title": title, "body": body})
            if self.issue_urls is None:
                issue_url = "https://github.com/OWNER/REPO/issues/99"
            else:
                issue_url = self.issue_urls.pop(0)
            return CommandResult(cmd, cwd_path, f"{issue_url or ''}\n", "", 0)

        if cmd[:3] == ["gh", "pr", "view"]:
            if "--jq" in cmd and ".headRefOid" in cmd:
                return CommandResult(cmd, cwd_path, "abc123\n", "", 0)
            return CommandResult(cmd, cwd_path, json_dumps(self.pr_payload), "", 0)

        if cmd[:3] == ["gh", "issue", "view"]:
            payload = {
                "number": self.issue_payload.get("number", 56),
                "title": self.issue_payload.get("title"),
                "body": self.issue_payload.get("body"),
                "url": self.issue_payload.get("url"),
                "author": self.issue_payload.get("author"),
                "createdAt": self.issue_payload.get("createdAt"),
                "comments": self.issue_comments,
            }
            return CommandResult(cmd, cwd_path, json_dumps(payload), "", 0)

        if cmd[:2] == ["gh", "api"] and "/issues/" in cmd[2]:
            return CommandResult(cmd, cwd_path, json_dumps(self.issue_payload), "", 0)

        if cmd[:2] == ["gh", "api"] and cmd[2].endswith("/check-runs"):
            if "--jq" in cmd:
                return CommandResult(cmd, cwd_path, "success\n", "", 0)
            stdout = (
                json_dumps(self.pr_check_runs_payload) if self.pr_check_runs_returncode == 0 else ""
            )
            return CommandResult(
                cmd,
                cwd_path,
                stdout,
                self.pr_check_runs_stderr,
                self.pr_check_runs_returncode,
            )

        if cmd[:2] == ["gh", "api"] and cmd[2].endswith("/status"):
            stdout = json_dumps(self.pr_status_payload) if self.pr_status_returncode == 0 else ""
            return CommandResult(
                cmd,
                cwd_path,
                stdout,
                self.pr_status_stderr,
                self.pr_status_returncode,
            )

        if cmd[:2] == ["gh", "api"] and "/protection/required_status_checks" in cmd[2]:
            stdout = (
                json_dumps(self.pr_branch_protection_payload)
                if self.pr_branch_protection_returncode == 0
                else ""
            )
            return CommandResult(
                cmd,
                cwd_path,
                stdout,
                self.pr_branch_protection_stderr,
                self.pr_branch_protection_returncode,
            )

        if cmd[:1] == ["sleep"]:
            return CommandResult(cmd, cwd_path, "", "", 0)

        if cmd[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
            if self.git_inside:
                return CommandResult(cmd, cwd_path, "true\n", "", 0)
            return CommandResult(cmd, cwd_path, "false\n", "", 1)

        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return CommandResult(cmd, cwd_path, f"{self.git_head}\n", "", 0)

        if cmd[:3] == ["git", "checkout", "--detach"]:
            if len(cmd) > 3 and cmd[3].startswith("refs/remotes/origin/pr/"):
                self.git_head = self.pr_payload.get("headRefOid", self.git_head)
            return CommandResult(cmd, cwd_path, "", "", 0)

        if cmd[:2] == ["git", "ls-files"]:
            return CommandResult(cmd, cwd_path, "\n".join(self.tracked_files) + "\n", "", 0)

        if cmd[:3] == ["git", "diff", "--name-only"]:
            stdout = "\n".join(self.changed_files) + "\n" if self.diff_returncode == 0 else ""
            return CommandResult(cmd, cwd_path, stdout, self.diff_stderr, self.diff_returncode)

        if cmd[:4] == ["git", "remote", "get-url", "origin"]:
            return CommandResult(cmd, cwd_path, f"{self.git_remote}\n", "", 0)

        if cmd[:3] == ["git", "status", "--porcelain"]:
            return CommandResult(cmd, cwd_path, self.git_status, "", 0)

        if cmd[:3] == ["gh", "repo", "clone"]:
            Path(cmd[4]).mkdir(parents=True, exist_ok=True)
            return CommandResult(cmd, cwd_path, "", "", 0)

        return CommandResult(cmd, cwd_path, "", "", 0)


def json_dumps(value):
    import json

    return json.dumps(value) + "\n"


def command_index(commands, prefix, *, start=0):
    for index in range(start, len(commands)):
        cmd = commands[index][0]
        if cmd[: len(prefix)] == prefix:
            return index
    raise AssertionError(f"Command with prefix {prefix!r} not found.")


def read_usage_summary(log_dir: Path) -> dict:
    summary_paths = list(log_dir.glob("*-usage-summary.json"))
    assert len(summary_paths) == 1
    return json.loads(summary_paths[0].read_text(encoding="utf-8"))


def prior_item_dispositions(*lines: str) -> str:
    if not lines:
        return ""
    return "\n\n### Prior unresolved item dispositions\n" + "\n".join(f"- {line}" for line in lines)


def blocking_issues(*lines: str) -> str:
    if not lines:
        return ""
    return "\n\n### Blocking issues\n" + "\n".join(f"- {line}" for line in lines)


def prior_plan_item_dispositions(*lines: str) -> str:
    if not lines:
        return ""
    return "\n\n### Prior unresolved plan item dispositions\n" + "\n".join(
        f"- {line}" for line in lines
    )


def structured_pr_review(
    *,
    state: str = "approved",
    summary: str = "Review complete.",
    blocking_items: list[str] | None = None,
    same_pr_followups: list[str] | None = None,
    future_followups: list[str] | None = None,
    prior_item_dispositions: list[dict[str, str]] | None = None,
    reviewer: str = "OpenAI Codex",
    human_requirements_resolved: bool = False,
) -> str:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": state,
                "summary": summary,
                "blocking_items": blocking_items or [],
                "same_pr_followups": same_pr_followups or [],
                "future_followups": future_followups or [],
                "prior_item_dispositions": prior_item_dispositions or [],
            }
        )
        + ("\n<!-- HUMAN_REQUIREMENTS_RESOLVED -->" if human_requirements_resolved else "")
        + f"\n<!-- AGENT_STATE: {state} -->\n-- {reviewer}"
    )


def structured_plan_review(
    *,
    state: str = "approved",
    summary: str = "Plan review complete.",
    blocking_plan_issues: list[str] | None = None,
    same_plan_followups: list[str] | None = None,
    future_followups: list[str] | None = None,
    prior_plan_item_dispositions: list[dict[str, str]] | None = None,
    reviewer: str = "OpenAI Codex",
) -> str:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_review",
                "state": state,
                "summary": summary,
                "blocking_plan_issues": blocking_plan_issues or [],
                "same_plan_followups": same_plan_followups or [],
                "future_followups": future_followups or [],
                "prior_plan_item_dispositions": prior_plan_item_dispositions or [],
            }
        )
        + f"\n<!-- AGENT_PLAN_STATE: {state} -->\n-- {reviewer}"
    )


def structured_plan_revision(
    *,
    summary: str = "Revised the plan.",
    prior_plan_item_dispositions: list[dict[str, str]] | None = None,
    plan_steps: list[str] | None = None,
    reviewer: str = "Anthropic Claude",
    human_requirements: str = "",
) -> str:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_revision",
                "state": "blocking",
                "summary": summary,
                "prior_plan_item_dispositions": prior_plan_item_dispositions or [],
                "plan_steps": plan_steps or ["Update the plan.", "Run the relevant tests."],
            }
        )
        + human_requirements
        + "\n<!-- AGENT_PLAN_STATE: blocking -->\n"
        + f"-- {reviewer}"
    )


def structured_coder_followup(
    *,
    state: str = "blocking",
    summary: str = "Updated the PR.",
    addressed_items: list[str] | None = None,
    remaining_items: list[str] | None = None,
    addressed_item_notes: dict[str, str] | None = None,
    remaining_item_notes: dict[str, str] | None = None,
    human_requirement_ids: list[str] | None = None,
    checked_discussion_directly: bool = False,
    reviewer: str = "Anthropic Claude",
) -> str:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": state,
                "summary": summary,
                "addressed_items": addressed_items or [],
                "remaining_items": remaining_items or [],
                "addressed_item_notes": addressed_item_notes or {},
                "remaining_item_notes": remaining_item_notes or {},
                "human_requirements": {
                    "addressed_ids": human_requirement_ids or [],
                    "checked_discussion_directly": checked_discussion_directly,
                },
            }
        )
        + f"\n<!-- AGENT_STATE: {state} -->\n-- {reviewer}"
    )


def make_config(tmp_path, *, create_dirs=True, **overrides):
    config = {
        "repo": "OWNER/REPO",
        "claude_dir": tmp_path / "claude",
        "codex_dir": tmp_path / "codex",
        "gemini_dir": tmp_path / "gemini",
        "coder": "claude",
        "reviewer": "codex",
        "base": "main",
        "max_rounds": 5,
        "auto_merge": False,
        "dry_run": False,
        "allow_shared_dir": False,
        "claude_cmd": "claude",
        "codex_cmd": "codex",
        "gemini_cmd": "gemini",
        "gh_cmd": "gh",
        "claude_args": (),
        "codex_args": (),
        "gemini_args": (),
        "test_command": None,
        "pre_review_tests": True,
        "ci_check_name": "test",
        "ci_timeout_seconds": 1200,
        "ci_poll_interval_seconds": 30,
        "quiet": True,
        "log_dir": tmp_path / "logs",
        "progress_interval_seconds": 30,
        "agent_max_retries": 2,
        "agent_retry_backoff_seconds": (1, 1),
        "agent_memory": True,
        "refresh_agent_memory": False,
        "agent_memory_dir": tmp_path / "claude" / ".agent-loop" / "memory",
        "refresh_test_profile": False,
    }
    config.update(overrides)
    if create_dirs:
        config["claude_dir"].mkdir(parents=True, exist_ok=True)
        config["codex_dir"].mkdir(parents=True, exist_ok=True)
        config["gemini_dir"].mkdir(parents=True, exist_ok=True)
    return AgentLoopConfig(**config)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_migration(revision: str, down_revision: str | tuple[str, ...] | None) -> str:
    return (
        f'revision = "{revision}"\n'
        f"down_revision = {repr(down_revision)}\n"
        "branch_labels = None\n"
        "depends_on = None\n"
    )


def plan_decomposition_json(*phases):
    if not phases:
        phases = (
            {
                "title": "Internal schema utilities",
                "scope": "Add internal helpers only.",
                "non_goals": "No live orchestrator behavior changes.",
                "dependency_notes": "First phase; no dependencies.",
                "rollout_risk": "low - internal only.",
                "validation": "Run parser and orchestrator tests before the next phase.",
                "parent_context": "Approved plan slice: add helpers and tests while preserving existing behavior.",
                "automation": "agent-pr",
                "depends_on": [],
            },
        )
    return json.dumps(
        {
            "schema_version": 1,
            "kind": "plan_decomposition",
            "phases": list(phases),
        }
    )


def _init_git_checkout_with_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    worktree = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "clone", str(origin), str(worktree))
    _git(worktree, "config", "user.email", "test@example.com")
    _git(worktree, "config", "user.name", "Test User")
    _git(worktree, "switch", "-c", "main")
    return worktree


def _commit_all(worktree: Path, message: str) -> None:
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-m", message)


def _push_main(worktree: Path) -> None:
    _git(worktree, "push", "-u", "origin", "main")


def test_validate_pr_migration_topology_blocks_wrong_down_revision(tmp_path):
    worktree = _init_git_checkout_with_origin(tmp_path)
    _write(
        worktree / "alembic" / "versions" / "5d5f0e1a2b3c_base.py",
        _make_migration("5d5f0e1a2b3c", None),
    )
    _write(
        worktree / "alembic" / "versions" / "a6b7c8d9e0f1_add_feature.py",
        _make_migration("a6b7c8d9e0f1", "5d5f0e1a2b3c"),
    )
    _write(
        worktree / "alembic" / "versions" / "402b9e8af79b_latest.py",
        _make_migration("402b9e8af79b", "a6b7c8d9e0f1"),
    )
    _commit_all(worktree, "Base migrations")
    _push_main(worktree)

    _git(worktree, "switch", "-c", "feature/wrong-parent")
    _write(
        worktree / "alembic" / "versions" / "e4f5a6b7c8d9_add_gemini_3_5_flash_pricing.py",
        _make_migration("e4f5a6b7c8d9", "5d5f0e1a2b3c"),
    )
    _commit_all(worktree, "Add migration with stale down_revision")

    result = validate_pr_migration_topology(
        Runner(),
        config=make_config(tmp_path / "config"),
        checkout=worktree,
        pr_metadata=PullRequestMetadata(
            number=77,
            repo="OWNER/REPO",
            title="Add migration",
            head_branch="feature/wrong-parent",
            base_branch="main",
            head_sha=None,
            url=None,
        ),
    )

    assert result.ok is False
    assert result.message is not None
    assert "e4f5a6b7c8d9_add_gemini_3_5_flash_pricing.py" in result.message
    assert "`down_revision = '5d5f0e1a2b3c'`" in result.message
    assert "`402b9e8af79b`" in result.message
    assert "`e4f5a6b7c8d9`" in result.message


def test_validate_pr_migration_topology_allows_linear_head_extension(tmp_path):
    worktree = _init_git_checkout_with_origin(tmp_path)
    _write(
        worktree / "alembic" / "versions" / "5d5f0e1a2b3c_base.py",
        _make_migration("5d5f0e1a2b3c", None),
    )
    _write(
        worktree / "alembic" / "versions" / "402b9e8af79b_latest.py",
        _make_migration("402b9e8af79b", "5d5f0e1a2b3c"),
    )
    _commit_all(worktree, "Base migrations")
    _push_main(worktree)

    _git(worktree, "switch", "-c", "feature/right-parent")
    _write(
        worktree / "alembic" / "versions" / "e4f5a6b7c8d9_add_pricing.py",
        _make_migration("e4f5a6b7c8d9", "402b9e8af79b"),
    )
    _commit_all(worktree, "Add linear migration")

    result = validate_pr_migration_topology(
        Runner(),
        config=make_config(tmp_path / "config"),
        checkout=worktree,
        pr_metadata=PullRequestMetadata(
            number=77,
            repo="OWNER/REPO",
            title="Add migration",
            head_branch="feature/right-parent",
            base_branch="main",
            head_sha=None,
            url=None,
        ),
    )

    assert result.ok is True
    assert result.message is None


def test_validate_pr_migration_topology_skips_block_when_base_already_has_multiple_heads(tmp_path):
    worktree = _init_git_checkout_with_origin(tmp_path)
    _write(
        worktree / "alembic" / "versions" / "111111111111_first_head.py",
        _make_migration("111111111111", None),
    )
    _write(
        worktree / "alembic" / "versions" / "222222222222_second_head.py",
        _make_migration("222222222222", None),
    )
    _commit_all(worktree, "Base has multiple heads")
    _push_main(worktree)

    _git(worktree, "switch", "-c", "feature/merge-heads")
    _write(
        worktree / "alembic" / "versions" / "333333333333_merge_heads.py",
        _make_migration("333333333333", ("111111111111", "222222222222")),
    )
    _commit_all(worktree, "Merge existing heads")

    result = validate_pr_migration_topology(
        Runner(),
        config=make_config(tmp_path / "config"),
        checkout=worktree,
        pr_metadata=PullRequestMetadata(
            number=77,
            repo="OWNER/REPO",
            title="Merge heads",
            head_branch="feature/merge-heads",
            base_branch="main",
            head_sha=None,
            url=None,
        ),
    )

    assert result.ok is True
    assert result.message is None


def test_validate_pr_migration_topology_blocks_non_literal_changed_metadata(tmp_path):
    worktree = _init_git_checkout_with_origin(tmp_path)
    _write(
        worktree / "alembic" / "versions" / "402b9e8af79b_latest.py",
        _make_migration("402b9e8af79b", None),
    )
    _commit_all(worktree, "Base migrations")
    _push_main(worktree)

    _git(worktree, "switch", "-c", "feature/non-literal-migration")
    _write(
        worktree / "alembic" / "versions" / "e4f5a6b7c8d9_non_literal.py",
        'revision = "e4f5a6b7c8d9"\n'
        "PREVIOUS = '402b9e8af79b'\n"
        "down_revision = PREVIOUS\n"
        "branch_labels = None\n"
        "depends_on = None\n",
    )
    _commit_all(worktree, "Add non-literal migration metadata")

    result = validate_pr_migration_topology(
        Runner(),
        config=make_config(tmp_path / "config"),
        checkout=worktree,
        pr_metadata=PullRequestMetadata(
            number=77,
            repo="OWNER/REPO",
            title="Bad migration metadata",
            head_branch="feature/non-literal-migration",
            base_branch="main",
            head_sha=None,
            url=None,
        ),
    )

    assert result.ok is False
    assert result.message is not None
    assert "Could not validate Alembic revision metadata" in result.message
    assert "e4f5a6b7c8d9_non_literal.py" in result.message


def test_parse_claude_output_extracts_text_and_session_id():
    raw = json.dumps({"result": "Hello.", "session_id": "abc123"})
    text, sid, usage, raw_usage = _parse_claude_output(raw)
    assert text == "Hello."
    assert sid == "abc123"
    assert usage is None
    assert raw_usage is None


def test_parse_claude_output_falls_back_on_plain_text():
    raw = "plain response"
    text, sid, usage, raw_usage = _parse_claude_output(raw)
    assert text == "plain response"
    assert sid is None
    assert usage is None
    assert raw_usage is None


def test_parse_claude_output_falls_back_on_non_string_result():
    raw = json.dumps({"result": 42, "session_id": "abc"})
    text, sid, usage, raw_usage = _parse_claude_output(raw)
    assert text == raw  # non-string result → fall back to raw
    assert sid == "abc"
    assert usage is None
    assert raw_usage is None


def test_parse_gemini_output_extracts_json_response():
    raw = json.dumps({
        "response": "Reviewed.\n<!-- AGENT_STATE: approved -->",
        "session_id": "gemini-session-1",
    })
    text, sid, usage, raw_usage, source = _parse_gemini_payload(raw)
    assert text == "Reviewed.\n<!-- AGENT_STATE: approved -->"
    assert sid == "gemini-session-1"
    assert usage is None
    assert raw_usage is None


def test_parse_gemini_output_falls_back_on_plain_text():
    text, sid, usage, raw_usage, source = _parse_gemini_payload("plain response")
    assert text == "plain response"
    assert sid is None
    assert usage is None
    assert raw_usage is None


def test_parse_gemini_output_falls_back_on_non_string_response():
    raw = json.dumps({"response": 42, "session_id": "gemini-session-1"})
    text, sid, usage, raw_usage, source = _parse_gemini_payload(raw)
    assert text == raw
    assert sid == "gemini-session-1"
    assert usage is None
    assert raw_usage is None


def test_parse_gemini_output_prefers_public_response_marker():
    raw = f"""Warning: True color (24-bit) support not detected.
YOLO mode is enabled. All tool calls will be automatically approved.
I will inspect the PR before giving the final answer.
Error executing tool read_file: Path not in workspace.
{PUBLIC_RESPONSE_MARKER}
## Review

No blocking findings.

<!-- AGENT_STATE: approved -->

-- Google Gemini
"""
    text, sid, usage, raw_usage, source = _parse_gemini_payload(raw)
    assert text.startswith("## Review")
    assert "True color" not in text
    assert "YOLO mode" not in text
    assert "I will inspect" not in text
    assert "Error executing tool" not in text
    assert "<!-- AGENT_STATE: approved -->" in text
    assert sid is None
    assert usage is None
    assert raw_usage is None


def test_parse_gemini_output_uses_last_public_response_marker():
    raw = f"""Gemini may mention {PUBLIC_RESPONSE_MARKER} while planning.
{PUBLIC_RESPONSE_MARKER}
intermediate draft
{PUBLIC_RESPONSE_MARKER}
Final answer.
<!-- AGENT_STATE: approved -->
"""
    text, _sid, usage, raw_usage, source = _parse_gemini_payload(raw)
    assert text == "Final answer.\n<!-- AGENT_STATE: approved -->\n"
    assert usage is None
    assert raw_usage is None


def test_parse_gemini_json_response_strips_public_response_marker():
    raw = json.dumps({
        "response": f"diagnostic\n{PUBLIC_RESPONSE_MARKER}\nReviewed.\n<!-- AGENT_STATE: approved -->",
        "session_id": "gemini-session-1",
    })
    text, sid, usage, raw_usage, source = _parse_gemini_payload(raw)
    assert text == "Reviewed.\n<!-- AGENT_STATE: approved -->"
    assert sid == "gemini-session-1"
    assert usage is None
    assert raw_usage is None


def test_parse_gemini_output_strips_cli_preamble_before_final_response():
    raw = """Warning: True color (24-bit) support not detected.
YOLO mode is enabled. All tool calls will be automatically approved.
Attempt 1 failed with status 429. Retrying with backoff... _GaxiosError: [{
  "error": {
    "code": 429,
    "message": "No capacity available for model gemini-3-flash-preview on the server"
  }
}]
I am now ready to provide my final response.

---

## Code Review

Looks good.

<!-- AGENT_STATE: approved -->

-- Google Gemini
"""
    text, sid, usage, raw_usage, source = _parse_gemini_payload(raw)
    assert text.startswith("## Code Review")
    assert "_GaxiosError" not in text
    assert "YOLO mode" not in text
    assert "<!-- AGENT_STATE: approved -->" in text
    assert sid is None
    assert usage is None
    assert raw_usage is None


def test_parse_gemini_output_strips_cli_preamble_before_plan_state_marker():
    raw = """Warning: True color (24-bit) support not detected.
YOLO mode is enabled.
I will now review the plan.

---

## Plan Review

Looks like a solid approach.

<!-- AGENT_PLAN_STATE: approved -->

-- Google Gemini
"""
    text, sid, usage, raw_usage, source = _parse_gemini_payload(raw)
    assert text.startswith("## Plan Review")
    assert "YOLO mode" not in text
    assert "<!-- AGENT_PLAN_STATE: approved -->" in text
    assert sid is None


def test_parse_gemini_output_preserves_markdown_rules_after_preamble():
    raw = """Warning: True color (24-bit) support not detected.
YOLO mode is enabled.

---

## Summary

Reviewed the change.

---

## Details

Still looks good.

<!-- AGENT_STATE: approved -->
"""
    text, sid, usage, raw_usage, source = _parse_gemini_payload(raw)
    assert text.startswith("## Summary")
    assert "YOLO mode" not in text
    assert "## Details" in text
    assert "\n---\n\n## Details" in text
    assert sid is None
    assert usage is None
    assert raw_usage is None


def test_parse_gemini_output_strips_preamble_before_clarification_marker():
    raw = """Warning: True color (24-bit) support not detected.
I need to ask a question.

---

    Which endpoint should I update?
<!-- AGENT_CLARIFY -->
"""
    text, sid, usage, raw_usage, source = _parse_gemini_payload(raw)
    assert text.startswith("    Which endpoint")
    assert "True color" not in text
    assert "<!-- AGENT_CLARIFY -->" in text
    assert sid is None
    assert usage is None
    assert raw_usage is None


def test_normalize_claude_usage_keeps_zero_cached_tokens_exact():
    usage = _normalize_claude_usage(
        {
            "input_tokens": 12,
            "cached_input_tokens": 0,
            "output_tokens": 8,
            "total_tokens": 20,
        }
    )

    assert usage is not None
    assert usage.mode == "exact"
    assert usage.cached_input_tokens == 0


def test_normalize_codex_usage_keeps_zero_reasoning_tokens():
    usage = _normalize_codex_usage(
        {
            "input_tokens": 12,
            "cached_input_tokens": 0,
            "output_tokens": 8,
            "reasoning_tokens": 0,
            "total_tokens": 20,
        }
    )

    assert usage is not None
    assert usage.mode == "exact"
    assert usage.reasoning_tokens == 0


def test_normalize_gemini_usage_keeps_zero_token_values_exact():
    usage = _normalize_gemini_usage(
        {
            "inputTokenCount": 0,
            "cachedInputTokenCount": 0,
            "outputTokenCount": 4,
            "totalTokenCount": 4,
        }
    )

    assert usage is not None
    assert usage.mode == "exact"
    assert usage.input_tokens == 0
    assert usage.cached_input_tokens == 0


def test_extract_codex_usage_reads_turn_completed_jsonl():
    usage, raw_usage = _extract_codex_usage(
        "\n".join(
            [
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 120,
                            "cached_input_tokens": 30,
                            "output_tokens": 45,
                            "reasoning_tokens": 11,
                            "total_tokens": 206,
                        },
                    }
                ),
            ]
        )
    )

    assert usage is not None
    assert usage.mode == "exact"
    assert usage.input_tokens == 120
    assert usage.cached_input_tokens == 30
    assert usage.output_tokens == 45
    assert usage.reasoning_tokens == 11
    assert usage.total_tokens == 206
    assert raw_usage == {
        "input_tokens": 120,
        "cached_input_tokens": 30,
        "output_tokens": 45,
        "reasoning_tokens": 11,
        "total_tokens": 206,
    }


def test_claude_backend_prefers_response_file_over_message_text(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            json.dumps(
                {
                    "result": "stdout message text",
                    "session_id": "claude-session-1",
                }
            )
        ],
        public_response_outputs=["response file text"],
    )
    config = make_config(tmp_path)

    result = CLAUDE_BACKEND.run(runner, config, "Review this PR.", run_id="run-1")

    assert result.response_file_text == "response file text"
    assert result.message_text == "stdout message text"
    assert result.text == "response file text"
    assert result.session_id == "claude-session-1"


def test_gemini_backend_prefers_response_file_over_message_text(tmp_path):
    runner = FakeRunner(
        gemini_outputs=[
            json.dumps(
                {
                    "response": f"diagnostic\n{PUBLIC_RESPONSE_MARKER}\nstdout message text",
                    "session_id": "gemini-session-1",
                }
            )
        ],
        public_response_outputs=["response file text"],
    )
    config = make_config(tmp_path)

    result = GEMINI_BACKEND.run(runner, config, "Review this PR.", run_id="run-1")

    assert result.response_file_text == "response file text"
    assert result.message_text == "stdout message text"
    assert result.text == "response file text"
    assert result.session_id == "gemini-session-1"


def test_codex_backend_prefers_response_file_over_last_message_and_stdout(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            {
                "public_response": "last message text",
                "stdout": "\n".join(
                    [
                        "noisy stdout chatter",
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 12,
                                    "cached_input_tokens": 3,
                                    "output_tokens": 4,
                                    "reasoning_tokens": 1,
                                    "total_tokens": 20,
                                },
                            }
                        ),
                    ]
                ),
            }
        ],
        public_response_outputs=["response file text"],
    )
    config = make_config(tmp_path)

    result = CODEX_BACKEND.run(runner, config, "Review this PR.", run_id="run-1")

    assert result.response_file_text == "response file text"
    assert result.message_text == "last message text"
    assert result.text == "response file text"
    assert result.usage is not None
    assert result.usage.total_tokens == 20


def test_codex_backend_prefers_last_message_over_stdout_without_response_file(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            {
                "public_response": "last message text",
                "stdout": "raw stdout fallback",
            }
        ]
    )
    config = make_config(tmp_path)

    result = CODEX_BACKEND.run(runner, config, "Review this PR.", run_id="run-1")

    assert result.response_file_text is None
    assert result.message_text == "last message text"
    assert result.text == "last message text"


def test_codex_backend_uses_stdout_when_files_are_absent_or_empty(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            {
                "public_response": "",
                "stdout": "raw stdout fallback",
            }
        ]
    )
    config = make_config(tmp_path)

    result = CODEX_BACKEND.run(runner, config, "Review this PR.", run_id="run-1")

    assert result.response_file_text is None
    assert result.message_text == "raw stdout fallback"
    assert result.text == "raw stdout fallback"


def test_codex_backend_dry_run_sets_message_text_without_response_file(tmp_path):
    runner = FakeRunner(codex_outputs=[{"stdout": "dry run stdout"}])
    config = make_config(tmp_path, dry_run=True)

    result = CODEX_BACKEND.run(runner, config, "Review this PR.", run_id="run-1")

    assert result.response_file_text is None
    assert result.message_text == "dry run stdout"
    assert result.text == "dry run stdout"


def test_parse_agent_state_accepts_html_marker():
    assert parse_agent_state("looks fine\n<!-- AGENT_STATE: approved -->") == "approved"
    assert parse_agent_state("needs work\n<!-- agent_state: BLOCKING -->") == "blocking"


def test_parse_agent_state_uses_last_marker_as_authoritative():
    text = """
    Quoting earlier review: <!-- AGENT_STATE: blocking -->

    Final decision:
    <!-- AGENT_STATE: approved -->
    """
    assert parse_agent_state(text) == "approved"


def test_parse_agent_state_requires_marker():
    with pytest.raises(AgentLoopError):
        parse_agent_state("LGTM")


def test_parse_plan_state_uses_last_marker_as_authoritative():
    text = """
    Quoting earlier plan review: <!-- AGENT_PLAN_STATE: blocking -->

    Final decision:
    <!-- AGENT_PLAN_STATE: approved -->
    """
    assert parse_plan_state(text) == "approved"


def test_parse_plan_state_requires_plan_marker():
    with pytest.raises(AgentLoopError):
        parse_plan_state("<!-- AGENT_STATE: approved -->")


def test_parse_signed_human_requirement_body_extracts_text_before_signature():
    body = parse_signed_human_requirement_body(
        "Please use the absolute URL.\n\n-- Human Reviewer\n\nExtra text ignored."
    )

    assert body == "Please use the absolute URL."


@pytest.mark.parametrize(
    "signature",
    [
        "-- Human Reviewer",
        "  -- Human Reviewer  ",
        "-- human reviewer",
        "-- HUMAN REVIEWER",
    ],
)
def test_parse_signed_human_requirement_body_accepts_standalone_signature_variants(signature):
    assert parse_signed_human_requirement_body(f"Required change.\n{signature}\n") == "Required change."


@pytest.mark.parametrize(
    "signature",
    [
        "-- OpenAI Codex",
        "-- Anthropic Claude",
        "-- Google Gemini",
        "-- coding-review-agent-loop",
        "Inline text -- Human Reviewer",
    ],
)
def test_parse_signed_human_requirement_body_rejects_agent_and_non_standalone_signatures(
    signature,
):
    assert parse_signed_human_requirement_body(f"Comment body.\n{signature}\n") is None


def test_parse_non_blocking_followups_extracts_bullets_only_from_section():
    review = """
    Looks good.

    ### Non-blocking follow-ups
    - Add `.agent-loop/` to `.gitignore`.
    1. Add regression coverage for stale memory refresh.
       Include multiple reviewers.

    ### Notes
    - This is not a follow-up.

    <!-- AGENT_STATE: approved -->
    -- OpenAI Codex
    """

    followups = parse_non_blocking_followups(review, reviewer="OpenAI Codex")

    assert [(item.reviewer, item.text) for item in followups] == [
        ("OpenAI Codex", "Add `.agent-loop/` to `.gitignore`."),
        (
            "OpenAI Codex",
            "Add regression coverage for stale memory refresh. Include multiple reviewers.",
        ),
    ]


def test_parse_approved_followups_extracts_same_pr_and_future_independently():
    review = """
    LGTM with cleanup.

    ### Same-PR follow-ups
    - Rename the helper for clarity.
      Keep the public behavior unchanged.

    ### Future follow-ups
    1. Add an integration fixture later.

    ### Non-blocking follow-ups
    - Legacy future item.

    <!-- AGENT_STATE: approved -->
    -- OpenAI Codex
    """

    followups = parse_approved_followups(review, reviewer="OpenAI Codex")

    assert isinstance(followups.same_pr, tuple)
    assert isinstance(followups.future, tuple)
    assert [(item.reviewer, item.text) for item in followups.same_pr] == [
        ("OpenAI Codex", "Rename the helper for clarity. Keep the public behavior unchanged.")
    ]
    assert [(item.reviewer, item.text) for item in followups.future] == [
        ("OpenAI Codex", "Add an integration fixture later."),
        ("OpenAI Codex", "Legacy future item."),
    ]


def test_parse_approved_followups_accepts_trailing_colons_on_headings():
    review = """
    LGTM with follow-ups.

    ### Same-PR follow-ups:
    - Rename the helper for clarity.

    ### Future follow-ups:
    - Add an integration fixture later.

    ### Non-blocking follow-ups:
    - Legacy future item.

    <!-- AGENT_STATE: approved -->
    -- Google Gemini
    """

    followups = parse_approved_followups(review, reviewer="Gemini")

    assert [(item.reviewer, item.text) for item in followups.same_pr] == [
        ("Gemini", "Rename the helper for clarity.")
    ]
    assert [(item.reviewer, item.text) for item in followups.future] == [
        ("Gemini", "Add an integration fixture later."),
        ("Gemini", "Legacy future item."),
    ]


@pytest.mark.parametrize(
    ("same_pr_heading", "future_heading", "legacy_heading"),
    [
        (
            "### **Same-PR follow-ups**",
            "### **Future follow-ups**",
            "### **Non-blocking follow-ups**",
        ),
        (
            "### **Same-PR follow-ups**:",
            "### **Future follow-ups.**",
            "### **Non-blocking follow-ups:**",
        ),
        (
            "### Same-PR follow-ups.",
            "### Future follow-ups.",
            "### Non-blocking follow-ups.",
        ),
    ],
)
def test_parse_approved_followups_accepts_common_markdown_heading_variants(
    same_pr_heading, future_heading, legacy_heading
):
    review = f"""
    LGTM with follow-ups.

    {same_pr_heading}
    - Rename the helper for clarity.

    {future_heading}
    - Add an integration fixture later.

    {legacy_heading}
    - Legacy future item.

    <!-- AGENT_STATE: approved -->
    -- Google Gemini
    """

    followups = parse_approved_followups(review, reviewer="Gemini")

    assert [(item.reviewer, item.text) for item in followups.same_pr] == [
        ("Gemini", "Rename the helper for clarity.")
    ]
    assert [(item.reviewer, item.text) for item in followups.future] == [
        ("Gemini", "Add an integration fixture later."),
        ("Gemini", "Legacy future item."),
    ]


def test_parse_approved_followups_stops_at_unrelated_bold_heading():
    review = """
    LGTM with follow-ups.

    ### Future follow-ups
    - Add an integration fixture later.

    ### **Notes**
    - This is not a follow-up.

    <!-- AGENT_STATE: approved -->
    -- Google Gemini
    """

    followups = parse_approved_followups(review, reviewer="Gemini")

    assert [(item.reviewer, item.text) for item in followups.future] == [
        ("Gemini", "Add an integration fixture later."),
    ]


def test_parse_approved_followups_extracts_bullets_and_prose_paragraphs():
    bullet_review = """
    Codex approves final pass.

    ### Future follow-ups
    - Refine token estimation for large review prompts.

    <!-- AGENT_STATE: approved -->
    -- OpenAI Codex
    """
    prose_review = """
    Claude approves final pass.

    ### Future follow-ups
    The `_parse_gemini_output` helper is dead production code and could be removed
    in a future cleanup.

    ### Same-PR follow-ups
    Rename the helper in this PR before merge.
    Keep the behavior unchanged.

    ### Notes
    This note is outside the follow-up sections.

    <!-- AGENT_STATE: approved -->
    -- Anthropic Claude
    """

    bullet_followups = parse_approved_followups(bullet_review, reviewer="Codex")
    prose_followups = parse_approved_followups(prose_review, reviewer="Claude")

    assert [(item.reviewer, item.text) for item in bullet_followups.future] == [
        ("Codex", "Refine token estimation for large review prompts."),
    ]
    assert [(item.reviewer, item.text) for item in prose_followups.future] == [
        (
            "Claude",
            "The `_parse_gemini_output` helper is dead production code and could be removed in a future cleanup.",
        ),
    ]
    assert [(item.reviewer, item.text) for item in prose_followups.same_pr] == [
        ("Claude", "Rename the helper in this PR before merge. Keep the behavior unchanged."),
    ]


def test_parse_approved_followups_keeps_multiline_markdown_finding_as_one_item():
    review = """
    Still blocked.

    ### Same-PR follow-ups
    #### Normalize `_plan_subject` whitespace handling

    Keep the helper from creating distinct round subjects for leading/trailing
    whitespace-only differences.

    ```python
    assert _plan_subject("x") == _plan_subject(" x ")
    ```

    The implementation should preserve the current hash format.

    ---

    #### Harden `_decode_round_metadata` exception handling

    Invalid base64 and invalid JSON should still become `AgentLoopError`
    consistently.

    ### Notes
    CI note outside the section.

    <!-- AGENT_STATE: blocking -->
    -- Anthropic Claude
    """

    followups = parse_approved_followups(review, reviewer="Anthropic Claude")

    assert [(item.reviewer, item.text) for item in followups.same_pr] == [
        (
            "Anthropic Claude",
            "\n".join(
                [
                    "#### Normalize `_plan_subject` whitespace handling",
                    "",
                    "Keep the helper from creating distinct round subjects for leading/trailing whitespace-only differences.",
                    "",
                    "```python",
                    'assert _plan_subject("x") == _plan_subject(" x ")',
                    "```",
                    "",
                    "The implementation should preserve the current hash format.",
                ]
            ),
        ),
        (
            "Anthropic Claude",
            "\n".join(
                [
                    "#### Harden `_decode_round_metadata` exception handling",
                    "",
                    "Invalid base64 and invalid JSON should still become `AgentLoopError` consistently.",
                ]
            ),
        ),
    ]


@pytest.mark.parametrize(
    "placeholder",
    [
        "None",
        "none.",
        "(none)",
        "(n/a)",
        "N/A",
        "No follow-ups",
        "No same-PR follow-ups.",
        "No future follow-ups",
    ],
)
def test_parse_approved_followups_ignores_empty_placeholders(placeholder):
    review = f"""
    LGTM.

    ### Same-PR follow-ups
    - {placeholder}

    ### Future follow-ups
    - {placeholder}

    <!-- AGENT_STATE: approved -->
    -- Google Gemini
    """

    followups = parse_approved_followups(review, reviewer="Gemini")

    assert followups.same_pr == ()
    assert followups.future == ()


def test_parse_approved_followups_ignores_prose_empty_placeholders():
    review = """
    LGTM.

    ### Same-PR follow-ups
    No same-PR follow-ups.

    ### Future follow-ups
    None

    ### Notes
    This sentence should not be captured.

    <!-- AGENT_STATE: approved -->
    -- Google Gemini
    """

    followups = parse_approved_followups(review, reviewer="Gemini")

    assert followups.same_pr == ()
    assert followups.future == ()


def test_parse_plan_review_items_extracts_structured_sections():
    review = """
    Plan looks sound with one required revision.

    ### Blocking plan issues
    - Cover how the plan avoids mixing `AGENT_STATE` and `AGENT_PLAN_STATE`.

    ### Same-plan follow-ups
    - Mention the exact docs pages to update.

    ### Future follow-ups
    - Consider a later helper to unify plan and PR disposition rendering.

    <!-- AGENT_PLAN_STATE: approved -->
    -- OpenAI Codex
    """

    items = parse_plan_review_items(review, reviewer="OpenAI Codex")

    assert [(item.reviewer, item.text) for item in items.blocking] == [
        (
            "OpenAI Codex",
            "Cover how the plan avoids mixing `AGENT_STATE` and `AGENT_PLAN_STATE`.",
        )
    ]
    assert [(item.reviewer, item.text) for item in items.same_plan] == [
        ("OpenAI Codex", "Mention the exact docs pages to update.")
    ]
    assert [(item.reviewer, item.text) for item in items.future] == [
        (
            "OpenAI Codex",
            "Consider a later helper to unify plan and PR disposition rendering.",
        )
    ]


def test_parse_plan_review_items_keeps_multiline_markdown_blocking_item_as_one_entry():
    review = """
    Plan needs one revision.

    ### Blocking plan issues
    #### Preserve multiline review items during tracking

    Do not split one reviewer-authored finding into separate ledger entries for
    paragraphs or code blocks.

    ```text
    item-2: heading
    item-3: paragraph
    ```

    ### Same-plan follow-ups
    - Mention the regression shape in the implementation plan.

    <!-- AGENT_PLAN_STATE: blocking -->
    -- OpenAI Codex
    """

    items = parse_plan_review_items(review, reviewer="OpenAI Codex")

    assert [(item.reviewer, item.text) for item in items.blocking] == [
        (
            "OpenAI Codex",
            "\n".join(
                [
                    "#### Preserve multiline review items during tracking",
                    "",
                    "Do not split one reviewer-authored finding into separate ledger entries for paragraphs or code blocks.",
                    "",
                    "```text",
                    "item-2: heading",
                    "item-3: paragraph",
                    "```",
                ]
            ),
        )
    ]


@pytest.mark.parametrize(
    "placeholder",
    [
        "None",
        "(none)",
        "(n/a)",
        "No blocking plan issues.",
        "No same-plan follow-ups",
        "No future follow-ups.",
    ],
)
def test_parse_plan_review_items_ignores_empty_placeholders(placeholder):
    review = f"""
    Looks good.

    ### Blocking plan issues
    - {placeholder}

    ### Same-plan follow-ups
    {placeholder}

    ### Future follow-ups
    - {placeholder}

    <!-- AGENT_PLAN_STATE: approved -->
    -- Google Gemini
    """

    items = parse_plan_review_items(review, reviewer="Gemini")

    assert items.blocking == ()
    assert items.same_plan == ()
    assert items.future == ()


def test_parse_plan_item_dispositions_extracts_same_plan_status():
    review = """
    Approved after the latest revision.

    ### Prior unresolved plan item dispositions
    - [item-1] resolved
    - [item-2] still blocking
    - [item-3] same-plan
    - [item-4] future follow-up: okay to track separately now

    <!-- AGENT_PLAN_STATE: approved -->
    -- OpenAI Codex
    """

    dispositions = parse_plan_item_dispositions(review, reviewer="OpenAI Codex")

    assert [(item.item_id, item.disposition, item.note) for item in dispositions] == [
        ("item-1", "resolved", None),
        ("item-2", "blocking", None),
        ("item-3", "same-plan", None),
        ("item-4", "future", "okay to track separately now"),
    ]


def test_parse_plan_item_dispositions_accepts_enriched_labels_with_trailing_arrow():
    review = """
    Approved after the latest revision.

    ### Prior unresolved plan item dispositions
    - [item-1] Same-plan follow-up from Google Gemini, round 1: keep the exact wording distinct -> same-plan: still need the mixed-reviewer case
    - [item-2] Blocking issue from OpenAI Codex, round 1: preserve public labels -> resolved

    <!-- AGENT_PLAN_STATE: blocking -->
    -- Anthropic Claude
    """

    dispositions = parse_plan_item_dispositions(review, reviewer="Anthropic Claude")

    assert [(item.item_id, item.disposition, item.note) for item in dispositions] == [
        ("item-1", "same-plan", "still need the mixed-reviewer case"),
        ("item-2", "resolved", None),
    ]


@pytest.mark.parametrize(
    "line",
    [
        "[item-1] same-plan: none",
        "[item-1] same-plan: N/A",
        "[item-1] same-plan: no same-plan follow-ups",
        "[item-1] still blocking: none",
        "[item-1] still blocking: no blocking plan issues",
        "[item-1] future follow-up: none",
        "[item-1] future follow-up: no future follow-ups",
    ],
)
def test_parse_plan_item_dispositions_rejects_contradictory_active_notes(line):
    review = (
        "Approved after the latest revision."
        + prior_plan_item_dispositions(line)
        + "\n\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="use `resolved` when nothing remains"):
        parse_plan_item_dispositions(review, reviewer="OpenAI Codex")


@pytest.mark.parametrize("line", ["[item-1] same-plan:", "[item-1] still blocking:"])
def test_parse_plan_item_dispositions_rejects_trailing_colon_syntax(line):
    review = (
        "Approved after the latest revision."
        + prior_plan_item_dispositions(line)
        + "\n\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="Invalid prior unresolved plan item disposition"):
        parse_plan_item_dispositions(review, reviewer="OpenAI Codex")


def test_parse_plan_item_dispositions_allows_resolved_none_and_substantive_same_plan():
    review = """
    Approved after the latest revision.
    """
    review += prior_plan_item_dispositions(
        "[item-1] resolved: none",
        "[item-2] same-plan: still need the mixed-reviewer case",
    )
    review += "\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex"

    dispositions = parse_plan_item_dispositions(review, reviewer="OpenAI Codex")

    assert [(item.item_id, item.disposition, item.note) for item in dispositions] == [
        ("item-1", "resolved", "none"),
        ("item-2", "same-plan", "still need the mixed-reviewer case"),
    ]


def test_parse_plan_item_dispositions_ignores_parenthesized_empty_placeholders():
    review = """
    Approved after the latest revision.

    ### Prior unresolved plan item dispositions
    - (none)
    - (n/a)

    <!-- AGENT_PLAN_STATE: approved -->
    -- OpenAI Codex
    """

    assert parse_plan_item_dispositions(review, reviewer="OpenAI Codex") == ()


def test_parse_plan_review_drops_future_followups_in_blocking_reviews():
    review = structured_plan_review(
        state="blocking",
        summary="Still blocked.",
        future_followups=["Do this later."],
    )

    with pytest.raises(AgentLoopError, match="Blocking structured plan reviews may not include future"):
        parse_plan_review(review, reviewer="OpenAI Codex")


def test_parse_plan_review_rejects_future_disposition_in_blocking_reviews():
    review = structured_plan_review(
        state="blocking",
        summary="Still blocked.",
        prior_plan_item_dispositions=[
            {"item_id": "item-1", "disposition": "future", "note": "maybe later"}
        ],
    )

    with pytest.raises(AgentLoopError, match="Blocking plan reviews may not downgrade"):
        parse_plan_review(review, reviewer="OpenAI Codex")


def test_parse_plan_review_rejects_contradictory_prior_plan_item_disposition():
    review = structured_plan_review(
        state="approved",
        summary="Looks good now.",
        prior_plan_item_dispositions=[
            {"item_id": "item-1", "disposition": "same-plan", "note": "none"}
        ],
    )

    with pytest.raises(AgentLoopError, match="empty placeholder"):
        parse_plan_review(review, reviewer="OpenAI Codex")


def test_parse_plan_review_rejects_approved_state_with_active_items():
    plan_review = structured_plan_review(
        state="approved",
        summary="Needs work.",
        same_plan_followups=["Add one more orchestration test."],
    )

    with pytest.raises(AgentLoopError, match="Approved plan reviews must be fully complete"):
        parse_plan_review(plan_review, reviewer="OpenAI Codex")

    with pytest.raises(AgentLoopError, match="AGENT_STATE"):
        parse_review(plan_review, reviewer="OpenAI Codex")


def test_parse_plan_review_rejects_approved_state_with_blocking_items():
    review = structured_plan_review(
        state="approved",
        summary="Needs work.",
        blocking_plan_issues=["Add one more orchestration test."],
    )

    with pytest.raises(AgentLoopError, match="Approved plan reviews must be fully complete"):
        parse_plan_review(review, reviewer="OpenAI Codex")


@pytest.mark.parametrize("line", ["[item-1] still blocking", "[item-1] same-plan"])
def test_parse_plan_review_rejects_approved_state_with_active_prior_disposition(line):
    item_id, disposition = ("item-1", "blocking") if "blocking" in line else ("item-1", "same-plan")
    review = structured_plan_review(
        state="approved",
        summary="Looks good now.",
        prior_plan_item_dispositions=[{"item_id": item_id, "disposition": disposition}],
    )

    with pytest.raises(AgentLoopError, match="Approved plan reviews must be fully complete"):
        parse_plan_review(review, reviewer="OpenAI Codex")


def test_validate_plan_review_response_rejects_duplicate_item_ids():
    review = structured_plan_review(
        state="blocking",
        summary="Still refining the plan.",
        prior_plan_item_dispositions=[
            {"item_id": "item-1", "disposition": "same-plan", "note": "keep the extra regression coverage"},
            {"item_id": "item-1", "disposition": "resolved"},
        ],
    )

    with pytest.raises(AgentLoopError, match="more than once: item-1"):
        _validate_plan_review_response(
            review,
            reviewer="OpenAI Codex",
            unresolved_items=(
                UnresolvedReviewItem(
                    item_id="item-1",
                    reviewer="Anthropic Claude",
                    source_round=1,
                    text="Keep the extra regression coverage.",
                    status="same-plan",
                ),
            ),
        )


def test_validate_plan_review_response_rejects_unknown_item_ids():
    review = structured_plan_review(
        state="approved",
        summary="Looks good now.",
        prior_plan_item_dispositions=[{"item_id": "item-9", "disposition": "resolved"}],
    )

    with pytest.raises(AgentLoopError, match="unknown prior unresolved plan item IDs: item-9"):
        _validate_plan_review_response(
            review,
            reviewer="OpenAI Codex",
            unresolved_items=(
                UnresolvedReviewItem(
                    item_id="item-1",
                    reviewer="Anthropic Claude",
                    source_round=1,
                    text="Keep the extra regression coverage.",
                    status="same-plan",
                ),
            ),
        )


def test_validate_plan_review_response_accepts_structured_resolved_dispositions():
    review = structured_plan_review(
        state="approved",
        summary="Looks good now.",
        prior_plan_item_dispositions=[
            {"item_id": "item-1", "disposition": "resolved"},
            {"item_id": "item-2", "disposition": "resolved"},
        ],
    )

    parsed = _validate_plan_review_response(
        review,
        reviewer="OpenAI Codex",
        unresolved_items=(
            UnresolvedReviewItem(
                item_id="item-1",
                reviewer="Anthropic Claude",
                source_round=1,
                text="Keep the extra regression coverage.",
                status="same-plan",
            ),
            UnresolvedReviewItem(
                item_id="item-2",
                reviewer="Google Gemini",
                source_round=1,
                text="Clarify the fallback trigger.",
                status="blocking",
            ),
        ),
    )

    assert [(item.item_id, item.disposition) for item in parsed.dispositions] == [
        ("item-1", "resolved"),
        ("item-2", "resolved"),
    ]


def test_validate_plan_review_response_rejects_missing_structured_dispositions():
    review = structured_plan_review(
        state="approved",
        summary="Looks good now.",
        prior_plan_item_dispositions=[{"item_id": "item-1", "disposition": "resolved"}],
    )

    with pytest.raises(
        AgentLoopError, match="did not evaluate all prior unresolved plan items: item-2"
    ):
        _validate_plan_review_response(
            review,
            reviewer="OpenAI Codex",
            unresolved_items=(
                UnresolvedReviewItem(
                    item_id="item-1",
                    reviewer="Anthropic Claude",
                    source_round=1,
                    text="Keep the extra regression coverage.",
                    status="same-plan",
                ),
                UnresolvedReviewItem(
                    item_id="item-2",
                    reviewer="Google Gemini",
                    source_round=1,
                    text="Clarify the fallback trigger.",
                    status="blocking",
                ),
            ),
        )


def test_parse_unresolved_item_dispositions_extracts_structured_updates():
    review = """
    LGTM.

    ### Prior unresolved item dispositions
    - [item-1] resolved
    - [item-2] still blocking
    - [item-3] same-pr
    - [item-4] future follow-up: split this into a separate PR

    <!-- AGENT_STATE: approved -->
    -- OpenAI Codex
    """

    dispositions = parse_unresolved_item_dispositions(review, reviewer="OpenAI Codex")

    assert [(item.item_id, item.disposition, item.note) for item in dispositions] == [
        ("item-1", "resolved", None),
        ("item-2", "blocking", None),
        ("item-3", "same-pr", None),
        ("item-4", "future", "split this into a separate PR"),
    ]


def test_parse_unresolved_item_dispositions_accepts_enriched_labels_with_trailing_arrow():
    review = """
    LGTM.

    ### Prior unresolved item dispositions
    - [item-1] Same-PR follow-up from Google Gemini, round 1: require source issue reference in PR body -> same-pr: keep the body reference
    - [item-2] Blocking issue from OpenAI Codex, round 1: rename the helper -> resolved

    <!-- AGENT_STATE: blocking -->
    -- Anthropic Claude
    """

    dispositions = parse_unresolved_item_dispositions(review, reviewer="Anthropic Claude")

    assert [(item.item_id, item.disposition, item.note) for item in dispositions] == [
        ("item-1", "same-pr", "keep the body reference"),
        ("item-2", "resolved", None),
    ]


@pytest.mark.parametrize(
    "line",
    [
        "[item-1] same-pr: none",
        "[item-1] same-pr: N/A",
        "[item-1] same-pr: no same-pr follow-ups",
        "[item-1] still blocking: none",
        "[item-1] still blocking: no blocking issues",
        "[item-1] future follow-up: none",
        "[item-1] future follow up: none",
        "[item-1] future follow-up: no future follow-ups",
        "[item-1] future follow-up: no follow-ups",
    ],
)
def test_parse_unresolved_item_dispositions_rejects_contradictory_active_notes(line):
    review = "LGTM." + prior_item_dispositions(line) + "\n\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"

    with pytest.raises(AgentLoopError, match="use `resolved` when nothing remains"):
        parse_unresolved_item_dispositions(review, reviewer="OpenAI Codex")


@pytest.mark.parametrize("line", ["[item-1] same-pr:", "[item-1] still blocking:"])
def test_parse_unresolved_item_dispositions_rejects_trailing_colon_syntax(line):
    review = "LGTM." + prior_item_dispositions(line) + "\n\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"

    with pytest.raises(
        AgentLoopError,
        match=r"Invalid prior unresolved item disposition.*section `### Prior unresolved item dispositions`, line 4",
    ):
        parse_unresolved_item_dispositions(review, reviewer="OpenAI Codex")


def test_parse_unresolved_item_dispositions_allows_resolved_none_and_substantive_same_pr():
    review = """
    LGTM.
    """
    review += prior_item_dispositions(
        "[item-1] resolved: none",
        "[item-2] same-pr: rename the helper before merge",
    )
    review += "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"

    dispositions = parse_unresolved_item_dispositions(review, reviewer="OpenAI Codex")

    assert [(item.item_id, item.disposition, item.note) for item in dispositions] == [
        ("item-1", "resolved", "none"),
        ("item-2", "same-pr", "rename the helper before merge"),
    ]


def test_parse_unresolved_item_dispositions_ignores_parenthesized_empty_placeholders():
    review = """
    LGTM.

    ### Prior unresolved item dispositions
    - (none)
    - (n/a)

    <!-- AGENT_STATE: approved -->
    -- OpenAI Codex
    """

    assert parse_unresolved_item_dispositions(review, reviewer="OpenAI Codex") == ()


def test_parse_unresolved_item_dispositions_ignores_non_bullet_prose():
    review = """
    LGTM.

    ### Prior unresolved item dispositions
    These are the remaining status calls.
    - [item-1] resolved
    Closing thought after the bullets.

    <!-- AGENT_STATE: approved -->
    -- OpenAI Codex
    """

    dispositions = parse_unresolved_item_dispositions(review, reviewer="OpenAI Codex")

    assert [(item.item_id, item.disposition, item.note) for item in dispositions] == [
        ("item-1", "resolved", None),
    ]


def test_validate_review_response_accepts_structured_resolved_dispositions():
    review = structured_pr_review(
        state="approved",
        summary="LGTM.",
        prior_item_dispositions=[
            {"item_id": "item-1", "disposition": "resolved"},
            {"item_id": "item-2", "disposition": "resolved"},
        ],
    )

    parsed = _validate_review_response(
        review,
        reviewer="OpenAI Codex",
        unresolved_items=(
            UnresolvedReviewItem(
                item_id="item-1",
                reviewer="Anthropic Claude",
                source_round=1,
                text="Rename the helper.",
                status="same-pr",
            ),
            UnresolvedReviewItem(
                item_id="item-2",
                reviewer="Google Gemini",
                source_round=1,
                text="Keep the PR body issue reference.",
                status="blocking",
            ),
        ),
    )

    assert [(item.item_id, item.disposition) for item in parsed.dispositions] == [
        ("item-1", "resolved"),
        ("item-2", "resolved"),
    ]


def test_validate_review_response_rejects_ambiguous_blanket_prose():
    review = """
    LGTM.

    ### Prior unresolved item dispositions
    All prior items look resolved.

    <!-- AGENT_STATE: approved -->
    -- OpenAI Codex
    """

    with pytest.raises(AgentLoopError, match="required structured format"):
        _validate_review_response(
            review,
            reviewer="OpenAI Codex",
            unresolved_items=(
                UnresolvedReviewItem(
                    item_id="item-1",
                    reviewer="Anthropic Claude",
                    source_round=1,
                    text="Rename the helper.",
                    status="same-pr",
                ),
            ),
        )


def test_parse_review_drops_future_followups_in_blocking_reviews():
    review = """
    Still blocked.

    ### Same-PR follow-ups
    - Tighten the helper in this file.

    ### Future follow-ups
    - Do this later.

    <!-- AGENT_STATE: blocking -->
    -- OpenAI Codex
    """

    parsed = parse_review(review, reviewer="OpenAI Codex")

    assert parsed.state == "blocking"
    assert [item.text for item in parsed.followups.same_pr] == ["Tighten the helper in this file."]
    assert parsed.followups.future == ()


def test_parse_review_rejects_contradictory_prior_item_disposition():
    review = "LGTM." + prior_item_dispositions("[item-1] same-pr: none")
    review += "\n\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"

    with pytest.raises(AgentLoopError, match="use `resolved` when nothing remains"):
        parse_review(review, reviewer="OpenAI Codex")


def test_parse_review_rejects_approved_state_with_same_pr_followups():
    review = """
    LGTM.

    ### Same-PR follow-ups
    - Tighten the helper in this file.

    <!-- AGENT_STATE: approved -->
    -- OpenAI Codex
    """

    with pytest.raises(AgentLoopError, match="Approved reviews must be fully complete"):
        parse_review(review, reviewer="OpenAI Codex")


@pytest.mark.parametrize("line", ["[item-1] still blocking", "[item-1] same-pr"])
def test_parse_review_rejects_approved_state_with_active_prior_disposition(line):
    review = "LGTM." + prior_item_dispositions(line)
    review += "\n\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"

    with pytest.raises(AgentLoopError, match="Approved reviews must be fully complete"):
        parse_review(review, reviewer="OpenAI Codex")


def test_parse_review_populates_summary_from_legacy_markdown():
    review = """
    Blocking issue summary.

    ### Same-PR follow-ups
    - Rename the helper.

    <!-- AGENT_STATE: blocking -->
    -- OpenAI Codex
    """

    parsed = parse_review(review, reviewer="OpenAI Codex")

    assert parsed.summary == "Blocking issue summary."


def test_parse_review_round_trips_blocking_issues_section_without_polluting_summary():
    review = (
        "Blocking issue summary."
        + blocking_issues(
            "Cover the regression case in the PR test suite.",
            "Tighten the error assertion wording.",
        )
        + "\n\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
    )

    parsed = parse_review(review, reviewer="OpenAI Codex")

    assert parsed.summary == "Blocking issue summary."
    assert [item.text for item in parsed.blocking_items] == [
        "Cover the regression case in the PR test suite.",
        "Tighten the error assertion wording.",
    ]


def test_parse_review_dedupes_same_pr_items_that_duplicate_blocking_items():
    review = (
        "Blocking issue summary."
        + blocking_issues("`Add the missing share.html CSS update.`")
        + "\n\n### Same-PR follow-ups\n"
        + "- Add the missing share.html CSS update.\n"
        + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
    )

    parsed = parse_review(review, reviewer="OpenAI Codex")

    assert [item.text for item in parsed.blocking_items] == [
        "`Add the missing share.html CSS update.`"
    ]
    assert parsed.followups.same_pr == ()


def test_parse_structured_pr_review_dedupes_exact_normalized_same_pr_duplicates():
    review = json.dumps(
        {
            "schema_version": 1,
            "kind": "pr_review",
            "state": "blocking",
            "summary": "Blocked.",
            "blocking_items": ["- Add the missing `share.html` CSS update."],
            "same_pr_followups": ["Add the missing share.html CSS update"],
            "future_followups": [],
            "prior_item_dispositions": [],
        }
    )
    review += "\n\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"

    parsed = parse_pr_review(review, reviewer="OpenAI Codex")

    assert [item.text for item in parsed.blocking_items] == [
        "- Add the missing `share.html` CSS update."
    ]
    assert parsed.followups.same_pr == ()


def test_parse_structured_pr_review_keeps_near_but_distinct_same_pr_items():
    review = json.dumps(
        {
            "schema_version": 1,
            "kind": "pr_review",
            "state": "blocking",
            "summary": "Blocked.",
            "blocking_items": ["Add the missing share.html CSS update."],
            "same_pr_followups": ["Add the missing share.html print CSS update."],
            "future_followups": [],
            "prior_item_dispositions": [],
        }
    )
    review += "\n\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"

    parsed = parse_pr_review(review, reviewer="OpenAI Codex")

    assert [item.text for item in parsed.blocking_items] == [
        "Add the missing share.html CSS update."
    ]
    assert [item.text for item in parsed.followups.same_pr] == [
        "Add the missing share.html print CSS update."
    ]


def test_legacy_plan_review_helpers_populate_summary_from_markdown():
    review = """
    Plan needs one more regression test.

    ### Same-plan follow-ups
    - Add a regression test matrix.

    <!-- AGENT_PLAN_STATE: blocking -->
    -- OpenAI Codex
    """

    items = parse_plan_review_items(review, reviewer="OpenAI Codex")

    assert _review_freeform_summary_text(review) == "Plan needs one more regression test."
    assert [item.text for item in items.same_plan] == ["Add a regression test matrix."]


def test_parse_plan_review_items_dedupes_plan_buckets_by_normalized_text():
    review = """
    Plan still needs cleanup.

    ### Blocking plan issues
    - Add `retry` coverage.

    ### Same-plan follow-ups
    - *add retry coverage!*
    - Add parser comment.

    ### Future follow-ups
    - ADD RETRY COVERAGE.
    - add `parser` comment
    - Add parser documentation later.

    <!-- AGENT_PLAN_STATE: blocking -->
    -- OpenAI Codex
    """

    items = parse_plan_review_items(review, reviewer="OpenAI Codex")

    assert [item.text for item in items.blocking] == ["Add `retry` coverage."]
    assert [item.text for item in items.same_plan] == ["Add parser comment."]
    assert [item.text for item in items.future] == ["Add parser documentation later."]


def test_parse_structured_pr_review_normalizes_v1_payload_with_footer_contract():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "approved",
                "summary": "Looks good after the latest fix.",
                "blocking_items": [],
                "same_pr_followups": [],
                "future_followups": ["Document cleanup for a later PR."],
                "prior_item_dispositions": [
                    {"item_id": "item-1", "disposition": "resolved"},
                    {
                        "item_id": "item-2",
                        "disposition": "future",
                        "note": "okay to split into follow-up work",
                    },
                ],
            }
        )
        + "\n<!-- HUMAN_REQUIREMENTS_RESOLVED -->\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex\n"
    )

    parsed = parse_structured_pr_review(payload, reviewer="OpenAI Codex")

    assert parsed is not None
    assert parsed.state == "approved"
    assert parsed.summary == "Looks good after the latest fix."
    assert parsed.blocking_items == ()
    assert [item.text for item in parsed.followups.future] == ["Document cleanup for a later PR."]
    assert [(item.item_id, item.disposition, item.note) for item in parsed.dispositions] == [
        ("item-1", "resolved", None),
        ("item-2", "future", "okay to split into follow-up work"),
    ]


def test_parse_structured_pr_review_tolerates_omitted_empty_collections():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "approved",
                "summary": "Looks good after the latest fix.",
                "prior_item_dispositions": [
                    {"item_id": "item-1", "disposition": "resolved"},
                ],
            }
        )
        + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex\n"
    )

    parsed = parse_structured_pr_review(payload, reviewer="OpenAI Codex")

    assert parsed is not None
    assert parsed.state == "approved"
    assert parsed.blocking_items == ()
    assert parsed.followups.same_pr == ()
    assert parsed.followups.future == ()
    assert [(item.item_id, item.disposition) for item in parsed.dispositions] == [
        ("item-1", "resolved")
    ]


def test_parse_structured_pr_review_strips_verdict_and_sections_from_json_summary():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "blocking",
                "summary": (
                    "**Review verdict:** blocking\n\n"
                    "Need one more regression test.\n\n"
                    "### Blocking issues\n"
                    "- Duplicate line that should not remain in the summary."
                ),
                "blocking_items": ["Need one more regression test."],
                "same_pr_followups": [],
                "future_followups": [],
                "prior_item_dispositions": [],
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex\n"
    )

    parsed = parse_structured_pr_review(payload, reviewer="OpenAI Codex")

    assert parsed is not None
    assert parsed.summary == "Need one more regression test."


def test_parse_structured_pr_review_rejects_kind_mismatch():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_review",
                "state": "approved",
                "summary": "Wrong kind.",
                "blocking_plan_issues": [],
                "same_plan_followups": [],
                "future_followups": [],
                "prior_plan_item_dispositions": [],
            }
        )
        + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="kind mismatch"):
        parse_structured_pr_review(payload, reviewer="OpenAI Codex")


def test_parse_structured_pr_review_hard_fails_on_unsupported_schema_version():
    payload = (
        json.dumps(
            {
                "schema_version": 2,
                "kind": "pr_review",
                "state": "approved",
                "summary": "Wrong version.",
                "blocking_items": [],
                "same_pr_followups": [],
                "future_followups": [],
                "prior_item_dispositions": [],
            }
        )
        + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="Unsupported structured response schema_version: 2"):
        parse_structured_pr_review(payload, reviewer="OpenAI Codex")


def test_parse_pr_review_rejects_markdown_when_no_structured_candidate_exists():
    review = "Looks good in markdown.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"

    with pytest.raises(AgentLoopError, match="required structured format"):
        parse_pr_review(review, reviewer="OpenAI Codex")


def test_legacy_parse_review_still_parses_markdown_for_historical_display():
    review = "Looks good in markdown.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"

    parsed = parse_review(review, reviewer="OpenAI Codex")

    assert parsed.state == "approved"
    assert parsed.summary == "Looks good in markdown."


def test_parse_pr_review_rejects_invalid_structured_candidate_instead_of_falling_back_to_markdown():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "approved",
                "summary": "Missing required arrays.",
                "blocking_items": [],
                "same_pr_followups": [],
                "future_followups": [],
            }
        )
        + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="missing required field"):
        parse_pr_review(payload, reviewer="OpenAI Codex")


def test_parse_structured_pr_review_rejects_future_followups_in_blocking_reviews():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "blocking",
                "summary": "Still blocked.",
                "blocking_items": ["Needs one more test."],
                "same_pr_followups": [],
                "future_followups": ["Clean this up later."],
                "prior_item_dispositions": [],
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="Blocking structured reviews may not include future"):
        parse_structured_pr_review(payload, reviewer="OpenAI Codex")


def test_parse_pr_review_rejects_structured_candidate_with_unknown_nested_keys():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "approved",
                "summary": "LGTM.",
                "blocking_items": [],
                "same_pr_followups": [],
                "future_followups": [],
                "prior_item_dispositions": [
                    {"item_id": "item-1", "disposition": "resolved", "extra": "nope"},
                ],
            }
        )
        + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="unknown field"):
        parse_pr_review(payload, reviewer="OpenAI Codex")


def test_parse_pr_review_rejects_structured_candidate_with_invalid_item_id():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "approved",
                "summary": "LGTM.",
                "blocking_items": [],
                "same_pr_followups": [],
                "future_followups": [],
                "prior_item_dispositions": [
                    {"item_id": "item 1", "disposition": "resolved"},
                ],
            }
        )
        + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="must match"):
        parse_pr_review(payload, reviewer="OpenAI Codex")


def test_parse_pr_review_requires_strict_structured_disposition_enums():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "approved",
                "summary": "LGTM.",
                "blocking_items": [],
                "same_pr_followups": [],
                "future_followups": [],
                "prior_item_dispositions": [
                    {"item_id": "item-1", "disposition": "still blocking"},
                ],
            }
        )
        + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="must be one of"):
        parse_pr_review(payload, reviewer="OpenAI Codex")


def test_parse_structured_pr_review_rejects_approved_blocking_items():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "approved",
                "summary": "Almost there.",
                "blocking_items": ["Still needs a regression test."],
                "same_pr_followups": [],
                "future_followups": [],
                "prior_item_dispositions": [],
            }
        )
        + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="Approved reviews must be fully complete"):
        parse_structured_pr_review(payload, reviewer="OpenAI Codex")


@pytest.mark.parametrize(
    "suffix",
    [
        "\nExtra explanation after the payload.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        "\n```text\nextra block\n```\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        "\n- stray bullet\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
    ],
)
def test_parse_structured_pr_review_rejects_trailing_content_before_footer(suffix):
    payload = json.dumps(
        {
            "schema_version": 1,
            "kind": "pr_review",
            "state": "approved",
            "summary": "LGTM.",
            "blocking_items": [],
            "same_pr_followups": [],
            "future_followups": [],
            "prior_item_dispositions": [],
        }
    )

    with pytest.raises(
        AgentLoopError,
        match="place <!-- AGENT_STATE|may not include prose between|may not include trailing prose",
    ):
        parse_structured_pr_review(payload + suffix, reviewer="OpenAI Codex")


def test_parse_structured_pr_review_rejects_footer_state_mismatch():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "approved",
                "summary": "LGTM.",
                "blocking_items": [],
                "same_pr_followups": [],
                "future_followups": [],
                "prior_item_dispositions": [],
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="must match the payload state"):
        parse_structured_pr_review(payload, reviewer="OpenAI Codex")


def test_parse_structured_pr_review_falls_back_when_json_is_embedded_in_markdown():
    review = """
    Here is an example:

    ```json
    {"schema_version": 1, "kind": "pr_review"}
    ```

    <!-- AGENT_STATE: approved -->
    -- OpenAI Codex
    """

    assert parse_structured_pr_review(review, reviewer="OpenAI Codex") is None
    assert parse_review(review, reviewer="OpenAI Codex").state == "approved"


def test_parse_structured_plan_review_normalizes_v1_payload():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_review",
                "state": "approved",
                "summary": "Plan looks good.",
                "blocking_plan_issues": [],
                "same_plan_followups": [],
                "future_followups": ["Consider a later cleanup pass."],
                "prior_plan_item_dispositions": [{"item_id": "item-1", "disposition": "resolved"}],
            }
        )
        + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex"
    )

    parsed = parse_structured_plan_review(payload, reviewer="OpenAI Codex")

    assert parsed is not None
    assert parsed.summary == "Plan looks good."
    assert [item.text for item in parsed.items.future] == ["Consider a later cleanup pass."]
    assert [(item.item_id, item.disposition) for item in parsed.dispositions] == [
        ("item-1", "resolved")
    ]


def test_parse_structured_plan_review_tolerates_omitted_empty_collections():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_review",
                "state": "approved",
                "summary": "Plan looks good.",
                "prior_plan_item_dispositions": [{"item_id": "item-1", "disposition": "resolved"}],
            }
        )
        + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex"
    )

    parsed = parse_structured_plan_review(payload, reviewer="OpenAI Codex")

    assert parsed is not None
    assert parsed.items.blocking == ()
    assert parsed.items.same_plan == ()
    assert parsed.items.future == ()
    assert [(item.item_id, item.disposition) for item in parsed.dispositions] == [
        ("item-1", "resolved")
    ]


def test_parse_structured_plan_review_strips_verdict_and_sections_from_json_summary():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_review",
                "state": "blocking",
                "summary": (
                    "**Review verdict:** blocking\n\n"
                    "Need clearer rollback coverage.\n\n"
                    "### Same-plan follow-ups\n"
                    "- Extra duplicate text."
                ),
                "blocking_plan_issues": ["Need clearer rollback coverage."],
                "same_plan_followups": [],
                "future_followups": [],
                "prior_plan_item_dispositions": [],
            }
        )
        + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex"
    )

    parsed = parse_structured_plan_review(payload, reviewer="OpenAI Codex")

    assert parsed is not None
    assert parsed.summary == "Need clearer rollback coverage."


def test_parse_structured_plan_review_dedupes_same_plan_against_blocking_items():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_review",
                "state": "blocking",
                "summary": "Still blocked.",
                "blocking_plan_issues": ["Add `retry` coverage."],
                "same_plan_followups": [
                    "*add retry coverage!*",
                    "Add retry coverage for timeout handling.",
                ],
                "future_followups": [],
                "prior_plan_item_dispositions": [],
            }
        )
        + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex"
    )

    parsed = parse_structured_plan_review(payload, reviewer="OpenAI Codex")

    assert parsed is not None
    assert [item.text for item in parsed.items.blocking] == ["Add `retry` coverage."]
    assert [item.text for item in parsed.items.same_plan] == [
        "Add retry coverage for timeout handling."
    ]


def test_parse_structured_plan_review_rejects_blocking_future_followups():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_review",
                "state": "blocking",
                "summary": "Still blocked.",
                "blocking_plan_issues": ["Need clearer rollback coverage."],
                "same_plan_followups": [],
                "future_followups": ["Refactor the prompt later."],
                "prior_plan_item_dispositions": [],
            }
        )
        + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="Blocking structured plan reviews may not include future"):
        parse_structured_plan_review(payload, reviewer="OpenAI Codex")


def test_validate_structured_coder_followup_accepts_v1_payload():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Addressed the first item; one remains.",
                "addressed_items": ["item-1"],
                "remaining_items": ["item-2"],
                "human_requirements": {
                    "addressed_ids": ["Requirement 1"],
                    "checked_discussion_directly": False,
                },
                "tests_run": ["python -m pytest tests/test_agent_loop.py -k structured"],
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
    )

    parsed = validate_structured_coder_followup(payload)

    assert parsed is not None
    assert parsed.addressed_items == ("item-1",)
    assert parsed.remaining_items == ("item-2",)
    assert parsed.human_requirements.addressed_ids == ("Requirement 1",)
    assert parsed.addressed_item_notes == {}
    assert parsed.remaining_item_notes == {}


def test_validate_structured_coder_followup_accepts_optional_item_notes():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Addressed the parser; deferred the docs.",
                "addressed_items": ["item-1"],
                "remaining_items": ["item-2"],
                "addressed_item_notes": {"item-1": "Added parsing coverage."},
                "remaining_item_notes": {"item-2": "Deferred until the docs owner weighs in."},
                "human_requirements": {
                    "addressed_ids": [],
                    "checked_discussion_directly": False,
                },
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
    )

    parsed = validate_structured_coder_followup(payload)

    assert parsed is not None
    assert parsed.addressed_item_notes == {"item-1": "Added parsing coverage."}
    assert parsed.remaining_item_notes == {
        "item-2": "Deferred until the docs owner weighs in."
    }


def test_validate_structured_coder_followup_rejects_note_for_unlisted_item():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Addressed one item.",
                "addressed_items": ["item-1"],
                "remaining_items": [],
                "addressed_item_notes": {"item-2": "This note is stale."},
                "human_requirements": {
                    "addressed_ids": [],
                    "checked_discussion_directly": False,
                },
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="item-2.*not listed in coder_followup.addressed_items"):
        validate_structured_coder_followup(payload)


@pytest.mark.parametrize("bad_note", ["", "   ", 5, None])
def test_validate_structured_coder_followup_rejects_invalid_note_values(bad_note):
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Addressed one item.",
                "addressed_items": ["item-1"],
                "remaining_items": [],
                "addressed_item_notes": {"item-1": bad_note},
                "human_requirements": {
                    "addressed_ids": [],
                    "checked_discussion_directly": False,
                },
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="coder_followup.addressed_item_notes.item-1"):
        validate_structured_coder_followup(payload)


def test_validate_structured_coder_followup_returns_none_when_no_structured_candidate_exists():
    assert (
        validate_structured_coder_followup(
            "Implemented the fix.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
        )
        is None
    )


def test_validate_structured_coder_followup_rejects_unknown_keys_in_structured_candidate():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "approved",
                "summary": "Done.",
                "addressed_items": [],
                "remaining_items": [],
                "human_requirements": {
                    "addressed_ids": [],
                    "checked_discussion_directly": True,
                    "extra": "nope",
                },
            }
        )
        + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="unknown field\\(s\\): extra"):
        validate_structured_coder_followup(payload)


def test_validate_structured_coder_followup_rejects_footer_state_mismatch():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Done.",
                "addressed_items": [],
                "remaining_items": [],
                "human_requirements": {
                    "addressed_ids": [],
                    "checked_discussion_directly": True,
                },
            }
        )
        + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="footer AGENT_STATE must match the payload state"):
        validate_structured_coder_followup(payload)


def test_validate_structured_coder_followup_rejects_trailing_prose_after_footer():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Done.",
                "addressed_items": [],
                "remaining_items": [],
                "human_requirements": {
                    "addressed_ids": [],
                    "checked_discussion_directly": True,
                },
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex\nextra"
    )

    with pytest.raises(AgentLoopError, match="may not include trailing prose"):
        validate_structured_coder_followup(payload)


@pytest.mark.parametrize(
    ("addressed_ids", "checked_discussion_directly", "surfaced_ids", "requires_direct_discussion_ack", "message"),
    [
        (
            ("Requirement 1",),
            False,
            ("Requirement 1", "Requirement 2"),
            False,
            "did not address all surfaced signed human requirement IDs",
        ),
        (
            ("Requirement 1", "Requirement 1"),
            False,
            ("Requirement 1",),
            False,
            "listed signed human requirement IDs more than once",
        ),
        (
            ("Requirement 99",),
            False,
            ("Requirement 1",),
            False,
            "referenced unknown signed human requirement IDs",
        ),
        (
            (),
            False,
            (),
            True,
            "must acknowledge that the prompt omitted the detailed signed human requirements",
        ),
    ],
)
def test_validate_structured_human_requirements_acknowledgement_rejects_invalid_payloads(
    addressed_ids,
    checked_discussion_directly,
    surfaced_ids,
    requires_direct_discussion_ack,
    message,
):
    with pytest.raises(AgentLoopError, match=message):
        validate_structured_human_requirements_acknowledgement(
            addressed_ids,
            checked_discussion_directly=checked_discussion_directly,
            surfaced_requirement_ids=surfaced_ids,
            requires_direct_discussion_ack=requires_direct_discussion_ack,
        )


def test_validate_structured_plan_revision_accepts_v1_payload():
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_revision",
                "state": "blocking",
                "summary": "Revised the plan to cover rollback testing.",
                "prior_plan_item_dispositions": [
                    {"item_id": "item-1", "disposition": "resolved", "note": "Covered in the new tests."}
                ],
                "plan_steps": ["Update protocol.py.", "Add regression tests."],
            }
        )
        + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex"
    )

    parsed = validate_structured_plan_revision(payload)

    assert parsed is not None
    assert parsed.state == "blocking"
    assert [(item.item_id, item.disposition) for item in parsed.prior_plan_item_dispositions] == [
        ("item-1", "resolved")
    ]
    assert parsed.plan_steps == ("Update protocol.py.", "Add regression tests.")


def test_validate_plan_revision_response_rejects_marker_only_markdown():
    with pytest.raises(AgentLoopError, match="Plan revision did not use the required structured format"):
        _validate_plan_revision_response(
            "Revised plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex"
        )


@pytest.mark.parametrize(
    ("payload", "pattern"),
    [
        (
            {
                "schema_version": 1,
                "kind": "plan_revision",
                "state": "approved",
                "summary": "Wrong state.",
                "prior_plan_item_dispositions": [],
                "plan_steps": ["Update protocol.py."],
            },
            "plan_revision.state must be `blocking`",
        ),
        (
            {
                "schema_version": 1,
                "kind": "plan_revision",
                "state": "blocking",
                "summary": "",
                "prior_plan_item_dispositions": [],
                "plan_steps": ["Update protocol.py."],
            },
            "plan_revision.summary must be a non-empty string",
        ),
        (
            {
                "schema_version": 1,
                "kind": "plan_revision",
                "state": "blocking",
                "summary": "Missing steps.",
                "prior_plan_item_dispositions": [],
                "plan_steps": [],
            },
            "plan_revision.plan_steps must contain at least 1 item",
        ),
    ],
)
def test_validate_structured_plan_revision_rejects_invalid_payload(payload, pattern):
    footer_state = payload["state"]
    text = json.dumps(payload) + f"\n<!-- AGENT_PLAN_STATE: {footer_state} -->\n-- OpenAI Codex"

    with pytest.raises(AgentLoopError, match=pattern):
        validate_structured_plan_revision(text)


def test_extract_structured_plan_review_payload_rejects_embedded_json_markdown():
    review = """
    Here is an example:

    ```json
    {"schema_version": 1, "kind": "plan_review"}
    ```

    <!-- AGENT_PLAN_STATE: approved -->
    -- OpenAI Codex
    """

    assert _extract_structured_plan_review_payload(review) is None


@pytest.mark.parametrize(
    ("builder", "extractor"),
    [
        (lambda: structured_plan_review(reviewer="Google Gemini"), _extract_structured_plan_review_payload),
        (lambda: structured_pr_review(reviewer="Google Gemini"), _extract_structured_pr_review_payload),
        (lambda: structured_coder_followup(reviewer="Anthropic Claude"), _extract_structured_coder_followup_payload),
        (lambda: structured_plan_revision(reviewer="Anthropic Claude"), _extract_structured_plan_revision_payload),
    ],
)
def test_structured_extractors_recover_leading_public_response_marker(builder, extractor):
    text = f"\n\n{PUBLIC_RESPONSE_MARKER}\n{builder()}"

    payload = extractor(text)

    assert payload is not None


def test_response_file_marker_normalization_reports_unrecoverable_marker():
    text = f"{PUBLIC_RESPONSE_MARKER}\n### Review\nLooks good."

    normalized, status = normalize_response_file_structured_text(text)

    assert normalized == text
    assert status == "leading-public-response-marker-not-recoverable"


@pytest.mark.parametrize(
    "text",
    [
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_review",
                "state": "approved",
                "summary": PUBLIC_RESPONSE_MARKER,
                "blocking_plan_issues": [],
                "same_plan_followups": [],
                "future_followups": [],
                "prior_plan_item_dispositions": [],
            }
        )
        + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- Google Gemini",
        "Some prose first.\n"
        + PUBLIC_RESPONSE_MARKER
        + "\n"
        + structured_plan_review(reviewer="Google Gemini"),
    ],
)
def test_response_file_marker_not_stripped_inside_json_or_mid_prose(text):
    normalized, status = normalize_response_file_structured_text(text)

    assert normalized == text
    assert status is None


def test_extract_structured_plan_review_payload_rejects_footer_state_mismatch():
    payload = json.dumps(
        {
            "schema_version": 1,
            "kind": "plan_review",
            "state": "approved",
            "summary": "Plan looks good.",
            "blocking_plan_issues": [],
            "same_plan_followups": [],
            "future_followups": [],
            "prior_plan_item_dispositions": [],
        }
    )
    text = payload + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex"

    with pytest.raises(AgentLoopError, match="footer AGENT_PLAN_STATE must match"):
        _extract_structured_plan_review_payload(text)


def test_extract_structured_plan_review_payload_rejects_trailing_prose_after_signature():
    payload = json.dumps(
        {
            "schema_version": 1,
            "kind": "plan_review",
            "state": "approved",
            "summary": "Plan looks good.",
            "blocking_plan_issues": [],
            "same_plan_followups": [],
            "future_followups": [],
            "prior_plan_item_dispositions": [],
        }
    )
    text = payload + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex\nextra"

    with pytest.raises(AgentLoopError, match="trailing prose"):
        _extract_structured_plan_review_payload(text)


def test_parse_plan_review_hard_fails_after_top_level_json_prefix():
    review = (
        '{"schema_version":1,"kind":"plan_review","state":"approved","summary":"Plan looks good.",'
        '"blocking_plan_issues":[],"same_plan_followups":[],"future_followups":[]}\n'
        "<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match="plan_review is missing required field"):
        parse_plan_review(review, reviewer="OpenAI Codex")


def test_extract_structured_plan_revision_payload_accepts_human_requirements_prefix():
    payload = json.dumps(
        {
            "schema_version": 1,
            "kind": "plan_revision",
            "state": "blocking",
            "summary": "Revised the plan.",
            "prior_plan_item_dispositions": [],
            "plan_steps": ["Update protocol.py."],
        }
    )
    text = (
        payload
        + "\n<!-- HUMAN_REQUIREMENTS_ADDRESSED -->\n\n### Human requirements\n- Requirement 1: covered in step 1.\n"
        + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex"
    )

    assert _extract_structured_plan_revision_payload(text) is not None


def test_extract_structured_plan_revision_payload_rejects_bad_footer_ordering():
    payload = json.dumps(
        {
            "schema_version": 1,
            "kind": "plan_revision",
            "state": "blocking",
            "summary": "Revised the plan.",
            "prior_plan_item_dispositions": [],
            "plan_steps": ["Update protocol.py."],
        }
    )
    text = payload + "\n-- OpenAI Codex\n<!-- AGENT_PLAN_STATE: blocking -->"

    with pytest.raises(AgentLoopError, match="AGENT_PLAN_STATE"):
        _extract_structured_plan_revision_payload(text)


def test_expect_string_list_enforces_min_length():
    with pytest.raises(AgentLoopError, match="must contain at least 1 item"):
        _expect_string_list([], context="plan_revision.plan_steps", item_context="plan_revision.plan_steps", min_length=1)


def test_review_prompt_includes_prior_unresolved_items_and_disposition_instructions(tmp_path):
    config = make_config(tmp_path, approved_followups="fix-and-summarize")
    prompt = build_review_prompt(
        77,
        2,
        config,
        reviewer="codex",
        pr_metadata=PullRequestMetadata(
            number=77,
            repo="OWNER/REPO",
            title="Improve review prompt context",
            head_branch="feature/review-context",
            base_branch="main",
            head_sha="abc123",
            url="https://github.com/OWNER/REPO/pull/77",
        ),
        unresolved_items=(
            UnresolvedReviewItem(
                item_id="item-1",
                reviewer="Anthropic Claude",
                source_round=1,
                text="Needs a regression test before merge.",
                status="blocking",
            ),
            UnresolvedReviewItem(
                item_id="item-2",
                reviewer="OpenAI Codex",
                source_round=1,
                text="Rename the helper before merge.",
                status="same-pr",
            ),
        ),
    )

    assert "Prior unresolved review items from earlier rounds" in prompt
    assert "[item-1] blocking from Anthropic Claude in round 1" in prompt
    assert "[item-2] same-pr from OpenAI Codex in round 1" in prompt
    assert "### Prior unresolved item dispositions" in prompt
    assert "- [item-id] resolved" in prompt
    assert "Only use `future follow-up` when returning `approved`." in prompt
    assert "Contradictory forms like `same-pr: none`, `still blocking: none`, and `future follow-up: none` are invalid" in prompt
    assert "Only items listed under `Prior unresolved review items from earlier rounds`" in prompt
    assert "same-round findings from\nother reviewers appear elsewhere in the PR discussion" in prompt
    assert "Same-PR follow-ups may appear only in blocking reviews." in prompt
    assert "no blocking issues, no Same-PR follow-ups, and no" in prompt
    assert '"kind": "pr_review"' in prompt
    assert "After the JSON object, include only:" in prompt
    assert "Use this mandatory structured PR review format" in prompt
    assert "Markdown fallback" not in prompt


def test_review_prompt_indents_multiline_prior_unresolved_item_text(tmp_path):
    config = make_config(tmp_path, approved_followups="fix-and-summarize")
    prompt = build_review_prompt(
        77,
        2,
        config,
        reviewer="codex",
        pr_metadata=PullRequestMetadata(
            number=77,
            repo="OWNER/REPO",
            title="Improve review prompt context",
            head_branch="feature/review-context",
            base_branch="main",
            head_sha="abc123",
            url="https://github.com/OWNER/REPO/pull/77",
        ),
        unresolved_items=(
            UnresolvedReviewItem(
                item_id="item-1",
                reviewer="Anthropic Claude",
                source_round=1,
                text="Needs a regression test before merge.\n\nInclude the mixed-reviewer approval case.",
                status="blocking",
            ),
        ),
    )

    assert "  Needs a regression test before merge." in prompt
    assert "\n\n  Include the mixed-reviewer approval case." in prompt


def test_plan_review_prompt_includes_structured_sections_and_prior_items(tmp_path):
    config = make_config(tmp_path, reviewer=("codex", "gemini"))
    prompt = build_plan_review_prompt(
        56,
        2,
        "Revise protocol parsing and add tests.",
        config,
        reviewer="codex",
        unresolved_items=(
            UnresolvedReviewItem(
                item_id="item-1",
                reviewer="Anthropic Claude",
                source_round=1,
                text="Define exact plan-review headings.",
                status="blocking",
            ),
            UnresolvedReviewItem(
                item_id="item-2",
                reviewer="Google Gemini",
                source_round=1,
                text="Add an orchestration carry-forward test.",
                status="same-plan",
            ),
        ),
    )

    assert "### Prior unresolved plan item dispositions" in prompt
    assert "[item-1] blocking from Anthropic Claude in round 1" in prompt
    assert "[item-2] same-plan from Google Gemini in round 1" in prompt
    assert "Only use `future follow-up` when returning `approved`." in prompt
    assert "Contradictory forms like `same-plan: none`, `still blocking: none`, and `future follow-up: none` are invalid" in prompt
    assert "Only items listed under `Prior unresolved plan items from earlier rounds`" in prompt
    assert "same-round findings from other\nreviewers appear elsewhere in the issue discussion" in prompt
    assert "Same-plan\nfollow-ups are small current-plan refinements" in prompt
    assert "must be incorporated before\nimplementation starts" in prompt
    assert "they may appear only in blocking plan reviews" in prompt
    assert "Future\nfollow-ups are independent later work" in prompt
    assert "A concern or\nparaphrase belongs in exactly one current-round list" in prompt
    assert "Do not duplicate or reclassify\nthe same concern across Same-plan and Future follow-up lists" in prompt
    assert "do not use structured Future\nfollow-ups" in prompt
    assert "no blocking plan issues, no Same-plan\nfollow-ups, and no carried-forward plan items left active" in prompt
    assert '"kind": "plan_review"' in prompt
    assert '"prior_plan_item_dispositions"' in prompt
    assert "Use this mandatory structured JSON response format" in prompt
    assert "markdown compatibility" not in prompt.lower()


def test_plan_revision_prompt_includes_unresolved_ledger_and_required_dispositions(tmp_path):
    config = make_config(tmp_path, reviewer=("codex", "gemini"))
    prompt = build_plan_revision_prompt(
        56,
        2,
        "Previous plan text.",
        "OpenAI Codex plan review:\n\nNeeds a carry-forward ledger.",
        config,
        unresolved_items=(
            UnresolvedReviewItem(
                item_id="item-3",
                reviewer="OpenAI Codex",
                source_round=1,
                text="Track unresolved plan items across rounds.",
                status="blocking",
            ),
        ),
    )

    assert "Prior unresolved plan items from earlier rounds" in prompt
    assert "[item-3] blocking from OpenAI Codex in round 1" in prompt
    assert "### Prior plan review item dispositions" in prompt
    assert "- [item-id] same-plan:" in prompt
    assert "Use `same-plan`, never `same-pr`" in prompt
    assert '"kind": "plan_revision"' in prompt
    assert '"plan_steps"' in prompt
    assert "normalize structured plan revisions into canonical\nmarkdown for stored plan state" in prompt
    assert "Use this mandatory structured JSON response format" in prompt
    assert "fall back to markdown" not in prompt.lower()


def test_render_canonical_plan_steps_numbers_items():
    assert render_canonical_plan_steps(("Update protocol.py.", "Add tests.")) == (
        "1. Update protocol.py.\n2. Add tests."
    )


def test_render_canonical_plan_revision_and_public_comment():
    parsed = validate_structured_plan_revision(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_revision",
                "state": "blocking",
                "summary": "Revised the plan to cover rollback behavior.",
                "prior_plan_item_dispositions": [
                    {"item_id": "item-4", "disposition": "resolved", "note": "Added a resume-path step."}
                ],
                "plan_steps": ["Update protocol.py.", "Add orchestrator resume tests."],
            }
        )
        + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex"
    )
    assert parsed is not None
    prior_items = (
        UnresolvedReviewItem(
            item_id="item-4",
            reviewer="OpenAI Codex",
            source_round=2,
            text="Add a resume-path step.",
            status="blocking",
        ),
    )

    canonical = render_canonical_plan_revision(parsed, prior_items)
    public = _render_public_plan_revision_comment(
        parsed,
        prior_items=prior_items,
        raw_text='{"schema_version":1}\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex',
        signature="OpenAI Codex",
    )

    assert canonical == (
        "Revised the plan to cover rollback behavior.\n\n"
        "### Prior plan item dispositions\n"
        "- [item-4] Blocking issue from OpenAI Codex, round 2: Add a resume-path step. -> "
        "resolved: Added a resume-path step.\n\n"
        "### Plan steps\n"
        "1. Update protocol.py.\n"
        "2. Add orchestrator resume tests."
    )
    assert public == (
        "## Revised plan\n\n"
        + canonical
        + "\n\n<!-- AGENT_PLAN_STATE: blocking -->\n\n-- OpenAI Codex"
    )
    assert '"kind": "plan_revision"' not in public


def test_render_public_coder_followup_comment():
    parsed = validate_structured_coder_followup(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Added the requested regression test.",
                "addressed_items": ["item-1", "item-2"],
                "remaining_items": [],
                "addressed_item_notes": {
                    "item-1": "Added coverage for the parser.",
                    "item-2": "Updated the helper.",
                },
                "human_requirements": {
                    "addressed_ids": ["Requirement 1"],
                    "checked_discussion_directly": False,
                },
                "tests_run": [
                    "python -m pytest tests/test_agent_loop.py -k coder_followup"
                ],
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
    )
    assert parsed is not None
    prior_items = (
        UnresolvedReviewItem(
            item_id="item-1",
            reviewer="OpenAI Codex",
            source_round=1,
            text="Add a regression test before merge.",
            status="blocking",
        ),
        UnresolvedReviewItem(
            item_id="item-2",
            reviewer="Google Gemini",
            source_round=2,
            text="Rename the shared helper.",
            status="same-pr",
        ),
    )

    rendered = _render_public_coder_followup_comment(
        parsed,
        signature="Anthropic Claude",
        prior_items=prior_items,
    )

    assert rendered == (
        "## Coder follow-up\n\n"
        "Added the requested regression test.\n\n"
        "### Addressed items\n"
        "- item-1: Blocking issue from OpenAI Codex, round 1: Add a regression test before merge.\n"
        "  - Resolution: Added coverage for the parser.\n"
        "- item-2: Same-PR follow-up from Google Gemini, round 2: Rename the shared helper.\n"
        "  - Resolution: Updated the helper.\n\n"
        "### Remaining items\n"
        "- None.\n\n"
        "### Tests run\n"
        "- python -m pytest tests/test_agent_loop.py -k coder_followup\n\n"
        "<!-- AGENT_STATE: blocking -->\n\n"
        "-- Anthropic Claude"
    )
    assert "```json" not in rendered
    assert '"kind": "coder_followup"' not in rendered

    without_tests = validate_structured_coder_followup(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Still working through the review.",
                "addressed_items": [],
                "remaining_items": ["item-3"],
                "human_requirements": {
                    "addressed_ids": [],
                    "checked_discussion_directly": False,
                },
                "tests_run": [],
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
    )
    assert without_tests is not None
    rendered_without_tests = _render_public_coder_followup_comment(
        without_tests,
        signature="Anthropic Claude",
    )
    assert "### Tests run" not in rendered_without_tests
    assert "### Addressed items\n- None." in rendered_without_tests
    assert (
        "### Remaining items\n"
        "- item-3: Item context unavailable in current round metadata.\n"
        "  - Reason: No reason provided by coder."
    ) in rendered_without_tests


def test_render_public_coder_followup_comment_expands_carried_items_with_notes_and_placeholders():
    parsed = validate_structured_coder_followup(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Fixed the blocker and deferred the follow-up.",
                "addressed_items": ["item-1"],
                "remaining_items": ["item-2"],
                "addressed_item_notes": {"item-1": "Restored the missing validation branch."},
                "human_requirements": {
                    "addressed_ids": [],
                    "checked_discussion_directly": False,
                },
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
    )
    assert parsed is not None
    prior_items = (
        UnresolvedReviewItem(
            item_id="item-1",
            reviewer="OpenAI Codex",
            source_round=2,
            text="  - Preserve structured coder follow-up metadata.\n\nExtra context should be summarized.",
            status="blocking",
        ),
        UnresolvedReviewItem(
            item_id="item-2",
            reviewer="Google Gemini",
            source_round=3,
            text="Move the rendering helper into a shared module.",
            status="same-pr",
        ),
    )

    rendered = _render_public_coder_followup_comment(
        parsed,
        signature="Anthropic Claude",
        prior_items=prior_items,
    )

    assert (
        "- item-1: Blocking issue from OpenAI Codex, round 2: "
        "Preserve structured coder follow-up metadata."
    ) in rendered
    assert "  - Resolution: Restored the missing validation branch." in rendered
    assert (
        "- item-2: Same-PR follow-up from Google Gemini, round 3: "
        "Move the rendering helper into a shared module."
    ) in rendered
    assert "  - Reason: No reason provided by coder." in rendered


def test_render_public_coder_followup_comment_expands_pr_220_remaining_items():
    parsed = validate_structured_coder_followup(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Hardened markdown stripping; two follow-ups remain.",
                "addressed_items": ["item-3", "item-4"],
                "remaining_items": ["item-5", "item-6"],
                "remaining_item_notes": {
                    "item-5": "Deferred because URL canonicalization needs product confirmation.",
                    "item-6": "Deferred because the helper move should be isolated from this fix.",
                },
                "human_requirements": {
                    "addressed_ids": [],
                    "checked_discussion_directly": False,
                },
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
    )
    assert parsed is not None
    prior_items = (
        UnresolvedReviewItem(
            item_id="item-5",
            reviewer="Google Gemini",
            source_round=3,
            text=(
                "Update `server/static/index.html` and `server/static/landing.html` to use "
                "relative paths for `og:image` and `og:url` if possible."
            ),
            status="same-pr",
        ),
        UnresolvedReviewItem(
            item_id="item-6",
            reviewer="Google Gemini",
            source_round=3,
            text=(
                "Deduplicate `_strip_markdown` helper logic between `server/app.py` and "
                "`core/orchestrator.py` by moving it to `core/utils.py`."
            ),
            status="same-pr",
        ),
    )

    rendered = _render_public_coder_followup_comment(
        parsed,
        signature="Anthropic Claude",
        prior_items=prior_items,
    )

    assert "- item-5: Same-PR follow-up from Google Gemini, round 3:" in rendered
    assert "relative paths" in rendered
    assert "  - Reason: Deferred because URL canonicalization needs product confirmation." in rendered
    assert "- item-6: Same-PR follow-up from Google Gemini, round 3:" in rendered
    assert "Deduplicate `_strip_markdown` helper logic" in rendered
    assert "  - Reason: Deferred because the helper move should be isolated from this fix." in rendered
    assert "\n- item-5\n" not in rendered
    assert "\n- item-6\n" not in rendered


def test_render_public_plan_review_comment_normalizes_sections():
    parsed = parse_structured_plan_review(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_review",
                "state": "blocking",
                "summary": "Still blocked on coverage.",
                "blocking_plan_issues": ["Add a resume coverage test."],
                "same_plan_followups": ["Mention canonical hashing explicitly."],
                "future_followups": [],
                "prior_plan_item_dispositions": [
                    {"item_id": "item-2", "disposition": "same-plan", "note": "Still needs one more prompt assertion."}
                ],
            }
        )
        + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
        reviewer="OpenAI Codex",
    )
    assert parsed is not None
    prior_items = (
        UnresolvedReviewItem(
            item_id="item-2",
            reviewer="Google Gemini",
            source_round=1,
            text="Mention canonical hashing explicitly.",
            status="same-plan",
        ),
    )

    rendered = _render_public_plan_review_comment(
        parsed,
        reviewer="OpenAI Codex",
        prior_items=prior_items,
        dispositions=parsed.dispositions,
    )

    assert rendered == (
        "**Review verdict:** Blocking\n\n"
        "Still blocked on coverage.\n\n"
        "### Blocking plan issues\n"
        "- Add a resume coverage test.\n\n"
        "### Same-plan follow-ups\n"
        "- Mention canonical hashing explicitly.\n\n"
        "### Prior unresolved plan item dispositions\n"
        "- [item-2] Same-plan follow-up from Google Gemini, round 1: Mention canonical hashing explicitly. -> "
        "same-plan: Still needs one more prompt assertion.\n\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n"
        "-- OpenAI Codex"
    )


def test_review_freeform_summary_text_strips_structured_followup_sections():
    review = """**Review verdict:** blocking

Blocking issue summary.

### Blocking issues
- needs one more assertion

### Prior unresolved item dispositions
- [item-1] still blocking: needs one more assertion

### Human requirements
- Requirement 1: addressed in the latest patch

### Same-PR follow-ups
- Rename helper

### Future follow-ups
- Document cleanup later

<!-- AGENT_STATE: blocking -->
-- OpenAI Codex
"""

    assert _review_freeform_summary_text(review) == "Blocking issue summary."


def test_validate_review_response_accepts_structured_pr_review():
    review = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "blocking",
                "summary": "Need one more regression test before merge.",
                "blocking_items": ["Add the mixed-history regression case to the suite."],
                "same_pr_followups": [],
                "future_followups": [],
                "prior_item_dispositions": [],
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
    )

    parsed = _validate_review_response(review, reviewer="OpenAI Codex", unresolved_items=())

    assert parsed.summary == "Need one more regression test before merge."
    assert [item.text for item in parsed.blocking_items] == [
        "Add the mixed-history regression case to the suite."
    ]


def test_validate_coder_followup_response_accepts_structured_item_partition():
    unresolved_items = (
        UnresolvedReviewItem(
            item_id="item-1",
            reviewer="OpenAI Codex",
            source_round=1,
            text="Add a regression test.",
            status="blocking",
        ),
        UnresolvedReviewItem(
            item_id="item-2",
            reviewer="Anthropic Claude",
            source_round=1,
            text="Rename the helper.",
            status="same-pr",
        ),
        UnresolvedReviewItem(
            item_id=HUMAN_REQUIREMENTS_ACK_ITEM_ID,
            reviewer="Orchestrator",
            source_round=1,
            text="Ack missing.",
            status="blocking",
        ),
    )
    response = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Addressed the test, helper rename still pending.",
                "addressed_items": ["item-1"],
                "remaining_items": ["item-2"],
                "human_requirements": {
                    "addressed_ids": [],
                    "checked_discussion_directly": False,
                },
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
    )

    parsed = _validate_coder_followup_response(
        response,
        unresolved_items=unresolved_items,
        human_requirements=(),
    )

    assert parsed.addressed_items == ("item-1",)
    assert parsed.remaining_items == ("item-2",)


def test_validate_coder_followup_response_rejects_issue_acceptance_criteria_as_human_requirement():
    response = structured_coder_followup(
        state="blocking",
        addressed_items=["item-1"],
        remaining_items=[],
        human_requirement_ids=["Issue #221 acceptance criteria"],
        reviewer="OpenAI Codex",
    )

    with pytest.raises(AgentLoopError, match="issue acceptance criteria.*not signed human requirements"):
        _validate_coder_followup_response(
            response,
            unresolved_items=(
                UnresolvedReviewItem(
                    item_id="item-1",
                    reviewer="Anthropic Claude",
                    source_round=1,
                    text="Add a regression test.",
                    status="blocking",
                ),
            ),
            human_requirements=(),
        )


def test_validate_coder_followup_response_rejects_requirement_label_when_none_surfaced():
    response = structured_coder_followup(
        state="blocking",
        addressed_items=["item-1"],
        remaining_items=[],
        human_requirement_ids=["Requirement 1"],
        reviewer="OpenAI Codex",
    )

    with pytest.raises(AgentLoopError, match="no signed human requirements were surfaced"):
        _validate_coder_followup_response(
            response,
            unresolved_items=(
                UnresolvedReviewItem(
                    item_id="item-1",
                    reviewer="Anthropic Claude",
                    source_round=1,
                    text="Add a regression test.",
                    status="blocking",
                ),
            ),
            human_requirements=(),
        )


def test_validate_coder_followup_response_accepts_surfaced_requirement_label():
    response = structured_coder_followup(
        state="blocking",
        addressed_items=["item-1"],
        remaining_items=[],
        human_requirement_ids=["Requirement 1"],
        reviewer="OpenAI Codex",
    )

    parsed = _validate_coder_followup_response(
        response,
        unresolved_items=(
            UnresolvedReviewItem(
                item_id="item-1",
                reviewer="Anthropic Claude",
                source_round=1,
                text="Add a regression test.",
                status="blocking",
            ),
        ),
        human_requirements=(
            HumanReviewRequirement(
                source_type="PR comment",
                author="maintainer",
                created_at="2026-06-02T12:00:00Z",
                url="https://github.com/OWNER/REPO/pull/1#issuecomment-1",
                body="Add coverage for the rejected label case.",
            ),
        ),
    )

    assert parsed.human_requirements.addressed_ids == ("Requirement 1",)


def test_validate_coder_followup_response_rejects_mixed_valid_and_invalid_requirement_labels():
    response = structured_coder_followup(
        state="blocking",
        addressed_items=["item-1"],
        remaining_items=[],
        human_requirement_ids=["Requirement 1", "Issue #221 acceptance criteria"],
        reviewer="OpenAI Codex",
    )

    with pytest.raises(AgentLoopError, match="issue acceptance criteria.*not signed human requirements"):
        _validate_coder_followup_response(
            response,
            unresolved_items=(
                UnresolvedReviewItem(
                    item_id="item-1",
                    reviewer="Anthropic Claude",
                    source_round=1,
                    text="Add a regression test.",
                    status="blocking",
                ),
            ),
            human_requirements=(
                HumanReviewRequirement(
                    source_type="PR comment",
                    author="maintainer",
                    created_at="2026-06-02T12:00:00Z",
                    url="https://github.com/OWNER/REPO/pull/1#issuecomment-1",
                    body="Add coverage for the rejected label case.",
                ),
            ),
        )


@pytest.mark.parametrize(
    ("addressed_items", "remaining_items", "message"),
    [
        (["item-1"], ["item-1"], "listed unresolved reviewer item IDs more than once"),
        (["item-9"], [], "referenced unknown unresolved reviewer item IDs"),
        (["item-1"], [], "did not classify all unresolved reviewer items"),
    ],
)
def test_validate_coder_followup_response_rejects_invalid_structured_item_partition(
    addressed_items,
    remaining_items,
    message,
):
    unresolved_items = (
        UnresolvedReviewItem(
            item_id="item-1",
            reviewer="OpenAI Codex",
            source_round=1,
            text="Add a regression test.",
            status="blocking",
        ),
        UnresolvedReviewItem(
            item_id="item-2",
            reviewer="Anthropic Claude",
            source_round=1,
            text="Rename the helper.",
            status="same-pr",
        ),
    )
    response = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Status update.",
                "addressed_items": addressed_items,
                "remaining_items": remaining_items,
                "human_requirements": {
                    "addressed_ids": [],
                    "checked_discussion_directly": False,
                },
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
    )

    with pytest.raises(AgentLoopError, match=message):
        _validate_coder_followup_response(
            response,
            unresolved_items=unresolved_items,
            human_requirements=(),
        )


def test_validate_coder_followup_response_rejects_marker_only_markdown():
    with pytest.raises(AgentLoopError, match="Coder response did not use the required structured format"):
        _validate_coder_followup_response(
            "Updated the PR.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            unresolved_items=(),
            human_requirements=(),
        )


def test_validate_coder_followup_response_requires_regular_synthetic_human_requirement_item():
    unresolved_items = (
        UnresolvedReviewItem(
            item_id="item-8",
            reviewer="Orchestrator",
            source_round=4,
            text="Reviewers approved without acknowledging signed human requirements.",
            status="blocking",
        ),
        UnresolvedReviewItem(
            item_id=HUMAN_REQUIREMENTS_ACK_ITEM_ID,
            reviewer="Orchestrator",
            source_round=4,
            text="Internal human requirements acknowledgement pseudo-item.",
            status="blocking",
        ),
    )
    response = structured_coder_followup(
        state="approved",
        addressed_items=[],
        remaining_items=[],
        reviewer="Anthropic Claude",
    )

    with pytest.raises(AgentLoopError, match="item-8"):
        _validate_coder_followup_response(
            response,
            unresolved_items=unresolved_items,
            human_requirements=(),
        )


def test_render_public_pr_review_comment_uses_normalized_sections_and_footer():
    parsed = parse_review(
        (
            "Need one more regression test."
            + blocking_issues("Exercise the structured-resume path.")
            + "\n\n### Same-PR follow-ups\n- Rename the helper for clarity."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- HUMAN_REQUIREMENTS_RESOLVED -->\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
        ),
        reviewer="OpenAI Codex",
    )
    prior_items = (
        UnresolvedReviewItem(
            item_id="item-1",
            reviewer="Anthropic Claude",
            source_round=1,
            text="Add a regression test before merge.",
            status="blocking",
        ),
    )

    rendered = _render_public_pr_review_comment(
        parsed,
        reviewer="Codex",
        human_requirements_resolved_flag=True,
        prior_items=prior_items,
        dispositions=parsed.dispositions,
    )

    assert rendered == (
        "**Review verdict:** Blocking\n\n"
        "Need one more regression test.\n\n"
        "### Blocking issues\n"
        "- Exercise the structured-resume path.\n\n"
        "### Same-PR follow-ups\n"
        "- Rename the helper for clarity.\n\n"
        "### Prior unresolved item dispositions\n"
        "- [item-1] Blocking issue from Anthropic Claude, round 1: Add a regression test before merge. -> resolved\n\n"
        "<!-- HUMAN_REQUIREMENTS_RESOLVED -->\n"
        "<!-- AGENT_STATE: blocking -->\n"
        "-- OpenAI Codex"
    )


def test_render_public_pr_review_comment_normalizes_markdown_and_structured_reviews_the_same():
    markdown_review = (
        "Need one more regression test."
        + blocking_issues("Exercise the structured-resume path.")
        + "\n\n### Same-PR follow-ups\n- Rename the helper for clarity."
        + prior_item_dispositions("[item-1] resolved")
        + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
    )
    structured_review = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "blocking",
                "summary": "Need one more regression test.",
                "blocking_items": ["Exercise the structured-resume path."],
                "same_pr_followups": ["Rename the helper for clarity."],
                "future_followups": [],
                "prior_item_dispositions": [{"item_id": "item-1", "disposition": "resolved"}],
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
    )
    prior_items = (
        UnresolvedReviewItem(
            item_id="item-1",
            reviewer="Anthropic Claude",
            source_round=1,
            text="Add a regression test before merge.",
            status="blocking",
        ),
    )

    markdown_rendered = _render_public_pr_review_comment(
        parse_review(markdown_review, reviewer="OpenAI Codex"),
        reviewer="Codex",
        human_requirements_resolved_flag=False,
        prior_items=prior_items,
        dispositions=parse_review(markdown_review, reviewer="OpenAI Codex").dispositions,
    )
    structured_parsed = parse_pr_review(structured_review, reviewer="OpenAI Codex")
    structured_rendered = _render_public_pr_review_comment(
        structured_parsed,
        reviewer="Codex",
        human_requirements_resolved_flag=False,
        prior_items=prior_items,
        dispositions=structured_parsed.dispositions,
    )

    assert markdown_rendered == structured_rendered


def test_render_public_pr_review_comment_includes_visible_approved_verdict():
    rendered = _render_public_pr_review_comment(
        parse_review(
            "Looks good to me.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
            reviewer="OpenAI Codex",
        ),
        reviewer="Codex",
        human_requirements_resolved_flag=False,
        prior_items=(),
        dispositions=(),
    )

    assert rendered == (
        "**Review verdict:** Approved\n\n"
        "Looks good to me.\n\n"
        "<!-- AGENT_STATE: approved -->\n"
        "-- OpenAI Codex"
    )


def test_format_unresolved_item_label_normalizes_multiline_text_and_preserves_origin_status():
    item = UnresolvedReviewItem(
        item_id="item-7",
        reviewer="Google Gemini",
        source_round=1,
        text="  - require source issue reference in PR body  \n\nUpdate from Anthropic Claude: keep the wording compact",
        status="resolved",
        source_status="same-pr",
    )

    assert _format_unresolved_item_label(item) == (
        "Same-PR follow-up from Google Gemini, round 1: require source issue reference in PR body"
    )


def test_format_unresolved_item_label_truncates_at_fixed_limit():
    summary = "a" * (ITEM_SUMMARY_LIMIT + 20)
    item = UnresolvedReviewItem(
        item_id="item-8",
        reviewer="OpenAI Codex",
        source_round=2,
        text=summary,
        status="blocking",
    )

    label = _format_unresolved_item_label(item)

    assert label.startswith("Blocking issue from OpenAI Codex, round 2: ")
    assert label.endswith("...")
    rendered_summary = label.split(": ", 1)[1]
    assert len(rendered_summary) == ITEM_SUMMARY_LIMIT


def test_format_unresolved_item_label_special_cases_human_requirements_ack_item():
    item = UnresolvedReviewItem(
        item_id=HUMAN_REQUIREMENTS_ACK_ITEM_ID,
        reviewer="Orchestrator",
        source_round=3,
        text="Coder response missing required `### Human requirements` section.",
        status="blocking",
    )

    assert _format_unresolved_item_label(item) == (
        "Human-requirements acknowledgement item, round 3: "
        "Coder response missing required `### Human requirements` section."
    )


def test_render_public_review_comment_replaces_dispositions_without_exposing_same_round_new_items():
    body = """Still blocked.

### Same-PR follow-ups
- Keep the source issue reference in the PR body.

### Prior unresolved item dispositions
- [item-1] same-pr

<!-- AGENT_STATE: blocking -->
-- OpenAI Codex
"""
    prior_items = (
        UnresolvedReviewItem(
            item_id="item-1",
            reviewer="Google Gemini",
            source_round=1,
            text="Require source issue reference in PR body.\n\nUpdate from Anthropic Claude: keep the note compact",
            status="same-pr",
        ),
    )
    dispositions = parse_unresolved_item_dispositions(
        prior_item_dispositions("[item-1] Same-PR follow-up from Google Gemini, round 1: ignored by parser -> same-pr: keep the body reference"),
        reviewer="OpenAI Codex",
    )
    new_items = (
        UnresolvedReviewItem(
            item_id="item-2",
            reviewer="OpenAI Codex",
            source_round=2,
            text="Keep the source issue reference in the PR body.",
            status="same-pr",
        ),
    )

    rendered = _render_public_review_comment(
        body,
        review_kind="pr",
        prior_items=prior_items,
        dispositions=dispositions,
        new_items=new_items,
    )

    assert "### Same-PR follow-ups\n- Keep the source issue reference in the PR body." in rendered
    assert (
        "### Prior unresolved item dispositions\n"
        "- [item-1] Same-PR follow-up from Google Gemini, round 1: Require source issue reference in PR body. -> same-pr: keep the body reference"
    ) in rendered
    assert "### New tracked unresolved items" not in rendered
    assert "[item-2]" not in rendered
    assert rendered.rstrip().endswith("-- OpenAI Codex")


def test_render_public_review_comment_preserves_unknown_disposition_values():
    body = """Still blocked.

### Prior unresolved item dispositions
- [item-1] same-pr

<!-- AGENT_STATE: blocking -->
-- OpenAI Codex
"""
    prior_items = (
        UnresolvedReviewItem(
            item_id="item-1",
            reviewer="Google Gemini",
            source_round=1,
            text="Keep the parser and renderer aligned when new dispositions are added.",
            status="same-pr",
        ),
    )
    dispositions = (
        ReviewItemDisposition(
            item_id="item-1",
            reviewer="OpenAI Codex",
            disposition="deferred",
            note="tracked for a later parser update",
        ),
    )

    rendered = _render_public_review_comment(
        body,
        review_kind="pr",
        prior_items=prior_items,
        dispositions=dispositions,
        new_items=(),
    )

    assert (
        "### Prior unresolved item dispositions\n"
        "- [item-1] Same-PR follow-up from Google Gemini, round 1: "
        "Keep the parser and renderer aligned when new dispositions are added. "
        "-> deferred: tracked for a later parser update"
    ) in rendered


def test_apply_unresolved_item_dispositions_appends_disposition_notes_to_text():
    unresolved_items = (
        UnresolvedReviewItem(
            item_id="item-1",
            reviewer="OpenAI Codex",
            source_round=1,
            text="Needs regression coverage before merge.",
            status="blocking",
        ),
    )
    dispositions_by_item = {
        "item-1": [
            parse_unresolved_item_dispositions(
                prior_item_dispositions("[item-1] still blocking: include API error path too"),
                reviewer="Anthropic Claude",
            )[0]
        ]
    }

    updated_items, future_items = _apply_unresolved_item_dispositions(
        unresolved_items, dispositions_by_item
    )

    assert len(updated_items) == 1
    assert future_items == []
    assert updated_items[0].text == (
        "Needs regression coverage before merge.\n\n"
        "Update from Anthropic Claude: include API error path too"
    )
    assert updated_items[0].notes == ("Anthropic Claude: include API error path too",)


@pytest.mark.parametrize("terminator", ["<!-- AGENT_STATE: approved -->", "-- OpenAI Codex"])
def test_parse_non_blocking_followups_stops_at_final_markers(terminator):
    review = f"""
    Looks good.

    ### Non-blocking follow-ups
    - Add cleanup docs.
    {terminator}
    - This is outside the follow-up section.
    """

    followups = parse_non_blocking_followups(review, reviewer="OpenAI Codex")

    assert [(item.reviewer, item.text) for item in followups] == [
        ("OpenAI Codex", "Add cleanup docs."),
    ]


def test_parse_non_blocking_followups_returns_empty_without_section():
    review = "LGTM.\n- A normal bullet outside the section.\n<!-- AGENT_STATE: approved -->"

    assert parse_non_blocking_followups(review, reviewer="OpenAI Codex") == []


def test_parse_pr_number_accepts_marker_and_url():
    assert parse_pr_number("opened\n<!-- AGENT_PR: 61 -->") == 61
    assert parse_pr_number("https://github.com/OWNER/REPO/pull/62") == 62
    assert parse_pr_number("no pr here") is None


def test_issue_loop_creates_pr_then_alternates_until_codex_approval(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Created PR.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->",
            "Fixed review.\n<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=[
            "Finding: bug remains.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    command_names = [cmd[:2] for cmd, _cwd in runner.commands]
    assert ["claude", "--print"] in command_names
    assert ["codex", "exec"] in command_names
    assert len(runner.comments) == 4
    assert runner.comments[-1].startswith("**Review verdict:** Approved\n\nLGTM.")
    assert list((tmp_path / "logs").glob("*-claude.log"))
    assert list((tmp_path / "logs").glob("*-codex.log"))
    assert (tmp_path / "logs" / ".gitignore").read_text(encoding="utf-8") == "*\n!.gitignore\n"


def test_issue_loop_syncs_coder_base_after_memory_before_coder(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Created PR.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)
    config.agent_memory_dir.mkdir(parents=True)
    (config.agent_memory_dir / "last-analyzed-commit").write_text("base123\n", encoding="utf-8")

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    commands = runner.commands
    issue_context_index = command_index(commands, ["gh", "issue", "view"])
    memory_index = command_index(commands, ["git", "diff", "--name-only"])
    fetch_index = command_index(commands, ["git", "fetch", "origin"])
    switch_index = command_index(commands, ["git", "switch", "main"])
    pull_index = command_index(commands, ["git", "pull", "--ff-only", "origin", "main"])
    coder_index = command_index(commands, ["claude", "--print"])

    assert issue_context_index < memory_index < fetch_index < switch_index < pull_index < coder_index


def test_issue_loop_includes_issue_comments_in_coder_and_review_prompts(tmp_path):
    runner = FakeRunner(
        issue_payload={
            "number": 56,
            "state": "open",
            "is_pr": False,
            "url": "https://github.com/OWNER/REPO/issues/56",
            "title": "Support issue comments",
            "body": "Original request.",
        },
        issue_comments=[
            {
                "author": {"login": "second-user"},
                "createdAt": "2026-05-17T10:00:00Z",
                "body": "Later comment should come second.",
            },
            {
                "author": {"login": "first-user"},
                "createdAt": "2026-05-17T09:00:00Z",
                "body": "Earlier comment refines the request.",
            },
        ],
        claude_outputs=[
            "Created PR.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    claude_prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["claude"])
    codex_prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    for prompt in (claude_prompt, codex_prompt):
        assert "Issue context from GitHub" in prompt
        assert "Later comments may refine or supersede the original issue body" in prompt
        assert "GitHub issue #56" in prompt
        assert "Title:\nSupport issue comments" in prompt
        assert "Body:\nOriginal request." in prompt
        assert "Comments, oldest to newest:" in prompt
        assert prompt.index("Comment by first-user at 2026-05-17T09:00:00Z") < prompt.index(
            "Comment by second-user at 2026-05-17T10:00:00Z"
        )
        assert "Earlier comment refines the request." in prompt
        assert "Later comment should come second." in prompt
    assert "include `Fixes #56` or another direct reference to issue #56" in claude_prompt


def test_format_issue_context_truncates_oversized_newest_comment():
    issue_context = IssueContext(
        number=56,
        repo="OWNER/REPO",
        title="Support issue comments",
        body="Original request.",
        url="https://github.com/OWNER/REPO/issues/56",
        comments=(
            IssueComment(
                author="first-user",
                created_at="2026-05-17T09:00:00Z",
                body="Older detail should not be kept instead of the newest comment.",
            ),
            IssueComment(
                author="second-user",
                created_at="2026-05-17T10:00:00Z",
                body="Newest detail. " + ("x" * 1000),
            ),
        ),
    )

    text = format_issue_context(issue_context, max_chars=700)

    assert len(text) <= 700
    assert "Older comments omitted: 1 comment(s)" in text
    assert "Comment by second-user at 2026-05-17T10:00:00Z" in text
    assert "Newest detail." in text
    assert "[Newest comment truncated to keep this prompt bounded.]" in text
    assert "Older detail should not be kept instead of the newest comment." not in text


def test_get_issue_context_parses_signed_issue_body_and_comments(tmp_path):
    runner = FakeRunner(
        issue_payload={
            "number": 56,
            "title": "Support signed issue requirements",
            "body": "Use the stable API path.\n\n-- Human Reviewer",
            "url": "https://github.com/OWNER/REPO/issues/56",
            "author": {"login": "issue-author"},
            "createdAt": "2026-05-17T08:00:00Z",
        },
        issue_comments=[
            {
                "author": {"login": "maintainer"},
                "createdAt": "2026-05-17T09:00:00Z",
                "url": "https://github.com/OWNER/REPO/issues/56#issuecomment-1",
                "body": "Unsigned discussion remains normal context.",
            },
            {
                "author": {"login": "lead"},
                "createdAt": "2026-05-17T10:00:00Z",
                "url": "https://github.com/OWNER/REPO/issues/56#issuecomment-2",
                "body": "Add a regression test.\n\n-- Human Reviewer",
            },
        ],
    )
    config = make_config(tmp_path)

    issue_context = get_issue_context(runner, config=config, issue_number=56)

    assert [item.source_type for item in issue_context.human_requirements] == [
        "Issue body",
        "Issue comment",
    ]
    assert [item.author for item in issue_context.human_requirements] == ["issue-author", "lead"]
    assert [item.created_at for item in issue_context.human_requirements] == [
        "2026-05-17T08:00:00Z",
        "2026-05-17T10:00:00Z",
    ]
    assert issue_context.human_requirements[0].body == "Use the stable API path."
    assert issue_context.human_requirements[1].body == "Add a regression test."
    assert issue_context.comments[0].body == "Unsigned discussion remains normal context."


def test_format_human_requirements_uses_distinct_high_priority_section():
    text = format_human_requirements(
        (
            HumanReviewRequirement(
                source_type="PR comment",
                author="reviewer",
                created_at="2026-05-18T10:00:00Z",
                url="https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                body="Please use the absolute URL.",
            ),
        )
    )

    assert text.startswith("Signed Human Reviewer Requirements")
    assert "high-priority PR requirements" in text
    assert "latest human instruction wins" in text
    assert "- Source: PR comment" in text
    assert "- Author: reviewer" in text
    assert "Please use the absolute URL." in text


def test_format_human_requirements_supports_issue_specific_wording_and_fallback():
    text = format_human_requirements(
        (
            HumanReviewRequirement(
                source_type="Issue body",
                author="maintainer",
                created_at="2026-05-18T10:00:00Z",
                url="https://github.com/OWNER/REPO/issues/56",
                body="Keep the current CLI flag.",
            ),
        ),
        max_chars=120,
        requirement_scope="planning requirements",
        full_omission_fallback="Fetch the issue discussion directly before finalizing the plan.",
    )

    assert "high-priority planning requirements" in text
    assert "Fetch the issue discussion directly before finalizing the plan." in text


def test_format_human_requirements_preserves_entry_spacing_when_truncated():
    requirements = (
        HumanReviewRequirement(
            source_type="PR comment",
            author="reviewer",
            created_at="2026-05-18T10:00:00Z",
            url="https://github.com/OWNER/REPO/pull/77#issuecomment-1",
            body="Oldest requirement.",
        ),
        HumanReviewRequirement(
            source_type="PR comment",
            author="reviewer",
            created_at="2026-05-18T11:00:00Z",
            url="https://github.com/OWNER/REPO/pull/77#issuecomment-2",
            body="Middle requirement.",
        ),
        HumanReviewRequirement(
            source_type="PR comment",
            author="reviewer",
            created_at="2026-05-18T12:00:00Z",
            url="https://github.com/OWNER/REPO/pull/77#issuecomment-3",
            body="Newest requirement.",
        ),
    )
    full_text = format_human_requirements(requirements)

    text = format_human_requirements(requirements, max_chars=len(full_text) - 1)

    assert "Older signed human requirement(s) omitted: 1." in text
    assert "Oldest requirement." not in text
    assert "Middle requirement.\n\nRequirement 3:" in text
    assert "Newest requirement." in text


def test_render_coder_human_requirements_prompt_context_tracks_surfaced_ids_after_truncation():
    requirements = (
        HumanReviewRequirement(
            source_type="PR comment",
            author="reviewer",
            created_at="2026-05-18T10:00:00Z",
            url="https://github.com/OWNER/REPO/pull/77#issuecomment-1",
            body="Oldest requirement.",
        ),
        HumanReviewRequirement(
            source_type="PR comment",
            author="reviewer",
            created_at="2026-05-18T11:00:00Z",
            url="https://github.com/OWNER/REPO/pull/77#issuecomment-2",
            body="Middle requirement.",
        ),
        HumanReviewRequirement(
            source_type="PR comment",
            author="reviewer",
            created_at="2026-05-18T12:00:00Z",
            url="https://github.com/OWNER/REPO/pull/77#issuecomment-3",
            body="Newest requirement.",
        ),
    )
    full_text = format_human_requirements(requirements)

    context = render_coder_human_requirements_prompt_context(
        requirements,
        max_chars=len(full_text) - 1,
    )

    assert context.block.endswith("\n")
    assert "Older signed human requirement(s) omitted: 1." in context.block
    assert context.surfaced_requirement_ids == ("Requirement 2", "Requirement 3")
    assert context.requires_direct_discussion_ack is False


def test_render_coder_human_requirements_prompt_context_handles_full_omission_fallback():
    context = render_coder_human_requirements_prompt_context(
        (
            HumanReviewRequirement(
                source_type="PR comment",
                author="reviewer",
                created_at="2026-05-18T10:00:00Z",
                url="https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                body="Please use the absolute URL.",
            ),
        ),
        max_chars=120,
    )

    assert "All 1 signed human requirement(s) were omitted" in context.block
    assert context.surfaced_requirement_ids == ()
    assert context.requires_direct_discussion_ack is True


@pytest.mark.parametrize(
    ("builder_name", "expected_scope", "expected_guidance"),
    [
        ("issue", "high-priority implementation requirements", "how you addressed that item"),
        ("issue_plan", "high-priority planning requirements", "how the plan covers that item"),
        ("plan_revision", "high-priority planning requirements", "how the revised plan covers that item"),
        (
            "issue_implementation",
            "high-priority implementation requirements",
            "how you addressed that item",
        ),
    ],
)
def test_issue_and_plan_prompts_surface_signed_human_requirements_before_issue_context(
    tmp_path,
    builder_name,
    expected_scope,
    expected_guidance,
):
    config = make_config(tmp_path, reviewer=("codex", "gemini"))
    issue_context = IssueContext(
        number=56,
        repo="OWNER/REPO",
        title="Support issue comments",
        body="Original request.",
        url="https://github.com/OWNER/REPO/issues/56",
        comments=(
            IssueComment(
                author="commenter",
                created_at="2026-05-17T10:00:00Z",
                body="General issue context.",
            ),
        ),
        human_requirements=(
            HumanReviewRequirement(
                source_type="Issue comment",
                author="maintainer",
                created_at="2026-05-17T11:00:00Z",
                url="https://github.com/OWNER/REPO/issues/56#issuecomment-1",
                body="Preserve backward compatibility.",
            ),
        ),
    )
    if builder_name == "issue":
        prompt = build_issue_prompt(56, config, issue_context=issue_context)
    elif builder_name == "issue_plan":
        prompt = build_issue_plan_prompt(56, config, issue_context=issue_context)
    elif builder_name == "plan_revision":
        prompt = build_plan_revision_prompt(
            56,
            2,
            "Old plan.",
            "Blocking review.",
            config,
            issue_context=issue_context,
        )
    else:
        prompt = build_issue_implementation_prompt(
            56,
            "Approved plan.",
            config,
            issue_context=issue_context,
        )

    assert "Signed Human Reviewer Requirements" in prompt
    assert expected_scope in prompt
    assert expected_guidance in prompt
    assert prompt.index("Signed Human Reviewer Requirements") < prompt.index("Issue context from GitHub")


def test_followup_prompt_with_no_human_requirements_guides_empty_addressed_ids(tmp_path):
    config = make_config(tmp_path, reviewer=("codex", "gemini"))
    prompt = build_followup_prompt(
        222,
        2,
        "item-1: Add a regression test.",
        config,
        human_requirements=(),
    )

    assert '"human_requirements": {' in prompt
    assert '"addressed_ids": []' in prompt
    assert '"addressed_ids": ["Requirement 1"]' not in prompt
    assert "No signed human requirements are surfaced in this prompt" in prompt
    assert "issue acceptance criteria" in prompt
    assert "reviewer item IDs" in prompt


def test_plan_review_prompt_surfaces_signed_issue_requirements_as_approval_critical(tmp_path):
    config = make_config(tmp_path, reviewer=("codex", "gemini"))
    issue_context = IssueContext(
        number=56,
        repo="OWNER/REPO",
        title="Support issue comments",
        body="Original request.",
        url="https://github.com/OWNER/REPO/issues/56",
        comments=(),
        human_requirements=(
            HumanReviewRequirement(
                source_type="Issue body",
                author="maintainer",
                created_at="2026-05-17T08:00:00Z",
                url="https://github.com/OWNER/REPO/issues/56",
                body="Keep the public API unchanged.",
            ),
        ),
    )

    prompt = build_plan_review_prompt(
        56,
        1,
        "Plan:\n- Update the parser.",
        config,
        reviewer="codex",
        issue_context=issue_context,
    )

    assert "Signed Human Reviewer Requirements" in prompt
    assert "high-priority planning requirements" in prompt
    assert "approval-critical issue constraints" in prompt
    assert "Verify each requirement in this set before approving." in prompt
    assert prompt.index("Signed Human Reviewer Requirements") < prompt.index("Issue context from GitHub")


@pytest.mark.parametrize("builder", [build_followup_prompt, build_same_pr_followup_prompt])
def test_coder_followup_prompts_require_human_requirements_acknowledgement_only_when_present(
    tmp_path, builder
):
    config = make_config(tmp_path)
    with_requirements = builder(
        77,
        2,
        "Fix the bug.",
        config,
        human_requirements=(
            HumanReviewRequirement(
                source_type="PR comment",
                author="reviewer",
                created_at="2026-05-18T10:00:00Z",
                url="https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                body="Please use the absolute URL.",
            ),
        ),
    )
    without_requirements = builder(77, 2, "Fix the bug.", config)

    assert "mandatory next-revision requirements" in with_requirements
    assert HUMAN_REQUIREMENTS_ADDRESSED_MARKER in with_requirements
    assert "### Human requirements" in with_requirements
    assert "`Requirement 1`" in with_requirements
    assert "mandatory next-revision requirements" not in without_requirements
    assert "`Requirement 1`" not in without_requirements


@pytest.mark.parametrize("builder", [build_followup_prompt, build_same_pr_followup_prompt])
def test_coder_followup_prompts_accept_precomputed_human_requirements_context(tmp_path, builder):
    config = make_config(tmp_path)
    requirements = (
        HumanReviewRequirement(
            source_type="PR comment",
            author="reviewer",
            created_at="2026-05-18T10:00:00Z",
            url="https://github.com/OWNER/REPO/pull/77#issuecomment-1",
            body="Please use the absolute URL.",
        ),
    )
    context = render_coder_human_requirements_prompt_context(requirements)

    prompt = builder(
        77,
        2,
        "Fix the bug.",
        config,
        human_requirements=requirements,
        human_requirements_context=context,
    )

    assert context.block in prompt
    assert HUMAN_REQUIREMENTS_ADDRESSED_MARKER in prompt
    assert "`Requirement 1`" in prompt


@pytest.mark.parametrize("builder", [build_followup_prompt, build_same_pr_followup_prompt])
def test_coder_followup_prompts_require_structured_json(tmp_path, builder):
    config = make_config(tmp_path)

    prompt = builder(77, 2, "Fix the bug.", config)

    assert '"kind": "coder_followup"' in prompt
    assert '"addressed_items": ["item-1"]' in prompt
    assert '"remaining_items": ["item-2"]' in prompt
    assert '"human_requirements": {' in prompt
    assert "The JSON `state` must match the `AGENT_STATE` footer exactly." in prompt
    assert "Use this mandatory structured JSON follow-up format" in prompt
    assert "compatibility fallback" not in prompt
    assert "Legacy markdown replies" not in prompt


def test_validate_human_requirements_acknowledgement_accepts_multiple_bullet_styles():
    response = f"""Implemented the fix.
{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}
### Human requirements
1. Requirement 1: updated the URL handling.
* Requirement 2: could not satisfy safely without widening scope, so I documented the limit.
<!-- AGENT_STATE: blocking -->
"""

    validate_human_requirements_acknowledgement(
        response,
        surfaced_requirement_ids=("Requirement 1", "Requirement 2"),
        requires_direct_discussion_ack=False,
    )

    parsed = parse_human_requirements_acknowledgement(response)
    assert parsed.addressed_ids == ("Requirement 1", "Requirement 2")


@pytest.mark.parametrize(
    ("response", "surfaced_ids", "requires_direct_discussion_ack", "message"),
    [
        (
            "Implemented.\n### Human requirements\n- Requirement 1: handled.\n<!-- AGENT_STATE: blocking -->",
            ("Requirement 1",),
            False,
            "missing required signed human requirements marker",
        ),
        (
            f"Implemented.\n{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n<!-- AGENT_STATE: blocking -->",
            ("Requirement 1",),
            False,
            "missing required `### Human requirements` section",
        ),
        (
            f"Implemented.\n{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n### Human requirements\n- Requirement 1: handled.\n<!-- AGENT_STATE: blocking -->",
            ("Requirement 1", "Requirement 2"),
            False,
            "did not address all surfaced signed human requirement IDs",
        ),
        (
            f"Implemented.\n{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n### Human requirements\n- Requirement 1: handled.\n- Requirement 1: repeated.\n<!-- AGENT_STATE: blocking -->",
            ("Requirement 1",),
            False,
            "listed signed human requirement IDs more than once",
        ),
        (
            f"Implemented.\n{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n### Human requirements\n- Requirement 99: handled.\n<!-- AGENT_STATE: blocking -->",
            ("Requirement 1",),
            False,
            "referenced unknown signed human requirement IDs",
        ),
        (
            f"Implemented.\n{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n### Human requirements\n- Prompt omitted details.\n<!-- AGENT_STATE: blocking -->",
            (),
            True,
            "must acknowledge that the prompt omitted the detailed signed human requirements",
        ),
    ],
)
def test_validate_human_requirements_acknowledgement_rejects_structural_failures(
    response,
    surfaced_ids,
    requires_direct_discussion_ack,
    message,
):
    with pytest.raises(AgentLoopError, match=message):
        validate_human_requirements_acknowledgement(
            response,
            surfaced_requirement_ids=surfaced_ids,
            requires_direct_discussion_ack=requires_direct_discussion_ack,
        )


def test_validate_human_requirements_acknowledgement_accepts_full_truncation_fallback():
    response = f"""Implemented the fix.
{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}
### Human requirements
- The prompt omitted the detailed signed human requirements, so I {HUMAN_REQUIREMENTS_DIRECT_DISCUSSION_ACK}.
<!-- AGENT_STATE: blocking -->
"""

    validate_human_requirements_acknowledgement(
        response,
        surfaced_requirement_ids=(),
        requires_direct_discussion_ack=True,
    )


def test_issue_loop_can_use_codex_as_coder_and_claude_as_reviewer(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Created PR.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->",
            "Fixed review.\n<!-- AGENT_STATE: blocking -->",
        ],
        claude_outputs=[
            "Finding: bug remains.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    agent_commands = [cmd[:2] for cmd, _cwd in runner.commands if cmd[:1] in (["claude"], ["codex"])]
    assert agent_commands == [
        ["codex", "exec"],
        ["claude", "--print"],
        ["codex", "exec"],
        ["claude", "--print"],
    ]
    assert len(runner.comments) == 4
    assert runner.comments[-1].startswith("**Review verdict:** Approved\n\nLGTM.")


def test_issue_loop_runs_pre_review_tests_after_coder_changes(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Created PR.\nTests: pytest passed.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->",
            "Fixed review.\nTests: pytest passed.\n<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=[
            "Finding: bug remains.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, test_command=("pytest", "tests/test_agent_loop.py"))

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    first_coder = command_index(runner.commands, ["claude", "--print"])
    first_test = commands.index(["pytest", "tests/test_agent_loop.py"])
    first_review = command_index(runner.commands, ["codex", "exec"])
    assert first_coder < first_test < first_review
    assert commands.count(["pytest", "tests/test_agent_loop.py"]) == 3


def test_pre_review_tests_can_be_disabled(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Created PR.\nTests: pytest passed.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    config = make_config(
        tmp_path,
        test_command=("pytest", "tests/test_agent_loop.py"),
        pre_review_tests=False,
    )

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    first_test = commands.index(["pytest", "tests/test_agent_loop.py"])
    first_review = command_index(runner.commands, ["codex", "exec"])
    assert first_review < first_test
    assert commands.count(["pytest", "tests/test_agent_loop.py"]) == 1


def test_codex_usage_summary_records_exact_tokens_from_jsonl_and_public_response(tmp_path):
    public_response = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    runner = FakeRunner(
        codex_outputs=[
            {
                "public_response": public_response,
                "stdout": "\n".join(
                    [
                        json.dumps({"type": "turn.started"}),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 200,
                                    "cached_input_tokens": 40,
                                    "output_tokens": 50,
                                    "reasoning_tokens": 10,
                                    "total_tokens": 300,
                                },
                            }
                        ),
                    ]
                ),
            }
        ]
    )
    config = make_config(tmp_path, reviewer="codex")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{public_response}"]
    summary = read_usage_summary(tmp_path / "logs")
    assert summary["totals"]["exact_calls"] == 1
    assert summary["totals"]["estimated_calls"] == 0
    assert summary["totals"]["input_tokens"] == 200
    assert summary["totals"]["cached_input_tokens"] == 40
    assert summary["totals"]["output_tokens"] == 50
    assert summary["totals"]["reasoning_tokens"] == 10
    assert summary["totals"]["total_tokens"] == 300
    assert summary["calls"][0]["raw_backend_usage"]["cached_input_tokens"] == 40
    assert summary["calls"][0]["validation_status"] == "validated"


def test_usage_summary_estimates_tokens_when_backend_exposes_none(tmp_path):
    public_response = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[public_response])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    summary = read_usage_summary(tmp_path / "logs")
    call = summary["calls"][0]
    assert call["usage"]["mode"] == "estimated"
    assert call["usage"]["input_tokens"] == max(1, (call["usage"]["input_bytes"] + 3) // 4)
    assert call["usage"]["output_tokens"] == max(1, (call["usage"]["output_bytes"] + 3) // 4)
    assert call["usage"]["output_chars"] > len(public_response)


def test_usage_summary_keeps_retry_attempts_and_marks_only_validated_call_successful(tmp_path):
    near_miss = "LGTM.\nAGENT_STATE: approved.\n-- Google Gemini"
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[near_miss, valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    summary = read_usage_summary(tmp_path / "logs")
    assert len(summary["calls"]) == 2
    assert summary["totals"]["call_count"] == 2
    assert summary["totals"]["success_count"] == 1
    assert summary["calls"][0]["validation_status"] == "invalid"
    assert summary["calls"][1]["validation_status"] == "validated"


def test_plan_first_issue_run_writes_one_summary_for_planning_implementation_and_review(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Implement usage logging.\n<!-- AGENT_PLAN_STATE: approved -->\n-- Anthropic Claude",
            "Opened PR.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Plan reviewed.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(
        runner,
        issue_number=56,
        config=config,
        plan_first=True,
        implement_after_approval=True,
    ) == 0

    summary = read_usage_summary(tmp_path / "logs")
    assert len(list((tmp_path / "logs").glob("*-usage-summary.json"))) == 1
    assert summary["totals"]["call_count"] == 4
    assert set(summary["per_agent"]) == {"claude", "codex"}
    assert [call["agent"] for call in summary["calls"]] == ["claude", "codex", "claude", "codex"]


def test_ensure_log_dir_ignored_does_not_overwrite_existing_file(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    gitignore = log_dir / ".gitignore"
    gitignore.write_text("custom\n", encoding="utf-8")

    ensure_log_dir_ignored(log_dir)

    assert gitignore.read_text(encoding="utf-8") == "custom\n"


def test_pr_loop_runs_tests_and_merge_only_after_codex_approval(tmp_path):
    runner = FakeRunner(codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"])
    config = make_config(
        tmp_path,
        auto_merge=True,
        test_command=("pytest", "tests/test_agent_loop.py"),
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    assert ["pytest", "tests/test_agent_loop.py"] in commands
    assert [
        "gh",
        "api",
        "repos/OWNER/REPO/commits/abc123/check-runs",
    ] in commands
    assert [
        "gh",
        "api",
        "repos/OWNER/REPO/commits/abc123/status",
    ] in commands
    assert [
        "gh",
        "api",
        "repos/OWNER/REPO/branches/main/protection/required_status_checks",
    ] in commands
    assert ["gh", "pr", "merge", "77", "--repo", "OWNER/REPO", "--merge"] in commands


def test_pr_loop_does_not_post_gemini_diagnostics_without_agent_state(tmp_path):
    diagnostic = "[ERROR] Invalid stream: The model returned an empty response or malformed tool call."
    runner = FakeRunner(gemini_outputs=[diagnostic, diagnostic, diagnostic])
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(AgentLoopError, match="No review result was recorded"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert runner.comments == []
    assert not any(diagnostic in comment for comment in runner.comments)
    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert sleep_commands == [["sleep", "1"], ["sleep", "1"]]


@pytest.mark.parametrize(
    "text",
    [
        "orchestrator.py lines 577-581: it currently falls back to parse_plan_state(text)",
        "orchestrator.py:577-581: it currently falls back to parse_plan_state(text)",
        "A bare 500 in diagnostic prose without HTTP context.",
    ],
)
def test_source_line_references_with_5xx_numbers_are_not_transient(text):
    assert not _is_transient_agent_output(text)
    assert _failure_category(text) == "deterministic"


@pytest.mark.parametrize(
    "text",
    [
        "Internal Server Error",
        "Bad Gateway",
        "Service Unavailable",
        "Gateway Timeout",
    ],
)
def test_explicit_server_error_phrases_remain_transient(text):
    assert _is_transient_agent_output(text)
    assert _failure_category(text) == "transient"


def test_plan_review_does_not_post_diagnostics_without_plan_state(tmp_path):
    diagnostic = "[ERROR] Invalid stream: The model returned an empty response or malformed tool call."
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Update the CLI.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        gemini_outputs=[diagnostic, diagnostic, diagnostic],
    )
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(AgentLoopError, match="AGENT_PLAN_STATE"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)

    assert len(runner.comments) == 1
    assert runner.comments[0].startswith("Plan:")
    assert not any(diagnostic in comment for comment in runner.comments)


def test_pr_loop_retries_transient_gemini_diagnostic_and_posts_only_valid_response(tmp_path):
    diagnostic = "[ERROR] Invalid stream: The model returned an empty response or malformed tool call."
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[diagnostic, valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{valid}"]
    assert diagnostic not in runner.comments[0]
    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert sleep_commands == [["sleep", "1"]]


@pytest.mark.parametrize("terminator", ["", "."])
def test_pr_loop_retries_plain_agent_state_near_miss_once(tmp_path, terminator):
    near_miss = f"LGTM.\nAGENT_STATE: approved{terminator}\n-- Google Gemini"
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[near_miss, valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{valid}"]
    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert sleep_commands == [["sleep", "1"]]


def test_plan_loop_retries_plain_agent_plan_state_near_miss_once(tmp_path):
    near_miss = "Plan looks sound.\nAGENT_PLAN_STATE: approved.\n-- Google Gemini"
    valid = "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Update the CLI.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        gemini_outputs=[near_miss, valid],
    )
    config = make_config(tmp_path, reviewer="gemini")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert near_miss not in runner.comments
    assert any(comment == f"**Review verdict:** Approved\n\n{valid}" for comment in runner.comments)
    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert sleep_commands == [["sleep", "1"]]


def test_gemini_public_response_file_is_inside_git_dir(tmp_path):
    valid = "LGTM from response file.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=["stdout should be ignored"], public_response_outputs=[valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{valid}"]
    gemini_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["gemini"]]
    assert len(gemini_commands) == 1
    prompt = "\n".join(gemini_commands[0])
    expected_prefix = str(config.gemini_dir / ".git" / "agent-loop" / "responses" / "gemini")
    assert expected_prefix in prompt
    assert "/tmp/coding-review-agent-loop/responses/" not in prompt


def test_gemini_public_response_file_resolves_worktree_git_dir(tmp_path):
    valid = "LGTM from response file.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=["stdout should be ignored"], public_response_outputs=[valid])
    config = make_config(tmp_path, reviewer="gemini")
    git_dir = tmp_path / "main-repo" / ".git" / "worktrees" / "gemini"
    git_dir.mkdir(parents=True)
    (config.gemini_dir / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{valid}"]
    gemini_call = next(cmd for cmd, _cwd in runner.commands if cmd[:1] == ["gemini"])
    assert str(git_dir / "agent-loop" / "responses" / "gemini") in gemini_call[2]
    assert str(config.gemini_dir / ".git" / "agent-loop") not in gemini_call[2]


def test_gemini_pre_marker_429_does_not_suppress_structured_review_repair(tmp_path):
    malformed_public_review = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "approved",
                "summary": "Found one issue.",
                "blocking_items": [],
                "same_pr_followups": [],
                "future_followups": [],
                "prior_item_dispositions": [],
            }
        )
        + "\nProse between JSON and footer should be repaired.\n"
        "<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    )
    raw_stdout = (
        "Attempt 1 failed with status 429. Retrying with backoff... "
        "No capacity available for model gemini-3-flash-preview on the server.\n"
        f"{PUBLIC_RESPONSE_MARKER}\n"
        f"{malformed_public_review}"
    )
    repaired_review = structured_pr_review(
        state="approved",
        summary="Review passed after repair.",
        reviewer="Google Gemini",
    )
    runner = FakeRunner(gemini_outputs=[{"stdout": raw_stdout}])
    config = make_config(tmp_path, reviewer="gemini", agent_max_retries=0)
    captured_repairs = []

    def fake_attempt_repair(raw: str, gemini_cmd: str, *, expected_kind: str | None = None, unresolved_item_ids=None, surfaced_requirement_ids=None) -> str | None:
        captured_repairs.append(raw)
        assert expected_kind == "pr_review"
        return repaired_review

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", fake_attempt_repair):
        assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert captured_repairs == [malformed_public_review]
    assert "429" not in captured_repairs[0]
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)
    assert any("Review passed after repair." in comment for comment in runner.comments)


def test_gemini_response_file_repair_ignores_raw_stdout_transient_diagnostics(tmp_path):
    malformed_public_review = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "approved",
                "summary": "Found one issue.",
                "blocking_items": ["Approved reviews cannot have blocking items."],
                "same_pr_followups": [],
                "future_followups": [],
                "prior_item_dispositions": [],
            }
        )
        + "\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    )
    repaired_review = structured_pr_review(
        state="approved",
        summary="Response file review passed after repair.",
        reviewer="Google Gemini",
    )
    runner = FakeRunner(
        gemini_outputs=[
            {"stdout": "Attempt 1 failed with status 429. No capacity available, then recovered."}
        ],
        public_response_outputs=[{"text": malformed_public_review}],
    )
    config = make_config(tmp_path, reviewer="gemini", agent_max_retries=0)
    captured_repairs = []

    def fake_attempt_repair(raw: str, gemini_cmd: str, *, expected_kind: str | None = None, unresolved_item_ids=None, surfaced_requirement_ids=None) -> str | None:
        captured_repairs.append(raw)
        return repaired_review

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", fake_attempt_repair):
        assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert captured_repairs == [malformed_public_review]
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)
    assert any("Response file review passed after repair." in comment for comment in runner.comments)


def test_pr_loop_exhausted_transient_retry_reports_attempt_logs(tmp_path):
    diagnostic = "[ERROR] Invalid stream: The model returned an empty response or malformed tool call."
    runner = FakeRunner(gemini_outputs=[(diagnostic, 1), (diagnostic, 1), (diagnostic, 1)])
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(AgentLoopError) as exc_info:
        run_pr_loop(runner, pr_number=77, config=config)

    message = str(exc_info.value)
    assert "No review result was recorded" in message
    assert "Failure category: transient" in message
    assert "Attempt logs:" in message
    assert "gemini.log" in message
    assert runner.comments == []


def test_pr_loop_retries_quota_error(tmp_path):
    quota_output = "Quota exceeded for this project."
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[(quota_output, 1), valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{valid}"]
    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert len(sleep_commands) == 1


def test_pr_loop_does_not_retry_normal_missing_marker_response(tmp_path):
    output = "I reviewed the PR and it looks fine."
    runner = FakeRunner(gemini_outputs=[output])
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(AgentLoopError, match="AGENT_STATE"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert runner.comments == []
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)


def test_pr_loop_retries_rate_limit_429(tmp_path):
    rate_limit_output = "HTTP 429 Too Many Requests: rate limit exceeded."
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[(rate_limit_output, 1), valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{valid}"]
    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert len(sleep_commands) == 1


def test_pr_loop_retries_claude_session_limit(tmp_path):
    session_limit_output = "Error: session_limit_exceeded — too many sessions for this project."
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[(session_limit_output, 1), valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{valid}"]
    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert len(sleep_commands) == 1


def test_pr_loop_retries_gemini_no_capacity(tmp_path):
    no_capacity_output = "No capacity available for model gemini-flash on the server."
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[(no_capacity_output, 1), valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [f"**Review verdict:** Approved\n\n{valid}"]
    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert len(sleep_commands) == 1


def test_diagnostic_shaped_public_response_remains_transient(tmp_path):
    public_response = (
        f"{PUBLIC_RESPONSE_MARKER}\n"
        "HTTP 429 Too Many Requests: rate limit exceeded.\n"
        "<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    )
    runner = FakeRunner(gemini_outputs=[{"stdout": public_response}])
    config = make_config(tmp_path, reviewer="gemini", agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair") as repair_mock:
        with pytest.raises(AgentLoopError) as exc_info:
            run_pr_loop(runner, pr_number=77, config=config)

    repair_mock.assert_not_called()
    assert "Failure category: transient" in str(exc_info.value)


def test_public_response_error_payload_remains_transient():
    assert _is_transient_public_response(
        json.dumps(
            {
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Quota exceeded. Retry-After: 60",
                }
            }
        )
    )


def test_public_response_structured_json_after_known_artifact_is_not_transient():
    text = (
        f"{PUBLIC_RESPONSE_MARKER}\n"
        + structured_pr_review(
            summary="Wrong structured kind discusses 429, quota, capacity, and transient behavior.",
            reviewer="Google Gemini",
        )
    )

    assert not _is_transient_public_response(text, repair_expected_kind="coder_followup")


def test_structured_plan_review_transient_terms_with_trailing_prose_runs_repair(tmp_path):
    malformed_review = (
        structured_plan_review(
            state="approved",
            summary=(
                "The plan discusses 429, quota, resource exhausted, timeout, capacity, "
                "and transient retry handling as domain text."
            ),
            reviewer="Google Gemini",
        )
        + "\nTrailing prose after the signature should be repaired."
    )
    repaired_review = structured_plan_review(
        state="approved",
        summary="Plan review repaired.",
        reviewer="Google Gemini",
    )
    runner = FakeRunner(gemini_outputs=[malformed_review])
    config = make_config(tmp_path, reviewer="gemini", agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value=repaired_review) as repair_mock:
        response = _run_validated_agent(
            runner,
            agent="gemini",
            config=config,
            prompt="Review the plan.",
            marker_description="<!-- AGENT_PLAN_STATE: approved|blocking -->",
            validate=lambda text: _validate_plan_review_response(
                text,
                reviewer="Google Gemini",
                unresolved_items=(),
            ),
            use_repair=True,
            repair_expected_kind="plan_review",
        )

    assert response.text == repaired_review
    repair_mock.assert_called_once_with(
        malformed_review,
        config.gemini_cmd,
        expected_kind="plan_review",
    )
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)


def test_structured_pr_review_transient_terms_duplicate_footer_fails_deterministically(tmp_path):
    malformed_review = (
        structured_pr_review(
            state="approved",
            summary=(
                "The review covers capacity, timeout, 429, quota, resource-exhausted, "
                "and transient classifier behavior."
            ),
            reviewer="Google Gemini",
        )
        + "\n\n<!-- AGENT_STATE: approved -->"
    )
    runner = FakeRunner(gemini_outputs=[malformed_review])
    config = make_config(tmp_path, reviewer="gemini", agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value="still invalid") as repair_mock:
        with pytest.raises(AgentLoopError) as exc_info:
            _run_validated_agent(
                runner,
                agent="gemini",
                config=config,
                prompt="Review the PR.",
                marker_description="<!-- AGENT_STATE: approved|blocking -->",
                validate=lambda text: _validate_review_response(
                    text,
                    reviewer="Google Gemini",
                    unresolved_items=(),
                ),
                use_repair=True,
                repair_expected_kind="pr_review",
            )

    repair_mock.assert_called_once_with(
        malformed_review,
        config.gemini_cmd,
        expected_kind="pr_review",
    )
    message = str(exc_info.value)
    assert "Failure category: deterministic" in message
    assert "Failure category: transient" not in message
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)


def test_structured_coder_followup_transient_terms_before_footer_runs_repair(tmp_path):
    unresolved_items = (
        UnresolvedReviewItem(
            item_id="item-1",
            reviewer="OpenAI Codex",
            source_round=1,
            text="Add timeout regression coverage.",
            status="blocking",
        ),
    )
    malformed_followup = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "approved",
                "summary": "Updated timeout and capacity handling without treating prose as transient.",
                "addressed_items": ["item-1"],
                "remaining_items": [],
                "human_requirements": {
                    "addressed_ids": [],
                    "checked_discussion_directly": False,
                },
            }
        )
        + "\n## Changes made\nMentioned timeout and capacity in prose before the footer.\n"
        "<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"
    )
    repaired_followup = structured_coder_followup(
        state="approved",
        summary="Updated timeout and capacity handling.",
        addressed_items=["item-1"],
        remaining_items=[],
        reviewer="Anthropic Claude",
    )
    runner = FakeRunner(claude_outputs=[malformed_followup])
    config = make_config(tmp_path, coder="claude", agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value=repaired_followup) as repair_mock:
        response = _run_validated_agent(
            runner,
            agent="claude",
            config=config,
            prompt="Address review feedback.",
            marker_description="<!-- AGENT_STATE: approved|blocking -->",
            validate=lambda text: _validate_coder_followup_response(
                text,
                unresolved_items=unresolved_items,
                human_requirements=(),
            ),
            use_repair=True,
            repair_expected_kind="coder_followup",
        )

    assert response.text == repaired_followup
    repair_mock.assert_called_once_with(
        malformed_followup,
        config.gemini_cmd,
        expected_kind="coder_followup",
    )
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)


def test_run_validated_agent_recovers_coder_followup_from_message_text_when_response_file_markdown(tmp_path):
    unresolved_items = (
        UnresolvedReviewItem(
            item_id="item-8",
            reviewer="Orchestrator",
            source_round=4,
            text="Acknowledge signed human requirements.",
            status="blocking",
        ),
    )
    valid_followup = structured_coder_followup(
        state="approved",
        summary="Acknowledged the signed human requirements.",
        addressed_items=["item-8"],
        remaining_items=[],
        reviewer="OpenAI Codex",
    )
    markdown_response_file = (
        "### Human requirements\n\n"
        "Acknowledged.\n\n"
        "<!-- HUMAN_REQUIREMENTS_ADDRESSED -->\n"
        "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    )
    runner = FakeRunner(
        codex_outputs=[{"public_response": valid_followup, "stdout": "diagnostic output"}],
        public_response_outputs=[{"text": markdown_response_file}],
    )
    config = make_config(tmp_path, coder="codex", agent_max_retries=0)

    response = _run_validated_agent(
        runner,
        agent="codex",
        config=config,
        prompt="Address feedback.",
        marker_description="<!-- AGENT_STATE: approved|blocking -->",
        validate=lambda text: _validate_coder_followup_response(
            text,
            unresolved_items=unresolved_items,
            human_requirements=(),
        ),
        repair_expected_kind="coder_followup",
    )

    assert response.text == valid_followup
    assert response.marker_value.addressed_items == ("item-8",)


def test_run_validated_agent_recovers_fenced_coder_followup_from_raw_stdout(tmp_path):
    unresolved_items = (
        UnresolvedReviewItem(
            item_id="item-1",
            reviewer="OpenAI Codex",
            source_round=1,
            text="Fix the bug.",
            status="blocking",
        ),
    )
    valid_followup = structured_coder_followup(
        state="approved",
        addressed_items=["item-1"],
        remaining_items=[],
        reviewer="OpenAI Codex",
    )
    json_part, footer = valid_followup.split("\n<!-- AGENT_STATE:", 1)
    fenced_stdout = f"tool diagnostic\n```json\n{json_part}\n```\n<!-- AGENT_STATE:{footer}"
    runner = FakeRunner(
        codex_outputs=[{"public_response": "legacy markdown", "stdout": fenced_stdout}],
        public_response_outputs=[{"text": "### Update\nFixed it.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"}],
    )
    config = make_config(tmp_path, coder="codex", agent_max_retries=0)

    response = _run_validated_agent(
        runner,
        agent="codex",
        config=config,
        prompt="Address feedback.",
        marker_description="<!-- AGENT_STATE: approved|blocking -->",
        validate=lambda text: _validate_coder_followup_response(
            text,
            unresolved_items=unresolved_items,
            human_requirements=(),
        ),
        repair_expected_kind="coder_followup",
    )

    assert response.text == valid_followup


@pytest.mark.parametrize(
    "stdout",
    [
        structured_pr_review(reviewer="OpenAI Codex"),
        "diagnostic output without a structured response",
    ],
)
def test_run_validated_agent_refuses_unrecoverable_stdout_when_response_file_markdown(tmp_path, stdout):
    runner = FakeRunner(
        codex_outputs=[{"public_response": "legacy markdown", "stdout": stdout}],
        public_response_outputs=[{"text": "### Update\nFixed it.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"}],
    )
    config = make_config(tmp_path, coder="codex", agent_max_retries=0)

    with pytest.raises(AgentLoopError, match="No review result was recorded"):
        _run_validated_agent(
            runner,
            agent="codex",
            config=config,
            prompt="Address feedback.",
            marker_description="<!-- AGENT_STATE: approved|blocking -->",
            validate=lambda text: _validate_coder_followup_response(
                text,
                unresolved_items=(),
                human_requirements=(),
            ),
            repair_expected_kind="coder_followup",
        )


def test_run_validated_agent_refuses_multiple_stdout_structured_candidates(tmp_path):
    first = structured_coder_followup(
        state="approved",
        summary="First candidate.",
        reviewer="OpenAI Codex",
    )
    second = structured_coder_followup(
        state="approved",
        summary="Second candidate.",
        reviewer="OpenAI Codex",
    )
    runner = FakeRunner(
        codex_outputs=[{"public_response": "legacy markdown", "stdout": first + "\n\n" + second}],
        public_response_outputs=[{"text": "### Update\nFixed it.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"}],
    )
    config = make_config(tmp_path, coder="codex", agent_max_retries=0)

    with pytest.raises(AgentLoopError, match="No review result was recorded"):
        _run_validated_agent(
            runner,
            agent="codex",
            config=config,
            prompt="Address feedback.",
            marker_description="<!-- AGENT_STATE: approved|blocking -->",
            validate=lambda text: _validate_coder_followup_response(
                text,
                unresolved_items=(),
                human_requirements=(),
            ),
            repair_expected_kind="coder_followup",
        )


def test_run_validated_agent_keeps_valid_response_file_authoritative_over_noisy_stdout(tmp_path):
    valid_followup = structured_coder_followup(
        state="approved",
        summary="Response file wins.",
        reviewer="OpenAI Codex",
    )
    runner = FakeRunner(
        codex_outputs=[{"public_response": "ignored message", "stdout": "unrelated noisy diagnostics"}],
        public_response_outputs=[{"text": valid_followup}],
    )
    config = make_config(tmp_path, coder="codex", agent_max_retries=0)

    response = _run_validated_agent(
        runner,
        agent="codex",
        config=config,
        prompt="Address feedback.",
        marker_description="<!-- AGENT_STATE: approved|blocking -->",
        validate=lambda text: _validate_coder_followup_response(
            text,
            unresolved_items=(),
            human_requirements=(),
        ),
        repair_expected_kind="coder_followup",
    )

    assert response.text == valid_followup


def test_structured_plan_revision_transient_terms_before_footer_runs_repair(tmp_path):
    malformed_revision = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_revision",
                "state": "blocking",
                "summary": "Revise handling for 429, quota, resource exhausted, transient, and timeout.",
                "prior_plan_item_dispositions": [],
                "plan_steps": [
                    "Separate public-response validation from transient raw diagnostics.",
                    "Keep capacity and quota retry handling for raw provider failures.",
                ],
            }
        )
        + "\n## Revised plan\nProse before the AGENT_PLAN_STATE footer is invalid.\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    repaired_revision = structured_plan_revision(
        summary="Revised transient classifier plan.",
        plan_steps=[
            "Separate public-response validation from transient raw diagnostics.",
            "Keep capacity and quota retry handling for raw provider failures.",
        ],
        reviewer="Anthropic Claude",
    )
    runner = FakeRunner(claude_outputs=[malformed_revision])
    config = make_config(tmp_path, coder="claude", agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value=repaired_revision) as repair_mock:
        response = _run_validated_agent(
            runner,
            agent="claude",
            config=config,
            prompt="Revise the plan.",
            marker_description="<!-- AGENT_PLAN_STATE: approved|blocking -->",
            validate=_validate_plan_revision_response,
            use_repair=True,
            repair_expected_kind="plan_revision",
        )

    assert response.text == repaired_revision
    repair_mock.assert_called_once_with(
        malformed_revision,
        config.gemini_cmd,
        expected_kind="plan_revision",
    )
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)


def test_gemini_duplicate_trailing_agent_state_marker_is_repairable(tmp_path):
    malformed_public_review = (
        structured_pr_review(
            state="approved",
            summary="Found one issue.",
            reviewer="Google Gemini",
        )
        + "\n\n<!-- AGENT_STATE: approved -->"
    )
    raw_stdout = f"{PUBLIC_RESPONSE_MARKER}\n{malformed_public_review}"
    repaired_review = structured_pr_review(
        state="approved",
        summary="Duplicate marker repaired.",
        reviewer="Google Gemini",
    )
    runner = FakeRunner(gemini_outputs=[{"stdout": raw_stdout}])
    config = make_config(tmp_path, reviewer="gemini", agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value=repaired_review) as repair_mock:
        assert run_pr_loop(runner, pr_number=77, config=config) == 0

    repair_mock.assert_called_once_with(
        malformed_public_review,
        config.gemini_cmd,
        expected_kind="pr_review",
    )
    assert any("Duplicate marker repaired." in comment for comment in runner.comments)


def test_gemini_pre_marker_429_malformed_public_response_fails_deterministically(tmp_path):
    malformed_public_review = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pr_review",
                "state": "approved",
                "summary": "Found one issue.",
                "blocking_items": [],
                "same_pr_followups": [],
                "future_followups": [],
                "prior_item_dispositions": [],
            }
        )
        + "\nExtra prose before the footer.\n"
        "<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    )
    raw_stdout = (
        "Attempt 1 failed with status 429. No capacity available for model gemini.\n"
        f"{PUBLIC_RESPONSE_MARKER}\n"
        f"{malformed_public_review}"
    )
    runner = FakeRunner(gemini_outputs=[{"stdout": raw_stdout}])
    config = make_config(tmp_path, reviewer="gemini", agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value="still invalid"):
        with pytest.raises(AgentLoopError) as exc_info:
            run_pr_loop(runner, pr_number=77, config=config)

    message = str(exc_info.value)
    assert "Failure category: deterministic" in message
    assert "Failure category: transient" not in message


def test_pr_loop_does_not_retry_billing_credit_exhaustion(tmp_path):
    output = "Quota exceeded: billing credits are exhausted."
    runner = FakeRunner(gemini_outputs=[output])
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(AgentLoopError, match="No review result was recorded"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert runner.comments == []
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)


def test_pr_loop_does_not_retry_auth_failure(tmp_path):
    output = "Unauthorized: invalid api key provided."
    runner = FakeRunner(gemini_outputs=[output])
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(AgentLoopError, match="No review result was recorded"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert runner.comments == []
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)


def test_pr_loop_failure_log_distinguishes_transient_failure(tmp_path):
    rate_limit_output = "HTTP 429: rate limit exceeded."
    runner = FakeRunner(gemini_outputs=[(rate_limit_output, 1)] * 3)
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(AgentLoopError) as exc_info:
        run_pr_loop(runner, pr_number=77, config=config)

    message = str(exc_info.value)
    assert "transient" in message
    assert "rerun may succeed" in message


def test_pr_loop_failure_log_identifies_non_retryable(tmp_path):
    billing_output = "Your billing account has no credits remaining."
    runner = FakeRunner(gemini_outputs=[billing_output])
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(AgentLoopError) as exc_info:
        run_pr_loop(runner, pr_number=77, config=config)

    message = str(exc_info.value)
    assert "non-retryable" in message
    assert "credentials or billing" in message


def test_pr_loop_exits_immediately_on_long_reset_rate_limit(tmp_path):
    # "Retry-After: 3600" → 3600 s reset > 300 s threshold → must exit, not retry.
    rate_limit_output = "HTTP 429: rate limit exceeded. Retry-After: 3600"
    runner = FakeRunner(gemini_outputs=[(rate_limit_output, 1)])
    config = make_config(tmp_path, reviewer="gemini")

    with pytest.raises(QuotaResetExceededError) as exc_info:
        run_pr_loop(runner, pr_number=77, config=config)

    message = str(exc_info.value)
    assert "quota exhausted" in message.lower()
    assert "1h" in message  # 3600 s = 1h
    assert "Rerun when quota resets" in message
    # Must not have slept / retried.
    assert not any(cmd[:1] == ["sleep"] for cmd, _cwd in runner.commands)


def test_pr_loop_retries_on_short_reset_rate_limit(tmp_path):
    # "Retry-After: 60" → 60 s reset ≤ 300 s threshold → retry automatically.
    rate_limit_output = "HTTP 429: rate limit exceeded. Retry-After: 60"
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[(rate_limit_output, 1), valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert len(sleep_commands) == 1


def test_pr_loop_retries_on_rate_limit_without_reset_time(tmp_path):
    # No parseable reset time → fall back to normal retry behavior.
    rate_limit_output = "HTTP 429: rate limit exceeded."
    valid = "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
    runner = FakeRunner(gemini_outputs=[(rate_limit_output, 1), valid])
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    sleep_commands = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["sleep"]]
    assert len(sleep_commands) == 1


@pytest.mark.parametrize("text,expected_secs", [
    ("Retry-After: 3600", 3600),
    ("retry after 1800", 1800),
    ("retryDelay: '7200s'", 7200),
    ("try again in 2h 30m", 9000),
    ("try again in 45m", 2700),
    ("resets in 1h", 3600),
    ("reset in 5m", 300),
])
def test_parse_rate_limit_reset_seconds(text, expected_secs):
    assert _parse_rate_limit_reset_seconds(text) == expected_secs


@pytest.mark.parametrize("text", [
    "HTTP 429: rate limit exceeded.",
    "Too many requests.",
    "quota exceeded",
])
def test_parse_rate_limit_reset_seconds_returns_none_when_unparseable(text):
    assert _parse_rate_limit_reset_seconds(text) is None


@pytest.mark.parametrize("seconds,expected", [
    (3600, "1h"),
    (7200, "2h"),
    (9000, "2h 30m"),
    (300, "5m"),
    (45, "45s"),
    (3660, "1h 1m"),
])
def test_format_reset_duration(seconds, expected):
    assert _format_reset_duration(seconds) == expected


def test_quota_reset_exceeded_error_exit_code():
    assert QuotaResetExceededError.EXIT_CODE == 3


def test_pr_loop_reinjects_blocking_item_when_human_requirement_marker_missing(tmp_path):
    # Reviewer approves without HUMAN_REQUIREMENTS_RESOLVED → synthetic blocking item,
    # loop hits max_rounds (set to 1) instead of a terminal deadlock.
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        pr_payload={
            "number": 77,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/77",
            "title": "Improve review prompt context",
            "headRefName": "feature/review-context",
            "baseRefName": "main",
            "headRefOid": "abc123",
            "comments": [
                {
                    "author": {"login": "maintainer"},
                    "createdAt": "2026-05-18T10:00:00Z",
                    "url": "https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                    "body": "Please use the absolute URL.\n\n-- Human Reviewer",
                }
            ],
            "reviews": [],
        },
    )
    config = make_config(
        tmp_path,
        auto_merge=True,
        test_command=("pytest", "tests/test_agent_loop.py"),
        approved_followups="summarize",
        max_rounds=1,
    )

    # The old behaviour was a terminal deadlock; now the loop continues and hits max_rounds.
    with pytest.raises(AgentLoopError, match="blocking issues after round 1"):
        run_pr_loop(runner, pr_number=77, config=config)

    commands = [cmd for cmd, _cwd in runner.commands]
    assert ["pytest", "tests/test_agent_loop.py"] not in commands
    assert ["gh", "pr", "merge", "77", "--repo", "OWNER/REPO", "--merge"] not in commands
    assert not any(comment.startswith("Approved-review future follow-ups") for comment in runner.comments)


def test_pr_loop_recovers_when_second_reviewer_includes_human_requirement_marker(tmp_path):
    # Round 1: reviewer approves without HUMAN_REQUIREMENTS_RESOLVED → blocking item injected.
    # Round 2: coder addresses it; reviewer approves with the marker → success.
    pr_payload = {
        "number": 77,
        "state": "OPEN",
        "url": "https://github.com/OWNER/REPO/pull/77",
        "title": "Improve review prompt context",
        "headRefName": "feature/review-context",
        "baseRefName": "main",
        "headRefOid": "abc123",
        "comments": [
            {
                "author": {"login": "maintainer"},
                "createdAt": "2026-05-18T10:00:00Z",
                "url": "https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                "body": "Please use the absolute URL.\n\n-- Human Reviewer",
            }
        ],
        "reviews": [],
    }
    runner = FakeRunner(
        claude_outputs=[
            # Round 2: coder addresses the re-injected blocking item and acknowledges human requirements
            "Addressed human requirements.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: used the absolute URL.\n"
            "<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            # Round 1: approves but forgets the marker
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
            # Round 2: resolves the synthetic blocking item and acknowledges human requirements
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- HUMAN_REQUIREMENTS_RESOLVED -->\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        pr_payload=pr_payload,
    )
    config = make_config(tmp_path, max_rounds=2)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0


def test_pr_loop_allows_approval_with_human_requirement_resolution_marker(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "LGTM.\n<!-- HUMAN_REQUIREMENTS_RESOLVED -->\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        pr_payload={
            "number": 77,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/77",
            "title": "Improve review prompt context",
            "headRefName": "feature/review-context",
            "baseRefName": "main",
            "headRefOid": "abc123",
            "comments": [
                {
                    "author": {"login": "maintainer"},
                    "createdAt": "2026-05-18T10:00:00Z",
                    "url": "https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                    "body": "Please use the absolute URL.\n\n-- Human Reviewer",
                }
            ],
            "reviews": [],
        },
    )
    config = make_config(tmp_path, test_command=("pytest", "tests/test_agent_loop.py"))

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    assert ["pytest", "tests/test_agent_loop.py"] in commands


def test_pr_loop_accepts_structured_coder_followup_in_pr_round(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "coder_followup",
                    "state": "blocking",
                    "summary": "Added the requested regression test.",
                    "addressed_items": ["item-1"],
                    "remaining_items": [],
                    "addressed_item_notes": {
                        "item-1": "Added the structured coder follow-up regression case."
                    },
                    "human_requirements": {
                        "addressed_ids": [],
                        "checked_discussion_directly": False,
                    },
                    "tests_run": ["pytest tests/test_agent_loop.py -k structured_coder_followup"],
                }
            )
            + "\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
        ],
        codex_outputs=[
            "Need one more regression test before merge."
            + blocking_issues("Add the structured coder follow-up regression case.")
            + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", max_rounds=2)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    followup_comments = [comment for comment in runner.comments if "## Coder follow-up" in comment]
    assert len(followup_comments) == 1
    visible_followup = _strip_round_metadata(followup_comments[0])
    assert "Added the requested regression test." in visible_followup
    assert "### Addressed items\n- item-1: Blocking issue from OpenAI Codex" in visible_followup
    assert "  - Resolution: Added the structured coder follow-up regression case." in visible_followup
    assert "### Remaining items\n- None." in visible_followup
    assert (
        "### Tests run\n- pytest tests/test_agent_loop.py -k structured_coder_followup"
        in visible_followup
    )
    assert '"kind": "coder_followup"' not in visible_followup


def test_pr_loop_rejects_malformed_structured_coder_followup_before_re_review(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "coder_followup",
                    "state": "blocking",
                    "summary": "Tried to handle the feedback.",
                    "addressed_items": ["item-9"],
                    "remaining_items": [],
                    "human_requirements": {
                        "addressed_ids": [],
                        "checked_discussion_directly": False,
                    },
                }
            )
            + "\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
        ],
        codex_outputs=[
            "Need one more regression test before merge."
            + blocking_issues("Add the structured coder follow-up regression case.")
            + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(
        tmp_path,
        coder="claude",
        reviewer="codex",
        max_rounds=2,
        agent_max_retries=0,
    )

    with pytest.raises(
        AgentLoopError,
        match="Coder follow-up referenced unknown unresolved reviewer item IDs: item-9",
    ):
        run_pr_loop(runner, pr_number=77, config=config)


def test_reconcile_human_requirements_ack_item_surfaces_markdown_ack_blocker():
    human_requirements = (
        HumanReviewRequirement(
            source_type="PR comment",
            author="reviewer",
            created_at="2026-05-18T10:00:00Z",
            url="https://github.com/OWNER/REPO/pull/77#issuecomment-1",
            body="Please use the absolute URL.",
        ),
    )

    reconciled = _reconcile_human_requirements_ack_item(
        (),
        coder_output="Implemented fix without the extra acknowledgement.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        human_requirements=human_requirements,
        source_round=2,
    )

    assert [item.item_id for item in reconciled] == [HUMAN_REQUIREMENTS_ACK_ITEM_ID]
    assert "missing required signed human requirements marker" in reconciled[0].text


def test_reconcile_human_requirements_ack_item_clears_markdown_ack_blocker():
    human_requirements = (
        HumanReviewRequirement(
            source_type="PR comment",
            author="reviewer",
            created_at="2026-05-18T10:00:00Z",
            url="https://github.com/OWNER/REPO/pull/77#issuecomment-1",
            body="Please use the absolute URL.",
        ),
    )

    reconciled = _reconcile_human_requirements_ack_item(
        (
            UnresolvedReviewItem(
                item_id=HUMAN_REQUIREMENTS_ACK_ITEM_ID,
                reviewer="Orchestrator",
                source_round=1,
                text="Ack missing.",
                status="blocking",
            ),
        ),
        coder_output=(
            "Implemented follow-up.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: updated the URL handling.\n"
            "<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
        ),
        human_requirements=human_requirements,
        source_round=2,
    )

    assert reconciled == []


def test_pr_loop_revalidates_latest_coder_output_against_refreshed_human_requirements(
    tmp_path, monkeypatch
):
    runner = FakeRunner(
        claude_outputs=[
            "Implemented fix with the required acknowledgement.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: updated the URL handling.\n"
            "<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Blocking issue.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        pr_payload={
            "number": 77,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/77",
            "title": "Improve review prompt context",
            "headRefName": "feature/review-context",
            "baseRefName": "main",
            "headRefOid": "abc123",
            "comments": [
                {
                    "author": {"login": "maintainer"},
                    "createdAt": "2026-05-18T10:00:00Z",
                    "url": "https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                    "body": "Please use the absolute URL.\n\n-- Human Reviewer",
                }
            ],
            "reviews": [],
        },
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", max_rounds=2)
    metadata = PullRequestMetadata(
        number=77,
        repo="OWNER/REPO",
        title="Improve review prompt context",
        head_branch="feature/review-context",
        base_branch="main",
        head_sha="abc123",
        url="https://github.com/OWNER/REPO/pull/77",
    )
    contexts = iter(
        [
            PullRequestReviewContext(
                metadata=metadata,
                comments=(),
                human_requirements=(
                    HumanReviewRequirement(
                        source_type="PR comment",
                        author="maintainer",
                        created_at="2026-05-18T10:00:00Z",
                        url="https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                        body="Please use the absolute URL.",
                    ),
                ),
            ),
            PullRequestReviewContext(
                metadata=metadata,
                comments=(),
                human_requirements=(),
            ),
        ]
    )

    monkeypatch.setattr(
        "coding_review_agent_loop.orchestrator.get_pr_review_context",
        lambda *args, **kwargs: next(contexts),
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    review_prompts = [cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"]]
    assert len(review_prompts) == 2
    assert HUMAN_REQUIREMENTS_ACK_ITEM_ID not in review_prompts[1]


def test_pr_loop_routes_migration_validation_failure_through_coder_followup(tmp_path, monkeypatch):
    runner = FakeRunner(
        claude_outputs=["Fixed migration.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
        codex_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
            "LGTM again."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, test_command=("pytest", "tests/test_agent_loop.py"), max_rounds=2)
    validations = iter(
        [
            MigrationValidationResult(
                ok=False,
                message=(
                    "alembic/versions/e4f5a6b7c8d9_add_pricing.py declares `down_revision = '5d5f0e1a2b3c'`; "
                    "expected current head `402b9e8af79b`."
                ),
            ),
            MigrationValidationResult(ok=True),
        ]
    )

    monkeypatch.setattr(
        "coding_review_agent_loop.orchestrator.validate_pr_migration_topology",
        lambda *args, **kwargs: next(validations),
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    coder_prompts = [cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(coder_prompts) == 1
    assert "Alembic migration validation unresolved blocking item [item-1]" in coder_prompts[0]
    assert "expected current head `402b9e8af79b`" in coder_prompts[0]

    commands = runner.commands
    pytest_index = command_index(commands, ["pytest", "tests/test_agent_loop.py"])
    first_review_index = [
        index for index, (cmd, _cwd) in enumerate(commands) if cmd[:2] == ["codex", "exec"]
    ][0]
    second_review_index = [
        index for index, (cmd, _cwd) in enumerate(commands) if cmd[:2] == ["codex", "exec"]
    ][1]
    assert first_review_index < pytest_index < second_review_index


def test_review_prompt_includes_pr_metadata_and_suggested_commands(tmp_path):
    runner = FakeRunner(codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"])
    config = make_config(tmp_path)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    prompts = [cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"]]
    assert len(prompts) == 1
    prompt = prompts[0]
    assert "PR metadata:" in prompt
    assert "- Repo: OWNER/REPO" in prompt
    assert "- PR: #77" in prompt
    assert "- Title: Improve review prompt context" in prompt
    assert "- Head branch: feature/review-context" in prompt
    assert "- Base branch: main" in prompt
    assert "- Head SHA: abc123" in prompt
    assert "Use this PR metadata as authoritative." in prompt
    assert "Do not spend time discovering the PR\nbranch." in prompt
    assert (
        "gh pr view 77 --repo OWNER/REPO --json "
        "title,body,headRefName,baseRefName,headRefOid,comments,reviews"
    ) in prompt
    assert "gh pr diff 77 --repo OWNER/REPO" in prompt
    assert "requires confirmation in non-interactive mode" in prompt
    assert "write them outside the repository checkout" in prompt
    assert "/tmp/coding-review-agent-loop/scratch/" in prompt
    assert "GitHub PR checks:" in prompt
    assert "- Overall state: passing" in prompt
    assert "- Required checks: test" in prompt
    assert "Do not say or imply that tests passed globally unless the GitHub PR checks" in prompt
    assert "ignore approved-review follow-up sections" in prompt
    assert "### Future follow-ups" not in prompt
    assert "legacy heading `### Non-blocking follow-ups`" not in prompt
    assert "verify migration topology" in prompt
    assert "Use blocking only for issues that should prevent merge." in prompt


def test_review_prompt_includes_signed_human_requirements(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "LGTM.\n<!-- HUMAN_REQUIREMENTS_RESOLVED -->\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        pr_payload={
            "number": 77,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/77",
            "title": "Improve review prompt context",
            "headRefName": "feature/review-context",
            "baseRefName": "main",
            "headRefOid": "abc123",
            "comments": [
                {
                    "author": {"login": "maintainer"},
                    "createdAt": "2026-05-18T10:00:00Z",
                    "url": "https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                    "body": "Please use the absolute URL.\n\n-- Human Reviewer",
                }
            ],
            "reviews": [],
        },
    )
    config = make_config(tmp_path)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    assert "Signed Human Reviewer Requirements" in prompt
    assert "Please use the absolute URL." in prompt
    assert "Signed human reviewer requirements override AI reviewer preferences" in prompt
    assert "Verify each requirement in this set before approving." in prompt
    assert "<!-- HUMAN_REQUIREMENTS_RESOLVED -->" in prompt


def test_pr_loop_routes_failing_github_checks_through_coder_followup(tmp_path, monkeypatch):
    runner = FakeRunner(
        codex_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
            "Still failing upstream."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=["Investigated CI.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(tmp_path, max_rounds=2)
    check_states = iter(
        [
            {
                "check_runs": [
                    {"name": "tests/test_server.py", "status": "completed", "conclusion": "success"},
                    {"name": "tests/test_security.py", "status": "completed", "conclusion": "failure"},
                ]
            },
            {"check_runs": [{"name": "test", "status": "completed", "conclusion": "success"}]},
        ]
    )

    def advance_checks(*_args, **_kwargs):
        runner.pr_check_runs_payload = next(check_states)
        return original_get_pr_checks(*_args, **_kwargs)

    from coding_review_agent_loop import orchestrator as orchestrator_module

    original_get_pr_checks = orchestrator_module.get_pr_checks
    monkeypatch.setattr(orchestrator_module, "get_pr_checks", advance_checks)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert any(
        comment.startswith("GitHub PR checks are failing for PR #77.") for comment in runner.comments
    )
    followup_prompt = next(
        cmd[-1]
        for cmd, _cwd in runner.commands
        if cmd[:1] == ["claude"]
        and "GitHub PR checks unresolved blocking item [item-1] from round 1:" in cmd[-1]
    )
    assert "Failing checks: tests/test_security.py (failure)" in followup_prompt
    assert "Do not claim global test success unless GitHub PR checks are green." in followup_prompt


def test_pr_loop_blocks_final_approval_when_github_checks_pending(tmp_path):
    runner = FakeRunner(
        codex_outputs=["Looks good locally.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        pr_check_runs_payload={"check_runs": []},
        pr_status_payload={"state": "pending", "statuses": []},
        pr_branch_protection_payload={"contexts": ["test"]},
    )
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="GitHub PR checks for PR #77 are pending"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert any(
        comment.startswith("GitHub PR checks are still pending for PR #77.")
        for comment in runner.comments
    )


def test_pr_loop_summarizes_approved_followups_before_pending_check_exit(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Looks good locally.\n\n### Future follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        pr_check_runs_payload={"check_runs": []},
        pr_status_payload={"state": "pending", "statuses": []},
        pr_branch_protection_payload={"contexts": ["test"]},
    )
    config = make_config(tmp_path, approved_followups="summarize")

    with pytest.raises(AgentLoopError, match="GitHub PR checks for PR #77 are pending"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert len(runner.comments) == 3
    assert runner.comments[1].startswith("Approved-review future follow-ups for PR #77:")
    assert "- Add cleanup docs. (Codex)" in runner.comments[1]
    assert "<!-- AGENT_APPROVED_FOLLOWUPS: pr=77 head=abc123 mode=summarize -->" in runner.comments[1]
    assert runner.comments[2].startswith("GitHub PR checks are still pending for PR #77.")


def test_pr_loop_summary_marker_has_single_blank_line_before_footer_marker(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Looks good locally.\n\n### Future follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
    )
    config = make_config(tmp_path, approved_followups="summarize")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    summary = runner.comments[-1]
    assert (
        "These were mentioned in approved reviews as future work and did not block merge readiness.\n\n"
        "<!-- AGENT_APPROVED_FOLLOWUPS: pr=77 head=abc123 mode=summarize -->\n"
        "-- coding-review-agent-loop"
    ) in summary


def test_pr_loop_creates_approved_followup_issues_before_unavailable_check_exit(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Looks good locally.\n\n### Future follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        pr_check_runs_payload={"check_runs": []},
        pr_branch_protection_returncode=1,
        pr_branch_protection_stderr="500 Internal Server Error",
        pr_check_runs_returncode=1,
        pr_check_runs_stderr="500 Internal Server Error",
        pr_status_returncode=1,
        pr_status_stderr="500 Internal Server Error",
    )
    config = make_config(tmp_path, approved_followups="issue")

    with pytest.raises(AgentLoopError, match="GitHub PR checks for PR #77 are unavailable"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert len(runner.issues) == 1
    assert runner.issues[0]["title"] == "Follow up future review note: Add cleanup docs."
    assert len(runner.comments) == 3
    assert runner.comments[1].startswith("Created approved-review future follow-up issues for PR #77:")
    assert "<!-- AGENT_APPROVED_FOLLOWUPS: pr=77 head=abc123 mode=issue -->" in runner.comments[1]
    assert runner.comments[2].startswith("GitHub PR check status is unavailable for PR #77.")


def test_pr_loop_skips_duplicate_approved_followup_issue_creation_when_marker_exists(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex approves.\n\n### Future follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        pr_payload={
            "comments": [
                {
                    "author": {"login": "coding-review-agent-loop"},
                    "createdAt": "2026-05-22T10:00:00Z",
                    "body": (
                        "Created approved-review future follow-up issues for PR #77:\n\n"
                        "- https://github.com/OWNER/REPO/issues/99\n\n"
                        "These were mentioned in approved reviews as future work and did not block merge readiness.\n\n"
                        "<!-- AGENT_APPROVED_FOLLOWUPS: pr=77 head=abc123 mode=issue -->\n"
                        "-- coding-review-agent-loop"
                    ),
                }
            ]
        },
    )
    config = make_config(tmp_path, approved_followups="issue")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.issues == []
    assert runner.comments == [
        "**Review verdict:** Approved\n\n"
        "Codex approves.\n\n### Future follow-ups\n- Add cleanup docs.\n"
        "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    ]


def test_pr_loop_allows_repos_without_github_checks_when_branch_protection_404(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        pr_check_runs_payload={"check_runs": []},
        pr_status_payload={"state": "pending", "statuses": []},
        pr_branch_protection_returncode=1,
        pr_branch_protection_stderr="404 Not Found",
    )
    config = make_config(tmp_path)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0
    assert not any(comment.startswith("GitHub PR checks are") for comment in runner.comments)


def test_pr_loop_allows_repos_without_github_checks_when_branch_protection_403(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        pr_check_runs_payload={"check_runs": []},
        pr_status_payload={"state": "pending", "statuses": []},
        pr_branch_protection_returncode=1,
        pr_branch_protection_stderr="403 Forbidden",
    )
    config = make_config(tmp_path)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0
    assert not any(comment.startswith("GitHub PR checks are") for comment in runner.comments)


def test_review_prompt_includes_failing_github_check_status(tmp_path):
    runner = FakeRunner(
        codex_outputs=["Blocking.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"],
        pr_check_runs_payload={
            "check_runs": [
                {"name": "tests/test_security.py", "status": "completed", "conclusion": "failure"}
            ]
        },
        pr_branch_protection_payload={"contexts": ["tests/test_security.py"]},
    )
    config = make_config(tmp_path, max_rounds=1)

    with pytest.raises(AgentLoopError, match="blocking issues after round 1"):
        run_pr_loop(runner, pr_number=77, config=config)

    prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    assert "GitHub PR checks:" in prompt
    assert "- Overall state: failing" in prompt
    assert "- Failing checks: tests/test_security.py (failure)" in prompt


def test_review_prompt_mentions_branch_protection_forbidden_when_checks_exist(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        pr_check_runs_payload={
            "check_runs": [{"name": "test", "status": "completed", "conclusion": "success"}]
        },
        pr_branch_protection_returncode=1,
        pr_branch_protection_stderr="403 Forbidden",
    )
    config = make_config(tmp_path)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    assert "Current GitHub token cannot inspect branch protection on the PR base branch." in prompt


def test_get_pr_checks_returns_no_checks_in_dry_run(tmp_path):
    runner = FakeRunner()
    config = make_config(tmp_path, dry_run=True)

    pr_checks = get_pr_checks(
        runner,
        config=config,
        metadata=PullRequestMetadata(
            number=77,
            repo="OWNER/REPO",
            title="Improve review prompt context",
            head_branch="feature/review-context",
            base_branch="main",
            head_sha="abc123",
            url="https://github.com/OWNER/REPO/pull/77",
        ),
    )

    assert pr_checks.state == "no_checks"
    assert pr_checks.branch_protection_status == "unavailable"
    assert pr_checks.branch_protection_note == "Dry run mode does not query live GitHub PR checks."


def test_blocking_followup_prompt_reinjects_issue_context(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Needs a fix.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            structured_coder_followup(
                state="approved",
                addressed_items=["item-1"],
                remaining_items=[],
                reviewer="Anthropic Claude",
            )
        ],
    )
    config = make_config(tmp_path)
    issue_context = IssueContext(
        number=56,
        repo="OWNER/REPO",
        title="Support issue comments",
        body="Original request.",
        url="https://github.com/OWNER/REPO/issues/56",
        comments=(
            IssueComment(
                author="commenter",
                created_at="2026-05-17T10:00:00Z",
                body="Clarifying issue comment.",
            ),
        ),
    )

    assert run_pr_loop(runner, pr_number=77, config=config, issue_context=issue_context) == 0

    followup_prompt = next(
        cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["claude"] and "Address the review below" in cmd[-1]
    )
    assert "Issue context from GitHub" in followup_prompt
    assert "Title:\nSupport issue comments" in followup_prompt
    assert "Clarifying issue comment." in followup_prompt
    assert "Needs a fix." in followup_prompt


def test_blocking_followup_prompt_includes_human_requirements_before_ai_feedback(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Needs a fix.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- HUMAN_REQUIREMENTS_RESOLVED -->\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "Fixed review.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: used the absolute URL.\n"
            "<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
        ],
        pr_payload={
            "number": 77,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/77",
            "title": "Improve review prompt context",
            "headRefName": "feature/review-context",
            "baseRefName": "main",
            "headRefOid": "abc123",
            "comments": [
                {
                    "author": {"login": "maintainer"},
                    "createdAt": "2026-05-18T10:00:00Z",
                    "url": "https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                    "body": "Please use the absolute URL.\n\n-- Human Reviewer",
                }
            ],
            "reviews": [],
        },
    )
    config = make_config(tmp_path)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    followup_prompt = next(
        cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["claude"] and "Address the review below" in cmd[-1]
    )
    assert followup_prompt.index("Signed Human Reviewer Requirements") < followup_prompt.index(
        "Codex unresolved blocking item [item-1] from round 1:"
    )
    assert "Please use the absolute URL." in followup_prompt
    assert "Needs a fix." in followup_prompt


def test_pr_loop_combines_issue_and_pr_signed_human_requirements(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "LGTM.\n<!-- HUMAN_REQUIREMENTS_RESOLVED -->\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        pr_payload={
            "comments": [
                {
                    "author": {"login": "maintainer"},
                    "createdAt": "2026-05-18T10:00:00Z",
                    "url": "https://github.com/OWNER/REPO/pull/77#issuecomment-1",
                    "body": "Use the absolute URL in the PR path.\n\n-- Human Reviewer",
                }
            ],
            "reviews": [],
        },
    )
    config = make_config(tmp_path, reviewer="codex")
    issue_context = IssueContext(
        number=56,
        repo="OWNER/REPO",
        title="Support issue comments",
        body="Original request.",
        url="https://github.com/OWNER/REPO/issues/56",
        comments=(),
        human_requirements=(
            HumanReviewRequirement(
                source_type="Issue body",
                author="issue-author",
                created_at="2026-05-17T08:00:00Z",
                url="https://github.com/OWNER/REPO/issues/56",
                body="Preserve backward compatibility.",
            ),
        ),
    )

    assert run_pr_loop(runner, pr_number=77, config=config, issue_context=issue_context) == 0

    prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    assert "Preserve backward compatibility." in prompt
    assert "Use the absolute URL in the PR path." in prompt
    assert prompt.index("Preserve backward compatibility.") < prompt.index(
        "Use the absolute URL in the PR path."
    )


def test_review_prompt_requests_future_followups_when_processed(tmp_path):
    runner = FakeRunner(codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"])
    config = make_config(tmp_path, approved_followups="summarize")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    assert "### Future follow-ups" in prompt
    assert "legacy heading `### Non-blocking follow-ups`" in prompt
    assert "Do not use the Same-PR follow-ups section in this mode" in prompt
    assert "Use blocking only for issues that should prevent merge." in prompt


def test_review_prompt_allows_same_pr_followups_for_fix_modes(tmp_path):
    runner = FakeRunner(codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"])
    config = make_config(tmp_path, approved_followups="fix-and-summarize")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    assert "### Same-PR follow-ups" in prompt
    assert "### Future follow-ups" in prompt
    assert "small, localized, low-risk cleanup" in prompt
    assert "narrow current-PR cleanup in files already\ntouched by this PR or directly adjacent code" in prompt
    assert "Keep `blocking_items` and `same_pr_followups` mutually exclusive." in prompt
    assert (
        "Use\n`blocking_items` for defects, missing requirements, regressions, security\n"
        "issues, or consistency gaps that make the PR not merge-ready."
    ) in prompt
    assert (
        "Use\n`same_pr_followups` only for small localized cleanup that should be handled in\n"
        "this PR but is not itself the reason the PR is blocked."
    ) in prompt
    assert "Same-PR follow-ups may appear only in blocking reviews." in prompt
    assert "will be sent back to Claude and require another review" in prompt
    assert "Approved means there are no blocking issues, no Same-PR follow-ups, and no\ncarried-forward prior unresolved items left active" in prompt
    assert "If you return `<!-- AGENT_STATE: blocking -->`, do not use structured\nFuture follow-ups" in prompt
    assert (
        "`blocking_items` and `same_pr_followups` must be mutually exclusive: a single\n"
        "concern belongs in exactly one list."
    ) in prompt
    assert (
        "Put merge-blocking defects, missing\nrequirements, regressions, security "
        "issues, and consistency gaps in\n`blocking_items`"
    ) in prompt
    assert (
        "put only small Same-PR cleanup that is not itself the reason\n"
        "the PR is blocked in `same_pr_followups`."
    ) in prompt


def test_same_pr_followup_prompt_no_longer_claims_pr_was_approved(tmp_path):
    config = make_config(tmp_path)

    prompt = build_same_pr_followup_prompt(77, 2, "Rename the helper.", config)

    assert "requested same-PR follow-ups" in prompt
    assert "approved pull request" not in prompt
    assert "remains blocked pending another review round" in prompt


def test_pr_loop_keeps_blocking_review_when_future_followups_are_misclassified(tmp_path):
    runner = FakeRunner(
        gemini_outputs=[
            "Still blocked.\n\n"
            "### Same-PR follow-ups\n"
            "- Tighten the reset helper.\n\n"
            "### Future follow-ups\n"
            "- Consider a broader cleanup later.\n\n"
            "<!-- AGENT_STATE: blocking -->\n"
            "-- Google Gemini",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved", "[item-2] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- Google Gemini",
        ],
        codex_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved", "[item-2] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=["Fixed review.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(
        tmp_path,
        reviewer=("gemini", "codex"),
        approved_followups="fix-and-issue",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments[0].startswith("**Review verdict:** Blocking\n\nStill blocked.")
    assert "Consider a broader cleanup later." not in runner.comments[0]
    followup_prompt = next(
        cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["claude"] and "Address the review below" in cmd[-1]
    )
    assert "Still blocked." in followup_prompt


def test_agent_memory_is_created_and_added_to_review_prompt(tmp_path):
    runner = FakeRunner(codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"])
    memory_dir = tmp_path / "memory"
    config = make_config(tmp_path, agent_memory_dir=memory_dir)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert (memory_dir / "repo-summary.md").exists()
    assert (memory_dir / "architecture-map.md").exists()
    assert (memory_dir / "module-index.json").exists()
    assert (memory_dir / "test-profile.md").exists()
    assert (memory_dir / "toolchain.json").exists()
    assert (memory_dir / "last-analyzed-commit").read_text(encoding="utf-8") == "abc123\n"
    architecture_map = (memory_dir / "architecture-map.md").read_text(encoding="utf-8")
    assert "## Top-level Layout" in architecture_map
    assert "## Python Modules" in architecture_map
    assert "## Supporting Surfaces" in architecture_map
    assert "`src/coding_review_agent_loop`" in architecture_map

    prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    assert "Agent memory context:" in prompt
    assert "Use cached repo memory and execution memory only for orientation." in prompt
    assert "inspect the actual source files and PR diff directly" in prompt
    assert "Do not search the whole filesystem for test tools." in prompt
    assert "src/coding_review_agent_loop/cli.py" in prompt


def test_agent_memory_default_parent_ignores_generated_contents(tmp_path):
    runner = FakeRunner(codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"])
    config = make_config(tmp_path)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    gitignore = tmp_path / "claude" / ".agent-loop" / ".gitignore"
    assert gitignore.read_text(encoding="utf-8") == "*\n!.gitignore\n"


def test_agent_memory_does_not_ignore_custom_parent_directory(tmp_path):
    runner = FakeRunner(codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"])
    memory_dir = tmp_path / "custom-memory"
    config = make_config(tmp_path, agent_memory_dir=memory_dir)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert not (tmp_path / ".gitignore").exists()


def test_agent_memory_detects_changed_files_since_previous_commit(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        git_head="def456",
        changed_files=["src/coding_review_agent_loop/prompts.py", "tests/test_agent_loop.py"],
    )
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "last-analyzed-commit").write_text("abc123\n", encoding="utf-8")
    config = make_config(tmp_path, agent_memory_dir=memory_dir)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    diff_commands = [cmd for cmd, _cwd in runner.commands if cmd[:3] == ["git", "diff", "--name-only"]]
    assert ["git", "diff", "--name-only", "abc123..def456"] in diff_commands
    prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    assert "src/coding_review_agent_loop/prompts.py" in prompt
    assert "tests/test_agent_loop.py" in prompt
    assert (memory_dir / "last-analyzed-commit").read_text(encoding="utf-8") == "def456\n"


def test_agent_memory_logs_when_changed_file_diff_falls_back(tmp_path, capsys):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        git_head="def456",
        diff_returncode=128,
        diff_stderr="fatal: bad revision 'abc123..def456'",
    )
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "last-analyzed-commit").write_text("abc123\n", encoding="utf-8")
    config = make_config(tmp_path, agent_memory_dir=memory_dir, quiet=False)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    captured = capsys.readouterr()
    assert "Could not diff agent memory baseline abc123..def456" in captured.err
    assert "treating all tracked files as changed" in captured.err
    prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    assert "README.md" in prompt


def test_test_profile_records_provided_test_command(tmp_path):
    runner = FakeRunner(codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"])
    memory_dir = tmp_path / "memory"
    config = make_config(
        tmp_path,
        agent_memory_dir=memory_dir,
        test_command=("python", "-m", "pytest", "-q"),
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    profile = (memory_dir / "test-profile.md").read_text(encoding="utf-8")
    assert "`python -m pytest -q`" in profile
    prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    assert "prefer verified test commands from the execution profile" in prompt


def test_agent_memory_can_be_disabled(tmp_path):
    runner = FakeRunner(codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"])
    memory_dir = tmp_path / "memory"
    config = make_config(tmp_path, agent_memory=False, agent_memory_dir=memory_dir)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert not memory_dir.exists()
    prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    assert "Agent memory context:" not in prompt


def test_pr_loop_requires_all_reviewers_to_approve(tmp_path):
    runner = FakeRunner(
        codex_outputs=["Codex approves.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        claude_outputs=["Claude approves.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude"),
        auto_merge=True,
        test_command=("pytest", "tests/test_agent_loop.py"),
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    agent_commands = [cmd[:2] for cmd, _cwd in runner.commands if cmd[:1] in (["claude"], ["codex"])]
    assert agent_commands == [["codex", "exec"], ["claude", "--print"]]
    assert len(runner.comments) == 2
    commands = [cmd for cmd, _cwd in runner.commands]
    metadata_fetches = [
        cmd
        for cmd in commands
        if cmd[:3] == ["gh", "pr", "view"]
        and "--json" in cmd
        and cmd[cmd.index("--json") + 1]
        == "number,title,headRefName,baseRefName,headRefOid,url,comments,reviews"
    ]
    assert len(metadata_fetches) == 1
    assert ["pytest", "tests/test_agent_loop.py"] in commands
    assert ["gh", "pr", "merge", "77", "--repo", "OWNER/REPO", "--merge"] in commands


def test_pr_loop_ignores_approved_followups_by_default(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "LGTM.\n\n### Non-blocking follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
    )
    config = make_config(tmp_path)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments == [
        "**Review verdict:** Approved\n\n"
        "LGTM.\n\n### Future follow-ups\n- Add cleanup docs.\n"
        "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    ]


def test_pr_loop_summarizes_approved_followups_from_multiple_reviewers(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex approves.\n\n### Non-blocking follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        claude_outputs=[
            "Claude approves.\n\n### Non-blocking follow-ups\n- Add regression coverage.\n"
            "<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude"),
        approved_followups="summarize",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.comments) == 3
    summary = runner.comments[-1]
    assert summary.startswith("Approved-review future follow-ups for PR #77:")
    assert "- Add cleanup docs. (Codex)" in summary
    assert "- Add regression coverage. (Claude)" in summary
    assert "future work and did not block merge readiness" in summary
    assert summary.endswith("-- coding-review-agent-loop")


def test_pr_loop_creates_issues_for_approved_followups(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex approves.\n\n### Non-blocking follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        claude_outputs=[
            "Claude approves.\n\n### Non-blocking follow-ups\n- Add regression coverage.\n"
            "<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude"),
        approved_followups="issue",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.comments) == 3
    assert runner.issues == [
        {
            "title": "Follow up future review note: Add cleanup docs.",
            "body": (
                "Future follow-up from approved review on PR #77.\n\n"
                "Reviewer: Codex\n\n"
                "Follow-up:\n"
                "- Add cleanup docs.\n\n"
                "Original reviewer notes:\n"
                "- Codex: Add cleanup docs.\n\n"
                "This was mentioned in an approved review as future work and did not block merge readiness."
            ),
        },
        {
            "title": "Follow up future review note: Add regression coverage.",
            "body": (
                "Future follow-up from approved review on PR #77.\n\n"
                "Reviewer: Claude\n\n"
                "Follow-up:\n"
                "- Add regression coverage.\n\n"
                "Original reviewer notes:\n"
                "- Claude: Add regression coverage.\n\n"
                "This was mentioned in an approved review as future work and did not block merge readiness."
            ),
        },
    ]
    issue_summary = runner.comments[-1]
    assert issue_summary.startswith("Created approved-review future follow-up issues for PR #77:")
    assert "- https://github.com/OWNER/REPO/issues/99" in issue_summary
    assert issue_summary.count("https://github.com/OWNER/REPO/issues/99") == 1
    assert "future work and did not block merge readiness" in issue_summary
    assert issue_summary.endswith("-- coding-review-agent-loop")


def test_pr_loop_deduplicates_approved_followup_issues_across_reviewers(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex approves.\n\n### Future follow-ups\n"
            "- **Remote validation**: Validate explicit workdir git remotes against the target repo.\n"
            "- Add a distinct dry-run smoke test.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        claude_outputs=[
            "Claude approves.\n\n### Future follow-ups\n"
            "- **Remote validation**: Validate explicit workdir git remotes against the target repo.\n"
            "- Document cache cleanup behavior.\n"
            "<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"
        ],
        issue_urls=[
            "https://github.com/OWNER/REPO/issues/99",
            "https://github.com/OWNER/REPO/issues/100",
            "https://github.com/OWNER/REPO/issues/101",
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude"),
        approved_followups="issue",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert [issue["title"] for issue in runner.issues] == [
        "Follow up future review note: **Remote validation**: Validate explicit workdir git remotes against the target repo.",
        "Follow up future review note: Add a distinct dry-run smoke test.",
        "Follow up future review note: Document cache cleanup behavior.",
    ]
    remote_body = runner.issues[0]["body"]
    assert "Reviewers:\n- Codex\n- Claude" in remote_body
    assert "Original reviewer notes:" in remote_body
    assert "- Codex: **Remote validation**" in remote_body
    assert "- Claude: **Remote validation**" in remote_body
    issue_summary = runner.comments[-1]
    assert "- https://github.com/OWNER/REPO/issues/99" in issue_summary
    assert "- https://github.com/OWNER/REPO/issues/100" in issue_summary
    assert "- https://github.com/OWNER/REPO/issues/101" in issue_summary


def test_reconcile_approved_followups_groups_semantic_duplicates_and_preserves_distinct_items():
    reconciliation = reconcile_approved_followups(
        [
            ApprovedFollowup(
                reviewer="Claude",
                text="Clarify repair-pass ownership across the flowchart and sequence diagram.",
            ),
            ApprovedFollowup(
                reviewer="Gemini",
                text="Document repair pass ownership in the flowchart and sequence diagram so the handoff is clear.",
            ),
            ApprovedFollowup(
                reviewer="Codex",
                text="Add memory freshness checks before planning starts.",
            ),
            ApprovedFollowup(
                reviewer="Claude",
                text="Add sync-before-planning coverage for reviewer workdirs.",
            ),
        ],
        issue_limit=MAX_APPROVED_FOLLOWUP_ISSUES,
    )

    assert len(reconciliation.groups) == 3
    assert reconciliation.deduplicated_count == 1
    assert reconciliation.skipped_by_cap == 0
    grouped_reviewers = [group.reviewers for group in reconciliation.groups]
    assert ("Claude", "Gemini") in grouped_reviewers
    assert any("memory freshness" in group.text for group in reconciliation.groups)
    assert any("sync-before-planning" in group.text for group in reconciliation.groups)


def test_reconcile_approved_followups_selects_more_specific_canonical_wording_and_caps():
    reconciliation = reconcile_approved_followups(
        [
            ApprovedFollowup(reviewer="Claude", text="Clarify repair-pass ownership."),
            ApprovedFollowup(
                reviewer="Gemini",
                text="Clarify repair-pass ownership in `docs/local_agent_loop.md` and the sequence diagram.",
            ),
            ApprovedFollowup(reviewer="Codex", text="Follow up two."),
            ApprovedFollowup(reviewer="Claude", text="Follow up three."),
            ApprovedFollowup(reviewer="Gemini", text="Follow up four."),
        ],
        issue_limit=3,
    )

    assert reconciliation.groups[0].text == (
        "Clarify repair-pass ownership in `docs/local_agent_loop.md` and the sequence diagram."
    )
    assert len(reconciliation.selected_groups) == 3
    assert reconciliation.skipped_by_cap == 1
    assert reconciliation.deduplicated_count == 1


def test_pr_loop_files_earlier_future_followup_not_repeated_in_final_round(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            structured_pr_review(
                state="approved",
                summary="Codex approves with a later cleanup.",
                future_followups=["Add memory freshness checks before planning starts."],
                reviewer="OpenAI Codex",
            ),
            structured_pr_review(
                state="approved",
                summary="Codex final approval.",
                prior_item_dispositions=[
                    {
                        "item_id": "item-1",
                        "disposition": "future",
                        "note": "Still useful as separate tracking.",
                    },
                    {"item_id": "item-2", "disposition": "resolved"},
                ],
                reviewer="OpenAI Codex",
            ),
        ],
        claude_outputs=[
            structured_pr_review(
                state="blocking",
                summary="Need one current-PR fix.",
                blocking_items=["Fix the current sync regression."],
                reviewer="Anthropic Claude",
            ),
            structured_coder_followup(
                addressed_items=["item-2"],
                remaining_items=["item-1"],
                reviewer="Anthropic Claude",
            ),
            structured_pr_review(
                state="approved",
                summary="Claude final approval.",
                prior_item_dispositions=[
                    {"item_id": "item-1", "disposition": "future", "note": "Still valid."},
                    {"item_id": "item-2", "disposition": "resolved"},
                ],
                reviewer="Anthropic Claude",
            ),
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude"),
        approved_followups="issue",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.issues) == 1
    assert runner.issues[0]["title"] == (
        "Follow up future review note: Add memory freshness checks before planning starts."
    )
    assert "Update from Codex: Still useful as separate tracking." in runner.issues[0]["body"]


def test_pr_loop_does_not_file_resolved_earlier_future_followup(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            structured_pr_review(
                state="approved",
                summary="Codex approves with a later cleanup.",
                future_followups=["Remove stale final-round-only follow-up handling."],
                reviewer="OpenAI Codex",
            ),
            structured_pr_review(
                state="approved",
                summary="Codex final approval.",
                prior_item_dispositions=[
                    {
                        "item_id": "item-1",
                        "disposition": "resolved",
                        "note": "Fixed in the second commit.",
                    },
                    {"item_id": "item-2", "disposition": "resolved"},
                ],
                reviewer="OpenAI Codex",
            ),
        ],
        claude_outputs=[
            structured_pr_review(
                state="blocking",
                summary="Need one current-PR fix.",
                blocking_items=["Fix the current sync regression."],
                reviewer="Anthropic Claude",
            ),
            structured_coder_followup(
                addressed_items=["item-2"],
                remaining_items=["item-1"],
                reviewer="Anthropic Claude",
            ),
            structured_pr_review(
                state="approved",
                summary="Claude final approval.",
                prior_item_dispositions=[
                    {"item_id": "item-1", "disposition": "resolved", "note": "Fixed."},
                    {"item_id": "item-2", "disposition": "resolved"},
                ],
                reviewer="Anthropic Claude",
            ),
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude"),
        approved_followups="issue",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.issues == []
    assert not any(comment.startswith("Created approved-review future follow-up issues") for comment in runner.comments)


def test_pr_loop_semantically_deduplicates_followup_issues_and_keeps_provenance(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            structured_pr_review(
                state="approved",
                summary="Codex approves.",
                reviewer="OpenAI Codex",
            )
        ],
        claude_outputs=[
            structured_pr_review(
                state="approved",
                summary="Claude approves.",
                future_followups=[
                    "Clarify repair-pass ownership across the flowchart and sequence diagram."
                ],
                reviewer="Anthropic Claude",
            )
        ],
        gemini_outputs=[
            structured_pr_review(
                state="approved",
                summary="Gemini approves.",
                future_followups=[
                    "Document repair pass ownership in the flowchart and sequence diagram so the handoff is clear."
                ],
                reviewer="Google Gemini",
            )
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude", "gemini"),
        approved_followups="issue",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.issues) == 1
    body = runner.issues[0]["body"]
    assert "Reviewers:\n- Claude\n- Gemini" in body
    assert "Original reviewer notes:" in body
    assert "- Claude: Clarify repair-pass ownership" in body
    assert "- Gemini: Document repair pass ownership" in body
    assert "Reconciliation: 1 filed, 1 deduplicated, 0 skipped by cap." in runner.comments[-1]


def test_pr_loop_suppresses_followup_issue_summary_when_no_urls_returned(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex approves.\n\n### Future follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
        issue_urls=[None],
    )
    config = make_config(tmp_path, approved_followups="issue")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.comments) == 1
    assert len(runner.issues) == 1


def test_pr_loop_creates_no_issues_without_approved_followups(tmp_path):
    runner = FakeRunner(
        codex_outputs=["Codex approves.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    config = make_config(tmp_path, approved_followups="issue")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.comments) == 1
    assert runner.issues == []


def test_pr_loop_logs_created_followup_issue_url(tmp_path, capsys):
    runner = FakeRunner(
        codex_outputs=[
            "Codex approves.\n\n### Future follow-ups\n- Add cleanup docs.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
    )
    config = make_config(tmp_path, approved_followups="issue", quiet=False)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    captured = capsys.readouterr()
    assert "Created GitHub issue: https://github.com/OWNER/REPO/issues/99" in captured.err


@pytest.mark.parametrize("mode", ["summarize", "issue"])
def test_pr_loop_treats_same_pr_followups_as_blocking_without_fix_mode(tmp_path, mode):
    runner = FakeRunner(
        codex_outputs=[
            "Codex found cleanup.\n\n"
            "### Same-PR follow-ups\n"
            "- Rename the helper before merge.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
        ],
    )
    config = make_config(tmp_path, approved_followups=mode, max_rounds=1)

    with pytest.raises(AgentLoopError, match="still reported blocking"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert len(runner.comments) == 1
    assert not runner.issues
    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


@pytest.mark.parametrize("mode", ["summarize", "issue"])
def test_pr_loop_treats_same_pr_prose_followups_as_blocking_without_fix_mode(tmp_path, mode):
    runner = FakeRunner(
        codex_outputs=[
            "Codex found cleanup.\n\n"
            "### Same-PR follow-ups\n"
            "Rename the helper before merge.\n"
            "Keep the behavior unchanged.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex"
        ],
    )
    config = make_config(tmp_path, approved_followups=mode, max_rounds=1)

    with pytest.raises(AgentLoopError, match="still reported blocking"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert len(runner.comments) == 1
    assert not runner.issues
    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


def test_pr_loop_caps_approved_followup_issues(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex approves.\n\n### Non-blocking follow-ups\n"
            "- Follow up one.\n"
            "- Follow up two.\n"
            "- Follow up three.\n"
            "- Follow up four.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
    )
    config = make_config(tmp_path, approved_followups="issue")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert [issue["title"] for issue in runner.issues] == [
        "Follow up future review note: Follow up one.",
        "Follow up future review note: Follow up two.",
        "Follow up future review note: Follow up three.",
    ]
    assert len(runner.comments) == 2
    issue_summary = runner.comments[-1]
    assert "- https://github.com/OWNER/REPO/issues/99" in issue_summary
    assert "Skipped 1 additional item(s) to avoid issue noise" in issue_summary
    assert issue_summary.endswith("-- coding-review-agent-loop")


def test_pr_loop_fix_and_summarize_sends_same_pr_followups_to_coder_then_rereviews(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "### Same-PR follow-ups\n"
            "- Rename the helper before merge.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Codex approves final pass.\n\n"
            "### Future follow-ups\n"
            "- Add broader integration coverage later.\n"
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=["Renamed helper.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(tmp_path, approved_followups="fix-and-summarize")
    issue_context = IssueContext(
        number=56,
        repo="OWNER/REPO",
        title="Support issue comments",
        body="Original request.",
        url="https://github.com/OWNER/REPO/issues/56",
        comments=(
            IssueComment(
                author="commenter",
                created_at="2026-05-17T10:00:00Z",
                body="Clarifying issue comment.",
            ),
        ),
    )

    assert run_pr_loop(runner, pr_number=77, config=config, issue_context=issue_context) == 0

    agent_commands = [cmd[:2] for cmd, _cwd in runner.commands if cmd[:1] in (["claude"], ["codex"])]
    assert agent_commands == [["codex", "exec"], ["claude", "--print"], ["codex", "exec"]]
    assert len(runner.comments) == 4
    followup_prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["claude"])
    assert "requested same-PR follow-ups" in followup_prompt
    assert "remains blocked pending another review round" in followup_prompt
    assert "Rename the helper before merge." in followup_prompt
    assert "[item-1]" in followup_prompt
    assert "Issue context from GitHub" in followup_prompt
    assert "Title:\nSupport issue comments" in followup_prompt
    assert "Clarifying issue comment." in followup_prompt
    assert "small, localized cleanup for the\ncurrent PR" in followup_prompt
    assert "Keep the change narrowly scoped to the listed items" in followup_prompt
    assert "Do not take on\nlarger redesigns or unrelated future work" in followup_prompt
    assert "Add broader integration coverage later." in runner.comments[-1]


def test_pr_loop_fix_and_issue_uses_final_round_future_followups_after_same_pr_cleanup(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "### Same-PR follow-ups\n"
            "- Tighten the validation message.\n\n"
            "### Future follow-ups\n"
            "- Stale future item from the blocking round.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Codex approves final pass.\n\n"
            "### Future follow-ups\n"
            "- Add a separate migration dry-run command.\n"
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=["Tightened message.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(tmp_path, approved_followups="fix-and-issue")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.issues) == 1
    assert runner.issues[0]["title"] == "Follow up future review note: Add a separate migration dry-run command."
    assert "Stale future item from the blocking round." not in runner.issues[0]["body"]
    commands = [cmd[:3] for cmd, _cwd in runner.commands]
    assert commands.count(["gh", "issue", "create"]) == 1


def test_pr_loop_fix_and_issue_drops_blocking_round_future_followups(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "### Same-PR follow-ups\n"
            "- Tighten the validation message.\n\n"
            "### Future follow-ups\n"
            "- Stale future item from the blocking round.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Codex approves final pass.\n\n"
            "### Future follow-ups\n"
            "- Add a separate migration dry-run command.\n"
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=["Tightened message.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(tmp_path, approved_followups="fix-and-issue")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.issues) == 1
    assert "Stale future item from the blocking round." not in runner.issues[0]["body"]


def test_pr_loop_fix_and_issue_uses_only_final_round_future_followups(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex found cleanup.\n\n"
            "### Same-PR follow-ups\n"
            "- Tighten the validation message.\n\n"
            "### Future follow-ups\n"
            "- Stale item fixed by the same-PR pass.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Codex approves final pass.\n\n"
            "### Future follow-ups\n"
            "- Add a separate migration dry-run command.\n"
            + prior_item_dispositions(
                "[item-1] resolved",
                "[item-2] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=["Tightened message.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(tmp_path, approved_followups="fix-and-issue")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.issues) == 1
    assert runner.issues[0]["title"] == "Follow up future review note: Add a separate migration dry-run command."
    assert "Stale item fixed by the same-PR pass." not in runner.issues[0]["body"]
    assert "- https://github.com/OWNER/REPO/issues/99" in runner.comments[-1]
    commands = [cmd[:3] for cmd, _cwd in runner.commands]
    assert commands.count(["gh", "issue", "create"]) == 1


def test_pr_loop_fix_and_summarize_uses_only_final_round_future_followups(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex found cleanup.\n\n"
            "### Same-PR follow-ups\n"
            "- Add a small assertion before merge.\n\n"
            "### Future follow-ups\n"
            "- Add Codex's larger follow-up later.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Codex approves final pass.\n\n"
            "### Future follow-ups\n"
            "- Add Codex's final follow-up later.\n"
            + prior_item_dispositions(
                "[item-1] resolved",
                "[item-2] resolved",
                "[item-3] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "Claude approves.\n\n"
            "### Future follow-ups\n"
            "- Add Claude's larger follow-up later.\n"
            "<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
            "Claude approves final pass.\n\n"
            "### Future follow-ups\n"
            "- Add Claude's final follow-up later.\n"
            + prior_item_dispositions(
                "[item-1] resolved",
                "[item-2] resolved",
                "[item-3] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
        gemini_outputs=["Added assertion.\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini"],
    )
    config = make_config(
        tmp_path,
        coder="gemini",
        reviewer=("codex", "claude"),
        approved_followups="fix-and-summarize",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    agent_commands = [cmd[:2] for cmd, _cwd in runner.commands if cmd[:1] in (["claude"], ["codex"], ["gemini"])]
    assert agent_commands == [
        ["codex", "exec"],
        ["claude", "--print"],
        ["gemini", "--prompt"],
        ["codex", "exec"],
        ["claude", "--print"],
    ]
    summary = runner.comments[-1]
    assert "- Add Codex's final follow-up later. (Codex)" in summary
    assert "- Add Claude's final follow-up later. (Claude)" in summary
    assert "Add Codex's larger follow-up later." not in summary
    assert "Add Claude's larger follow-up later." not in summary


def test_pr_loop_fix_and_issue_extracts_final_round_bullet_and_prose_future_followups(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex found cleanup.\n\n"
            "### Same-PR follow-ups\n"
            "- Tighten the validation message.\n\n"
            "### Future follow-ups\n"
            "- Stale Codex item fixed by the same-PR pass.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Codex approves final pass.\n\n"
            "### Future follow-ups\n"
            "- Refine token estimation for large review prompts.\n"
            + prior_item_dispositions(
                "[item-1] resolved",
                "[item-2] resolved",
                "[item-3] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "Claude approves with cleanup.\n\n"
            "### Future follow-ups\n"
            "- Stale Claude item fixed by the same-PR pass.\n"
            "<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
            "Claude approves final pass.\n\n"
            "### Future follow-ups\n"
            "The `_parse_gemini_output` helper is dead production code and could be removed\n"
            "in a future cleanup.\n\n"
            "### Same-PR follow-ups\n"
            "No same-PR follow-ups.\n"
            + prior_item_dispositions(
                "[item-1] resolved",
                "[item-2] resolved",
                "[item-3] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
        gemini_outputs=["Tightened message.\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini"],
        issue_urls=[
            "https://github.com/OWNER/REPO/issues/99",
            "https://github.com/OWNER/REPO/issues/100",
        ],
    )
    config = make_config(
        tmp_path,
        coder="gemini",
        reviewer=("codex", "claude"),
        approved_followups="fix-and-issue",
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.issues[0]["title"] == (
        "Follow up future review note: Refine token estimation for large review prompts."
    )
    assert runner.issues[1]["title"].startswith(
        "Follow up future review note: The `_parse_gemini_output` helper is dead production code"
    )
    assert "could be removed in a future cleanup." in runner.issues[1]["body"]
    assert "Stale Codex item fixed by the same-PR pass." not in runner.issues[0]["body"]
    assert "Stale Claude item fixed by the same-PR pass." not in runner.issues[1]["body"]
    issue_summary = runner.comments[-1]
    assert "- https://github.com/OWNER/REPO/issues/99" in issue_summary
    assert "- https://github.com/OWNER/REPO/issues/100" in issue_summary
    assert "Stale Codex item fixed by the same-PR pass." not in issue_summary


def test_pr_loop_reruns_all_reviewers_when_any_reviewer_blocks(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Needs a regression test.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "Addressed review.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "Claude approves."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Codex approves first pass.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
            "Codex approves second pass."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, coder="claude", reviewer=("claude", "codex"))

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert len(runner.comments) == 5
    followup_prompt = next(
        cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["claude"] and "Address the review below" in cmd[-1]
    )
    assert "Needs a regression test." in followup_prompt
    assert "Codex approves first pass." not in followup_prompt
    commands = [cmd for cmd, _cwd in runner.commands]
    metadata_fetches = [
        cmd
        for cmd in commands
        if cmd[:3] == ["gh", "pr", "view"]
        and "--json" in cmd
        and cmd[cmd.index("--json") + 1]
        == "number,title,headRefName,baseRefName,headRefOid,url,comments,reviews"
    ]
    assert len(metadata_fetches) == 2


def test_pr_loop_rejects_cross_reviewer_approval_without_prior_item_disposition(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Needs a regression test.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "Claude resolves it."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Codex approves first pass.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
            "Codex approves second pass.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        gemini_outputs=["Implemented fix.\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini"],
    )
    config = make_config(tmp_path, coder="gemini", reviewer=("claude", "codex"), max_rounds=2)

    with pytest.raises(AgentLoopError, match="did not evaluate all prior unresolved items: item-1"):
        run_pr_loop(runner, pr_number=77, config=config)

    second_codex_prompt = [
        cmd[-1]
        for cmd, _cwd in runner.commands
        if cmd[:2] == ["codex", "exec"] and "round 2" in cmd[-1]
    ][0]
    assert "Prior unresolved review items from earlier rounds" in second_codex_prompt
    assert "[item-1] blocking from Claude in round 1" in second_codex_prompt


def test_pr_loop_can_downgrade_prior_blocker_to_future_followup_only_in_approved_review(tmp_path):
    runner = FakeRunner(
        claude_outputs=["Addressed review.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
        codex_outputs=[
            "Missing docs cleanup.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM now."
            + prior_item_dispositions("[item-1] future follow-up: cleanup can wait")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, approved_followups="summarize", max_rounds=2)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    summary = runner.comments[-1]
    assert summary.startswith("Approved-review future follow-ups for PR #77:")
    assert "Missing docs cleanup." in summary


def test_pr_loop_persists_downgraded_future_followup_across_later_blocking_rounds(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Still blocked.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "LGTM."
            + prior_item_dispositions("[item-1] future follow-up: cleanup can wait", "[item-2] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Missing docs cleanup.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Implemented fix for Claude.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Looks good."
            + prior_item_dispositions("[item-1] future follow-up: cleanup can wait", "[item-2] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude"),
        coder="codex",
        approved_followups="summarize",
        max_rounds=2,
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    summary = runner.comments[-1]
    assert summary.startswith("Approved-review future follow-ups for PR #77:")
    assert "Missing docs cleanup." in summary


def test_pr_loop_finalized_future_followup_summary_preserves_disposition_notes(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Still blocked.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "Claude approves final pass."
            + prior_item_dispositions(
                "[item-1] future follow-up: cleanup can wait until after rollout",
                "[item-2] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Missing docs cleanup.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Implemented blocker.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Codex approves final pass."
            + prior_item_dispositions(
                "[item-1] future follow-up: cleanup can wait until after rollout",
                "[item-2] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(
        tmp_path,
        reviewer=("codex", "claude"),
        coder="codex",
        approved_followups="summarize",
        max_rounds=2,
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    summary = runner.comments[-1]
    assert "Missing docs cleanup." in summary
    assert "Update from Codex: cleanup can wait until after rollout" in summary


def test_pr_loop_carries_new_future_followups_into_later_reviewer_prompts(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Codex approves with future work.\n\n"
            "### Future follow-ups\n"
            "- Document cache cleanup behavior.\n"
            "<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
            "Codex approves final pass."
            + prior_item_dispositions(
                "[item-1] future follow-up: still future work",
                "[item-2] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "Claude still blocks.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "Claude approves final pass."
            + prior_item_dispositions(
                "[item-1] future follow-up: still future work",
                "[item-2] resolved",
            )
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
        gemini_outputs=["Implemented blocker.\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini"],
    )
    config = make_config(
        tmp_path,
        coder="gemini",
        reviewer=("codex", "claude"),
        approved_followups="summarize",
        max_rounds=2,
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    second_claude_prompt = [
        cmd[-1]
        for cmd, _cwd in runner.commands
        if cmd[:1] == ["claude"] and "round 2" in cmd[-1]
    ][0]
    assert "Document cache cleanup behavior." in second_claude_prompt
    assert "[item-1] future" in second_claude_prompt
    summary = runner.comments[-1]
    assert summary.startswith("Approved-review future follow-ups for PR #77:")
    assert "Document cache cleanup behavior." in summary


def test_pr_loop_carries_prior_item_notes_without_creating_duplicate_blocker_items(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Needs regression coverage.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Still blocked."
            + prior_item_dispositions("[item-1] still blocking: include API error path too")
            + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Looks good."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "Added coverage.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "Expanded coverage.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path, max_rounds=3)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    second_coder_prompt = [cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]][1]
    assert "Latest reviewer updates:" in second_coder_prompt
    assert "Codex: include API error path too" in second_coder_prompt
    assert "[item-2]" not in second_coder_prompt


def test_pr_loop_posts_human_readable_item_labels_in_new_and_prior_sections(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Implemented the requested PR body change.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "### Same-PR follow-ups\n"
            "- Require source issue reference in PR body.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Looks good."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", approved_followups="fix-and-summarize", max_rounds=2)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert runner.comments[0] == (
        "**Review verdict:** Blocking\n\n"
        "### Same-PR follow-ups\n"
        "- Require source issue reference in PR body.\n"
        "<!-- AGENT_STATE: blocking -->\n"
        "-- OpenAI Codex"
    )
    assert runner.comments[2] == (
        "**Review verdict:** Approved\n\n"
        "Looks good.\n\n"
        "### Prior unresolved item dispositions\n"
        "- [item-1] Same-PR follow-up from OpenAI Codex, round 1: Require source issue reference in PR body. -> resolved\n"
        "<!-- AGENT_STATE: approved -->\n"
        "-- OpenAI Codex"
    )


def test_pr_loop_tracks_only_summary_when_blocking_items_phrase_the_issue_differently(tmp_path):
    runner = FakeRunner(
        claude_outputs=["Implemented fixes.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
        codex_outputs=[
            "Needs one more regression test before merge."
            + blocking_issues("Add the mixed-history resume case to `tests/test_agent_loop.py`.")
            + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Looks good."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", max_rounds=2)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    second_coder_prompt = [cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]][0]
    assert "Needs one more regression test before merge." in second_coder_prompt
    assert "Add the mixed-history resume case" not in second_coder_prompt
    assert runner.comments[0] == (
        "**Review verdict:** Blocking\n\n"
        "Needs one more regression test before merge.\n\n"
        "### Blocking issues\n"
        "- Add the mixed-history resume case to `tests/test_agent_loop.py`.\n"
        "<!-- AGENT_STATE: blocking -->\n"
        "-- OpenAI Codex"
    )


def test_resume_pr_round_reparses_orchestrator_rendered_blocking_issues_comment():
    carried_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Need one more regression test before merge.",
        status="blocking",
        source_status="blocking",
    )
    rendered_review = _render_public_pr_review_comment(
        parse_review(
            "Need one more regression test before merge."
            + blocking_issues("Exercise the structured-resume path.")
            + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            reviewer="OpenAI Codex",
        ),
        reviewer="Codex",
        human_requirements_resolved_flag=False,
        prior_items=(),
        dispositions=(),
    )
    review_comment = _attach_round_metadata(
        rendered_review,
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Codex",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
            dispositions=(),
            new_items=(),
            state="blocking",
        ),
    )
    coder_comment = _attach_round_metadata(
        "Addressed the review.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
        ),
    )

    resumed = _resume_pr_round(
        [
            IssueComment(author="bot", created_at="2026-05-25T00:00:00Z", body=coder_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:01:00Z", body=review_comment),
        ],
        head_sha="abc123",
        configured_reviewers=("codex",),
    )

    assert resumed is not None
    resumed_review = parse_review(resumed.completed_reviews[0].body, reviewer="Codex")
    assert [item.text for item in resumed_review.blocking_items] == [
        "Exercise the structured-resume path."
    ]
    assert resumed_review.summary == "Need one more regression test before merge."


def test_resume_pr_round_prefers_structured_coder_followup_metadata():
    carried_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Need one more regression test before merge.",
        status="blocking",
        source_status="blocking",
    )
    raw_structured_followup = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Added the requested regression test.",
                "addressed_items": ["item-1"],
                "remaining_items": [],
                "human_requirements": {
                    "addressed_ids": ["Requirement 1"],
                    "checked_discussion_directly": False,
                },
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
    )
    parsed = validate_structured_coder_followup(raw_structured_followup)
    assert parsed is not None
    public_comment = _render_public_coder_followup_comment(parsed, signature="Anthropic Claude")
    coder_comment = _attach_round_metadata(
        public_comment,
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
            raw_structured_coder_response=raw_structured_followup,
        ),
    )

    resumed = _resume_pr_round(
        [IssueComment(author="bot", created_at="2026-05-25T00:00:00Z", body=coder_comment)],
        head_sha="abc123",
        configured_reviewers=("codex",),
    )

    assert resumed is not None
    assert resumed.coder_output == raw_structured_followup
    resumed_followup = validate_structured_coder_followup(resumed.coder_output)
    assert resumed_followup is not None
    assert resumed_followup.human_requirements.addressed_ids == ("Requirement 1",)
    assert '"kind": "coder_followup"' not in _strip_round_metadata(coder_comment)


def test_resume_pr_round_prefers_latest_metadata_ledger_for_same_head_replay():
    stale_item = UnresolvedReviewItem(
        item_id="item-3",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Stale replay item.",
        status="blocking",
        source_status="blocking",
    )
    active_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Active replay item.",
        status="blocking",
        source_status="blocking",
    )
    stale_coder_comment = _attach_round_metadata(
        "Stale replay.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=2,
            subject="abc123",
            prior_items=(stale_item,),
        ),
    )
    stale_reviewer_comment = _attach_round_metadata(
        "Still blocked."
        + prior_item_dispositions("[item-3] still blocking: stale replay")
        + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Codex",
            round_number=2,
            subject="abc123",
            prior_items=(stale_item,),
            dispositions=(
                parse_unresolved_item_dispositions(
                    prior_item_dispositions("[item-3] still blocking: stale replay"),
                    reviewer="OpenAI Codex",
                )[0],
            ),
            state="blocking",
        ),
    )
    active_coder_comment = _attach_round_metadata(
        "Current replay.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=2,
            subject="abc123",
            prior_items=(active_item,),
        ),
    )
    active_reviewer_comment = _attach_round_metadata(
        "Looks good."
        + prior_item_dispositions("[item-1] resolved")
        + "\n<!-- AGENT_STATE: approved -->\n-- Google Gemini",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Gemini",
            round_number=2,
            subject="abc123",
            prior_items=(active_item,),
            dispositions=(
                parse_unresolved_item_dispositions(
                    prior_item_dispositions("[item-1] resolved"),
                    reviewer="Google Gemini",
                )[0],
            ),
            state="approved",
        ),
    )
    previous_head_comment = _attach_round_metadata(
        "Older head.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=4,
            subject="old-head",
            prior_items=(
                UnresolvedReviewItem(
                    item_id="item-9",
                    reviewer="OpenAI Codex",
                    source_round=3,
                    text="Older head item.",
                    status="blocking",
                ),
            ),
        ),
    )

    resumed = _resume_pr_round(
        [
            IssueComment(author="bot", created_at="2026-05-25T00:00:00Z", body=previous_head_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:01:00Z", body=stale_coder_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:02:00Z", body=stale_reviewer_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:03:00Z", body=active_coder_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:04:00Z", body=active_reviewer_comment),
        ],
        head_sha="abc123",
        configured_reviewers=("codex", "gemini"),
    )

    assert resumed is not None
    assert [item.item_id for item in resumed.prior_items] == ["item-1"]
    assert resumed.next_unresolved_item_number == 4
    assert [record.metadata.agent for record in resumed.completed_reviews] == ["Gemini"]


def test_pr_loop_resume_hybrid_history_prefers_metadata_ledger_over_legacy_markdown(tmp_path):
    carried_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Add a regression test before merge.",
        status="blocking",
        source_status="blocking",
    )
    legacy_comment = (
        "Legacy raw markdown review.\n\n"
        "### Blocking issues\n"
        "- Keep the legacy fallback path.\n"
        "<!-- AGENT_STATE: blocking -->\n"
        "-- OpenAI Codex"
    )
    coder_comment = _attach_round_metadata(
        "Updated the PR.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
        ),
    )
    codex_comment = _attach_round_metadata(
        "Looks good."
        + prior_item_dispositions("[item-1] resolved")
        + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Codex",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
            dispositions=(
                parse_unresolved_item_dispositions(
                    prior_item_dispositions("[item-1] resolved"),
                    reviewer="OpenAI Codex",
                )[0],
            ),
            state="approved",
        ),
    )
    runner = FakeRunner(
        gemini_outputs=[
            "Ship it."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
        ],
        pr_payload={
            "comments": [
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:00:00Z", "body": legacy_comment},
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:05:00Z", "body": coder_comment},
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:06:00Z", "body": codex_comment},
            ],
        },
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"))

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    gemini_prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["gemini"])
    assert "[item-1]" in gemini_prompt
    assert "Add a regression test before merge." in gemini_prompt
    assert "Keep the legacy fallback path." not in gemini_prompt


def test_reconcile_human_requirements_ack_item_accepts_stored_structured_coder_followup():
    human_requirements = (
        HumanReviewRequirement(
            source_type="PR comment",
            author="reviewer",
            created_at="2026-05-18T10:00:00Z",
            url="https://github.com/OWNER/REPO/pull/77#issuecomment-1",
            body="Please use the absolute URL.",
        ),
    )
    structured_followup = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Implemented the requested URL fix.",
                "addressed_items": ["item-1"],
                "remaining_items": [],
                "human_requirements": {
                    "addressed_ids": ["Requirement 1"],
                    "checked_discussion_directly": False,
                },
            }
        )
        + "\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
    )

    reconciled = _reconcile_human_requirements_ack_item(
        (
            UnresolvedReviewItem(
                item_id=HUMAN_REQUIREMENTS_ACK_ITEM_ID,
                reviewer="Orchestrator",
                source_round=1,
                text="Ack missing.",
                status="blocking",
            ),
        ),
        coder_output=structured_followup,
        human_requirements=human_requirements,
        source_round=2,
    )

    assert reconciled == []


def test_pr_loop_does_not_expose_same_round_item_ids_to_later_reviewers(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "### Same-PR follow-ups\n"
            "- Require source issue reference in PR body.\n"
            "<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Still blocked.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(
        tmp_path,
        coder="gemini",
        reviewer=("claude", "codex"),
        approved_followups="fix-and-summarize",
        max_rounds=1,
    )

    with pytest.raises(AgentLoopError, match="still reported blocking issues after round 1"):
        run_pr_loop(runner, pr_number=77, config=config)

    second_reviewer_prompt = [
        cmd[-1]
        for cmd, _cwd in runner.commands
        if cmd[:2] == ["codex", "exec"] and "round 1" in cmd[-1]
    ][0]
    assert "Only items listed under `Prior unresolved review items from earlier rounds`" in second_reviewer_prompt
    assert "[item-1]" not in second_reviewer_prompt
    assert "### New tracked unresolved items" not in runner.comments[0]


def test_pr_loop_same_pr_items_remain_blocking_until_explicitly_resolved(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "### Same-PR follow-ups\n"
            "- Rename the helper before merge.\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Codex still wants the rename."
            + prior_item_dispositions("[item-1] same-pr")
            + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
        claude_outputs=["Tried a partial fix.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(tmp_path, approved_followups="fix-and-summarize", max_rounds=2)

    with pytest.raises(AgentLoopError, match="still reported blocking issues after round 2"):
        run_pr_loop(runner, pr_number=77, config=config)


def test_pr_loop_resumes_with_only_missing_reviewer_for_current_head(tmp_path):
    carried_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="Codex",
        source_round=1,
        text="Add a regression test before merge.",
        status="blocking",
        source_status="blocking",
    )
    coder_comment = _attach_round_metadata(
        "Updated the PR with the requested fix.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
        ),
    )
    codex_comment = _attach_round_metadata(
        "Looks good."
        + prior_item_dispositions("[item-1] resolved")
        + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Codex",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
            dispositions=(
                parse_unresolved_item_dispositions(
                    prior_item_dispositions("[item-1] resolved"),
                    reviewer="OpenAI Codex",
                )[0],
            ),
            state="approved",
        ),
    )
    runner = FakeRunner(
        gemini_outputs=[
            "Ship it."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"
        ],
        pr_payload={
            "comments": [
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T10:00:00Z", "body": coder_comment},
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T10:05:00Z", "body": codex_comment},
            ],
        },
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"))

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    agent_commands = [cmd[0] for cmd, _cwd in runner.commands if cmd[:1] in (["claude"], ["codex"], ["gemini"])]
    assert agent_commands == ["gemini"]
    gemini_prompt = next(cmd[-1] for cmd, _cwd in runner.commands if cmd[:1] == ["gemini"])
    assert "[item-1]" in gemini_prompt
    assert "Add a regression test before merge." in gemini_prompt


def test_pr_loop_resume_raises_agent_loop_error_for_missing_reconstructed_prior_item(tmp_path):
    carried_item = UnresolvedReviewItem(
        item_id="item-2",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Actual active carried item.",
        status="blocking",
        source_status="blocking",
    )
    coder_comment = _attach_round_metadata(
        "Updated the PR.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        PostedRoundMetadata(
            flow="pr",
            role="coder",
            agent="Claude",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
        ),
    )
    invalid_disposition = ReviewItemDisposition(
        item_id="item-1",
        reviewer="OpenAI Codex",
        disposition="resolved",
    )
    codex_comment = _attach_round_metadata(
        "Looks good."
        + prior_item_dispositions("[item-1] resolved")
        + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="pr",
            role="reviewer",
            agent="Codex",
            round_number=2,
            subject="abc123",
            prior_items=(carried_item,),
            dispositions=(invalid_disposition,),
            state="approved",
        ),
    )
    runner = FakeRunner(
        pr_payload={
            "comments": [
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T10:00:00Z", "body": coder_comment},
                {"author": {"login": "bot"}, "createdAt": "2026-05-20T10:05:00Z", "body": codex_comment},
            ],
        },
    )
    config = make_config(tmp_path, reviewer=("codex",))

    with pytest.raises(
        AgentLoopError,
        match=r"Resumed pr round 2 reconstructed prior items item-2, but Codex dispositioned unknown item `item-1`",
    ):
        run_pr_loop(runner, pr_number=77, config=config)


@pytest.mark.parametrize(
    "line",
    [
        "[item-1] same-pr: none",
        "[item-1] still blocking: none",
        "[item-1] future follow-up: none",
    ],
)
def test_pr_loop_rejects_contradictory_disposition_before_extra_coder_round(tmp_path, line):
    runner = FakeRunner(
        codex_outputs=[
            "Needs regression coverage.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Looks good overall."
            + prior_item_dispositions(line)
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        claude_outputs=["Added coverage.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(tmp_path, approved_followups="fix-and-summarize", max_rounds=3)

    with pytest.raises(AgentLoopError, match="use `resolved` when nothing remains"):
        run_pr_loop(runner, pr_number=77, config=config)

    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(claude_calls) == 1


def test_pr_loop_does_not_run_claude_after_final_blocking_round(tmp_path):
    runner = FakeRunner(codex_outputs=["Still blocked.\n<!-- AGENT_STATE: blocking -->"])
    config = make_config(tmp_path, max_rounds=1)

    with pytest.raises(AgentLoopError, match="still reported blocking"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


def test_shared_workdir_requires_explicit_override(tmp_path):
    runner = FakeRunner()
    shared = tmp_path / "repo"
    shared.mkdir()
    config = make_config(tmp_path, claude_dir=shared, codex_dir=shared)

    with pytest.raises(AgentLoopError, match="same directory"):
        run_pr_loop(runner, pr_number=77, config=config)


def test_gemini_shared_workdir_requires_explicit_override(tmp_path):
    runner = FakeRunner()
    shared = tmp_path / "repo"
    shared.mkdir()
    config = make_config(
        tmp_path,
        reviewer=("codex", "gemini"),
        codex_dir=shared,
        gemini_dir=shared,
    )

    with pytest.raises(AgentLoopError, match="same directory"):
        run_pr_loop(runner, pr_number=77, config=config)


def test_missing_agent_workdirs_are_created(tmp_path):
    runner = FakeRunner(
        claude_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"],
    )
    claude_dir = tmp_path / "missing" / "claude"
    codex_dir = tmp_path / "missing" / "codex"
    config = make_config(
        tmp_path,
        claude_dir=claude_dir,
        codex_dir=codex_dir,
        coder="codex",
        reviewer="claude",
        create_dirs=False,
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0
    assert claude_dir.is_dir()
    assert codex_dir.is_dir()


def test_missing_gemini_workdir_is_created_when_configured(tmp_path):
    runner = FakeRunner(
        gemini_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"],
    )
    gemini_dir = tmp_path / "missing" / "gemini"
    config = make_config(
        tmp_path,
        reviewer="gemini",
        gemini_dir=gemini_dir,
        create_dirs=False,
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0
    assert gemini_dir.is_dir()


def test_non_codex_loop_uses_active_workdir_for_github_and_tests(tmp_path):
    runner = FakeRunner(
        gemini_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"],
    )
    codex_dir = tmp_path / "inactive" / "codex"
    config = make_config(
        tmp_path,
        claude_dir=tmp_path / "missing" / "claude",
        codex_dir=codex_dir,
        gemini_dir=tmp_path / "missing" / "gemini",
        coder="claude",
        reviewer="gemini",
        test_command=("pytest", "tests/test_agent_loop.py"),
        create_dirs=False,
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert not codex_dir.exists()
    github_or_test_cwds = [
        cwd
        for cmd, cwd in runner.commands
        if cmd[:1] == ["gh"] or cmd == ["pytest", "tests/test_agent_loop.py"]
    ]
    assert github_or_test_cwds
    assert set(github_or_test_cwds) == {config.claude_dir}


def test_omitted_agent_dirs_default_to_repo_scoped_temp_checkouts(monkeypatch, tmp_path):
    parser = build_parser()
    cache_home = tmp_path / "cache"
    monkeypatch.setattr("coding_review_agent_loop.config.sys.platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    args = parser.parse_args([
        "task",
        "Fix the bug",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "codex",
        "--reviewer",
        "claude",
    ])

    config = config_from_args(args, FakeRunner())

    assert config.codex_dir == default_agent_workdir("OWNER/REPO", "codex").resolve()
    assert config.claude_dir == default_agent_workdir("OWNER/REPO", "claude").resolve()
    assert config.gemini_dir == default_agent_workdir("OWNER/REPO", "gemini").resolve()
    assert set(config.auto_agent_dirs) == {"claude", "codex", "gemini"}
    assert config.agent_memory_dir == (
        cache_home / "coding-review-agent-loop" / "repos" / "OWNER-REPO" / "memory"
    ).resolve()


def test_pre_review_tests_cli_defaults_on_and_can_be_disabled(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "task",
        "Fix the bug",
        "--repo",
        "OWNER/REPO",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
    ])
    config = config_from_args(args, FakeRunner())
    assert config.pre_review_tests is True

    args = parser.parse_args([
        "task",
        "Fix the bug",
        "--repo",
        "OWNER/REPO",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
        "--no-pre-review-tests",
    ])
    config = config_from_args(args, FakeRunner())
    assert config.pre_review_tests is False


@pytest.mark.parametrize("repo", ["OWNER", "OWNER/", "/REPO", "OWNER/REPO/EXTRA"])
def test_default_agent_workdir_rejects_invalid_repo_formats(repo):
    with pytest.raises(AgentLoopError, match="OWNER/REPO"):
        default_agent_workdir(repo, "codex")


def test_default_agent_memory_dir_uses_xdg_cache_and_repo_scope(monkeypatch, tmp_path):
    cache_home = tmp_path / "xdg-cache"
    monkeypatch.setattr("coding_review_agent_loop.config.sys.platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))

    assert default_agent_memory_dir("OWNER/REPO") == (
        cache_home / "coding-review-agent-loop" / "repos" / "OWNER-REPO" / "memory"
    )


def test_default_cache_root_uses_posix_home_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr("coding_review_agent_loop.config.sys.platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    assert default_cache_root() == tmp_path / ".cache" / "coding-review-agent-loop"


@pytest.mark.parametrize(
    ("platform", "home_parts"),
    [
        ("darwin", ("Library", "Caches", "coding-review-agent-loop")),
        ("win32", ("AppData", "Local", "coding-review-agent-loop", "Cache")),
    ],
)
def test_default_cache_root_uses_platform_home_fallbacks(
    monkeypatch,
    tmp_path,
    platform,
    home_parts,
):
    monkeypatch.setattr("coding_review_agent_loop.config.sys.platform", platform)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert default_cache_root() == tmp_path.joinpath(*home_parts)


def test_default_cache_root_uses_windows_local_app_data(monkeypatch, tmp_path):
    local_app_data = tmp_path / "local-app-data"
    monkeypatch.setattr("coding_review_agent_loop.config.sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    assert default_cache_root() == local_app_data / "coding-review-agent-loop" / "Cache"


@pytest.mark.parametrize("repo", ["OWNER", "OWNER/", "/REPO", "OWNER/REPO/EXTRA"])
def test_default_agent_memory_dir_rejects_invalid_repo_formats(repo):
    with pytest.raises(AgentLoopError, match="OWNER/REPO"):
        default_agent_memory_dir(repo)


@pytest.mark.parametrize("mode", ["ignore", "summarize", "issue", "fix-and-summarize", "fix-and-issue"])
def test_approved_followups_cli_mode_is_configurable(tmp_path, mode):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--approved-followups",
        mode,
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
        "--gemini-dir",
        str(tmp_path / "gemini"),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.approved_followups == mode


@pytest.mark.parametrize(
    "mode",
    ["plan-only", "decompose-only", "implement-one-shot", "implement-by-phase"],
)
def test_plan_execution_mode_cli_is_configurable(tmp_path, mode):
    parser = build_parser()
    args = parser.parse_args([
        "issue",
        "56",
        "--repo",
        "OWNER/REPO",
        "--plan-first",
        "--plan-execution-mode",
        mode,
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
        "--gemini-dir",
        str(tmp_path / "gemini"),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.plan_execution_mode == mode


def test_explicit_agent_dirs_are_preserved_when_others_default(tmp_path):
    parser = build_parser()
    codex_dir = tmp_path / "codex"
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "codex",
        "--reviewer",
        "claude",
        "--codex-dir",
        str(codex_dir),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.codex_dir == codex_dir
    assert config.claude_dir == default_agent_workdir("OWNER/REPO", "claude").resolve()
    assert set(config.auto_agent_dirs) == {"claude", "gemini"}


def test_relative_log_dir_defaults_under_active_coder_workdir(tmp_path):
    parser = build_parser()
    claude_dir = tmp_path / "claude"
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "claude",
        "--reviewer",
        "gemini",
        "--claude-dir",
        str(claude_dir),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.log_dir == claude_dir / ".agent-loop-logs"


def test_agent_memory_flags_configure_memory_dir_and_refresh(tmp_path):
    parser = build_parser()
    codex_dir = tmp_path / "codex"
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "codex",
        "--reviewer",
        "claude",
        "--codex-dir",
        str(codex_dir),
        "--no-agent-memory",
        "--refresh-agent-memory",
        "--refresh-test-profile",
        "--agent-memory-dir",
        "custom-memory",
    ])

    config = config_from_args(args, FakeRunner())

    assert config.agent_memory is False
    assert config.refresh_agent_memory is True
    assert config.refresh_test_profile is True
    assert config.agent_memory_dir == codex_dir / "custom-memory"


def test_agent_memory_explicit_absolute_dir_is_resolved(tmp_path):
    parser = build_parser()
    memory_dir = tmp_path / "memory-parent" / ".." / "agent-memory"
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--agent-memory-dir",
        str(memory_dir),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.agent_memory_dir == memory_dir.resolve()


def test_agent_memory_default_ignores_active_coder_workdir(tmp_path, monkeypatch):
    parser = build_parser()
    cache_home = tmp_path / "cache"
    codex_dir = tmp_path / "codex"
    monkeypatch.setattr("coding_review_agent_loop.config.sys.platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "codex",
        "--reviewer",
        "claude",
        "--codex-dir",
        str(codex_dir),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.agent_memory_dir == (
        cache_home / "coding-review-agent-loop" / "repos" / "OWNER-REPO" / "memory"
    ).resolve()
    assert codex_dir not in config.agent_memory_dir.parents


def test_auto_created_agent_dir_is_cloned_before_use(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    codex_dir = tmp_path / "tmp-root" / "owner-repo" / "codex" / "repo"
    config = make_config(
        tmp_path,
        claude_dir=tmp_path / "explicit-claude",
        codex_dir=codex_dir,
        reviewer="codex",
        auto_agent_dirs=("codex",),
        create_dirs=False,
    )
    config.claude_dir.mkdir(parents=True)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    assert ["gh", "repo", "clone", "OWNER/REPO", str(codex_dir)] in [
        cmd for cmd, _cwd in runner.commands
    ]
    assert codex_dir.is_dir()


def test_clean_existing_auto_agent_dir_is_synced(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        reviewer="codex",
        auto_agent_dirs=("codex",),
        create_dirs=False,
    )
    config.claude_dir.mkdir(parents=True)
    config.gemini_dir.mkdir(parents=True)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    assert ["git", "fetch", "origin"] in commands
    assert ["git", "switch", "main"] in commands
    assert ["git", "pull", "--ff-only", "origin", "main"] in commands


def test_reviewer_checkout_is_refreshed_to_pr_head_before_review(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    config = make_config(tmp_path, reviewer="codex")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    review_index = command_index(runner.commands, ["codex", "exec"])
    fetch_index = command_index(runner.commands, ["git", "fetch", "origin"], start=0)
    pr_fetch_index = command_index(
        runner.commands,
        ["git", "fetch", "origin", "+pull/77/head:refs/remotes/origin/pr/77"],
    )
    checkout_index = command_index(
        runner.commands,
        ["git", "checkout", "--detach", "refs/remotes/origin/pr/77"],
    )
    head_index = command_index(runner.commands, ["git", "rev-parse", "HEAD"], start=checkout_index)

    assert commands[pr_fetch_index] == ["git", "fetch", "origin", "+pull/77/head:refs/remotes/origin/pr/77"]
    assert fetch_index < pr_fetch_index < checkout_index < head_index < review_index


def test_reviewer_checkout_refreshes_each_round_before_review(tmp_path):
    runner = FakeRunner(
        claude_outputs=["Fixed.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
        codex_outputs=[
            "Please fix it.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    pr_fetches = [
        index
        for index, cmd in enumerate(commands)
        if cmd == ["git", "fetch", "origin", "+pull/77/head:refs/remotes/origin/pr/77"]
    ]
    review_indices = [index for index, cmd in enumerate(commands) if cmd[:2] == ["codex", "exec"]]

    assert len(pr_fetches) == 3
    assert len(review_indices) == 2
    assert pr_fetches[0] < review_indices[0]
    assert pr_fetches[1] < review_indices[1]


def test_dirty_default_reviewer_checkout_is_cleaned_before_review(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        git_status=" M stale.py\n",
    )
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        coder="claude",
        reviewer="codex",
        auto_agent_dirs=("claude", "codex"),
        create_dirs=False,
    )
    config.claude_dir.mkdir(parents=True)
    config.gemini_dir.mkdir(parents=True)

    assert run_pr_loop(runner, pr_number=77, config=config, workdirs_ready=True) == 0

    reset_index = command_index(runner.commands, ["git", "reset", "--hard"])
    clean_index = command_index(runner.commands, ["git", "clean", "-fd"])
    review_index = command_index(runner.commands, ["codex", "exec"])

    assert reset_index < clean_index < review_index


def test_dirty_explicit_reviewer_checkout_fails_before_review_invocation(tmp_path):
    runner = FakeRunner(
        codex_outputs=["This should not run.\n<!-- AGENT_STATE: approved -->"],
        git_status=" M stale.py\n",
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", agent_memory=False)

    with pytest.raises(AgentLoopError, match="--codex-dir is dirty"):
        run_pr_loop(runner, pr_number=77, config=config, workdirs_ready=True)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


def test_review_prompt_warns_that_pr_head_sha_is_authoritative(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    config = make_config(tmp_path, reviewer="codex")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    codex_command = next(cmd for cmd, _cwd in runner.commands if cmd[:2] == ["codex", "exec"])
    prompt = codex_command[-1]
    assert "The Head SHA above is the PR head this\nreview round is about." in prompt
    assert "If local files do not match that SHA, refresh/fetch the\ncheckout before reviewing." in prompt
    assert "Do not report findings based on untracked files unless those files are\npresent in the PR diff." in prompt


def test_dirty_existing_auto_agent_dir_is_cleaned_before_sync(tmp_path, capsys):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        git_status=" M file.py\n",
    )
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        coder="codex",
        reviewer="codex",
        auto_agent_dirs=("codex",),
        create_dirs=False,
        quiet=False,
    )
    config.gemini_dir.mkdir(parents=True)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    assert ["git", "reset", "--hard"] in commands
    assert ["git", "clean", "-fd"] in commands
    assert ["git", "pull", "--ff-only", "origin", "main"] in commands
    captured = capsys.readouterr()
    assert f"Cleaning dirty default codex workdir: {codex_dir}" in captured.err


def test_dirty_explicit_agent_dir_fails_clearly(tmp_path):
    runner = FakeRunner(git_status=" M file.py\n")
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        coder="codex",
        reviewer="codex",
        create_dirs=False,
    )
    config.gemini_dir.mkdir(parents=True)

    with pytest.raises(AgentLoopError, match="--codex-dir is dirty"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


@pytest.mark.parametrize("loop_name", ["issue", "task"])
def test_dirty_explicit_coder_dir_fails_before_issue_or_task_coder_invocation(tmp_path, loop_name):
    runner = FakeRunner(
        git_status=" M file.py\n",
        codex_outputs=[
            "Implemented.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
    )
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        coder="codex",
        reviewer="codex",
        create_dirs=False,
    )
    config.claude_dir.mkdir(parents=True)
    config.gemini_dir.mkdir(parents=True)

    with pytest.raises(AgentLoopError, match="--codex-dir is dirty"):
        if loop_name == "issue":
            run_issue_loop(runner, issue_number=56, config=config)
        else:
            run_task_loop(runner, task_text="Add /healthz endpoint.", config=config)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


def test_explicit_agent_dir_must_match_requested_repo(tmp_path):
    runner = FakeRunner(git_remote="git@github.com:OTHER/REPO.git")
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        coder="codex",
        reviewer="codex",
        create_dirs=False,
    )
    config.gemini_dir.mkdir(parents=True)

    with pytest.raises(AgentLoopError, match="not 'OWNER/REPO'"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


def test_existing_auto_agent_dir_must_be_git_checkout(tmp_path):
    runner = FakeRunner(git_inside=False)
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        reviewer="codex",
        auto_agent_dirs=("codex",),
        create_dirs=False,
    )
    config.claude_dir.mkdir(parents=True)
    config.gemini_dir.mkdir(parents=True)

    with pytest.raises(AgentLoopError, match="not a git checkout"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


def test_existing_auto_agent_dir_must_match_requested_repo(tmp_path):
    runner = FakeRunner(git_remote="git@github.com:OTHER/REPO.git")
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    config = make_config(
        tmp_path,
        codex_dir=codex_dir,
        reviewer="codex",
        auto_agent_dirs=("codex",),
        create_dirs=False,
    )
    config.claude_dir.mkdir(parents=True)
    config.gemini_dir.mkdir(parents=True)

    with pytest.raises(AgentLoopError, match="not 'OWNER/REPO'"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


def test_agent_workdir_existing_file_fails_clearly(tmp_path):
    runner = FakeRunner()
    claude_path = tmp_path / "claude-file"
    claude_path.write_text("not a dir", encoding="utf-8")
    config = make_config(tmp_path, claude_dir=claude_path, create_dirs=False)

    with pytest.raises(AgentLoopError, match="not a directory"):
        run_pr_loop(runner, pr_number=77, config=config)


def test_gemini_workdir_existing_file_fails_clearly(tmp_path):
    runner = FakeRunner()
    gemini_path = tmp_path / "gemini-file"
    gemini_path.write_text("not a dir", encoding="utf-8")
    config = make_config(
        tmp_path,
        reviewer="gemini",
        gemini_dir=gemini_path,
        create_dirs=False,
    )

    with pytest.raises(AgentLoopError, match="not a directory"):
        run_pr_loop(runner, pr_number=77, config=config)


def test_config_allows_same_coder_and_reviewer(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "codex",
        "--reviewer",
        "codex",
        "--codex-dir",
        str(tmp_path / "codex"),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.coder == "codex"
    assert config.reviewer == ("codex",)


def test_config_allows_coder_in_multiple_reviewers(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "codex",
        "--reviewer",
        "claude",
        "--reviewer",
        "codex",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.coder == "codex"
    assert config.reviewer == ("claude", "codex")


def test_config_accepts_gemini_as_coder_and_reviewer(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--coder",
        "gemini",
        "--reviewer",
        "claude",
        "--reviewer",
        "gemini",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--gemini-dir",
        str(tmp_path / "gemini"),
    ])

    config = config_from_args(args, FakeRunner())

    assert config.coder == "gemini"
    assert config.reviewer == ("claude", "gemini")
    assert config.gemini_dir == tmp_path / "gemini"


def test_config_rejects_duplicate_reviewers(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--reviewer",
        "codex",
        "--reviewer",
        "codex",
        "--codex-dir",
        str(tmp_path / "codex"),
    ])

    with pytest.raises(AgentLoopError, match="same agent more than once"):
        config_from_args(args, FakeRunner())


@pytest.mark.parametrize("max_rounds", ["0", "-1"])
def test_config_rejects_non_positive_max_rounds(tmp_path, max_rounds):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--max-rounds",
        max_rounds,
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
    ])

    with pytest.raises(AgentLoopError, match="--max-rounds must be greater than zero"):
        config_from_args(args, FakeRunner())


def test_config_defaults_do_not_bypass_agent_permissions(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
    ])
    config = config_from_args(args, FakeRunner())

    assert config.claude_args == ()
    assert config.codex_args == ()
    assert config.gemini_args == ()


def test_config_can_opt_into_dangerous_agent_permissions(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
        "--dangerous-agent-permissions",
    ])
    config = config_from_args(args, FakeRunner())

    assert config.claude_args == ("--dangerously-skip-permissions",)
    assert config.codex_args == ("--dangerously-bypass-approvals-and-sandbox",)
    assert config.gemini_args == ("--yolo", "--skip-trust")


def test_explicit_agent_args_replace_dangerous_profile(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "pr",
        "77",
        "--repo",
        "OWNER/REPO",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
        "--dangerous-agent-permissions",
        "--claude-arg=--permission-mode",
        "--claude-arg=acceptEdits",
        "--codex-arg=--sandbox",
        "--codex-arg=workspace-write",
        "--gemini-arg=--approval-mode",
        "--gemini-arg=auto_edit",
    ])
    config = config_from_args(args, FakeRunner())

    assert config.claude_args == ("--permission-mode", "acceptEdits")
    assert config.codex_args == ("--sandbox", "workspace-write")
    assert config.gemini_args == ("--approval-mode", "auto_edit")


def test_issue_loop_requires_claude_to_report_pr_number(tmp_path):
    runner = FakeRunner(claude_outputs=["Created something.\n<!-- AGENT_STATE: blocking -->"])
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="PR marker"):
        run_issue_loop(runner, issue_number=56, config=config)


def test_issue_loop_rejects_missing_initial_issue_human_requirements_acknowledgement(tmp_path):
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Keep the legacy flag.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Created PR.\nTests: python -m pytest passed.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->"
        ],
    )
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="missing required signed human requirements marker"):
        run_issue_loop(runner, issue_number=56, config=config)


def test_issue_loop_accepts_initial_issue_human_requirements_acknowledgement(tmp_path):
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Keep the legacy flag.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Created PR.\nTests: python3 -m pytest passed.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: kept the legacy flag path.\n"
            "<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->"
        ],
        codex_outputs=[
            "LGTM.\n<!-- HUMAN_REQUIREMENTS_RESOLVED -->\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config) == 0


def test_issue_loop_rejects_pr_number_before_running_claude(tmp_path):
    runner = FakeRunner(issue_payload={
        "number": 62,
        "state": "closed",
        "is_pr": True,
        "url": "https://github.com/OWNER/REPO/pull/62",
    })
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="pull request, not an issue"):
        run_issue_loop(runner, issue_number=62, config=config)

    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


def test_issue_loop_plan_first_stops_after_approved_plan(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Update the CLI.\n- Add tests.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert any(cmd[:3] == ["claude", "--print", "--output-format"] for cmd, _cwd in runner.commands)
    assert any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)
    assert not any(cmd[:3] == ["gh", "pr", "view"] for cmd, _cwd in runner.commands)
    assert len(runner.comments) == 3
    assert runner.comments[0].startswith("Plan:")
    assert runner.comments[1].startswith("**Review verdict:** Approved\n\nPlan looks sound.")
    assert "Outcome: implement" in runner.comments[2]
    assert not any(cmd[:2] == ["git", "fetch"] for cmd, _cwd in runner.commands)
    assert not any(cmd[:2] == ["git", "switch"] for cmd, _cwd in runner.commands)


def test_parse_plan_decomposition_accepts_agent_and_human_phases():
    parsed = parse_plan_decomposition(
        plan_decomposition_json(
            {
                "title": "Internal schema utilities",
                "scope": "Add helpers.",
                "non_goals": "No live switch.",
                "dependency_notes": "First phase.",
                "rollout_risk": "low - internal only.",
                "validation": "Run python -m pytest.",
                "parent_context": "Approved plan slice and invariant details.",
                "automation": "agent-pr",
                "depends_on": [],
            },
            {
                "title": "Manual rollout checkpoint",
                "scope": "Human validates the deployed behavior.",
                "non_goals": "No code changes.",
                "dependency_notes": "After Internal schema utilities.",
                "rollout_risk": "medium - live checkpoint.",
                "validation": "Human remark and closure required.",
                "parent_context": "Approved plan slice for the manual checkpoint.",
                "automation": "human-action",
                "depends_on": ["Internal schema utilities"],
            },
        )
    )

    assert [phase.title for phase in parsed.phases] == [
        "Internal schema utilities",
        "Manual rollout checkpoint",
    ]
    assert parsed.phases[1].automation == "human-action"
    assert parsed.phases[1].depends_on == ("Internal schema utilities",)


def test_parse_plan_decomposition_accepts_normalized_earlier_phase_dependency():
    parsed = parse_plan_decomposition(
        plan_decomposition_json(
            {
                "title": "Internal schema utilities",
                "scope": "Add helpers.",
                "non_goals": "No live switch.",
                "dependency_notes": "First phase.",
                "rollout_risk": "low - internal only.",
                "validation": "Run python -m pytest.",
                "parent_context": "Approved plan slice and invariant details.",
                "automation": "agent-pr",
                "depends_on": [],
            },
            {
                "title": "Manual rollout checkpoint",
                "scope": "Human validates the deployed behavior.",
                "non_goals": "No code changes.",
                "dependency_notes": "After Internal schema utilities.",
                "rollout_risk": "medium - live checkpoint.",
                "validation": "Human remark and closure required.",
                "parent_context": "Approved plan slice for the manual checkpoint.",
                "automation": "human-action",
                "depends_on": ["  internal   SCHEMA utilities  "],
            },
        )
    )

    assert parsed.phases[1].depends_on == ("internal   SCHEMA utilities",)


def test_parse_plan_decomposition_rejects_self_dependency():
    phase = {
        "title": "Internal schema utilities",
        "scope": "Add helpers.",
        "non_goals": "No live switch.",
        "dependency_notes": "First phase.",
        "rollout_risk": "low - internal only.",
        "validation": "Run python -m pytest.",
        "parent_context": "Approved plan slice and invariant details.",
        "automation": "agent-pr",
        "depends_on": ["Internal schema utilities"],
    }

    with pytest.raises(AgentLoopError, match="cannot depend on itself"):
        parse_plan_decomposition(plan_decomposition_json(phase))


def test_parse_plan_decomposition_rejects_forward_dependency():
    first_phase = {
        "title": "Internal schema utilities",
        "scope": "Add helpers.",
        "non_goals": "No live switch.",
        "dependency_notes": "First phase.",
        "rollout_risk": "low - internal only.",
        "validation": "Run python -m pytest.",
        "parent_context": "Approved plan slice and invariant details.",
        "automation": "agent-pr",
        "depends_on": ["Manual rollout checkpoint"],
    }
    second_phase = {
        "title": "Manual rollout checkpoint",
        "scope": "Human validates the deployed behavior.",
        "non_goals": "No code changes.",
        "dependency_notes": "After Internal schema utilities.",
        "rollout_risk": "medium - live checkpoint.",
        "validation": "Human remark and closure required.",
        "parent_context": "Approved plan slice for the manual checkpoint.",
        "automation": "human-action",
        "depends_on": [],
    }

    with pytest.raises(AgentLoopError, match="dependencies must reference an earlier phase"):
        parse_plan_decomposition(plan_decomposition_json(first_phase, second_phase))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda phase: phase.pop("parent_context"), "parent_context"),
        (lambda phase: phase.pop("rollout_risk"), "rollout_risk"),
        (lambda phase: phase.pop("validation"), "validation"),
        (lambda phase: phase.__setitem__("automation", "robot"), "invalid automation"),
        (lambda phase: phase.__setitem__("depends_on", ["Missing phase"]), "unknown phase"),
    ],
)
def test_parse_plan_decomposition_rejects_invalid_phase_fields(mutate, message):
    phase = {
        "title": "Internal schema utilities",
        "scope": "Add helpers.",
        "non_goals": "No live switch.",
        "dependency_notes": "First phase.",
        "rollout_risk": "low - internal only.",
        "validation": "Run python -m pytest.",
        "parent_context": "Approved plan slice and invariant details.",
        "automation": "agent-pr",
        "depends_on": [],
    }
    mutate(phase)

    with pytest.raises(AgentLoopError, match=message):
        parse_plan_decomposition(plan_decomposition_json(phase))


def test_parse_plan_decomposition_rejects_duplicates_and_over_cap():
    phase = {
        "title": "Repeated phase",
        "scope": "Add helpers.",
        "non_goals": "No live switch.",
        "dependency_notes": "First phase.",
        "rollout_risk": "low - internal only.",
        "validation": "Run python -m pytest.",
        "parent_context": "Approved plan slice and invariant details.",
        "automation": "agent-pr",
        "depends_on": [],
    }
    with pytest.raises(AgentLoopError, match="duplicate phase title"):
        parse_plan_decomposition(plan_decomposition_json(phase, dict(phase)))

    phases = [dict(phase, title=f"Phase {index}") for index in range(MAX_DECOMPOSITION_PHASES + 1)]
    with pytest.raises(AgentLoopError, match="MAX_DECOMPOSITION_PHASES"):
        parse_plan_decomposition(plan_decomposition_json(*phases))


def test_issue_loop_plan_first_rejects_missing_initial_plan_human_requirements_acknowledgement(
    tmp_path,
):
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Keep the public API unchanged.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Plan:\n- Update the parser.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
        ],
    )
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="missing required signed human requirements marker"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)


def test_issue_loop_plan_first_accepts_initial_plan_human_requirements_acknowledgement(tmp_path):
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Keep the public API unchanged.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Plan:\n- Update the parser.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: the plan keeps the public API unchanged.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
        ],
        codex_outputs=[structured_plan_review(summary="Plan looks sound.")],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0


def test_issue_loop_plan_first_revises_until_all_reviewers_approve(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            structured_plan_revision(summary="Revised plan with tests."),
        ],
        codex_outputs=[
            structured_plan_review(
                state="blocking",
                summary="Missing test strategy.",
                blocking_plan_issues=["Missing test strategy."],
            ),
            structured_plan_review(
                summary="Plan looks sound.",
                prior_plan_item_dispositions=[{"item_id": "item-1", "disposition": "resolved"}],
            ),
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0


def test_issue_loop_plan_revision_stores_raw_structured_metadata(tmp_path):
    raw_structured_revision = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_revision",
                "state": "blocking",
                "summary": "Revised plan with tests.",
                "prior_plan_item_dispositions": [
                    {"item_id": "item-1", "disposition": "resolved", "note": "Added the missing test step."}
                ],
                "plan_steps": ["Add the regression test.", "Run the focused suite."],
            }
        )
        + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            raw_structured_revision,
        ],
        codex_outputs=[
            structured_plan_review(
                state="blocking",
                summary="Missing test strategy.",
                blocking_plan_issues=["Missing test strategy."],
            ),
            structured_plan_review(
                summary="Plan looks sound.",
                prior_plan_item_dispositions=[{"item_id": "item-1", "disposition": "resolved"}],
            ),
        ],
    )
    config = make_config(tmp_path, reviewer="codex")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert runner.comments[2].startswith("## Revised plan")
    assert '"kind": "plan_revision"' not in _strip_round_metadata(runner.comments[2])
    raw_comment = runner.issue_comments[2]["body"]
    match = re.search(r"<!--\s*AGENT_LOOP_META:\s*(?P<payload>[A-Za-z0-9+/=_-]+)\s*-->", raw_comment)
    assert match is not None
    metadata = _decode_round_metadata(match.group("payload"))
    assert metadata.raw_structured_coder_response == raw_structured_revision


def test_issue_loop_plan_revision_rejects_missing_human_requirements_acknowledgement(tmp_path):
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Preserve backward compatibility.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Initial plan.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: the plan preserves backward compatibility.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            structured_plan_revision(summary="Revised plan."),
            "Revised plan.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: the revised plan still preserves backward compatibility.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            structured_plan_review(
                state="blocking",
                summary="Missing a regression test.",
                blocking_plan_issues=["Missing a regression test."],
            )
        ],
    )
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="missing required signed human requirements marker"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)


def test_issue_loop_plan_revision_accepts_human_requirements_acknowledgement(tmp_path):
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Preserve backward compatibility.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Initial plan.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: the plan preserves backward compatibility.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            structured_plan_revision(
                summary="Revised plan.",
                human_requirements=(
                    f"\n{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
                    "### Human requirements\n"
                    "- Requirement 1: the revised plan still preserves backward compatibility.\n"
                ),
            ),
        ],
        codex_outputs=[
            structured_plan_review(
                state="blocking",
                summary="Missing a regression test.",
                blocking_plan_issues=["Missing a regression test."],
            ),
            structured_plan_review(
                summary="Plan looks sound.",
                prior_plan_item_dispositions=[{"item_id": "item-1", "disposition": "resolved"}],
            ),
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(claude_calls) == 2
    assert "Missing a regression test." in claude_calls[1][-1]
    assert len(runner.comments) == 5
    assert runner.comments[2].startswith("## Revised plan")


def test_issue_loop_plan_revision_repair_preserves_signed_human_requirements(tmp_path):
    malformed_revision = (
        "### Prior plan review item dispositions\n"
        "- item-1: resolved by adding compatibility tests.\n\n"
        "### Revised plan\n"
        "- Preserve backward compatibility.\n"
        "- Add regression tests.\n\n"
        f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
        "### Human requirements\n"
        "- Requirement 1: the revised plan preserves backward compatibility.\n\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    repaired_revision = structured_plan_revision(
        summary="Revised plan with compatibility tests.",
        prior_plan_item_dispositions=[
            {
                "item_id": "item-1",
                "disposition": "resolved",
                "note": "Added compatibility tests.",
            }
        ],
        plan_steps=["Preserve backward compatibility.", "Add regression tests."],
        human_requirements=(
            f"\n{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: the revised plan preserves backward compatibility.\n"
        ),
    )
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Preserve backward compatibility.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Initial plan.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: the plan preserves backward compatibility.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            malformed_revision,
        ],
        codex_outputs=[
            structured_plan_review(
                state="blocking",
                summary="Missing a regression test.",
                blocking_plan_issues=["Missing a regression test."],
            ),
            structured_plan_review(
                summary="Plan looks sound.",
                prior_plan_item_dispositions=[{"item_id": "item-1", "disposition": "resolved"}],
            ),
        ],
    )
    config = make_config(tmp_path, agent_max_retries=0)
    captured_repairs = []

    def fake_attempt_repair(raw: str, gemini_cmd: str, *, expected_kind: str | None = None, unresolved_item_ids=None, surfaced_requirement_ids=None) -> str | None:
        captured_repairs.append((raw, expected_kind))
        return repaired_revision

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", fake_attempt_repair):
        assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert len(captured_repairs) == 1
    assert captured_repairs[0][1] == "plan_revision"
    assert HUMAN_REQUIREMENTS_ADDRESSED_MARKER in captured_repairs[0][0]
    public_revision = _strip_round_metadata(runner.comments[2])
    assert '"kind": "plan_revision"' not in public_revision
    assert HUMAN_REQUIREMENTS_ADDRESSED_MARKER in public_revision
    assert "### Human requirements" in public_revision


def test_issue_loop_plan_revision_repair_rejects_wrong_kind_from_human_requirements_text(tmp_path):
    malformed_revision = (
        "### Revised plan\n"
        "- Preserve backward compatibility.\n\n"
        f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
        "### Human requirements\n"
        "- Requirement 1: the revised plan preserves backward compatibility.\n\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    wrong_kind_repair = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "coder_followup",
                "state": "blocking",
                "summary": "Revised the plan.",
                "addressed_items": [],
                "remaining_items": [],
                "human_requirements": {
                    "addressed_ids": ["Requirement 1"],
                    "checked_discussion_directly": False,
                },
            }
        )
        + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Preserve backward compatibility.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Initial plan.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: the plan preserves backward compatibility.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            malformed_revision,
        ],
        codex_outputs=[
            structured_plan_review(
                state="blocking",
                summary="Missing a regression test.",
                blocking_plan_issues=["Missing a regression test."],
            )
        ],
    )
    config = make_config(tmp_path, agent_max_retries=0)
    captured_kinds = []

    def fake_attempt_repair(raw: str, gemini_cmd: str, *, expected_kind: str | None = None, unresolved_item_ids=None, surfaced_requirement_ids=None) -> str | None:
        captured_kinds.append(expected_kind)
        return wrong_kind_repair

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", fake_attempt_repair):
        with pytest.raises(AgentLoopError, match="expected `plan_revision`"):
            run_issue_loop(runner, issue_number=56, config=config, plan_first=True)

    assert captured_kinds == ["plan_revision"]


def test_issue_loop_plan_revision_repair_without_human_ack_fails_clearly(tmp_path):
    malformed_revision = (
        "### Revised plan\n"
        "- Preserve backward compatibility.\n\n"
        f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
        "### Human requirements\n"
        "- Requirement 1: the revised plan preserves backward compatibility.\n\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    repaired_without_ack = structured_plan_revision(
        summary="Revised plan with compatibility tests.",
        plan_steps=["Preserve backward compatibility.", "Add regression tests."],
    )
    runner = FakeRunner(
        issue_payload={
            "author": {"login": "maintainer"},
            "createdAt": "2026-05-17T08:00:00Z",
            "body": "Preserve backward compatibility.\n\n-- Human Reviewer",
        },
        claude_outputs=[
            "Initial plan.\n"
            f"{HUMAN_REQUIREMENTS_ADDRESSED_MARKER}\n"
            "### Human requirements\n"
            "- Requirement 1: the plan preserves backward compatibility.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            malformed_revision,
        ],
        codex_outputs=[
            structured_plan_review(
                state="blocking",
                summary="Missing a regression test.",
                blocking_plan_issues=["Missing a regression test."],
            )
        ],
    )
    config = make_config(tmp_path, agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value=repaired_without_ack):
        with pytest.raises(AgentLoopError, match="missing required signed human requirements marker"):
            run_issue_loop(runner, issue_number=56, config=config, plan_first=True)


def test_issue_loop_plan_first_requires_reviewers_to_disposition_prior_items(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Revised plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Second revised plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "### Blocking plan issues\n- Add parser validation tests.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
            "Still needs the test.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, max_rounds=2)

    with pytest.raises(AgentLoopError, match="did not evaluate all prior unresolved plan items"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)


def test_issue_loop_plan_first_carries_same_plan_item_across_reviewers_and_rounds(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Revised plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Second revised plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "### Same-plan follow-ups\n- Add the carry-forward orchestration test.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
            "Still needs one plan refinement."
            + prior_plan_item_dispositions("[item-1] same-plan: still need the mixed-reviewer case")
            + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
            "Plan looks sound."
            + prior_plan_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
        gemini_outputs=[
            "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- Google Gemini",
            "Plan looks sound now."
            + prior_plan_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- Google Gemini",
            "Final pass."
            + prior_plan_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- Google Gemini",
        ],
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"), max_rounds=3)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert any("item-1" in call[-1] for call in claude_calls[1:])
    assert "Approved plan:" in runner.comments[-1]


def test_issue_loop_plan_first_posts_human_readable_item_labels_in_new_and_prior_sections(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Revised plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "### Blocking plan issues\n"
            "- Keep plan-review wording distinct from PR wording.\n"
            "### Same-plan follow-ups\n"
            "- Add one carry-forward plan test.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
            "Plan looks sound."
            + prior_plan_item_dispositions(
                "[item-1] resolved",
                "[item-2] resolved",
            )
            + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, reviewer="codex", max_rounds=2)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert runner.comments[1] == (
        "**Review verdict:** Blocking\n\n"
        "### Blocking plan issues\n"
        "- Keep plan-review wording distinct from PR wording.\n"
        "\n"
        "### Same-plan follow-ups\n"
        "- Add one carry-forward plan test.\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n"
        "-- OpenAI Codex"
    )
    assert runner.comments[3] == (
        "**Review verdict:** Approved\n\n"
        "Plan looks sound.\n\n"
        "### Prior unresolved plan item dispositions\n"
        "- [item-1] Blocking issue from OpenAI Codex, round 1: Keep plan-review wording distinct from PR wording. -> resolved\n"
        "- [item-2] Same-plan follow-up from OpenAI Codex, round 1: Add one carry-forward plan test. -> resolved\n"
        "<!-- AGENT_PLAN_STATE: approved -->\n"
        "-- OpenAI Codex"
    )


def test_issue_loop_plan_first_does_not_expose_same_round_item_ids_to_later_reviewers(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
        ],
        gemini_outputs=[
            "### Same-plan follow-ups\n"
            "- Add the carry-forward orchestration test.\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n-- Google Gemini",
        ],
        claude_outputs=[
            "Still blocked.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(
        tmp_path,
        coder="codex",
        reviewer=("gemini", "claude"),
        max_rounds=1,
    )

    with pytest.raises(AgentLoopError, match="still reported blocking plan issues after round 1"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)

    second_reviewer_prompt = [
        cmd[-1]
        for cmd, _cwd in runner.commands
        if cmd[:1] == ["claude"] and "planning round 1" in cmd[-1]
    ][0]
    assert "Only items listed under `Prior unresolved plan items from earlier rounds`" in second_reviewer_prompt
    assert "[item-1]" not in second_reviewer_prompt
    assert "### New tracked unresolved items" not in runner.comments[1]


def test_issue_loop_plan_first_resumes_with_only_missing_reviewer_for_current_plan(tmp_path):
    current_plan = "Revised plan.\n- Add state reconstruction.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    coder_comment = _attach_round_metadata(
        current_plan,
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=_plan_subject(current_plan),
            prior_items=(),
        ),
    )
    codex_comment = _attach_round_metadata(
        "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="plan",
            role="reviewer",
            agent="Codex",
            round_number=2,
            subject=_plan_subject(current_plan),
            state="approved",
        ),
    )
    runner = FakeRunner(
        issue_comments=[
            {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:00:00Z", "body": coder_comment},
            {"author": {"login": "bot"}, "createdAt": "2026-05-20T09:05:00Z", "body": codex_comment},
        ],
        gemini_outputs=["Plan looks sound too.\n<!-- AGENT_PLAN_STATE: approved -->\n-- Google Gemini"],
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"))

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    agent_commands = [cmd[0] for cmd, _cwd in runner.commands if cmd[:1] in (["claude"], ["codex"], ["gemini"])]
    assert agent_commands == ["gemini"]
    assert runner.comments[-1].startswith("Planning complete for issue #56.")


def test_resume_plan_round_prefers_latest_metadata_ledger_for_same_plan_replay():
    current_plan = "Revised plan.\n- Add the active-ledger replay test.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    subject = _plan_subject(current_plan)
    stale_item = UnresolvedReviewItem(
        item_id="item-3",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Stale plan replay item.",
        status="same-plan",
        source_status="same-plan",
    )
    active_item = UnresolvedReviewItem(
        item_id="item-1",
        reviewer="OpenAI Codex",
        source_round=1,
        text="Active plan replay item.",
        status="same-plan",
        source_status="same-plan",
    )
    stale_coder_comment = _attach_round_metadata(
        current_plan,
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=subject,
            prior_items=(stale_item,),
            canonical_plan=current_plan,
        ),
    )
    stale_reviewer_comment = _attach_round_metadata(
        "Still needs work."
        + prior_plan_item_dispositions("[item-3] same-plan: stale replay")
        + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
        PostedRoundMetadata(
            flow="plan",
            role="reviewer",
            agent="Codex",
            round_number=2,
            subject=subject,
            prior_items=(stale_item,),
            dispositions=(
                parse_plan_item_dispositions(
                    prior_plan_item_dispositions("[item-3] same-plan: stale replay"),
                    reviewer="OpenAI Codex",
                )[0],
            ),
            state="blocking",
        ),
    )
    active_coder_comment = _attach_round_metadata(
        current_plan,
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=subject,
            prior_items=(active_item,),
            canonical_plan=current_plan,
        ),
    )
    active_reviewer_comment = _attach_round_metadata(
        "Plan looks sound."
        + prior_plan_item_dispositions("[item-1] resolved")
        + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- Google Gemini",
        PostedRoundMetadata(
            flow="plan",
            role="reviewer",
            agent="Gemini",
            round_number=2,
            subject=subject,
            prior_items=(active_item,),
            dispositions=(
                parse_plan_item_dispositions(
                    prior_plan_item_dispositions("[item-1] resolved"),
                    reviewer="Google Gemini",
                )[0],
            ),
            state="approved",
        ),
    )

    resumed = _resume_plan_round(
        [
            IssueComment(author="bot", created_at="2026-05-25T00:00:00Z", body=stale_coder_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:01:00Z", body=stale_reviewer_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:02:00Z", body=active_coder_comment),
            IssueComment(author="bot", created_at="2026-05-25T00:03:00Z", body=active_reviewer_comment),
        ],
        configured_reviewers=("codex", "gemini"),
    )

    assert resumed is not None
    current_plan_text, resumed_state = resumed
    assert current_plan_text == current_plan
    assert [item.item_id for item in resumed_state.prior_items] == ["item-1"]
    assert resumed_state.next_unresolved_item_number == 4
    assert [record.metadata.agent for record in resumed_state.completed_reviews] == ["Gemini"]


def test_resume_plan_round_prefers_canonical_plan_metadata():
    public_body = (
        "Revised plan summary.\n\n### Plan steps\n1. Public body copy.\n\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    canonical_plan = (
        "Revised plan summary.\n\n### Prior plan review item dispositions\n- None.\n\n"
        "### Plan steps\n1. Canonical copy."
    )
    coder_comment = _attach_round_metadata(
        public_body,
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=_plan_subject(canonical_plan),
            prior_items=(),
            canonical_plan=canonical_plan,
        ),
    )

    resumed = _resume_plan_round(
        [IssueComment(author="bot", created_at="2026-05-20T09:00:00Z", body=coder_comment)],
        configured_reviewers=("codex", "gemini"),
    )

    assert resumed is not None
    current_plan, state = resumed
    assert current_plan == canonical_plan
    assert state.coder_output == canonical_plan


def test_resume_plan_round_prefers_structured_plan_revision_metadata_for_coder_output():
    public_body = (
        "## Revised plan\n\nRevised plan summary.\n\n### Plan steps\n1. Public body copy.\n\n"
        "<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    raw_structured_revision = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "plan_revision",
                "state": "blocking",
                "summary": "Revised plan summary.",
                "prior_plan_item_dispositions": [],
                "plan_steps": ["Canonical copy."],
            }
        )
        + "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    )
    parsed = validate_structured_plan_revision(raw_structured_revision)
    assert parsed is not None
    canonical_plan = render_canonical_plan_revision(parsed, ())
    coder_comment = _attach_round_metadata(
        public_body,
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=_plan_subject(canonical_plan),
            prior_items=(),
            canonical_plan=canonical_plan,
            raw_structured_coder_response=raw_structured_revision,
        ),
    )

    resumed = _resume_plan_round(
        [IssueComment(author="bot", created_at="2026-05-20T09:00:00Z", body=coder_comment)],
        configured_reviewers=("codex", "gemini"),
    )

    assert resumed is not None
    current_plan, state = resumed
    assert current_plan == canonical_plan
    assert state.coder_output == raw_structured_revision
    assert validate_structured_plan_revision(state.coder_output) is not None
    assert '"kind": "plan_revision"' not in _strip_round_metadata(coder_comment)


def test_resume_plan_round_falls_back_to_raw_body_for_markdown_plan():
    plan = "Revised plan.\n- Add state reconstruction.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    coder_comment = _attach_round_metadata(
        plan,
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=_plan_subject(plan),
            prior_items=(),
        ),
    )

    resumed = _resume_plan_round(
        [IssueComment(author="bot", created_at="2026-05-20T09:00:00Z", body=coder_comment)],
        configured_reviewers=("codex",),
    )

    assert resumed is not None
    current_plan, state = resumed
    assert current_plan == plan
    assert state.coder_output == plan


def test_plan_subject_ignores_trailing_whitespace_added_by_metadata_round_trip():
    plan = "Revised plan.\n- Add state reconstruction.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"

    attached = _attach_round_metadata(
        f"{plan}\n",
        PostedRoundMetadata(
            flow="plan",
            role="coder",
            agent="Claude",
            round_number=2,
            subject=_plan_subject(f"{plan}\n"),
            prior_items=(),
        ),
    )

    assert _plan_subject(f"{plan}\n") == _plan_subject(_strip_round_metadata(attached))


def test_round_metadata_round_trip_preserves_canonical_plan():
    metadata = PostedRoundMetadata(
        flow="plan",
        role="coder",
        agent="Claude",
        round_number=2,
        subject="abc",
        canonical_plan="Summary\n\n### Plan steps\n1. Canonical step.",
    )

    assert _decode_round_metadata(_encode_round_metadata(metadata)).canonical_plan == metadata.canonical_plan


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"flow": "plan"},
        {
            "flow": "plan",
            "role": "coder",
            "agent": "Claude",
            "round_number": "not-an-int",
            "subject": "abc",
        },
    ],
)
def test_decode_round_metadata_rejects_missing_or_invalid_required_fields(payload):
    encoded = json.dumps(payload).encode("utf-8")

    with pytest.raises(AgentLoopError, match="Invalid AGENT_LOOP_META payload"):
        _decode_round_metadata(encoded=base64.urlsafe_b64encode(encoded).decode("ascii"))


@pytest.mark.parametrize(
    "line",
    [
        "[item-1] same-plan: none",
        "[item-1] still blocking: none",
        "[item-1] future follow-up: none",
    ],
)
def test_issue_loop_plan_first_rejects_contradictory_disposition_before_extra_revision(
    tmp_path, line
):
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Revised plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "### Same-plan follow-ups\n- Add the carry-forward orchestration test.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
            "Plan looks sound now."
            + prior_plan_item_dispositions(line)
            + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, reviewer="codex", max_rounds=3)

    with pytest.raises(AgentLoopError, match="use `resolved` when nothing remains"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)

    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(claude_calls) == 2


def test_issue_loop_plan_first_approved_future_followups_are_summarized_without_reopening(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Revised plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "### Same-plan follow-ups\n- Tighten the prompt wording.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- OpenAI Codex",
            "Looks good."
            + prior_plan_item_dispositions("[item-1] future follow-up: document parser helper reuse separately")
            + "\n### Future follow-ups\n- Add a later cleanup to dedupe shared prompt rendering.\n"
            + "<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert "Approved plan future follow-ups:" in runner.comments[-1]
    assert "document parser helper reuse separately" in runner.comments[-1]
    assert "Add a later cleanup to dedupe shared prompt rendering." in runner.comments[-1]


def test_issue_loop_plan_first_decompose_only_creates_child_issues(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Add schema helpers.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            plan_decomposition_json(
                {
                    "title": "Schema helpers",
                    "scope": "Add parser dataclasses and tests.",
                    "non_goals": "No live orchestrator switch.",
                    "dependency_notes": "First phase; no dependencies.",
                    "rollout_risk": "low - internal only.",
                    "validation": "Run python -m pytest tests/test_agent_loop.py.",
                    "parent_context": "Approved plan slice: add schema helpers and preserve behavior.",
                    "automation": "agent-pr",
                    "depends_on": [],
                },
                {
                    "title": "Human rollout checkpoint",
                    "scope": "Human validates rollout readiness.",
                    "non_goals": "No code changes.",
                    "dependency_notes": "Depends on Schema helpers.",
                    "rollout_risk": "medium - manual checkpoint.",
                    "validation": "Human must add a remark and close the issue.",
                    "parent_context": "Approved plan slice: stop for human validation.",
                    "automation": "manual-close",
                    "depends_on": ["Schema helpers"],
                },
            ),
        ],
        codex_outputs=[
            "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
        issue_urls=[
            "https://github.com/OWNER/REPO/issues/101",
            "https://github.com/OWNER/REPO/issues/102",
        ],
    )
    config = make_config(tmp_path, plan_execution_mode="decompose-only")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert len(runner.issues) == 2
    assert runner.issues[0]["title"] == "Phase 1: Schema helpers (from #56)"
    assert "Run `agent-loop issue <this issue number>`" in runner.issues[0]["body"]
    assert "Approved plan slice: add schema helpers" in runner.issues[0]["body"]
    assert runner.issues[1]["title"] == "[Human] Phase 2: Human rollout checkpoint (from #56)"
    assert "depends on #101: Schema helpers" in runner.issues[1]["body"]
    assert "human should add the required remark/update and close this issue" in runner.issues[1]["body"]
    summary = runner.comments[-1]
    assert summary.startswith("Approved plan decomposed for issue #56.")
    assert "Every phase above has a GitHub child issue" in summary
    assert "<!-- AGENT_PLAN_DECOMPOSITION:" in summary
    assert not any(cmd[:3] == ["gh", "pr", "view"] for cmd, _cwd in runner.commands)


def test_issue_loop_plan_first_decompose_only_is_idempotent(tmp_path):
    plan = "Plan:\n- Add schema helpers.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    summary = format_decomposition_parent_summary(
        parent_issue=56,
        mode="decompose-only",
        plan_hash=approved_plan_hash(plan),
        created=(),
    )
    runner = FakeRunner(
        issue_comments=[
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:00Z",
                "body": _attach_round_metadata(
                    plan,
                    PostedRoundMetadata(
                        flow="plan",
                        role="coder",
                        agent="Claude",
                        round_number=1,
                        subject=_plan_subject(plan),
                    ),
                ),
            },
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:01Z",
                "body": _attach_round_metadata(
                    "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
                    PostedRoundMetadata(
                        flow="plan",
                        role="reviewer",
                        agent="Codex",
                        round_number=1,
                        subject=_plan_subject(plan),
                        state="approved",
                    ),
                ),
            },
            {"author": {"login": "bot"}, "createdAt": "2026-05-23T00:00:02Z", "body": summary},
        ],
    )
    config = make_config(tmp_path, plan_execution_mode="decompose-only")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert runner.issues == []
    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


def test_issue_loop_plan_first_implement_by_phase_rerun_without_handoff_implements_once(tmp_path):
    plan = "Plan:\n- Add schema helpers.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    child = CreatedPhaseIssue(
        phase=RecordedPhase(title="Schema helpers", automation="agent-pr"),
        issue_url="https://github.com/OWNER/REPO/issues/99",
        issue_number=99,
    )
    summary = format_decomposition_parent_summary(
        parent_issue=56,
        mode="implement-by-phase",
        plan_hash=approved_plan_hash(plan),
        created=(child,),
    )
    runner = FakeRunner(
        claude_outputs=[
            "Implemented first phase.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        issue_comments=[
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:00Z",
                "body": _attach_round_metadata(
                    plan,
                    PostedRoundMetadata(
                        flow="plan",
                        role="coder",
                        agent="Claude",
                        round_number=1,
                        subject=_plan_subject(plan),
                    ),
                ),
            },
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:01Z",
                "body": _attach_round_metadata(
                    "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
                    PostedRoundMetadata(
                        flow="plan",
                        role="reviewer",
                        agent="Codex",
                        round_number=1,
                        subject=_plan_subject(plan),
                        state="approved",
                    ),
                ),
            },
            {"author": {"login": "bot"}, "createdAt": "2026-05-23T00:00:02Z", "body": summary},
        ],
        pr_payload={"body": "Fixes #99"},
    )
    config = make_config(tmp_path, plan_execution_mode="implement-by-phase")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert runner.issues == []
    assert any("<!-- AGENT_PLAN_PHASE_IMPLEMENTATION:" in comment for comment in runner.comments)
    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(claude_calls) == 1
    assert "GitHub issue #99" in claude_calls[0][-1]


def test_issue_loop_plan_first_implement_by_phase_rerun_with_handoff_stops(tmp_path, capsys):
    plan = "Plan:\n- Add schema helpers.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    child = CreatedPhaseIssue(
        phase=RecordedPhase(title="Schema helpers", automation="agent-pr"),
        issue_url="https://github.com/OWNER/REPO/issues/99",
        issue_number=99,
    )
    summary = format_decomposition_parent_summary(
        parent_issue=56,
        mode="implement-by-phase",
        plan_hash=approved_plan_hash(plan),
        created=(child,),
    )
    handoff = format_phase_implementation_handoff_comment(
        parent_issue=56,
        mode="implement-by-phase",
        plan_hash=approved_plan_hash(plan),
        phase_index=1,
        created=child,
    )
    runner = FakeRunner(
        issue_comments=[
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:00Z",
                "body": _attach_round_metadata(
                    plan,
                    PostedRoundMetadata(
                        flow="plan",
                        role="coder",
                        agent="Claude",
                        round_number=1,
                        subject=_plan_subject(plan),
                    ),
                ),
            },
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:01Z",
                "body": _attach_round_metadata(
                    "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
                    PostedRoundMetadata(
                        flow="plan",
                        role="reviewer",
                        agent="Codex",
                        round_number=1,
                        subject=_plan_subject(plan),
                        state="approved",
                    ),
                ),
            },
            {"author": {"login": "bot"}, "createdAt": "2026-05-23T00:00:02Z", "body": summary},
            {"author": {"login": "bot"}, "createdAt": "2026-05-23T00:00:03Z", "body": handoff},
        ],
    )
    config = make_config(tmp_path, plan_execution_mode="implement-by-phase")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    output = capsys.readouterr().out
    assert "already handed off to child issue #99" in output
    assert "agent-loop issue 99" in output
    assert runner.issues == []
    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)
    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


def test_issue_loop_plan_first_implement_by_phase_human_first_rerun_does_not_handoff(tmp_path):
    plan = "Plan:\n- Validate migration manually first.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude"
    child = CreatedPhaseIssue(
        phase=RecordedPhase(title="Manual readiness check", automation="human-action"),
        issue_url="https://github.com/OWNER/REPO/issues/99",
        issue_number=99,
    )
    summary = format_decomposition_parent_summary(
        parent_issue=56,
        mode="implement-by-phase",
        plan_hash=approved_plan_hash(plan),
        created=(child,),
    )
    runner = FakeRunner(
        issue_comments=[
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:00Z",
                "body": _attach_round_metadata(
                    plan,
                    PostedRoundMetadata(
                        flow="plan",
                        role="coder",
                        agent="Claude",
                        round_number=1,
                        subject=_plan_subject(plan),
                    ),
                ),
            },
            {
                "author": {"login": "bot"},
                "createdAt": "2026-05-23T00:00:01Z",
                "body": _attach_round_metadata(
                    "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
                    PostedRoundMetadata(
                        flow="plan",
                        role="reviewer",
                        agent="Codex",
                        round_number=1,
                        subject=_plan_subject(plan),
                        state="approved",
                    ),
                ),
            },
            {"author": {"login": "bot"}, "createdAt": "2026-05-23T00:00:02Z", "body": summary},
        ],
    )
    config = make_config(tmp_path, plan_execution_mode="implement-by-phase")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert runner.issues == []
    assert not any("<!-- AGENT_PLAN_PHASE_IMPLEMENTATION:" in comment for comment in runner.comments)
    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


def test_issue_loop_plan_first_implement_by_phase_stops_on_human_first_phase(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Validate migration manually first.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            plan_decomposition_json(
                {
                    "title": "Manual readiness check",
                    "scope": "Human validates external readiness.",
                    "non_goals": "No agent PR.",
                    "dependency_notes": "First phase; no dependencies.",
                    "rollout_risk": "medium - manual readiness gate.",
                    "validation": "Human remark and closure required.",
                    "parent_context": "Approved plan slice: manual readiness gate.",
                    "automation": "human-action",
                    "depends_on": [],
                }
            ),
        ],
        codex_outputs=["Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex"],
    )
    config = make_config(tmp_path, plan_execution_mode="implement-by-phase")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert len(runner.issues) == 1
    assert runner.issues[0]["title"].startswith("[Human] Phase 1")
    assert not any("<!-- AGENT_PLAN_PHASE_IMPLEMENTATION:" in comment for comment in runner.comments)
    assert not any(cmd[:3] == ["gh", "pr", "view"] for cmd, _cwd in runner.commands)


def test_issue_loop_plan_first_implement_by_phase_implements_first_agent_phase(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Add schema helpers.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            plan_decomposition_json(),
            "Implemented first phase.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        issue_urls=["https://github.com/OWNER/REPO/issues/99"],
        pr_payload={"body": "Fixes #99"},
    )
    config = make_config(tmp_path, plan_execution_mode="implement-by-phase")

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    assert len(runner.issues) == 1
    decomposition_index = next(
        index for index, comment in enumerate(runner.comments) if "<!-- AGENT_PLAN_DECOMPOSITION:" in comment
    )
    handoff_index = next(
        index for index, comment in enumerate(runner.comments) if "<!-- AGENT_PLAN_PHASE_IMPLEMENTATION:" in comment
    )
    implementation_index = next(
        index for index, comment in enumerate(runner.comments) if comment.startswith("Implemented first phase.")
    )
    assert decomposition_index < handoff_index < implementation_index
    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(claude_calls) == 3
    assert "GitHub issue #99" in claude_calls[2][-1]
    assert "Approved implementation plan" in claude_calls[2][-1]


def test_issue_loop_plan_first_implement_by_phase_missing_child_number_does_not_handoff(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Add schema helpers.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            plan_decomposition_json(),
        ],
        codex_outputs=[
            "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
        issue_urls=[None],
    )
    config = make_config(tmp_path, plan_execution_mode="implement-by-phase")

    with pytest.raises(AgentLoopError, match="child issue number"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)

    assert not any("<!-- AGENT_PLAN_PHASE_IMPLEMENTATION:" in comment for comment in runner.comments)


def test_phase_implementation_handoff_rejects_malformed_marker():
    comment = IssueComment(
        author="bot",
        created_at="2026-05-23T00:00:00Z",
        body="<!-- AGENT_PLAN_PHASE_IMPLEMENTATION: not-valid-base64 -->",
    )

    with pytest.raises(AgentLoopError, match="Invalid AGENT_PLAN_PHASE_IMPLEMENTATION payload"):
        find_existing_phase_implementation_handoff(
            (comment,),
            parent_issue=56,
            plan_hash="abc123",
            mode="implement-by-phase",
            phase_index=1,
            child_issue_number=99,
        )


def test_issue_loop_plan_first_keeps_blocking_review_when_future_followups_are_misclassified(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Initial plan.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Revised plan with focused tests.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Still blocked.\n\n"
            "### Blocking plan issues\n"
            "- Add parser coverage for blocking reviews with stray future follow-ups.\n\n"
            "### Same-plan follow-ups\n"
            "- Tighten the plan-review prompt wording.\n\n"
            "### Future follow-ups\n"
            "- Consider a later prompt dedupe cleanup.\n\n"
            "<!-- AGENT_PLAN_STATE: blocking -->\n"
            "-- OpenAI Codex",
            "Plan looks sound."
            + prior_plan_item_dispositions("[item-1] resolved", "[item-2] resolved")
            + "\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)

    assert run_issue_loop(runner, issue_number=56, config=config, plan_first=True) == 0

    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert "Add parser coverage for blocking reviews with stray future follow-ups." in claude_calls[1][-1]
    assert "Tighten the plan-review prompt wording." in claude_calls[1][-1]
    assert runner.comments[1].startswith("**Review verdict:** Blocking\n\nStill blocked.")
    assert "### Future follow-ups" not in runner.comments[1]


def test_issue_loop_plan_first_can_implement_after_approval(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Make the change.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Implemented approved plan.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path)

    assert (
        run_issue_loop(
            runner,
            issue_number=56,
            config=config,
            plan_first=True,
            implement_after_approval=True,
        )
        == 0
    )

    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(claude_calls) == 2
    assert "Approved implementation plan" in claude_calls[1][-1]
    assert "include `Fixes #56` or another direct reference to issue #56" in claude_calls[1][-1]
    first_claude_index = command_index(runner.commands, ["claude", "--print"])
    fetch_index = command_index(runner.commands, ["git", "fetch", "origin"])
    switch_index = command_index(runner.commands, ["git", "switch", "main"])
    second_claude_index = command_index(runner.commands, ["claude", "--print"], start=first_claude_index + 1)
    assert first_claude_index < fetch_index < switch_index < second_claude_index
    assert len(runner.comments) == 5
    assert runner.comments[3].startswith("Implemented approved plan.")
    assert runner.comments[4].startswith("**Review verdict:** Approved\n\nLGTM.")


def test_issue_loop_rejects_pr_without_issue_reference_in_body(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Fixed issue.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        pr_payload={
            "number": 77,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/77",
            "body": "Summary only.",
        },
    )
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="does not reference issue #56") as excinfo:
        run_issue_loop(runner, issue_number=56, config=config)

    assert "Edit the PR description on GitHub" in str(excinfo.value)
    assert "rerun the orchestrator as `agent-loop pr 77` to continue the review" in str(excinfo.value)


def test_issue_loop_plan_first_implementation_rejects_pr_without_issue_reference_in_body(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Plan:\n- Make the change.\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Anthropic Claude",
            "Implemented approved plan.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            "Plan looks sound.\n<!-- AGENT_PLAN_STATE: approved -->\n-- OpenAI Codex",
        ],
        pr_payload={
            "number": 77,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/77",
            "body": "Summary only.",
        },
    )
    config = make_config(tmp_path, reviewer=("codex",))

    with pytest.raises(AgentLoopError, match="does not reference issue #56") as excinfo:
        run_issue_loop(
            runner,
            issue_number=56,
            config=config,
            plan_first=True,
            implement_after_approval=True,
        )

    assert "Edit the PR description on GitHub" in str(excinfo.value)
    assert "rerun the orchestrator as `agent-loop pr 77` to continue the review" in str(excinfo.value)


def test_is_clarification_request_detects_marker():
    assert is_clarification_request("need more info\n<!-- AGENT_CLARIFY -->")
    assert is_clarification_request("<!-- agent_clarify -->")
    assert not is_clarification_request("done\n<!-- AGENT_STATE: blocking -->")


def test_task_loop_creates_pr_then_alternates_until_codex_approval(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Implemented.\n<!-- AGENT_PR: 91 -->\n<!-- AGENT_STATE: blocking -->",
            "Fixed review.\n<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=[
            "One nit.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        pr_payload={
            "number": 91,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/91",
        },
    )
    config = make_config(tmp_path)

    assert (
        run_task_loop(
            runner,
            task_text="Add a /healthz endpoint that returns 200 OK.",
            config=config,
        )
        == 0
    )

    command_names = [cmd[:2] for cmd, _cwd in runner.commands]
    assert ["claude", "--print"] in command_names
    assert ["codex", "exec"] in command_names
    assert len(runner.comments) == 4
    assert runner.comments[0].startswith("Implemented.")
    assert runner.comments[-1].startswith("**Review verdict:** Approved\n\nLGTM.")


def test_task_loop_syncs_coder_base_before_first_implementation_attempt(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Implemented.\n<!-- AGENT_PR: 91 -->\n<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        pr_payload={
            "number": 91,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/91",
        },
    )
    config = make_config(tmp_path)
    config.agent_memory_dir.mkdir(parents=True)
    (config.agent_memory_dir / "last-analyzed-commit").write_text("base123\n", encoding="utf-8")

    assert run_task_loop(runner, task_text="Add a /healthz endpoint.", config=config) == 0

    commands = runner.commands
    memory_index = command_index(commands, ["git", "diff", "--name-only"])
    fetch_index = command_index(commands, ["git", "fetch", "origin"])
    switch_index = command_index(commands, ["git", "switch", "main"])
    pull_index = command_index(commands, ["git", "pull", "--ff-only", "origin", "main"])
    coder_index = command_index(commands, ["claude", "--print"])

    assert memory_index < fetch_index < switch_index < pull_index < coder_index


def test_task_loop_picks_up_pr_url_when_marker_missing(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Opened https://github.com/OWNER/REPO/pull/77\n"
            "<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    config = make_config(tmp_path)

    assert (
        run_task_loop(
            runner,
            task_text="Tighten the rate limiter to 5 rps.",
            config=config,
        )
        == 0
    )


def test_task_loop_non_interactive_fails_on_clarification_request(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "I need to know which endpoint.\n<!-- AGENT_CLARIFY -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="non-interactive"):
        run_task_loop(
            runner,
            task_text="Add caching",
            config=config,
        )

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)
    assert runner.comments == []


def test_task_loop_interactive_supplies_clarification_then_creates_pr(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Which endpoint and how long?\n<!-- AGENT_CLARIFY -->\n-- Anthropic Claude",
            "Implemented.\n<!-- AGENT_PR: 99 -->\n<!-- AGENT_STATE: blocking -->",
        ],
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
        pr_payload={
            "number": 99,
            "state": "OPEN",
            "url": "https://github.com/OWNER/REPO/pull/99",
        },
    )
    config = make_config(tmp_path)
    answers = iter(["recent-debates endpoint, 60s TTL"])

    assert (
        run_task_loop(
            runner,
            task_text="Add caching",
            config=config,
            interactive=True,
            clarification_input=lambda: next(answers),
        )
        == 0
    )

    claude_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"]]
    assert len(claude_calls) == 2
    assert "recent-debates endpoint, 60s TTL" in claude_calls[1][-1]


def test_task_loop_interactive_aborts_after_max_clarification_rounds(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            "Q1?\n<!-- AGENT_CLARIFY -->",
            "Q2?\n<!-- AGENT_CLARIFY -->",
        ],
    )
    config = make_config(tmp_path)
    answers = iter(["a1", "a2"])

    with pytest.raises(AgentLoopError, match="after 1 rounds"):
        run_task_loop(
            runner,
            task_text="Refactor everything",
            config=config,
            interactive=True,
            max_clarification_rounds=1,
            clarification_input=lambda: next(answers),
        )


def test_task_loop_rejects_empty_task_text(tmp_path):
    runner = FakeRunner()
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="empty"):
        run_task_loop(runner, task_text="   ", config=config)

    assert runner.commands == []


def test_task_loop_requires_pr_or_clarification_marker(tmp_path):
    runner = FakeRunner(
        claude_outputs=["I just wrote some prose without any markers."],
    )
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="PR marker"):
        run_task_loop(runner, task_text="Do something", config=config)


def test_pr_loop_rejects_non_open_pr_before_running_codex(tmp_path):
    runner = FakeRunner(pr_payload={
        "number": 62,
        "state": "MERGED",
        "url": "https://github.com/OWNER/REPO/pull/62",
    })
    config = make_config(tmp_path)

    with pytest.raises(AgentLoopError, match="provide an open PR"):
        run_pr_loop(runner, pr_number=62, config=config)


def test_pr_loop_refreshes_pr_head_without_just_in_time_base_sync(tmp_path):
    runner = FakeRunner(
        codex_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"],
    )
    config = make_config(tmp_path)

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    assert ["git", "fetch", "origin"] in commands
    assert ["git", "fetch", "origin", "+pull/77/head:refs/remotes/origin/pr/77"] in commands
    assert ["git", "switch", "main"] not in commands
    assert ["git", "pull", "--ff-only", "origin", "main"] not in commands


# ---------------------------------------------------------------------------
# Reverse flow: Codex creates PR, Claude reviews
# ---------------------------------------------------------------------------


def test_codex_issue_loop_creates_pr_then_claude_approves(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Fixed issue.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "Looks good.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    command_names = [cmd[:2] for cmd, _cwd in runner.commands]
    assert ["codex", "exec"] in command_names
    assert ["claude", "--print"] in command_names
    assert len(runner.comments) == 2
    assert runner.comments[0].startswith("Fixed issue.")
    assert runner.comments[1].startswith("**Review verdict:** Approved\n\nLooks good.")


def test_codex_issue_loop_alternates_until_claude_approval(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Implemented fix.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Addressed Claude's review.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "Missing test.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    assert len(runner.comments) == 4
    assert runner.comments[-1].startswith("**Review verdict:** Approved\n\nLGTM.")


def test_codex_issue_loop_requires_codex_to_report_pr_number(tmp_path):
    runner = FakeRunner(
        codex_outputs=["Did some work.\n<!-- AGENT_STATE: blocking -->"],
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    with pytest.raises(AgentLoopError, match="PR marker"):
        run_issue_loop(runner, issue_number=56, config=config)

    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)


def test_codex_task_loop_creates_pr_then_claude_approves(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Implemented task.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "Ship it.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    assert run_task_loop(runner, task_text="Add /healthz endpoint.", config=config) == 0

    assert len(runner.comments) == 2
    assert runner.comments[0].startswith("Implemented task.")
    assert runner.comments[1].startswith("**Review verdict:** Approved\n\nShip it.")


def test_codex_task_loop_picks_up_pr_url_when_marker_missing(tmp_path):
    runner = FakeRunner(
        codex_outputs=[
            "Opened https://github.com/OWNER/REPO/pull/77\n"
            "<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
        claude_outputs=[
            "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    assert run_task_loop(runner, task_text="Tighten rate limiter.", config=config) == 0


def test_gemini_issue_loop_creates_pr_then_codex_approves(tmp_path):
    runner = FakeRunner(
        gemini_outputs=[
            "Fixed issue.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini",
        ],
        codex_outputs=[
            "Looks good.\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, coder="gemini", reviewer="codex")

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    agent_commands = [cmd[:2] for cmd, _cwd in runner.commands if cmd[:1] in (["gemini"], ["codex"])]
    assert agent_commands == [["gemini", "--prompt"], ["codex", "exec"]]
    assert len(runner.comments) == 2
    assert runner.comments[0].startswith("Fixed issue.")
    assert runner.comments[1].startswith("**Review verdict:** Approved\n\nLooks good.")


def test_gemini_issue_loop_resumes_session_for_followup(tmp_path):
    runner = FakeRunner(
        gemini_outputs=[
            json.dumps({
                "response": "Fixed issue.\n<!-- AGENT_PR: 77 -->\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini",
                "session_id": "gemini-session-1",
            }),
            # Plain-text output intentionally clears the tracked session; a third
            # Gemini turn would start without --resume.
            "Addressed review.\n<!-- AGENT_STATE: blocking -->\n-- Google Gemini",
        ],
        codex_outputs=[
            "Needs a regression test.\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "Looks good."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(
        tmp_path,
        coder="gemini",
        reviewer="codex",
        gemini_args=("--output-format", "json"),
    )

    assert run_issue_loop(runner, issue_number=56, config=config) == 0

    gemini_calls = [cmd for cmd, _cwd in runner.commands if cmd[:1] == ["gemini"]]
    assert len(gemini_calls) == 2
    assert "--resume" not in gemini_calls[0]
    assert gemini_calls[1][-2:] == ["--resume", "gemini-session-1"]


def test_gemini_review_loop_uses_prompt_and_extra_args(tmp_path):
    runner = FakeRunner(
        gemini_outputs=[
            json.dumps({"response": "LGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"}),
        ],
    )
    config = make_config(
        tmp_path,
        reviewer="gemini",
        gemini_args=("--output-format", "json", "--model", "gemini-2.5-flash"),
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    gemini_call = next(cmd for cmd, _cwd in runner.commands if cmd[:1] == ["gemini"])
    assert gemini_call[:2] == ["gemini", "--prompt"]
    assert PUBLIC_RESPONSE_MARKER in gemini_call[2]
    assert "Only content after that line will be posted to GitHub" in gemini_call[2]
    assert "--output-format" in gemini_call
    assert "--model" in gemini_call
    assert runner.comments == ["**Review verdict:** Approved\n\nLGTM.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"]


def test_gemini_review_loop_prefers_public_response_file_over_stdout(tmp_path):
    runner = FakeRunner(
        gemini_outputs=[
            "Warning: True color (24-bit) support not detected.\n"
            "YOLO mode is enabled. All tool calls will be automatically approved.\n"
            "I will fetch the PR and inspect the diff.\n"
            "Error executing tool run_shell_command: confirmation required.\n"
            "This stdout chatter should not be posted.\n",
        ],
        public_response_outputs=[
            "LGTM from response file.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini",
        ],
    )
    config = make_config(tmp_path, reviewer="gemini")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    gemini_call = next(cmd for cmd, _cwd in runner.commands if cmd[:1] == ["gemini"])
    assert "PUBLIC RESPONSE FILE:" in gemini_call[2]
    assert str(config.gemini_dir / ".git" / "agent-loop" / "responses" / "gemini") in gemini_call[2]
    assert runner.comments == ["**Review verdict:** Approved\n\nLGTM from response file.\n<!-- AGENT_STATE: approved -->\n-- Google Gemini"]


def test_claude_review_loop_prefers_public_response_file_over_stdout(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            json.dumps(
                {
                    "result": (
                        "I will inspect the PR diff.\n"
                        "Tool output chatter should not be posted.\n"
                    ),
                    "session_id": "claude-session-1",
                }
            ),
        ],
        public_response_outputs=[
            "LGTM from response file.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude",
        ],
    )
    config = make_config(tmp_path, reviewer="claude")

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    claude_call = next(cmd for cmd, _cwd in runner.commands if cmd[:1] == ["claude"])
    assert "PUBLIC RESPONSE FILE:" in claude_call[-1]
    assert "/coding-review-agent-loop/responses/OWNER-REPO/claude/" in claude_call[-1]
    assert runner.comments == ["**Review verdict:** Approved\n\nLGTM from response file.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"]


def test_codex_task_loop_rejects_empty_task_text(tmp_path):
    runner = FakeRunner()
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    with pytest.raises(AgentLoopError, match="empty"):
        run_task_loop(runner, task_text="   ", config=config)

    assert runner.commands == []


def test_claude_review_loop_runs_tests_and_merge_only_after_approval(tmp_path):
    runner = FakeRunner(
        claude_outputs=["LGTM.\n<!-- AGENT_STATE: approved -->\n-- Anthropic Claude"],
    )
    config = make_config(
        tmp_path,
        coder="codex",
        reviewer="claude",
        auto_merge=True,
        test_command=("pytest", "tests/test_agent_loop.py"),
    )

    assert run_pr_loop(runner, pr_number=77, config=config) == 0

    commands = [cmd for cmd, _cwd in runner.commands]
    assert ["pytest", "tests/test_agent_loop.py"] in commands
    assert ["gh", "pr", "merge", "77", "--repo", "OWNER/REPO", "--merge"] in commands


def test_claude_review_loop_does_not_run_codex_after_final_blocking_round(tmp_path):
    runner = FakeRunner(
        claude_outputs=["Still blocked.\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"],
    )
    config = make_config(tmp_path, coder="codex", reviewer="claude", max_rounds=1)

    with pytest.raises(AgentLoopError, match="still reported blocking"):
        run_pr_loop(runner, pr_number=77, config=config)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


def test_claude_review_loop_rejects_non_open_pr(tmp_path):
    runner = FakeRunner(pr_payload={
        "number": 62,
        "state": "CLOSED",
        "url": "https://github.com/OWNER/REPO/pull/62",
    })
    config = make_config(tmp_path, coder="codex", reviewer="claude")

    with pytest.raises(AgentLoopError, match="provide an open PR"):
        run_pr_loop(runner, pr_number=62, config=config)

    assert not any(cmd[:1] == ["claude"] for cmd, _cwd in runner.commands)

    assert not any(cmd[:2] == ["codex", "exec"] for cmd, _cwd in runner.commands)


# ---------------------------------------------------------------------------
# Repair pass tests
# ---------------------------------------------------------------------------

from coding_review_agent_loop.repair import attempt_repair, _REPAIR_PROMPT


def test_attempt_repair_returns_none_when_subprocess_fails(monkeypatch):
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    with patch("coding_review_agent_loop.repair.subprocess.run", return_value=mock_result):
        result = attempt_repair("some malformed review text", "gemini")
    assert result is None


def test_attempt_repair_returns_none_when_subprocess_raises(monkeypatch):
    with patch("coding_review_agent_loop.repair.subprocess.run", side_effect=FileNotFoundError("gemini not found")):
        result = attempt_repair("some malformed review text", "gemini")
    assert result is None


def test_attempt_repair_calls_cli_and_returns_text():
    repaired = (
        '{"schema_version":1,"kind":"pr_review","state":"approved","summary":"OK",'
        '"blocking_items":[],"same_pr_followups":[],"future_followups":[],'
        '"prior_item_dispositions":[]}\n<!-- AGENT_STATE: approved -->\n-- Gemini'
    )
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = repaired

    with patch("coding_review_agent_loop.repair.subprocess.run", return_value=mock_result) as mock_run:
        result = attempt_repair("malformed review", "gemini")

    assert result == repaired
    mock_run.assert_called_once()
    call_args = mock_run.call_args
    cmd = call_args.args[0]
    assert cmd[0] == "gemini"
    assert "--model" in cmd
    assert "gemini-3.1-flash-lite" in cmd
    assert "--prompt" in cmd
    prompt_idx = cmd.index("--prompt")
    assert "malformed review" in cmd[prompt_idx + 1]


def test_attempt_repair_includes_expected_kind_instruction():
    repaired = (
        '{"schema_version":1,"kind":"plan_revision","state":"blocking","summary":"Revised.",'
        '"prior_plan_item_dispositions":[],"plan_steps":["Add tests."]}'
        "\n<!-- AGENT_PLAN_STATE: blocking -->\n-- Gemini"
    )
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = repaired

    with patch("coding_review_agent_loop.repair.subprocess.run", return_value=mock_result) as mock_run:
        result = attempt_repair(
            "malformed response mentioning human requirements and addressed_items",
            "gemini",
            expected_kind="plan_revision",
        )

    assert result == repaired
    cmd = mock_run.call_args.args[0]
    prompt = cmd[cmd.index("--prompt") + 1]
    assert "You MUST repair this response as `plan_revision`" in prompt
    assert "Output no other `kind` value" in prompt


def test_attempt_repair_includes_coder_followup_required_item_ids():
    repaired = structured_coder_followup(
        state="approved",
        addressed_items=["item-8"],
        remaining_items=[],
        reviewer="Gemini",
    )
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = repaired

    with patch("coding_review_agent_loop.repair.subprocess.run", return_value=mock_result) as mock_run:
        result = attempt_repair(
            "### Human requirements\nAcknowledged.\n<!-- HUMAN_REQUIREMENTS_ADDRESSED -->",
            "gemini",
            expected_kind="coder_followup",
            unresolved_item_ids=["item-8"],
        )

    assert result == repaired
    cmd = mock_run.call_args.args[0]
    prompt = cmd[cmd.index("--prompt") + 1]
    assert "Required coder follow-up item IDs" in prompt
    assert "`item-8`" in prompt
    assert "exactly one of `addressed_items` or `remaining_items`" in prompt
    assert "HUMAN_REQUIREMENTS_ADDRESSED" in prompt
    assert "do not classify regular reviewer or orchestrator-injected item-N records" in prompt


def test_attempt_repair_includes_empty_surfaced_requirement_guidance():
    repaired = structured_coder_followup(
        state="blocking",
        addressed_items=["item-1"],
        remaining_items=[],
        human_requirement_ids=[],
        reviewer="Gemini",
    )
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = repaired

    with patch("coding_review_agent_loop.repair.subprocess.run", return_value=mock_result) as mock_run:
        result = attempt_repair(
            '"human_requirements":{"addressed_ids":["Issue #221 acceptance criteria"],'
            '"checked_discussion_directly":false}',
            "gemini",
            expected_kind="coder_followup",
            unresolved_item_ids=["item-1"],
            surfaced_requirement_ids=[],
        )

    assert result == repaired
    cmd = mock_run.call_args.args[0]
    prompt = cmd[cmd.index("--prompt") + 1]
    assert "Surfaced signed human requirement labels for coder follow-up" in prompt
    assert "- (none)" in prompt
    assert "set `human_requirements.addressed_ids` to `[]`" in prompt
    assert "Issue #221 acceptance criteria" in prompt
    assert '"addressed_ids": []' in prompt


def test_attempt_repair_includes_surfaced_requirement_labels_for_mixed_repairs():
    repaired = structured_coder_followup(
        state="blocking",
        addressed_items=["item-1"],
        remaining_items=[],
        human_requirement_ids=["Requirement 1"],
        reviewer="Gemini",
    )
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = repaired

    with patch("coding_review_agent_loop.repair.subprocess.run", return_value=mock_result) as mock_run:
        result = attempt_repair(
            '"addressed_ids":["Requirement 1","Issue #221 acceptance criteria"]',
            "gemini",
            expected_kind="coder_followup",
            unresolved_item_ids=["item-1"],
            surfaced_requirement_ids=["Requirement 1"],
        )

    assert result == repaired
    cmd = mock_run.call_args.args[0]
    prompt = cmd[cmd.index("--prompt") + 1]
    assert "`Requirement 1`" in prompt
    assert "keep [\"Requirement 1\"] and drop \"Issue #221 acceptance criteria\"" in prompt


def test_attempt_repair_rejects_unresolved_item_ids_for_non_coder_kind():
    with pytest.raises(ValueError, match="unresolved_item_ids"):
        attempt_repair(
            "malformed plan review",
            "gemini",
            expected_kind="plan_review",
            unresolved_item_ids=["item-1"],
        )


def test_attempt_repair_handles_json_wrapped_cli_output():
    repaired_text = (
        '{"schema_version":1,"kind":"pr_review","state":"approved","summary":"OK",'
        '"blocking_items":[],"same_pr_followups":[],"future_followups":[],'
        '"prior_item_dispositions":[]}\n<!-- AGENT_STATE: approved -->\n-- Gemini'
    )
    json_wrapped = json.dumps({"response": repaired_text, "session_id": "s1"})
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json_wrapped

    with patch("coding_review_agent_loop.repair.subprocess.run", return_value=mock_result):
        result = attempt_repair("malformed review", "gemini")

    assert result == repaired_text


def test_repair_prompt_contains_raw_response_placeholder():
    assert "{raw_response}" in _REPAIR_PROMPT


def test_repair_prompt_substitution_leaves_json_examples_intact():
    raw = "some {curly} braces {in} the review text"
    substituted = _REPAIR_PROMPT.replace("{raw_response}", raw, 1)
    assert raw in substituted
    assert "{raw_response}" not in substituted
    assert "schema_version" in substituted


def test_run_pr_loop_uses_repair_pass_on_format_failure(tmp_path):
    """Repair pass is invoked when schema validation fails; repaired output is used."""
    malformed_review = (
        "Looks good overall.\n\n"
        "AGENT_STATE: approved\n"
        "-- OpenAI Codex"
    )
    repaired_review = (
        '{"schema_version":1,"kind":"pr_review","state":"approved","summary":"Looks good overall.",'
        '"blocking_items":[],"same_pr_followups":[],"future_followups":[],'
        '"prior_item_dispositions":[]}'
        "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    )
    runner = FakeRunner(
        codex_outputs=[malformed_review],
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", agent_max_retries=0)

    captured_repairs = []

    def fake_attempt_repair(raw: str, gemini_cmd: str, *, expected_kind: str | None = None, unresolved_item_ids=None, surfaced_requirement_ids=None) -> str | None:
        captured_repairs.append(raw)
        assert expected_kind == "pr_review"
        return repaired_review

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", fake_attempt_repair):
        run_pr_loop(runner, pr_number=77, config=config)

    assert len(captured_repairs) == 1
    assert "AGENT_STATE: approved" in captured_repairs[0]


def test_run_pr_loop_repairs_format_failure_with_5xx_source_line_reference(tmp_path):
    """A 500-series source line reference must not make deterministic format errors transient."""
    malformed_review = (
        "Looks good overall.\n\n"
        "Note: orchestrator.py:577-581 currently falls back to parse_plan_state(text).\n"
        "AGENT_STATE: approved\n"
        "-- OpenAI Codex"
    )
    repaired_review = (
        '{"schema_version":1,"kind":"pr_review","state":"approved","summary":"Looks good overall.",'
        '"blocking_items":[],"same_pr_followups":[],"future_followups":[],'
        '"prior_item_dispositions":[]}'
        "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex"
    )
    runner = FakeRunner(
        codex_outputs=[malformed_review],
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", agent_max_retries=0)

    captured_repairs = []

    def fake_attempt_repair(raw: str, gemini_cmd: str, *, expected_kind: str | None = None, unresolved_item_ids=None, surfaced_requirement_ids=None) -> str | None:
        captured_repairs.append(raw)
        assert expected_kind == "pr_review"
        return repaired_review

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", fake_attempt_repair):
        run_pr_loop(runner, pr_number=77, config=config)

    assert len(captured_repairs) == 1
    assert "orchestrator.py:577-581" in captured_repairs[0]


def test_run_pr_loop_falls_back_to_error_when_repair_also_fails(tmp_path):
    """When repair also produces invalid output, the original error is raised."""
    malformed_review = (
        "Something went wrong with the format.\n"
        "AGENT_STATE: approved\n"
        "-- OpenAI Codex"
    )
    runner = FakeRunner(
        codex_outputs=[malformed_review],
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", agent_max_retries=0)

    def fake_attempt_repair_fails(raw: str, gemini_cmd: str, *, expected_kind: str | None = None, unresolved_item_ids=None, surfaced_requirement_ids=None) -> str | None:
        assert expected_kind == "pr_review"
        return "still broken output without valid schema"

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", fake_attempt_repair_fails):
        with pytest.raises(AgentLoopError, match="Codex"):
            run_pr_loop(runner, pr_number=77, config=config)


def test_run_pr_loop_skips_repair_when_repair_returns_none(tmp_path):
    """When attempt_repair returns None (e.g. no API key), normal error is raised."""
    malformed_review = (
        "Something went wrong.\n"
        "AGENT_STATE: approved\n"
        "-- OpenAI Codex"
    )
    runner = FakeRunner(
        codex_outputs=[malformed_review],
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value=None):
        with pytest.raises(AgentLoopError, match="Codex"):
            run_pr_loop(runner, pr_number=77, config=config)


def test_run_pr_loop_uses_repair_pass_on_coder_followup_format_failure(tmp_path):
    """Repair pass is invoked when coder followup schema validation fails; repaired output is used."""
    malformed_coder_followup = (
        '{"schema_version":1,"kind":"pr_review","state":"blocking","summary":"Fixed the bug.",'
        '"blocking_items":[],"same_pr_followups":[],"future_followups":[],'
        '"prior_item_dispositions":[]}'
        "\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
    )
    repaired_followup = (
        '{"schema_version":1,"kind":"coder_followup","state":"blocking","summary":"Fixed the bug.",'
        '"addressed_items":["item-1"],"remaining_items":[],'
        '"human_requirements":{"addressed_ids":[],"checked_discussion_directly":false}}'
        "\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
    )
    runner = FakeRunner(
        claude_outputs=[malformed_coder_followup],
        codex_outputs=[
            "Need a fix."
            + blocking_issues("Fix the bug.")
            + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
            "LGTM."
            + prior_item_dispositions("[item-1] resolved")
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", max_rounds=2, agent_max_retries=0)

    captured_repairs = []
    captured_unresolved_item_ids = []

    def fake_attempt_repair(raw: str, gemini_cmd: str, *, expected_kind: str | None = None, unresolved_item_ids=None, surfaced_requirement_ids=None) -> str | None:
        captured_repairs.append(raw)
        captured_unresolved_item_ids.append(tuple(unresolved_item_ids or ()))
        assert expected_kind == "coder_followup"
        return repaired_followup

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", fake_attempt_repair):
        result = run_pr_loop(runner, pr_number=77, config=config)

    assert result == 0
    assert len(captured_repairs) == 1
    assert "pr_review" in captured_repairs[0]
    assert captured_unresolved_item_ids == [("item-1",)]


def test_run_pr_loop_falls_back_to_error_when_coder_followup_repair_also_fails(tmp_path):
    """When repair also produces invalid output for coder followup, the original error is raised."""
    malformed_coder_followup = (
        '{"schema_version":1,"kind":"pr_review","state":"blocking","summary":"Fixed the bug.",'
        '"blocking_items":[],"same_pr_followups":[],"future_followups":[],'
        '"prior_item_dispositions":[]}'
        "\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
    )
    runner = FakeRunner(
        claude_outputs=[malformed_coder_followup],
        codex_outputs=[
            "Need a fix."
            + blocking_issues("Fix the bug.")
            + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", max_rounds=2, agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value="still broken output"):
        with pytest.raises(AgentLoopError, match="Claude"):
            run_pr_loop(runner, pr_number=77, config=config)


def test_run_pr_loop_skips_repair_when_coder_followup_repair_returns_none(tmp_path):
    """When attempt_repair returns None for coder followup, normal error is raised."""
    malformed_coder_followup = (
        '{"schema_version":1,"kind":"pr_review","state":"blocking","summary":"Fixed the bug.",'
        '"blocking_items":[],"same_pr_followups":[],"future_followups":[],'
        '"prior_item_dispositions":[]}'
        "\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude"
    )
    runner = FakeRunner(
        claude_outputs=[malformed_coder_followup],
        codex_outputs=[
            "Need a fix."
            + blocking_issues("Fix the bug.")
            + "\n<!-- AGENT_STATE: blocking -->\n-- OpenAI Codex",
        ],
    )
    config = make_config(tmp_path, coder="claude", reviewer="codex", max_rounds=2, agent_max_retries=0)

    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value=None):
        with pytest.raises(AgentLoopError, match="Claude"):
            run_pr_loop(runner, pr_number=77, config=config)


def test_repair_prompt_contains_coder_followup_format():
    """Repair prompt must include the coder_followup format so the model knows about it."""
    assert "coder_followup" in _REPAIR_PROMPT
    assert "addressed_items" in _REPAIR_PROMPT
    assert "remaining_items" in _REPAIR_PROMPT


def test_repair_prompt_distinguishes_item_ids_from_requirement_labels():
    """Repair prompt must warn that addressed_items uses item IDs, not requirement labels."""
    assert "Requirement 1" in _REPAIR_PROMPT
    assert "addressed_ids" in _REPAIR_PROMPT
    # The prompt must explicitly state item IDs cannot contain spaces
    assert "spaces" in _REPAIR_PROMPT or "DO NOT CONFUSE" in _REPAIR_PROMPT or "NEVER put" in _REPAIR_PROMPT


def test_repair_prompt_includes_plan_review_dedupe_guidance():
    assert "Same-plan follow-ups and Future follow-ups are mutually exclusive" in _REPAIR_PROMPT
    assert "keep blocking_plan_issues and drop the duplicate same_plan_followups entry" in _REPAIR_PROMPT
    assert (
        "keep same_plan_followups/current-plan work and drop the duplicate future_followups entry"
        in _REPAIR_PROMPT
    )
    assert "keep blocking_plan_issues and drop the duplicate future_followups entry" in _REPAIR_PROMPT


def test_repair_prompt_includes_skip_trust_in_cli_invocation():
    """The CLI invocation must include --skip-trust so repair works outside trusted dirs."""
    repaired = (
        '{"schema_version":1,"kind":"pr_review","state":"approved","summary":"OK",'
        '"blocking_items":[],"same_pr_followups":[],"future_followups":[],'
        '"prior_item_dispositions":[]}\n<!-- AGENT_STATE: approved -->\n-- Gemini'
    )
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = repaired

    with patch("coding_review_agent_loop.repair.subprocess.run", return_value=mock_result) as mock_run:
        attempt_repair("malformed review", "gemini")

    cmd = mock_run.call_args.args[0]
    assert "--skip-trust" in cmd


def test_repair_prompt_coder_followup_fenced_json_example():
    """Repair prompt must include a worked example showing fenced JSON being stripped."""
    assert "```json" in _REPAIR_PROMPT
    assert "HUMAN_REQUIREMENTS_ADDRESSED" in _REPAIR_PROMPT
    # The prompt explains the marker is not needed in structured path
    assert "NOT needed" in _REPAIR_PROMPT or "not needed" in _REPAIR_PROMPT.lower()


def test_repair_prompt_plan_revision_preserves_human_requirements_acknowledgement():
    assert "WORKED EXAMPLE 4" in _REPAIR_PROMPT
    assert "do not output coder_followup" in _REPAIR_PROMPT
    assert "preserve both after the JSON and before <!-- AGENT_PLAN_STATE: blocking -->" in _REPAIR_PROMPT


def test_repair_prompt_does_not_suggest_ack_pseudo_item_in_addressed_items():
    """The ack pseudo-item must never be suggested as a value for addressed_items.

    The orchestrator's _validate_structured_coder_followup_items explicitly excludes
    HUMAN_REQUIREMENTS_ACK_ITEM_ID from expected_ids, so any response that puts
    'item-human-requirements-acknowledgement' in addressed_items will be rejected
    as an unknown item ID.
    """
    from coding_review_agent_loop.orchestrator import HUMAN_REQUIREMENTS_ACK_ITEM_ID

    # The ack pseudo-item must not appear in the repair prompt at all, because
    # any mention of it in an addressed_items context will teach Gemini to produce
    # responses that the validator rejects.
    assert HUMAN_REQUIREMENTS_ACK_ITEM_ID not in _REPAIR_PROMPT
