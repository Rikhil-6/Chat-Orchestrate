from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from terminal_control import shutdown_message

RESTART_MARKER = Path(".tmp") / "restart-local"
RESTART_DELAY_SECONDS = 2.0
CRASH_WINDOW_SECONDS = 60.0
MAX_FAST_CHAINLIT_RESTARTS = 5


def session_path(port: str) -> Path:
    return Path(".tmp") / f"local-session-{port}.json"


def lock_port(port: str) -> int:
    try:
        numeric_port = int(port)
    except ValueError:
        numeric_port = 7862
    return 30000 + (numeric_port % 20000)


def acquire_lock(port: str) -> socket.socket | None:
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", lock_port(port)))
        lock.listen(1)
        return lock
    except OSError:
        lock.close()
        return None


def process_exists(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_process_tree(pid: int) -> None:
    if not process_exists(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        return
    try:
        os.kill(pid, 15)
    except OSError:
        return


def cleanup_previous_session(path: Path) -> None:
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}
    pids = [int(pid) for pid in payload.get("pids", []) if str(pid).isdigit()]
    for pid in pids:
        stop_process_tree(pid)
    for pid_file in Path(".tmp").glob("hosted-coordinator-*.pid"):
        try:
            pid_text = pid_file.read_text(encoding="utf-8").strip()
            if pid_text.isdigit():
                stop_process_tree(int(pid_text))
            pid_file.unlink()
        except OSError:
            pass
    try:
        path.unlink()
    except OSError:
        pass


def write_session(path: Path, *processes: subprocess.Popen | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pids = [os.getpid(), *[process.pid for process in processes if process]]
    path.write_text(json.dumps({"launcher": os.getpid(), "pids": pids}, indent=2), encoding="utf-8")


def clear_session(path: Path) -> None:
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}
    if payload.get("launcher") == os.getpid():
        path.unlink()


def launch_worker(env_args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "scripts/run_worker.py", *env_args],
        stdin=subprocess.DEVNULL,
    )


def launch_chainlit(host: str, port: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "scripts/run_chainlit.py",
            "src/chat_orchestrate/chainlit_app.py",
            "--host",
            host,
            "--port",
            port,
        ]
    )


def stop_child(process: subprocess.Popen | None, timeout: float = 10.0) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()


def print_listen_urls(host: str, port: str) -> None:
    local_url = f"http://localhost:{port}"
    print(f"Local UI: {local_url}")
    if host in {"0.0.0.0", "::"}:
        addresses = network_addresses()
        if addresses:
            print("LAN UI:")
            for address in addresses[:4]:
                print(f"  http://{address}:{port}")
        else:
            print("LAN UI: bound to all interfaces; use this machine's Wi-Fi/LAN IP.")
    else:
        print(f"Bound UI host: {host}. Use --host 0.0.0.0 to expose it to the LAN.")


def network_addresses() -> list[str]:
    addresses: list[str] = []
    try:
        hostname = socket.gethostname()
        for item in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
            address = item[4][0]
            if address.startswith("127.") or address in addresses:
                continue
            addresses.append(address)
    except OSError:
        pass
    return addresses


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Chainlit UI and local worker together.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default="7862")
    parser.add_argument("--machine-id", default="")
    parser.add_argument("--backends", default="")
    parser.add_argument("--no-keepalive", action="store_true", help="Do not restart crashed local child processes.")
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

    local_session = session_path(str(args.port))
    cleanup_previous_session(local_session)
    local_lock = acquire_lock(str(args.port))
    if local_lock is None:
        print(
            f"Another Chat Orchestrate local launcher is already starting or running for port {args.port}. "
            "Close that session first, then run this command again."
        )
        return

    print_listen_urls(args.host, args.port)
    chainlit_crashes: list[float] = []
    while True:
        if RESTART_MARKER.exists():
            RESTART_MARKER.unlink()
        worker = launch_worker(env_args)
        chainlit = launch_chainlit(args.host, args.port)
        write_session(local_session, chainlit, worker)
        exit_reason = ""
        chainlit_code: int | None = None
        try:
            while True:
                if RESTART_MARKER.exists():
                    exit_reason = "requested-restart"
                    break

                chainlit_code = chainlit.poll()
                if chainlit_code is not None:
                    exit_reason = "chainlit-exited"
                    break

                worker_code = worker.poll()
                if worker_code is not None and not args.no_keepalive:
                    print(
                        f"Local worker exited with code {worker_code}; restarting worker in "
                        f"{RESTART_DELAY_SECONDS:g}s..."
                    )
                    time.sleep(RESTART_DELAY_SECONDS)
                    worker = launch_worker(env_args)
                    write_session(local_session, chainlit, worker)

                time.sleep(1.0)
        except KeyboardInterrupt:
            shutdown_message()
            return
        finally:
            stop_child(chainlit)
            stop_child(worker)
            clear_session(local_session)

        if RESTART_MARKER.exists():
            print("Restart marker detected. Relaunching local UI and worker...")
            continue
        if exit_reason == "chainlit-exited" and chainlit_code != 0 and not args.no_keepalive:
            now = time.monotonic()
            chainlit_crashes = [stamp for stamp in chainlit_crashes if now - stamp <= CRASH_WINDOW_SECONDS]
            chainlit_crashes.append(now)
            if len(chainlit_crashes) > MAX_FAST_CHAINLIT_RESTARTS:
                print(
                    "Chainlit exited repeatedly in under a minute. Leaving the launcher stopped so the "
                    "terminal log can be inspected."
                )
                return
            print(
                f"Local Chainlit UI exited with code {chainlit_code}; restarting in "
                f"{RESTART_DELAY_SECONDS:g}s so localhost comes back..."
            )
            time.sleep(RESTART_DELAY_SECONDS)
            continue
        return


if __name__ == "__main__":
    main()
