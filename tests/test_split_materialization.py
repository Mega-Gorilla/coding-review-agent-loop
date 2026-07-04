"""Tests for materializing discuss split consensus / plan-first deferred
stages into follow-up GitHub issues (#476)."""
import hashlib
import json

import pytest

from coding_review_agent_loop.cli import AgentLoopError, run_issue_loop
from coding_review_agent_loop.comment_rendering import (
    render_deferred_stages_section,
    render_discuss_round_summary_comment,
)
from coding_review_agent_loop.github import (
    FoundIssue,
    search_issues,
    validate_pr_body_does_not_close_issue,
)
from coding_review_agent_loop.orchestrator import (
    DISCUSS_SPLIT_UNFILED_WARNING,
    PostedRoundMetadata,
    _attach_round_metadata,
    _discuss_subject,
    render_public_agent_comment,
    run_discuss_loop,
)
from coding_review_agent_loop.protocol import DeferredStage, ParsedDiscussReview, parse_deferred_stages
from coding_review_agent_loop.round_state import _decode_round_metadata, _encode_round_metadata
from coding_review_agent_loop.split_materialization import (
    SPLIT_CHILD_MARKER_RE,
    SplitChild,
    SplitMaterializationMetadata,
    find_existing_split_materialization,
    find_existing_split_stage_handoff,
    materialize_split_proposals,
    resolve_selected_stage,
)

from agent_loop_helpers import FakeRunner, make_config, structured_plan_review, structured_plan_state


def _discuss_review_text(
    *,
    outcome: str = "split",
    rationale: str = "Too broad.",
    split_proposals: list[str] | None = None,
    reviewer: str = "OpenAI Codex",
) -> str:
    payload: dict = {
        "schema_version": 1,
        "kind": "discuss_review",
        "outcome": outcome,
        "rationale": rationale,
    }
    if split_proposals is not None:
        payload["split_proposals"] = split_proposals
    return json.dumps(payload) + f"\n<!-- AGENT_PLAN_STATE: approved -->\n-- {reviewer}"


def _issue_subject(title: str = "Fix issue-mode context", body: str = "Original issue body.") -> str:
    return hashlib.sha256((title + "\n\n" + body).strip().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# split_materialization.py unit tests
# ---------------------------------------------------------------------------


def test_materialize_split_proposals_creates_children_and_parent_marker(tmp_path):
    runner = FakeRunner(
        issue_urls=[
            "https://github.com/OWNER/REPO/issues/101",
            "https://github.com/OWNER/REPO/issues/102",
        ]
    )
    config = make_config(tmp_path)

    metadata = materialize_split_proposals(
        runner,
        config=config,
        parent_issue=56,
        subject="subj",
        proposals=["Auth flow", "Authorization checks"],
        issue_comments=(),
    )

    assert len(runner.issues) == 2
    assert runner.issues[0]["title"] == "[#56 stage] Auth flow"
    assert "Part of #56" in runner.issues[0]["body"]
    assert SPLIT_CHILD_MARKER_RE.search(runner.issues[0]["body"]) is not None
    assert "never use closing keywords" in runner.issues[0]["body"]
    assert metadata.children[0].origin == "created"
    assert metadata.children[0].number == 101
    parent_comment = runner.comments[-1]
    assert "materialized into child issues" in parent_comment
    assert "<!-- AGENT_DISCUSS_SPLIT:" in parent_comment


def test_materialize_split_proposals_second_child_links_sibling(tmp_path):
    runner = FakeRunner(
        issue_urls=[
            "https://github.com/OWNER/REPO/issues/101",
            "https://github.com/OWNER/REPO/issues/102",
        ]
    )
    config = make_config(tmp_path)

    materialize_split_proposals(
        runner,
        config=config,
        parent_issue=56,
        subject="subj",
        proposals=["Auth flow", "Authorization checks"],
        issue_comments=(),
    )

    assert "https://github.com/OWNER/REPO/issues/101" in runner.issues[1]["body"]


def test_materialize_split_proposals_is_idempotent_via_parent_marker(tmp_path):
    runner = FakeRunner(issue_urls=["https://github.com/OWNER/REPO/issues/101"])
    config = make_config(tmp_path)
    first = materialize_split_proposals(
        runner,
        config=config,
        parent_issue=56,
        subject="subj",
        proposals=["Auth flow"],
        issue_comments=(),
    )
    marker_comment = runner.comments[-1]

    class _Comment:
        def __init__(self, body):
            self.body = body

    second = materialize_split_proposals(
        runner,
        config=config,
        parent_issue=56,
        subject="subj",
        proposals=["Auth flow"],
        issue_comments=[_Comment(marker_comment)],
    )

    assert len(runner.issues) == 1
    assert len(runner.comments) == 1
    assert second.children == first.children


def test_materialize_split_proposals_files_only_new_proposals_when_others_exist(tmp_path):
    runner = FakeRunner(issue_urls=["https://github.com/OWNER/REPO/issues/101"])
    config = make_config(tmp_path)
    materialize_split_proposals(
        runner,
        config=config,
        parent_issue=56,
        subject="subj",
        proposals=["Auth flow"],
        issue_comments=(),
    )
    marker_comment = runner.comments[-1]

    class _Comment:
        def __init__(self, body):
            self.body = body

    runner.issue_urls = ["https://github.com/OWNER/REPO/issues/102"]
    metadata = materialize_split_proposals(
        runner,
        config=config,
        parent_issue=56,
        subject="subj2",
        proposals=["Auth flow", "Rate limiting"],
        issue_comments=[_Comment(marker_comment)],
    )

    assert len(runner.issues) == 2
    assert runner.issues[-1]["title"] == "[#56 stage] Rate limiting"
    assert {child.title for child in metadata.children} == {"Auth flow", "Rate limiting"}


def test_materialize_split_proposals_adopts_existing_children_via_search(tmp_path):
    key_hash = hashlib.sha256("auth flow".encode("utf-8")).hexdigest()[:16]
    runner = FakeRunner(
        issue_urls=["https://github.com/OWNER/REPO/issues/102"],
        search_issues_results=[
            {
                "number": 555,
                "title": "[#56 stage] Auth flow",
                "url": "https://github.com/OWNER/REPO/issues/555",
                "body": f"<!-- AGENT_SPLIT_CHILD: parent=56 key={key_hash} -->",
            }
        ],
    )
    config = make_config(tmp_path)

    metadata = materialize_split_proposals(
        runner,
        config=config,
        parent_issue=56,
        subject="subj",
        proposals=["Auth flow", "Authorization checks"],
        issue_comments=(),
    )

    assert len(runner.issues) == 1
    assert runner.issues[0]["title"] == "[#56 stage] Authorization checks"
    origins = {child.title: child.origin for child in metadata.children}
    assert origins["Auth flow"] == "adopted"
    assert origins["Authorization checks"] == "created"
    numbers = {child.title: child.number for child in metadata.children}
    assert numbers["Auth flow"] == 555


def test_materialize_split_proposals_caps_children(tmp_path):
    runner = FakeRunner(
        issue_urls=[f"https://github.com/OWNER/REPO/issues/{n}" for n in range(200, 208)]
    )
    config = make_config(tmp_path)
    proposals = [f"Stage {i}" for i in range(10)]

    metadata = materialize_split_proposals(
        runner,
        config=config,
        parent_issue=56,
        subject="subj",
        proposals=proposals,
        issue_comments=(),
    )

    assert len(metadata.children) == 8
    assert "skipped by the" in runner.comments[-1]


def test_find_existing_split_materialization_matches_by_parent_not_subject(tmp_path):
    runner = FakeRunner(issue_urls=["https://github.com/OWNER/REPO/issues/101"])
    config = make_config(tmp_path)
    materialize_split_proposals(
        runner, config=config, parent_issue=56, subject="subj-a", proposals=["Auth flow"], issue_comments=()
    )
    marker_comment = runner.comments[-1]

    class _Comment:
        def __init__(self, body):
            self.body = body

    found = find_existing_split_materialization([_Comment(marker_comment)], parent_issue=56)
    assert found is not None
    assert found.children[0].title == "Auth flow"

    not_found = find_existing_split_materialization([_Comment(marker_comment)], parent_issue=99)
    assert not_found is None


def test_resolve_selected_stage_auto_resolves_single_child():
    existing = SplitMaterializationMetadata(
        parent_issue=56,
        subject="s",
        proposal_titles=("Auth flow",),
        children=(SplitChild(title="Auth flow", key="auth flow", url=None, number=101, origin="created"),),
    )
    selected = resolve_selected_stage(existing=existing, split_stage=None)
    assert selected.number == 101


def test_resolve_selected_stage_requires_split_stage_when_ambiguous():
    existing = SplitMaterializationMetadata(
        parent_issue=56,
        subject="s",
        proposal_titles=("Auth flow", "Rate limiting"),
        children=(
            SplitChild(title="Auth flow", key="auth flow", url=None, number=101, origin="created"),
            SplitChild(title="Rate limiting", key="rate limiting", url=None, number=102, origin="created"),
        ),
    )
    with pytest.raises(AgentLoopError, match="--split-stage"):
        resolve_selected_stage(existing=existing, split_stage=None)

    selected = resolve_selected_stage(existing=existing, split_stage=102)
    assert selected.title == "Rate limiting"


def test_resolve_selected_stage_rejects_unknown_split_stage_number():
    existing = SplitMaterializationMetadata(
        parent_issue=56,
        subject="s",
        proposal_titles=("Auth flow",),
        children=(SplitChild(title="Auth flow", key="auth flow", url=None, number=101, origin="created"),),
    )
    with pytest.raises(AgentLoopError, match="does not match"):
        resolve_selected_stage(existing=existing, split_stage=999)


# ---------------------------------------------------------------------------
# github.py unit tests
# ---------------------------------------------------------------------------


def test_search_issues_parses_results(tmp_path):
    runner = FakeRunner(
        search_issues_results=[
            {"number": 5, "title": "T", "url": "https://github.com/OWNER/REPO/issues/5", "body": "b"}
        ]
    )
    config = make_config(tmp_path)
    found = search_issues(runner, config=config, search="query")
    assert found == (FoundIssue(number=5, title="T", url="https://github.com/OWNER/REPO/issues/5", body="b"),)


def test_search_issues_dry_run_returns_empty(tmp_path):
    runner = FakeRunner(search_issues_results=[{"number": 5, "title": "T", "url": None, "body": None}])
    config = make_config(tmp_path, dry_run=True)
    found = search_issues(runner, config=config, search="query")
    assert found == ()


def test_validate_pr_body_does_not_close_issue_rejects_closing_keyword(tmp_path):
    runner = FakeRunner(pr_payload={"body": "Closes #56\nRefs #99"})
    config = make_config(tmp_path)
    with pytest.raises(AgentLoopError, match="closing keyword"):
        validate_pr_body_does_not_close_issue(runner, config=config, pr_number=77, issue_number=56)


def test_validate_pr_body_does_not_close_issue_accepts_refs(tmp_path):
    runner = FakeRunner(pr_payload={"body": "Closes #99\nRefs #56"})
    config = make_config(tmp_path)
    validate_pr_body_does_not_close_issue(runner, config=config, pr_number=77, issue_number=56)


# ---------------------------------------------------------------------------
# protocol.py: deferred_stages parsing
# ---------------------------------------------------------------------------


def test_parse_deferred_stages_from_structured_plan_state():
    text = structured_plan_state(
        deferred_stages=[{"title": "Stage 2", "summary": "Later work."}],
    )
    stages = parse_deferred_stages(text)
    assert stages == (DeferredStage(title="Stage 2", summary="Later work."),)


def test_parse_deferred_stages_from_canonical_markdown_section():
    text = "\n\n".join(
        [
            "Summary text.",
            "### Plan steps\n1. Do the thing.",
            render_deferred_stages_section(
                (DeferredStage(title="Stage 2", summary="Later work."),)
            ),
        ]
    )
    stages = parse_deferred_stages(text)
    assert stages == (DeferredStage(title="Stage 2", summary="Later work."),)


def test_parse_deferred_stages_empty_when_absent():
    assert parse_deferred_stages(structured_plan_state()) == ()


# ---------------------------------------------------------------------------
# round_state.py: split_proposals metadata roundtrip
# ---------------------------------------------------------------------------


def test_posted_round_metadata_split_proposals_roundtrip():
    metadata = PostedRoundMetadata(
        flow="discuss",
        role="summary",
        agent="Orchestrator",
        round_number=1,
        subject="subj",
        is_final=True,
        split_proposals=("Auth flow", "Rate limiting"),
    )
    decoded = _decode_round_metadata(_encode_round_metadata(metadata))
    assert decoded.split_proposals == ("Auth flow", "Rate limiting")


def test_posted_round_metadata_split_proposals_defaults_empty_for_legacy_payload():
    metadata = PostedRoundMetadata(
        flow="discuss", role="summary", agent="Orchestrator", round_number=1, subject="subj"
    )
    encoded = _encode_round_metadata(metadata)
    import base64

    payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
    del payload["split_proposals"]
    legacy_encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")
    decoded = _decode_round_metadata(legacy_encoded)
    assert decoded.split_proposals == ()


# ---------------------------------------------------------------------------
# comment_rendering.py
# ---------------------------------------------------------------------------


def test_render_discuss_round_summary_comment_embeds_unfiled_split_warning():
    body = render_discuss_round_summary_comment(
        round_number=1,
        reviewer_votes=[ParsedDiscussReview(outcome="split", rationale="x", split_proposals=("A",), reviewer="Codex")],
        is_final=True,
        subject="subj",
        outcome="split",
        consensus_kind="unanimous",
        split_proposals=["A"],
        unfiled_split_warning=DISCUSS_SPLIT_UNFILED_WARNING,
    )
    assert "Split follow-ups are **not** filed" in body


# ---------------------------------------------------------------------------
# discuss loop integration
# ---------------------------------------------------------------------------


def test_discuss_split_materializes_child_issues_when_flag_enabled(tmp_path):
    split_text = _discuss_review_text(split_proposals=["Auth flow", "Authorization checks"])
    runner = FakeRunner(
        codex_outputs=[split_text],
        gemini_outputs=[split_text],
        issue_urls=[
            "https://github.com/OWNER/REPO/issues/101",
            "https://github.com/OWNER/REPO/issues/102",
        ],
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"), materialize_split_issues=True)

    result = run_discuss_loop(runner, issue_number=56, config=config)

    assert result == 0
    assert len(runner.issues) == 2
    assert "materialized into child issues" in runner.comments[-1]
    assert "Consensus: Split" in runner.comments[-2]


def test_discuss_split_warns_when_flag_disabled(tmp_path):
    split_text = _discuss_review_text(split_proposals=["Auth flow", "Authorization checks"])
    runner = FakeRunner(codex_outputs=[split_text], gemini_outputs=[split_text])
    config = make_config(tmp_path, reviewer=("codex", "gemini"), materialize_split_issues=False)

    result = run_discuss_loop(runner, issue_number=56, config=config)

    assert result == 0
    assert len(runner.issues) == 0
    assert "Split follow-ups are **not** filed" in runner.comments[-1]


def test_discuss_split_rerun_via_raw_marker_materializes_instead_of_skipping(tmp_path):
    split_text = _discuss_review_text(split_proposals=["Auth flow", "Authorization checks"])
    runner = FakeRunner(
        codex_outputs=[split_text],
        gemini_outputs=[split_text],
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"), materialize_split_issues=False)
    run_discuss_loop(runner, issue_number=56, config=config)

    runner2 = FakeRunner(
        issue_comments=list(runner.issue_comments),
        codex_outputs=[],
        gemini_outputs=[],
        issue_urls=[
            "https://github.com/OWNER/REPO/issues/201",
            "https://github.com/OWNER/REPO/issues/202",
        ],
    )
    config2 = make_config(tmp_path, reviewer=("codex", "gemini"), materialize_split_issues=True)

    result = run_discuss_loop(runner2, issue_number=56, config=config2)

    assert result == 0
    assert len(runner2.issues) == 2
    assert "materialized into child issues" in runner2.comments[-1]


def test_discuss_split_rerun_after_materialization_is_idempotent(tmp_path):
    split_text = _discuss_review_text(split_proposals=["Auth flow", "Authorization checks"])
    runner = FakeRunner(
        codex_outputs=[split_text],
        gemini_outputs=[split_text],
        issue_urls=[
            "https://github.com/OWNER/REPO/issues/101",
            "https://github.com/OWNER/REPO/issues/102",
        ],
    )
    config = make_config(tmp_path, reviewer=("codex", "gemini"), materialize_split_issues=True)
    run_discuss_loop(runner, issue_number=56, config=config)

    runner2 = FakeRunner(
        issue_comments=list(runner.issue_comments), codex_outputs=[], gemini_outputs=[]
    )
    config2 = make_config(tmp_path, reviewer=("codex", "gemini"), materialize_split_issues=True)

    result = run_discuss_loop(runner2, issue_number=56, config=config2)

    assert result == 0
    assert len(runner2.issues) == 0
    assert len(runner2.comments) == 0


def test_discuss_split_rerun_via_resume_state_done_materializes(tmp_path):
    """Round metadata is present (not the pre-#476 legacy case) but the fast
    subject-marker scan is what actually fires; the resume-state fallback
    path is exercised when the marker text itself is absent but full round
    metadata resumes as done."""
    split_text = _discuss_review_text(split_proposals=["Auth flow"])
    runner = FakeRunner(codex_outputs=[split_text], gemini_outputs=[split_text])
    config = make_config(tmp_path, reviewer=("codex", "gemini"), materialize_split_issues=False)
    run_discuss_loop(runner, issue_number=56, config=config)

    # Strip the textual `AGENT_DISCUSS_CONSENSUS` marker from the stored final
    # summary comment so the fast raw-marker scan can't match, forcing
    # `_run_discuss_loop` to rely on the full `_resume_discuss_round`
    # reconstruction (the `resume_state.done` path) instead.
    stripped_comments = []
    for comment in runner.issue_comments:
        body = comment["body"].replace("Consensus: Split", "Consensus: Split (edited)")
        stripped_comments.append({**comment, "body": body})

    runner2 = FakeRunner(
        issue_comments=stripped_comments,
        codex_outputs=[],
        gemini_outputs=[],
        issue_urls=["https://github.com/OWNER/REPO/issues/301"],
    )
    config2 = make_config(tmp_path, reviewer=("codex", "gemini"), materialize_split_issues=True)

    result = run_discuss_loop(runner2, issue_number=56, config=config2)

    assert result == 0
    assert len(runner2.issues) == 1


# ---------------------------------------------------------------------------
# plan-first integration: deferred stages + selected-stage handoff
# ---------------------------------------------------------------------------


def _plan_state_with_deferred(state="blocking"):
    return structured_plan_state(
        state=state,
        summary="Scope narrowed to stage 1 only.",
        plan_steps=["Implement stage 1 helpers.", "Add tests for stage 1."],
        deferred_stages=[{"title": "Stage 2 follow-up", "summary": "Covers the remaining API surface."}],
    )


def test_plan_first_deferred_stages_materializes_when_flag_enabled(tmp_path):
    runner = FakeRunner(
        claude_outputs=[_plan_state_with_deferred()],
        codex_outputs=[structured_plan_review(state="approved")],
    )
    config = make_config(
        tmp_path, plan_execution_mode="plan-only", materialize_split_issues=True
    )
    runner.issue_urls = ["https://github.com/OWNER/REPO/issues/601"]

    result = run_issue_loop(runner, issue_number=56, config=config, plan_first=True)

    assert result == 0
    assert len(runner.issues) == 1
    assert runner.issues[0]["title"] == "[#56 stage] Stage 2 follow-up"
    assert "materialized into child issues" in runner.comments[-1]


def test_plan_first_deferred_stages_warns_when_flag_disabled(tmp_path):
    runner = FakeRunner(
        claude_outputs=[_plan_state_with_deferred()],
        codex_outputs=[structured_plan_review(state="approved")],
    )
    config = make_config(
        tmp_path, plan_execution_mode="plan-only", materialize_split_issues=False
    )

    result = run_issue_loop(runner, issue_number=56, config=config, plan_first=True)

    assert result == 0
    assert len(runner.issues) == 0
    assert "Deferred stages not filed" in runner.comments[-1]
    assert "Stage 2 follow-up" in runner.comments[-1]


def test_plan_first_selected_stage_handoff_closes_child_refs_parent(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            _plan_state_with_deferred(),
            "Implemented stage 1.\n<!-- AGENT_PR: 300 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[
            structured_plan_review(state="approved"),
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
            + "\n<!-- AGENT_STATE: approved -->\n-- OpenAI Codex",
        ],
        issue_urls=["https://github.com/OWNER/REPO/issues/601"],
        pr_payload={
            "number": 300,
            "state": "OPEN",
            "body": "Closes #601\nRefs #56",
            "headRefName": "f",
            "baseRefName": "main",
            "headRefOid": "abc123",
            "comments": [],
            "reviews": [],
        },
    )
    config = make_config(
        tmp_path, plan_execution_mode="implement-one-shot", materialize_split_issues=True
    )

    result = run_issue_loop(runner, issue_number=56, config=config, plan_first=True)

    assert result == 0
    assert len(runner.issues) == 1
    handoff_comment = next(c for c in runner.comments if "AGENT_SPLIT_STAGE_HANDOFF" in c)
    assert "implements stage" in handoff_comment
    assert "601" in handoff_comment


def test_plan_first_staged_pr_body_closing_parent_is_rejected(tmp_path):
    runner = FakeRunner(
        claude_outputs=[
            _plan_state_with_deferred(),
            "Implemented stage 1.\n<!-- AGENT_PR: 300 -->\n<!-- AGENT_STATE: blocking -->\n-- Anthropic Claude",
        ],
        codex_outputs=[structured_plan_review(state="approved")],
        issue_urls=["https://github.com/OWNER/REPO/issues/601"],
        pr_payload={
            "number": 300,
            "state": "OPEN",
            "body": "Closes #601\nCloses #56",
            "headRefName": "f",
            "baseRefName": "main",
            "headRefOid": "abc123",
            "comments": [],
            "reviews": [],
        },
    )
    config = make_config(
        tmp_path, plan_execution_mode="implement-one-shot", materialize_split_issues=True
    )

    with pytest.raises(AgentLoopError, match="closing keyword"):
        run_issue_loop(runner, issue_number=56, config=config, plan_first=True)
