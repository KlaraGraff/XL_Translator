# ADR 0003: Tauri shell with a Python engine sidecar

Date: 2026-07-16

## Status

Accepted

## Context

Translator's document fidelity depends on mature Python libraries and the existing
`core/` and `engines/` behavior. The PySide6 shell dominated the installed size
and could not directly reuse the completed HTML/CSS redesign prototype.

The assessment in `docs/refactor/2026-07-16_stack_refactor_assessment.md`
selected Route B: keep the Python engine intact and replace only the desktop
shell with Tauri 2 and a system WebView.

## Decision

- Tauri 2 owns the desktop window, single-instance activation, packaging and
  sidecar lifecycle.
- The UI is Vite plus vanilla TypeScript. React, Vue and another runtime UI
  framework are not part of this route.
- The Python engine is a FastAPI process bound to `127.0.0.1` on a random port.
  The Rust shell generates the one-launch token contract, and all API requests
  include that token. SSE carries task logs and progress.
- Release bundles include a PyInstaller onedir sidecar without PySide6. The
  sidecar is packaged as a Tauri resource so its colocated native dependencies
  remain resolvable on macOS and Windows.
- Existing app-data locations, settings migrations, `keys.json`, TM databases
  and model-configuration import/export remain the compatibility boundary.

## Consequences

`native_app/`, PySide6 dependencies and the Qt launcher/spec files are retired.
The product still has two executables at release time, so macOS signing and
notarization must cover both the Tauri app and the embedded sidecar. Windows
packages use NSIS with the WebView2 download bootstrapper.
