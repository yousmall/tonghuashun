"""Local API supervisor. Restart only its own worker tree; no production SLA claim."""
import argparse
from pathlib import Path
import os
import subprocess
import sys
import time
import socket
import json

ROOT = Path(__file__).resolve().parents[1]


def stop(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
    elif process.poll() is None:
        process.terminate()
    process.wait(timeout=15)


def run(args):
    import httpx
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", args.port)) == 0:
            raise RuntimeError("Port occupied; refusing to manage an existing service")
    args.log.parent.mkdir(parents=True, exist_ok=True)
    process = None
    try:
        with args.log.open("a", encoding="utf-8") as log:
            for restart in range(args.max_restarts + 1):
                process = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.app.main:app",
                    "--host", args.host, "--port", str(args.port), "--workers", str(args.workers)],
                    cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                log.write("SUPERVISOR " + json.dumps({"event":"started", "pid":process.pid, "restart":restart}) + "\n")
                log.flush()
                failures = 0
                time.sleep(args.startup_grace)
                while process.poll() is None:
                    try:
                        ok = httpx.get(f"http://127.0.0.1:{args.port}/api/v1/health", timeout=3, trust_env=False).status_code == 200
                    except httpx.HTTPError:
                        ok = False
                    failures = 0 if ok else failures + 1
                    if failures >= 3:
                        break
                    time.sleep(args.interval)
                stop(process)
                process = None
                if restart < args.max_restarts:
                    time.sleep(min(30, 2 ** restart))
    finally:
        if process is not None:
            stop(process)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--max-restarts", type=int, default=5)
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--startup-grace", type=float, default=15)
    p.add_argument("--log", type=Path, default=ROOT / ".tmp_flow/supervisor.log")
    args = p.parse_args()
    if not 1 <= args.port <= 65535 or args.workers < 1 or args.max_restarts < 0 or min(args.interval, args.startup_grace) <= 0:
        p.error("Invalid service limits")
    run(args)
