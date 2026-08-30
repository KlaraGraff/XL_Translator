# LibreOffice 保公式转换路线调查（2026-08-30）

背景：用户提出「兼容转换保留的原表若能带公式才是最好的路线」。本文是三路调查（本仓库
LibreOffice 集成摸底、Excel 转换架构、外部保真度调研）加一次本机实测的汇总，供产品拍板。

## 结论先行

**路线成立，且比预期更顺。** 本机实测（LibreOffice 26.8.0.3，headless 命令行转换一个带
公式/样式/合并/日期的 .xls）12 项检查全部存活：公式（含跨表引用）、公式缓存值、合并单元格、
字体加粗/颜色/底色/边框、数字格式、日期、列宽，单文件耗时 1.3 秒。配合已核实的架构事实——
「_原文」副本是转换产物的**字节级克隆**且快照发生在翻译写入之前（xlsx_patcher.py:1843、
:2268-2276）——只要转换产物带公式，「_原文」副本就自动带公式，不需要改克隆逻辑一行代码。

## 证据

### 1. 本仓库已有 LibreOffice 集成（Word 管线）

- soffice 探测：`core/word_converter.py:570-593`（`_find_soffice`），macOS 先查
  `/Applications/LibreOffice.app/Contents/MacOS/soffice`，再退回 PATH。LibreOffice 是
  **用户自装**，不随安装包分发（README.md:54）。
- `.doc→.docx` 用的就是 headless `--convert-to docx`（word_converter.py:195-204），
  超时 180 秒。与 .xls→.xlsx 需要的调用方式完全同构。
- b307e3a 那套「内置 Python 签名预检」只服务于 UNO 编号预处理场景；纯格式转换用不到
  UNO，不需要搬这套复杂度。复用面只有 `_find_soffice` + 子进程调用两个通用件。

### 2. Excel 架构的插入点

- 现状二元分流：`task_runner.py:824-830` 按 `_allow_xls_fallback` 选
  `convert_with_excel`（xlwings + 真 Excel）或 `convert_with_fallback`（xlrd 值化，
  公式必丢——xlrd 根本不暴露公式字符串）。
- 「_原文」副本：`_snapshot_sheet`（xlsx_patcher.py:1841-1869）读的是分表 XML **原始字节**，
  `<f>` 公式元素原样保留；快照在翻译写入循环（:2284 起）之前完成（:2268-2276），克隆写出
  （:2332-2343）消费的是早已固化的字节。转换产物有公式 ⇒ 副本就有公式，链路已通。
- 假设「兼容=值化」的现有钉子：file_scanner 两条告警、task_manager.py:1478 预检文案、
  workspace.ts 弹窗文案、`tests/test_audit_excel_fixes.py` 的文案钉子、
  `tests/test_phase4_excel_contracts.py:354-397` 与 `tests/test_audit_task_runner_fixes.py:234-289`
  的 `convert_with_fallback` 打桩断言。这些是落地时要同步改的全部清单。

### 3. 保真度：外部调研 + 本机实测

外部证据碎片化（多为二手归纳），因此以本机实测为准：

| 要素 | 实测结果 | 备注 |
| --- | --- | --- |
| 公式（同表 SUM/IF、跨表引用） | ✅ 存活 | 缓存计算值也在 |
| 合并单元格 | ✅ 存活 | |
| 字体加粗/红色、底色、边框 | ✅ 存活 | |
| 数字格式、日期格式 | ✅ 存活 | |
| 列宽 | ✅ 存活 | |
| 图表、图片 | ⚠️ 未实测 | xlwt 造不出来；外部证据显示图表是相对弱项。需拿真实样本验证 |
| VBA 宏 | ❌ 不适用 | 输出为 .xlsx，任何转换方式都带不了宏；原始 .xls 里的宏不受影响 |

工程坑（外部证据扎实）：两个 soffice 进程不能共享用户配置目录，用户开着 LibreOffice
图形界面时 headless 会静默失败——解法是每次转换传独立 `-env:UserInstallation`（本仓库
UNO 路径 word_converter.py:308-319 已是这个做法；实测脚本也用了，工作正常）。
顺带发现：Word 的 `--convert-to` 路径没加这个隔离，已发任务卡片单独修。

## 方案与推荐

**推荐：把 LibreOffice 藏进「兼容转换」内部，不加第三个按钮。**
兼容转换执行时先探测 soffice：有 → LibreOffice 转换（公式样式合并全保留）；
没有/失败 → 现有 xlrd 值化兜底。授权语义保持二元（「高保真 / 兼容」），API 和
弹窗按钮全不动，只有弹窗文案按「本机是否装了 LibreOffice」分两个变体：

- 装了：兼容转换会用本机 LibreOffice 转换，公式、样式、合并单元格通常能保留
  （图表、图片可能有出入）。
- 没装：维持刚落地的后果导向文案（公式变数值、样式丢失），可附一句
  「安装免费的 LibreOffice 可保留公式与样式」。

理由：没装 Excel 的用户正是最可能装 LibreOffice 的人群；显式第三档要把
`allow_xls_fallback` 从布尔改三态、前后端授权语义连动、弹窗三按钮——产品面翻倍，
收益却和「藏在兼容里」一样。

改动清单（若拍板做）：`xls_converter.py` 新增 `convert_with_libreoffice`（迁出/复用
`_find_soffice`）、`task_runner.py` 兼容分支先试 LO、扫描或预检暴露「本机有无 soffice」
给前端、弹窗文案双变体、上述测试钉子同步调整。规模估计：一个工作日内，无 schema 变更。

## 遗留未核实点

1. 图表/图片保真度需真实 .xls 样本实测（拍板后第一步）。
2. LibreOffice 转换失败的具体报错形态（损坏文件、密码保护文件）需在实现时补测试。
3. 转换耗时随文件大小的伸缩（实测 5KB 文件 1.3 秒，大头是进程冷启动，量级在个位秒/文件）。
