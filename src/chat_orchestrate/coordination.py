from __future__ import annotations

import json
import hashlib
import socket
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from json import JSONDecodeError
from uuid import uuid4

import httpx

from .backends import CLAUDE_CODE_BACKEND, CODEX_BACKEND, GEMINI_CLI_BACKEND, OPEN_SWARM_BACKEND, SIMULATED_BACKEND
from .capabilities import infer_goal_roles
from .models import DelegatedTask, MachineNode, ProjectSpace


class CoordinationError(RuntimeError):
    pass


class CoordinationManager:
    """Shared file-backed machine registry and task delegation state."""

    def __init__(
        self,
        state_path: Path,
        machine_id: str,
        agent_roles: list[str],
        agent_backends: list[str] | None = None,
        orchestrator_ttl_seconds: int = 120,
        cluster_id: str = "local",
        coordination_token: str = "",
        backend: str = "file",
        http_url: str = "",
        http_urls: str = "",
    ) -> None:
        self.state_path = state_path.resolve()
        self.backend = backend.lower().strip() or "file"
        self.http_urls = self._normalize_urls(http_url, http_urls)
        self.http_url = self.http_urls[0] if self.http_urls else ""
        self.cluster_id = cluster_id.strip() or "local"
        self.coordination_token = coordination_token.strip()
        self.token_hash = self._hash_token(coordination_token)
        self.machine_id = machine_id.strip() or socket.gethostname().lower()
        self.hostname = socket.gethostname()
        self.agent_roles = agent_roles
        self.agent_backends = agent_backends or [SIMULATED_BACKEND]
        self.orchestrator_ttl = timedelta(seconds=orchestrator_ttl_seconds)
        if self.backend == "file":
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
        elif self.backend == "http" and not self.http_urls:
            raise CoordinationError(
                "COORDINATION_HTTP_URL or COORDINATION_HTTP_URLS is required when COORDINATION_BACKEND=http."
            )

    def heartbeat(self) -> MachineNode:
        state = self._load()
        now = datetime.now(UTC)
        orchestrator = self._active_orchestrator_id(state, now)
        if state.get("orchestrator_machine") == self.machine_id:
            orchestrator = self.machine_id
        role = "orchestrator" if orchestrator == self.machine_id else "worker"

        node = MachineNode(
            machine_id=self.machine_id,
            hostname=self.hostname,
            role=role,
            status="online",
            capabilities=self.agent_roles,
            agent_backends=self.agent_backends,
            last_seen=now,
        )
        state["machines"][self.machine_id] = self._machine_to_json(node)
        self._save(state)
        return node

    def list_machines(self) -> list[MachineNode]:
        state = self._load()
        machines = [self._machine_from_json(item) for item in state["machines"].values()]
        return sorted(machines, key=lambda machine: machine.machine_id)

    def claim_orchestrator(self) -> MachineNode:
        state = self._load()
        state["orchestrator_machine"] = self.machine_id
        state["orchestrator_claimed_at"] = self._now_text()
        self._save(state)
        return self.heartbeat()

    def release_orchestrator(self) -> None:
        state = self._load()
        if state.get("orchestrator_machine") == self.machine_id:
            state["orchestrator_machine"] = None
            state["orchestrator_claimed_at"] = None
        self._save(state)
        self.heartbeat()

    def get_or_elect_orchestrator(self) -> MachineNode:
        state = self._load()
        now = datetime.now(UTC)
        orchestrator_id = self._active_orchestrator_id(state, now)

        if orchestrator_id is None:
            online = self._online_machines(state, now)
            orchestrator_id = online[0].machine_id if online else self.machine_id
            state["orchestrator_machine"] = orchestrator_id
            state["orchestrator_claimed_at"] = self._now_text()
            self._save(state)

        self.heartbeat()
        return self._machine_from_json(self._load()["machines"][orchestrator_id])

    def plan_delegation(
        self,
        run_id: str,
        project: ProjectSpace,
        goal: str,
        machine_preferences: dict[str, str] | None = None,
        roles: list[str] | None = None,
    ) -> list[DelegatedTask]:
        state = self._load()
        now = datetime.now(UTC)
        machines = self._online_machines(state, now)
        if not machines:
            machines = [self.heartbeat()]

        tasks = []
        for role in roles or self._roles_for_goal(goal):
            machine = self._best_machine_for_role(machines, role, goal, (machine_preferences or {}).get(role, ""))
            preferred_backend = self._preferred_backend_for_role(machine, role, goal)
            tasks.append(
                DelegatedTask(
                    task_id=uuid4().hex,
                    run_id=run_id,
                    project=project.name,
                    goal=goal,
                    role=role,
                    title=self._task_title(role, goal),
                    assigned_machine=machine.machine_id,
                    preferred_backend=preferred_backend,
                    status="delegated",
                    created_at=now,
                    updated_at=now,
                )
            )

        state = self._load()
        state["tasks"].extend(self._task_to_json(task) for task in tasks)
        self._save(state)
        return tasks

    def list_tasks(self, limit: int = 20) -> list[DelegatedTask]:
        state = self._load()
        tasks = [self._task_from_json(item) for item in state["tasks"]]
        return sorted(tasks, key=lambda task: task.created_at, reverse=True)[:limit]

    def get_task(self, task_id: str) -> DelegatedTask | None:
        state = self._load()
        for item in state["tasks"]:
            if item["task_id"] == task_id:
                return self._task_from_json(item)
        return None

    def claim_next_task(self) -> DelegatedTask | None:
        state = self._load()
        now = datetime.now(UTC)
        for item in state["tasks"]:
            if item["assigned_machine"] != self.machine_id:
                continue
            if item["status"] != "delegated":
                continue
            if item.get("preferred_backend") not in self.agent_backends:
                continue
            item["status"] = "running"
            item["updated_at"] = now.isoformat()
            self._save(state)
            return self._task_from_json(item)
        return None

    def complete_task(self, task_id: str, result: str, status: str = "completed") -> None:
        state = self._load()
        now = self._now_text()
        for item in state["tasks"]:
            if item["task_id"] == task_id:
                item["status"] = status
                item["result"] = result
                item["updated_at"] = now
                break
        self._save(state)

    def _roles_for_goal(self, goal: str) -> list[str]:
        return infer_goal_roles(goal)

    def _best_machine_for_role(
        self,
        machines: list[MachineNode],
        role: str,
        goal: str = "",
        preferred_machine_id: str = "",
    ) -> MachineNode:
        preferred = self._machine_by_id(machines, preferred_machine_id)
        if preferred is not None:
            return preferred
        hinted = self._hinted_machine_for_role(machines, role, goal)
        if hinted is not None:
            return hinted
        capable = [machine for machine in machines if role in machine.capabilities]
        pool = capable or machines
        backend_capable = [machine for machine in pool if self._preferred_backend_for_role(machine, role, "")]
        pool = backend_capable or pool
        workers = [machine for machine in pool if machine.role != "orchestrator"]
        pool = workers or pool
        index = sum(ord(char) for char in role) % len(pool)
        return pool[index]

    def _machine_by_id(self, machines: list[MachineNode], machine_id: str) -> MachineNode | None:
        clean = self._normalize_machine_id(machine_id)
        if not clean:
            return None
        for machine in machines:
            if clean == self._normalize_machine_id(machine.machine_id):
                return machine
        for machine in machines:
            if clean == self._normalize_machine_id(machine.hostname):
                return machine
        return None

    def _hinted_machine_for_role(self, machines: list[MachineNode], role: str, goal: str) -> MachineNode | None:
        lowered = goal.lower()
        if role in {"backend", "engineer"} and "this machine" in lowered:
            for machine in machines:
                if machine.machine_id == self.machine_id:
                    return machine
        if role == "frontend":
            for machine in machines:
                identifiers = [machine.machine_id.lower(), machine.hostname.lower()]
                if machine.machine_id != self.machine_id and any(identifier in lowered for identifier in identifiers):
                    return machine
        return None

    def _preferred_backend_for_role(self, machine: MachineNode, role: str, goal: str) -> str:
        lowered = goal.lower()
        if role in {"engineer", "backend", "frontend"} and CODEX_BACKEND in machine.agent_backends:
            return CODEX_BACKEND
        if role in {"coordinator", "researcher"} and CLAUDE_CODE_BACKEND in machine.agent_backends:
            return CLAUDE_CODE_BACKEND
        if "codex" in lowered and CODEX_BACKEND in machine.agent_backends:
            return CODEX_BACKEND
        if "claude" in lowered and CLAUDE_CODE_BACKEND in machine.agent_backends:
            return CLAUDE_CODE_BACKEND
        if "gemini" in lowered and GEMINI_CLI_BACKEND in machine.agent_backends:
            return GEMINI_CLI_BACKEND
        if OPEN_SWARM_BACKEND in machine.agent_backends:
            return OPEN_SWARM_BACKEND
        if machine.agent_backends:
            return machine.agent_backends[0]
        return SIMULATED_BACKEND

    def _task_title(self, role: str, goal: str) -> str:
        short_goal = goal.strip().splitlines()[0][:80]
        return f"{role.title()} pass: {short_goal}"

    def _active_orchestrator_id(self, state: dict, now: datetime) -> str | None:
        orchestrator_id = state.get("orchestrator_machine")
        if not orchestrator_id:
            return None
        raw_node = state["machines"].get(orchestrator_id)
        if not raw_node:
            return None
        node = self._machine_from_json(raw_node)
        if now - node.last_seen > self.orchestrator_ttl:
            return None
        return orchestrator_id

    def _online_machines(self, state: dict, now: datetime) -> list[MachineNode]:
        machines = [self._machine_from_json(item) for item in state["machines"].values()]
        online = [
            machine
            for machine in machines
            if machine.status == "online" and now - machine.last_seen <= self.orchestrator_ttl
        ]
        return sorted(online, key=lambda machine: machine.machine_id)

    def _load(self) -> dict:
        if self.backend == "http":
            state = self._http_load()
            self._assert_cluster_access(state)
            state.setdefault("machines", {})
            state.setdefault("tasks", [])
            return state

        if not self.state_path.exists():
            return self._initial_state()
        state = self._load_file_state()
        self._assert_cluster_access(state)
        state.setdefault("cluster_id", self.cluster_id)
        state.setdefault("token_hash", self.token_hash)
        state.setdefault("machines", {})
        state.setdefault("tasks", [])
        return state

    def _save(self, state: dict) -> None:
        self._assert_cluster_access(state)
        if self.backend == "http":
            self._http_save(state)
            return
        self._atomic_write_state(state)

    def _initial_state(self) -> dict:
        return {
            "cluster_id": self.cluster_id,
            "token_hash": self.token_hash,
            "orchestrator_machine": None,
            "orchestrator_claimed_at": None,
            "machines": {},
            "tasks": [],
        }

    def _load_file_state(self) -> dict:
        raw = self.state_path.read_text(encoding="utf-8")
        try:
            return json.loads(raw)
        except JSONDecodeError as exc:
            decoder = json.JSONDecoder()
            try:
                state, end = decoder.raw_decode(raw)
            except JSONDecodeError:
                self._quarantine_corrupt_state(raw)
                return self._initial_state()
            if not isinstance(state, dict):
                self._quarantine_corrupt_state(raw)
                return self._initial_state()
            if raw[end:].strip():
                self._quarantine_corrupt_state(raw)
                self._atomic_write_state(state)
                return state
            raise exc

    def _quarantine_corrupt_state(self, raw: str) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        backup = self.state_path.with_name(f"{self.state_path.stem}.corrupt-{stamp}{self.state_path.suffix}")
        backup.write_text(raw, encoding="utf-8")

    def _atomic_write_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, indent=2)
        temp_path = self.state_path.with_name(f"{self.state_path.name}.{uuid4().hex}.tmp")
        temp_path.write_text(payload, encoding="utf-8")
        temp_path.replace(self.state_path)

    def _http_load(self) -> dict:
        return self._http_request("GET").json()

    def _http_save(self, state: dict) -> None:
        self._http_request("PUT", state)

    def _http_request(self, method: str, state: dict | None = None) -> httpx.Response:
        last_error: Exception | None = None
        for url in self.http_urls:
            try:
                if method == "GET":
                    response = httpx.get(
                        f"{url}/state",
                        headers=self._http_headers(),
                        params={"cluster_id": self.cluster_id},
                        timeout=8,
                        trust_env=False,
                    )
                else:
                    response = httpx.put(
                        f"{url}/state",
                        headers=self._http_headers(),
                        params={"cluster_id": self.cluster_id},
                        json=state,
                        timeout=8,
                        trust_env=False,
                    )
                if response.status_code in {401, 403}:
                    raise CoordinationError(
                        f"Coordinator authentication failed at `{url}`. "
                        "Use Connect to Coordinator again or End Session to clear stale tokens."
                    )
                response.raise_for_status()
            except CoordinationError:
                raise
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_error = exc
                continue
            self.http_url = url
            return response
        raise CoordinationError(f"No reachable coordinator URL. Last error: {last_error}") from last_error

    def _http_headers(self) -> dict[str, str]:
        headers = {}
        if self.coordination_token:
            headers["Authorization"] = f"Bearer {self.coordination_token}"
        return headers

    def _assert_cluster_access(self, state: dict) -> None:
        state_cluster = state.get("cluster_id", self.cluster_id)
        state_token_hash = state.get("token_hash", "")
        if state_cluster != self.cluster_id:
            raise CoordinationError(
                f"Coordination state belongs to cluster `{state_cluster}`, not `{self.cluster_id}`."
            )
        if state_token_hash and state_token_hash != self.token_hash:
            raise CoordinationError("Coordination token does not match this shared state.")

    def _hash_token(self, token: str) -> str:
        clean = token.strip()
        if not clean:
            return ""
        return hashlib.sha256(clean.encode("utf-8")).hexdigest()

    def _normalize_urls(self, primary: str, additional: str) -> list[str]:
        raw_items = [primary, *additional.replace("\n", ",").split(",")]
        urls = []
        for item in raw_items:
            url = item.strip().rstrip("/")
            if url and url not in urls:
                urls.append(url)
        return urls

    def _normalize_machine_id(self, value: str) -> str:
        return "".join(char for char in value.lower().strip() if char.isalnum())

    def _machine_from_json(self, item: dict) -> MachineNode:
        return MachineNode(
            machine_id=item["machine_id"],
            hostname=item["hostname"],
            role=item["role"],
            status=item["status"],
            capabilities=list(item.get("capabilities", [])),
            agent_backends=list(item.get("agent_backends", [SIMULATED_BACKEND])),
            last_seen=datetime.fromisoformat(item["last_seen"]),
        )

    def _machine_to_json(self, node: MachineNode) -> dict:
        payload = asdict(node)
        payload["last_seen"] = node.last_seen.isoformat()
        return payload

    def _task_from_json(self, item: dict) -> DelegatedTask:
        return DelegatedTask(
            task_id=item["task_id"],
            run_id=item["run_id"],
            project=item["project"],
            goal=item["goal"],
            role=item["role"],
            title=item["title"],
            assigned_machine=item["assigned_machine"],
            preferred_backend=item.get("preferred_backend", SIMULATED_BACKEND),
            status=item["status"],
            created_at=datetime.fromisoformat(item["created_at"]),
            updated_at=datetime.fromisoformat(item["updated_at"]) if item.get("updated_at") else None,
            result=item.get("result", ""),
        )

    def _task_to_json(self, task: DelegatedTask) -> dict:
        payload = asdict(task)
        payload["created_at"] = task.created_at.isoformat()
        payload["updated_at"] = task.updated_at.isoformat() if task.updated_at else None
        return payload

    def _now_text(self) -> str:
        return datetime.now(UTC).isoformat()
