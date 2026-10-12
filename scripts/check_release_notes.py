"""发布前核对 Release 正文，供 .github/workflows/release.yml 的 publish 步骤调用。

AGENTS.md 第 9 条要求：正文由人写好、替换掉草稿里的「整理中」占位之后才能取消
草稿；签名和公证状态必须以当次构建的实际结果为准。这里把能机械检查的部分落成
代码，工作流替换正文之前先过一遍，任何一条不满足就不动草稿：

- 正文不能是空的，也不能还带着草稿占位句；
- 中文在前、英文在后；
- 下载一节写的文件名必须和本次构建渠道实际产出的文件名一致；
- 渠道是 formal-release 时不许写「未公证」，是 temporary-test 时必须写明「未公证」。
  V8.1.0 / V8.0.1 的说明写着已签名，实物其实没签——这一条就是防那种情况。

渠道从草稿正文里构建工作流写下的那一行 ``Channel: `...` `` 读出来，那一行是
build-distributions.yml 按 Apple 密钥是否齐全当场决定的，不靠人记。

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
    parser.add_argument("--draft-body", type=Path, help="草稿 Release 当前正文，用来读构建渠道")
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

    if not args.version or args.draft_body is None:
        parser.error("核对正文需要 --version 和 --draft-body")

    channel = extract_channel(args.draft_body.read_text(encoding="utf-8"))
    if channel is None:
        print(
            "草稿正文里找不到构建渠道那一行（Channel: `...`）。草稿可能已被手动改过，"
            "请到 Actions 里查这次构建 macOS 任务的「Configure Developer ID」是否执行。",
            file=sys.stderr,
        )
        return 1

    errors = notes_errors(notes, args.version.strip().removeprefix("v"), channel)
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"发布说明核对通过（构建渠道 {channel}）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
