from __future__ import annotations

from datetime import UTC, datetime

import chainlit as cl

from chat_orchestrate.backends import backend_availability, detect_agent_backends
from chat_orchestrate.config import get_settings
from chat_orchestrate.coordination import CoordinationError, CoordinationManager
from chat_orchestrate.models import OrchestrationRun
from chat_orchestrate.orchestrator import Orchestrator
from chat_orchestrate.project_space import ProjectSpaceError, ProjectSpaceManager
from chat_orchestrate.swarm_client import build_swarm_client

settings = get_settings()
agent_backends = detect_agent_backends(settings.configured_backends)
spaces = ProjectSpaceManager(settings.workspaces_root, settings.workspace_state_path)
coordination = CoordinationManager(
    settings.coordination_state_path,
    settings.machine_id,
    settings.default_agents,
    agent_backends,
    settings.orchestrator_ttl_seconds,
    settings.cluster_id,
    settings.coordination_token,
    settings.coordination_backend,
    settings.coordination_http_url,
)
orchestrator = Orchestrator(build_swarm_client(settings), settings.default_agents, coordination)


@cl.on_chat_start
async def on_chat_start() -> None:
    try:
        local_node = coordination.heartbeat()
        orchestrator_node = coordination.get_or_elect_orchestrator()
    except CoordinationError as exc:
        await cl.Message(content=f"Coordination error: {exc}").send()
        return
    existing = spaces.list_spaces()
    if existing:
        cl.user_session.set("project_space", existing[0])
        active = f"`{existing[0].name}`"
    else:
        default_space = spaces.upsert("default", "default")
        cl.user_session.set("project_space", default_space)
        active = "`default`"

    await cl.Message(
        content=(
            f"Ready. Active project space: {active}\n\n"
            f"Machine: `{local_node.machine_id}` as `{local_node.role}`\n"
            f"Orchestrator: `{orchestrator_node.machine_id}`\n\n"
            f"Cluster: `{settings.cluster_id}`\n"
            f"Coordination: `{settings.coordination_backend}`\n"
            f"Backends: `{', '.join(agent_backends)}`\n\n"
            "Commands: `/spaces`, `/use <name>`, `/create-space <name> <path>`, "
            "`/worktree <name> <repo-path> <branch>`, `/clone <name> <git-url> [branch]`, "
            "`/workspace-modes`, `/machines`, "
            "`/claim-orchestrator`, `/release-orchestrator`, `/tasks`, `/backends`."
        )
    ).send()
    await show_machines()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    text = message.content.strip()
    if text.startswith("/"):
        await handle_command(text)
        return

    project = cl.user_session.get("project_space")
    if project is None:
        await cl.Message(content="Pick a project space first with `/use <name>`.").send()
        return

    try:
        local_node = coordination.heartbeat()
        orchestrator_node = coordination.get_or_elect_orchestrator()
    except CoordinationError as exc:
        await cl.Message(content=f"Coordination error: {exc}").send()
        return
    status = cl.Message(
        content=(
            f"Starting orchestration for `{project.name}`...\n\n"
            f"Local machine: `{local_node.machine_id}`\n"
            f"Orchestrator machine: `{orchestrator_node.machine_id}`"
        )
    )
    await status.send()

    async for event in orchestrator.run(text, project):
        if isinstance(event, OrchestrationRun):
            await cl.Message(content=event.final).send()
            continue

        await cl.Message(
            author=event.agent,
            content=f"**{event.role}**\n\n{event.content}",
        ).send()


async def handle_command(text: str) -> None:
    parts = text.split()
    command = parts[0].lower()

    try:
        if command == "/spaces":
            await show_spaces()
        elif command == "/use" and len(parts) == 2:
            project = spaces.get(parts[1])
            cl.user_session.set("project_space", project)
            await cl.Message(content=f"Active project space set to `{project.name}`.").send()
        elif command == "/create-space" and len(parts) >= 3:
            name = parts[1]
            path = " ".join(parts[2:])
            project = spaces.upsert(name, path)
            cl.user_session.set("project_space", project)
            await cl.Message(content=f"Created and selected `{project.name}` at `{project.path}`.").send()
        elif command == "/worktree" and len(parts) >= 4:
            name, repo_path, branch = parts[1], parts[2], parts[3]
            project = spaces.create_worktree(name, repo_path, branch)
            cl.user_session.set("project_space", project)
            await cl.Message(content=f"Created worktree `{project.name}` at `{project.path}`.").send()
        elif command == "/clone" and len(parts) >= 3:
            name, git_url = parts[1], parts[2]
            branch = parts[3] if len(parts) >= 4 else None
            project = spaces.clone_repository(name, git_url, branch)
            cl.user_session.set("project_space", project)
            await cl.Message(content=f"Cloned and selected `{project.name}` at `{project.path}`.").send()
        elif command == "/workspace-modes":
            await show_workspace_modes()
        elif command == "/machines":
            coordination.heartbeat()
            await show_machines()
        elif command == "/claim-orchestrator":
            node = coordination.claim_orchestrator()
            await cl.Message(content=f"`{node.machine_id}` is now the orchestrator machine.").send()
            await show_machines()
        elif command == "/release-orchestrator":
            coordination.release_orchestrator()
            elected = coordination.get_or_elect_orchestrator()
            await cl.Message(content=f"Released claim. Current orchestrator: `{elected.machine_id}`.").send()
            await show_machines()
        elif command == "/tasks":
            await show_tasks()
        elif command == "/backends":
            await show_backends()
        else:
            await cl.Message(content=command_help()).send()
    except ProjectSpaceError as exc:
        await cl.Message(content=f"Project space error: {exc}").send()
    except CoordinationError as exc:
        await cl.Message(content=f"Coordination error: {exc}").send()


async def show_spaces() -> None:
    items = spaces.list_spaces()
    if not items:
        await cl.Message(content="No project spaces registered yet.").send()
        return

    lines = [
        f"- `{space.name}` `{space.mode}`: `{space.path}`"
        f"{f' on `{space.branch}`' if space.branch else ''}"
        f"{f' from `{space.git_remote}`' if space.git_remote else ''}"
        for space in items
    ]
    await cl.Message(content="## Project Spaces\n\n" + "\n".join(lines)).send()


async def show_machines() -> None:
    coordination.heartbeat()
    machines = coordination.list_machines()
    orchestrator_node = coordination.get_or_elect_orchestrator()
    content = machine_cards(machines, orchestrator_node.machine_id)
    await cl.Message(
        content=content,
        actions=machine_actions(),
    ).send()


async def show_tasks() -> None:
    tasks = coordination.list_tasks()
    if not tasks:
        await cl.Message(content="No delegated tasks recorded yet.").send()
        return

    lines = [
        f"- `{task.status}` `{task.role}` via `{task.preferred_backend}` "
        f"on `{task.assigned_machine}`: {task.title}"
        for task in tasks
    ]
    await cl.Message(content="## Recent Delegated Tasks\n\n" + "\n".join(lines)).send()


async def show_backends() -> None:
    await cl.Message(content=backend_status_cards(), actions=machine_actions()).send()


async def show_workspace_modes() -> None:
    await cl.Message(
        content=(
            "## Workspace Modes\n\n"
            "> ### Worktree Mode\n"
            "> Use when agents are working on the same repo/project with isolated branches.  \n"
            "> Command: `/worktree <name> <repo-path> <branch>`\n\n"
            "> ### Clone Mode\n"
            "> Use when agents should work on separate copies or competing versions of the same repo.  \n"
            "> Command: `/clone <name> <git-url> [branch]`\n\n"
            "> ### Local Folder Mode\n"
            "> Use when the project already exists as a local folder.  \n"
            "> Command: `/create-space <name> <path>`"
        )
    ).send()


@cl.action_callback("refresh_machines")
async def refresh_machines(_: cl.Action) -> None:
    await show_machines()


@cl.action_callback("claim_orchestrator")
async def claim_orchestrator(_: cl.Action) -> None:
    node = coordination.claim_orchestrator()
    await cl.Message(content=f"`{node.machine_id}` claimed orchestrator status.").send()
    await show_machines()


@cl.action_callback("release_orchestrator")
async def release_orchestrator(_: cl.Action) -> None:
    coordination.release_orchestrator()
    elected = coordination.get_or_elect_orchestrator()
    await cl.Message(content=f"Released orchestrator claim. Current orchestrator: `{elected.machine_id}`.").send()
    await show_machines()


@cl.action_callback("show_tasks")
async def show_tasks_action(_: cl.Action) -> None:
    await show_tasks()


@cl.action_callback("show_backends")
async def show_backends_action(_: cl.Action) -> None:
    await show_backends()


def machine_actions() -> list[cl.Action]:
    return [
        cl.Action(
            name="refresh_machines",
            label="Refresh Machines",
            tooltip="Refresh machine heartbeat and status.",
            icon="refresh-cw",
            payload={},
        ),
        cl.Action(
            name="claim_orchestrator",
            label="Claim Orchestrator",
            tooltip="Make this machine the orchestrator.",
            icon="crown",
            payload={},
        ),
        cl.Action(
            name="release_orchestrator",
            label="Release Orchestrator",
            tooltip="Release this machine's orchestrator claim.",
            icon="unlink",
            payload={},
        ),
        cl.Action(
            name="show_tasks",
            label="Recent Tasks",
            tooltip="Show delegated tasks recorded by the orchestrator.",
            icon="list-checks",
            payload={},
        ),
        cl.Action(
            name="show_backends",
            label="Backends",
            tooltip="Show local agent backend availability.",
            icon="cpu",
            payload={},
        ),
    ]


def machine_cards(machines, orchestrator_id: str) -> str:
    online_count = sum(1 for machine in machines if _machine_status(machine) == "online")
    stale_count = len(machines) - online_count
    cards = "\n".join(_format_machine_card(machine, orchestrator_id) for machine in machines)
    return (
        "## Machine Status\n\n"
        f"> **Orchestrator** `{orchestrator_id}`  \n"
        f"> **Online** `{online_count}`  \n"
        f"> **Stale** `{stale_count}`\n\n"
        f"{cards}"
    )


def _format_machine_card(machine, orchestrator_id: str) -> str:
    age = datetime.now(UTC) - machine.last_seen
    status = _machine_status(machine)
    lead = "orchestrator" if machine.machine_id == orchestrator_id else machine.role
    capabilities = " ".join(f"`{capability}`" for capability in machine.capabilities)
    backends = " ".join(f"`{backend}`" for backend in machine.agent_backends)
    return (
        f"> ### {machine.machine_id}\n"
        f"> `{status}` `{lead}` `seen {int(age.total_seconds())}s ago`  \n"
        f"> Host: `{machine.hostname}`  \n"
        f"> Backends: {backends}  \n"
        f"> Capabilities: {capabilities}\n"
    )


def _machine_status(machine) -> str:
    age = datetime.now(UTC) - machine.last_seen
    return "online" if age.total_seconds() <= settings.orchestrator_ttl_seconds else "stale"


def command_help() -> str:
    return (
        "## Commands\n\n"
        "- `/spaces`\n"
        "- `/use <name>`\n"
        "- `/create-space <name> <path>`\n"
        "- `/worktree <name> <repo-path> <branch>`\n"
        "- `/clone <name> <git-url> [branch]`\n"
        "- `/workspace-modes`\n"
        "- `/machines`\n"
        "- `/claim-orchestrator`\n"
        "- `/release-orchestrator`\n"
        "- `/tasks`\n"
        "- `/backends`"
    )


def backend_status_cards() -> str:
    items = backend_availability(agent_backends)
    lines = []
    for item in items:
        state = "available" if item.available else "configured"
        command = f" `{item.command}`" if item.command else ""
        lines.append(f"> ### {item.name}\n> `{state}`{command}\n")
    return "## Agent Backends\n\n" + "\n".join(lines)
