from pathlib import Path

from chat_orchestrate.ui_state import (
    append_chat,
    archive_chat_thread,
    chat_thread_state,
    create_chat_thread,
    load_chat_history,
    load_credentials,
    rename_chat_thread,
    save_credentials,
    set_chat_thread_project,
    set_active_chat_thread,
)


def test_credentials_are_saved_by_backend(tmp_path: Path) -> None:
    path = tmp_path / "ui_state.json"

    save_credentials("codex", {"openai_api_key": "sk-test", "codex_command": "codex"}, path)

    assert load_credentials(path)["codex"]["openai_api_key"] == "sk-test"
    assert load_credentials(path)["codex"]["codex_command"] == "codex"


def test_chat_threads_are_isolated_and_archivable(tmp_path):
    state_path = tmp_path / "ui_state.json"

    initial = chat_thread_state(state_path)
    first_id = initial["active_id"]
    append_chat("user", "You", "build a tiny app", state_path)
    append_chat("assistant", "Assistant", "on it", state_path)

    assert chat_thread_state(state_path)["threads"][0]["title"] == "build a tiny app"

    second_id = create_chat_thread("Second", state_path).id
    append_chat("user", "You", "second message", state_path)
    assert [record.content for record in load_chat_history(path=state_path)] == ["second message"]

    assert set_active_chat_thread(first_id, state_path).id == first_id
    assert [record.content for record in load_chat_history(path=state_path)] == [
        "build a tiny app",
        "on it",
    ]

    archived = archive_chat_thread(first_id, state_path)
    state = chat_thread_state(state_path)
    assert archived.id == first_id
    assert state["active_id"] == second_id
    assert state["archived_count"] == 1


def test_chat_threads_track_project_names(tmp_path):
    state_path = tmp_path / "ui_state.json"

    first = create_chat_thread("First", state_path, project_name="chat-a1b2")
    second = create_chat_thread("Second", state_path, project_name="chat-c3d4")

    assert chat_thread_state(state_path)["active_project_name"] == "chat-c3d4"
    assert set_active_chat_thread(first.id, state_path).project_name == "chat-a1b2"
    assert chat_thread_state(state_path)["active_project_name"] == "chat-a1b2"

    set_chat_thread_project(first.id, "renamed-project", state_path)
    assert chat_thread_state(state_path)["active_project_name"] == "renamed-project"

    archive_chat_thread(first.id, state_path)
    state = chat_thread_state(state_path)
    assert state["active_id"] == second.id
    assert state["active_project_name"] == "chat-c3d4"


def test_chat_thread_can_be_renamed(tmp_path):
    state_path = tmp_path / "ui_state.json"
    thread = create_chat_thread(path=state_path)

    renamed = rename_chat_thread(thread.id, "GitHub-like site", state_path)

    assert renamed is not None
    assert renamed.title == "GitHub-like site"
    assert chat_thread_state(state_path)["active_title"] == "GitHub-like site"
