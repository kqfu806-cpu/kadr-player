#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
export PYTHONUTF8=1
if command -v python3 >/dev/null 2>&1; then
  python3 launch.py
else
  python launch.py
fi
