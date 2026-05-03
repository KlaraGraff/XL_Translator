"""
浮动图片检测模块（逐行累加方案）。

功能：
  - 检测工作表中所有浮动图片
  - 精确计算每张图片覆盖的行范围（从起始行到结束行）
  - 返回所有含浮动图片的行号集合

算法：逐行累加
  1. 获取图片锚点行和高度
  2. 从锚点行开始，逐行累加实际行高
  3. 当累计高度 ≥ 图片高度时，停止
  4. 返回 [起始行, 结束行] 范围

特性：
  - 精确处理行高不一致的情况
  - 完善的容错机制（属性验证、异常捕获）
  - 详细的日志记录（便于调试）
"""
from typing import Set, Tuple, Optional, Dict, List
from openpyxl.worksheet.worksheet import Worksheet
from loguru import logger

# KNOWN-ISSUE-VAL-006:
# This module is intentionally retained as historical source, but it is not
# wired into the current translation scan/write path. See docs/KNOWN_ISSUES.md.
#
# KNOWN-ISSUE-VAL-005:
# The current implementation still assumes some object anchors expose row/col
# directly. If this module is re-enabled later, review docs/KNOWN_ISSUES.md
# before changing anchor parsing.


# ── 常量定义 ──────────────────────────────────────────────────────────────
DEFAULT_ROW_HEIGHT_PT = 15.0  # Excel 默认行高（磅）
PT_TO_PX_RATIO = 1.33  # 磅到像素的转换系数（1pt ≈ 1.33px）
SAFETY_MARGIN = 1.05  # 安全余量（5%），用于规避单位转换误差
MAX_ROWS_LIMIT = 1000  # 行号上限（防止无限循环）


def get_rows_with_floating_images(ws: Worksheet) -> Set[int]:
    """
    获取工作表中所有含浮动图片的行号集合。
    
    :param ws: openpyxl Worksheet 对象
    :return: 行号集合（1-based），例如 {5, 6, 7, 8, 12, 13, 14, 15}
    
    示例：
        >>> ws = load_workbook('file.xlsx').active
        >>> rows = get_rows_with_floating_images(ws)
        >>> print(rows)
        {5, 6, 7, 8}  # 表示第 5-8 行包含浮动图片
    """
    rows_with_images = set()
    image_ranges = get_image_row_ranges(ws)
    
    for start_row, end_row in image_ranges.values():
        # 包含起始行和结束行
        for row_num in range(start_row, end_row + 1):
            rows_with_images.add(row_num)
    
    if rows_with_images:
        logger.debug(
            f"检测到浮动图片，涉及 {len(rows_with_images)} 行："
            f" {sorted(rows_with_images)}"
        )
    
    return rows_with_images


def get_image_row_ranges(ws: Worksheet) -> Dict[int, Tuple[int, int]]:
    """
    获取工作表中所有浮动图片覆盖的行范围。
    
    :param ws: openpyxl Worksheet 对象
    :return: {图片索引: (起始行, 结束行)} 字典
    
    示例：
        >>> ranges = get_image_row_ranges(ws)
        >>> print(ranges)
        {
            0: (5, 8),    # 第一张图片覆盖第 5-8 行
            1: (12, 15),  # 第二张图片覆盖第 12-15 行
        }
    """
    image_ranges = {}
    
    # 检查工作表是否有图片
    if not hasattr(ws, '_images') or not ws._images:
        logger.debug("工作表中无浮动图片")
        return image_ranges
    
    logger.debug(f"工作表中检测到 {len(ws._images)} 张图片")
    
    # 处理 _images 可能是列表或字典的情况
    images = ws._images
    if isinstance(images, dict):
        images_list = images.values()
    else:
        images_list = images
    
    for idx, image in enumerate(images_list):
        row_range = _calculate_image_row_range(image, ws)
        
        if row_range is not None:
            start_row, end_row = row_range
            image_ranges[idx] = row_range
            # 获取锚点行（处理字符串或对象的情况）
            anchor = image.anchor
            if isinstance(anchor, str):
                import re
                row_match = re.search(r'\d+', anchor)
                anchor_row = int(row_match.group()) if row_match else "unknown"
            else:
                anchor_row = getattr(anchor, 'row', 'unknown')
            logger.debug(
                f"图片 {idx}：行范围 {start_row}-{end_row} "
                f"(锚点行={anchor_row}, 高度={image.height}px)"
            )
        else:
            logger.warning(f"图片 {idx}：无法计算行范围，已跳过")
    
    return image_ranges


def _calculate_image_row_range(
    image,
    ws: Worksheet,
) -> Optional[Tuple[int, int]]:
    """
    逐行累加方案：精确计算单张图片覆盖的行范围。
    
    :param image: openpyxl Image 对象
    :param ws: openpyxl Worksheet 对象
    :return: (起始行, 结束行) 元组（1-based），或 None（异常时）
    
    算法：
      1. 获取图片锚点行和高度（像素）
      2. 从锚点行开始，逐行累加实际行高
      3. 当累计高度 ≥ 图片高度时，停止
      4. 返回 [起始行, 结束行]
    
    容错机制：
      - 属性验证：检查锚点行和高度的有效性
      - 行号上限：防止无限循环
      - 异常捕获：捕获所有异常并返回 None
      - 单位转换：加入 5% 安全余量
    """
    try:
        # ── 1. 属性验证 ────────────────────────────────────────────────
        start_row = _validate_anchor_row(image)
        if start_row is None:
            return None
        
        image_height_px = _validate_image_height(image)
        if image_height_px is None:
            return None
        
        # ── 2. 初始化 ──────────────────────────────────────────────────
        cumulative_height_px = 0.0
        current_row = start_row
        
        # ── 3. 逐行累加 ────────────────────────────────────────────────
        while cumulative_height_px < image_height_px and current_row <= MAX_ROWS_LIMIT:
            row_height_pt = _get_row_height(ws, current_row)
            row_height_px = row_height_pt * PT_TO_PX_RATIO * SAFETY_MARGIN
            cumulative_height_px += row_height_px
            current_row += 1
        
        # ── 4. 边界检查 ────────────────────────────────────────────────
        if current_row > MAX_ROWS_LIMIT:
            logger.warning(
                f"图片超出工作表范围，已截断至第 {MAX_ROWS_LIMIT} 行"
            )
            end_row = MAX_ROWS_LIMIT
        else:
            end_row = current_row - 1
        
        # ── 5. 返回结果 ────────────────────────────────────────────────
        return (start_row, end_row)
    
    except Exception as e:
        logger.error(f"计算图片行范围异常：{e}", exc_info=True)
        return None


def _validate_anchor_row(image) -> Optional[int]:
    # KNOWN-ISSUE-VAL-005:
    # This code path still documents the legacy anchor.row assumption in source.
    # See docs/KNOWN_ISSUES.md for the compatibility note and re-enable checklist.
    """
    验证图片锚点行号的有效性。
    
    :param image: openpyxl Image 对象
    :return: 有效的行号（1-based），或 None（无效时）
    """
    try:
        if not hasattr(image, 'anchor') or image.anchor is None:
            logger.warning("图片缺少 anchor 属性")
            return None
        
        anchor = image.anchor
        
        # 处理两种情况：
        # 1. anchor 是对象，有 row 属性
        # 2. anchor 是字符串（如 "B5"），需要解析
        if isinstance(anchor, str):
            # 从字符串中提取行号（如 "B5" -> 5）
            import re
            match = re.search(r'\d+', anchor)
            if match:
                anchor_row = int(match.group())
            else:
                logger.warning(f"无法从锚点字符串中提取行号：{anchor}")
                return None
        else:
            # anchor 是对象
            anchor_row = getattr(anchor, 'row', None)
            if anchor_row is None:
                logger.warning("图片锚点对象缺少 row 属性")
                return None
        
        if not isinstance(anchor_row, int):
            logger.warning(f"图片锚点行号类型异常：{type(anchor_row)}")
            return None
        
        if anchor_row < 1:
            logger.warning(f"图片锚点行号无效（< 1）：{anchor_row}")
            return None
        
        return anchor_row
    
    except Exception as e:
        logger.error(f"验证锚点行号异常：{e}")
        return None


def _validate_image_height(image) -> Optional[float]:
    """
    验证图片高度的有效性。
    
    :param image: openpyxl Image 对象
    :return: 有效的高度（像素），或 None（无效时）
    """
    try:
        if not hasattr(image, 'height') or image.height is None:
            logger.warning("图片缺少 height 属性")
            return None
        
        image_height = image.height
        
        if not isinstance(image_height, (int, float)):
            logger.warning(f"图片高度类型异常：{type(image_height)}")
            return None
        
        if image_height <= 0:
            logger.warning(f"图片高度无效（≤ 0）：{image_height}")
            return None
        
        return float(image_height)
    
    except Exception as e:
        logger.error(f"验证图片高度异常：{e}")
        return None


def _get_row_height(ws: Worksheet, row_num: int) -> float:
    """
    获取指定行的高度（磅）。
    
    :param ws: openpyxl Worksheet 对象
    :param row_num: 行号（1-based）
    :return: 行高（磅）
    
    策略：
      1. 如果行有显式设置的高度，使用该高度
      2. 否则使用 Excel 默认行高 15pt
    """
    try:
        row_dim = ws.row_dimensions.get(row_num)
        
        if row_dim is not None and row_dim.height is not None:
            return float(row_dim.height)
        
        return DEFAULT_ROW_HEIGHT_PT
    
    except Exception as e:
        logger.warning(f"获取第 {row_num} 行高度异常，使用默认值：{e}")
        return DEFAULT_ROW_HEIGHT_PT


def get_image_details(ws: Worksheet) -> List[Dict]:
    """
    获取工作表中所有图片的详细信息（用于调试和日志）。
    
    :param ws: openpyxl Worksheet 对象
    :return: 图片信息列表
    
    示例：
        >>> details = get_image_details(ws)
        >>> for detail in details:
        ...     print(detail)
        {
            'index': 0,
            'anchor_row': 5,
            'anchor_col': 2,
            'width_px': 200,
            'height_px': 150,
            'row_range': (5, 8),
            'filename': 'image1.png'
        }
    """
    details = []
    image_ranges = get_image_row_ranges(ws)
    
    if not hasattr(ws, '_images') or not ws._images:
        return details
    
    # 处理 _images 可能是列表或字典的情况
    images = ws._images
    if isinstance(images, dict):
        images_list = images.values()
    else:
        images_list = images
    
    for idx, image in enumerate(images_list):
        try:
            start_row, end_row = image_ranges.get(idx, (None, None))
            
            # 处理 anchor 可能是字符串或对象的情况
            anchor = image.anchor
            if isinstance(anchor, str):
                # 从字符串中提取行列号（如 "B5" -> col=2, row=5）
                import re
                col_match = re.match(r'([A-Z]+)', anchor)
                row_match = re.search(r'\d+', anchor)
                anchor_col = None
                anchor_row = None
                if col_match:
                    # 将列字母转换为数字（A=1, B=2, ...）
                    col_str = col_match.group(1)
                    anchor_col = sum((ord(c) - ord('A') + 1) * (26 ** (len(col_str) - i - 1)) 
                                    for i, c in enumerate(col_str))
                if row_match:
                    anchor_row = int(row_match.group())
            else:
                anchor_row = getattr(anchor, 'row', None)
                anchor_col = getattr(anchor, 'col', None)
            
            detail = {
                'index': idx,
                'anchor_row': anchor_row,
                'anchor_col': anchor_col,
                'width_px': getattr(image, 'width', None),
                'height_px': getattr(image, 'height', None),
                'row_range': (start_row, end_row) if start_row else None,
                'filename': getattr(image, 'filename', 'unknown'),
            }
            details.append(detail)
        
        except Exception as e:
            logger.warning(f"获取图片 {idx} 详细信息异常：{e}")
    
    return details


def log_image_summary(ws: Worksheet) -> None:
    """
    输出工作表中所有图片的摘要日志。
    
    :param ws: openpyxl Worksheet 对象
    """
    details = get_image_details(ws)
    
    if not details:
        logger.info("工作表中无浮动图片")
        return
    
    logger.info(f"工作表中检测到 {len(details)} 张浮动图片：")
    for detail in details:
        logger.info(
            f"  图片 {detail['index']}："
            f" 锚点=({detail['anchor_row']}, {detail['anchor_col']})，"
            f" 尺寸={detail['width_px']}×{detail['height_px']}px，"
            f" 覆盖行={detail['row_range']}，"
            f" 文件={detail['filename']}"
        )
