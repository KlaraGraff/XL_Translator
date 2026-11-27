"""重新生成「段落重排组件（BabelDOC）」的固定文件清单 core/babeldoc_component_manifest.py。

升级 BabelDOC 或它的依赖时在开发机上跑（要能连 PyPI 和 GitHub，装了 uv）：

    python scripts/babeldoc_component_manifest.py            # 写 core/babeldoc_component_manifest.py
    python scripts/babeldoc_component_manifest.py --check    # 只核对，清单过期时退出 1

做法：
1. 用 ``uv pip compile`` 按三个平台（macOS 12 arm64、Windows x64、Linux x64，都是 Python 3.11）
   解出 babeldoc 的全部依赖。版本钉在翻译引擎安装包的那一套（constraints-release-py311.txt）
   和「PDF 处理组件」的 NumPy / OpenCV / PyMuPDF 上：组件在翻译引擎的子进程里跑，安装包自带
   的库优先，两边版本必须对得上。
2. 每个包按平台标签挑一个 wheel，记下 PyPI 上的路径、大小和 SHA-256。
3. 字体、CMap、版面模型和 tiktoken 词表照 BabelDOC 自己写死的清单（名字、大小、SHA3-256），
   字体和 CMap 再从 GitHub 上的 BabelDOC-Assets 算出 SHA-256（版面模型和词表 GitHub 上没有，
   用 ``ASSET_SHA256`` 里手填的值，来自一台真装过 BabelDOC 的机器）。

不进安装包，只在开发机上用。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from packaging import tags
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "core" / "babeldoc_component_manifest.py"
BABELDOC_VERSION = "0.7.1"

# 「PDF 处理组件」里已有的，组件不再带一份（core/pdf_component_install.py 里的版本）。
PDF_COMPONENT_PINS = {"numpy": "2.2.6", "opencv-python-headless": "4.10.0.84", "pymupdf": "1.28.2"}
# 还支持 macOS 12 的最后一版（与文字识别组件相同）。
EXTRA_PINS = {"onnxruntime": "1.19.2"}
# 只是命令行工具、运行时从不 import 的：xsdata[cli] 拉进来的代码格式化程序。
SKIPPED = {"ruff"}

PLATFORMS = {
    "macos-arm64": "aarch64-apple-darwin",
    "windows-x64": "x86_64-pc-windows-msvc",
    "linux-x64": "x86_64-manylinux_2_28",
}

# 版面模型和 tiktoken 词表：GitHub 上没有，SHA-256 和大小取自真装过 BabelDOC 0.7.1 的 Mac
# （~/.cache/babeldoc，BabelDOC 下载时已按它自己的 SHA3-256 核对过）。
ASSET_SHA256: dict[str, tuple[int, str]] = {
    "doclayout_yolo_docstructbench_imgsz1024.onnx":
        (75324598, "fece9af02f618b603ff7921ccec6861d13e7e1f9830e091dfb7e8ad9311e5b21"),
    "fb374d419588a4632f3f557e76b4b70aebbca790":
        (3613922, "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"),
}

_HF = "https://huggingface.co/datasets/awwaawwa/BabelDOC-Assets/resolve/main"
_HF_MIRROR = "https://hf-mirror.com/datasets/awwaawwa/BabelDOC-Assets/resolve/main"
_MODELSCOPE = "https://www.modelscope.cn/datasets/awwaawwa/BabelDOCAssets/resolve/master"
_GITHUB = "https://raw.githubusercontent.com/funstory-ai/BabelDOC-Assets/refs/heads/main"


def _platform_tags(key: str) -> list[tags.Tag]:
    if key == "macos-arm64":
        platforms = list(tags.mac_platforms((12, 0), "arm64"))
    elif key == "windows-x64":
        platforms = ["win_amd64"]
    else:
        platforms = [f"manylinux_2_{minor}_x86_64" for minor in range(28, 4, -1)]
        platforms += ["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"]
    result = list(tags.cpython_tags((3, 11), abis=["cp311"], platforms=platforms))
    result += list(tags.compatible_tags((3, 11), interpreter="cp311", platforms=platforms))
    return result


def _resolve(key: str, work: Path) -> list[tuple[str, str]]:
    constraints = work / "constraints.txt"
    lines = [line for line in (ROOT / "constraints-release-py311.txt").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    lines += [f"{name}=={version}" for name, version in {**PDF_COMPONENT_PINS, **EXTRA_PINS}.items()]
    constraints.write_text("\n".join(lines) + "\n", encoding="utf-8")
    requirement = work / "requirements.in"
    requirement.write_text(f"babeldoc=={BABELDOC_VERSION}\n", encoding="utf-8")
    output = work / f"resolved-{key}.txt"
    subprocess.run(
        ["uv", "pip", "compile", str(requirement), "-c", str(constraints), "--python-version", "3.11",
         "--python-platform", PLATFORMS[key], "--only-binary", ":all:", "--no-header", "--no-annotate",
         "-q", "-o", str(output)],
        check=True, env={**os.environ, "MACOSX_DEPLOYMENT_TARGET": "12.0"},
    )
    pins = []
    for line in output.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;]+)", line.strip())
        if match:
            pins.append((match.group(1), match.group(2)))
    return pins


def _pick_wheel(name: str, version: str, key: str, cache: dict) -> dict:
    if (name, version) not in cache:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=60) as response:
            cache[(name, version)] = json.load(response)["urls"]
    ranked = {tag: index for index, tag in enumerate(_platform_tags(key))}
    best = None
    for item in cache[(name, version)]:
        if item.get("packagetype") != "bdist_wheel" or item.get("yanked"):
            continue
        _name, _version, _build, wheel_tags = parse_wheel_filename(item["filename"])
        rank = min((ranked[tag] for tag in wheel_tags if tag in ranked), default=None)
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, item)
    if best is None:
        raise SystemExit(f"no {key} wheel for {name}=={version}")
    item = best[1]
    path = item["url"].split("/packages/", 1)[1]
    return {"name": item["filename"], "path": path, "size": int(item["size"]), "sha256": item["digests"]["sha256"]}


def _babeldoc_asset_metadata() -> dict:
    """BabelDOC wheel 里写死的资源清单（名字、大小、SHA3-256）。"""
    with tempfile.TemporaryDirectory() as folder:
        cache: dict = {}
        wheel = _pick_wheel("babeldoc", BABELDOC_VERSION, "linux-x64", cache)
        target = Path(folder) / wheel["name"]
        urllib.request.urlretrieve("https://files.pythonhosted.org/packages/" + wheel["path"], target)
        with zipfile.ZipFile(target) as archive:
            source = archive.read("babeldoc/assets/embedding_assets_metadata.py")
        module_path = Path(folder) / "embedding_assets_metadata.py"
        module_path.write_bytes(source)
        spec = importlib.util.spec_from_file_location("bd_assets_meta", module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return {
            "fonts": {name: (int(meta["size"]), meta["sha3_256"]) for name, meta in module.EMBEDDING_FONT_METADATA.items()},
            "cmap": {name: (int(meta["size"]), meta["sha3_256"]) for name, meta in module.CMAP_METADATA.items()},
            "model_sha3": module.DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256,
            "tiktoken": dict(module.TIKTOKEN_CACHES),
        }


def _github_sha256(folder: str, name: str, size: int, sha3: str) -> str:
    with urllib.request.urlopen(f"{_GITHUB}/{folder}/{name}", timeout=300) as response:
        data = response.read()
    if len(data) != size or hashlib.sha3_256(data).hexdigest() != sha3:
        raise SystemExit(f"{folder}/{name}: GitHub copy does not match BabelDOC's metadata")
    return hashlib.sha256(data).hexdigest()


def _assets(meta: dict) -> list[dict]:
    assets = []
    for folder in ("fonts", "cmap"):
        for name, (size, sha3) in sorted(meta[folder].items()):
            assets.append({"folder": folder, "name": name, "size": size, "sha3": sha3,
                           "sha256": _github_sha256(folder, name, size, sha3),
                           "urls": [f"{_GITHUB}/{folder}/{name}", f"{_HF}/{folder}/{name}?download=true",
                                    f"{_HF_MIRROR}/{folder}/{name}?download=true", f"{_MODELSCOPE}/{folder}/{name}"]})
    model = "doclayout_yolo_docstructbench_imgsz1024.onnx"
    size, sha256 = ASSET_SHA256[model]
    assets.append({"folder": "models", "name": model, "size": size, "sha3": meta["model_sha3"], "sha256": sha256,
                   "urls": ["https://huggingface.co/wybxc/DocLayout-YOLO-DocStructBench-onnx/resolve/main/"
                            f"{model}?download=true",
                            f"https://hf-mirror.com/wybxc/DocLayout-YOLO-DocStructBench-onnx/resolve/main/{model}"
                            "?download=true",
                            "https://www.modelscope.cn/models/AI-ModelScope/DocLayout-YOLO-DocStructBench-onnx/"
                            f"resolve/master/{model}"]})
    for name, sha3 in meta["tiktoken"].items():
        size, sha256 = ASSET_SHA256[name]
        assets.append({"folder": "tiktoken", "name": name, "size": size, "sha3": sha3, "sha256": sha256,
                       "urls": ["https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken",
                                f"{_HF}/tiktoken/{name}?download=true", f"{_MODELSCOPE}/tiktoken/{name}"]})
    return assets


def render(manifest: dict[str, list[dict]], assets: list[dict]) -> str:
    out = [
        '"""「段落重排组件（BabelDOC）」的固定文件清单。',
        "",
        "由 scripts/babeldoc_component_manifest.py 生成，不要手改；升级时重新生成并把",
        "core/babeldoc_component_install.py 的 MANIFEST_VERSION 加一。",
        '"""',
        "from __future__ import annotations",
        "",
        "from core.component_download import RemoteFile, pypi_file",
        "",
        f'BABELDOC_VERSION = "{BABELDOC_VERSION}"',
        "",
        "WHEELS: dict[str, tuple[RemoteFile, ...]] = {",
    ]
    for key, wheels in manifest.items():
        out.append(f'    "{key}": (')
        for wheel in wheels:
            out.append(f'        pypi_file("{wheel["name"]}",')
            out.append(f'                  "{wheel["path"]}",')
            out.append(f'                  {wheel["size"]}, "{wheel["sha256"]}"),')
        out.append("    ),")
    out.append("}")
    out.append("")
    out.append("# (BabelDOC 缓存里的子目录, 文件名, 大小, SHA-256, BabelDOC 自己核对用的 SHA3-256, 下载地址)")
    out.append("ASSETS: tuple[tuple[str, str, int, str, str, tuple[str, ...]], ...] = (")
    for asset in assets:
        urls = ", ".join(f'"{url}"' for url in asset["urls"])
        out.append(f'    ("{asset["folder"]}", "{asset["name"]}", {asset["size"]},')
        out.append(f'     "{asset["sha256"]}",')
        out.append(f'     "{asset["sha3"]}",')
        out.append(f"     ({urls},)),")
    out.append(")")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    cache: dict = {}
    manifest: dict[str, list[dict]] = {}
    with tempfile.TemporaryDirectory() as folder:
        for key in PLATFORMS:
            pins = _resolve(key, Path(folder))
            skip = {canonicalize_name(name) for name in (*PDF_COMPONENT_PINS, *SKIPPED)}
            manifest[key] = [_pick_wheel(name, version, key, cache) for name, version in pins
                             if canonicalize_name(name) not in skip]
    text = render(manifest, _assets(_babeldoc_asset_metadata()))
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != text:
            print("core/babeldoc_component_manifest.py is out of date", file=sys.stderr)
            return 1
        return 0
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}: "
          + ", ".join(f"{key} {len(items)} wheels" for key, items in manifest.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
