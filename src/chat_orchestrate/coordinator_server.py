from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from .a2a import agent_card, create_task_from_send_message, task_to_a2a
from .capabilities import infer_goal_roles, infer_machine_capabilities
from .config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Chat Orchestrate Coordinator")
    state_path = settings.coordination_state_path.resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    expected_token_hash = _hash_token(settings.coordination_token)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "cluster_id": settings.cluster_id}

    @app.get("/api/smoke")
    def smoke() -> dict[str, str | bool]:
        return {"ok": True, "service": "chat-orchestrate", "workspace": "default"}

    @app.get("/api/smoke/tags")
    def smoke_tags(goal: str = "") -> dict[str, object]:
        goal_roles = infer_goal_roles(goal) if goal.strip() else []
        capabilities = infer_machine_capabilities(
            settings.default_agents,
            "auto",
            settings.default_agents,
            goal,
        )
        return {
            "ok": True,
            "service": "chat-orchestrate",
            "workspace": "default",
            "dynamic_tags": {
                "goal_roles": goal_roles,
                "capabilities": capabilities,
            },
        }

    @app.get("/.well-known/agent-card.json")
    def well_known_agent_card(request: Request) -> dict[str, Any]:
        return agent_card(settings, _base_url(request))

    @app.get("/a2a/agent-card")
    def a2a_agent_card(request: Request) -> dict[str, Any]:
        return agent_card(settings, _base_url(request))

    @app.post("/a2a/rpc")
    def a2a_rpc(
        payload: dict[str, Any],
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _authorize(authorization, expected_token_hash)
        method = payload.get("method")
        request_id = payload.get("id")
        params = payload.get("params") or {}
        state = _load_state(state_path, settings.cluster_id, expected_token_hash)
        _assert_cluster(state, settings.cluster_id, expected_token_hash)

        if method == "GetExtendedAgentCard":
            return _rpc_result(request_id, agent_card(settings, _base_url(request)))
        if method == "ListTasks":
            tasks = [task_to_a2a(task) for task in state.get("tasks", [])]
            status_filter = params.get("status")
            if status_filter:
                tasks = [task for task in tasks if task.get("status", {}).get("state") == status_filter]
            page_size = int(params.get("pageSize") or 50)
            return _rpc_result(
                request_id,
                {
                    "tasks": tasks[:page_size],
                    "nextPageToken": "",
                    "pageSize": min(page_size, len(tasks)),
                    "totalSize": len(tasks),
                },
            )
        if method == "GetTask":
            task_id = params.get("id")
            for task in state.get("tasks", []):
                if task.get("task_id") == task_id or task.get("id") == task_id:
                    return _rpc_result(request_id, task_to_a2a(task))
            return _rpc_error(request_id, -32001, "Task not found", {"taskId": task_id})
        if method == "SendMessage":
            default_machine = state.get("orchestrator_machine") or ""
            task = create_task_from_send_message(state, params, default_machine=default_machine)
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            return _rpc_result(request_id, {"task": task_to_a2a(task)})
        return _rpc_error(request_id, -32601, f"Method not found: {method}")

    @app.get("/state")
    def get_state(cluster_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _authorize(authorization, expected_token_hash)
        state = _load_state(state_path, cluster_id, expected_token_hash)
        _assert_cluster(state, cluster_id, expected_token_hash)
        return state

    @app.put("/state")
    def put_state(
        cluster_id: str,
        state: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        _authorize(authorization, expected_token_hash)
        _assert_cluster(state, cluster_id, expected_token_hash)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return {"status": "saved"}

    return app


def _load_state(state_path: Path, cluster_id: str, token_hash: str) -> dict[str, Any]:
    if state_path.exists():
        return json.loads(state_path.read_text(encoding="utf-8"))
    return {
        "cluster_id": cluster_id,
        "token_hash": token_hash,
        "orchestrator_machine": None,
        "orchestrator_claimed_at": None,
        "machines": {},
        "tasks": [],
    }


def _authorize(authorization: str | None, expected_token_hash: str) -> None:
    if not expected_token_hash:
        return
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Missing coordination token.")
    supplied_hash = _hash_token(authorization.removeprefix(prefix))
    if supplied_hash != expected_token_hash:
        raise HTTPException(status_code=403, detail="Invalid coordination token.")


def _assert_cluster(state: dict[str, Any], cluster_id: str, token_hash: str) -> None:
    if state.get("cluster_id") != cluster_id:
        raise HTTPException(status_code=409, detail="Cluster ID mismatch.")
    state_token_hash = state.get("token_hash", "")
    if state_token_hash and state_token_hash != token_hash:
        raise HTTPException(status_code=403, detail="Coordination token mismatch.")
    state.setdefault("token_hash", token_hash)
    state.setdefault("machines", {})
    state.setdefault("tasks", [])


def _hash_token(token: str) -> str:
    clean = token.strip()
    if not clean:
        return ""
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _rpc_result(request_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: object, code: int, message: str, data: object | None = None) -> dict[str, object]:
    error: dict[str, object] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


app = create_app()
