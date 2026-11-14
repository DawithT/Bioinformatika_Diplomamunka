"""
start.py — launch the whole stack with one command.

Replaces the three-terminal dance (redis-server / run.py / worker.py):

    python start.py

Starts Redis (only if it is not already running and a redis-server binary is on
PATH — on Windows Redis is normally an always-on service such as Memurai), then
the RQ worker, then the Flask dev server, and opens the browser.  Ctrl+C stops
everything it started.

For dev/prod parity use `docker compose up` instead; this script is for
bare-metal development.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST, PORT = "127.0.0.1", 5000
URL = f"http://{HOST}:{PORT}"

# Progress lines interleave with the children's logs; unbuffered keeps them in order.
sys.stdout.reconfigure(line_buffering=True)


def _fail(msg: str) -> None:
    print(f"\n[start] {msg}\n", file=sys.stderr)
    sys.exit(1)


def _check_deps() -> None:
    """Fail early and clearly rather than with a traceback from a child."""
    missing = []
    for mod in ("flask", "redis", "rq"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        _fail(
            f"This Python ({sys.executable}) is missing: {', '.join(missing)}.\n"
            f"        Install them with:  {sys.executable} -m pip install -r requirements.txt"
        )


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


def _redis_is_up() -> bool:
    import redis

    from config import settings

    try:
        redis.from_url(settings.redis_url, socket_connect_timeout=2).ping()
        return True
    except Exception:
        return False


def _start_redis() -> subprocess.Popen | None:
    """Start redis-server if one is not already listening. Returns the child."""
    if _redis_is_up():
        print("[start] Redis already running")
        return None

    binary = shutil.which("redis-server")
    if binary is None:
        _fail(
            "Redis is not running and no 'redis-server' binary was found.\n"
            "        Linux/macOS:  sudo apt install redis-server   (or: brew install redis)\n"
            "        Windows:      winget install Memurai.MemuraiDeveloper   (run as Administrator)\n"
            "                      — installs Redis as an always-on Windows service."
        )

    print(f"[start] Starting Redis ({binary})")
    proc = subprocess.Popen([binary], cwd=ROOT)
    for _ in range(20):
        time.sleep(0.5)
        if _redis_is_up():
            return proc
    proc.terminate()
    _fail("Redis was started but never became reachable.")


def main() -> None:
    _check_deps()

    # Windows lets a second server bind a port that is already listening, so a
    # forgotten `python run.py` would keep serving half the requests from stale
    # code. Refuse to start rather than leave that to chance.
    if _port_in_use(HOST, PORT):
        _fail(
            f"Something is already serving {URL}.\n"
            "        Close the other 'python run.py' window (and its worker) first."
        )

    children: list[subprocess.Popen] = []
    redis_proc = _start_redis()
    if redis_proc is not None:
        children.append(redis_proc)

    try:
        print("[start] Starting RQ worker")
        children.append(subprocess.Popen([sys.executable, "worker.py"], cwd=ROOT))

        print("[start] Starting Flask")
        flask_proc = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT)
        children.append(flask_proc)

        time.sleep(2)
        if flask_proc.poll() is not None:
            _fail("Flask exited immediately — see the error above.")

        print(f"[start] Ready: {URL}   (Ctrl+C to stop)")
        webbrowser.open(URL)

        # Exit as soon as any child dies, so a crashed worker is visible
        # instead of leaving a half-running stack behind.
        while True:
            time.sleep(1)
            for child in children:
                if child.poll() is not None:
                    print(f"[start] A process exited (code {child.returncode}) — shutting down.")
                    return
    except KeyboardInterrupt:
        print("\n[start] Stopping…")
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in reversed(children):
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    main()
