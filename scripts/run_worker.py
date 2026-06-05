from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chat_orchestrate.worker import run_worker
from terminal_control import shutdown_message, start_q_listener


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Chat Orchestrate worker.")
    parser.add_argument("--machine-id", help="Override MACHINE_ID for this worker.")
    parser.add_argument("--backends", help="Comma-separated backends, e.g. codex,claude-code.")
    parser.add_argument("--dry-run", choices=["true", "false"], help="Override WORKER_DRY_RUN.")
    args = parser.parse_args()

    if args.machine_id:
        os.environ["MACHINE_ID"] = args.machine_id
    if args.backends:
        os.environ["AGENT_BACKENDS"] = args.backends
    if args.dry_run:
        os.environ["WORKER_DRY_RUN"] = args.dry_run

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )
    try:
        asyncio.run(run_until_stopped())
    except KeyboardInterrupt:
        shutdown_message()


async def run_until_stopped() -> None:
    loop = asyncio.get_running_loop()
    worker_task = asyncio.create_task(run_worker())

    def request_stop() -> None:
        loop.call_soon_threadsafe(worker_task.cancel)

    start_q_listener(request_stop)
    print("Press q then Enter, or Ctrl-C, to stop.")
    try:
        await worker_task
    except asyncio.CancelledError:
        shutdown_message()


if __name__ == "__main__":
    main()
