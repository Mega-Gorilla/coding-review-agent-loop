# Agent Loop Skill — Claude Code Native Mode

This skill lets you run the `coding-review-agent-loop` orchestration directly inside
an interactive Claude Code session, without calling `claude -p` for Claude turns.

Claude (you, the host) performs coder/plan turns using your active session context.
External agents (Codex, Gemini) are invoked via their local CLIs as subprocesses.
GitHub operations go through `gh`.

## Prerequisites

- `gh` authenticated and configured.
- `codex` CLI installed (for Codex reviewer turns).
- `gemini` CLI installed (for Gemini reviewer turns).
- The `coding-review-agent-loop` package importable from `src/` (run from repo root).

## How to start a plan loop for an issue

Provide the following information:

1. **Repository**: `OWNER/REPO`
2. **Issue number**: e.g. `123`
3. **Reviewers**: e.g. `codex`, `gemini`, or both

Then follow the steps below.

---

## Orchestration steps

### Step 1 — Check for an existing session

```bash
python -m helpers.state_manager build-resume \
  --issue ISSUE --repo OWNER/REPO \
  --reviewers codex gemini \
  --flow plan
```

If `round_number` > 1 or `completed_reviewer_names` is non-empty, a prior round
was found and you can skip already-completed reviewer turns.

### Step 2 — Write the plan (Claude host turn)

Write the implementation plan to a temp file, e.g.:

```
/tmp/agent-loop-skill/{session-id}/plan-{uuid}.md
```

The file must end with:

```
<!-- AGENT_PLAN_STATE: approved -->
-- Anthropic Claude
```

### Step 3 — Validate the plan

```bash
python -m helpers.validate_response \
  --file /tmp/agent-loop-skill/{session-id}/plan-{uuid}.md \
  --kind plan_state
```

### Step 4 — Attach AGENT_LOOP_META to the plan comment

The comment posted to GitHub must carry an `AGENT_LOOP_META` marker so that
`build-resume` can reconstruct the round state in future sessions.  Use
`attach-metadata` to produce a metadata-tagged version of the plan file:

```bash
python -m helpers.state_manager attach-metadata \
  --body-file /tmp/agent-loop-skill/{session-id}/plan-{uuid}.md \
  --output /tmp/agent-loop-skill/{session-id}/plan-tagged.md \
  --flow plan --role coder --agent Claude \
  --round-number {round_number} --state approved \
  --subject-plan-file /tmp/agent-loop-skill/{session-id}/plan-{uuid}.md \
  --canonical-plan-file /tmp/agent-loop-skill/{session-id}/plan-{uuid}.md \
  [--prior-items-file /tmp/agent-loop-skill/{session-id}/prior_items.json]
```

`prior_items.json` is the `prior_items` array from the `build-resume` JSON
output.  Omit the flag when `prior_items` is empty (round 1).

### Step 5 — Save as pending comment and post

```bash
python -m helpers.state_manager write-pending-comment \
  --issue ISSUE --repo OWNER/REPO \
  --body /tmp/agent-loop-skill/{session-id}/plan-tagged.md
```

```bash
python -m helpers.gh_ops post-issue-comment \
  --issue ISSUE --file /tmp/agent-loop-skill/{session-id}/plan-tagged.md \
  --repo OWNER/REPO
```

```bash
python -m helpers.state_manager clear-pending-comment \
  --issue ISSUE --repo OWNER/REPO
```

### Step 6 — Run each reviewer

For each reviewer (e.g. Codex):

```bash
python -m helpers.run_external \
  --agent codex \
  --prompt-file /tmp/agent-loop-skill/{session-id}/reviewer-prompt.md \
  --output /tmp/agent-loop-skill/{session-id}/codex-review.md \
  --workdir /path/to/codex/checkout
```

Validate the reviewer response:

```bash
python -m helpers.validate_response \
  --file /tmp/agent-loop-skill/{session-id}/codex-review.md \
  --kind plan_review \
  --context-file /tmp/agent-loop-skill/{session-id}/context.json
```

The `context.json` must contain:

```json
{
  "reviewer": "Codex",
  "prior_items": [...],
  "current_round_items": [...]
}
```

Attach AGENT_LOOP_META to the reviewer comment (subject must match the coder comment):

```bash
python -m helpers.state_manager attach-metadata \
  --body-file /tmp/agent-loop-skill/{session-id}/codex-review.md \
  --output /tmp/agent-loop-skill/{session-id}/codex-review-tagged.md \
  --flow plan --role reviewer --agent Codex \
  --round-number {round_number} --state approved \
  --subject-plan-file /tmp/agent-loop-skill/{session-id}/plan-{uuid}.md \
  [--prior-items-file /tmp/agent-loop-skill/{session-id}/prior_items.json] \
  [--dispositions-file /tmp/agent-loop-skill/{session-id}/codex_dispositions.json]
```

Post the reviewer comment (with metadata):

```bash
python -m helpers.gh_ops post-issue-comment \
  --issue ISSUE --file /tmp/agent-loop-skill/{session-id}/codex-review-tagged.md \
  --repo OWNER/REPO
```

### Step 7 — Update session state

```bash
python -m helpers.state_manager write-session \
  --issue ISSUE --repo OWNER/REPO \
  --fields '{"last_completed_step": "post_review", "round_number": 1}'
```

### Step 8 — Decision

- If all reviewers approved: implementation is complete.
- If any reviewer blocked: perform a new plan revision and loop back to Step 2.
- If clarification is needed: post an `<!-- AGENT_CLARIFY -->` comment and stop.

---

## PR review mode

Use `--flow pr` with `build-resume` and pass `--pr PR_NUMBER` (or `--head-sha SHA`)
to operate in PR-review mode. All other steps are the same, using `--kind pr_review`
for validation.

---

## Billing and terms note

This skill runs Claude turns inside your active interactive Claude Code session.
Whether that counts as interactive or programmatic usage depends on Anthropic's
current terms and product behavior at the time you run it.
Do not use this skill to proxy one user's session to other users, to build
unattended 24/7 automation, or in any way that violates Anthropic's usage policies.

---

## Session state location

Session state is stored in:

```
~/.local/state/coding-review-agent-loop/skill-sessions/{owner-repo}/{issue}.json
```

This location is outside git checkouts, so it never dirties any working tree.

---

## Limitations

- If Claude Code's session ends mid-loop, resume from the last posted GitHub comment
  by re-running Step 1 with `build-resume`.
- Long-running Codex/Gemini subprocess progress is not streamed; check the log
  file in `/tmp/coding-review-agent-loop/skill-logs/` if a reviewer hangs.
- The structured protocol (AGENT_LOOP_META markers, structured JSON responses)
  must match the versions expected by the existing library in `src/`.

---

## Demo

Run a minimal dry-run demo (no live GitHub or agent calls):

```bash
python -m helpers.demo_loop --issue 123 --repo demo/repo
```

Expected output includes:
```
validation passed: plan_state
validation passed: plan_review
demo_loop: all steps completed successfully
```
