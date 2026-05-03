#!/bin/bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "Usage: $0 <project-root>" >&2
  exit 1
fi

PROJECT_ROOT="$1"
LAUNCHER_SCRIPT="$PROJECT_ROOT/scripts/launcher.py"
VENV_PYTHON3="$PROJECT_ROOT/.venv/bin/python3"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"

if [[ -x "$VENV_PYTHON3" ]]; then
  PYTHON_BIN="$VENV_PYTHON3"
elif [[ -x "$VENV_PYTHON" ]]; then
  PYTHON_BIN="$VENV_PYTHON"
else
  echo "Missing bootstrapped macOS venv: $PROJECT_ROOT/.venv" >&2
  exit 1
fi

if [[ ! -f "$LAUNCHER_SCRIPT" ]]; then
  echo "Missing launcher script: $LAUNCHER_SCRIPT" >&2
  exit 1
fi

mkdir -p "$PROJECT_ROOT/.runtime"

shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

quoted_project_root="$(shell_quote "$PROJECT_ROOT")"
quoted_python_bin="$(shell_quote "$PYTHON_BIN")"
quoted_launcher_script="$(shell_quote "$LAUNCHER_SCRIPT")"
launch_command="cd $quoted_project_root && nohup $quoted_python_bin $quoted_launcher_script --silent >/dev/null 2>&1 &"

if command -v osascript >/dev/null 2>&1; then
  escaped_launch_command="${launch_command//\\/\\\\}"
  escaped_launch_command="${escaped_launch_command//\"/\\\"}"
  osascript <<EOF >/dev/null
do shell script "$escaped_launch_command"
EOF
else
  /bin/sh -c "$launch_command"
fi
