# ADR 0001: Native-only mainline (historical)

Date: 2026-05-24

Updated: 2026-05-25 for the V5.0 native release line.

## Status

Superseded by ADR 0003 (2026-07-16)

> Historical record: this decision governed the V5 PySide6 release line. It
> was superseded by the Tauri + Python sidecar architecture in ADR 0003; the
> legacy paths named below no longer exist on the maintained V8 mainline.

## Context

The project has completed the main UI migration to the PySide6 native desktop
interface. V5.0 is the first major native-only release line. Keeping the legacy
Streamlit/web-wrapper path in the same mainline
adds duplicate launchers, duplicate UI documentation, extra dependencies, and
extra test paths that no longer represent the product direction.

The legacy web-wrapper release remains available from older GitHub Releases,
with V4.6 documented as the last line for users who still need that route.
That path is historical context only and is not a maintained launch route in
V5.0 or later.

## Decision

The mainline keeps only the native desktop route:

- `native_app/` is the maintained UI surface.
- `scripts/launch_native.py` is the application entry used by source launchers
  and native packaging.
- Excel, Word, translation memory, engine dispatch, settings, diagnostics, and
  headless translation helpers remain in the shared core modules.
- Streamlit pages, AppTest wrappers, WebView launchers, and Streamlit runtime
  dependencies are removed from the maintained mainline.

## Consequences

Users cannot launch the old browser/Streamlit UI from this branch. That is an
intentional cleanup, not a regression of the native app.

The install footprint is smaller because Streamlit, streamlit-extras, and
pywebview are no longer runtime dependencies. Future UI work should target the
PySide6 native interface and be tested with native/offscreen UI checks.
