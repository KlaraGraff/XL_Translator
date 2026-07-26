"""Test package hook: a test run must never write to the real user data directory.

``config`` resolves ``APP_DATA_DIR`` once at import time, from
``TRANSLATOR_APP_DATA_DIR`` or the platform default.  Isolating individual
modules stays the rule (AGENTS.md #4), but a module that forgets it writes task
history, app.log and tm.db straight into the developer's own installation — that
is how fixture tasks ended up showing as phantom results in a shipped build.  CI
already exports the variable; this makes a local ``unittest discover -s tests``
just as safe, and a runner that sets its own directory still wins.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

if not os.environ.get("TRANSLATOR_APP_DATA_DIR"):
    _ISOLATED_APP_DATA_DIR = tempfile.mkdtemp(prefix="translator-tests-app-data-")
    os.environ["TRANSLATOR_APP_DATA_DIR"] = _ISOLATED_APP_DATA_DIR
    atexit.register(shutil.rmtree, _ISOLATED_APP_DATA_DIR, ignore_errors=True)
