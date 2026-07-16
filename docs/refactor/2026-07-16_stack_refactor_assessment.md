# 整体重构升级评估：体积、工具链与语言选型

- **日期**：2026-07-16
- **背景**：安装包已达约 140MB；考虑是否有更优逻辑实现、更好的工具包、以及是否从 Python 转向 Rust 等语言。
- **关联**：UI 重设计已暂停在第 1 步之后（`docs/redesign/2026-07-16_ui_redesign_progress.md`），因为本评估的结论直接决定 UI 第 2 步在哪个壳层上做。

---

## TL;DR（推荐结论）

1. **不建议现在改写为 Rust（或任何语言的全量重写）。** 本应用的核心价值在 Excel/Word/PDF 的格式边缘处理——这恰是 Python 生态全网最强、Rust 生态明显不成熟的部分。全量重写等于拿最弱的生态去重做你最成熟的资产（v7.4、380 个测试、七个大版本积累的边缘修复）。
2. **立刻可做三件低风险瘦身，预计 140MB → 90–100MB**：PyMuPDF→pypdfium2、云端 SDK 收敛为 OpenAI 兼容单客户端、PyInstaller 裁剪。与任何重构路线兼容，做了不亏。
3. **真正的战略分叉只有一个：UI 壳层。** 留 PySide6（路线 A）或换 Tauri 壳 + Python 引擎 sidecar（路线 B）。这个决策应在恢复 UI 第 2 步**之前**做出——所以现在暂停 UI 是对的。
4. 对 UI 重设计的影响：**设计（token/原型/图标）零损失**，任何壳层都能用；已提交的 Qt 主题层（commit `1091f30`）只在留 Qt 时有效，沉没成本很小；UI 第 2 步（贵的部分）是纯 Qt 工作，必须等壳层定了再动。

---

## 一、140MB 到底花在哪（实测）

对本仓库依赖在 macOS arm64 venv 内实测（安装后解压体积；安装包是其压缩子集）：

| 依赖 | 实测体积 | 用途 | 判定 |
|---|---:|---|---|
| PySide6 (Essentials) | **333MB** | GUI 壳层 | 最大头。战略分叉点（见路线 A/B） |
| pymupdf | **58MB** | 仅 `core/pdf_image_translation.py` | **换掉**（见下） |
| lxml | 20MB | python-docx 依赖 | 保留（随 docx 必要） |
| openai | 19MB | OpenAI 引擎 | 收敛后保留为唯一 SDK 或改 httpx 直连 |
| Pillow | 14MB | PDF 页图处理 | 保留 |
| cryptography | 12MB | **仅 dashscope 拖入** | 随 SDK 收敛移除 |
| anthropic | 12MB | Claude 引擎 | 换薄客户端（httpx 直连，约百行） |
| dashscope | 6.4MB(+aiohttp 3.2MB+websocket-client) | 通义引擎 | **移除**：阿里云已提供 OpenAI 兼容端点 |
| xlwings | 4.6MB | .xls 转换 + Excel 精调行高（3 个文件） | 保留（运行时可选，依赖本机 Excel） |
| zhipuai | ~2MB | 智谱引擎 | **移除**：智谱已提供 OpenAI 兼容端点 |

结论：**140MB ≈ Qt(大头) + MuPDF + 四家云 SDK 及其传递依赖 + CPython 运行时(~25MB)**。这对 Python+Qt 桌面应用是正常水平，但有明确压缩空间。

## 二、工具包逐项审计

### 建议替换的

**1. PyMuPDF → pypdfium2 + Pillow**（收益最大、风险最小）

- 实测使用面极窄：只有 1 个文件，8 种调用（`fitz.open/doc.save/get_pixmap/Rect/Matrix/new_page/load_page/close`）——渲染页图 + 把译图组装回 PDF。
- pypdfium2（Chrome 的 PDF 引擎 pdfium 的绑定）轮子实测 **3.3MB** vs pymupdf 装后 58MB；渲染质量优秀、CJK 处理好。页图组装可用 Pillow 的多页 PDF 输出（`save_all=True`）或 pypdfium2 原生 API，结构性操作（如需拆合页）补 pikepdf（MPL）。
- **附带解决一个合规隐患**：PyMuPDF 是 AGPL 授权，而本仓库**没有 LICENSE 文件**（默认版权保留）。以当前形态分发二进制安装包，严格说与 AGPL 义务存在冲突。pypdfium2 是 Apache/BSD，无此问题。（顺带建议：无论如何给仓库补一个 LICENSE。）
- 预估工作量：1–2 人日（含回归 PDF 测试）。

**2. 四家云 SDK → 一个 OpenAI 兼容客户端（httpx）**

- 现状：`engines/` 下 anthropic / openai / zhipuai / dashscope 四个 SDK 各带一坨传递依赖（dashscope 一家就拖进 cryptography 12MB + aiohttp + websocket-client ≈ 22MB）。
- 智谱与阿里云通义**均已提供 OpenAI 兼容端点**，本应用早已有 `cloud_base_url` 与"自定义网关"概念，engines 层本质就是"发 chat 请求、收文本"。收敛为一个基于 httpx 的 OpenAI 兼容客户端 + 每服务商一份 base_url 预设即可；Anthropic 协议略有差异，写一个约百行的薄客户端或保留官方 SDK 二选一。
- 收益：依赖面大幅缩小、重试/超时/并发逻辑只写一份、以后接新服务商=加一条配置而不是加一个 SDK。
- 预估工作量：2–4 人日（engines 层已有 base_engine 抽象，改动集中）。

**3. PyInstaller 裁剪**

- spec 已排除 numpy/pandas，可再排除未用到的 Qt 模块/插件/翻译文件（QtQml、QtQuick、不用的 imageformats、translations 目录等）。
- 预估工作量：1 人日 + 双平台冒烟（已有 `--smoke-test` 流程）。

**三项合计预估：安装包 140MB → 90–100MB 量级**，全部与后续任何重构路线兼容。

### 建议保留的

| 工具 | 理由 |
|---|---|
| **openpyxl** | 全生态（含所有语言）**编辑保真**最强的 xlsx 库：读入-修改-写回并保留样式/合并/公式。这是本应用双语输出的命脉。Rust 无成熟等价物（见语言分析） |
| **python-docx** | 同上之于 docx。Word 双语生成涉及编号/表格/页眉等复杂结构，v7.x 多个版本的修复都沉淀在这层之上 |
| **xlwings** | 只用于 .xls 转换与"Excel 精调行高"，本就是运行时可选路径，无替代必要 |
| **xlrd** | 仅读遗留 .xls，小而稳 |
| **pydantic / loguru / httpx / tenacity / psutil** | 小、稳、无更优替代 |
| **PyInstaller** | 成熟；Nuitka 等替代收益边际且引入新风险 |

## 三、语言选型分析

判断框架：**本应用三层的难度分布极不均匀。**

- **文档处理层（护城河所在）**：难点全在格式边缘情况。语言表达力不是瓶颈，**库的成熟度才是**。
- **LLM API 层**：就是发 HTTP 收 JSON，任何语言都琐碎，不构成选型依据。
- **GUI/打包层**：Python 最弱的一层（运行时+Qt 体积），也是唯一真正值得重新考虑的一层。

### Rust

- ✅ 单二进制、体积小（Tauri 全家桶 15–25MB）、性能好。
- ❌ **致命短板在文档层**：calamine 只读、rust_xlsxwriter 只写新文件，"读入-修改-写回保真"的 umya-spreadsheet 成熟度远逊 openpyxl；docx 生态（docx-rs 等）与 python-docx 差距更大。等于把最难、最依赖积累的部分推倒重来。
- ❌ 380 个测试需重写；七个版本的边缘修复面临系统性回归风险。
- **结论：现在不换。** 性能也不是本应用瓶颈（瓶颈在网络 API 延迟，Python 的并发批处理已够用）。

### Go / C#（顺带回答"有没有更好的语言表达"）

- Go：excelize 确实优秀，但 docx 生态弱、GUI 弱（Wails 与 Tauri 同路），综合不如"Python 引擎 + 换壳"。
- C#：Open XML SDK 是微软官方的 docx/xlsx 参考实现，其实是**Office 文档类应用理论上的最佳语言**；但跨平台 GUI 要走 Avalonia，且全量重写成本与 Rust 同级。若某天真要换语言，C# 优先于 Rust——但现在同样没有充分理由。
- **Python 仍是这个领域表达力和生态匹配度最高的语言。** LLM 辅助维护下开发速度也最快。

## 四、战略分叉：UI 壳层三条路线

| | A · 留 PySide6 | B · Tauri 2 壳 + Python sidecar | C · 全 Rust |
|---|---|---|---|
| 安装包 | ~90–100MB（做完瘦身） | ~50–70MB（无 Qt 的 PyInstaller 引擎 + 系统 WebView 壳） | ~15–25MB |
| UI 重设计怎么落地 | 恢复第 2 步，Qt 里逐页重写版式 | **原型 HTML/CSS 直接就是真 UI**，1:1 落地，Qt 版式工作全部跳过 | 同 B |
| 文档处理层 | 不动 | **不动**（整个 core/engines 原样保留，测试保留） | 全部重写，高回归风险 |
| 新增复杂度 | 无 | 壳↔引擎 IPC（localhost HTTP 或 stdio JSON-RPC）；双进程生命周期 | 全栈 |
| 工作量（AI 辅助） | UI 第 2 步本身 ~1–2 人周 | ~2–4 人周 | ~2–4 人月起 |
| 风险 | 最低 | 中（IPC、打包双进程、macOS 公证多一个可执行体） | 高 |

关于 ADR-0001"native-only 主线"：当年放弃的是 **Streamlit 网页包装**（要开浏览器、起本地端口、非原生窗口）。Tauri 是真原生窗口 + 系统 WebView 渲染，无浏览器、无可见服务器，用户感知与原生应用一致——与当年否掉的东西不是一类。但既有 ADR 表达过对 web 技术栈的负面经验，此点应作为决策输入而非被无视。

### 推荐决策路径

1. **现在（与壳层决策无关）**：做三项瘦身 + 补 LICENSE。落点 90–100MB。
2. **然后二选一**：
   - 若 90–100MB 可接受、且看重 Qt 原生感与最低风险 → **路线 A**，恢复 UI 第 2 步。
   - 若安装包体积是硬指标（如 <70MB）、或希望原型像素级落地且未来 UI 迭代走 web 技术 → **路线 B**。
3. **路线 C 仅在**未来出现"Python 运行时本身成为不可接受负担"级别的新理由时再评估。

两条路线下 UI 重设计的资产利用率：

| 资产 | A | B |
|---|---|---|
| 设计决策 / token / 原型 / 图标 | ✅ 全用 | ✅ 全用（原型直接变 UI） |
| commit `1091f30`（Qt 主题层） | ✅ 是地基 | ❌ 弃用（约一天的沉没成本） |
| UI 第 2 步 Qt 版式工作 | 需要做 | 完全跳过 |

---

## 附：本评估的实测依据

- 依赖体积：`du -sh .venv/lib/python3.13/site-packages/*`（macOS arm64，PySide6 6.11.1）
- PyMuPDF 使用面：`grep` 全库，仅 `core/pdf_image_translation.py`，8 种调用
- SDK 传递依赖归属：`pip show` 的 Required-by 字段
- pypdfium2 体积：`pip download` 实测轮子 3.3MB
- xlwings 使用面：`core/bilingual_writer.py`、`core/excel_automation.py`、`core/xls_converter.py`
