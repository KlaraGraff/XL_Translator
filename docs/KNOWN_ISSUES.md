# Known Issues

这个文件记录“当前明确知道、但暂不修复或暂不启用”的长期问题。

使用约定：
- 每条问题都有稳定的 `Issue ID`。
- 文档里必须能定位到源码位置。
- 源码里也会保留同样的 `Issue ID` 或 `KNOWN-ISSUE-*` 检索词，便于反向索引。

## 索引

- `VAL-005` 图片锚点对象兼容假设错误
- `VAL-006` `image_detector` 保留源码，但当前未启用

## VAL-005

### 概要
- 状态：已记录，当前不修改
- 模块：图片检测
- 性质：实现假设与 `openpyxl` 常见对象锚点结构不一致

### 问题描述
- `core/image_detector.py` 当前把对象型图片锚点当成可直接读取 `anchor.row` / `anchor.col`。
- 但 `openpyxl` 的常见图片锚点通常是 `OneCellAnchor` 或 `TwoCellAnchor`，坐标更常见地存放在 `anchor._from.row` / `anchor._from.col`。
- 同时这些值通常还是 `0-based`，如果后续真的启用该模块，需要转换成 Excel 语义里的 `1-based` 行列号。

### 可能影响
- 某些真实工作簿里的图片会被误判为“没有 row 属性”而跳过。
- 即使没有被跳过，也可能出现行号偏移。

### 源码位置
- `core/image_detector.py::_validate_anchor_row`
- `core/image_detector.py::get_image_row_ranges`
- `core/image_detector.py::get_image_details`

### 源码反向索引关键词
- `VAL-005`
- `KNOWN-ISSUE-VAL-005`

### 当前决定
- 先记录，不修。
- 原因：图片能力当前并未重新接入主流程；这条先作为恢复该模块前必须回看的技术债说明。

### 后续判断建议
- 如果未来决定重新启用图片检测，优先先处理本条，再讨论主流程接线。

## VAL-006

### 概要
- 状态：已记录，当前不修改功能
- 模块：图片检测接线
- 性质：历史保留源码，当前明确未启用

### 问题描述
- `image_detector` 这套能力最初设计过，但实际使用时 bug 较多，因此当前版本已下线。
- 现阶段选择保留源码，作为未来参考或重启能力时的基础，但主流程不再接入它。

### 当前行为
- 翻译扫描主流程不依赖 `image_detector`
- 写回链路按“纯文本模式”工作
- 图片相关扫描字段已经从当前 MVP 主线中移除

### 源码位置
- `core/image_detector.py`
- `core/bilingual_writer.py::_write_with_openpyxl`
- `core/file_scanner.py`
- `core/task_runner.py::TaskRunner._run`

### 源码反向索引关键词
- `VAL-006`
- `KNOWN-ISSUE-VAL-006`

### 当前决定
- 保留源码，但不启用。
- 后续若要重启，不应直接接回主流程，而应先重新验证设计目标、兼容性和失败模式。

### 与 VAL-005 的关系
- `VAL-006` 是“当前不启用”的产品与工程决策。
- `VAL-005` 是“如果未来要启用，该模块内部还有已知逻辑风险”的实现问题。
