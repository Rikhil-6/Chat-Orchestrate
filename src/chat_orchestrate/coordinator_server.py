from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException

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


app = create_app()
