# Phase 5 验收记录：Word 翻译工作流

状态：`passed`

执行日期：`2026-07-24`

实现提交：`e196cf7 feat: complete Word workflow phase`

## 范围与四线证据

- 上一阶段：[Phase 4 Excel 验收](2026-07-24_phase-04-excel_acceptance.md)，状态 `passed`。
- L1 核心与契约：Word 扫描、显式 `.doc` 兼容确认、独立输出/设置快照、逐项语言回报的 TM 门、恢复与 W5D 终态契约。
- L2 UI/Tauri：Word 清单、未知统计、兼容确认、独立输出检查、编号/批处理/复核控制、`word_recovery` 实时状态及结果详情。
- L3 测试与兼容：隔离 Mock Word 回归、`.doc`/目录/域/封面/编号/恢复/TM/报告失败/停止边界。
- L4 集成：接口仲裁、报告失败可见性复核、全套质量门、动态回归及当前源码 Tauri 壳核验。

## 已验收行为

- `.docx` 与目录递归扫描提供文件、相对路径、格式、正文/表格统计、跳过项、汇总及风险；`.doc` 的段落/表格统计为转换后的未知值，绝不伪造为零。
- `.doc` 默认只走 Microsoft Word 高保真路径；LibreOffice 或 `textutil` 转换必须由本次任务显式传递 `allow_doc_fallback=true`。高保真失败不会静默降级，最终输出固定 `.docx`。
- Word 的输出、编号、批处理与复核设置独立于 Excel/PDF，并在任务开始时冻结；输出目录不会覆盖源文件或既有结果。
- 目录、域和封面保护、双语正文/表格写入、编号保守回退、严格重试、恢复/仲裁与 TM 写入边界均已纳入可解释结果。
- 自动模式按每个文件一次预检，并只对模型逐项回报、且在该文件预检候选范围内的实际语言对写入 TM；不产生 `auto-*`。
- 终态为每个已选文件提供状态、路径、格式、转换/编号记录、错误；任务提供 KPI、语言、恢复、复核、报告路径及 `report_warning`。报告写入失败只产生警告，不丢弃已生成的 Word 产物。

## 验证记录

| 命令/检查 | 结果 | 覆盖 |
| --- | --- | --- |
| `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1` | 通过 | Python 静态检查 |
| 隔离 `./.venv/bin/python3 -m unittest`（Phase 5 Word 套件） | 80 passed | `.doc` 确认、扫描统计、结构保护、批处理、重试/恢复、TM 门、报告失败、停止与文件终态 |
| 隔离 `./.venv/bin/python3 -m unittest tests.test_api_app tests.test_api_task_selection tests.test_api_concurrency_control` | 10 passed | API、SSE、任务选择与资源控制回归 |
| `npm run check` 与 `npm run build`（`ui/`） | 通过 | TypeScript 与生产前端构建；仅有既存 Vite dynamic-import 提示 |
| `cargo test` 与 `cargo check`（`src-tauri/`） | 通过 | Rust 壳层 |
| 隔离 `TRANSLATOR_APP_DATA_DIR` 的 `tauri dev` | 通过 | 新进程 `39785` → `target/debug/translator` `39858` → `api.launcher` `41088`；监听 `127.0.0.1:52368`，源码壳的认证 `/health` 启动门已通过 |

动态测试产物：`.runtime/self-tests/phase-05-word/`。

## 未执行项与范围

- 真实 API Key/真实翻译验收按已确认例外暂缓；本阶段使用隔离 Mock 与固定样例。
- Windows 新版、Windows 验收和发布不在新版范围内。
- macOS 12 双架构实机、签名/公证及真实 Office/LibreOffice 自动化验收属于 Phase 8/9 发布门；本阶段只验证明确失败/回退路径。
- 直接 UI DOM/辅助功能树检查受本机 macOS 会话锁定限制；源码 Tauri 进程、路径及认证 sidecar 健康门已核验。该限制不影响已通过的 TypeScript、Rust 与 API/Word 动态测试，但将在有可交互会话时补做页面级检查。

阻断项：`none`。Phase 5 满足放行条件，允许建立 Phase 6 开工记录。
