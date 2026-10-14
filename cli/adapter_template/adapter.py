"""「我的格式」的格式适配器：照着改成你要支持的文件格式。

Translator 负责页面、调用模型、记忆库、重试、复核和进度；适配器只做三件事：

- ``extract(path)``：从文件里取出要翻译的文字，每段一个 ``TextUnit``；
- ``write(path, units, mode, out_path, options)``：把译文写回，生成新文件；
- ``scan(path)``（可省）：统计要翻译的段数，显示在文件列表里。

这个示例处理「键 = 值」形式的文本文件（``.myfmt``）：

- 每行一个条目，等号左边是键（不翻译，用作复核清单里的位置），右边是要翻译的值；
- 以 ``#`` 开头的注释行和空行原样保留，不取出；
- 键以下划线开头的条目原样保留、不送去翻译（``locked``）；
- 值里 ``{name}`` 这类占位符必须原样保留（``protected``）。

只能用 Python 标准库和 ``translator_adapter``：不能联网、不能启动其他程序、不能调用模型。
"""

from __future__ import annotations

import re
from pathlib import Path

from translator_adapter import MODE_BILINGUAL, OutputFile, ScanItem, TextUnit, TranslatedUnit

# 一行「键 = 值」：缩进、键、等号（连同两边的空格）、值、行尾空白。
_ENTRY = re.compile(r"^(\s*)([^#=\s][^=]*?)(\s*=\s*)(.*?)(\s*)$")
# 值里必须原样保留的占位符，例如 {name}、{count}。
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def _read_lines(path: str) -> list[str]:
    # keepends=True：写回时保留每行原来的换行符（\n 或 \r\n）；utf-8-sig 去掉开头可能有的 BOM。
    return Path(path).read_text(encoding="utf-8-sig").splitlines(keepends=True)


def _entry(line: str) -> re.Match[str] | None:
    """这一行是「键 = 值」条目时返回匹配结果；注释、空行和其他内容返回 None。"""
    body = line.rstrip("\r\n")
    if not body.strip() or body.lstrip().startswith("#"):
        return None
    return _ENTRY.match(body)


def extract(path: str) -> list[TextUnit]:
    """取出要翻译的文字。

    path：要翻译的文件（只读，不要改动它）。

    返回 ``TextUnit`` 列表，按文件里的顺序。每个 ``TextUnit``：

    - ``id``：在本文件内唯一，``write`` 时用它找回位置（这里用行号）；
    - ``text``：要翻译的原文；
    - ``context``：复核清单里显示的位置（这里用键名）；
    - ``locked``：True 时不送去翻译，写回时就是原文；
    - ``protected``：``text`` 里必须原样保留的片段，应用会替模型守住它们。
    """
    units = []
    for number, line in enumerate(_read_lines(path), start=1):
        match = _entry(line)
        if match is None or not match.group(4):
            continue  # 不是条目，或者值是空的
        key, value = match.group(2), match.group(4)
        units.append(
            TextUnit(
                id=f"L{number}",
                text=value,
                context=key,
                locked=key.startswith("_"),
                protected=_PLACEHOLDER.findall(value),
            )
        )
    return units


def scan(path: str) -> ScanItem:
    """统计要翻译的段数。可以不写这个函数，应用会用 ``extract`` 的结果计数。"""
    return ScanItem(unit_count=sum(1 for unit in extract(path) if not unit.locked))


def write(path: str, units: list[TranslatedUnit], mode: str, out_path: str, options: dict) -> OutputFile:
    """把译文写回，生成新文件。

    path：原文件（只读）。
    units：``extract`` 取出的每一段和它的结果。``unit.translation`` 总是要写进去的文字
        （没译成的段和 ``locked`` 的段就是原文）；``unit.changed`` 表示译文与原文不同。
    mode：``"translation_only"``（只写译文）或 ``"bilingual"``（原文和译文都写）；
        ``adapter.json`` 的 ``capabilities`` 声明支持哪几种。
    out_path：要写出的文件（文件名和输出目录由应用定好）。
    options：``adapter.json`` 里 ``options`` 声明的开关和取值；这个示例没有开关。

    返回 ``OutputFile(path=out_path)``。
    """
    by_id = {unit.id: unit for unit in units}
    out = []
    for number, line in enumerate(_read_lines(path), start=1):
        unit = by_id.get(f"L{number}")
        match = _entry(line)
        if unit is None or match is None:
            out.append(line)  # 注释、空行、空值的条目原样写回
            continue
        # 这个格式一行一条：译文里万一有换行，换成空格。
        translation = " ".join(unit.translation.splitlines())
        if mode == MODE_BILINGUAL and unit.changed:
            translation = f"{unit.text} / {translation}"
        indent, key, equals, _value, trailing = match.groups()
        ending = line[len(line.rstrip("\r\n")):]
        out.append(f"{indent}{key}{equals}{translation}{trailing}{ending}")
    Path(out_path).write_text("".join(out), encoding="utf-8", newline="")
    return OutputFile(path=out_path)
