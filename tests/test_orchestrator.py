from pathlib import Path

import pytest

from chat_orchestrate.models import AgentSpec, AgentTurn, OrchestrationRun, ProgressUpdate, ProjectSpace
from chat_orchestrate.orchestrator import Orchestrator
from chat_orchestrate.swarm_client import SwarmClient


class FakeClient(SwarmClient):
    async def run_agent(self, agent: AgentSpec, project: ProjectSpace, goal: str, context: str) -> str:
        return f"{agent.name} handled {goal} in {project.name}"


class FakeRoutingClient(SwarmClient):
    async def run_agent(self, agent: AgentSpec, project: ProjectSpace, goal: str, context: str) -> str:
        if agent.name == "Routing Planner":
            return (
                '{"assignments":['
                '{"role":"backend","machine_id":"desktop-p4k08ab","reason":"user assigned backend to that machine"},'
                '{"role":"frontend","machine_id":"sg-akc-dt330","reason":"user assigned frontend to this machine"}'
                ']}'
            )
        return f"{agent.name} handled {goal} in {project.name}"


class FakeVisualRoutingClient(SwarmClient):
    async def run_agent(self, agent: AgentSpec, project: ProjectSpace, goal: str, context: str) -> str:
        if agent.name == "Routing Planner":
            return (
                '{"roles":["coordinator","frontend","reviewer"],"assignments":['
                '{"role":"frontend","machine_id":"sg-akc-dt330","reason":"model saw visual feedback"},'
                '{"role":"reviewer","machine_id":"sg-akc-dt330","reason":"model wants verification"}'
                ']}'
            )
        return f"{agent.name} handled {goal} in {project.name}"


class FakeStreamingClient(SwarmClient):
    async def run_agent(self, agent: AgentSpec, project: ProjectSpace, goal: str, context: str) -> str:
        return f"{agent.name} handled {goal} in {project.name}"

    async def run_agent_events(self, agent: AgentSpec, project: ProjectSpace, goal: str, context: str):
        yield ProgressUpdate(
            message=f"{agent.name} saw the workspace",
            phase="agent-output",
            agent=agent.name,
            role=agent.role,
        )
        yield ProgressUpdate(
            message=f"{agent.name} noticed a recoverable warning",
            phase="agent-warning",
            agent=agent.name,
            role=agent.role,
        )
        yield await self.run_agent(agent, project, goal, context)


@pytest.mark.asyncio
async def test_orchestrator_emits_turns_and_final() -> None:
    project = ProjectSpace(name="demo", path=Path("/tmp/demo"))
    orchestrator = Orchestrator(FakeClient(), ["coordinator", "reviewer"])

    events = [event async for event in orchestrator.run("ship it", project)]
    turns = [event for event in events if isinstance(event, AgentTurn)]
    progress = [event for event in events if isinstance(event, ProgressUpdate)]
    final = next(event for event in events if isinstance(event, OrchestrationRun))

    assert progress
    assert turns[0].agent == "Coordinator"
    assert turns[1].agent == "Reviewer"
    assert final.final.startswith("## Run")


@pytest.mark.asyncio
async def test_orchestrator_streams_agent_updates_before_turn() -> None:
    project = ProjectSpace(name="demo", path=Path("/tmp/demo"))
    orchestrator = Orchestrator(FakeStreamingClient(), ["engineer"], progress_interval_seconds=0.01)

    events = [event async for event in orchestrator.run("ship it", project)]
    progress = [event for event in events if isinstance(event, ProgressUpdate)]
    turn_index = next(index for index, event in enumerate(events) if isinstance(event, AgentTurn))
    output_index = next(index for index, event in enumerate(events) if isinstance(event, ProgressUpdate) and event.phase == "agent-output")
    warning_index = next(index for index, event in enumerate(events) if isinstance(event, ProgressUpdate) and event.phase == "agent-warning")

    assert output_index < turn_index
    assert warning_index < turn_index
    assert any(update.message.endswith("recoverable warning") for update in progress)


@pytest.mark.asyncio
async def test_orchestrator_uses_reasoned_routing_assignments(tmp_path: Path) -> None:
    from chat_orchestrate.coordination import CoordinationManager

    state = tmp_path / "coordination.json"
    host = CoordinationManager(
        state,
        "sg-akc-dt330",
        ["coordinator", "frontend"],
        ["codex"],
        cluster_id="test",
        coordination_token="secret",
    )
    remote = CoordinationManager(
        state,
        "desktop-p4k08ab",
        ["backend"],
        ["claude-code"],
        cluster_id="test",
        coordination_token="secret",
    )
    host.claim_orchestrator()
    remote.heartbeat()

    project = ProjectSpace(name="demo", path=tmp_path / "demo")
    orchestrator = Orchestrator(
        FakeRoutingClient(),
        ["coordinator"],
        host,
        delegated_task_wait_seconds=0,
        progress_interval_seconds=0.01,
    )
    events = [
        event
        async for event in orchestrator.run(
            "desktop-p4k08ab should handle data plumbing; this machine should build the browser surface",
            project,
        )
    ]
    final = next(event for event in events if isinstance(event, OrchestrationRun))
    by_role = {task.role: task for task in final.delegated_tasks}

    assert by_role["backend"].assigned_machine == "desktop-p4k08ab"
    assert by_role["frontend"].assigned_machine == "sg-akc-dt330"


@pytest.mark.asyncio
async def test_orchestrator_accepts_model_inferred_roles_not_keyword_hints(tmp_path: Path) -> None:
    from chat_orchestrate.coordination import CoordinationManager

    state = tmp_path / "coordination.json"
    host = CoordinationManager(
        state,
        "sg-akc-dt330",
        ["coordinator", "frontend", "reviewer"],
        ["codex"],
        cluster_id="test",
        coordination_token="secret",
    )
    host.claim_orchestrator()

    project = ProjectSpace(name="demo", path=tmp_path / "demo")
    orchestrator = Orchestrator(
        FakeVisualRoutingClient(),
        ["coordinator"],
        host,
        delegated_task_wait_seconds=0,
        progress_interval_seconds=0.01,
    )
    events = [
        event
        async for event in orchestrator.run(
            "the colours feel off for the video site",
            project,
        )
    ]
    final = next(event for event in events if isinstance(event, OrchestrationRun))
    roles = {task.role for task in final.delegated_tasks}

    assert {"coordinator", "frontend", "reviewer"}.issubset(roles)


@pytest.mark.asyncio
async def test_coordinator_only_plan_runs_the_actual_coordinator_agent(tmp_path: Path) -> None:
    from chat_orchestrate.coordination import CoordinationManager

    state = tmp_path / "coordination.json"
    host = CoordinationManager(
        state,
        "sg-akc-dt330",
        ["coordinator"],
        ["codex"],
        cluster_id="test",
        coordination_token="secret",
    )
    host.claim_orchestrator()

    project = ProjectSpace(name="demo", path=tmp_path / "demo")
    orchestrator = Orchestrator(
        FakeClient(),
        ["coordinator"],
        host,
        delegated_task_wait_seconds=0,
        progress_interval_seconds=0.01,
        conversation_context="User: make the page more YouTube-like\nAssistant: I updated the workspace.",
    )
    events = [event async for event in orchestrator.run("try again?", project)]
    turns = [event for event in events if isinstance(event, AgentTurn)]

    assert turns
    assert turns[-1].content == "Coordinator handled try again? in demo"
    assert "Coordinator routing is ready" not in turns[-1].content
