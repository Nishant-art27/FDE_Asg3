"""One-command local start: mock vendor-risk API + Streamlit UI.

    python run_local.py            # API on :8001, UI on :8501
    python run_local.py --no-ui    # API only (e.g. before running the evals)
"""
from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)


def start(cmd: list[str]) -> subprocess.Popen:
    return subprocess.Popen(cmd, cwd=ROOT)


def api_already_running(url: str) -> bool:
    """True if a healthy mock vendor-risk API already answers on the port (e.g. an eval run started it)."""
    try:
        return requests.get(url, timeout=0.5).ok
    except requests.RequestException:
        return False


def wait_for_api(url: str, proc: subprocess.Popen, timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Vendor-risk API exited during startup with code {proc.returncode} "
                               "(is port 8001 already in use by something other than the mock API? "
                               "Find it with: lsof -nP -iTCP:8001)")
        try:
            if requests.get(url, timeout=0.5).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"Vendor-risk API did not become ready within {timeout_seconds:.0f}s: {url}")


def _handle_termination(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ui", action="store_true", help="start only the mock API")
    args = ap.parse_args()
    signal.signal(signal.SIGTERM, _handle_termination)
    procs: list[subprocess.Popen] = []
    try:
        if api_already_running("http://127.0.0.1:8001/health"):
            print("Vendor-risk API is already running on http://127.0.0.1:8001 - reusing it.")
        else:
            print("Starting vendor-risk API on http://127.0.0.1:8001 ...")
            api = start([sys.executable, "-m", "uvicorn", "mock_api.app:app", "--host", "127.0.0.1", "--port", "8001"])
            procs.append(api)
            wait_for_api("http://127.0.0.1:8001/health", api)
            print("Vendor-risk API is ready.")
        if not args.no_ui:
            print("Starting UI on http://127.0.0.1:8501 ...")
            procs.append(start([sys.executable, "-m", "streamlit", "run", "app.py", "--server.port", "8501",
                                "--server.headless", "true"]))
        if not procs:
            print("Nothing to run (API reused, --no-ui given). Exiting.")
            return
        while True:
            time.sleep(1)
            for proc in procs:
                if proc.poll() is not None:
                    raise RuntimeError(f"A local process exited with code {proc.returncode}")
    except KeyboardInterrupt:
        print("\nStopping local services ...")
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
