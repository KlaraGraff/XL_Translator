"""``python -m cli …`` from source; the frozen sidecar enters through scripts/launch_sidecar.py."""

from cli.main import main

raise SystemExit(main())
