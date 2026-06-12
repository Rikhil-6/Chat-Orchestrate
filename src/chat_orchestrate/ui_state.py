from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

UI_STATE_PATH = Path("./ui_state.json")
CHAT_HISTORY_LIMIT = 80
CHAT_THREAD_LIMIT = 40


@dataclass(frozen=True)
class ChatRecord:
    role: str
    author: str
    content: str
    created_at: str


@dataclass(frozen=True)
class ChatThreadSummary:
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    archived: bool = False
    preview: str = ""
    project_name: str = ""


def load_ui_state(path: Path = UI_STATE_PATH) -> dict:
    if not path.exists():
        return _empty_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _empty_state()
    if not isinstance(payload, dict):
        return _empty_state()
    payload.setdefault("preferences", {})
    payload.setdefault("chat", [])
    payload.setdefault("credentials", {})
    payload["chat_threads"] = _normalized_chat_threads(payload.get("chat_threads"), payload.get("chat", []))
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


def active_chat_id(path: Path = UI_STATE_PATH) -> str:
    payload = load_ui_state(path)
    thread = _ensure_active_thread(payload)
    save_ui_state(payload, path)
    return str(thread["id"])


def create_chat_thread(title: str = "", path: Path = UI_STATE_PATH, project_name: str = "") -> ChatThreadSummary:
    payload = load_ui_state(path)
    thread = _new_thread(title or f"New chat {datetime.now().astimezone().strftime('%H:%M')}", project_name)
    chat_threads = payload.setdefault("chat_threads", {"active_id": "", "threads": []})
    threads = chat_threads.setdefault("threads", [])
    threads.append(thread)
    chat_threads["active_id"] = thread["id"]
    _trim_threads(chat_threads)
    save_ui_state(payload, path)
    return _thread_summary(thread)


def set_active_chat_thread(chat_id: str, path: Path = UI_STATE_PATH) -> ChatThreadSummary | None:
    payload = load_ui_state(path)
    chat_threads = payload.setdefault("chat_threads", {"active_id": "", "threads": []})
    thread = _find_thread(chat_threads, chat_id)
    if thread is None or thread.get("archived"):
        return None
    chat_threads["active_id"] = str(thread["id"])
    payload["chat"] = list(thread.get("messages", []))[-CHAT_HISTORY_LIMIT:]
    save_ui_state(payload, path)
    return _thread_summary(thread)


def rename_chat_thread(chat_id: str, title: str, path: Path = UI_STATE_PATH) -> ChatThreadSummary | None:
    payload = load_ui_state(path)
    chat_threads = payload.setdefault("chat_threads", {"active_id": "", "threads": []})
    thread = _find_thread(chat_threads, chat_id)
    clean_title = _title_from_content(title, limit=52)
    if thread is None or not clean_title:
        return None
    thread["title"] = clean_title
    thread["updated_at"] = datetime.now(UTC).isoformat()
    save_ui_state(payload, path)
    return _thread_summary(thread)


def set_chat_thread_project(chat_id: str, project_name: str, path: Path = UI_STATE_PATH) -> ChatThreadSummary | None:
    payload = load_ui_state(path)
    chat_threads = payload.setdefault("chat_threads", {"active_id": "", "threads": []})
    thread = _find_thread(chat_threads, chat_id)
    clean_project = _clean_project_name(project_name)
    if thread is None or not clean_project:
        return None
    thread["project_name"] = clean_project
    thread["updated_at"] = datetime.now(UTC).isoformat()
    save_ui_state(payload, path)
    return _thread_summary(thread)


def archive_chat_thread(chat_id: str | None = None, path: Path = UI_STATE_PATH) -> ChatThreadSummary:
    payload = load_ui_state(path)
    chat_threads = payload.setdefault("chat_threads", {"active_id": "", "threads": []})
    active_thread = _ensure_active_thread(payload)
    target_id = chat_id or str(active_thread["id"])
    target = _find_thread(chat_threads, target_id) or active_thread
    was_active = str(target.get("id")) == str(chat_threads.get("active_id", ""))
    now = datetime.now(UTC).isoformat()
    target["archived"] = True
    target["archived_at"] = now
    target["updated_at"] = now

    if was_active:
        replacement = _latest_unarchived_thread(chat_threads, exclude_id=str(target["id"]))
        if replacement is None:
            replacement = _new_thread()
            chat_threads.setdefault("threads", []).append(replacement)
        chat_threads["active_id"] = str(replacement["id"])
        payload["chat"] = list(replacement.get("messages", []))[-CHAT_HISTORY_LIMIT:]
    _trim_threads(chat_threads)
    save_ui_state(payload, path)
    return _thread_summary(target)


def list_chat_threads(include_archived: bool = False, limit: int = 12, path: Path = UI_STATE_PATH) -> list[ChatThreadSummary]:
    payload = load_ui_state(path)
    chat_threads = payload.setdefault("chat_threads", {"active_id": "", "threads": []})
    _ensure_active_thread(payload)
    return _thread_summaries(chat_threads, include_archived, limit)


def chat_thread_state(path: Path = UI_STATE_PATH) -> dict:
    payload = load_ui_state(path)
    active = _ensure_active_thread(payload)
    chat_threads = payload.setdefault("chat_threads", {"active_id": "", "threads": []})
    threads = _thread_summaries(chat_threads, include_archived=False, limit=CHAT_THREAD_LIMIT)
    archived_count = sum(1 for thread in chat_threads.get("threads", []) if thread.get("archived"))
    save_ui_state(payload, path)
    return {
        "active_id": str(active["id"]),
        "active_title": _thread_summary(active).title,
        "active_project_name": str(active.get("project_name", "")),
        "threads": [asdict(thread) for thread in threads],
        "archived_count": archived_count,
    }


def append_chat(role: str, author: str, content: str, path: Path = UI_STATE_PATH) -> None:
    payload = load_ui_state(path)
    thread = _ensure_active_thread(payload)
    records = thread.setdefault("messages", [])
    clean_content = re_space(str(content or ""))
    if _is_duplicate_tail_record(records, role, author, clean_content):
        records[-1]["created_at"] = datetime.now(UTC).isoformat()
        thread["updated_at"] = records[-1]["created_at"]
        payload["chat"] = list(records)[-CHAT_HISTORY_LIMIT:]
        save_ui_state(payload, path)
        return
    record = asdict(
        ChatRecord(
            role=role,
            author=author,
            content=content,
            created_at=datetime.now(UTC).isoformat(),
        )
    )
    records.append(record)
    thread["messages"] = records[-CHAT_HISTORY_LIMIT:]
    thread["updated_at"] = record["created_at"]
    if role == "user" and _is_placeholder_title(str(thread.get("title", ""))):
        thread["title"] = _title_from_content(content)
    payload["chat"] = list(thread["messages"])[-CHAT_HISTORY_LIMIT:]
    save_ui_state(payload, path)


def load_chat_history(limit: int = 12, chat_id: str | None = None, path: Path = UI_STATE_PATH) -> list[ChatRecord]:
    payload = load_ui_state(path)
    chat_threads = payload.setdefault("chat_threads", {"active_id": "", "threads": []})
    thread = _find_thread(chat_threads, chat_id) if chat_id else _ensure_active_thread(payload)
    if thread is None:
        return []
    records = list(thread.get("messages", []))[-limit:]
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


def clear_chat_history(chat_id: str | None = None, path: Path = UI_STATE_PATH) -> None:
    payload = load_ui_state(path)
    chat_threads = payload.setdefault("chat_threads", {"active_id": "", "threads": []})
    thread = _find_thread(chat_threads, chat_id) if chat_id else _ensure_active_thread(payload)
    if thread is None:
        return
    now = datetime.now(UTC).isoformat()
    thread["messages"] = []
    thread["updated_at"] = now
    if not thread.get("title"):
        thread["title"] = "New chat"
    if str(thread.get("id")) == str(chat_threads.get("active_id", "")):
        payload["chat"] = []
    save_ui_state(payload, path)


def _empty_state() -> dict:
    return {
        "preferences": {},
        "credentials": {},
        "chat": [],
        "chat_threads": {"active_id": "", "threads": []},
    }


def _normalized_chat_threads(raw_threads: object, legacy_chat: object) -> dict:
    chat_threads = raw_threads if isinstance(raw_threads, dict) else {}
    raw_thread_items = chat_threads.get("threads", [])
    threads = []
    if isinstance(raw_thread_items, list):
        for item in raw_thread_items:
            if isinstance(item, dict):
                threads.append(_normalize_thread(item))

    if not threads and isinstance(legacy_chat, list) and legacy_chat:
        imported = _new_thread("Imported chat")
        imported["id"] = "chat-imported"
        imported["messages"] = [_normalize_record(item) for item in legacy_chat if isinstance(item, dict)][-CHAT_HISTORY_LIMIT:]
        imported["title"] = _title_from_messages(imported["messages"]) or "Imported chat"
        if imported["messages"]:
            imported["updated_at"] = imported["messages"][-1].get("created_at") or imported["created_at"]
        threads.append(imported)

    active_id = str(chat_threads.get("active_id", ""))
    if not any(str(thread.get("id")) == active_id for thread in threads):
        active = _latest_unarchived_thread({"threads": threads})
        active_id = str(active["id"]) if active else ""
    return {"active_id": active_id, "threads": threads}


def _new_thread(title: str = "New chat", project_name: str = "") -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "id": f"chat-{uuid4().hex[:12]}",
        "title": title,
        "project_name": _clean_project_name(project_name),
        "created_at": now,
        "updated_at": now,
        "archived": False,
        "messages": [],
    }


def _normalize_thread(item: dict) -> dict:
    thread = _new_thread(str(item.get("title", "") or "New chat"))
    thread["id"] = str(item.get("id") or thread["id"])
    thread["created_at"] = str(item.get("created_at") or thread["created_at"])
    thread["updated_at"] = str(item.get("updated_at") or thread["created_at"])
    thread["project_name"] = _clean_project_name(str(item.get("project_name", "")))
    thread["archived"] = bool(item.get("archived", False))
    if item.get("archived_at"):
        thread["archived_at"] = str(item.get("archived_at"))
    messages = item.get("messages", [])
    if isinstance(messages, list):
        thread["messages"] = [_normalize_record(record) for record in messages if isinstance(record, dict)][-CHAT_HISTORY_LIMIT:]
    if _is_low_quality_title(str(thread.get("title", ""))) and thread["messages"]:
        thread["title"] = _title_from_messages(thread["messages"]) or "New chat"
    return thread


def _normalize_record(item: dict) -> dict:
    return asdict(
        ChatRecord(
            role=str(item.get("role", "assistant")),
            author=str(item.get("author", "Assistant")),
            content=str(item.get("content", "")),
            created_at=str(item.get("created_at") or datetime.now(UTC).isoformat()),
        )
    )


def _ensure_active_thread(payload: dict) -> dict:
    chat_threads = payload.setdefault("chat_threads", {"active_id": "", "threads": []})
    threads = chat_threads.setdefault("threads", [])
    active = _find_thread(chat_threads, str(chat_threads.get("active_id", "")))
    if active is None or active.get("archived"):
        active = _latest_unarchived_thread(chat_threads)
    if active is None:
        active = _new_thread()
        threads.append(active)
    chat_threads["active_id"] = str(active["id"])
    return active


def _find_thread(chat_threads: dict, chat_id: str | None) -> dict | None:
    if not chat_id:
        return None
    for thread in chat_threads.get("threads", []):
        if str(thread.get("id")) == str(chat_id):
            return thread
    return None


def _latest_unarchived_thread(chat_threads: dict, exclude_id: str = "") -> dict | None:
    threads = [
        thread
        for thread in chat_threads.get("threads", [])
        if not thread.get("archived") and str(thread.get("id")) != exclude_id
    ]
    if not threads:
        return None
    threads.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return threads[0]


def _trim_threads(chat_threads: dict) -> None:
    threads = chat_threads.get("threads", [])
    archived = [thread for thread in threads if thread.get("archived")]
    live = [thread for thread in threads if not thread.get("archived")]
    archived.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    chat_threads["threads"] = live + archived[:CHAT_THREAD_LIMIT]


def _thread_summaries(chat_threads: dict, include_archived: bool, limit: int) -> list[ChatThreadSummary]:
    threads = [
        thread
        for thread in chat_threads.get("threads", [])
        if include_archived or not thread.get("archived")
    ]
    threads.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return [_thread_summary(thread) for thread in threads[:limit]]


def _thread_summary(thread: dict) -> ChatThreadSummary:
    messages = [item for item in thread.get("messages", []) if isinstance(item, dict) and item.get("content")]
    title = str(thread.get("title") or "").strip() or _title_from_messages(messages) or "New chat"
    preview = str(messages[-1].get("content", "")) if messages else ""
    return ChatThreadSummary(
        id=str(thread.get("id", "")),
        title=title,
        created_at=str(thread.get("created_at", "")),
        updated_at=str(thread.get("updated_at", "")),
        message_count=len(messages),
        archived=bool(thread.get("archived", False)),
        preview=_title_from_content(preview, limit=80) if preview else "",
        project_name=str(thread.get("project_name", "")),
    )


def _is_duplicate_tail_record(records: list[dict], role: str, author: str, content: str) -> bool:
    if not records:
        return False
    previous = records[-1]
    return (
        str(previous.get("role", "")) == str(role)
        and str(previous.get("author", "")) == str(author)
        and re_space(str(previous.get("content", ""))) == content
    )


def _title_from_messages(messages: list[dict]) -> str:
    for item in messages:
        if str(item.get("role", "")) == "user" and item.get("content"):
            return _title_from_content(str(item.get("content", "")))
    for item in messages:
        if item.get("content"):
            return _title_from_content(str(item.get("content", "")))
    return ""


def _title_from_content(content: str, limit: int = 52) -> str:
    text = re_space(" ".join(str(content).split()))
    text = _clean_title_prefixes(text.strip(" #`*_>"))
    if not text:
        return "New chat"
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _is_placeholder_title(title: str) -> bool:
    clean = title.strip().lower()
    return not clean or clean == "new chat" or clean.startswith("new chat ")


def _is_low_quality_title(title: str) -> bool:
    clean = title.strip()
    return _is_placeholder_title(clean) or bool(re.match(r"^[;:,.>\-]+", clean))


def _clean_title_prefixes(text: str) -> str:
    clean = text.strip(" ;:,.>-")
    leading_patterns = [
        r"^(?:orite|alright|right-o|right|ok|okay|so|again|then|yo|hey|m8|mate|pls|please)\b[\s>:,;-]*",
        r"^(?:task|plan|goal)\s+is\s+to\s+",
        r"^(?:i\s+want(?:na)?|i\s+wanna|i\s+need|let'?s)\s+(?:to\s+)?",
    ]
    previous = None
    while previous != clean:
        previous = clean
        for pattern in leading_patterns:
            clean = re.sub(pattern, "", clean, flags=re.I).strip(" ;:,.>-")
    return clean


def _clean_project_name(name: str) -> str:
    clean = str(name or "").strip().lower().replace(" ", "-")
    if any(char in clean for char in "\\/:*?\"<>|"):
        return ""
    return clean


def re_space(text: str) -> str:
    return " ".join(text.replace("\n", " ").replace("\r", " ").split())
