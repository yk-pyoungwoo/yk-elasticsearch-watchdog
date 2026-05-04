#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Emit a temp .bat file that sets process env from a dotenv-style file (for Windows cmd).

Usage:
  python bootstrap_dotenv.py <.env path> <output .bat path>
Repo root defaults to the directory containing .env (usually the project root).
"""

from __future__ import annotations

import importlib.util
import re
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


def main() -> int:
    if len(sys.argv) < 3:
        print(
            "usage: bootstrap_dotenv.py <.env path> <output .bat path>",
            file=sys.stderr,
        )
        return 2
    src = Path(sys.argv[1])
    out = Path(sys.argv[2])
    if not src.is_file():
        print(f"missing env file: {src}", file=sys.stderr)
        return 1
    repo = src.resolve().parent
    lines: list[str] = []
    for raw in src.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        val = resolve_watchdog_path_value(key, val, repo)
        val = val.replace("%", "%%")
        lines.append(f'set "{key}={val}"')
    content = "@echo off\r\n" + "\r\n".join(lines) + "\r\n"
    out.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
