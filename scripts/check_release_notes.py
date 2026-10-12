"""发布前核对 Release 正文，供 .github/workflows/release.yml 的 publish 步骤调用。

AGENTS.md 第 9 条要求：正文由人写好、替换掉草稿里的「整理中」占位之后才能取消
草稿；签名和公证状态必须以当次构建的实际结果为准。这里把能机械检查的部分落成
代码，工作流替换正文之前先过一遍，任何一条不满足就不动草稿：

- 正文不能是空的，也不能还带着草稿占位句；
- 中文在前、英文在后；
- 下载一节写的文件名必须和本次构建渠道实际产出的文件名一致；
- 渠道是 formal-release 时不许写「未公证」，是 temporary-test 时必须写明「未公证」。
  V8.1.0 / V8.0.1 的说明写着已签名，实物其实没签——这一条就是防那种情况。

渠道以 Release 上实际挂着的 macOS 安装包文件名为准（临时签名包带
``_TEMP_SIGNED_TEST``）；草稿正文里构建工作流写的那一行 ``Channel: `...` ``
若还在，也必须与之一致。只看文件名，是因为撤回后重新发布时，正文早已换成
正式说明，那一行已经不在了。

另有 ``--compare`` 模式：替换正文后从 GitHub 读回，和仓库里的文件逐字比对。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PLACEHOLDER_MARKERS = (
    "发布说明整理中",
    "Release notes are being written",
)
CHANNEL_RE = re.compile(r"Channel:\s*`(?P<channel>[a-z-]+)`")
KNOWN_CHANNELS = ("formal-release", "temporary-test")


def normalize(text: str) -> str:
    """GitHub 存正文时可能改换行符、去掉末尾换行，比对前统一掉。"""
    return text.replace("\r\n", "\n").strip()


def extract_channel(draft_body: str) -> str | None:
    match = CHANNEL_RE.search(draft_body)
    return match.group("channel") if match else None


def channel_from_assets(asset_names: list[str], version: str) -> str | None:
    """按 Release 上实际的 macOS 安装包判断这次构建的签名渠道。"""
    names = {name.strip() for name in asset_names if name.strip()}
    formal = f"Translator_macOS_arm64_{version}.dmg" in names
    temporary = f"Translator_macOS_arm64_{version}_TEMP_SIGNED_TEST.dmg" in names
    if formal == temporary:
        # 两个都没有（产物缺失）或两个都有（不该发生），都不能凭此判断。
        return None
    return "formal-release" if formal else "temporary-test"


def resolve_channel(
    asset_names: list[str] | None, draft_body: str | None, version: str
) -> tuple[str | None, str | None]:
    """返回 (渠道, 错误)。文件名优先，正文里的渠道行只用来交叉核对。"""
    from_assets = channel_from_assets(asset_names, version) if asset_names is not None else None
    from_body = extract_channel(draft_body) if draft_body is not None else None
    if asset_names is not None and from_assets is None:
        return None, (
            f"Release 上找不到唯一的 macOS 安装包 Translator_macOS_arm64_{version}[_TEMP_SIGNED_TEST].dmg，"
            "无法判断这次构建是否签名和公证。"
        )
    if from_assets and from_body and from_assets != from_body:
        return None, f"安装包文件名显示渠道是 {from_assets}，草稿正文里写的却是 {from_body}。"
    channel = from_assets or from_body
    if channel is None:
        return None, (
            "无法判断构建渠道：既没有安装包列表，草稿正文里也没有 Channel 行。"
            "请到 Actions 里查这次构建 macOS 任务的「Configure Developer ID」是否执行。"
        )
    return channel, None


def expected_asset_names(version: str, channel: str) -> list[str]:
    suffix = "_TEMP_SIGNED_TEST" if channel == "temporary-test" else ""
    return [
        f"Translator_macOS_arm64_{version}{suffix}.dmg",
        f"Translator_Windows_x64_{version}_Setup.exe",
    ]


def notes_errors(notes: str, version: str, channel: str) -> list[str]:
    errors: list[str] = []
    text = normalize(notes)
    if not text:
        return ["发布说明是空的。"]

    for marker in PLACEHOLDER_MARKERS:
        if marker in text:
            errors.append(f"发布说明里还有草稿占位句「{marker}」。")

    zh = text.find("## 中文")
    en = text.find("## English")
    if zh < 0 or en < 0:
        errors.append("发布说明需要「## 中文」和「## English」两节。")
    elif zh > en:
        errors.append("中文一节必须写在英文一节前面。")

    if channel not in KNOWN_CHANNELS:
        errors.append(f"无法识别的构建渠道：{channel!r}，只接受 {', '.join(KNOWN_CHANNELS)}。")
        return errors

    for name in expected_asset_names(version, channel):
        if name not in text:
            errors.append(f"下载一节缺少本次构建的文件名 {name}。")

    if channel == "formal-release":
        if "未公证" in text or "not notarized" in text.lower():
            errors.append("本次是正式签名并已公证的构建，说明里却写了「未公证」。")
        if "TEMP_SIGNED_TEST" in text:
            errors.append("本次是正式签名构建，说明里却出现了临时签名包的文件名。")
    elif "未公证" not in text:
        errors.append("本次 macOS 包是临时签名、未公证（temporary-test），说明里必须写明「未公证」。")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--notes", type=Path, required=True, help="仓库里的发布说明文件")
    parser.add_argument("--version", help="版本号，例如 10.0.1（不带 v）")
    parser.add_argument("--draft-body", type=Path, help="Release 当前正文，用来交叉核对构建渠道")
    parser.add_argument("--assets", type=Path, help="Release 上的产物文件名，每行一个")
    parser.add_argument(
        "--compare",
        type=Path,
        help="只做读回比对：给出从 GitHub 读回的正文，与 --notes 逐字比较",
    )
    args = parser.parse_args(argv)

    notes = args.notes.read_text(encoding="utf-8")

    if args.compare is not None:
        if normalize(args.compare.read_text(encoding="utf-8")) != normalize(notes):
            print("读回的 Release 正文与仓库里的发布说明不一致。", file=sys.stderr)
            return 1
        print("读回核对通过：Release 正文与仓库文件一致。")
        return 0

    if not args.version or (args.draft_body is None and args.assets is None):
        parser.error("核对正文需要 --version，以及 --assets 或 --draft-body")

    version = args.version.strip().removeprefix("v")
    channel, error = resolve_channel(
        args.assets.read_text(encoding="utf-8").splitlines() if args.assets else None,
        args.draft_body.read_text(encoding="utf-8") if args.draft_body else None,
        version,
    )
    if error:
        print(error, file=sys.stderr)
        return 1

    errors = notes_errors(notes, version, channel)
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"发布说明核对通过（构建渠道 {channel}）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
