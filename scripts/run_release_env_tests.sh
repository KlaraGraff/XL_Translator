#!/bin/bash
# 在本地复刻发布构建机的条件跑一遍全量测试。
#
# 平时开发用 .venv（解释器版本随手边的为准）；这个脚本另外维护一个 .venv311，
# 用 Python 3.11 加 constraints-release-py311.txt 里锁死的依赖——和 CI 装的是
# 同一批。依赖清单直接复用 install_macos_dependencies.sh，不在这里抄第二份。
#
# 首次运行要装依赖，约一两分钟；之后只要 constraints 没动就直接跑，十几秒。
# 想强制重建：rm -rf .venv311
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv311"
CONSTRAINTS="$ROOT_DIR/constraints-release-py311.txt"
STAMP="$VENV_DIR/.deps-stamp"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "[跳过] 这个脚本依赖 install_macos_dependencies.sh，目前只在 macOS 上可用。" >&2
  exit 0
fi

resolve_python311() {
  if [[ -n "${PYTHON311:-}" && -x "${PYTHON311:-}" ]]; then
    echo "$PYTHON311"
    return 0
  fi
  for candidate in \
    "$(command -v python3.11 2>/dev/null || true)" \
    "$HOME/.local/bin/python3.11" \
    "/opt/homebrew/opt/python@3.11/bin/python3.11"
  do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      echo "$candidate"
      return 0
    fi
  done
  # uv 管理的解释器不一定在 PATH 上
  if command -v uv >/dev/null 2>&1; then
    local found
    found="$(uv python find 3.11 2>/dev/null || true)"
    if [[ -n "$found" && -x "$found" ]]; then
      echo "$found"
      return 0
    fi
  fi
  return 1
}

if ! PYTHON311="$(resolve_python311)"; then
  cat >&2 <<'EOS'
[失败] 找不到 Python 3.11——发布构建机用的就是这个版本，本地不跑就等于没检查。
       装一个：uv python install 3.11
       或者指定：PYTHON311=/path/to/python3.11 bash scripts/run_release_env_tests.sh
EOS
  exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python3" ]]; then
  echo "[信息] 创建 $VENV_DIR（$("$PYTHON311" -V 2>&1)）"
  "$PYTHON311" -m venv "$VENV_DIR"
  rm -f "$STAMP"
fi

# constraints 变了就重装，否则跳过——这是首次之后能压到十几秒的关键。
if [[ ! -f "$STAMP" || "$CONSTRAINTS" -nt "$STAMP" ]]; then
  echo "[信息] 按 constraints-release-py311.txt 同步发布依赖"
  PYTHON_BIN="$VENV_DIR/bin/python3" bash "$ROOT_DIR/scripts/install_macos_dependencies.sh"
  touch "$STAMP"
fi

# 绝不能碰用户真实的应用数据目录：测试会写设置、密钥和翻译记忆库。
APP_DATA_DIR="$(mktemp -d)"
trap 'rm -rf "$APP_DATA_DIR"' EXIT

echo "[信息] 用 $("$VENV_DIR/bin/python3" -V 2>&1) 跑全量测试（模拟构建机上没有 Microsoft Excel）"
cd "$ROOT_DIR"
TRANSLATOR_APP_DATA_DIR="$APP_DATA_DIR" "$VENV_DIR/bin/python3" scripts/release_env_tests.py
