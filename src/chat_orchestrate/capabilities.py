from __future__ import annotations

ROLE_KEYWORDS = {
    "backend": ["backend", "back-end", "api", "server", "database", "auth", "endpoint"],
    "frontend": ["frontend", "front-end", "ui", "website", "page", "browser", "css", "react"],
    "researcher": ["research", "discover", "compare", "investigate", "explore"],
    "engineer": ["build", "code", "implement", "fix", "add", "launch", "make"],
    "reviewer": ["test", "review", "qa", "risk", "verify", "check"],
    "documenter": ["doc", "readme", "deploy", "handoff", "write up", "setup"],
}


def infer_goal_roles(goal: str, defaults: list[str] | None = None) -> list[str]:
    """Infer the run roles from the user's prompt.

    Defaults describe the local agent library, not a mandatory run plan. Keep
    the run plan narrow until the user's task asks for more workstreams.
    """
    del defaults
    lowered = goal.lower().strip()
    roles = ["coordinator"]
    for role, keywords in ROLE_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            roles.append(role)

    if any(role in roles for role in {"backend", "frontend"}) and _looks_like_work_request(lowered):
        roles.append("engineer")
    return unique(roles)


def infer_machine_capabilities(
    backends: list[str],
    selected_backend: str = "auto",
    defaults: list[str] | None = None,
    goal: str = "",
) -> list[str]:
    """Infer what a machine should advertise from selected agent, installed agents, and current goal."""
    if not goal.strip():
        return []

    goal_roles = infer_goal_roles(goal)
    return unique(goal_roles)


def capability_policy_summary(backends: list[str], selected_backend: str, goal: str) -> list[str]:
    roles = infer_goal_roles(goal)
    capabilities = infer_machine_capabilities(backends, selected_backend, goal=goal)
    return [
        f"agent={selected_backend or 'auto'}",
        f"goal_roles={','.join(roles[:4])}",
        f"advertised={','.join(capabilities[:6])}",
    ]


def unique(items: list[str]) -> list[str]:
    result = []
    for item in items:
        clean = item.strip()
        if clean and clean not in result:
            result.append(clean)
    return result


def _looks_like_work_request(goal: str) -> bool:
    if not goal:
        return False
    work_terms = {
        "add",
        "build",
        "code",
        "create",
        "delegate",
        "develop",
        "fix",
        "implement",
        "make",
        "setup",
        "work",
    }
    return any(term in goal for term in work_terms)
