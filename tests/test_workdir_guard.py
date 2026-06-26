import pytest

from agent_loop_helpers import *  # noqa: F403


@pytest.fixture(autouse=True)
def _no_real_repair():
    """Prevent orchestrator repair from invoking a real agent in this module."""
    with patch("coding_review_agent_loop.orchestrator.attempt_repair", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _agent_commands_available(monkeypatch):
    """Keep config tests independent of agent CLIs installed on the test host."""
    import coding_review_agent_loop.config as config_module

    real_which = config_module.shutil.which

    def which(command):
        resolved = real_which(command)
        if resolved is not None:
            return resolved
        if command in {"claude", "codex", "gemini", "agy"}:
            return f"/mock/bin/{command}"
        return None

    monkeypatch.setattr(config_module.shutil, "which", which)


def test_workdir_guard_rejects_outside_home_path(tmp_path):
    assigned = tmp_path / "claude" / "repo"
    assigned.mkdir(parents=True)

    with pytest.raises(AgentLoopError, match="outside the assigned checkout"):
        validate_test_commands_within_workdir(
            ("cd ~/llm-dialectic && python -m pytest",),
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
