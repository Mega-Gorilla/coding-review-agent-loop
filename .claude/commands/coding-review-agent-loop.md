Read `SKILL.md` in full, then run the coding-review-agent-loop orchestration.

Arguments provided by the user: $ARGUMENTS

Parse the arguments to extract:
- **repo** (`OWNER/REPO`) — required
- **flow** — `issue <N>` (maps to `--flow plan`) or `pr <N>` (maps to `--flow pr`)
- **reviewers** — `--reviewers codex`, `gemini`, or both (default: gemini)
- **plan-first** — present if the user passes `--plan-first` (only relevant for issue flow)
- **coder** — `--coder claude` (default) or `--coder codex`

If any required argument is missing, ask the user before proceeding.

Then follow the orchestration steps in `SKILL.md` from Step 1.

When `--coder codex` is parsed, follow the **Reversed roles** section of `SKILL.md`
instead of the default Claude-as-coder flow: Codex handles Step 2 (plan writing)
via `run_external --role coder`, and Claude performs Step 6 (review turn) directly
in the session.

Note: `task "<text>"` is not supported in skill mode. Direct the user to the
headless CLI (`agent-loop task "..." --repo OWNER/REPO`) for task-based flows.
