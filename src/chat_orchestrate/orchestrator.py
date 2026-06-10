from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator

from .capabilities import infer_goal_roles
from .coordination import CoordinationManager
from .models import AgentSpec, AgentTurn, DelegatedTask, MachineNode, OrchestrationRun, ProgressUpdate, ProjectSpace
from .swarm_client import SwarmClient


AGENT_LIBRARY: dict[str, AgentSpec] = {
    "coordinator": AgentSpec(
        name="Coordinator",
        role="planning lead",
        instructions="Break the goal into clear workstreams, identify dependencies, and define success.",
    ),
    "researcher": AgentSpec(
        name="Researcher",
        role="context scout",
        instructions="Find constraints, unknowns, risks, and useful references before implementation.",
    ),
    "engineer": AgentSpec(
        name="Engineer",
        role="implementation specialist",
        instructions="Propose concrete implementation steps and code-level changes.",
    ),
    "backend": AgentSpec(
        name="Backend",
        role="backend builder",
        instructions="Build or specify server routes, data models, APIs, persistence, auth, and integration points.",
    ),
    "frontend": AgentSpec(
        name="Frontend",
        role="frontend builder",
        instructions="Build or specify UI screens, interactions, styling, state management, and browser verification.",
    ),
    "reviewer": AgentSpec(
        name="Reviewer",
        role="quality reviewer",
        instructions="Look for bugs, missing tests, unsafe assumptions, and deployment risks.",
    ),
    "documenter": AgentSpec(
        name="Documenter",
        role="documentation writer",
        instructions="Convert the outcome into crisp notes, runbooks, and handoff-ready markdown.",
    ),
}

ROUTING_PLANNER = AgentSpec(
    name="Routing Planner",
    role="machine routing coordinator",
    instructions=(
        "Read the user's goal and the live machine roster. Decide which exact live machine should own each role. "
        "Use the user's intent, machine names, local/remote references, capabilities, and available backends."
    ),
)


class Orchestrator:
    def __init__(
        self,
        client: SwarmClient,
        agent_names: list[str],
        coordination: CoordinationManager | None = None,
        delegated_task_wait_seconds: float = 30.0,
        progress_interval_seconds: float = 3.0,
        delegated_task_ack_seconds: float = 6.0,
    ) -> None:
        self.client = client
        self.agents = [AGENT_LIBRARY[name] for name in agent_names if name in AGENT_LIBRARY]
        self.coordination = coordination
        self.delegated_task_wait_seconds = delegated_task_wait_seconds
        self.progress_interval_seconds = progress_interval_seconds
        self.delegated_task_ack_seconds = (
            0.0
            if delegated_task_wait_seconds <= 0
            else min(delegated_task_ack_seconds, delegated_task_wait_seconds)
        )
        if not self.agents:
            self.agents = list(AGENT_LIBRARY.values())

    async def run(self, goal: str, project: ProjectSpace) -> AsyncIterator[ProgressUpdate | AgentTurn | OrchestrationRun]:
        run = OrchestrationRun(goal=goal, project=project)
        delegation_context = ""
        yield ProgressUpdate(
            message=f"Reading the goal and preparing `{project.name}` coordination.",
            phase="intake",
            role="coordinator",
        )

        if self.coordination is not None:
            yield ProgressUpdate(
                message="Confirming live orchestrator and machine roster before role routing.",
                phase="coordinator-check",
                role="coordinator",
            )
            orchestrator_node = self.coordination.get_or_elect_orchestrator()
            run.orchestrator_machine = orchestrator_node.machine_id
            roles = infer_goal_roles(goal)
            yield ProgressUpdate(
                message="Asking the coordinator agent to reason over the machine roster before assigning roles.",
                phase="routing",
                role="coordinator",
                assigned_machine=orchestrator_node.machine_id,
            )
            machines = self.coordination.list_machines()
            machine_preferences, planned_roles = await self._reasoned_machine_preferences(goal, project, roles, machines)
            roles = planned_roles or roles
            run.delegated_tasks = self.coordination.plan_delegation(
                run.run_id,
                project,
                goal,
                machine_preferences=machine_preferences,
                roles=roles,
            )
            self._add_delegated_agents(run)
            delegation_context = self._delegation_context(run)
            yield ProgressUpdate(
                message=self._planning_progress(run, machine_preferences),
                phase="delegation",
                role="coordinator",
                assigned_machine=orchestrator_node.machine_id,
            )

        context = ""

        for batch in self._execution_batches(self.agents):
            batch_context = f"{delegation_context}\n\n{context}".strip()
            batch_turns: list[AgentTurn] = []
            if len(batch) == 1:
                async for event in self._execute_agent(batch[0], project, goal, batch_context, run):
                    if isinstance(event, AgentTurn):
                        batch_turns.append(event)
                    yield event
            else:
                yield ProgressUpdate(
                    message=(
                        "Running independent role passes in parallel: "
                        + ", ".join(f"`{self._agent_key(agent)}`" for agent in batch)
                        + "."
                    ),
                    phase="parallel",
                    role="coordinator",
                    assigned_machine=run.orchestrator_machine,
                )
                async for event in self._execute_agent_batch(batch, project, goal, batch_context, run):
                    if isinstance(event, AgentTurn):
                        batch_turns.append(event)
                    yield event
            for turn in self._ordered_turns(batch, batch_turns):
                run.turns.append(turn)
                context = self._append_context(context, turn)

        run.final = self._summarize(run)
        yield ProgressUpdate(
            message="The run has enough agent output to answer; preparing the conversational summary.",
            phase="synthesis",
            role="coordinator",
            assigned_machine=run.orchestrator_machine,
        )
        yield run

    async def _execute_agent(
        self,
        agent: AgentSpec,
        project: ProjectSpace,
        goal: str,
        context: str,
        run: OrchestrationRun,
    ) -> AsyncIterator[ProgressUpdate | AgentTurn]:
        task = self._task_for_role(run, self._agent_key(agent))
        yield self._agent_start_progress(agent, task, run)
        if self._agent_key(agent) == "coordinator" and run.delegated_tasks:
            output = self._fast_coordinator_result(run)
            await self._complete_local_task(task, output)
            yield ProgressUpdate(
                message="Coordinator routing is ready; skipping a redundant local agent pass and moving directly to assigned work.",
                phase="coordinator-ready",
                agent=agent.name,
                role=task.role if task else agent.role,
                assigned_machine=task.assigned_machine if task else run.orchestrator_machine,
                preferred_backend=task.preferred_backend if task else None,
                task_id=task.task_id if task else None,
            )
            yield AgentTurn(
                agent=agent.name,
                role=agent.role,
                content=output,
                assigned_machine=self._assigned_machine_for_role(run, self._agent_key(agent)),
                preferred_backend=self._preferred_backend_for_role(run, self._agent_key(agent)),
            )
            return
        output = ""
        async for update in self.run_agent_with_progress(agent, project, goal, context, task):
            if isinstance(update, ProgressUpdate):
                yield update
            else:
                output = update
        await self._complete_local_task(task, output)
        yield AgentTurn(
            agent=agent.name,
            role=agent.role,
            content=output,
            assigned_machine=self._assigned_machine_for_role(run, self._agent_key(agent)),
            preferred_backend=self._preferred_backend_for_role(run, self._agent_key(agent)),
        )

    async def _complete_local_task(self, task: DelegatedTask | None, output: str) -> None:
        if task is None or self.coordination is None:
            return
        if task.assigned_machine != self.coordination.machine_id:
            return
        await asyncio.to_thread(self.coordination.complete_task, task.task_id, output)

    def _fast_coordinator_result(self, run: OrchestrationRun) -> str:
        assignments = "\n".join(
            f"- {task.role}: `{task.assigned_machine}` via `{task.preferred_backend}` ({task.status})"
            for task in run.delegated_tasks
        )
        return (
            "Coordinator routing is ready.\n\n"
            f"Goal: {run.goal}\n\n"
            f"Assignments:\n{assignments}\n\n"
            "I will watch the delegated task statuses and merge returned worker outputs."
        )

    async def _execute_agent_batch(
        self,
        agents: list[AgentSpec],
        project: ProjectSpace,
        goal: str,
        context: str,
        run: OrchestrationRun,
    ) -> AsyncIterator[ProgressUpdate | AgentTurn]:
        queue: asyncio.Queue[ProgressUpdate | AgentTurn | tuple[str, str]] = asyncio.Queue()

        async def pipe_agent(agent: AgentSpec) -> None:
            try:
                async for event in self._execute_agent(agent, project, goal, context, run):
                    await queue.put(event)
            finally:
                await queue.put(("done", self._agent_key(agent)))

        tasks = [asyncio.create_task(pipe_agent(agent)) for agent in agents]
        remaining = len(tasks)
        while remaining:
            item = await queue.get()
            if isinstance(item, tuple) and item[0] == "done":
                remaining -= 1
                continue
            yield item
        await asyncio.gather(*tasks)

    def _execution_batches(self, agents: list[AgentSpec]) -> list[list[AgentSpec]]:
        by_key = {self._agent_key(agent): agent for agent in agents}
        batches = []
        if "coordinator" in by_key:
            batches.append([by_key["coordinator"]])
        primary_keys = ["researcher", "backend", "frontend", "engineer"]
        primary = [by_key[key] for key in primary_keys if key in by_key]
        if primary:
            batches.append(primary)
        finishing = [by_key[key] for key in ["reviewer", "documenter"] if key in by_key]
        if finishing:
            batches.append(finishing)
        seen = {self._agent_key(agent) for batch in batches for agent in batch}
        remaining = [agent for agent in agents if self._agent_key(agent) not in seen]
        if remaining:
            batches.append(remaining)
        return batches

    def _ordered_turns(self, agents: list[AgentSpec], turns: list[AgentTurn]) -> list[AgentTurn]:
        order = {self._agent_key(agent): index for index, agent in enumerate(agents)}
        return sorted(turns, key=lambda turn: order.get(turn.agent.lower(), len(order)))

    def _append_context(self, context: str, turn: AgentTurn) -> str:
        block = f"## {turn.agent} ({turn.role})\n{turn.content}"
        return f"{context}\n\n{block}".strip()

    async def _reasoned_machine_preferences(
        self,
        goal: str,
        project: ProjectSpace,
        roles: list[str],
        machines: list[MachineNode],
    ) -> tuple[dict[str, str], list[str]]:
        if not machines or not roles:
            return {}, roles
        machine_payload = [
            {
                "machine_id": machine.machine_id,
                "hostname": machine.hostname,
                "aliases": self._machine_aliases(machine),
                "role": machine.role,
                "status": machine.status,
                "capabilities": machine.capabilities,
                "agent_backends": machine.agent_backends,
                "is_this_machine": self.coordination is not None and machine.machine_id == self.coordination.machine_id,
            }
            for machine in machines
        ]
        prompt = (
            "Return only compact JSON. Do not include markdown.\n"
            "Schema: {\"roles\":[\"coordinator\",\"backend\"],"
            "\"assignments\":[{\"role\":\"backend\",\"machine_id\":\"exact-live-machine-id\",\"reason\":\"short reason\"}]}\n\n"
            "Rules:\n"
            "- Infer the roles needed from the user's intent; use only these allowed role names: "
            "coordinator, researcher, engineer, backend, frontend, reviewer, documenter.\n"
            "- Always include coordinator plus any concrete work roles needed.\n"
            "- Do named-entity and coreference resolution over the whole goal: resolve hostnames, shortened "
            "machine names, nearby role phrases, and references like my laptop, local machine, this one, "
            "the other PC, host, worker, or coordinator.\n"
            "- Prefer exact machine_id values from the roster, but use aliases to resolve shortened names.\n"
            "- If the user refers to this/current/local/here, map that to the roster item with is_this_machine=true.\n"
            "- If the user names another machine or alias, map the mentioned responsibility to that live machine.\n"
            "- If a role is not clearly assigned by the user, choose the best live machine by capability/backend.\n"
            "- Never invent a machine_id.\n\n"
            f"Project: {project.name}\n"
            f"Goal: {goal}\n"
            f"Roles: {json.dumps(roles)}\n"
            f"Roster: {json.dumps(machine_payload)}"
        )
        try:
            output = await self.client.run_agent(ROUTING_PLANNER, project, goal, prompt)
        except Exception:
            return {}, roles
        return self._parse_machine_plan(output, machines, roles)

    def _parse_machine_plan(
        self,
        output: str,
        machines: list[MachineNode],
        fallback_roles: list[str],
    ) -> tuple[dict[str, str], list[str]]:
        payload = self._extract_json_object(output)
        if not payload:
            return {}, list(fallback_roles)
        assignments = payload.get("assignments", [])
        preferences: dict[str, str] = {}
        allowed_roles = {"coordinator", "researcher", "engineer", "backend", "frontend", "reviewer", "documenter"}
        planned_roles = [
            str(role).strip().lower()
            for role in payload.get("roles", [])
            if str(role).strip().lower() in allowed_roles
        ]
        if not isinstance(assignments, list):
            return preferences, self._unique_roles([*planned_roles, *fallback_roles])
        for item in assignments:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip().lower()
            machine_id = str(
                item.get("machine_id")
                or item.get("machine")
                or item.get("machine_ref")
                or item.get("target")
                or item.get("host")
                or ""
            ).strip()
            if role not in allowed_roles:
                continue
            if not machine_id and item.get("is_this_machine") and self.coordination is not None:
                machine_id = self.coordination.machine_id
            exact_machine = self._resolve_machine_reference(machine_id, machines)
            if not exact_machine and item.get("is_this_machine") and self.coordination is not None:
                exact_machine = self.coordination.machine_id
            if exact_machine:
                preferences[role] = exact_machine
                planned_roles.append(role)
        return preferences, self._unique_roles(["coordinator", *planned_roles, *fallback_roles])

    def _resolve_machine_reference(self, reference: str, machines: list[MachineNode]) -> str:
        clean = str(reference or "").strip()
        if not clean:
            return ""
        for machine in machines:
            if clean == machine.machine_id:
                return machine.machine_id

        normalized = self._normalize_machine_id(clean)
        if not normalized:
            return ""
        local_references = {
            "this",
            "thismachine",
            "thiscomputer",
            "thispc",
            "currentmachine",
            "currentcomputer",
            "local",
            "localmachine",
            "localcomputer",
            "here",
        }
        if normalized in local_references:
            return self.coordination.machine_id if self.coordination is not None else ""
        alias_matches: dict[str, set[str]] = {}
        for machine in machines:
            for alias in self._machine_aliases(machine):
                alias_matches.setdefault(self._normalize_machine_id(alias), set()).add(machine.machine_id)

        exact = alias_matches.get(normalized, set())
        if len(exact) == 1:
            return next(iter(exact))

        if len(normalized) < 5:
            return ""
        fuzzy: set[str] = set()
        for alias, machine_ids in alias_matches.items():
            if alias.startswith(normalized) or normalized.startswith(alias) or normalized in alias:
                fuzzy.update(machine_ids)
        return next(iter(fuzzy)) if len(fuzzy) == 1 else ""

    def _machine_aliases(self, machine: MachineNode) -> list[str]:
        aliases = [machine.machine_id, machine.hostname]
        for value in [machine.machine_id, machine.hostname]:
            normalized = self._normalize_machine_id(value)
            aliases.append(normalized)
            for size in (6, 8, 10, 12):
                if len(normalized) >= size:
                    aliases.append(normalized[:size])
        return self._unique_roles(aliases)

    def _unique_roles(self, roles: list[str]) -> list[str]:
        result = []
        for role in roles:
            clean = str(role).strip().lower()
            if clean and clean not in result:
                result.append(clean)
        return result

    def _extract_json_object(self, text: str) -> dict:
        clean = text.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)
        try:
            payload = json.loads(clean)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", clean, flags=re.DOTALL)
            if not match:
                return {}
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}
        return payload if isinstance(payload, dict) else {}

    def _normalize_machine_id(self, value: str) -> str:
        return "".join(char for char in value.lower().strip() if char.isalnum())

    def _planning_progress(self, run: OrchestrationRun, machine_preferences: dict[str, str] | None = None) -> str:
        if not run.delegated_tasks:
            return f"`{run.orchestrator_machine}` is coordinating this run locally."
        assignments = []
        for task in run.delegated_tasks:
            assignments.append(
                f"{task.role} -> `{task.assigned_machine}` via `{task.preferred_backend}`"
            )
        source = "coordinator-agent routing" if machine_preferences else "scheduler fallback"
        return f"Delegation is set from {source}: " + "; ".join(assignments[:6])

    def _agent_start_progress(
        self,
        agent: AgentSpec,
        task: DelegatedTask | None,
        run: OrchestrationRun,
    ) -> ProgressUpdate:
        if task is None:
            return ProgressUpdate(
                message=f"{agent.name} is starting as {agent.role} on the local agent brain.",
                phase="starting",
                agent=agent.name,
                role=agent.role,
                assigned_machine=run.orchestrator_machine,
            )
        if self.coordination is not None and task.assigned_machine != self.coordination.machine_id:
            message = (
                f"{agent.name} is assigned to `{task.assigned_machine}` as `{task.role}` "
                f"using `{task.preferred_backend}`. Waiting for that machine to report back."
            )
        else:
            message = (
                f"{agent.name} is running here as `{task.role}` using `{task.preferred_backend}`. "
                "It knows this is its slice of the orchestrated project run."
            )
        return ProgressUpdate(
            message=message,
            phase="assigned",
            agent=agent.name,
            role=task.role,
            assigned_machine=task.assigned_machine,
            preferred_backend=task.preferred_backend,
            task_id=task.task_id,
        )

    def _agent_wait_progress(
        self,
        agent: AgentSpec,
        task: DelegatedTask | None,
        elapsed: int,
        tick: int,
        observed_task: DelegatedTask | None = None,
    ) -> ProgressUpdate:
        phase, activity = self._wait_activity(agent, task, tick)
        if task is None:
            return ProgressUpdate(
                message=f"{agent.name}: {activity} Elapsed: {elapsed}s.",
                phase=phase,
                agent=agent.name,
                role=agent.role,
                elapsed_seconds=elapsed,
            )
        if self.coordination is not None and task.assigned_machine != self.coordination.machine_id:
            status = observed_task.status if observed_task else task.status
            if status == "running":
                activity = "Remote worker has claimed the task and is running the local agent."
            elif status == "failed":
                activity = "Remote worker reported a failure; capturing the error for recovery."
            elif status == "completed":
                activity = "Remote worker finished; collecting the returned result."
            elif status == "delegated":
                activity = "Task is delegated but has not been claimed yet; checking worker availability."
            message = (
                f"{activity} Status `{status}` on `{task.assigned_machine}` for `{task.role}` "
                f"via `{task.preferred_backend}`. Elapsed: {elapsed}s."
            )
        else:
            message = (
                f"{agent.name}: {activity} Role `{task.role}` is running via `{task.preferred_backend}`. "
                f"Elapsed: {elapsed}s."
            )
        return ProgressUpdate(
            message=message,
            phase=phase,
            agent=agent.name,
            role=task.role,
            assigned_machine=task.assigned_machine,
            preferred_backend=task.preferred_backend,
            task_id=task.task_id,
            elapsed_seconds=elapsed,
        )

    def _wait_activity(self, agent: AgentSpec, task: DelegatedTask | None, tick: int) -> tuple[str, str]:
        role = (task.role if task else agent.role).lower()
        agent_name = agent.name.lower()
        remote = bool(task and self.coordination is not None and task.assigned_machine != self.coordination.machine_id)
        if remote:
            activities = [
                ("handoff", "Task is handed off; checking whether the remote worker has claimed it."),
                ("remote-work", "Remote agent should be working its assigned slice now."),
                ("artifact-check", "Watching for returned notes, code output, or failure details."),
                ("routing-check", "Keeping the assignment live while the coordinator waits for a result."),
            ]
        elif role == "coordinator" or agent_name == "coordinator":
            activities = [
                ("routing", "Reading the user intent against the live machine roster."),
                ("decomposition", "Separating project work into roles and dependencies."),
                ("validation", "Checking that assignments line up with available machines and backends."),
                ("synthesis", "Preparing the coordination summary without exposing private chain-of-thought."),
            ]
        elif role in {"backend", "frontend", "engineer"}:
            activities = [
                ("context", "Checking project scope and existing handoff context."),
                ("implementation", "Working through the concrete build path for this role."),
                ("artifact-plan", "Identifying files, commands, patches, or preview artifacts to return."),
                ("handoff", "Shaping the result so the coordinator can merge it with other workstreams."),
            ]
        elif role == "reviewer":
            activities = [
                ("review", "Scanning for risks, regressions, and missing validation."),
                ("test-plan", "Thinking through the most relevant checks for this change."),
                ("handoff", "Condensing findings into coordinator-ready notes."),
            ]
        else:
            activities = [
                ("context", "Reading the role instructions and prior agent context."),
                ("work", "Producing the assigned role output."),
                ("handoff", "Formatting the result for the coordinator."),
            ]
        index = max(0, tick - 1) % len(activities)
        return activities[index]

    async def _run_or_wait_for_agent(
        self,
        agent: AgentSpec,
        project: ProjectSpace,
        goal: str,
        context: str,
        task: DelegatedTask | None,
    ) -> str:
        if task is None or self.coordination is None:
            return await self.client.run_agent(agent, project, goal, context)
        if task.assigned_machine == self.coordination.machine_id:
            return await self.client.run_agent(agent, project, goal, context)

        completed = await self._wait_for_delegated_task(task, self.delegated_task_ack_seconds)
        if completed and completed.status == "failed":
            return (
                f"`{completed.preferred_backend}` on `{completed.assigned_machine}` reported a tool-access failure "
                f"while handling `{completed.role}`.\n\n"
                f"{self._friendly_worker_failure(completed.result)}"
            )
        if completed and completed.result:
            return f"`{completed.preferred_backend}` result from `{completed.assigned_machine}`\n\n{completed.result}"
        status = completed.status if completed else task.status
        return (
            f"`{agent.name}` is handed off to `{task.assigned_machine}` via `{task.preferred_backend}`. "
            f"Current status is `{status}`; that machine will keep working and report back through the dashboard."
        )

    def _friendly_worker_failure(self, result: str) -> str:
        clean = result.strip()
        lowered = clean.lower()
        if "sandbox" in lowered or "rejected" in lowered or "permission" in lowered:
            return (
                "The machine is online, but its local agent could not access the project workspace or shell tools. "
                "Restart that worker after pulling the latest code so Codex is launched with the project directory "
                "and `workspace-write` sandbox, then run the task again."
            )
        if "not executable" in lowered or "not reachable" in lowered or "not found" in lowered:
            return (
                "The machine is online, but the selected local-agent command is not callable from that process. "
                "Use Detect or set the command in Settings on that machine, then retry."
            )
        return clean or "The worker failed without returning details."

    async def run_agent_with_progress(
        self,
        agent: AgentSpec,
        project: ProjectSpace,
        goal: str,
        context: str,
        task: DelegatedTask | None,
    ) -> AsyncIterator[ProgressUpdate | str]:
        work = asyncio.create_task(self._run_or_wait_for_agent(agent, project, goal, context, task))
        started = asyncio.get_running_loop().time()
        tick = 0
        while not work.done():
            await asyncio.sleep(self.progress_interval_seconds)
            if work.done():
                break
            tick += 1
            elapsed = int(asyncio.get_running_loop().time() - started)
            observed_task = None
            if task is not None and self.coordination is not None:
                observed_task = await asyncio.to_thread(self.coordination.get_task, task.task_id)
            yield self._agent_wait_progress(agent, task, elapsed, tick, observed_task)
        yield await work

    async def _wait_for_delegated_task(self, task: DelegatedTask, wait_seconds: float | None = None) -> DelegatedTask | None:
        wait_seconds = self.delegated_task_wait_seconds if wait_seconds is None else wait_seconds
        if self.coordination is None or wait_seconds <= 0:
            return None
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            current = await asyncio.to_thread(self.coordination.get_task, task.task_id)
            if current and current.status in {"completed", "failed"}:
                return current
            await asyncio.sleep(1)
        return await asyncio.to_thread(self.coordination.get_task, task.task_id)

    def _add_delegated_agents(self, run: OrchestrationRun) -> None:
        existing = {self._agent_key(agent) for agent in self.agents}
        additions = []
        for task in run.delegated_tasks:
            if task.role in AGENT_LIBRARY and task.role not in existing:
                additions.append(AGENT_LIBRARY[task.role])
                existing.add(task.role)
        self.agents = [*self.agents, *additions]

    def _summarize(self, run: OrchestrationRun) -> str:
        handoffs = "\n".join(
            f"- **{turn.agent}**"
            f"{f' on `{turn.assigned_machine}`' if turn.assigned_machine else ''}: "
            f"{f'`{turn.preferred_backend}` ' if turn.preferred_backend else ''}"
            f"{self._first_line(turn.content)}"
            for turn in run.turns
        )
        delegation = self._delegation_summary(run)
        return (
            f"## Run {run.run_id}\n\n"
            f"**Project:** {run.project.name}\n"
            f"**Orchestrator machine:** `{run.orchestrator_machine or 'local'}`\n"
            f"**Goal:** {run.goal}\n\n"
            f"{delegation}"
            f"### Agent Handoffs\n{handoffs}"
        )

    def _first_line(self, content: str) -> str:
        for line in content.splitlines():
            clean = line.strip(" -")
            if clean:
                return clean
        return "No output captured."

    def _delegation_context(self, run: OrchestrationRun) -> str:
        if not run.delegated_tasks:
            return ""
        assignments = "\n".join(
            f"- {task.role}: {task.title} -> {task.assigned_machine}"
            f" ({task.preferred_backend})"
            for task in run.delegated_tasks
        )
        return (
            "## Distributed Coordination\n"
            f"Orchestrator machine: {run.orchestrator_machine}\n"
            f"Delegated tasks:\n{assignments}"
        )

    def _delegation_summary(self, run: OrchestrationRun) -> str:
        if not run.delegated_tasks:
            return ""
        assignments = "\n".join(
            f"- `{task.assigned_machine}` via `{task.preferred_backend}`: "
            f"**{task.role}** - {task.title}"
            for task in run.delegated_tasks
        )
        return f"### Delegation Plan\n{assignments}\n\n"

    def _assigned_machine_for_role(self, run: OrchestrationRun, role: str) -> str | None:
        for task in run.delegated_tasks:
            if task.role == role:
                return task.assigned_machine
        return None

    def _preferred_backend_for_role(self, run: OrchestrationRun, role: str) -> str | None:
        for task in run.delegated_tasks:
            if task.role == role:
                return task.preferred_backend
        return None

    def _task_for_role(self, run: OrchestrationRun, role: str) -> DelegatedTask | None:
        for task in run.delegated_tasks:
            if task.role == role:
                return task
        return None

    def _agent_key(self, agent: AgentSpec) -> str:
        return agent.name.lower()
