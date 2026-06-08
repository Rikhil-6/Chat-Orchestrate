from chat_orchestrate.backends import CLAUDE_CODE_BACKEND, CODEX_BACKEND, SIMULATED_BACKEND
from chat_orchestrate.capabilities import infer_goal_roles, infer_machine_capabilities


def test_goal_roles_are_inferred_from_prompt() -> None:
    roles = infer_goal_roles(
        "have this machine work on a backend API and delegate the frontend website to desktop-p4k08ab"
    )

    assert roles.index("backend") < roles.index("frontend")
    assert "engineer" in roles


def test_simple_chat_does_not_expand_to_default_agent_lineup() -> None:
    assert infer_goal_roles("hello") == ["coordinator"]
    assert infer_machine_capabilities([CODEX_BACKEND], CODEX_BACKEND) == []


def test_codex_machine_gets_goal_specific_capabilities() -> None:
    capabilities = infer_machine_capabilities(
        [CODEX_BACKEND],
        CODEX_BACKEND,
        goal="build backend API routes and review the website",
    )

    assert "backend" in capabilities
    assert "reviewer" in capabilities
    assert "frontend" in capabilities


def test_claude_and_simulated_capabilities_are_distinct() -> None:
    claude = infer_machine_capabilities([CLAUDE_CODE_BACKEND], CLAUDE_CODE_BACKEND, goal="build a frontend")
    simulated = infer_machine_capabilities([SIMULATED_BACKEND], SIMULATED_BACKEND, goal="review the docs")

    assert "frontend" in claude
    assert "documenter" in simulated
