#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Print `export KEY=value` lines for bash/zsh.

Usage:
  python3 env/dotenv_to_shell.py <.env path> [repo root]
If repo root is omitted, the parent directory of the .env file is used.
"""

from __future__ import annotations

import importlib.util
import re
import shlex
import sys
from pathlib import Path


def _load_shared():
    root = Path(__file__).resolve().parent
    path = root / "dotenv_shared.py"
    spec = importlib.util.spec_from_file_location("dotenv_shared", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load dotenv_shared")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_shared = _load_shared()
resolve_watchdog_path_value = _shared.resolve_watchdog_path_value


def iter_dotenv(path: Path):
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        yield key, val


def main() -> int:
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print("usage: dotenv_to_shell.py <.env path> [repo root]", file=sys.stderr)
        return 2
    src = Path(sys.argv[1])
    if not src.is_file():
        print(f"missing env file: {src}", file=sys.stderr)
        return 1
    repo = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else src.resolve().parent
    for k, v in iter_dotenv(src):
        v = resolve_watchdog_path_value(k, v, repo)
        print(f"export {k}={shlex.quote(v)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
