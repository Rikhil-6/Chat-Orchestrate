from __future__ import annotations

import json
import hashlib
import socket
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from json import JSONDecodeError
from uuid import uuid4

import httpx

from .backends import CLAUDE_CODE_BACKEND, CODEX_BACKEND, GEMINI_CLI_BACKEND, OPEN_SWARM_BACKEND, SIMULATED_BACKEND
from .capabilities import infer_goal_roles
from .models import DelegatedTask, MachineNode, ProjectSpace


def unique_non_empty(values: list[str]) -> list[str]:
    result = []
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in result:
            result.append(clean)
    return result


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
        task_lease_seconds: int = 120,
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
        self.task_lease = timedelta(seconds=max(10, task_lease_seconds))
        if self.backend == "file":
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
        elif self.backend == "http" and not self.http_urls:
            raise CoordinationError(
                "COORDINATION_HTTP_URL or COORDINATION_HTTP_URLS is required when COORDINATION_BACKEND=http."
            )

    def heartbeat(self) -> MachineNode:
        with self._state_lock():
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
        with self._state_lock():
            state = self._load()
            state["orchestrator_machine"] = self.machine_id
            state["orchestrator_claimed_at"] = self._now_text()
            self._save(state)
        return self.heartbeat()

    def release_orchestrator(self) -> None:
        with self._state_lock():
            state = self._load()
            if state.get("orchestrator_machine") == self.machine_id:
                state["orchestrator_machine"] = None
                state["orchestrator_claimed_at"] = None
            self._save(state)
        self.heartbeat()

    def get_or_elect_orchestrator(self) -> MachineNode:
        with self._state_lock():
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
        task_briefs: dict[str, str] | None = None,
    ) -> list[DelegatedTask]:
        with self._state_lock():
            state = self._load()
            now = datetime.now(UTC)
            machines = self._online_machines(state, now)
            if not machines:
                fallback_node = MachineNode(
                    machine_id=self.machine_id,
                    hostname=self.hostname,
                    role="worker",
                    status="online",
                    capabilities=self.agent_roles,
                    agent_backends=self.agent_backends,
                    last_seen=now,
                )
                state["machines"][self.machine_id] = self._machine_to_json(fallback_node)
                machines = [fallback_node]

            tasks = []
            for role in roles or self._roles_for_goal(goal):
                machine = self._best_machine_for_role(machines, role, goal, (machine_preferences or {}).get(role, ""))
                preferred_backend = self._preferred_backend_for_role(machine, role, goal)
                brief = self._task_brief(role, goal, (task_briefs or {}).get(role, ""))
                tasks.append(
                    DelegatedTask(
                        task_id=uuid4().hex,
                        run_id=run_id,
                        project=project.name,
                        goal=goal,
                        role=role,
                        title=self._task_title(role, goal, brief),
                        assigned_machine=machine.machine_id,
                        preferred_backend=preferred_backend,
                        status="delegated",
                        created_at=now,
                        brief=brief,
                        updated_at=now,
                        original_machine=machine.machine_id,
                    )
                )

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
        with self._state_lock():
            state = self._load()
            now = datetime.now(UTC)
            self._recover_expired_tasks(state, now)
            for item in state["tasks"]:
                if item["assigned_machine"] != self.machine_id:
                    continue
                if item["status"] != "delegated":
                    continue
                if item.get("preferred_backend") not in self.agent_backends:
                    continue
                item["status"] = "running"
                item["claimed_by"] = self.machine_id
                item["lease_expires_at"] = (now + self.task_lease).isoformat()
                item["original_machine"] = str(item.get("original_machine", "")).strip() or item["assigned_machine"]
                item["progress_note"] = (
                    item.get("progress_note")
                    or f"Task claimed by `{self.machine_id}`; local agent is opening the workspace."
                )
                item["updated_at"] = now.isoformat()
                self._save(state)
                return self._task_from_json(item)
            return None

    def note_task_progress(self, task_id: str, note: str, status: str | None = None) -> bool:
        with self._state_lock():
            state = self._load()
            now = datetime.now(UTC)
            for item in state["tasks"]:
                if item["task_id"] != task_id:
                    continue
                if status:
                    item["status"] = status
                item["claimed_by"] = self.machine_id
                item["progress_note"] = str(note or "").strip()
                if item.get("status") == "running":
                    item["lease_expires_at"] = (now + self.task_lease).isoformat()
                item["updated_at"] = now.isoformat()
                self._save(state)
                return True
            return False

    def renew_task_lease(self, task_id: str) -> bool:
        with self._state_lock():
            state = self._load()
            now = datetime.now(UTC)
            for item in state["tasks"]:
                if item["task_id"] != task_id:
                    continue
                if item.get("status") != "running":
                    return False
                claimed_by = str(item.get("claimed_by", "")).strip()
                if claimed_by and claimed_by != self.machine_id:
                    return False
                item["claimed_by"] = self.machine_id
                item["lease_expires_at"] = (now + self.task_lease).isoformat()
                if not item.get("progress_note"):
                    item["progress_note"] = f"Task is still running on `{self.machine_id}`."
                item["updated_at"] = now.isoformat()
                self._save(state)
                return True
            return False

    def complete_task(
        self,
        task_id: str,
        result: str,
        status: str = "completed",
        *,
        completed_by: str = "",
        completion_source: str = "direct",
    ) -> None:
        with self._state_lock():
            state = self._load()
            now = self._now_text()
            for item in state["tasks"]:
                if item["task_id"] == task_id:
                    item["status"] = status
                    item["result"] = result
                    item["claimed_by"] = ""
                    item["lease_expires_at"] = None
                    item["progress_note"] = ""
                    item["completed_by"] = (completed_by or self.machine_id).strip()
                    item["completion_source"] = (completion_source or "direct").strip()
                    item["updated_at"] = now
                    break
            self._save(state)

    def _recover_expired_tasks(self, state: dict, now: datetime) -> None:
        online = self._online_machines(state, now)
        online_ids = {node.machine_id for node in online}
        changed = False
        for item in state["tasks"]:
            if item.get("status") != "running":
                continue
            if not self._task_lease_is_expired(item, now):
                continue

            assigned_machine = str(item.get("assigned_machine", "")).strip()
            preferred_backend = str(item.get("preferred_backend", SIMULATED_BACKEND)).strip() or SIMULATED_BACKEND
            target_machine = assigned_machine
            if assigned_machine not in online_ids and online:
                role = str(item.get("role", "engineer")).strip() or "engineer"
                goal = str(item.get("goal", "")).strip()
                backend_capable = [machine for machine in online if preferred_backend in machine.agent_backends]
                candidates = backend_capable or online
                target_machine = self._best_machine_for_role(candidates, role, goal).machine_id
            elif assigned_machine in online_ids and self._task_can_move_from_online_worker(item, now) and online:
                role = str(item.get("role", "engineer")).strip() or "engineer"
                goal = str(item.get("goal", "")).strip()
                backend_capable = [machine for machine in online if preferred_backend in machine.agent_backends]
                candidates = backend_capable or online
                alternate_candidates = [
                    machine for machine in candidates if machine.machine_id != assigned_machine
                ]
                target_pool = alternate_candidates or candidates
                target_machine = self._best_machine_for_role(target_pool, role, goal).machine_id

            item["status"] = "delegated"
            item["original_machine"] = str(item.get("original_machine", "")).strip() or assigned_machine
            item["assigned_machine"] = target_machine
            item["claimed_by"] = ""
            item["lease_expires_at"] = None
            item["recovery_count"] = int(item.get("recovery_count", 0) or 0) + 1
            item["last_recovered_from"] = assigned_machine
            if target_machine == assigned_machine:
                item["progress_note"] = (
                    f"Lease expired on `{assigned_machine or 'unknown'}`; task is queued again for "
                    f"`{target_machine}`."
                )
            else:
                item["progress_note"] = (
                    f"Lease stayed quiet too long on `{assigned_machine or 'unknown'}`; task is reassigned to "
                    f"`{target_machine}`."
                )
            item["updated_at"] = now.isoformat()
            changed = True

        if changed:
            self._save(state)

    def _task_lease_is_expired(self, item: dict, now: datetime) -> bool:
        lease_text = str(item.get("lease_expires_at", "")).strip()
        if lease_text:
            try:
                return now >= datetime.fromisoformat(lease_text)
            except ValueError:
                return True
        updated_text = str(item.get("updated_at", "")).strip()
        if not updated_text:
            return True
        try:
            updated_at = datetime.fromisoformat(updated_text)
        except ValueError:
            return True
        return now - updated_at > self.task_lease

    def _task_can_move_from_online_worker(self, item: dict, now: datetime) -> bool:
        updated_text = str(item.get("updated_at", "")).strip()
        if not updated_text:
            return True
        try:
            updated_at = datetime.fromisoformat(updated_text)
        except ValueError:
            return True
        return now - updated_at > (self.task_lease * 2)

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
        local_reference = self._local_reference_index_for_role(role, lowered)
        if local_reference >= 0:
            for machine in machines:
                if machine.machine_id == self.machine_id:
                    return machine

        for machine in machines:
            mention_index = self._machine_mention_index(machine, lowered)
            if mention_index >= 0 and self._role_points_to_reference(role, lowered, mention_index):
                return machine
        return None

    def _machine_mention_index(self, machine: MachineNode, lowered_goal: str) -> int:
        normalized_goal = self._normalize_machine_id(lowered_goal)
        identifiers = unique_non_empty(
            [
                machine.machine_id.lower(),
                machine.hostname.lower(),
                self._normalize_machine_id(machine.machine_id),
                self._normalize_machine_id(machine.hostname),
            ]
        )
        for identifier in identifiers:
            if len(identifier) < 4:
                continue
            index = lowered_goal.find(identifier)
            if index >= 0:
                return index
        for identifier in identifiers:
            normalized = self._normalize_machine_id(identifier)
            if len(normalized) < 6:
                continue
            for size in (12, 10, 8, 6):
                prefix = normalized[:size]
                if len(prefix) >= 6 and prefix in normalized_goal:
                    return max(0, lowered_goal.find(prefix[:4]))
        return -1

    def _local_reference_index_for_role(self, role: str, lowered_goal: str) -> int:
        phrases = [
            "this machine",
            "this computer",
            "this pc",
            "current machine",
            "current computer",
            "local machine",
            "local computer",
            "on here",
            "run here",
            "handled here",
        ]
        for phrase in phrases:
            index = lowered_goal.find(phrase)
            if index >= 0 and self._role_points_to_reference(role, lowered_goal, index):
                return index
        return -1

    def _role_points_to_reference(self, role: str, lowered_goal: str, index: int) -> bool:
        before = lowered_goal[max(0, index - 140) : index]
        latest_role = self._latest_role_in_text(before)
        if latest_role:
            return latest_role == role

        after = lowered_goal[index : index + 90]
        earliest_role = self._earliest_role_in_text(after)
        return earliest_role == role

    def _latest_role_in_text(self, text: str) -> str:
        latest_role = ""
        latest_index = -1
        for role in self._known_roles():
            role_index = max(text.rfind(term) for term in self._role_terms(role))
            if role_index > latest_index:
                latest_role = role
                latest_index = role_index
        return latest_role

    def _earliest_role_in_text(self, text: str) -> str:
        earliest_role = ""
        earliest_index = len(text) + 1
        for role in self._known_roles():
            indexes = [text.find(term) for term in self._role_terms(role)]
            role_indexes = [index for index in indexes if index >= 0]
            if role_indexes and min(role_indexes) < earliest_index:
                earliest_role = role
                earliest_index = min(role_indexes)
        return earliest_role

    def _known_roles(self) -> list[str]:
        return ["coordinator", "researcher", "engineer", "backend", "frontend", "reviewer", "documenter"]

    def _role_terms(self, role: str) -> list[str]:
        return {
            "backend": ["backend", "back end", "api", "server", "database", "db"],
            "frontend": ["frontend", "front end", "ui", "ux", "page", "client"],
            "engineer": ["engineer", "implementation"],
            "researcher": ["research", "scout", "inspect", "context"],
            "reviewer": ["review", "test", "quality", "qa"],
            "documenter": ["docs", "document", "readme", "notes"],
            "coordinator": ["coordinate", "orchestrate", "plan", "lead"],
        }.get(role, [role])

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

    def _task_title(self, role: str, goal: str, brief: str = "") -> str:
        short_goal = (brief or goal).strip().splitlines()[0][:96]
        return f"{role.title()} pass: {short_goal}"

    def _task_brief(self, role: str, goal: str, brief: str = "") -> str:
        clean = " ".join(str(brief or "").split()).strip()
        if clean:
            return clean[:220]
        role_labels = {
            "coordinator": "Own orchestration, confirm assignments, and merge returned work.",
            "frontend": "Build the browser-facing UI, interaction flow, and visual polish for this goal.",
            "backend": "Build the API, server logic, data layer, and integration points for this goal.",
            "engineer": "Handle concrete implementation work and code-level changes needed for this goal.",
            "reviewer": "Review the current solution for bugs, regressions, and missing validation.",
            "documenter": "Write the handoff notes, README updates, and implementation summary.",
            "researcher": "Inspect constraints, references, and unknowns that affect implementation.",
        }
        goal_hint = " ".join(str(goal or "").strip().split())[:140]
        return f"{role_labels.get(role, 'Handle this assigned workstream.')}" + (f" Goal focus: {goal_hint}" if goal_hint else "")

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

    @contextmanager
    def _state_lock(self):
        if self.backend == "http":
            yield
            return

        lock_path = self.state_path.with_name(f"{self.state_path.name}.lock")
        deadline = time.monotonic() + 8.0
        acquired = False
        while not acquired:
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                lock_path.mkdir()
                acquired = True
            except FileExistsError:
                self._clear_stale_lock(lock_path)
                if time.monotonic() >= deadline:
                    raise CoordinationError(f"Timed out waiting for coordinator state lock `{lock_path}`.")
                time.sleep(0.025)

        try:
            yield
        finally:
            try:
                lock_path.rmdir()
            except OSError:
                pass

    def _clear_stale_lock(self, lock_path: Path) -> None:
        try:
            age_seconds = time.time() - lock_path.stat().st_mtime
        except OSError:
            return
        if age_seconds < 30:
            return
        try:
            lock_path.rmdir()
        except OSError:
            pass

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
        try:
            for attempt in range(12):
                try:
                    temp_path.replace(self.state_path)
                    return
                except OSError as exc:
                    if getattr(exc, "winerror", None) not in {5, 32} or attempt == 11:
                        raise
                    time.sleep(0.025 * (attempt + 1))
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

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
            brief=item.get("brief", ""),
            assigned_machine=item["assigned_machine"],
            preferred_backend=item.get("preferred_backend", SIMULATED_BACKEND),
            status=item["status"],
            created_at=datetime.fromisoformat(item["created_at"]),
            updated_at=datetime.fromisoformat(item["updated_at"]) if item.get("updated_at") else None,
            original_machine=item.get("original_machine", ""),
            claimed_by=item.get("claimed_by", ""),
            lease_expires_at=datetime.fromisoformat(item["lease_expires_at"]) if item.get("lease_expires_at") else None,
            recovery_count=int(item.get("recovery_count", 0) or 0),
            last_recovered_from=item.get("last_recovered_from", ""),
            progress_note=item.get("progress_note", ""),
            completed_by=item.get("completed_by", ""),
            completion_source=item.get("completion_source", ""),
            result=item.get("result", ""),
        )

    def _task_to_json(self, task: DelegatedTask) -> dict:
        payload = asdict(task)
        payload["created_at"] = task.created_at.isoformat()
        payload["updated_at"] = task.updated_at.isoformat() if task.updated_at else None
        payload["lease_expires_at"] = task.lease_expires_at.isoformat() if task.lease_expires_at else None
        return payload

    def _now_text(self) -> str:
        return datetime.now(UTC).isoformat()
