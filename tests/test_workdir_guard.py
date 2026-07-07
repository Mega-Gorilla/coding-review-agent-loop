from agent_loop_helpers import *  # noqa: F403


def test_workdir_guard_rejects_outside_home_path(tmp_path):
    assigned = tmp_path / "claude" / "repo"
    assigned.mkdir(parents=True)

    with pytest.raises(AgentLoopError, match="outside the assigned checkout"):
        validate_test_commands_within_workdir(
            ("cd ~/llm-dialectic && python -m pytest",),
            assigned_workdir=assigned,
        )

def test_workdir_guard_rejects_outside_absolute_cwd(tmp_path):
    assigned = tmp_path / "claude" / "repo"
    assigned.mkdir(parents=True)

    with pytest.raises(AgentLoopError, match="outside the assigned checkout"):
        validate_test_commands_within_workdir(
            ("cd /outside && python -m pytest",),
            assigned_workdir=assigned,
        )

def test_workdir_guard_rejects_windows_path_with_clear_message(tmp_path):
    assigned = tmp_path / "claude" / "repo"
    assigned.mkdir(parents=True)

    with pytest.raises(
        AgentLoopError,
        match="cannot be validated against the assigned Unix checkout",
    ):
        validate_test_commands_within_workdir(
            (r"cd C:\Users\dev\repo && python -m pytest",),
            assigned_workdir=assigned,
        )

def test_workdir_guard_accepts_assigned_absolute_path(tmp_path):
    assigned = tmp_path / "claude" / "repo"
    tests_dir = assigned / "tests"
    tests_dir.mkdir(parents=True)

    validate_test_commands_within_workdir(
        (f"cd {assigned} && python -m pytest {tests_dir}",),
        assigned_workdir=assigned,
    )

def test_workdir_guard_accepts_absolute_test_path_inside_checkout(tmp_path):
    assigned = tmp_path / "claude" / "repo"
    test_file = assigned / "tests" / "test_agent_loop.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")

    validate_test_commands_within_workdir(
        (f"python -m pytest {test_file}",),
        assigned_workdir=assigned,
    )

def test_workdir_guard_ignores_environment_prose_in_tests_line(tmp_path):
    assigned = tmp_path / "claude" / "repo"
    assigned.mkdir(parents=True)
    text = (
        "Tests: `python -m pytest` run from a fresh `pip install -e '.[dev]'` venv "
        "(matching CI exactly) — 1332 passed, 0 failed."
    )

    validate_response_tests_within_workdir(text, assigned_workdir=assigned)

def test_workdir_guard_ignores_url_like_prose_in_tests_line(tmp_path):
    assigned = tmp_path / "claude" / "repo"
    assigned.mkdir(parents=True)
    text = (
        "Tests: `.venv/bin/python -m pytest tests/test_server.py tests/test_security.py "
        "tests/test_pricing.py tests/test_summarizer.py tests/test_language.py "
        "tests/test_providers.py tests/test_attachments.py -q` (the mandated CI command) - "
        "435 passed. Also ran `tests/test_database.py`, `tests/test_sitemap.py`, and the new "
        "`tests/test_routes.py` - all passed. Did not run the Playwright E2E suite "
        "(`test_debate_e2e.js`, `test_csrf_redirect_e2e.js`); those target a live "
        "`https://dev.aispar.app` session cookie and are not runnable from this sandboxed "
        "checkout - recommend running them before any prod deploy, per project policy."
    )

    validate_response_tests_within_workdir(text, assigned_workdir=assigned)

def test_workdir_guard_accepts_javascript_regex_closing_script_tag(tmp_path):
    assigned = tmp_path / "codex" / "repo"
    assigned.mkdir(parents=True)

    validate_test_commands_within_workdir(
        (
            r"""node -e "const fs=require('fs'); const html=fs.readFileSync('server/static/index.html','utf8'); const scripts=[...html.matchAll(/<script(?![^>]*\\bsrc=)[^>]*>([\\s\\S]*?)<\\/script>/gi)].map(m=>m[1]); scripts.forEach((code,i)=>{ try { new Function(code); } catch(e) { console.error('script '+i+' parse failed'); throw e; } }); console.log(scripts.length+' inline scripts parsed');" (failed: naive regex matched non-code text)""",
        ),
        assigned_workdir=assigned,
    )

def test_workdir_guard_accepts_relative_test_commands(tmp_path):
    assigned = tmp_path / "claude" / "repo"
    assigned.mkdir(parents=True)

    validate_test_commands_within_workdir(
        ("python -m pytest tests/test_agent_loop.py", "make test"),
        assigned_workdir=assigned,
    )

def test_workdir_guard_extracts_tests_section_only(tmp_path):
    assigned = tmp_path / "claude" / "repo"
    assigned.mkdir(parents=True)
    text = (
        "Issue context mentioned Tests: cd ~/other && pytest.\n\n"
        "Implemented.\n"
        "Tests: python -m pytest tests/test_agent_loop.py passed.\n"
        "<!-- AGENT_PR: 77 -->"
    )

    assert extract_reported_tests_from_response(text) == (
        "python -m pytest tests/test_agent_loop.py passed.",
    )
    validate_response_tests_within_workdir(text, assigned_workdir=assigned)
