# -*- coding: utf-8 -*-
"""Resolve watchdog/* and other repo-relative paths in .env values."""

from __future__ import annotations

import re
from pathlib import Path

# Keys whose values are filesystem paths under the repo (or absolute / Windows paths).
WATCHDOG_PATH_KEYS: frozenset[str] = frozenset(
    {
        "CS_CALL_REPORT_BASE_DIR",
        "CS_EXTRACT_SCRIPT",
        "CS_OUTPUT_ROOT",
        "CS_LOG_DIR",
        "CS_RUN_SCRIPT",
        "KS_CALL_REPORT_BASE_DIR",
        "KS_EXTRACT_SCRIPT",
        "KS_OUTPUT_ROOT",
        "KS_LOG_DIR",
        "KS_RUN_SCRIPT",
        "VM_VIRAL_BASE_DIR",
        "VM_EXTRACT_SCRIPT",
        "VM_OUTPUT_ROOT",
        "VM_LOG_DIR",
        "VM_CHECKPOINT_DIR",
        "VM_RUN_SCRIPT",
    }
)

_WIN_ABS = re.compile(r"^[A-Za-z]:[\\/]")


def resolve_watchdog_path_value(key: str, val: str, repo: Path) -> str:
    if key not in WATCHDOG_PATH_KEYS:
        return val
    v = val.strip()
    if not v:
        return val
    if "://" in v:
        return val
    if v.startswith("/"):
        return str(Path(v).expanduser().resolve())
    if v.startswith("\\\\"):
        return val
    if _WIN_ABS.match(v):
        return val
    return str((repo / v).resolve())
