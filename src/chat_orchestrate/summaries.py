from __future__ import annotations

import re
from collections.abc import Iterable


ROLE_ORDER = ["frontend", "backend", "engineer", "reviewer", "documenter", "researcher", "coordinator"]
PRIMARY_ROLE_ORDER = ["frontend", "backend"]


def summarize_goal(goal: str, tasks: Iterable[object] | None = None, max_length: int = 90) -> str:
    subject = summarize_goal_subject(goal)
    assignments = summarize_assignments(tasks or [])
    if assignments:
        return shorten_summary(f"{subject} - {assignments}", max_length)
    return shorten_summary(subject, max_length)


def summarize_goal_subject(goal: str) -> str:
    clean = clean_goal_text(goal)
    lowered = clean.lower()
    if "github" in lowered and any(term in lowered for term in ["website", "page", "site", "app"]):
        return "GitHub-like site"
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
    shown: set[str] = set()
    parts: list[str] = []
    primary_roles = [role for role in PRIMARY_ROLE_ORDER if role in by_role]
    if (
        len(primary_roles) == len(PRIMARY_ROLE_ORDER)
        and by_role["frontend"] == by_role["backend"]
    ):
        parts.append(f"frontend/backend: {by_role['frontend']}")
        shown.update(primary_roles)
    else:
        for role in primary_roles:
            parts.append(f"{role}: {by_role[role]}")
            shown.add(role)

    if not parts:
        for role in ROLE_ORDER:
            if role in by_role and role != "coordinator":
                parts.append(f"{role}: {by_role[role]}")
                shown.add(role)
            if len(parts) >= 2:
                break
    if not parts and "coordinator" in by_role:
        parts.append(f"coordinator: {by_role['coordinator']}")
        shown.add("coordinator")

    extra_roles = [
        role for role in ROLE_ORDER
        if role in by_role and role not in shown and role != "coordinator"
    ]
    if extra_roles:
        label = "role" if len(extra_roles) == 1 else "roles"
        parts.append(f"+{len(extra_roles)} {label}")
    return "; ".join(parts)


def clean_goal_text(goal: str) -> str:
    clean = re.sub(r"\s+", " ", str(goal or "")).strip(" -")
    leading_patterns = [
        r"^(?:orite|alright|right-o|right|ok|okay|so|again|then|yo|hey|m8|mate|pls|please)\b[\s>:,-]*",
        r"^>{1,3}\s*",
        r"^(?:task|plan|goal)\s+is\s+to\s+",
        r"^(?:i\s+want(?:na)?|i\s+need|let'?s)\s+(?:to\s+)?",
    ]
    previous = None
    while previous != clean:
        previous = clean
        for pattern in leading_patterns:
            clean = re.sub(pattern, "", clean, flags=re.I).strip()
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
