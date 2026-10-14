"""测试用：把某种文件类型这一次的速率钉成固定值。

v38 起 Excel、Word 的同时请求数和每批条数来自「翻译参数」（core/surface_rate.py），
默认值随云端/本地变化。跑流程的测试要的是确定的串行顺序，不该跟着默认值走，所以
把运行器里的 ``resolve_surface_rate`` 换成这里的版本：其余各项照常解析，只覆盖
指定的几项。
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from core import surface_rate


def pinned_surface_rate(**overrides: int) -> Callable[..., Any]:
    real = surface_rate.resolve_surface_rate

    def _resolve(settings, surface, *, mode=None):
        return dataclasses.replace(real(settings, surface, mode=mode), **overrides)

    return _resolve
