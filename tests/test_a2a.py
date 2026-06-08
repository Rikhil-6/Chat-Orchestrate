from chat_orchestrate.a2a import create_task_from_send_message, task_to_a2a


def test_send_message_creates_delegated_task_and_a2a_task_shape() -> None:
    state = {"tasks": []}
    task = create_task_from_send_message(
        state,
        {
            "message": {
                "messageId": "msg-1",
                "contextId": "ctx-1",
                "role": "ROLE_USER",
                "parts": [{"text": "build a frontend view"}],
            },
            "metadata": {
                "project": "demo",
                "role": "frontend",
                "assignedMachine": "laptop-b",
                "preferredBackend": "claude-code",
            },
        },
        default_machine="laptop-a",
    )

    assert state["tasks"] == [task]
    assert task["goal"] == "build a frontend view"
    assert task["assigned_machine"] == "laptop-b"

    a2a_task = task_to_a2a(task)

    assert a2a_task["contextId"] == "ctx-1"
    assert a2a_task["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert a2a_task["metadata"]["role"] == "frontend"
    assert a2a_task["metadata"]["preferredBackend"] == "claude-code"
