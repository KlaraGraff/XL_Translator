"""Version update checks against the project's GitHub releases."""
from __future__ import annotations

import platform
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app_meta import APP_UPDATE_USER_AGENT, APP_VERSION


# Keep the existing release repository until the GitHub repo itself is renamed.
GITHUB_REPO = "KlaraGraff/XL_Translator"
LATEST_RELEASE_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
LATEST_RELEASE_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
DEFAULT_TIMEOUT_SECONDS = 6.0


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    download_url: str


@dataclass(frozen=True)
class UpdateCheckResult:
    ok: bool
    status: str
    message: str
    current_version: str = APP_VERSION
    latest_version: str = ""
    latest_tag: str = ""
    release_url: str = LATEST_RELEASE_PAGE_URL
    asset_name: str = ""
    download_url: str = ""
    release_notes: str = ""

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


def _strip_tag_prefix(tag_name: str) -> str:
    value = str(tag_name or "").strip()
    return value[1:] if value.lower().startswith("v") else value


def _select_platform_asset(
    assets: list[ReleaseAsset],
    *,
    platform_name: str | None = None,
) -> ReleaseAsset | None:
    system = (platform_name or platform.system()).lower()
    candidates: list[ReleaseAsset] = []

    if system == "darwin":
        candidates = [
            asset
            for asset in assets
            if "macos" in asset.name.lower() and asset.name.lower().endswith(".dmg")
        ]
    elif system == "windows":
        candidates = [
            asset
            for asset in assets
            if "windows" in asset.name.lower()
            and asset.name.lower().endswith("_setup.exe")
        ]

    if candidates:
        return candidates[0]
    return assets[0] if assets else None


def _parse_release_assets(payload: dict[str, Any]) -> list[ReleaseAsset]:
    assets: list[ReleaseAsset] = []
    for raw_asset in payload.get("assets") or []:
        if not isinstance(raw_asset, dict):
            continue
        name = str(raw_asset.get("name") or "").strip()
        download_url = str(raw_asset.get("browser_download_url") or "").strip()
        if name and download_url:
            assets.append(ReleaseAsset(name=name, download_url=download_url))
    return assets


def build_update_result_from_release_payload(
    payload: dict[str, Any],
    *,
    current_version: str = APP_VERSION,
    platform_name: str | None = None,
) -> UpdateCheckResult:
    tag_name = str(payload.get("tag_name") or "").strip()
    latest_version = _strip_tag_prefix(tag_name)
    release_url = str(payload.get("html_url") or LATEST_RELEASE_PAGE_URL).strip()
    release_notes = str(payload.get("body") or "").strip()

    if not tag_name:
        return UpdateCheckResult(
            ok=False,
            status="error",
            message="GitHub Release 响应缺少版本标签。",
            current_version=current_version,
            release_url=release_url,
            release_notes=release_notes,
        )

    if major_version(latest_version) is None or major_version(current_version) is None:
        return UpdateCheckResult(
            ok=True,
            status="unknown",
            message=f"无法判断版本号 {tag_name} 是否为新版，已跳过更新提示。",
            current_version=current_version,
            latest_version=latest_version,
            latest_tag=tag_name,
            release_url=release_url,
            release_notes=release_notes,
        )

    assets = _parse_release_assets(payload)
    selected_asset = _select_platform_asset(assets, platform_name=platform_name)

    if not is_newer_version(latest_version, current_version):
        return UpdateCheckResult(
            ok=True,
            status="current",
            message=f"当前已是最新版本 V{current_version}。",
            current_version=current_version,
            latest_version=latest_version,
            latest_tag=tag_name,
            release_url=release_url,
            release_notes=release_notes,
        )

    return UpdateCheckResult(
        ok=True,
        status="available",
        message=f"发现新版 V{latest_version}。",
        current_version=current_version,
        latest_version=latest_version,
        latest_tag=tag_name,
        release_url=release_url,
        asset_name=selected_asset.name if selected_asset else "",
        download_url=selected_asset.download_url if selected_asset else release_url,
        release_notes=release_notes,
    )


def check_for_updates(
    *,
    current_version: str = APP_VERSION,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    platform_name: str | None = None,
) -> UpdateCheckResult:
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
    except Exception as exc:  # noqa: BLE001 - UI needs a concise failure state.
        return UpdateCheckResult(
            ok=False,
            status="error",
            message=f"检查更新失败：{exc}",
            current_version=current_version,
        )

    if not isinstance(payload, dict):
        return UpdateCheckResult(
            ok=False,
            status="error",
            message="GitHub Release 响应格式异常。",
            current_version=current_version,
        )

    return build_update_result_from_release_payload(
        payload,
        current_version=current_version,
        platform_name=platform_name,
    )
