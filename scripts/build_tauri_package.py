"""Build a platform-native Tauri bundle with its frozen Python sidecar."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_TAURI = ROOT / "src-tauri"
UI = ROOT / "ui"
DIST_DIR = ROOT / "dist"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_tauri_sidecar import build_sidecar  # noqa: E402

SUPPORTED_PLATFORMS = {"macos", "windows"}

# `createUpdaterArtifacts` lives here rather than in tauri.conf.json on purpose.
# The config already carries the updater public key, and Tauri refuses to build
# ("a public key has been found, but no private key") the moment the flag is on
# without a signing key in the environment — which would break every local and
# every unsigned CI build. Passing it as a CLI override keeps update signing
# strictly opt-in for the release job that actually holds the key — the flag is
# also what makes the CLI sign the bundle at all (tauri-cli 2.11.4
# `get_bundle_settings`: no updater settings unless the flag is set, and
# `sign_updaters` returns early without them).
UPDATER_ARTIFACT_CONFIG = json.dumps({"bundle": {"createUpdaterArtifacts": True}})


def updater_signing_available() -> bool:
    """Whether this run holds the minisign key needed to sign update archives."""
    return bool(os.environ.get("TAURI_SIGNING_PRIVATE_KEY", "").strip())


def _export_github_env(values: dict[str, str]) -> None:
    github_env = os.environ.get("GITHUB_ENV")
    if not github_env:
        return
    with open(github_env, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def target_platform(raw: str) -> str:
    value = raw.strip().lower()
    if value == "current":
        system = platform.system()
        if system == "Darwin":
            value = "macos"
        elif system == "Windows":
            value = "windows"
        else:
            value = "unsupported"
    if value not in SUPPORTED_PLATFORMS:
        raise ValueError(
            "the release pipeline supports native macOS and Windows builds only"
        )
    system = platform.system()
    if value == "macos" and system != "Darwin":
        raise RuntimeError("a macOS bundle must be built on a native macOS host")
    if value == "windows" and system != "Windows":
        raise RuntimeError("a Windows bundle must be built on a native Windows host")
    return value


def tauri_cli() -> list[str]:
    executable = UI / "node_modules" / ".bin" / (
        "tauri.cmd" if sys.platform == "win32" else "tauri"
    )
    if not executable.is_file():
        raise FileNotFoundError(
            "Tauri CLI is unavailable. Run `npm ci` in ui/ before packaging."
        )
    if sys.platform == "win32":
        return ["cmd", "/c", str(executable)]
    return [str(executable)]


def package_windows_installer(*, dist_dir: Path = DIST_DIR) -> Path:
    """Rename the Tauri-built NSIS installer and write its sha256 sidecar.

    Windows has no local codesigning/notarization step in this pipeline, so
    (unlike macOS's separate shell packaging script) this one function does
    all of the post-build packaging: locate the single NSIS `.exe` Tauri
    produced, move it into `dist/` under the release asset name, and write a
    `<digest>  <filename>` sha256 file next to it.
    """
    from app_meta import windows_installer_basename

    nsis_dir = SRC_TAURI / "target" / "release" / "bundle" / "nsis"
    candidates = sorted(nsis_dir.glob("*.exe"))
    if not candidates:
        raise FileNotFoundError(f"No NSIS installer was produced under {nsis_dir}")
    if len(candidates) > 1:
        names = ", ".join(candidate.name for candidate in candidates)
        raise RuntimeError(f"Expected exactly one NSIS installer, found: {names}")
    built_installer = candidates[0]

    dist_dir.mkdir(parents=True, exist_ok=True)
    installer_name = f"{windows_installer_basename()}.exe"
    installer_path = dist_dir / installer_name
    if installer_path.exists():
        installer_path.unlink()
    shutil.copyfile(built_installer, installer_path)

    digest = hashlib.sha256(installer_path.read_bytes()).hexdigest()
    sha256_path = dist_dir / f"{installer_name}.sha256"
    # newline="\n" 固定 LF：Windows 上默认文本模式会把 \n 写成 \r\n，
    # 发布 job 在 macOS 用 `shasum -c` 交叉校验时会去找带 \r 的文件名而失败。
    sha256_path.write_text(f"{digest}  {installer_name}\n", encoding="utf-8", newline="\n")

    _export_github_env(
        {
            "WINDOWS_INSTALLER": f"dist/{installer_name}",
            "WINDOWS_INSTALLER_SHA256": f"dist/{installer_name}.sha256",
        }
    )

    return installer_path


def package_windows_updater_signature(*, dist_dir: Path = DIST_DIR) -> Path:
    """Stage the installer's minisign signature next to the installer itself.

    On Windows the in-app update artifact IS the NSIS setup `.exe` — Tauri v2
    calls it "self contained" and produces no separate archive. Confirmed in
    tauri-bundler 2.9.4 `bundle.rs`: with `createUpdaterArtifacts: true` (not
    `"v1Compatible"`) the zipping path only runs for `PackageType::MacOsBundle`,
    while `sign_updaters` in the CLI signs the NSIS bundle directly, leaving
    `<product>_<version>_x64-setup.exe.sig` beside it. The updater plugin sniffs
    the downloaded bytes and runs an `.exe` as an NSIS installer, so the exe we
    already publish for humans doubles as the update payload — it only needs its
    signature published alongside, under the same renamed basename.

    Must run after `package_windows_installer()`, whose copy of the exe is the
    file this signature belongs to (copying preserves the signed bytes).
    """
    from app_meta import windows_updater_artifact_name

    nsis_dir = SRC_TAURI / "target" / "release" / "bundle" / "nsis"
    candidates = sorted(nsis_dir.glob("*.exe.sig"))
    if not candidates:
        raise FileNotFoundError(
            f"The NSIS installer was not signed for updates: no *.exe.sig under {nsis_dir}"
        )
    if len(candidates) > 1:
        names = ", ".join(candidate.name for candidate in candidates)
        raise RuntimeError(f"Expected exactly one NSIS update signature, found: {names}")
    built_signature = candidates[0]
    if built_signature.stat().st_size == 0:
        raise RuntimeError(f"The NSIS update signature is empty: {built_signature}")

    dist_dir.mkdir(parents=True, exist_ok=True)
    artifact_name = windows_updater_artifact_name()
    installer_path = dist_dir / artifact_name
    if not installer_path.is_file():
        raise FileNotFoundError(
            f"{installer_path} is missing — package_windows_installer() must run first"
        )
    signature_path = dist_dir / f"{artifact_name}.sig"
    if signature_path.exists():
        signature_path.unlink()
    shutil.copyfile(built_signature, signature_path)

    _export_github_env({"WINDOWS_UPDATER_SIGNATURE": f"dist/{artifact_name}.sig"})
    return signature_path


def build_package(*, selected_platform: str, python: Path, skip_sidecar: bool = False) -> None:
    if not skip_sidecar:
        build_sidecar(python=python)
    if selected_platform == "macos":
        # No `createUpdaterArtifacts` here: Tauri would tar the app it just
        # built, which is the *unsigned* one. The macOS update archive is made
        # from the signed, notarized and stapled bundle by
        # scripts/build_macos_package.sh, after all of that has happened.
        subprocess.run(
            [*tauri_cli(), "build", "--bundles", "app"],
            cwd=SRC_TAURI,
            check=True,
        )
    elif selected_platform == "windows":
        sign_updates = updater_signing_available()
        command = [*tauri_cli(), "build", "--bundles", "nsis"]
        if sign_updates:
            command += ["--config", UPDATER_ARTIFACT_CONFIG]
        else:
            print(
                "[WARN] TAURI_SIGNING_PRIVATE_KEY is unset: building the installer only, "
                "without its in-app update signature.",
                file=sys.stderr,
            )
        subprocess.run(command, cwd=SRC_TAURI, check=True)
        package_windows_installer()
        if sign_updates:
            package_windows_updater_signature()
    else:
        raise ValueError(f"unsupported platform: {selected_platform}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default="current")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--skip-sidecar", action="store_true")
    args = parser.parse_args()
    build_package(
        selected_platform=target_platform(args.platform),
        # Do not resolve the virtualenv interpreter symlink: that discards the
        # venv context when the build helper invokes PyInstaller.
        python=args.python.absolute(),
        skip_sidecar=args.skip_sidecar,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
