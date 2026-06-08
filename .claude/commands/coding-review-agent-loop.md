Read `SKILL.md` in full, then run the coding-review-agent-loop orchestration.

Arguments provided by the user: $ARGUMENTS

Parse the arguments to extract:
- **repo** (`OWNER/REPO`) — required
- **flow** — `issue <N>` (maps to `--flow plan`) or `pr <N>` (maps to `--flow pr`)
- **reviewers** — `--reviewers codex`, `gemini`, or both (default: gemini)
- **plan-first** — present if the user passes `--plan-first` (only relevant for issue flow)

If any required argument is missing, ask the user before proceeding.

Then follow the orchestration steps in `SKILL.md` from Step 1.

Note: `task "<text>"` is not supported in skill mode. Direct the user to the
headless CLI (`agent-loop task "..." --repo OWNER/REPO`) for task-based flows.

**Current limitation**: this skill runs Claude as the coder. Reversed roles
(Codex as coder, Claude as reviewer) are not yet supported in skill mode,
though the headless `agent-loop` CLI supports `--coder codex --reviewer claude`.
