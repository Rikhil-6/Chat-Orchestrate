from __future__ import annotations

from .backends import CLAUDE_CODE_BACKEND, CODEX_BACKEND, OPEN_SWARM_BACKEND, SIMULATED_BACKEND


BASE_CAPABILITIES = ["coordinator", "researcher", "engineer", "reviewer", "documenter"]

BACKEND_CAPABILITIES = {
    CODEX_BACKEND: ["engineer", "backend", "frontend", "reviewer", "documenter"],
    CLAUDE_CODE_BACKEND: ["coordinator", "researcher", "engineer", "frontend", "reviewer", "documenter"],
    OPEN_SWARM_BACKEND: ["coordinator", "researcher", "engineer", "backend", "frontend", "reviewer", "documenter"],
    SIMULATED_BACKEND: ["researcher", "reviewer", "documenter"],
}

ROLE_KEYWORDS = {
    "backend": ["backend", "back-end", "api", "server", "database", "auth", "endpoint"],
    "frontend": ["frontend", "front-end", "ui", "website", "page", "browser", "css", "react"],
    "researcher": ["research", "discover", "compare", "investigate", "explore"],
    "engineer": ["build", "code", "implement", "fix", "add", "launch", "make"],
    "reviewer": ["test", "review", "qa", "risk", "verify", "check"],
    "documenter": ["doc", "readme", "deploy", "handoff", "write up", "setup"],
}


def infer_goal_roles(goal: str, defaults: list[str] | None = None) -> list[str]:
    """Infer the run roles from the user's prompt."""
    lowered = goal.lower()
    roles = ["coordinator"]
    for role, keywords in ROLE_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            roles.append(role)

    fallback_roles = defaults or BASE_CAPABILITIES
    for role in fallback_roles:
        if role not in roles:
            roles.append(role)
    return unique(roles)


def infer_machine_capabilities(
    backends: list[str],
    selected_backend: str = "auto",
    defaults: list[str] | None = None,
    goal: str = "",
) -> list[str]:
    """Infer what a machine should advertise from selected agent, installed agents, and current goal."""
    normalized = unique([backend for backend in [selected_backend, *backends] if _usable_backend(backend)])
    if not normalized:
        normalized = [SIMULATED_BACKEND]

    capabilities = ["coordinator"]
    for backend in normalized:
        capabilities.extend(BACKEND_CAPABILITIES.get(backend, []))
    capabilities.extend(role for role in infer_goal_roles(goal, defaults) if role in {"backend", "frontend"})
    if defaults:
        capabilities.extend(defaults)
    return unique(capabilities)


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


def _usable_backend(backend: str) -> bool:
    return backend not in {"", "auto", "Select"}
