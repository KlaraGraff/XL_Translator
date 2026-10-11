# PDF 图纸翻译写回路线：实验分支阶段记录

日期：2026-10-11

本记录属于 `codex/pdf-writeback-optimization` 实验分支，不进入 `main`。

## 当前结论

以 `SUNRISE FES-EXE-BAT DOUANE-COFF_A1.pdf` 为测试图，当前可交付的实验候选是“字号回归版”。它保留原始 PDF 的矢量内容和嵌入图像，只在需要翻译的区域增加文字与有限的遮罩/引出线处理。

已确认：

- 页面、旋转角度和大部分矢量图形保持不变；
- 输出保留 5/5 个嵌入图像；
- 坐标往返最大误差约 `0.000176 pt`；
- 写回 `planned=165`、`failed=0`；
- 中文字符数 `689`；
- `qpdf` 检查通过；
- 大标题、条件行和普通标注的字号层级通过局部检查。

## 已排除的方案

1. 全局几何分组、整框遮罩和宽范围引出线重绘会跨区域擦除图线，造成散落、断线和缺失，不能作为写回方案。
2. 紧遮罩虽然能处理少数重叠区域，但在相邻地址、重复 OCR 和截断标题区域会裁切译文；中文字符数由 `689` 降至 `375`，因此不交付。

## 多行文本边界

双行、三行隔离 fixture 已通过：显式基线、固定行距、无行间重叠、引出线不压字。实际 A1 密集区域探测到大量相交矢量片段，整框遮罩不安全，因此尚未把多行策略写入真实 A1 页面。

## 后续路线

- 继续使用字符墨迹级遮罩，而不是 OCR 外接矩形遮罩；
- 只在文字锚点、引出线端点和连通分量均能明确关联时恢复引出线；
- 对重复/截断 OCR 行保留原文，优先选择权威完整行；
- 对表格按矢量网格建立单元格边界，单元格内替换，禁止覆盖网格；
- ODA 仅作为用户已有 DWG/DXF 时的可选 CAD 插件边界；当前 PDF-only 路线不把 ODA 作为默认依赖。

## 产物

- 候选 PDF：`.runtime/self-tests/pdf-optimization-20261011/customs-a1/SUNRISE_FES_EXE_BAT_DOUANE_COFF_A1_中文_字号回归.pdf`
- 字号/矢量审查：`.runtime/self-tests/pdf-optimization-20261011/review/stylecluster_review.md`
- 多行隔离测试：`.runtime/self-tests/pdf-optimization-20261011/multiline/multiline_report.md`
- 引出线/重叠诊断：`.runtime/self-tests/pdf-optimization-20261011/leaders/leader_report.md`
