"""在发版构建时把真实的模型配置加密私钥写进 core/_embedded_key.py。

私钥绝不能写进源码，所以开发分支上 core/_embedded_key.py 里放的是一把公开
的开发用兜底密钥（EMBEDDED_KEY_ID == "dev"，见该文件顶部注释）。这个脚本
只在 CI 的构建 job 里跑，从加密的 GitHub Actions secret 读出真实密钥，重写
同一个文件，让打包出来的产物携带真实密钥而不是开发密钥。

打包之前必须跑这一步；`scripts/verify_release_metadata.py` 会在发布产物里
校验 EMBEDDED_KEY_ID 不是 "dev"，兜底防止这一步被漏掉。
"""

from __future__ import annotations

import base64
import binascii
import os
import sys
from pathlib import Path

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
TARGET_PATH = ROOT / "core" / "_embedded_key.py"

_DEV_KEY_ID = "dev"

_MODULE_TEMPLATE = '''"""内置的模型配置加密私钥。

这个文件由 scripts/inject_embedded_key.py 在发版构建时从 GitHub Actions 的
加密 secret 注入生成，取代了源码里那把公开的开发用兜底密钥。不要手工编辑
——下一次运行注入脚本会整份覆盖掉。
"""

from __future__ import annotations

EMBEDDED_KEY_ID = {key_id!r}
EMBEDDED_PRIVATE_KEY_B64 = {key_b64!r}
'''


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"[ERROR] 缺少环境变量 {name}，无法注入内置密钥。", file=sys.stderr)
        sys.exit(1)
    return value


def _validate_private_key(key_b64: str) -> None:
    try:
        raw = base64.b64decode(key_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        print(f"[ERROR] XLT_EMBEDDED_PRIVATE_KEY_B64 不是合法的 base64: {exc}", file=sys.stderr)
        sys.exit(1)

    if len(raw) != 32:
        print(
            f"[ERROR] XLT_EMBEDDED_PRIVATE_KEY_B64 解码后应为 32 字节，实际为 {len(raw)} 字节。",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        X25519PrivateKey.from_private_bytes(raw)
    except (InvalidKey, ValueError) as exc:
        print(f"[ERROR] XLT_EMBEDDED_PRIVATE_KEY_B64 不是合法的 X25519 私钥: {exc}", file=sys.stderr)
        sys.exit(1)


def inject(key_id: str, key_b64: str, *, target_path: Path = TARGET_PATH) -> None:
    if key_id == _DEV_KEY_ID:
        print(
            f'[ERROR] XLT_EMBEDDED_KEY_ID 不能是 "{_DEV_KEY_ID}"——那是开发兜底密钥的标记，'
            "正式发布必须用一个真实的密钥 ID。",
            file=sys.stderr,
        )
        sys.exit(1)

    _validate_private_key(key_b64)

    target_path.write_text(
        _MODULE_TEMPLATE.format(key_id=key_id, key_b64=key_b64), encoding="utf-8"
    )
    print(f"injected embedded key id={key_id}")


def main() -> int:
    key_id = _require_env("XLT_EMBEDDED_KEY_ID")
    key_b64 = _require_env("XLT_EMBEDDED_PRIVATE_KEY_B64")
    inject(key_id, key_b64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
