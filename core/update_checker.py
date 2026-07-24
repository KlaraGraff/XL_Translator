"""macOS-only, architecture-safe checks against official GitHub releases."""

from __future__ import annotations

import platform
import re
from dataclasses import dataclass, replace
from typing import Any

import httpx

from app_meta import APP_SAFE_NAME, APP_UPDATE_USER_AGENT, APP_VERSION


GITHUB_REPO = "KlaraGraff/XL_Translator"
LATEST_RELEASE_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
LATEST_RELEASE_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
DEFAULT_TIMEOUT_SECONDS = 6.0
_STABLE_TAG_RE = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$", re.IGNORECASE)
_SHA256_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", re.IGNORECASE)


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    download_url: str
    digest: str = ""


@dataclass(frozen=True)
class UpdateCheckResult:
    ok: bool
    status: str
    message: str
    current_version: str = APP_VERSION
    latest_version: str = ""
    latest_tag: str = ""
    release_url: str = LATEST_RELEASE_PAGE_URL
    release_date: str = ""
    asset_name: str = ""
    download_url: str = ""
    sha256: str = ""
    checksum_asset_name: str = ""
    checksum_url: str = ""
    architecture: str = ""
    release_notes: str = ""
    diagnostic_code: str = ""

    @property
    def has_update(self) -> bool:
        return self.status == "available"


def _version_key(version: str) -> tuple[int, ...]:
    parts = _parse_version_parts(version)
    if parts is None:
        return ()
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _parse_version_parts(version: str) -> list[int] | None:
    normalized = str(version or "").strip()
    normalized = normalized[1:] if normalized.lower().startswith("v") else normalized
    release_part = re.split(r"[-+]", normalized, maxsplit=1)[0]
    if not release_part or not re.fullmatch(r"\d+(?:\.\d+)*", release_part):
        return None
    return [int(part) for part in release_part.split(".")]


def is_newer_version(latest_version: str, current_version: str = APP_VERSION) -> bool:
    latest_key = _version_key(latest_version)
    current_key = _version_key(current_version)
    return bool(latest_key and current_key and latest_key > current_key)


def major_version(version: str) -> int | None:
    parts = _parse_version_parts(version)
    return parts[0] if parts else None


def is_major_upgrade(latest_version: str, current_version: str = APP_VERSION) -> bool:
    latest_major = major_version(latest_version)
    current_major = major_version(current_version)
    if latest_major is None or current_major is None:
        return False
    return latest_major > current_major


def normalized_architecture(
    machine: str | None = None,
    *,
    platform_name: str | None = None,
) -> str | None:
    """Return the release architecture label for a supported macOS host."""
    if (platform_name or platform.system()).lower() != "darwin":
        return None
    raw = str(machine or platform.machine()).strip().lower()
    if raw in {"arm64", "aarch64"}:
        return "arm64"
    if raw in {"x86_64", "amd64", "x64"}:
        return "x86_64"
    return None


def _public_asset_architecture(architecture: str) -> str:
    return "x64" if architecture == "x86_64" else architecture


def _expected_dmg_name(version: str, architecture: str) -> str:
    return f"{APP_SAFE_NAME}_macOS_{_public_asset_architecture(architecture)}_{version}.dmg"


def _select_platform_asset(
    assets: list[ReleaseAsset],
    *,
    version: str,
    platform_name: str | None = None,
    machine: str | None = None,
) -> ReleaseAsset | None:
    """Choose exactly one native DMG; never fall back to an arbitrary asset."""
    architecture = normalized_architecture(machine, platform_name=platform_name)
    if architecture is None:
        return None
    expected = _expected_dmg_name(version, architecture).lower()
    candidates = [asset for asset in assets if asset.name.lower() == expected]
    return candidates[0] if len(candidates) == 1 else None


def _select_checksum_asset(
    assets: list[ReleaseAsset],
    dmg_asset: ReleaseAsset,
) -> ReleaseAsset | None:
    expected = f"{dmg_asset.name}.sha256".lower()
    matches = [asset for asset in assets if asset.name.lower() == expected]
    return matches[0] if len(matches) == 1 else None


def _parse_release_assets(payload: dict[str, Any]) -> list[ReleaseAsset]:
    assets: list[ReleaseAsset] = []
    for raw_asset in payload.get("assets") or []:
        if not isinstance(raw_asset, dict):
            continue
        name = str(raw_asset.get("name") or "").strip()
        download_url = str(raw_asset.get("browser_download_url") or "").strip()
        digest = str(raw_asset.get("digest") or "").strip()
        if name and download_url:
            assets.append(ReleaseAsset(name=name, download_url=download_url, digest=digest))
    return assets


def _sha256_from_digest(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized.split(":", 1)[1]
    return normalized if re.fullmatch(r"[0-9a-f]{64}", normalized) else ""


def _read_checksum(content: str, *, asset_name: str) -> str:
    """Accept a checksum only when a sidecar clearly belongs to this DMG."""
    matches = _SHA256_RE.findall(str(content or ""))
    if len(matches) != 1:
        return ""
    # A conventional sidecar can contain only the digest, or "digest  name".
    tail = str(content or "").strip()[64:].strip(" *\t")
    if tail and tail != asset_name:
        return ""
    return matches[0].lower()


def _stable_release_version(payload: dict[str, Any]) -> tuple[str, str] | None:
    tag_name = str(payload.get("tag_name") or "").strip()
    match = _STABLE_TAG_RE.fullmatch(tag_name)
    if not match or bool(payload.get("draft")) or bool(payload.get("prerelease")):
        return None
    return tag_name, match.group("version")


def build_update_result_from_release_payload(
    payload: dict[str, Any],
    *,
    current_version: str = APP_VERSION,
    platform_name: str | None = None,
    machine: str | None = None,
) -> UpdateCheckResult:
    """Validate published release metadata before any update is presented."""
    release_url = str(payload.get("html_url") or LATEST_RELEASE_PAGE_URL).strip()
    release_notes = str(payload.get("body") or "").strip()
    release_date = str(payload.get("published_at") or "").strip()
    stable = _stable_release_version(payload)
    if stable is None:
        return UpdateCheckResult(
            ok=True,
            status="unknown",
            message="无法确认正式 Release 版本，已跳过更新提示。",
            current_version=current_version,
            release_url=release_url,
            diagnostic_code="release_tag_invalid",
        )
    tag_name, latest_version = stable
    architecture = normalized_architecture(machine, platform_name=platform_name)
    if architecture is None:
        return UpdateCheckResult(
            ok=True,
            status="unsupported_platform",
            message="仅 macOS 原生版本支持检查更新。",
            current_version=current_version,
            latest_version=latest_version,
            latest_tag=tag_name,
            release_url=release_url,
            release_date=release_date,
            release_notes=release_notes,
            diagnostic_code="unsupported_platform",
        )
    if _parse_version_parts(current_version) is None:
        return UpdateCheckResult(
            ok=True,
            status="unknown",
            message="当前版本格式异常，已跳过更新提示。",
            current_version=current_version,
            latest_version=latest_version,
            latest_tag=tag_name,
            release_url=release_url,
            release_date=release_date,
            release_notes=release_notes,
            architecture=architecture,
            diagnostic_code="current_version_invalid",
        )

    assets = _parse_release_assets(payload)
    dmg_asset = _select_platform_asset(
        assets,
        version=latest_version,
        platform_name=platform_name,
        machine=machine,
    )
    checksum_asset = _select_checksum_asset(assets, dmg_asset) if dmg_asset else None
    if dmg_asset is None or checksum_asset is None:
        return UpdateCheckResult(
            ok=True,
            status="release_not_ready",
            message="该架构的正式发布包尚未就绪。",
            current_version=current_version,
            latest_version=latest_version,
            latest_tag=tag_name,
            release_url=release_url,
            release_date=release_date,
            architecture=architecture,
            release_notes=release_notes,
            diagnostic_code="native_asset_or_checksum_missing",
        )

    checksum = _sha256_from_digest(dmg_asset.digest)
    base = UpdateCheckResult(
        ok=True,
        status="available" if is_newer_version(latest_version, current_version) else "current",
        message=(
            f"发现新版 V{latest_version}。"
            if is_newer_version(latest_version, current_version)
            else f"当前已是最新版本 V{current_version}。"
        ),
        current_version=current_version,
        latest_version=latest_version,
        latest_tag=tag_name,
        release_url=release_url,
        release_date=release_date,
        asset_name=dmg_asset.name,
        download_url=dmg_asset.download_url,
        sha256=checksum,
        checksum_asset_name=checksum_asset.name,
        checksum_url=checksum_asset.download_url,
        architecture=architecture,
        release_notes=release_notes,
    )
    # The GitHub asset API may carry a SHA-256 digest.  If it does not, the
    # caller will retrieve the required published sidecar before surfacing it.
    return base


def _fetch_checksum(client: httpx.Client, result: UpdateCheckResult) -> UpdateCheckResult:
    if result.status not in {"available", "current"} or result.sha256:
        return result
    try:
        response = client.get(result.checksum_url)
        response.raise_for_status()
        checksum = _read_checksum(response.text, asset_name=result.asset_name)
    except Exception as exc:  # noqa: BLE001 - keep network details out of UI payloads.
        return replace(
            result,
            ok=False,
            status="error",
            message="检查更新失败，请稍后重试。",
            diagnostic_code=f"checksum_request_{type(exc).__name__.lower()}",
            asset_name="",
            download_url="",
            sha256="",
        )
    if not checksum:
        return replace(
            result,
            ok=True,
            status="release_not_ready",
            message="该架构的正式发布包尚未就绪。",
            diagnostic_code="checksum_invalid",
            asset_name="",
            download_url="",
            sha256="",
        )
    return replace(result, sha256=checksum)


def check_for_updates(
    *,
    current_version: str = APP_VERSION,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    platform_name: str | None = None,
    machine: str | None = None,
) -> UpdateCheckResult:
    """Check one stable release and expose only a complete native DMG."""
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.get(
                LATEST_RELEASE_API_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": APP_UPDATE_USER_AGENT,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("release_payload_invalid")
            result = build_update_result_from_release_payload(
                payload,
                current_version=current_version,
                platform_name=platform_name,
                machine=machine,
            )
            return _fetch_checksum(client, result)
    except Exception as exc:  # noqa: BLE001 - UI receives a concise safe state.
        return UpdateCheckResult(
            ok=False,
            status="error",
            message="检查更新失败，请稍后重试。",
            current_version=current_version,
            diagnostic_code=f"release_request_{type(exc).__name__.lower()}",
        )
