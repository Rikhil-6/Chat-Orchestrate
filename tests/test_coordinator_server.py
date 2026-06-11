from pathlib import Path

from fastapi.testclient import TestClient

from chat_orchestrate.config import Settings
from chat_orchestrate.coordinator_server import create_app


def test_smoke_endpoint_is_public_with_contract(tmp_path: Path) -> None:
    app = create_app(
        Settings(coordination_state_path=tmp_path / "coordination.json", coordination_token="secret")
    )
    client = TestClient(app)

    response = client.get("/api/smoke")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "service": "chat-orchestrate",
        "workspace": "default",
    }


def test_smoke_tags_endpoint_infers_dynamic_tags_without_auth(tmp_path: Path) -> None:
    app = create_app(
        Settings(coordination_state_path=tmp_path / "coordination.json", coordination_token="secret")
    )
    client = TestClient(app)

    response = client.get(
        "/api/smoke/tags",
        params={"goal": "build a backend API and frontend page smoke dynamic tags"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["service"] == "chat-orchestrate"
    assert body["workspace"] == "default"
    assert body["dynamic_tags"]["goal_roles"] == [
        "coordinator",
        "backend",
        "frontend",
        "engineer",
    ]
    assert body["dynamic_tags"]["capabilities"] == [
        "coordinator",
        "backend",
        "frontend",
        "engineer",
    ]
