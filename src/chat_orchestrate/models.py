from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class AgentSpec:
    name: str
    role: str
    instructions: str


@dataclass(frozen=True)
class ProjectSpace:
    name: str
    path: Path
    mode: str = "local"
    git_remote: str | None = None
    branch: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class AgentTurn:
    agent: str
    role: str
    content: str
    assigned_machine: str | None = None
    preferred_backend: str | None = None


@dataclass(frozen=True)
class ProgressUpdate:
    message: str
    phase: str = "working"
    agent: str | None = None
    role: str | None = None
    assigned_machine: str | None = None
    preferred_backend: str | None = None
    task_id: str | None = None
    elapsed_seconds: int = 0


@dataclass(frozen=True)
class MachineNode:
    machine_id: str
    hostname: str
    role: str
    status: str
    capabilities: list[str]
    agent_backends: list[str]
    last_seen: datetime


@dataclass(frozen=True)
class DelegatedTask:
    task_id: str
    run_id: str
    project: str
    goal: str
    role: str
    title: str
    assigned_machine: str
    preferred_backend: str
    status: str
    created_at: datetime
    brief: str = ""
    updated_at: datetime | None = None
    original_machine: str = ""
    claimed_by: str = ""
    lease_expires_at: datetime | None = None
    recovery_count: int = 0
    last_recovered_from: str = ""
    progress_note: str = ""
    completed_by: str = ""
    completion_source: str = ""
    result: str = ""


@dataclass
class OrchestrationRun:
    goal: str
    project: ProjectSpace
    run_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    orchestrator_machine: str | None = None
    goal_summary: str = ""
    delegated_tasks: list[DelegatedTask] = field(default_factory=list)
    turns: list[AgentTurn] = field(default_factory=list)
    final: str = ""
