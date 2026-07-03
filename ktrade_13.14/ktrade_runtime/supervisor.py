from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

from .paths import app_data_dir, load_ktrade_env, logs_dir, runtime_status
from .process_lock import ProcessLock


def _health_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # nosec - localhost only
            return 200 <= response.status < 500
    except Exception:
        return False


class KTradeSupervisor:
    """Local-only backend supervisor for KTrade.

    Best practices imported from desktop-app style wrappers, without adding any
    external app-specific code: local host binding, app-data env/log paths,
    health checks, duplicate-process lock, optional macOS sleep prevention, and
    graceful shutdown.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 5001, open_browser: bool = False):
        load_ktrade_env()  # v12.5: precedence handled by _env_candidates order
        self.host = host
        self.port = int(port)
        self.open_browser = bool(open_browser)
        self.project_root = Path(__file__).resolve().parent.parent
        self.backend_proc: Optional[subprocess.Popen] = None
        self.caffeinate_proc: Optional[subprocess.Popen] = None
        self.log_file = logs_dir() / "ktrade_backend_supervised.log"
        self.lock = ProcessLock(app_data_dir() / "ktrade_backend.lock")

    @property
    def health_url(self) -> str:
        return f"http://{self.host}:{self.port}/health"

    @property
    def ui_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _start_caffeinate_if_requested(self) -> None:
        if sys.platform != "darwin":
            return
        if os.getenv("KTRADE_PREVENT_SLEEP", "true").strip().lower() not in {"1", "true", "yes", "on"}:
            return
        try:
            self.caffeinate_proc = subprocess.Popen(["/usr/bin/caffeinate", "-dimsu"])
        except Exception:
            self.caffeinate_proc = None

    def start_backend(self) -> None:
        env = os.environ.copy()
        env.setdefault("KTRADE_BIND_HOST", self.host)
        env.setdefault("KTRADE_PORT", str(self.port))
        env.setdefault("KTRADE_PAPER_ORDER_SUBMISSION", "false")
        env.setdefault("LIVE_TRADING", "false")
        env.setdefault("KTRADE_MANUAL_ALLOW_DEMO", "false")
        env.setdefault("KTRADE_APP_DATA_DIR", str(app_data_dir()))
        env.setdefault("KTRADE_LOG_DIR", str(logs_dir()))

        cmd = [sys.executable, "backend/ktrade_alpaca.py"]
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(self.log_file, "a", buffering=1, encoding="utf-8")
        self.backend_proc = subprocess.Popen(
            cmd,
            cwd=str(self.project_root),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def wait_for_health(self, timeout_s: float = 45.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.backend_proc and self.backend_proc.poll() is not None:
                return False
            if _health_ok(self.health_url):
                return True
            time.sleep(1.0)
        return False

    def run(self) -> int:
        if not self.lock.acquire():
            print(f"KTrade backend already appears to be running. Lock: {self.lock.path}")
            return 2
        try:
            self._start_caffeinate_if_requested()
            self.start_backend()
            ok = self.wait_for_health()
            print("KTrade runtime status:")
            for key, value in runtime_status().items():
                print(f"  {key}: {value}")
            print(f"  backend_log: {self.log_file}")
            print(f"  health_url: {self.health_url}")
            print(f"  ui_url: {self.ui_url}")
            print(f"  health_ok: {ok}")
            if ok and self.open_browser:
                webbrowser.open(self.ui_url)
            if not ok:
                return 1
            print("Backend is running. Press Ctrl+C to stop.")
            while self.backend_proc and self.backend_proc.poll() is None:
                time.sleep(2.0)
            return int(self.backend_proc.returncode or 0)
        except KeyboardInterrupt:
            return 0
        finally:
            self.stop()
            self.lock.release()

    def stop(self) -> None:
        if self.backend_proc and self.backend_proc.poll() is None:
            try:
                self.backend_proc.terminate()
                self.backend_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    self.backend_proc.kill()
                except Exception:
                    pass
            except Exception:
                pass
        if self.caffeinate_proc and self.caffeinate_proc.poll() is None:
            try:
                self.caffeinate_proc.terminate()
            except Exception:
                pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run KTrade backend with local runtime supervision")
    parser.add_argument("--host", default=os.getenv("KTRADE_BIND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("KTRADE_PORT", "5001")))
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args(argv)
    return KTradeSupervisor(host=args.host, port=args.port, open_browser=args.open_browser).run()


if __name__ == "__main__":
    raise SystemExit(main())
