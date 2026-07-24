# Phase 4 开工记录：Excel 翻译工作流

状态：`in_progress`

## 入口与范围

- 上一阶段验收：[2026-07-24_phase-03-models_acceptance.md](2026-07-24_phase-03-models_acceptance.md)，状态 `passed`，阻断项 `none`。
- 决策范围：`E4A-01–08`、`E4B-01–11`、`E4C-01–10`、`E4D-01–08`，以及 `X-01`、`X-02`、`X-10`、`X-13` 的 Excel 侧接口。
- 不进入后续工作流：Word、PDF/图片完整执行与跨类型并发预算仍分别属于 Phase 5–7；Phase 4 不自行实现全局锁或全局预算。
- 外部暂缓：真实服务 Key、Windows 新版、macOS 12 双架构实机/Office 自动化发布验证。阶段内使用隔离数据、Mock provider 和固定样例。

## 四线任务与边界

| 执行线 | 任务 | 交付边界 |
| --- | --- | --- |
| L1 核心与契约 | `phase-04-core` | `core/`、`api/`、`settings.py` 中的 Excel 扫描、按文件预检、实际语言回报、TM/自动入库边界、双语写入、文件终态与 `.xls` 兼容语义。 |
| L2 Tauri 与 UI | `phase-04-ui` | `ui/`（必要时 `src-tauri/`）中的 Excel 清单/概况、预检、输出和覆盖保护、保真风险/兼容确认、运行进度、停止确认和结果详情。 |
| L3 测试与兼容 | `phase-04-test` | `tests/`、隔离 Mock/动态样例和验收证据；覆盖 `.xlsx` 结构保真、按文件预检、TM 实际语言对、停止/容错和 `.xls` 高保真或明确回退；不修改生产代码。 |
| L4 集成与门禁 | 本体 | 接口仲裁、语言/TM/模型快照汇合、结果契约核对、质量门、动态验收与放行结论。 |

## 共享契约与隔离

- 共享 API：`/api/sources/scan`、`/api/languages/preflight`、`/api/tasks`、`/api/tasks/{id}`、`/api/tasks/{id}/events`、`/api/results/*`，及已冻结的语言、TM、模型角色和吞吐接口。
- 共享核心模块：`core/file_scanner.py`、`core/language_preflight.py`、`core/task_runner.py`、`core/engine_dispatcher.py`、`core/tm_manager.py`、Excel 处理/输出模块及 `api/task_manager.py`。
- 动态测试目录：`.runtime/self-tests/phase-04-excel/`；测试启动前隔离 `HOME`、`USERPROFILE`、`TEMP`、`TMP` 与 `TRANSLATOR_APP_DATA_DIR`。

## 阻断条件

- 自动模式未做到每个有候选内容的文件恰好一次预检，或上传完整文件、生成 `auto-*`、把 `mixed`/`und`/越界语言结果自动入库。
- 公式、样式、合并单元格、行高、已有底色、源文件或输出保护出现 P0/P1 数据损坏；单文件失败错误阻断其他文件。
- `.xls` 在无高保真 Excel 自动化路径、权限拒绝或未确认兼容模式时发生静默降级。
- 任务未冻结 Phase 1–3 的语言、TM、模型、Key、吞吐和输出快照，或 Excel 代码提前实现 Phase 7 的全局调度锁。
- UI/Tauri、质量门、阶段直接动态测试或结果字段契约失败。

本记录仅允许启动本阶段三条子 Agent 线与 L4 集成；在 Phase 4 验收记录为 `passed` 前，不得开始 Phase 5 代码变更。
