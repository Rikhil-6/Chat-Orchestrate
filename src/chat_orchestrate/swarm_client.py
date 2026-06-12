from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from .api_harness import apply_api_harness_response, build_api_harness_prompt
from .artifacts import scan_project_artifacts
from .backends import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    GEMINI_CLI_BACKEND,
    SIMULATED_BACKEND,
    api_httpx_trust_env,
    backend_api_name,
    backend_supports_api_fallback,
    command_for_backend,
    codex_final_message_path,
    detect_agent_backends,
    extract_claude_response_text,
    extract_gemini_response_text,
    extract_response_text,
    is_backend_runtime_failure,
    read_text_if_present,
    sanitized_agent_environment,
    task_command_args,
)
from .config import Settings
from .models import AgentSpec, AgentTurn, DelegatedTask, ProgressUpdate, ProjectSpace
from .summaries import summarize_goal as fallback_summarize_goal
from .scaffold import scaffold_project, should_scaffold


ARTIFACT_FINISH_MIN_SECONDS = 20.0
ARTIFACT_FINISH_QUIET_SECONDS = 10.0

BENIGN_STDERR_MARKERS = (
    "codex_core_skills::loader: ignoring interface.icon_small",
    "codex_core_skills::loader: ignoring interface.icon_large",
    "icon path with '..' must resolve under plugin assets/",
    "codex_core::shell_snapshot: Failed to create shell snapshot",
    "Shell snapshot not supported yet for PowerShell",
    "warning: could not open directory '.pytest-tmp",
    "warning: could not open directory \".pytest-tmp",
    "failed to clean up stale arg0 temp dirs",
    "proceeding, even though we could not update path",
    "\\.codex\\tmp\\arg0\\",
)

SURFACED_STDERR_MARKERS = (
    "access is denied",
    "denied",
    "exception",
    "failed",
    "forbidden",
    "in-process app-server client",
    "not found",
    "panic",
    "problem",
    "readonly database",
    "state db",
    "state_",
    "traceback",
    "unauthorized",
)

HIDE_STDERR_MARKERS = (
    "title: ",
    "export function notfound",
    "export function unauthorized",
    "missing or invalid admin api key",
    "invalid admin api key",
    "objectnotfound: (rg",
    "commandnotfoundexception",
    "fullyqualifiederrorid : commandnotfoundexception",
)


def is_benign_agent_stderr(line: str) -> bool:
    clean = str(line or "")
    if not clean.strip():
        return True
    lowered = clean.lower()
    if "time_wait" in lowered and "tcp " in lowered:
        return True
    return any(marker.lower() in lowered for marker in BENIGN_STDERR_MARKERS)


def workspace_write_contract(project: ProjectSpace) -> str:
    return (
        "Workspace write contract:\n"
        f"- Treat `{project.path}` as the read-write project workspace for `{project.name}`.\n"
        "- If implementation is requested, create or update files in that workspace instead of only planning.\n"
        "- If the workspace is empty, scaffold a greenfield app or monorepo structure there.\n"
        "- Do not claim the session is read-only unless an actual write attempt fails; if it fails, report the "
        "exact path, command, and error.\n"
        "- Return the files changed plus the commands needed to preview or verify the work."
    )


def trim_summary_line(value: str, limit: int = 180) -> str:
    clean = " ".join(str(value or "").split())
    if len(clean) <= limit:
        return clean
    return f"{clean[: limit - 1]}..."


class SwarmClient:
    async def run_agent(self, agent: AgentSpec, project: ProjectSpace, goal: str, context: str) -> str:
        raise NotImplementedError

    async def summarize_goal(
        self,
        goal: str,
        project: ProjectSpace,
        tasks: list[DelegatedTask] | None = None,
        turns: list[AgentTurn] | None = None,
    ) -> str:
        return fallback_summarize_goal(goal, tasks or [])

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
            "Response contract:\n"
            "- Answer the user's latest message directly first, in normal assistant prose.\n"
            "- Then include concrete implementation, diagnostics, changed files, commands, or blockers as needed.\n"
            "- Do not lead with generic coordination status, proof sections, or internal routing unless that is the answer.\n\n"
            f"Project space: {project.name}\n"
            f"Path: {project.path}\n"
            f"Branch: {project.branch or 'unknown'}\n\n"
            f"{workspace_write_contract(project)}\n\n"
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

    async def summarize_goal(
        self,
        goal: str,
        project: ProjectSpace,
        tasks: list[DelegatedTask] | None = None,
        turns: list[AgentTurn] | None = None,
    ) -> str:
        task_lines = "\n".join(
            f"- {task.role}: {task.assigned_machine} via {task.preferred_backend} ({task.status})"
            for task in (tasks or [])
            if task.role and task.assigned_machine
        )
        turn_lines = "\n".join(
            f"- {turn.agent} / {turn.role}: {trim_summary_line(turn.content)}"
            for turn in (turns or [])[-4:]
            if turn.content.strip()
        )
        prompt = (
            "Write one short dashboard summary for a distributed coding run.\n"
            "Keep it plain language, concrete, and under 120 characters if possible.\n"
            "Do not use bullets, headings, JSON, or template wording.\n\n"
            f"Project: {project.name}\n"
            f"Goal: {goal}\n"
            f"Assignments:\n{task_lines or '- none'}\n"
            f"Recent turns:\n{turn_lines or '- none'}\n"
        )

        headers = {}
        if self.settings.open_swarm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.open_swarm_api_key}"

        try:
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
        except httpx.HTTPError:
            return fallback_summarize_goal(goal, tasks or [])

        summary = payload["choices"][0]["message"]["content"].strip()
        return summary or fallback_summarize_goal(goal, tasks or [])


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

    async def summarize_goal(
        self,
        goal: str,
        project: ProjectSpace,
        tasks: list[DelegatedTask] | None = None,
        turns: list[AgentTurn] | None = None,
    ) -> str:
        return fallback_summarize_goal(goal, tasks or [])


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
        self.api_keys.setdefault(CODEX_BACKEND, self.openai_api_key)
        if settings.claude_api_key.strip():
            self.api_keys.setdefault(CLAUDE_CODE_BACKEND, settings.claude_api_key.strip())
        if settings.gemini_api_key.strip():
            self.api_keys.setdefault(GEMINI_CLI_BACKEND, settings.gemini_api_key.strip())
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

    async def summarize_goal(
        self,
        goal: str,
        project: ProjectSpace,
        tasks: list[DelegatedTask] | None = None,
        turns: list[AgentTurn] | None = None,
    ) -> str:
        prompt = self._summary_prompt(goal, project, tasks or [], turns or [])
        for backend in self._ordered_backends():
            summary = await self._run_summary_backend(backend, prompt, project)
            if summary:
                return self._normalize_summary(summary, goal, tasks or [])
        return fallback_summarize_goal(goal, tasks or [])

    async def run_agent_events(
        self,
        agent: AgentSpec,
        project: ProjectSpace,
        goal: str,
        context: str,
    ) -> AsyncIterator[ProgressUpdate | str]:
        if self.preferred_backend == SIMULATED_BACKEND:
            async for event in self.preview.run_agent_events(agent, project, goal, context):
                yield event
            return

        prompt = self._agent_prompt(agent, project, goal, context)
        for backend in self._ordered_backends():
            async for event in self._run_backend_events(backend, prompt, project, agent):
                if isinstance(event, ProgressUpdate):
                    yield event
                    continue
                if event:
                    if self._is_recoverable_backend_runtime_failure(backend, event):
                        yield ProgressUpdate(
                            message=(
                                f"{backend} CLI hit a local runtime/session issue before the model could answer; "
                                f"checking the configured {backend_api_name(backend)} API fallback automatically."
                            ),
                            phase="agent-recovery",
                            agent=agent.name,
                            role=agent.role,
                            preferred_backend=backend,
                        )
                        if not self._backend_api_key(backend):
                            yield ProgressUpdate(
                                message=(
                                    f"No {backend_api_name(backend)} API key is saved for `{backend}` fallback, so "
                                    "I am surfacing the local runtime failure instead of inventing an answer."
                                ),
                                phase="agent-recovery",
                                agent=agent.name,
                                role=agent.role,
                                preferred_backend=backend,
                            )
                            api_output = ""
                        else:
                            yield ProgressUpdate(
                                message=(
                                    f"{backend} API fallback is running as a workspace write harness for "
                                    f"`{project.name}`."
                                ),
                                phase="agent-recovery",
                                agent=agent.name,
                                role=agent.role,
                                preferred_backend=backend,
                            )
                            api_output = await self._try_backend_api_recovery(backend, prompt, project)
                        if api_output:
                            yield f"`{backend}` API fallback response\n\n{api_output}"
                            return
                        if self.preferred_backend != backend:
                            continue
                    yield f"`{backend}` local response\n\n{event}"
                    return
            if backend_supports_api_fallback(backend):
                yield ProgressUpdate(
                    message=(
                        f"{backend} CLI did not return output; checking the configured "
                        f"{backend_api_name(backend)} API fallback."
                    ),
                    phase="agent-output",
                    agent=agent.name,
                    role=agent.role,
                    preferred_backend=backend,
                )
                api_output = await self._try_backend_api_recovery(backend, prompt, project)
                if api_output:
                    yield f"`{backend}` API response\n\n{api_output}"
                    return
            if self.preferred_backend == backend and backend != SIMULATED_BACKEND:
                yield ProgressUpdate(
                    message=(
                        f"{backend} is selected but not callable here. Tried `{self._configured_command_label(backend)}`; "
                        "no simulated fallback was used."
                    ),
                    phase="agent-warning",
                    agent=agent.name,
                    role=agent.role,
                    preferred_backend=backend,
                )
                yield (
                    f"`{backend}` was selected, but this app process did not receive a usable response from it.\n\n"
                    f"Tried command: `{self._configured_command_label(backend)}`\n\n"
                    "The harness did not generate replacement code. Pick `simulated` explicitly for preview scaffolding, "
                    "or fix the selected local agent command/session and retry."
                )
                return
        yield (
            "No configured local agent backend returned a response.\n\n"
            "The harness did not generate replacement code. Select a reachable `codex`, `claude-code`, "
            "`gemini-cli`, or explicitly select `simulated` for preview scaffolding."
        )

    async def _try_backend_api_recovery(self, backend: str, prompt: str, project: ProjectSpace | None = None) -> str:
        if not self._backend_api_key(backend):
            return ""
        return await self._run_backend_api_fallback(backend, prompt, project)

    def _is_recoverable_backend_runtime_failure(self, backend: str, content: str) -> bool:
        return is_backend_runtime_failure(backend, content)

    def _agent_prompt(self, agent: AgentSpec, project: ProjectSpace, goal: str, context: str) -> str:
        return (
            f"You are {agent.name}, acting as {agent.role}.\n"
            f"{agent.instructions}\n\n"
            "Be concrete, linguistic, and useful. If the user asks for implementation, propose or perform "
            "specific work inside the project space. If distributed coordination context assigns work to "
            "another machine, respect that assignment and focus on your own role.\n\n"
            "Response contract:\n"
            "- Answer the user's latest message directly first, in normal assistant prose.\n"
            "- Then include concrete implementation, diagnostics, changed files, commands, or blockers as needed.\n"
            "- Do not lead with generic coordination status, proof sections, or internal routing unless that is the answer.\n\n"
            f"{workspace_write_contract(project)}\n\n"
            f"Project space: {project.name}\n"
            f"Path: {project.path}\n"
            f"Branch: {project.branch or 'unknown'}\n\n"
            f"User message:\n{goal}\n\n"
            f"Context from prior agents:\n{context or 'No prior context.'}"
        )

    def _summary_prompt(
        self,
        goal: str,
        project: ProjectSpace,
        tasks: list[DelegatedTask],
        turns: list[AgentTurn],
    ) -> str:
        task_lines = "\n".join(
            f"- {task.role}: {task.assigned_machine} via {task.preferred_backend} ({task.status})"
            for task in tasks
            if task.role and task.assigned_machine
        )
        turn_lines = "\n".join(
            f"- {turn.agent} / {turn.role}: {self._trim_summary_line(turn.content)}"
            for turn in turns[-4:]
            if turn.content.strip()
        )
        return (
            "You are a concise run-summary writer for a distributed coding dashboard.\n"
            "Return exactly one short sentence or sentence fragment suitable for a sidebar card.\n"
            "Do not use bullets, headings, JSON, code fences, or generic coordination wording.\n"
            "Focus on the actual task, the project, and the most relevant assignments.\n\n"
            f"Project: {project.name}\n"
            f"Path: {project.path}\n"
            f"Goal: {goal}\n"
            f"Assignments:\n{task_lines or '- none'}\n"
            f"Recent turns:\n{turn_lines or '- none'}\n"
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

    async def _run_summary_backend(self, backend: str, prompt: str, project: ProjectSpace) -> str:
        command = self._command_for_backend(backend)
        if command is None:
            api_key = self._backend_api_key(backend)
            if api_key and backend_supports_api_fallback(backend):
                return await self._run_backend_api_fallback(backend, prompt, project)
            return ""
        workspace_path = project.path.resolve() if project.path.exists() and project.path.is_dir() else None
        final_output_path = codex_final_message_path(workspace_path) if backend == CODEX_BACKEND else None
        args = task_command_args(backend, command, prompt, workspace_path, final_output_path)
        if args is None:
            return ""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                args,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.settings.local_agent_timeout_seconds,
                cwd=workspace_path,
                stdin=subprocess.DEVNULL,
                env=self._backend_env(backend),
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        output = read_text_if_present(final_output_path) or result.stdout.strip() or result.stderr.strip()
        if not output and backend_supports_api_fallback(backend):
            output = await self._run_backend_api_fallback(backend, prompt, project)
        return output

    def _normalize_summary(self, summary: str, goal: str, tasks: list[DelegatedTask]) -> str:
        clean = " ".join(str(summary or "").split()).strip(" -")
        if not clean:
            return fallback_summarize_goal(goal, tasks)
        if len(clean) > 120:
            clean = clean[:119].rstrip(" ,;:") + "..."
        return clean

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
        try:
            project.path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            yield ProgressUpdate(
                message=f"Could not prepare project workspace `{project.path}`: {exc}",
                phase="agent-error",
                agent=agent.name,
                role=agent.role,
                preferred_backend=backend,
            )
            yield ""
            return
        workspace_path = project.path.resolve() if project.path.exists() and project.path.is_dir() else None
        final_output_path = codex_final_message_path(workspace_path) if backend == CODEX_BACKEND else None
        args = task_command_args(backend, command, prompt, workspace_path, final_output_path)
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
                stdin=subprocess.DEVNULL,
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
                    if len(buffer) >= 1000:
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
        started_wall = time.time()
        last_activity = started
        deadline = started + self.settings.local_agent_timeout_seconds
        streams_remaining = 2
        process_done = False
        finished_for_artifacts = False
        return_code = 0

        def artifact_finish_due(now: float) -> bool:
            return (
                not process_done
                and not finished_for_artifacts
                and now - started >= ARTIFACT_FINISH_MIN_SECONDS
                and now - last_activity >= ARTIFACT_FINISH_QUIET_SECONDS
                and self._workspace_has_recent_artifacts(project, started_wall)
            )

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
                now = loop.time()
                if artifact_finish_due(now):
                    finished_for_artifacts = True
                    self._terminate_process(process)
                    yield ProgressUpdate(
                        message=(
                            "Workspace files have been written and the CLI is quiet; "
                            "collecting the generated artifacts now."
                        ),
                        phase="agent-output",
                        agent=agent.name,
                        role=agent.role,
                        preferred_backend=backend,
                        elapsed_seconds=int(now - started),
                    )
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
                if self._is_benign_stderr(line):
                    now = loop.time()
                    if artifact_finish_due(now):
                        finished_for_artifacts = True
                        self._terminate_process(process)
                        yield ProgressUpdate(
                            message=(
                                "Workspace files have been written and the CLI is quiet; "
                                "collecting the generated artifacts now."
                            ),
                            phase="agent-output",
                            agent=agent.name,
                            role=agent.role,
                            preferred_backend=backend,
                            elapsed_seconds=int(now - started),
                        )
                    continue
                if not self._should_surface_stderr(line):
                    now = loop.time()
                    if artifact_finish_due(now):
                        finished_for_artifacts = True
                        self._terminate_process(process)
                        yield ProgressUpdate(
                            message=(
                                "Workspace files have been written and the CLI is quiet; "
                                "collecting the generated artifacts now."
                            ),
                            phase="agent-output",
                            agent=agent.name,
                            role=agent.role,
                            preferred_backend=backend,
                            elapsed_seconds=int(now - started),
                        )
                    continue
                last_activity = loop.time()
                error_parts.append(line)
                phase = "agent-warning"
                stream_label = "stderr"
            else:
                last_activity = loop.time()
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

        if return_code != 0 and not finished_for_artifacts:
            yield ProgressUpdate(
                message=f"{backend} exited with code {return_code}.",
                phase="agent-error",
                agent=agent.name,
                role=agent.role,
                preferred_backend=backend,
                elapsed_seconds=int(loop.time() - started),
            )
        final = read_text_if_present(final_output_path) or "\n".join(output_parts or error_parts).strip()
        if not final and self._workspace_has_recent_artifacts(project, started_wall):
            final = self._workspace_artifact_result(project)
        if not final:
            final = self._empty_backend_result(backend, command, return_code, final_output_path)
        yield final

    async def _run_codex_api(self, prompt: str) -> str:
        if not self.openai_api_key:
            return ""
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.local_agent_timeout_seconds,
                trust_env=api_httpx_trust_env(),
            ) as client:
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

    async def _run_backend_api_fallback(
        self,
        backend: str,
        prompt: str,
        project: ProjectSpace | None = None,
    ) -> str:
        harness_prompt = build_api_harness_prompt(prompt, project)
        if backend == CODEX_BACKEND:
            output = await self._run_codex_api(harness_prompt)
        elif backend == CLAUDE_CODE_BACKEND:
            output = await self._run_claude_api(harness_prompt)
        elif backend == GEMINI_CLI_BACKEND:
            output = await self._run_gemini_api(harness_prompt)
        else:
            return ""
        return apply_api_harness_response(project, output).content

    async def _run_claude_api(self, prompt: str) -> str:
        api_key = self._backend_api_key(CLAUDE_CODE_BACKEND)
        if not api_key:
            return ""
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.local_agent_timeout_seconds,
                trust_env=api_httpx_trust_env(),
            ) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": self.settings.claude_api_model,
                        "max_tokens": 4096,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
            if response.status_code >= 400:
                return f"Claude API request failed: HTTP {response.status_code} {response.text}"
            return extract_claude_response_text(response.json())
        except httpx.HTTPError as exc:
            return f"Claude API request failed: {exc}"

    async def _run_gemini_api(self, prompt: str) -> str:
        api_key = self._backend_api_key(GEMINI_CLI_BACKEND)
        if not api_key:
            return ""
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.local_agent_timeout_seconds,
                trust_env=api_httpx_trust_env(),
            ) as client:
                response = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{self.settings.gemini_api_model}:generateContent",
                    params={"key": api_key},
                    json={"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
                )
            if response.status_code >= 400:
                return f"Gemini API request failed: HTTP {response.status_code} {response.text}"
            return extract_gemini_response_text(response.json())
        except httpx.HTTPError as exc:
            return f"Gemini API request failed: {exc}"

    def _backend_api_key(self, backend: str) -> str:
        if backend == CODEX_BACKEND:
            return self.openai_api_key
        return self.api_keys.get(backend, "")

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

    def _trim_summary_line(self, line: str, limit: int = 180) -> str:
        return self._trim_stream_line(line, limit)

    def _is_benign_stderr(self, line: str) -> bool:
        return is_benign_agent_stderr(line)

    def _should_surface_stderr(self, line: str) -> bool:
        lowered = str(line or "").lower()
        if any(marker in lowered for marker in HIDE_STDERR_MARKERS):
            return False
        return any(marker in lowered for marker in SURFACED_STDERR_MARKERS)

    def _workspace_has_recent_artifacts(self, project: ProjectSpace, started_wall: float) -> bool:
        for artifact in scan_project_artifacts(project, limit=6):
            try:
                if Path(artifact.absolute_path).stat().st_mtime >= started_wall - 1:
                    return True
            except OSError:
                continue
        return False

    def _workspace_has_artifacts(self, project: ProjectSpace) -> bool:
        return bool(scan_project_artifacts(project, limit=1))

    def _workspace_artifact_result(self, project: ProjectSpace) -> str:
        artifacts = scan_project_artifacts(project, limit=8)
        paths = ", ".join(f"`{artifact.relative_path}`" for artifact in artifacts)
        return (
            "The selected local agent did not return final chat text, but workspace files changed during this run.\n\n"
            f"Workspace: `{project.path.resolve()}`\n"
            f"Changed artifacts: {paths}."
        )

    def _empty_backend_result(
        self,
        backend: str,
        command: str,
        return_code: int,
        final_output_path: Path | None,
    ) -> str:
        lines = [
            f"`{backend}` exited with code `{return_code}` but did not return final chat text.",
            "",
            f"Tried command: `{command}`",
        ]
        if final_output_path is not None:
            lines.extend(
                [
                    f"Expected final-message file: `{final_output_path}`",
                    "",
                    "That file is internal harness plumbing, not project source. Generated project code belongs under "
                    "the active workspace's `frontend/`, `backend/`, and `README.generated.md` files.",
                    "",
                    "For Codex, the harness now passes `--output-last-message` and reads that file directly. "
                    "If this keeps happening, run the same Codex command from a terminal to check login, model, "
                    "or sandbox prompts.",
                ]
            )
        return "\n".join(lines)

    def _terminate_process(self, process: subprocess.Popen) -> None:
        try:
            process.terminate()
        except OSError:
            return

    def _recover_workspace_artifacts(self, project: ProjectSpace, goal: str, role: str, output: str) -> str:
        lowered = output.lower()
        blocked = any(
            marker in lowered
            for marker in [
                "read-only",
                "read only",
                "cannot create files",
                "can't create files",
                "cannot modify",
                "can't modify",
                "could not modify",
                "could not create",
            ]
        )
        missing_visible_artifacts = self._workspace_needs_visible_scaffold(project, goal, role)
        if not blocked and not missing_visible_artifacts:
            return ""
        written = scaffold_project(project, goal, role)
        if not written:
            return ""
        relative = [path.relative_to(project.path).as_posix() for path in written]
        prefix = (
            "Workspace recovery wrote visible code artifacts"
            if blocked
            else "Filled missing visible workspace artifacts"
        )
        return f"{prefix}: " + ", ".join(f"`{item}`" for item in relative[:8])

    def _workspace_needs_visible_scaffold(self, project: ProjectSpace, goal: str, role: str) -> bool:
        if not should_scaffold(goal, role):
            return False
        artifacts = {artifact.relative_path for artifact in scan_project_artifacts(project, limit=32)}
        lowered = f"{goal} {role}".lower()
        needs_frontend = any(term in lowered for term in ["frontend", "front-end", "website", "page", "ui", "browser"])
        needs_backend = any(term in lowered for term in ["backend", "back-end", "api", "server", "database"])
        has_frontend = any(
            path.startswith("frontend/") or path.startswith("public/") or path in {"index.html", "app.js", "styles.css"}
            for path in artifacts
        )
        has_backend = any(path.startswith("backend/") or path in {"server.js", "app.py"} for path in artifacts)
        return (needs_frontend and not has_frontend) or (needs_backend and not has_backend)

    def _recovery_note(self, recovery: str) -> str:
        return f"\n\n{recovery}" if recovery else ""

    def _backend_env(self, backend: str) -> dict[str, str]:
        env = sanitized_agent_environment(os.environ.copy())
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
