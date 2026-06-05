from __future__ import annotations

import argparse
import os

import uvicorn

from chat_orchestrate.config import get_settings
from chat_orchestrate.coordinator_server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Chat Orchestrate HTTP coordinator.")
    parser.add_argument("--host", help="Host to bind. Defaults to COORDINATOR_HOST.")
    parser.add_argument("--port", type=int, help="Port to bind. Defaults to COORDINATOR_PORT.")
    parser.add_argument("--cluster-id", help="Override CLUSTER_ID.")
    parser.add_argument("--token", help="Override COORDINATION_TOKEN.")
    parser.add_argument("--state-path", help="Override COORDINATION_STATE_PATH.")
    args = parser.parse_args()

    if args.cluster_id:
        os.environ["CLUSTER_ID"] = args.cluster_id
    if args.token:
        os.environ["COORDINATION_TOKEN"] = args.token
    if args.state_path:
        os.environ["COORDINATION_STATE_PATH"] = args.state_path

    settings = get_settings()
    host = args.host or settings.coordinator_host
    port = args.port or settings.coordinator_port
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
