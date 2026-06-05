from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RUNTIME_CONFIG_PATH = Path("./runtime_config.json")

ALLOWED_KEYS = {
    "COORDINATION_BACKEND",
    "COORDINATION_HTTP_URL",
    "COORDINATION_HTTP_URLS",
    "CLUSTER_ID",
    "COORDINATION_TOKEN",
    "COORDINATOR_AUTO_HOST",
    "COORDINATOR_HOST",
    "COORDINATOR_PORT",
    "MACHINE_ID",
    "AGENT_BACKENDS",
    "CODEX_COMMAND",
    "CLAUDE_COMMAND",
}


def load_runtime_env(path: Path = RUNTIME_CONFIG_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in payload.items()
        if key in ALLOWED_KEYS and value is not None
    }


def save_runtime_env(values: dict[str, str], path: Path = RUNTIME_CONFIG_PATH) -> None:
    current = load_runtime_env(path)
    for key, value in values.items():
        if key not in ALLOWED_KEYS:
            continue
        clean = value.strip()
        if clean:
            current[key] = clean
        else:
            current.pop(key, None)
    path.write_text(json.dumps(current, indent=2), encoding="utf-8")


def clear_runtime_env(path: Path = RUNTIME_CONFIG_PATH) -> None:
    if path.exists():
        path.unlink()


def clear_runtime_process_env(environ: Any) -> None:
    for key in ALLOWED_KEYS:
        environ.pop(key, None)


def apply_runtime_env(
    environ: Any,
    path: Path = RUNTIME_CONFIG_PATH,
    *,
    override: bool = False,
) -> None:
    for key, value in load_runtime_env(path).items():
        if override:
            environ[key] = value
        else:
            environ.setdefault(key, value)
