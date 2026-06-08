from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

UI_STATE_PATH = Path("./ui_state.json")
CHAT_HISTORY_LIMIT = 80


@dataclass(frozen=True)
class ChatRecord:
    role: str
    author: str
    content: str
    created_at: str


def load_ui_state(path: Path = UI_STATE_PATH) -> dict:
    if not path.exists():
        return {"preferences": {}, "chat": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"preferences": {}, "chat": []}
    if not isinstance(payload, dict):
        return {"preferences": {}, "chat": []}
    payload.setdefault("preferences", {})
    payload.setdefault("chat", [])
    return payload


def save_ui_state(payload: dict, path: Path = UI_STATE_PATH) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_preferences() -> dict[str, str]:
    preferences = load_ui_state().get("preferences", {})
    return {str(key): str(value) for key, value in preferences.items()}


def load_credentials(path: Path = UI_STATE_PATH) -> dict[str, dict[str, str]]:
    credentials = load_ui_state(path).get("credentials", {})
    if not isinstance(credentials, dict):
        return {}
    result = {}
    for backend, values in credentials.items():
        if isinstance(values, dict):
            result[str(backend)] = {str(key): str(value) for key, value in values.items()}
    return result


def save_preferences(values: dict[str, str]) -> None:
    payload = load_ui_state()
    preferences = payload.setdefault("preferences", {})
    preferences.update({key: value for key, value in values.items() if value is not None})
    save_ui_state(payload)


def save_credentials(backend: str, values: dict[str, str], path: Path = UI_STATE_PATH) -> None:
    payload = load_ui_state(path)
    credentials = payload.setdefault("credentials", {})
    backend_values = credentials.setdefault(backend, {})
    for key, value in values.items():
        clean = str(value or "").strip()
        if clean:
            backend_values[key] = clean
    save_ui_state(payload, path)


def append_chat(role: str, author: str, content: str) -> None:
    payload = load_ui_state()
    records = payload.setdefault("chat", [])
    records.append(
        asdict(
            ChatRecord(
                role=role,
                author=author,
                content=content,
                created_at=datetime.now(UTC).isoformat(),
            )
        )
    )
    payload["chat"] = records[-CHAT_HISTORY_LIMIT:]
    save_ui_state(payload)


def load_chat_history(limit: int = 12) -> list[ChatRecord]:
    records = load_ui_state().get("chat", [])[-limit:]
    return [
        ChatRecord(
            role=str(item.get("role", "assistant")),
            author=str(item.get("author", "Assistant")),
            content=str(item.get("content", "")),
            created_at=str(item.get("created_at", "")),
        )
        for item in records
        if isinstance(item, dict) and item.get("content")
    ]


def clear_chat_history() -> None:
    payload = load_ui_state()
    payload["chat"] = []
    save_ui_state(payload)
