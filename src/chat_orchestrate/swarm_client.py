from __future__ import annotations

import asyncio

import httpx

from .backends import CLAUDE_CODE_BACKEND, CODEX_BACKEND, SIMULATED_BACKEND, detect_agent_backends
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


class LocalAgentCliClient(SwarmClient):
    """Routes chat turns through locally installed agent CLIs when available."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.backends = [
            backend
            for backend in detect_agent_backends(settings.configured_backends)
            if backend != SIMULATED_BACKEND
        ]
        self.preview = LocalPreviewSwarmClient()

    async def run_agent(self, agent: AgentSpec, project: ProjectSpace, goal: str, context: str) -> str:
        prompt = (
            f"You are {agent.name}, acting as {agent.role}.\n"
            f"{agent.instructions}\n\n"
            f"Project space: {project.name}\n"
            f"Path: {project.path}\n"
            f"Branch: {project.branch or 'unknown'}\n\n"
            f"User message:\n{goal}\n\n"
            f"Context from prior agents:\n{context or 'No prior context.'}"
        )
        for backend in self.backends:
            output = await self._run_backend(backend, prompt)
            if output:
                return f"`{backend}` local response\n\n{output}"
        return await self.preview.run_agent(agent, project, goal, context)

    async def _run_backend(self, backend: str, prompt: str) -> str:
        if backend == CODEX_BACKEND:
            args = ["codex", "exec", "--skip-git-repo-check", prompt]
        elif backend == CLAUDE_CODE_BACKEND:
            args = ["claude", "-p", prompt]
        else:
            return ""

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.settings.local_agent_timeout_seconds,
            )
        except (OSError, asyncio.TimeoutError):
            return ""

        output = stdout.decode(errors="replace").strip() or stderr.decode(errors="replace").strip()
        return output


def build_swarm_client(settings: Settings) -> SwarmClient:
    if settings.use_open_swarm:
        return OpenSwarmClient(settings)
    if settings.use_local_agent_chat:
        return LocalAgentCliClient(settings)
    return LocalPreviewSwarmClient()
