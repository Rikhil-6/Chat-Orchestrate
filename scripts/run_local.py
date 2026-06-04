from __future__ import annotations

import argparse
import os
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Chainlit UI and local worker together.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default="7860")
    parser.add_argument("--machine-id", default="")
    parser.add_argument("--backends", default="")
    args = parser.parse_args()

    env_args = []
    if args.machine_id:
        subprocess_env_machine_id = args.machine_id
    else:
        subprocess_env_machine_id = ""
    if args.backends:
        subprocess_env_backends = args.backends
    else:
        subprocess_env_backends = ""

    if subprocess_env_machine_id:
        os.environ["MACHINE_ID"] = subprocess_env_machine_id
    if subprocess_env_backends:
        os.environ["AGENT_BACKENDS"] = subprocess_env_backends

    if args.machine_id:
        env_args.extend(["--machine-id", args.machine_id])
    if args.backends:
        env_args.extend(["--backends", args.backends])

    worker = subprocess.Popen([sys.executable, "scripts/run_worker.py", *env_args])
    try:
        subprocess.run(
            [
                sys.executable,
                "scripts/run_chainlit.py",
                "src/chat_orchestrate/chainlit_app.py",
                "--host",
                args.host,
                "--port",
                args.port,
            ],
            check=False,
        )
    finally:
        worker.terminate()
        worker.wait(timeout=10)


if __name__ == "__main__":
    main()
