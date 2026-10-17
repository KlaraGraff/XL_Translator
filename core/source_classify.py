"""拖入或选中的路径按扩展名数一数（「翻译」入口页按类型分配用）。

只数扩展名、不打开文件：文件夹按各类型扫描器的同一套规则往下找（递归，跳过 ``~`` 开头的
Office 临时文件和程序自己的输出目录），一个文件就记它自己的扩展名。扩展名归哪个类型由界面
决定（内置四种加扩展类型的描述符），这里不分类，免得两边各维护一张扩展名表。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from core.file_scanner import GENERATED_OUTPUT_DIR_MARKER

# 有人把整个用户目录拖进来时不能一直数下去：数到这么多个文件就停，结果标成「不完整」。
DEFAULT_FILE_LIMIT = 50_000


@dataclass
class ClassifiedPath:
    path: str
    # "file" / "dir" / "missing"
    kind: str
    # 扩展名（小写、不带点）→ 个数；文件只有它自己那一项。
    suffixes: dict[str, int] = field(default_factory=dict)


@dataclass
class ClassifyResult:
    items: list[ClassifiedPath]
    truncated: bool = False


def _suffix(name: str) -> str:
    return Path(name).suffix.lower().lstrip(".")


def classify_source_paths(paths: list[str], *, file_limit: int = DEFAULT_FILE_LIMIT) -> ClassifyResult:
    items: list[ClassifiedPath] = []
    seen = 0
    truncated = False
    for raw in paths:
        source = Path(str(raw)).expanduser()
        if source.is_file():
            suffix = _suffix(source.name)
            items.append(ClassifiedPath(str(raw), "file", {suffix: 1} if suffix else {}))
            continue
        if not source.is_dir():
            items.append(ClassifiedPath(str(raw), "missing"))
            continue
        counts: dict[str, int] = {}
        if not truncated:
            for current, dirs, files in os.walk(source):
                # 和扫描器一样跳过程序自己的输出目录（名字里带「_翻译输出_」）。
                dirs[:] = sorted(d for d in dirs if GENERATED_OUTPUT_DIR_MARKER not in d)
                for name in files:
                    if name.startswith("~"):
                        continue
                    suffix = _suffix(name)
                    if suffix:
                        counts[suffix] = counts.get(suffix, 0) + 1
                    seen += 1
                    if seen >= file_limit:
                        truncated = True
                        break
                if truncated:
                    break
        items.append(ClassifiedPath(str(raw), "dir", counts))
    return ClassifyResult(items=items, truncated=truncated)
