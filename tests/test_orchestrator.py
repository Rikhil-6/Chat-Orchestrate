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
