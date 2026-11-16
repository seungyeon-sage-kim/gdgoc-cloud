"""Run all three services on this laptop, the way Cloud Run would run them.

  web                http://localhost:8080   public
  ticket-worker      http://localhost:8081   private (only "Pub/Sub" calls it)
  waitlist-promoter  http://localhost:8082   private (only "Eventarc" calls it)

The job runs separately:  python -m jobs.housekeeping   (add --force-release to release seats now)

A fresh database gets nine sample events (shared/demo.py); after that, create
and edit events at http://localhost:8080/admin. Sign in as organizer@example.com
(or an address on ADMIN_EMAILS); emails, including the sign-in code, are saved
to http://localhost:8080/dev/outbox instead of being sent.

Usage:
  python dev.py                 # settings from .env
  python dev.py --open-in 5     # also add an event whose signups open 5 minutes from now
  python dev.py --reset         # delete local data first
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
SERVICES = [
    ("web", "web.main:app", 8080),
    ("ticket-worker", "ticket_worker.main:app", 8081),
    ("waitlist-promoter", "waitlist_promoter.main:app", 8082),
]


def load_env_file(path: Path) -> dict:
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-in", type=float, help="minutes until registration opens")
    parser.add_argument("--reset", action="store_true", help="delete .data before starting")
    parser.add_argument("--reload", action="store_true", help="restart services when code changes")
    args = parser.parse_args()

    # Real environment variables win over .env (the usual dotenv rule)
    env = {**load_env_file(ROOT / ".env"), **os.environ, "PYTHONUNBUFFERED": "1"}
    # Without a list nobody could open /admin locally; sign in as this address
    # and read the code at http://localhost:8080/dev/outbox
    env.setdefault("ADMIN_EMAILS", "organizer@example.com")
    if args.reset:
        data_dir = Path(env.get("DATA_DIR") or ROOT / ".data")
        shutil.rmtree(data_dir, ignore_errors=True)
        print(f"deleted {data_dir}")
    # Sample events on a fresh database, and the --open-in rush event
    seed = [sys.executable, "-m", "shared.demo"]
    if args.open_in is not None:
        seed += ["--open-in", str(args.open_in)]
    subprocess.run(seed, cwd=ROOT, env=env, check=True)

    processes = []
    for name, app, port in SERVICES:
        command = [sys.executable, "-m", "uvicorn", app, "--port", str(port), "--log-level", "warning"]
        if args.reload:
            command.append("--reload")
        processes.append(subprocess.Popen(command, cwd=ROOT, env={**env, "K_SERVICE": name}))
        print(f"{name:<17} http://localhost:{port}")

    def stop(*_):
        for process in processes:
            process.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while all(p.poll() is None for p in processes):
        time.sleep(0.5)
    stop()


if __name__ == "__main__":
    main()
