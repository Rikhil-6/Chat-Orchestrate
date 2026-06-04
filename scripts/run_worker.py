from __future__ import annotations

import argparse
import asyncio
import logging
import os

from chat_orchestrate.worker import run_worker


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
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
