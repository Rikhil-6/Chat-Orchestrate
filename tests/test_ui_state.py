from pathlib import Path

from chat_orchestrate.ui_state import load_credentials, save_credentials


def test_credentials_are_saved_by_backend(tmp_path: Path) -> None:
    path = tmp_path / "ui_state.json"

    save_credentials("codex", {"openai_api_key": "sk-test", "codex_command": "codex"}, path)

    assert load_credentials(path)["codex"]["openai_api_key"] == "sk-test"
    assert load_credentials(path)["codex"]["codex_command"] == "codex"
