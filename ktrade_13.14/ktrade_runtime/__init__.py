"""KTrade local runtime helpers.

This package contains KTrade-only runtime utilities inspired by desktop-app
best practices: keep mutable runtime files outside the source tree, use
local-only process supervision, and avoid duplicate backend instances.
"""

from .paths import (
    app_data_dir,
    db_path,
    env_path,
    load_ktrade_env,
    logs_dir,
    project_root,
    runtime_status,
)

__all__ = [
    "app_data_dir",
    "db_path",
    "env_path",
    "load_ktrade_env",
    "logs_dir",
    "project_root",
    "runtime_status",
]
