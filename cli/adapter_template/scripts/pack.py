"""把扩展打成 zip，并写出提交到 Translator 扩展列表用的条目。

用法::

    python scripts/pack.py [扩展目录] [--out 目录] [--tag v0.1.0] [--repo https://github.com/你/仓库] [--author 名字]

在 ``--out``（默认 ``<扩展目录>/dist``）下写出三个文件：

- ``<id>-<version>.zip``：扩展包。收 ``adapter.json``、``adapter.py``、其他 ``.py`` / ``.json`` / ``.md`` /
  ``.txt`` 和 ``LICENSE``，以及 ``samples/``；不收隐藏文件、``__pycache__``，也不收顶层的
  ``dist``、``build``、``scripts``、``tests``、``node_modules``（与 Translator 安装时的规则相同）。
- ``<id>-<version>.zip.sha256``：zip 的 SHA-256。
- ``<id>-<version>.entry.json``：扩展列表的条目，提交到
  https://github.com/KlaraGraff/XL_Translator-extensions 的 ``extensions/<id>.json``。

只用 Python 标准库。打包不做检查：先用 ``translator-sidecar adapter test <扩展目录>`` 自测。
Translator 的 ``translator-sidecar adapter pack`` 用的就是这份文件。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from pathlib import Path

MANIFEST_FILE = "adapter.json"
EXCLUDED_TOP_DIRS = frozenset({"dist", "build", "scripts", "tests", "node_modules"})
# 固定的时间戳和权限：同样的文件每次打出同样的 zip。
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


class PackError(Exception):
    """打不了包的原因（给作者看的一句话）。"""


def package_files(root: Path) -> list[str]:
    """会进扩展包的文件（相对路径，``/`` 分隔，已排序）。"""
    found = []
    for current, directories, filenames in os.walk(root):
        relative_dir = Path(current).relative_to(root).parts
        directories[:] = sorted(
            name for name in directories if not _ignored((*relative_dir, name)) and not Path(current, name).is_symlink()
        )
        for name in filenames:
            parts = (*relative_dir, name)
            if not _ignored(parts) and not Path(current, name).is_symlink():
                found.append("/".join(parts))
    return sorted(found)


def _ignored(parts: tuple[str, ...]) -> bool:
    if parts[0] in EXCLUDED_TOP_DIRS and len(parts) > 1:
        return True
    return any(
        part.startswith(".") or part in ("__pycache__", "__MACOSX") or part.endswith((".pyc", ".pyo"))
        for part in parts
    )


def read_manifest(root: Path) -> dict:
    path = root / MANIFEST_FILE
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise PackError(f"{root} 里没有 {MANIFEST_FILE}") from None
    except (OSError, UnicodeError, ValueError) as exc:
        raise PackError(f"{MANIFEST_FILE} 无法读取：{exc}") from None
    if not isinstance(manifest, dict) or not manifest.get("id") or not manifest.get("version"):
        raise PackError(f"{MANIFEST_FILE} 缺少 id 或 version")
    return manifest


def _safe(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", str(value))[:40]


def write_zip(root: Path, files: list[str], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in files:
            info = zipfile.ZipInfo(relative, date_time=_ZIP_DATE)
            info.create_system = 3
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, (root / relative).read_bytes(), compresslevel=9)


def _github_repo() -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    return f"{server}/{repository}" if repository else ""


def registry_entry(
    manifest: dict, *, zip_name: str, sha256: str, size: int, tag: str = "", repo: str = "", author: str = ""
) -> dict:
    """扩展列表的条目。``status``、``reviewed_at``、``reviewed_by`` 由扩展列表的维护者在合并时填写。"""
    repo = (repo or str(manifest.get("repo") or "") or _github_repo()).rstrip("/")
    tag = tag or f"v{manifest['version']}"
    if not author:
        author = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    if not author and repo.startswith("https://github.com/"):
        author = repo.removeprefix("https://github.com/").split("/")[0]
    download = [f"{repo}/releases/download/{tag}/{zip_name}"] if repo.startswith("https://github.com/") else []
    return {
        "id": manifest["id"],
        "name": manifest.get("label") or {},
        "summary": manifest.get("description") or {},
        "extensions": manifest.get("extensions") or [],
        "author": author,
        "repo": repo,
        "license": manifest.get("license") or "",
        "version": manifest["version"],
        "adapter_api_version": manifest.get("adapter_api_version"),
        "min_app_version": manifest.get("min_app_version") or "",
        "download": download,
        "sha256": sha256,
        "size": size,
        "tags": [],
        "status": "reviewed",
        "reviewed_at": "",
        "reviewed_by": "",
    }


def pack(directory: str | Path, *, out: str | Path | None = None, tag: str = "", repo: str = "", author: str = "") -> dict:
    """打包；返回 ``{"zip", "sha256_file", "entry_file", "entry", "files"}``（路径为字符串）。"""
    root = Path(directory).resolve()
    manifest = read_manifest(root)
    if tag and tag.removeprefix("v") != str(manifest["version"]):
        raise PackError(f"标签 {tag} 与 {MANIFEST_FILE} 的 version {manifest['version']} 不一致")
    files = package_files(root)
    base = f"{_safe(manifest['id'])}-{_safe(manifest['version'])}"
    target_dir = Path(out).resolve() if out else root / "dist"
    archive = target_dir / f"{base}.zip"
    write_zip(root, files, archive)
    data = archive.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    sha_file = target_dir / f"{base}.zip.sha256"
    sha_file.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    entry = registry_entry(manifest, zip_name=archive.name, sha256=digest, size=len(data), tag=tag, repo=repo, author=author)
    entry_file = target_dir / f"{base}.entry.json"
    entry_file.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"zip": str(archive), "sha256_file": str(sha_file), "entry_file": str(entry_file), "entry": entry, "files": files}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把扩展打成 zip，并写出扩展列表的条目。")
    parser.add_argument("directory", nargs="?", default=".", help="扩展目录（含 adapter.json），默认当前目录")
    parser.add_argument("--out", help="输出目录，默认 <扩展目录>/dist")
    parser.add_argument("--tag", default="", help="发布标签，如 v0.1.0；须与 adapter.json 的 version 一致")
    parser.add_argument("--repo", default="", help="项目主页，默认取 adapter.json 的 repo")
    parser.add_argument("--author", default="", help="作者，默认取 GitHub 账号名")
    args = parser.parse_args(argv)
    try:
        result = pack(args.directory, out=args.out, tag=args.tag, repo=args.repo, author=args.author)
    except PackError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(f"已打包：{result['zip']}（{len(result['files'])} 个文件）", file=sys.stderr)
    print(f"SHA-256：{result['entry']['sha256']}", file=sys.stderr)
    if not result["entry"]["download"]:
        print("提示：adapter.json 的 repo 不是 GitHub 地址，条目里的 download 需要手动填写。", file=sys.stderr)
    if not result["entry"]["summary"]:
        print("提示：adapter.json 没有 description，条目里的 summary 需要手动填写（中、英、法三种语言）。", file=sys.stderr)
    print(json.dumps(result["entry"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
