"""Best-effort translation metadata stored on generated files.

The metadata is deliberately kept outside the document package.  Excel, Word,
PDF and CAD all have different internal property systems, while macOS/Linux
extended attributes and Windows alternate data streams provide one consistent
file-level place for the same small JSON record.  A metadata failure never
turns a successfully published translation into a failed task.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

XATTR_NAME = "com.klara-graff.xl-translator"
XATTR_FALLBACK_NAME = "user.xl_translator"
WINDOWS_STREAM_SUFFIX = ":xl_translator_metadata:$DATA"
SCHEMA_VERSION = 1


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items() if str(key).lower() not in {"api_key", "key", "secret", "token", "password"}}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def build_translation_metadata(
    *,
    surface: str,
    source_path: str | Path,
    target_lang: str,
    source_lang: str | None = None,
    parameters: dict[str, Any] | None = None,
    output_mode: str = "task_folder",
    fallback_used: str = "",
) -> dict[str, Any]:
    """Create a safe, JSON-compatible record without credentials."""
    return {
        "schema_version": SCHEMA_VERSION,
        "application": "XL_Translator",
        "surface": str(surface),
        "source_name": Path(source_path).name,
        "source_path": str(Path(source_path).expanduser()),
        "source_lang": str(source_lang or "auto"),
        "target_lang": str(target_lang or ""),
        "output_mode": str(output_mode or "task_folder"),
        "fallback_used": str(fallback_used or ""),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "parameters": _clean(parameters or {}),
    }


def _payload(metadata: dict[str, Any]) -> bytes:
    return json.dumps(_clean(metadata), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _windows_stream(path: Path) -> Path:
    return Path(f"{path}{WINDOWS_STREAM_SUFFIX}")


def _macos_xattr_write(path: Path, name: str, payload: bytes) -> bool:
    """Use the system xattr tool when the bundled Python lacks os.setxattr."""
    if os.name != "posix" or shutil.which("xattr") is None:
        return False
    try:
        subprocess.run(
            ["xattr", "-wx", name, payload.hex(), str(path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _macos_xattr_read(path: Path, name: str) -> bytes | None:
    if os.name != "posix" or shutil.which("xattr") is None:
        return None
    try:
        result = subprocess.run(
            ["xattr", "-px", name, str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return bytes.fromhex("".join(result.stdout.split()))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def write_translation_metadata(path: str | Path, metadata: dict[str, Any]) -> bool:
    """Write metadata after publication; return False when the filesystem declines it."""
    target = Path(path)
    try:
        payload = _payload(metadata)
        if hasattr(os, "setxattr"):
            for name in (XATTR_NAME, XATTR_FALLBACK_NAME):
                try:
                    os.setxattr(str(target), name, payload)
                    return True
                except OSError:
                    continue
        if os.name == "nt":
            stream = _windows_stream(target)
            with stream.open("wb") as handle:
                handle.write(payload)
            return True
        for name in (XATTR_NAME, XATTR_FALLBACK_NAME):
            if _macos_xattr_write(target, name, payload):
                return True
        return False
    except (OSError, ValueError, TypeError):
        return False
    return False


def read_translation_metadata(path: str | Path) -> dict[str, Any] | None:
    """Read a generated file's metadata, returning None for absent/invalid data."""
    target = Path(path)
    try:
        if hasattr(os, "getxattr"):
            payload = None
            for name in (XATTR_NAME, XATTR_FALLBACK_NAME):
                try:
                    payload = os.getxattr(str(target), name)
                    break
                except OSError:
                    continue
            if payload is None:
                for name in (XATTR_NAME, XATTR_FALLBACK_NAME):
                    payload = _macos_xattr_read(target, name)
                    if payload is not None:
                        break
                if payload is None:
                    return None
        elif os.name == "nt":
            payload = _windows_stream(target).read_bytes()
        else:
            for name in (XATTR_NAME, XATTR_FALLBACK_NAME):
                payload = _macos_xattr_read(target, name)
                if payload is not None:
                    break
            if payload is None:
                return None
        value = json.loads(payload.decode("utf-8"))
        return value if isinstance(value, dict) and value.get("application") == "XL_Translator" else None
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None


def is_translation_output(path: str | Path, *, surface: str | None = None) -> bool:
    """Return whether a file carries XL_Translator's generated-file marker.

    Source-folder output is deliberately written beside the source, so a
    recursive scan cannot rely on the ``*_翻译输出_*`` directory marker. The
    file attribute is the reliable cross-format marker; callers can provide a
    surface when a format-specific scan must avoid another surface's output.
    Missing or unreadable attributes are treated as ``False`` so scanning
    remains best-effort on filesystems that do not support extended metadata.
    """
    metadata = read_translation_metadata(path)
    if metadata is None:
        return False
    if surface and str(metadata.get("surface") or "") != str(surface):
        return False
    return True


def metadata_parameters(settings: Any, surface: str) -> dict[str, Any]:
    """Capture only the selected surface's settings, excluding secrets and local paths."""
    field = f"{surface}_output"
    values: dict[str, Any] = {}
    section = getattr(settings, field, None)
    if section is not None and hasattr(section, "model_dump"):
        values["output"] = section.model_dump(mode="json")
    for name in (
        f"{surface}_source_lang",
        f"{surface}_target_lang",
        f"{surface}_domain_preset",
    ):
        if hasattr(settings, name):
            values[name] = getattr(settings, name)
    if surface == "pdf" and hasattr(settings, "pdf"):
        values["pdf"] = settings.pdf.model_dump(mode="json")
    cleaned = _clean(values)
    # The record travels with the output file and may be opened on another
    # machine.  Keep functional choices, but never persist local folder paths.
    output = cleaned.get("output") if isinstance(cleaned, dict) else None
    if isinstance(output, dict):
        for key in ("custom_output_dir", "fallback_output_dir"):
            output.pop(key, None)
    return cleaned
