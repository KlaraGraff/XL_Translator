# ADR 0001: Native-only mainline

Date: 2026-05-24

## Status

Accepted

## Context

The project has completed the main UI migration to the PySide6 native desktop
interface. Keeping the legacy Streamlit/web-wrapper path in the same mainline
adds duplicate launchers, duplicate UI documentation, extra dependencies, and
extra test paths that no longer represent the product direction.

The legacy web-wrapper release remains available from older GitHub Releases,
with V4.6 documented as the last line for users who still need that route.

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
