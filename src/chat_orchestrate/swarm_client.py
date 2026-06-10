from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from collections.abc import AsyncIterator

import httpx

from .backends import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    GEMINI_CLI_BACKEND,
    SIMULATED_BACKEND,
    command_for_backend,
    detect_agent_backends,
    extract_response_text,
    task_command_args,
)
from .config import Settings
from .models import AgentSpec, ProgressUpdate, ProjectSpace
from .scaffold import scaffold_project


class SwarmClient:
    async def run_agent(self, agent: AgentSpec, project: ProjectSpace, goal: str, context: str) -> str:
        raise NotImplementedError

    async def run_agent_events(
        self,
        agent: AgentSpec,
        project: ProjectSpace,
        goal: str,
        context: str,
    ) -> AsyncIterator[ProgressUpdate | str]:
        yield await self.run_agent(agent, project, goal, context)


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

    async def run_agent_events(
        self,
        agent: AgentSpec,
        project: ProjectSpace,
        goal: str,
        context: str,
    ) -> AsyncIterator[ProgressUpdate | str]:
        yield ProgressUpdate(
            message=f"{agent.name} preview is reading `{project.name}` and the current handoff context.",
            phase="agent-output",
            agent=agent.name,
            role=agent.role,
        )
        await asyncio.sleep(0)
        yield ProgressUpdate(
            message=f"{agent.name} preview found no blocking local tool errors.",
            phase="agent-output",
            agent=agent.name,
            role=agent.role,
        )
        written = scaffold_project(project, goal, agent.role)
        if written:
            relative = [path.relative_to(project.path).as_posix() for path in written]
            yield ProgressUpdate(
                message="Preview fallback wrote workspace code: " + ", ".join(f"`{item}`" for item in relative[:6]),
                phase="agent-output",
                agent=agent.name,
                role=agent.role,
                preferred_backend=SIMULATED_BACKEND,
            )
        yield await self.run_agent(agent, project, goal, context)

    async def run_agent(self, agent: AgentSpec, project: ProjectSpace, goal: str, context: str) -> str:
        context_note = "with prior context" if context else "as the first pass"
        return (
            f"{agent.name} ({agent.role}) reviewed `{project.name}` {context_note}.\n\n"
            f"- Goal focus: {goal}\n"
            f"- Workspace: {project.path}\n"
            f"- Code fallback: generated or refreshed workspace artifacts where the task asked for an app, UI, or API.\n"
            f"- Recommended next move: run the preview command and reconnect a real local CLI for deeper edits."
        )


class LocalAgentCliClient(SwarmClient):
    """Routes chat turns through locally installed agent CLIs when available."""

    def __init__(
        self,
        settings: Settings,
        preferred_backend: str = "auto",
        command_overrides: dict[str, str] | None = None,
        openai_api_key: str = "",
        api_keys: dict[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.preferred_backend = "auto" if preferred_backend in {"Select", ""} else preferred_backend
        self.command_overrides = command_overrides or settings.command_overrides
        self.api_keys = {
            backend: str(value or "").strip()
            for backend, value in (api_keys or {}).items()
            if str(value or "").strip()
        }
        self.openai_api_key = (
            openai_api_key.strip()
            or self.api_keys.get(CODEX_BACKEND, "")
            or settings.openai_api_key.strip()
        )
        self.backends = [
            backend
            for backend in detect_agent_backends(settings.configured_backends, self.command_overrides)
            if backend != SIMULATED_BACKEND
        ]
        self.preview = LocalPreviewSwarmClient()

    async def run_agent(self, agent: AgentSpec, project: ProjectSpace, goal: str, context: str) -> str:
        final = ""
        async for event in self.run_agent_events(agent, project, goal, context):
            if isinstance(event, str):
                final = event
        return final

    async def run_agent_events(
        self,
        agent: AgentSpec,
        project: ProjectSpace,
        goal: str,
        context: str,
    ) -> AsyncIterator[ProgressUpdate | str]:
        prompt = self._agent_prompt(agent, project, goal, context)
        for backend in self._ordered_backends():
            async for event in self._run_backend_events(backend, prompt, project, agent):
                if isinstance(event, ProgressUpdate):
                    yield event
                    continue
                if event:
                    yield f"`{backend}` local response\n\n{event}"
                    return
            if backend == CODEX_BACKEND:
                yield ProgressUpdate(
                    message="Codex CLI did not return output; checking the configured API fallback.",
                    phase="agent-output",
                    agent=agent.name,
                    role=agent.role,
                    preferred_backend=backend,
                )
                api_output = await self._run_codex_api(prompt)
                if api_output:
                    yield f"`{backend}` API response\n\n{api_output}"
                    return
            if self.preferred_backend == backend and backend != SIMULATED_BACKEND:
                yield ProgressUpdate(
                    message=(
                        f"{backend} is selected but not callable here. Tried `{self._configured_command_label(backend)}`; "
                        "continuing with the local preview fallback so the workspace still gets visible code artifacts."
                    ),
                    phase="agent-warning",
                    agent=agent.name,
                    role=agent.role,
                    preferred_backend=backend,
                )
                async for event in self.preview.run_agent_events(agent, project, goal, context):
                    yield event
                return
        async for event in self.preview.run_agent_events(agent, project, goal, context):
            yield event

    def _agent_prompt(self, agent: AgentSpec, project: ProjectSpace, goal: str, context: str) -> str:
        return (
            f"You are {agent.name}, acting as {agent.role}.\n"
            f"{agent.instructions}\n\n"
            "Be concrete, linguistic, and useful. If the user asks for implementation, propose or perform "
            "specific work inside the project space. If distributed coordination context assigns work to "
            "another machine, respect that assignment and focus on your own role.\n\n"
            f"Project space: {project.name}\n"
            f"Path: {project.path}\n"
            f"Branch: {project.branch or 'unknown'}\n\n"
            f"User message:\n{goal}\n\n"
            f"Context from prior agents:\n{context or 'No prior context.'}"
        )

    def _ordered_backends(self) -> list[str]:
        if self.preferred_backend != "auto":
            return [self.preferred_backend, *[backend for backend in self.backends if backend != self.preferred_backend]]
        return self.backends

    async def _run_backend(self, backend: str, prompt: str, project: ProjectSpace) -> str:
        final = ""
        dummy_agent = AgentSpec(name=backend, role=backend, instructions="")
        async for event in self._run_backend_events(backend, prompt, project, dummy_agent):
            if isinstance(event, str):
                final = event
        return final

    async def _run_backend_events(
        self,
        backend: str,
        prompt: str,
        project: ProjectSpace,
        agent: AgentSpec,
    ) -> AsyncIterator[ProgressUpdate | str]:
        command = self._command_for_backend(backend)
        if command is None:
            yield ProgressUpdate(
                message=f"{backend} command is not reachable from this app process.",
                phase="agent-warning",
                agent=agent.name,
                role=agent.role,
                preferred_backend=backend,
            )
            yield ""
            return
        workspace_path = project.path if project.path.exists() and project.path.is_dir() else None
        args = task_command_args(backend, command, prompt, workspace_path)
        if args is None:
            yield ProgressUpdate(
                message=f"{backend} has no runnable command template yet.",
                phase="agent-warning",
                agent=agent.name,
                role=agent.role,
                preferred_backend=backend,
            )
            yield ""
            return

        try:
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=workspace_path,
                env=self._backend_env(backend),
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            yield ProgressUpdate(
                message=f"{backend} could not start: {exc}",
                phase="agent-error",
                agent=agent.name,
                role=agent.role,
                preferred_backend=backend,
            )
            yield ""
            return

        output_parts: list[str] = []
        error_parts: list[str] = []
        queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def put_thread_event(label: str, line: str = "") -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, (label, line))
            except RuntimeError:
                pass

        def pump_stream(stream, label: str) -> None:
            if stream is None:
                put_thread_event(f"{label}-done")
                return
            buffer: list[str] = []

            def flush_buffer() -> None:
                if not buffer:
                    return
                put_thread_event(label, "".join(buffer).strip())
                buffer.clear()

            try:
                while True:
                    char = stream.read(1)
                    if char == "":
                        break
                    if char in {"\n", "\r"}:
                        flush_buffer()
                        continue
                    buffer.append(char)
                    if len(buffer) >= 160:
                        flush_buffer()
                flush_buffer()
            finally:
                put_thread_event(f"{label}-done")

        def wait_for_process() -> None:
            put_thread_event("process-done", str(process.wait()))

        threading.Thread(target=pump_stream, args=(process.stdout, "stdout"), daemon=True).start()
        threading.Thread(target=pump_stream, args=(process.stderr, "stderr"), daemon=True).start()
        threading.Thread(target=wait_for_process, daemon=True).start()

        started = loop.time()
        deadline = started + self.settings.local_agent_timeout_seconds
        streams_remaining = 2
        process_done = False
        return_code = 0

        while True:
            if process_done and streams_remaining <= 0 and queue.empty():
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                process.kill()
                yield ProgressUpdate(
                    message=f"{backend} timed out after {int(self.settings.local_agent_timeout_seconds)}s.",
                    phase="agent-error",
                    agent=agent.name,
                    role=agent.role,
                    preferred_backend=backend,
                    elapsed_seconds=int(loop.time() - started),
                )
                yield "\n".join(output_parts or error_parts).strip()
                return
            try:
                label, line = await asyncio.wait_for(queue.get(), timeout=min(0.5, remaining))
            except asyncio.TimeoutError:
                continue
            if label == "process-done":
                process_done = True
                try:
                    return_code = int(line)
                except ValueError:
                    return_code = 1
                continue
            if label.endswith("-done"):
                streams_remaining -= 1
                continue
            if label == "stderr":
                error_parts.append(line)
                phase = "agent-warning"
                stream_label = "stderr"
            else:
                output_parts.append(line)
                phase = "agent-output"
                stream_label = "output"
            yield ProgressUpdate(
                message=f"{backend} {stream_label}: {self._trim_stream_line(line)}",
                phase=phase,
                agent=agent.name,
                role=agent.role,
                preferred_backend=backend,
                elapsed_seconds=int(loop.time() - started),
            )

        if return_code != 0:
            yield ProgressUpdate(
                message=f"{backend} exited with code {return_code}.",
                phase="agent-error",
                agent=agent.name,
                role=agent.role,
                preferred_backend=backend,
                elapsed_seconds=int(loop.time() - started),
            )
        yield "\n".join(output_parts or error_parts).strip()

    async def _run_codex_api(self, prompt: str) -> str:
        if not self.openai_api_key:
            return ""
        try:
            async with httpx.AsyncClient(timeout=self.settings.local_agent_timeout_seconds) as client:
                response = await client.post(
                    "https://api.openai.com/v1/responses",
                    headers={"Authorization": f"Bearer {self.openai_api_key}"},
                    json={
                        "model": self.settings.codex_api_model,
                        "input": prompt,
                    },
                )
            if response.status_code >= 400:
                return f"OpenAI API request failed: HTTP {response.status_code} {response.text}"
            return extract_response_text(response.json())
        except httpx.HTTPError as exc:
            return f"OpenAI API request failed: {exc}"

    def _command_for_backend(self, backend: str) -> str | None:
        return command_for_backend(backend, self.command_overrides)

    def _configured_command_label(self, backend: str) -> str:
        configured = str(self.command_overrides.get(backend, "") or "").strip()
        if configured.lower() in {"none", "null", "undefined"}:
            configured = ""
        if configured:
            return configured
        return command_for_backend(backend) or (
            "codex"
            if backend == CODEX_BACKEND
            else "claude"
            if backend == CLAUDE_CODE_BACKEND
            else "gemini"
            if backend == GEMINI_CLI_BACKEND
            else backend
        )

    def _trim_stream_line(self, line: str, limit: int = 260) -> str:
        clean = " ".join(line.split())
        if len(clean) <= limit:
            return clean
        return f"{clean[: limit - 1]}..."

    def _backend_env(self, backend: str) -> dict[str, str]:
        env = os.environ.copy()
        if self.openai_api_key:
            env["OPENAI_API_KEY"] = self.openai_api_key
        claude_key = self.api_keys.get(CLAUDE_CODE_BACKEND, "")
        if claude_key:
            env["ANTHROPIC_API_KEY"] = claude_key
            env["CLAUDE_API_KEY"] = claude_key
        gemini_key = self.api_keys.get(GEMINI_CLI_BACKEND, "")
        if gemini_key:
            env["GEMINI_API_KEY"] = gemini_key
            env["GOOGLE_API_KEY"] = gemini_key
        return env


def build_swarm_client(
    settings: Settings,
    preferred_backend: str = "auto",
    command_overrides: dict[str, str] | None = None,
    openai_api_key: str = "",
    api_keys: dict[str, str] | None = None,
) -> SwarmClient:
    if settings.use_open_swarm:
        return OpenSwarmClient(settings)
    if settings.use_local_agent_chat:
        return LocalAgentCliClient(settings, preferred_backend, command_overrides, openai_api_key, api_keys)
    return LocalPreviewSwarmClient()
