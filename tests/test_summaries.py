from datetime import UTC, datetime

from chat_orchestrate.models import DelegatedTask
from chat_orchestrate.summaries import summarize_goal


def test_summarize_goal_uses_role_assignments() -> None:
    now = datetime.now(UTC)
    tasks = [
        DelegatedTask(
            "1", "run", "default", "", "frontend", "Frontend pass",
            "sg-akc-dt330", "codex", "delegated", now,
        ),
        DelegatedTask(
            "2", "run", "default", "", "backend", "Backend pass",
            "laptop-es9pcb92", "codex", "completed", now,
        ),
        DelegatedTask(
            "3", "run", "default", "", "coordinator", "Plan",
            "sg-akc-dt330", "codex", "completed", now,
        ),
    ]

    summary = summarize_goal(
        "orite >> task is to create a simple website that mimics google's page with the "
        "frontend code being handled by this computer and the backend code being handled by laptop-es9pcb92",
        tasks,
    )

    assert summary == "Google-like search site - frontend: sg-akc-dt330; backend: laptop-es9pcb92"


def test_summarize_goal_keeps_sidebar_copy_short() -> None:
    now = datetime.now(UTC)
    tasks = [
        DelegatedTask(
            "1", "run", "default", "", "frontend", "Frontend pass",
            "sg-akc-dt330", "codex", "delegated", now,
        ),
        DelegatedTask(
            "2", "run", "default", "", "backend", "Backend pass",
            "sg-akc-dt330", "codex", "delegated", now,
        ),
        DelegatedTask(
            "3", "run", "default", "", "engineer", "Engineer pass",
            "sg-akc-dt330", "codex", "delegated", now,
        ),
        DelegatedTask(
            "4", "run", "default", "", "coordinator", "Plan",
            "sg-akc-dt330", "codex", "completed", now,
        ),
    ]

    summary = summarize_goal(
        "so >> again i want codex on this machine to plan the frontend and backend out "
        "for a mock github website",
        tasks,
    )

    assert summary == "GitHub-like site - frontend/backend: sg-akc-dt330; +1 role"
    assert len(summary) <= 90
