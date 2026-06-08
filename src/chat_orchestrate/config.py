import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from .runtime_config import apply_runtime_env

apply_runtime_env(os.environ)


class Settings(BaseSettings):
    """Runtime settings loaded from environment and .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    use_open_swarm: bool = False
    open_swarm_base_url: str = "http://localhost:8000"
    open_swarm_api_key: str = ""
    open_swarm_model: str = "codey"
    open_swarm_timeout_seconds: int = 90
    openai_api_key: str = ""
    codex_api_model: str = "gpt-5.3-codex"

    workspaces_root: Path = Path("./workspaces")
    workspace_state_path: Path = Path("./workspace_state.json")
    coordination_state_path: Path = Path("./coordination_state.json")
    coordination_backend: str = "file"
    coordination_http_url: str = ""
    coordination_http_urls: str = ""
    cluster_id: str = "local"
    coordination_token: str = ""
    coordinator_auto_host: bool = False
    coordinator_host: str = "127.0.0.1"
    coordinator_port: int = 8765
    machine_id: str = ""
    agent_backends: str = "auto"
    orchestrator_ttl_seconds: int = 120
    worker_poll_seconds: float = 5.0
    worker_dry_run: bool = True
    use_local_agent_chat: bool = True
    local_agent_timeout_seconds: int = 180
    codex_command: str = ""
    claude_command: str = ""
    gemini_command: str = ""
    delegated_task_wait_seconds: float = 90.0
    default_agent_set: str = "coordinator,researcher,engineer,reviewer,documenter"

    @property
    def default_agents(self) -> list[str]:
        return [agent.strip() for agent in self.default_agent_set.split(",") if agent.strip()]

    @property
    def configured_backends(self) -> list[str]:
        return [backend.strip() for backend in self.agent_backends.split(",") if backend.strip()]

    @property
    def command_overrides(self) -> dict[str, str]:
        return {
            "codex": self.codex_command.strip(),
            "claude-code": self.claude_command.strip(),
            "gemini-cli": self.gemini_command.strip(),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
