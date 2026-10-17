"""Office 文件（.xlsx、.pptx 等 OPC 包）的 zip 读写：只重写改动过的部件。

Excel 文本框与单元格（``core/xlsx_patcher.py``、``core/xlsx_embedded_text.py``）和
PPT（``core/pptx_text.py``）共用：打开包、按部件名读写、保存时没改过的条目按原字节照抄，
所以用户文件里应用不认识的内容（宏、扩展数据、嵌入对象）一个字节都不会变。
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path


class OpcPackage:
    """打开 Office 文件的 zip 包，记录改写/新增/删除，保存时其余条目原字节照抄。"""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._zip = zipfile.ZipFile(self._path, "r")
        self._infos = {info.filename: info for info in self._zip.infolist()}
        self._order = [info.filename for info in self._zip.infolist()]
        self._overrides: dict[str, bytes] = {}
        self._added: list[str] = []
        self._removed: set[str] = set()

    def __enter__(self) -> OpcPackage:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def names(self) -> list[str]:
        """包里现有的部件名（按原顺序，新增的在后）。"""
        return [name for name in (*self._order, *self._added) if name not in self._removed]

    def has(self, name: str) -> bool:
        if name in self._removed:
            return False
        return name in self._overrides or name in self._infos

    def read(self, name: str) -> bytes | None:
        if name in self._overrides:
            return self._overrides[name]
        if name in self._removed or name not in self._infos:
            return None
        return self._zip.read(name)

    def write(self, name: str, data: bytes) -> None:
        self._removed.discard(name)
        if name not in self._infos and name not in self._overrides:
            self._added.append(name)
        self._overrides[name] = data

    def remove(self, name: str) -> None:
        self._overrides.pop(name, None)
        if name in self._added:
            self._added.remove(name)
        self._removed.add(name)

    def close(self) -> None:
        self._zip.close()

    def save(self) -> None:
        """把包写到临时文件再整体替换，避免半成品覆盖原文件。"""
        temp_path = self._path.with_name(self._path.name + ".patching")
        try:
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as target:
                for name in self._order:
                    if name in self._removed:
                        continue
                    source_info = self._infos[name]
                    if name in self._overrides:
                        target.writestr(
                            clone_zipinfo(source_info), self._overrides[name]
                        )
                    else:
                        target.writestr(
                            clone_zipinfo(source_info), self._zip.read(name)
                        )
                for name in self._added:
                    if name in self._removed:
                        continue
                    target.writestr(
                        zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)),
                        self._overrides[name],
                        zipfile.ZIP_DEFLATED,
                    )
        except BaseException:
            # 半成品 zip 不能留在磁盘上，它和成品同名只差一个后缀。
            temp_path.unlink(missing_ok=True)
            raise
        self._zip.close()
        os.replace(temp_path, self._path)


def clone_zipinfo(source: zipfile.ZipInfo) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(source.filename, date_time=source.date_time)
    info.compress_type = source.compress_type
    info.external_attr = source.external_attr
    info.internal_attr = source.internal_attr
    info.create_system = source.create_system
    return info


def resolve_part(base_part: str, target: str) -> str:
    """把关系里的相对 Target 解析成包内绝对部件名（不带前导斜杠）。"""
    target = str(target or "")
    if target.startswith("/"):
        return target[1:]
    base_dir = os.path.dirname(base_part)
    return os.path.normpath(os.path.join(base_dir, target)).replace(os.sep, "/")


def relative_part(base_part: str, target_part: str) -> str:
    base_dir = os.path.dirname(base_part) or "."
    return os.path.relpath(target_part, base_dir).replace(os.sep, "/")


def rels_path(part: str) -> str:
    directory, name = os.path.split(part)
    return f"{directory}/_rels/{name}.rels" if directory else f"_rels/{name}.rels"


__all__ = ["OpcPackage", "clone_zipinfo", "relative_part", "rels_path", "resolve_part"]
