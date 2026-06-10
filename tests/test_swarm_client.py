from pathlib import Path

import pytest

from chat_orchestrate.backends import CLAUDE_CODE_BACKEND, CODEX_BACKEND, GEMINI_CLI_BACKEND
from chat_orchestrate.config import Settings
from chat_orchestrate.models import AgentSpec, ProgressUpdate, ProjectSpace
from chat_orchestrate.swarm_client import LocalAgentCliClient
import chat_orchestrate.swarm_client as swarm_client


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
        lambda backend_name, command, prompt, workspace: ["python", "-c", script],
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
async def test_selected_cli_failure_falls_back_to_visible_workspace_code(tmp_path: Path) -> None:
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
    assert any("wrote workspace code" in event.message for event in progress)
    assert (project.path / "frontend" / "index.html").exists()
    assert (project.path / "backend" / "app.py").exists()
    assert "Code fallback" in final
