#!/usr/bin/env python3
"""Copy the current user's Translator config into an isolated test app-data dir."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

CONFIG_FILES = ("settings.json", "keys.json", "key_origins.json")


def formal_app_data_dir() -> Path:
    # app_paths intentionally honours the test override, so hide it while
    # resolving the real user's platform-native location.
    override = os.environ.pop("TRANSLATOR_APP_DATA_DIR", None)
    try:
        from core.app_paths import get_app_data_dir

        return get_app_data_dir().expanduser().resolve()
    finally:
        if override is not None:
            os.environ["TRANSLATOR_APP_DATA_DIR"] = override


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, type=Path, help="isolated test app-data directory")
    parser.add_argument("--source", type=Path, help="formal app-data directory (defaults to current user config)")
    args = parser.parse_args()

    source = (args.source or formal_app_data_dir()).expanduser().resolve()
    target = args.target.expanduser().resolve()
    if source == target:
        parser.error("source and target must be different directories")
    if not (source / "settings.json").is_file():
        raise FileNotFoundError("current user settings are missing")
    if any((target / name).is_symlink() for name in (*CONFIG_FILES, ".config-backups")):
        raise ValueError("test configuration paths must not be symbolic links")

    payloads: dict[str, bytes] = {}
    for name in CONFIG_FILES:
        path = source / name
        if path.exists():
            raw = path.read_bytes()
            if not isinstance(json.loads(raw.decode("utf-8")), dict):
                raise ValueError("configuration must be a JSON object")
            payloads[name] = raw

    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.chmod(0o700)
    previous = [name for name in CONFIG_FILES if (target / name).exists()]
    if previous:
        backup_root = target / ".config-backups"
        backup_root.mkdir(mode=0o700, exist_ok=True)
        backup_root.chmod(0o700)
        backup = backup_root / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup.mkdir(parents=True, mode=0o700)
        for name in previous:
            shutil.copy2(target / name, backup / name)
            (backup / name).chmod(0o600)

    for name in CONFIG_FILES:
        destination = target / name
        if name in payloads:
            temporary = destination.with_name(f".{name}.{os.getpid()}.tmp")
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payloads[name])
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            destination.unlink(missing_ok=True)

    print(f"Copied {len(payloads)} config file(s): {', '.join(payloads) if payloads else 'none'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Config seeding failed ({type(exc).__name__}).", file=sys.stderr)
        raise SystemExit(1)
