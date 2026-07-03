from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Dict, Iterable


def project_root() -> Path:
    """Return the KTrade project root directory."""
    return Path(__file__).resolve().parent.parent


def _default_app_data_dir() -> Path:
    system = platform.system().lower()
    if system == "darwin":
        return Path.home() / "Library" / "Application Support" / "KTrade"
    if system == "windows":
        base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or str(Path.home())
        return Path(base) / "KTrade"
    xdg = os.getenv("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "ktrade"
    return Path.home() / ".local" / "share" / "ktrade"


def app_data_dir() -> Path:
    """Writable runtime directory for secrets, DB, logs, and local state.

    The source folder stays clean and shareable; runtime artifacts go here.
    Override with KTRADE_APP_DATA_DIR for VPS/docker/custom layouts.
    """
    raw = os.getenv("KTRADE_APP_DATA_DIR", "").strip()
    path = Path(raw).expanduser() if raw else _default_app_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = Path(os.getenv("KTRADE_LOG_DIR", "").strip() or (app_data_dir() / "logs")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def env_path() -> Path:
    return Path(os.getenv("KTRADE_ENV_FILE", "").strip() or (app_data_dir() / ".env")).expanduser()


def db_path() -> Path:
    path = Path(os.getenv("KTRADE_DB_PATH", "").strip() or (app_data_dir() / "ktrade.db")).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _env_candidates() -> Iterable[Path]:
    # v12.5: runtime/app-data .env FIRST (local desktop usage), project .env
    # SECOND (server/VPS). With override=False the first existing file wins, so
    # precedence is explicit in the order rather than relying on override.
    yield env_path()
    yield project_root() / ".env"


def load_ktrade_env(override: bool = False) -> Dict[str, str]:
    """Load KTrade env files safely.

    Load order:
      1) project .env, if present, for legacy/server workflows
      2) runtime .env, if present, for local app-data secrets

    Runtime .env values override project values in memory by default only when
    `override=True`. Existing process env wins otherwise.
    """
    loaded: Dict[str, str] = {}
    try:
        from dotenv import dotenv_values
    except Exception:
        return loaded

    for candidate in _env_candidates():
        if not candidate.exists():
            continue
        try:
            values = dotenv_values(candidate, encoding="utf-8-sig")
        except TypeError:
            values = dotenv_values(candidate)
        except Exception:
            continue
        for key, value in values.items():
            if value is None:
                continue
            key_s = str(key).strip()
            value_s = str(value).strip()
            if override or key_s not in os.environ:
                os.environ[key_s] = value_s
            loaded[key_s] = value_s

    # Export runtime paths for modules that already read env vars.
    os.environ.setdefault("KTRADE_APP_DATA_DIR", str(app_data_dir()))
    os.environ.setdefault("KTRADE_LOG_DIR", str(logs_dir()))
    os.environ.setdefault("KTRADE_DB_PATH", str(db_path()))
    return loaded


def runtime_status() -> dict:
    return {
        "project_root": str(project_root()),
        "app_data_dir": str(app_data_dir()),
        "env_file": str(env_path()),
        "db_path": str(db_path()),
        "logs_dir": str(logs_dir()),
        "env_file_exists": env_path().exists(),
    }
