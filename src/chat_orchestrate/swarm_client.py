from __future__ import annotations

import httpx

from .config import Settings
from .models import AgentSpec, ProjectSpace


class SwarmClient:
    async def run_agent(self, agent: AgentSpec, project: ProjectSpace, goal: str, context: str) -> str:
        raise NotImplementedError


class OpenSwarmClient(SwarmClient):
    """Calls an OpenSwarm swarm-api endpoint through its OpenAI-compatible chat API."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run_agent(self, agent: AgentSpec, project: ProjectSpace, goal: str, context: str) -> str:
        headers = {}
        if self.settings.open_swarm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.open_swarm_api_key}"

        prompt = (
            f"You are {agent.name}, acting as {agent.role}.\n"
            f"{agent.instructions}\n\n"
            f"Project space: {project.name}\n"
            f"Path: {project.path}\n"
            f"Branch: {project.branch or 'unknown'}\n\n"
            f"Goal:\n{goal}\n\n"
            f"Context from prior agents:\n{context or 'No prior context.'}"
        )

        async with httpx.AsyncClient(timeout=self.settings.open_swarm_timeout_seconds) as client:
            response = await client.post(
                f"{self.settings.open_swarm_base_url.rstrip('/')}/v1/chat/completions",
                headers=headers,
                json={
                    "model": self.settings.open_swarm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                },
            )
            response.raise_for_status()
            payload = response.json()

        return payload["choices"][0]["message"]["content"].strip()


class LocalPreviewSwarmClient(SwarmClient):
    """Deterministic fallback for UI and workflow testing without external services."""

    async def run_agent(self, agent: AgentSpec, project: ProjectSpace, goal: str, context: str) -> str:
        context_note = "with prior context" if context else "as the first pass"
        return (
            f"{agent.name} ({agent.role}) reviewed `{project.name}` {context_note}.\n\n"
            f"- Goal focus: {goal}\n"
            f"- Workspace: {project.path}\n"
            f"- Recommended next move: keep the output scoped to this project space and hand "
            f"off concrete findings to the next agent."
        )


def build_swarm_client(settings: Settings) -> SwarmClient:
    if settings.use_open_swarm:
        return OpenSwarmClient(settings)
    return LocalPreviewSwarmClient()
