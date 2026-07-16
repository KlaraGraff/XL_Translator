"""Entrypoint used by the frozen Python translation-engine sidecar."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run() -> int:
    if "--smoke-test" in sys.argv[1:]:
        from scripts.frozen_smoke import run_smoke_test

        return run_smoke_test()

    from api.launcher import main

    main()
    return 0


if __name__ == "__main__":
    raise SystemExit(_run())
