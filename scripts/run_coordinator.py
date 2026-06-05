from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn

from chat_orchestrate.config import get_settings
from chat_orchestrate.coordinator_server import create_app
from terminal_control import (
    install_clean_asyncio_exception_handler,
    shutdown_message,
    start_q_listener,
)


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

    get_settings.cache_clear()
    settings = get_settings()
    host = args.host or settings.coordinator_host
    port = args.port or settings.coordinator_port
    try:
        asyncio.run(serve(settings, host, port))
    except KeyboardInterrupt:
        shutdown_message()


async def serve(settings, host: str, port: int) -> None:
    install_clean_asyncio_exception_handler()
    config = uvicorn.Config(create_app(settings), host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    start_q_listener(lambda: setattr(server, "should_exit", True))
    print("Press q then Enter, or Ctrl-C, to stop.")
    await server.serve()


if __name__ == "__main__":
    main()
