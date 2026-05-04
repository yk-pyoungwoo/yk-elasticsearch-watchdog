#!/usr/bin/env bash
# Print one usable Python interpreter path/name for exec.
# Arg $1: preferred from .env (e.g. python, python3, or absolute path). May be empty.
set -euo pipefail
pref="${1:-}"

if [[ -n "$pref" ]]; then
  if [[ "$pref" == */* ]] && [[ -x "$pref" ]]; then
    printf '%s\n' "$pref"
    exit 0
  fi
  if command -v "$pref" >/dev/null 2>&1; then
    command -v "$pref"
    exit 0
  fi
fi

if command -v python3 >/dev/null 2>&1; then
  command -v python3
  exit 0
fi
if command -v python >/dev/null 2>&1; then
  command -v python
  exit 0
fi

echo "resolve_python_bin: no python3 or python in PATH (preferred was: ${pref:-empty})" >&2
exit 1
