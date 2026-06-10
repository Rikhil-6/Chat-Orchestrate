from __future__ import annotations

import re
from collections.abc import Iterable


ROLE_ORDER = ["frontend", "backend", "engineer", "reviewer", "documenter", "researcher", "coordinator"]


def summarize_goal(goal: str, tasks: Iterable[object] | None = None, max_length: int = 120) -> str:
    subject = summarize_goal_subject(goal)
    assignments = summarize_assignments(tasks or [])
    if assignments:
        return shorten_summary(f"{subject}: {assignments}", max_length)
    return shorten_summary(subject, max_length)


def summarize_goal_subject(goal: str) -> str:
    clean = clean_goal_text(goal)
    lowered = clean.lower()
    if "google" in lowered and any(term in lowered for term in ["website", "page", "search"]):
        return "Google-like search site"
    if "search" in lowered and any(term in lowered for term in ["website", "app", "page"]):
        return "Search app"
    if "dashboard" in lowered:
        return "Dashboard update"
    if "website" in lowered:
        return title_case_fragment(_before_role_clause(clean) or "Website build")
    if "api" in lowered or "backend" in lowered:
        return title_case_fragment(_before_role_clause(clean) or "Backend/API work")
    return sentence_case(_before_role_clause(clean) or clean or "Project task")


def summarize_assignments(tasks: Iterable[object]) -> str:
    by_role: dict[str, str] = {}
    for task in tasks:
        if isinstance(task, dict):
            role_value = task.get("role", "")
            machine_value = task.get("assigned_machine", "") or task.get("machine", "")
        else:
            role_value = getattr(task, "role", "")
            machine_value = getattr(task, "assigned_machine", "") or getattr(task, "machine", "")
        role = str(role_value).strip().lower()
        machine = str(machine_value).strip()
        if role and machine and role not in by_role:
            by_role[role] = machine
    parts = [f"{role} on {by_role[role]}" for role in ROLE_ORDER if role in by_role and role != "coordinator"]
    return "; ".join(parts[:3])


def clean_goal_text(goal: str) -> str:
    clean = re.sub(r"\s+", " ", str(goal or "")).strip(" -")
    clean = re.sub(r"^(?:orite|alright|right|ok|okay|so|yo|hey|m8|mate|pls|please)\b[\s>:,-]*", "", clean, flags=re.I)
    clean = re.sub(r"^(?:task|plan|goal)\s+is\s+to\s+", "", clean, flags=re.I)
    clean = re.sub(r"^(?:i\s+want(?:na)?|i\s+need|let'?s)\s+(?:to\s+)?", "", clean, flags=re.I)
    clean = re.sub(r"^>{1,3}\s*", "", clean)
    return clean.strip()


def _before_role_clause(text: str) -> str:
    before = re.split(
        r"\b(?:with|where|and)\s+(?:the\s+)?(?:frontend|backend)\b|"
        r"\b(?:frontend|backend)\s+(?:code\s+)?(?:being\s+)?handled\b",
        text,
        maxsplit=1,
        flags=re.I,
    )[0]
    before = re.sub(r"^(?:create|build|make|have|implement)\s+(?:a\s+)?", "", before, flags=re.I).strip()
    return before


def shorten_summary(value: str, max_length: int) -> str:
    clean = re.sub(r"\s+", " ", value).strip()
    if len(clean) <= max_length:
        return clean
    shortened = clean[: max_length - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{shortened}..."


def sentence_case(value: str) -> str:
    clean = value.strip()
    if not clean:
        return clean
    return clean[0].upper() + clean[1:]


def title_case_fragment(value: str) -> str:
    clean = value.strip()
    if not clean:
        return clean
    if len(clean.split()) <= 4:
        return clean.title()
    return sentence_case(clean)
