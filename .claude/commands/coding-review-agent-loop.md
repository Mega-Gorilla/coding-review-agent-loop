Read `SKILL.md` in full, then run the coding-review-agent-loop orchestration.

Arguments provided by the user: $ARGUMENTS

Parse the arguments to extract:
- **repo** (`OWNER/REPO`) — required
- **flow** — `issue <N>` (default: plan-first=false), `pr <N>`, or `task "<text>"`
- **reviewers** — `--reviewers codex`, `gemini`, or both (default: gemini)
- **plan-first** — present if the user passes `--plan-first`

If any required argument is missing, ask the user before proceeding.

Then follow the orchestration steps in `SKILL.md` from Step 1.

**Current limitation**: this skill runs Claude as the coder. Reversed roles
(Codex as coder, Claude as reviewer) are not yet supported in skill mode,
though the headless `agent-loop` CLI supports `--coder codex --reviewer claude`.
