#!/usr/bin/env python3
"""Check or prepare the local Translator runtime for the skill."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


REQUIRED_ENTRYPOINTS = (
    "scripts/translate_excel_cli.py",
    "scripts/translate_word_cli.py",
    "scripts/translate_pdf_cli.py",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the Translator direct runtime.")
    parser.add_argument("--repo-root", help="Translator repository root.")
    parser.add_argument(
        "--install-deps",
        action="store_true",
        help="Create .venv if needed and install requirements.txt.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON status.")
    args = parser.parse_args(argv)

    repo_root = _find_repo_root(args.repo_root)
    payload = {
        "ok": False,
        "repo_root": str(repo_root) if repo_root else "",
        "python": "",
        "missing": [],
        "actions": [],
    }
    if repo_root is None:
        payload["missing"].append("translator_repository")
        payload["actions"].append("Set TRANSLATOR_REPO_ROOT or run from the Translator repository.")
        return _finish(payload, args.json, exit_code=2)

    missing_entries = [
        rel_path for rel_path in REQUIRED_ENTRYPOINTS if not (repo_root / rel_path).exists()
    ]
    payload["missing"].extend(missing_entries)
    if missing_entries:
        payload["actions"].append("Update the repository; required headless CLI files are missing.")
        return _finish(payload, args.json, exit_code=2)

    python_path = _project_python(repo_root)
    if args.install_deps:
        _ensure_venv(repo_root)
        python_path = _project_python(repo_root)
        subprocess.check_call(
            [str(python_path), "-m", "pip", "install", "-r", "requirements.txt"],
            cwd=repo_root,
        )

    payload["python"] = str(python_path)
    if not python_path.exists() and python_path != Path(sys.executable):
        payload["missing"].append("project_python")
        payload["actions"].append("Run bootstrap.py --install-deps or create .venv manually.")
        return _finish(payload, args.json, exit_code=2)

    payload["ok"] = True
    return _finish(payload, args.json, exit_code=0)


def _find_repo_root(raw_root: str | None) -> Path | None:
    candidates: list[Path] = []
    if raw_root:
        candidates.append(Path(raw_root).expanduser())
    env_value = str(__import__("os").environ.get("TRANSLATOR_REPO_ROOT") or "").strip()
    if env_value:
        candidates.append(Path(env_value).expanduser())

    here = Path(__file__).resolve()
    candidates.extend([Path.cwd(), *Path.cwd().parents, *here.parents])
    for candidate in candidates:
        if not candidate:
            continue
        root = candidate.resolve()
        if all((root / rel_path).exists() for rel_path in REQUIRED_ENTRYPOINTS):
            return root
    return None


def _project_python(repo_root: Path) -> Path:
    unix_python = repo_root / ".venv" / "bin" / "python3"
    if unix_python.exists():
        return unix_python
    win_python = repo_root / ".venv" / "Scripts" / "python.exe"
    if win_python.exists():
        return win_python
    return Path(sys.executable)


def _ensure_venv(repo_root: Path) -> None:
    venv_dir = repo_root / ".venv"
    if venv_dir.exists():
        return
    subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)], cwd=repo_root)


def _finish(payload: dict[str, object], as_json: bool, *, exit_code: int) -> int:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif payload.get("ok"):
        print(f"Translator runtime ready: {payload['repo_root']}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
