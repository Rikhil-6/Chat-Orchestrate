from datetime import UTC, datetime

from chat_orchestrate.models import DelegatedTask
from chat_orchestrate.summaries import summarize_goal


def test_summarize_goal_uses_role_assignments() -> None:
    now = datetime.now(UTC)
    tasks = [
        DelegatedTask("1", "run", "default", "", "frontend", "Frontend pass", "sg-akc-dt330", "codex", "delegated", now),
        DelegatedTask("2", "run", "default", "", "backend", "Backend pass", "laptop-es9pcb92", "codex", "completed", now),
        DelegatedTask("3", "run", "default", "", "coordinator", "Plan", "sg-akc-dt330", "codex", "completed", now),
    ]

    summary = summarize_goal(
        "orite >> task is to create a simple website that mimics google's page with the "
        "frontend code being handled by this computer and the backend code being handled by laptop-es9pcb92",
        tasks,
    )

    assert summary == "Google-like search site: frontend on sg-akc-dt330; backend on laptop-es9pcb92"
