from pathlib import Path

import pytest

from chat_orchestrate.models import AgentSpec, AgentTurn, OrchestrationRun, ProgressUpdate, ProjectSpace
from chat_orchestrate.orchestrator import Orchestrator
from chat_orchestrate.swarm_client import SwarmClient


class FakeClient(SwarmClient):
    async def run_agent(self, agent: AgentSpec, project: ProjectSpace, goal: str, context: str) -> str:
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
