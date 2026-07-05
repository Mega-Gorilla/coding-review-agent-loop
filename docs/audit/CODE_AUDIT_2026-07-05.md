# Code Audit — coding-review-agent-loop

**Date:** 2026-07-05
**Auditor:** Anthropic Claude (Claude Code)
**Scope:** Full repository at commit `d3faf6c` (main) — `src/coding_review_agent_loop/`, `helpers/`, `tests/`, docs, CI.
**Method:** Complete read of the security-relevant modules (runner, github, config, agents, workdir_guard, helpers) and targeted reads of the orchestration core; full test suite executed locally; per-module coverage measured with `coverage.py`; documentation cross-checked against code.

---

## Executive Summary

The codebase is in **very good health** — unusually disciplined for a fast-moving solo project (415 commits). Robustness engineering and test discipline are the standout strengths; module size and a duplicated orchestration stack are the main weaknesses. There is one real security finding: skill mode silently applies agent permission-bypass flags that the CLI mode requires explicit opt-in for, contradicting the documentation.

| Dimension | Grade | One-line assessment |
|-----------|-------|---------------------|
| Security | **B+** | Excellent subprocess hygiene; skill mode silently bypasses agent sandboxes, and the human-reviewer trust signal is spoofable |
| Robustness | **A−** | Exceptional failure taxonomy, recovery ladders, and crash-window handling; a few brittle substring classifiers |
| Testing | **A** | 1,367 tests green in ~60s; CI runs the whole suite opt-out; 82% measured coverage; no lint/type gate |
| Documentation | **A−** | Comprehensive README/SKILL/docs with rationale everywhere; one accuracy gap (permission flags) |
| Structure & cleanliness | **B−** | Two 4,000–5,600-line monoliths and a dual CLI/skill orchestration stack; otherwise clean, zero TODOs, no dead code found |
| **Overall** | **B+** | Ship-worthy; fix the skill-mode permission disclosure now, then invest in decomposition |

**Test run result (2026-07-05):** `1367 passed in 60.49s` — the full suite (which is exactly what CI runs) is green.
**Coverage (2026-07-05, in-process):** 82% overall; every `src/` module ≥ 76%, most ≥ 88%. The `helpers/` scripts show 0–56% only because tests drive them as subprocesses (see T-2).

The highest-priority finding is **SEC-1** — skill mode hardcodes `--dangerously-bypass-approvals-and-sandbox` (Codex) and `--dangerously-skip-permissions` (Antigravity) while the docs state permission bypasses are opt-in.

---

## 1. Security — B+

### Threat model context

This is a local, single-operator tool: it shells out to the operator's own authenticated CLIs (`gh`, `claude`, `codex`, `gemini`, `agy`) against repos the operator chooses. The findings below matter most when the loop is pointed at a repo where **other people can write issues, comments, or PRs** — every one of those surfaces feeds agent prompts.

### What's done well

- **No shell injection surface.** Every subprocess call in `runner.py`, `github.py`, and the helpers uses list-form argv; there is no `shell=True`, `os.system`, `eval`, or `exec` anywhere (the two `ast.literal_eval` calls in `migrations.py` are the safe variant).
- **Comment bodies are posted via `--body-file` temp files** (`github.py`), so GitHub-bound content can't be confused with CLI arguments and `NamedTemporaryFile` gives the file 0600 permissions.
- **No credential handling at all.** The tool never touches API keys or tokens; auth is delegated entirely to the already-authenticated CLIs. Nothing secret is written to state files or logs by design.
- **Careful workdir stewardship** (`config.py`): agent checkouts are validated against the expected `origin` remote before use; user-owned dirty checkouts are rejected rather than cleaned; only tool-owned default dirs under `/tmp` are ever `reset --hard`/`clean -fd`.
- **Defense-in-depth against a misbehaving coder:** `workdir_guard.py` rejects coder responses that report tests run outside the assigned checkout, and `validate_assigned_head_advanced` catches a coder that claims a PR without commits in its assigned checkout. (Both validate self-reported text — an accident guard, not an adversarial control, and the docstrings say so.)
- **Process lifecycle:** agents run in their own session (`start_new_session=True`) with `killpg` on timeout/interrupt; log dirs get an auto-written `.gitignore` so agent transcripts can't be committed into target repos.
- **CLI mode permission posture is right:** no bypass flags by default; `--dangerous-agent-permissions` is an explicit, documented opt-in (`docs/local_agent_loop.md` § Agent Permission Flags).
- **Merge is gated:** skill mode never merges (documented "always a human decision"); CLI `--auto-merge` is opt-in and waits on a named CI check.

### Findings

| ID | Severity | Finding |
|----|----------|---------|
| SEC-1 | **High** | **Skill mode hardcodes agent permission bypasses, contradicting the docs.** `helpers/run_external.py` builds its config with `codex_args=("--dangerously-bypass-approvals-and-sandbox",)` and `antigravity_args=("--dangerously-skip-permissions",)` unconditionally (lines 304–311), with no flag to opt out. Meanwhile `docs/local_agent_loop.md` states "By default, this standalone package does not pass permission-bypass flags" — true only for CLI mode — and neither `SKILL.md` nor `docs/skill_mode.md` mentions the bypass at all. External agents processing untrusted GitHub content (issue bodies, comments) thus run with sandbox/approvals off, silently. Fix: plumb an explicit `--dangerous-agent-permissions`-equivalent through `skill_runner.py` → `run_external.py` (or at minimum document the current behavior prominently in SKILL.md and skill_mode.md). |
| SEC-2 | Medium | **The "signed human requirement" signal is spoofable.** `parse_signed_human_requirement_body` (`protocol.py:412`) treats *any* issue/PR comment ending with a standalone `-- Human Reviewer` line as a signed human requirement, and `format_human_requirements` (`prompts.py`) instructs the coder to treat these as "high-priority requirements" that must be implemented or explicitly refused. On a repo where others can comment, any GitHub user can mint such a requirement — a direct prompt-injection escalation channel into a coder that (per SEC-1, in skill mode) runs without a sandbox. Fix options: restrict recognition to an author allowlist (e.g. repo owner / configured logins, checkable from the comment's `authorAssociation`), or document explicitly that the loop must only run on repos where all commenters are trusted. |
| SEC-3 | Low | **Untrusted GitHub text is embedded verbatim in prompts** (`format_issue_context`, comment blocks) with no data/instruction delimiting (fencing, "content below is data, not instructions" framing). Prompt injection can never be fully prevented, but explicit delimiting measurably raises the bar and is cheap to add to the prompt builders. |
| SEC-4 | Low | **Public response files live under a shared, predictable `/tmp` path.** `public_response_path` (`agents/base.py`) writes agent responses to `/tmp/coding-review-agent-loop/responses/<repo>/<agent>/<uuid>.md` with default umask permissions, so on a multi-user machine other local users can read review/plan content for private repos (the first user to create the parent directory also owns it). Consider `tempfile.mkdtemp` under the user's `XDG_RUNTIME_DIR`/cache dir, or `chmod 0700` on the base dir. |
| SEC-5 | Info | `.claude/settings.json` allowlists broad Bash prefixes (`python3 *`, `sed *`, `find *`) for skill sessions. Reasonable for this tool's own development; just note that it applies to any Claude Code session in this directory. |

---

## 2. Robustness — A−

### What's done well

This is the strongest dimension. The codebase treats agent CLIs as hostile weather and it shows:

- **Failure taxonomy with correct retry semantics** (`transient.py`, orchestrator `_failure_category`): transient signals (429, overloaded, quota, timeouts) are retried with backoff; non-retryable signals (auth, billing, dirty checkout) *override* transient matches and are never retried; a wall-clock timeout is deliberately **not** retried ("a kill deadline is not a provider hiccup"); quota exhaustion with a far-future reset exits with a distinct code 3 (`QuotaResetExceededError`) so callers can distinguish "retry later" from "broken".
- **A structured-response recovery ladder** before anything is declared failed: safe normalizers → envelope/fence cleanup → stdout-marker stripping → plan-revision human-ack reconstruction → an LLM repair pass (`repair.py`, with its own model chain and timeout) → a manual `retry-validate` escape hatch that saves the raw response plus context to a stable repair dir. Recovered responses are re-validated through the same validator, never trusted.
- **Crash-window recovery everywhere state is created remotely:** split-issue materialization posts its idempotency marker only after all children exist and *adopts* already-created children on rerun (`search_issues`, #476); an aborted implementation run resumes from an open PR that references the issue instead of duplicating it (#495); pending comments are reconciled from session state.
- **Resume is metadata-driven:** every posted comment carries a single-line base64 `AGENT_LOOP_META` marker; rounds reconstruct deterministically from GitHub comments rather than local state alone, so a crashed process on a different machine can resume.
- **Process-group correctness under concurrency:** the parallel discuss path registers live processes in a lock-guarded registry, and `KeyboardInterrupt` in the main thread kills every group and poisons the runner against new spawns (#475). The PTY path (needed because `agy` drops output on non-TTY stdout) handles EIO, fd cleanup on spawn failure, and timeout kills.
- **Reviewer resilience:** an unavailable reviewer marks the round `incomplete` rather than aborting — and a round with a missing reviewer is *never* reported `approved`.
- **A PATH-race spawn retry** (`_spawn_with_retry`) distinguishes a dangling symlink (e.g. mid-upgrade CLI) from a genuinely missing command, retrying only the former.

### Findings

| ID | Severity | Finding |
|----|----------|---------|
| R-1 | Medium | **Free-text transient classification can misfire.** `TRANSIENT_AGENT_OUTPUT_RE` matches words like `\bquota\b`, `timeout`, `overloaded` anywhere in the classified text. The code already bounds what it classifies (`_bounded_failure_classification_text`, public-response-specific rules), but an agent *review* that legitimately discusses rate limiting or timeouts in a failure message path could still be misclassified and retried/suppressed. Worth a regression test corpus of "innocent" texts and, longer term, anchoring on structured error output where the CLIs provide it. |
| R-2 | Low | **`state_manager._fetch_issue_comments` splits `gh --jq '.[].body'` output on newlines**, so multi-line comment bodies arrive as one fragment per line. This is safe today only because resume consumes single-line base64 `AGENT_LOOP_META` markers — a fragile invariant that deserves a comment, or a switch to JSON output (`--json body`) to get real comment boundaries. |
| R-3 | Low | **HTTP status classified by substring.** `_fetch_branch_protection_required_checks` (`github.py:504`) detects 404/403 by searching `"404"`/`"403"` in combined stdout+stderr — a check name or URL containing those digits misclassifies. `gh api` exit handling or `--include` status parsing would be exact. |
| R-4 | Low | `wait_for_ci` sleeps by spawning `runner.run(["sleep", N])` — presumably for dry-run visibility, but it's an odd dependency on an external binary where `time.sleep` (guarded by dry-run) would do. |
| R-5 | Low | **No version discipline:** `pyproject.toml` has said `0.1.0` across 415 commits, and there are no git tags or releases. `update-agent-loop.sh` (install-by-pull) makes this survivable for the author, but any second user has no way to pin or report a version. |

---

## 3. Testing — A

### What's done well

- **1,367 tests, all passing, in ~60 seconds**, with zero external dependencies (`dependencies = []`; dev needs only pytest). Agent CLIs and `gh` are faked at the process boundary, so the suite runs anywhere.
- **CI runs `python -m pytest` on the whole tree** (`.github/workflows/ci.yml`) — omission of a new test file is impossible by construction (the exact opt-out design the llm-dialectic audit had to recommend).
- **Measured coverage is strong where it counts:** `src/` overall lands at 90%+ for the orchestration core (orchestrator 90%, protocol 96%, prompts 96%, repair 95%, round_state 94%, followups 97%). The regression style is disciplined — tests cite the issue numbers they lock down.
- **The helpers are tested end-to-end as subprocesses** (`tests/test_skill_helpers.py`, 4,147 lines), which exercises argument parsing and exit codes exactly as the skill uses them.

### Findings

| ID | Severity | Finding |
|----|----------|---------|
| T-1 | Medium | **No lint or type-check gate.** The code is consistently type-annotated and clean, but nothing enforces it: no ruff/flake8, no mypy/pyright, in CI or config. At this codebase size (22K src lines), a ruff + mypy job is cheap insurance against the classes of bug the test suite can't see (unused imports, unreachable branches, annotation drift). |
| T-2 | Low | **Helper coverage is invisible.** Because `test_skill_helpers.py` drives `helpers/*.py` via `subprocess.run`, in-process coverage reports 0% for `state_manager`/`gh_ops`/`render_response`/`demo_loop` and 56% for `skill_runner` even though they are tested. Enabling subprocess coverage (`coverage run --parallel` + `COVERAGE_PROCESS_START`) or importing the mains in-process for some tests would make real gaps visible. |
| T-3 | Low | **No coverage measurement in CI.** Coverage had to be measured manually for this audit; a `coverage` step with a low ratcheting floor would keep the 82% from silently eroding. |
| T-4 | Low | Several test files are monoliths mirroring the src monoliths (`test_agent_loop.py` 3,955 lines, `test_orchestrator_pr.py` 3,855, `test_skill_helpers.py` 4,147). Fine while green, but they will resist navigation exactly when a refactor (S-1/S-2) needs them most. |

---

## 4. Documentation — A−

### What's done well

- **The README (41KB) is genuinely excellent** for an open-source tool: what it is, who it's for, why not GitHub Actions, honest comparisons with four similar tools, quick start, a real worked example, and a dated, sourced billing note about `claude -p` terms.
- **`docs/local_agent_loop.md` (1,100+ lines)** documents architecture, every CLI flag, the full response protocol (markers, structured JSON kinds), workdir semantics, and permission flags. **`docs/skill_mode.md` + `SKILL.md`** do the same for skill mode, including round states, resume, reversed roles, and guardrails.
- **Inline comments explain *why* and cite issues** (`#475`, `#476`, `#495`…) at nearly every non-obvious decision — the PTY rationale in `runner.py` and the crash-window comments in `github.py` are exemplary. This is the practice the llm-dialectic audit praised, applied even more consistently here.

### Findings

| ID | Severity | Finding |
|----|----------|---------|
| D-1 | **Medium** | **The permission-flags documentation is wrong for skill mode** (the doc half of SEC-1): `docs/local_agent_loop.md` promises bypasses are opt-in, `SKILL.md`/`docs/skill_mode.md` are silent, and skill mode hardcodes them. Whatever the code resolution of SEC-1, the docs must state the skill-mode posture explicitly. |
| D-2 | Low | **No CHANGELOG or release notes**, compounding R-5. The commit log is well-written, but a user updating via `update-agent-loop.sh` has no digest of behavior changes. |
| D-3 | Low | Dated external claims (the Claude billing postponement note, the comparison table) will silently rot; each carries an as-of date, which helps — consider a "last verified" sweep habit. |

---

## 5. Structure & Cleanliness — B−

The code *within* modules is clean: no TODOs/FIXMEs anywhere, no dead code found, small modules (`transient.py`, `workdirs.py`, `errors.py`, `logging.py`, `checks.py`) are exactly the right size, and the agents package is a textbook Protocol-based backend registry. The problem is the top of the size distribution and a structural fork.

| ID | Severity | Finding |
|----|----------|---------|
| S-1 | **High** (maintainability) | **`orchestrator.py` is 5,573 lines** spanning at least six responsibilities: failure classification/diagnostics (~1,300 lines), the validated-agent retry/recovery engine (`_run_validated_agent`, ~550 lines of deeply nested conditionals), the plan-first loop, the issue loop, the PR loop, the task loop, and the entire discuss subsystem (~1,500 lines). Natural seams already exist — `failure_classification.py`, `validated_agent.py`, `discuss.py`, `loops/{issue,pr,task}.py` — and the 90% test coverage makes the split mechanical and safe. |
| S-2 | High (maintainability) | **Dual orchestration stacks.** `helpers/skill_runner.py` (4,224 lines) re-implements round orchestration (reviewer turns, coder turns, resume, repair routing, followups) as a subprocess-driven mirror of `orchestrator.py`, coordinated through JSON files and CLI args. The parity burden is real and already tracked (#294 and successors). Every protocol change now lands twice. Long-term: extract the round engine into a shared library both entry points call, keeping only the session-boundary plumbing in `skill_runner`. |
| S-3 | Medium | **`helpers/` sits outside the package** and reaches into it via `sys.path.insert(0, .../src)` (`run_external.py:28`, `state_manager.py:61`, `skill_runner.py`), while also importing underscore-private functions from `round_state`. Moving `helpers/` under `src/coding_review_agent_loop/skill/` (they're already invoked as `python -m helpers.x` — a rename plus console-script entries would do) removes the path hacks and makes the private imports honest. |
| S-4 | Medium | **Repo hygiene:** `update-agent-loop.sh` (referenced by docs) and `.claude/settings.json` are untracked — commit them or ignore them deliberately; the agent workdir directories the tool creates in-repo during dogfooding (`claude/`, `codex/`, `gemini/`, `antigravity/`, including stale April logs under `codex/repo/.agent-loop-logs/`) are neither tracked nor gitignored — add them to `.gitignore` (or point dogfooding workdirs at the default `/tmp` location). |
| S-5 | Low | `prompts.py` (2,570) and `protocol.py` (2,146) are near their limits; each is at least single-purpose, so they're lower priority than S-1/S-2 — but don't let them absorb the next feature by default. |
| S-6 | Low | Alembic migration-topology validation (`migrations.py`) is a generic feature but reads as tuned to one consumer project; a one-line module docstring saying when it activates (repo contains `alembic/versions/`) would orient readers. *(It has one — this is satisfied; noted for completeness.)* |

---

## 6. Prioritized Recommendations

**Now (security/correctness):**
1. **SEC-1 / D-1** — make skill-mode permission bypasses explicit: plumb an opt-in flag through `skill_runner` → `run_external`, or document the hardcoded bypass in SKILL.md/skill_mode.md in bold. This is the only finding where documented behavior and actual behavior disagree on a safety property.
2. **SEC-2** — gate `-- Human Reviewer` recognition on comment author (owner/allowlist via `authorAssociation`), or document the all-commenters-trusted assumption.

**Next (cheap, high-value):**
3. **T-1** — add ruff + mypy to CI (start permissive, ratchet).
4. **SEC-3** — add data/instruction delimiting around embedded GitHub content in the prompt builders.
5. **S-4** — `.gitignore` the in-repo agent workdirs; commit or ignore `update-agent-loop.sh` and `.claude/settings.json`.
6. **T-3** — add a coverage step (with subprocess coverage, T-2) and a floor to CI.
7. **R-2 / R-3** — switch `state_manager` comment fetching to JSON-bounded bodies; classify GitHub API errors by status rather than substring.

**Then (structural, ongoing):**
8. **S-1** — split `orchestrator.py` along its existing seams (failure classification → validated-agent engine → per-flow loops → discuss).
9. **S-2** — extract a shared round engine so CLI and skill modes stop diverging (continue #294's direction).
10. **S-3** — fold `helpers/` into the package and drop the `sys.path` hacks.
11. **R-5 / D-2** — start tagging versions and keeping a minimal CHANGELOG.

---

*This audit reflects the repository state at commit `d3faf6c` (2026-07-05). Line references may drift as the code evolves.*
