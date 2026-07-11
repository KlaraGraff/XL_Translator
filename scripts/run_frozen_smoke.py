"""Run a frozen executable smoke test with a timeout and isolated app data."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    if not args.executable.is_file():
        print(f"[ERROR] Frozen executable not found: {args.executable}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="translator-frozen-smoke-") as app_data:
        environment = os.environ.copy()
        environment["TRANSLATOR_APP_DATA_DIR"] = app_data
        environment["QT_QPA_PLATFORM"] = "offscreen"
        try:
            result = subprocess.run(
                [str(args.executable), "--smoke-test"],
                check=False,
                capture_output=True,
                text=True,
                timeout=args.timeout,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            print(
                f"[ERROR] Frozen smoke test exceeded {args.timeout:g} seconds",
                file=sys.stderr,
            )
            return 1

        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if result.returncode != 0:
            print(
                f"[ERROR] Frozen smoke test exited with code {result.returncode}",
                file=sys.stderr,
            )
            return 1

        app_data_entries = list(Path(app_data).iterdir())
        if app_data_entries:
            names = ", ".join(sorted(path.name for path in app_data_entries))
            print(
                f"[ERROR] Smoke test wrote to application data: {names}",
                file=sys.stderr,
            )
            return 1

    print("[INFO] Frozen executable exited cleanly without touching application data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
