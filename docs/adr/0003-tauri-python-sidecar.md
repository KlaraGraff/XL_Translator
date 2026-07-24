# ADR 0003: Tauri shell with a Python engine sidecar

Date: 2026-07-16

## Status

Accepted, amended 2026-07-24 by the final functional-migration decisions.

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
  remain resolvable on macOS.
- The current release baseline is macOS 12.0+ only. It produces separate native
  arm64 and Intel x64 DMGs, with Python 3.11, a macOS 12 deployment target,
  full Mach-O scans, Developer ID signing, notarization and Gatekeeper checks.
- This release starts a new application-data baseline. It does not read,
  migrate, repair or delete data from earlier releases; subsequent releases
  must explicitly protect and migrate this new baseline forward.

## Consequences

`native_app/`, PySide6 dependencies and the Qt launcher/spec files are retired.
The product still has two executables at release time, so macOS signing and
notarization cover both the Tauri app and the embedded sidecar. Apple Events
automation is declared in the app usage description and signing entitlements.
