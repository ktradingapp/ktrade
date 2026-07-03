from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _pid_is_running(pid: int) -> bool:
    """True if a process with this PID is alive. Used to detect stale lockfiles
    left behind by a crash/SIGKILL."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists but owned by another user
    except Exception:
        return False


class ProcessLock:
    """Simple cross-platform lockfile to avoid duplicate local engines.

    This is intentionally conservative: it prevents accidental double-starts
    from two terminals or desktop launchers. It is not a distributed lock.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd: Optional[int] = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # v12.4: a lockfile whose PID is no longer alive is stale (prior crash) —
        # remove it so a relaunch isn't blocked forever.
        if self.path.exists():
            try:
                old_pid = int((self.path.read_text().strip() or "0"))
            except Exception:
                old_pid = 0
            if _pid_is_running(old_pid):
                return False
            try:
                self.path.unlink()
            except Exception:
                pass
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            self.fd = os.open(str(self.path), flags)
            os.write(self.fd, str(os.getpid()).encode("utf-8"))
            return True
        except FileExistsError:
            return False

    def release(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None
        # v12.5: only remove the lockfile if WE still own it (PID match), so a rare
        # timing race can't delete a lock held by another live process.
        try:
            current_pid = int((self.path.read_text().strip() or "0"))
        except Exception:
            current_pid = None
        if current_pid == os.getpid():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"KTrade appears to be already running: {self.path}")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
