from pathlib import Path

import pytest

from chat_orchestrate.backends import CLAUDE_CODE_BACKEND, CODEX_BACKEND, GEMINI_CLI_BACKEND, sanitized_agent_environment
from chat_orchestrate.config import Settings
from chat_orchestrate.models import AgentSpec, ProgressUpdate, ProjectSpace
from chat_orchestrate.swarm_client import LocalAgentCliClient, is_benign_agent_stderr
import chat_orchestrate.swarm_client as swarm_client


def test_cli_prompt_declares_project_workspace_writable(tmp_path: Path) -> None:
    project = ProjectSpace("demo", tmp_path / "demo")
    client = LocalAgentCliClient(Settings(), preferred_backend=CODEX_BACKEND)

    prompt = client._agent_prompt(
        AgentSpec("Engineer", "implementation specialist", "Build the requested code."),
        project,
        "create a website",
        "",
    )

    assert "read-write project workspace" in prompt
    assert "create or update files" in prompt
    assert "Do not claim the session is read-only" in prompt
    assert str(project.path) in prompt


def test_tcp_time_wait_stderr_is_treated_as_benign() -> None:
    assert is_benign_agent_stderr("codex stderr: TCP [::1]:8000 [::1]:49358 TIME_WAIT 0")


def test_arg0_temp_cleanup_warning_is_treated_as_benign() -> None:
    assert is_benign_agent_stderr(
        "WARNING: failed to clean up stale arg0 temp dirs: Access is denied. (os error 5)"
    )


def test_codex_state_db_warning_is_not_treated_as_benign() -> None:
    assert not is_benign_agent_stderr(
        "2026-06-11T01:02:12Z WARN codex_rollout::state_db: failed to initialize state "
        "runtime: failed to initialize in-process app-server client: Access is denied. (os error 5)"
    )


@pytest.mark.asyncio
async def test_cli_agent_hides_noisy_runtime_stderr(monkeypatch) -> None:
    script = (
        "import sys;"
        "sys.stderr.write('title: \"When a launch brings crowds and chaos\"\\n');"
        "sys.stderr.write('export function notFound(message = \"Not found\") {}\\n');"
        "sys.stderr.write('export function unauthorized(message = \"Missing or invalid admin API key\") {}\\n');"
        "sys.stdout.write('useful output\\n');"
        "sys.stdout.flush()"
    )

    monkeypatch.setattr(
        swarm_client,
        "task_command_args",
        lambda backend_name, command, prompt, workspace, final_output=None: ["python", "-c", script],
    )
    client = LocalAgentCliClient(Settings(local_agent_timeout_seconds=10), preferred_backend=CODEX_BACKEND)
    client._command_for_backend = lambda backend_name: "python"

    events = [
        event
        async for event in client._run_backend_events(
            CODEX_BACKEND,
            "prompt",
            ProjectSpace("demo", Path(".")),
            AgentSpec("Backend", "backend builder", ""),
        )
    ]
    progress = [event for event in events if isinstance(event, ProgressUpdate)]
    final = next(event for event in reversed(events) if isinstance(event, str))

    assert not any("title:" in event.message.lower() for event in progress)
    assert not any("export function notfound" in event.message.lower() for event in progress)
    assert not any("invalid admin api key" in event.message.lower() for event in progress)
    assert "useful output" in final


def test_dead_local_proxy_env_is_removed_for_agent_processes() -> None:
    env = sanitized_agent_environment(
        {
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://localhost:9",
            "ALL_PROXY": "http://proxy.example:8080",
            "NO_PROXY": "localhost,127.0.0.1",
        }
    )

    assert "HTTP_PROXY" not in env
    assert "HTTPS_PROXY" not in env
    assert env["ALL_PROXY"] == "http://proxy.example:8080"
    assert env["NO_PROXY"] == "localhost,127.0.0.1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "env_var", "api_key"),
    [
        (CODEX_BACKEND, "OPENAI_API_KEY", "openai-live-key"),
        (CLAUDE_CODE_BACKEND, "ANTHROPIC_API_KEY", "claude-live-key"),
        (GEMINI_CLI_BACKEND, "GEMINI_API_KEY", "gemini-live-key"),
    ],
)
async def test_cli_agent_streams_output_and_env_for_all_backends(
    backend: str,
    env_var: str,
    api_key: str,
    monkeypatch,
) -> None:
    script = (
        "import os,sys,time;"
        f"sys.stdout.write('chunk-' + os.environ.get({env_var!r}, 'missing') + '-' + ('x' * 180));"
        "sys.stdout.flush();"
        "time.sleep(.1);"
        f"sys.stderr.write('\\rproblem-' + os.environ.get({env_var!r}, 'missing'));"
        "sys.stderr.flush();"
        "time.sleep(.1);"
        "sys.stdout.write('\\ncomplete\\n');"
        "sys.stdout.flush()"
    )

    monkeypatch.setattr(
        swarm_client,
        "task_command_args",
        lambda backend_name, command, prompt, workspace, final_output=None: ["python", "-c", script],
    )
    client = LocalAgentCliClient(
        Settings(local_agent_timeout_seconds=10),
        preferred_backend=backend,
        api_keys={backend: api_key},
    )
    client._command_for_backend = lambda backend_name: "python"

    events = [
        event
        async for event in client._run_backend_events(
            backend,
            "prompt",
            ProjectSpace("demo", Path(".")),
            AgentSpec("Engineer", "implementation specialist", ""),
        )
    ]
    progress = [event for event in events if isinstance(event, ProgressUpdate)]
    final = next(event for event in reversed(events) if isinstance(event, str))

    assert any(event.phase == "agent-output" and f"chunk-{api_key}" in event.message for event in progress)
    assert any(event.phase == "agent-warning" and f"problem-{api_key}" in event.message for event in progress)
    assert f"chunk-{api_key}" in final
    assert "complete" in final


@pytest.mark.asyncio
async def test_cli_agent_suppresses_benign_codex_startup_warnings(monkeypatch) -> None:
    benign = (
        "2026-06-10T10:02:06Z WARN codex_core_skills::loader: "
        "ignoring interface.icon_small: icon path with '..' must resolve under plugin assets/"
    )
    script = (
        "import sys;"
        f"sys.stderr.write({benign!r} + '\\n');"
        "sys.stderr.write('actual recoverable problem\\n');"
        "sys.stdout.write('useful output\\n');"
        "sys.stdout.flush()"
    )

    monkeypatch.setattr(
        swarm_client,
        "task_command_args",
        lambda backend_name, command, prompt, workspace, final_output=None: ["python", "-c", script],
    )
    client = LocalAgentCliClient(Settings(local_agent_timeout_seconds=10), preferred_backend=CODEX_BACKEND)
    client._command_for_backend = lambda backend_name: "python"

    events = [
        event
        async for event in client._run_backend_events(
            CODEX_BACKEND,
            "prompt",
            ProjectSpace("demo", Path(".")),
            AgentSpec("Frontend", "frontend builder", ""),
        )
    ]
    progress = [event for event in events if isinstance(event, ProgressUpdate)]
    final = next(event for event in reversed(events) if isinstance(event, str))

    assert not any("interface.icon_small" in event.message for event in progress)
    assert any("actual recoverable problem" in event.message for event in progress)
    assert "useful output" in final
    assert "interface.icon_small" not in final


@pytest.mark.asyncio
async def test_cli_agent_finishes_when_artifacts_are_written_and_process_goes_quiet(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(swarm_client, "ARTIFACT_FINISH_MIN_SECONDS", 0.2)
    monkeypatch.setattr(swarm_client, "ARTIFACT_FINISH_QUIET_SECONDS", 0.2)
    script = (
        "import pathlib,sys,time;"
        "root=pathlib.Path.cwd();"
        "(root/'frontend').mkdir(exist_ok=True);"
        "(root/'frontend'/'index.html').write_text('<html>ok</html>', encoding='utf-8');"
        "\nfor _ in range(40):\n"
        "    sys.stderr.write('Browser use was stopped in the extension; permission prompt text\\n')\n"
        "    sys.stderr.flush()\n"
        "    time.sleep(.05)\n"
        "time.sleep(10)"
    )
    monkeypatch.setattr(
        swarm_client,
        "task_command_args",
        lambda backend_name, command, prompt, workspace, final_output=None: ["python", "-c", script],
    )
    project = ProjectSpace("demo", tmp_path / "demo")
    project.path.mkdir(parents=True)
    client = LocalAgentCliClient(Settings(local_agent_timeout_seconds=20), preferred_backend=CODEX_BACKEND)
    client._command_for_backend = lambda backend_name: "python"

    events = [
        event
        async for event in client._run_backend_events(
            CODEX_BACKEND,
            "prompt",
            project,
            AgentSpec("Frontend", "frontend builder", ""),
        )
    ]
    progress = [event for event in events if isinstance(event, ProgressUpdate)]
    final = next(event for event in reversed(events) if isinstance(event, str))

    assert any("CLI is quiet" in event.message for event in progress)
    assert not any("Browser use was stopped" in event.message for event in progress)
    assert not any(event.phase == "agent-error" and "timed out" in event.message for event in progress)
    assert "frontend/index.html" in final


def test_workspace_recovery_fills_missing_visible_frontend_backend(tmp_path: Path) -> None:
    project = ProjectSpace("demo", tmp_path / "demo")
    project.path.mkdir(parents=True)
    (project.path / "server.js").write_text('console.log("api")', encoding="utf-8")
    client = LocalAgentCliClient(Settings(), preferred_backend=CODEX_BACKEND)

    message = client._recover_workspace_artifacts(
        project,
        "build tiny website frontend backend",
        "frontend builder",
        "Workspace artifacts were written.",
    )

    assert "Filled missing visible workspace artifacts" in message
    assert (project.path / "frontend" / "index.html").exists()
    assert (project.path / "backend" / "app.py").exists()


@pytest.mark.asyncio
async def test_selected_cli_failure_does_not_fallback_to_visible_workspace_code(tmp_path: Path) -> None:
    project = ProjectSpace("demo", tmp_path / "demo")
    client = LocalAgentCliClient(
        Settings(local_agent_timeout_seconds=1),
        preferred_backend=CODEX_BACKEND,
    )
    client._command_for_backend = lambda backend_name: None

    events = [
        event
        async for event in client.run_agent_events(
            AgentSpec("Frontend", "frontend builder", ""),
            project,
            "build a website that looks like github",
            "",
        )
    ]
    progress = [event for event in events if isinstance(event, ProgressUpdate)]
    final = next(event for event in reversed(events) if isinstance(event, str))

    assert any(event.phase == "agent-warning" and "not callable" in event.message for event in progress)
    assert not any("wrote workspace code" in event.message for event in progress)
    assert not (project.path / "frontend" / "index.html").exists()
    assert not (project.path / "backend" / "app.py").exists()
    assert "did not receive a usable response" in final
    assert "did not generate replacement code" in final


@pytest.mark.asyncio
async def test_codex_runtime_failure_uses_api_fallback_when_key_is_saved(tmp_path: Path) -> None:
    project = ProjectSpace("demo", tmp_path / "demo")
    client = LocalAgentCliClient(
        Settings(local_agent_timeout_seconds=1),
        preferred_backend=CODEX_BACKEND,
        openai_api_key="sk-test",
    )

    async def fake_backend_events(backend, prompt, project, agent):
        yield "codex_rollout::state_db: failed to open state db: readonly database"

    async def fake_api(prompt):
        return "Recovered answer from Codex API."

    client._run_backend_events = fake_backend_events
    client._run_codex_api = fake_api

    events = [
        event
        async for event in client.run_agent_events(
            AgentSpec("Coordinator", "coordinator", ""),
            project,
            "fix the site",
            "",
        )
    ]
    progress = [event for event in events if isinstance(event, ProgressUpdate)]
    final = next(event for event in reversed(events) if isinstance(event, str))

    assert any(event.phase == "agent-recovery" for event in progress)
    assert "API fallback response" in final
    assert "Recovered answer from Codex API." in final


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "api_key"),
    [
        (CLAUDE_CODE_BACKEND, "claude-key"),
        (GEMINI_CLI_BACKEND, "gemini-key"),
    ],
)
async def test_runtime_failure_uses_matching_api_fallback_for_other_agents(
    tmp_path: Path,
    backend: str,
    api_key: str,
) -> None:
    project = ProjectSpace("demo", tmp_path / "demo")
    client = LocalAgentCliClient(
        Settings(local_agent_timeout_seconds=1),
        preferred_backend=backend,
        api_keys={backend: api_key},
    )

    async def fake_backend_events(backend_name, prompt, project, agent):
        yield f"{backend_name} authentication failed: not logged in"

    async def fake_api(backend_name, prompt, project=None):
        assert backend_name == backend
        return f"Recovered answer from {backend_name} API."

    client._run_backend_events = fake_backend_events
    client._run_backend_api_fallback = fake_api

    events = [
        event
        async for event in client.run_agent_events(
            AgentSpec("Engineer", "engineer", ""),
            project,
            "fix the site",
            "",
        )
    ]
    progress = [event for event in events if isinstance(event, ProgressUpdate)]
    final = next(event for event in reversed(events) if isinstance(event, str))

    assert any(event.phase == "agent-recovery" for event in progress)
    assert "API fallback response" in final
    assert f"Recovered answer from {backend} API." in final


@pytest.mark.asyncio
async def test_codex_runtime_failure_without_api_key_surfaces_local_failure(tmp_path: Path) -> None:
    project = ProjectSpace("demo", tmp_path / "demo")
    client = LocalAgentCliClient(
        Settings(local_agent_timeout_seconds=1),
        preferred_backend=CODEX_BACKEND,
    )

    async def fake_backend_events(backend, prompt, project, agent):
        yield "codex_rollout::state_db: failed to open state db: readonly database"

    client._run_backend_events = fake_backend_events

    events = [
        event
        async for event in client.run_agent_events(
            AgentSpec("Coordinator", "coordinator", ""),
            project,
            "fix the site",
            "",
        )
    ]
    progress = [event for event in events if isinstance(event, ProgressUpdate)]
    final = next(event for event in reversed(events) if isinstance(event, str))

    assert any("No OpenAI API key" in event.message for event in progress)
    assert "local response" in final
    assert "failed to open state db" in final


@pytest.mark.asyncio
async def test_read_only_cli_response_is_returned_without_workspace_recovery(tmp_path: Path, monkeypatch) -> None:
    project = ProjectSpace("demo", tmp_path / "demo")
    monkeypatch.setattr(
        swarm_client,
        "task_command_args",
        lambda backend_name, command, prompt, workspace, final_output=None: [
            "python",
            "-c",
            "print('This session is read-only, so I cannot create files yet.')",
        ],
    )
    client = LocalAgentCliClient(Settings(local_agent_timeout_seconds=5), preferred_backend=CODEX_BACKEND)
    client._command_for_backend = lambda backend_name: "python"

    events = [
        event
        async for event in client.run_agent_events(
            AgentSpec("Frontend", "frontend builder", ""),
            project,
            "build a github style website",
            "",
        )
    ]

    final = next(event for event in reversed(events) if isinstance(event, str))

    assert "read-only" in final
    assert not any(
        isinstance(event, ProgressUpdate) and "Workspace recovery wrote visible code artifacts" in event.message
        for event in events
    )
    assert not (project.path / "frontend" / "index.html").exists()
