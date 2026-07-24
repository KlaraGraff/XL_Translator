# Phase 5 开工记录：Word 翻译工作流

状态：`in_progress`

## 入口与范围

- 上一阶段验收：[2026-07-24_phase-04-excel_acceptance.md](2026-07-24_phase-04-excel_acceptance.md)，状态 `passed`，阻断项 `none`。
- 决策范围：`W5A-01–08`、`W5B-01–10`、`W5C-01–11`、`W5D-01–08`，以及 `X-01`、`X-02`、`X-10`、`X-13` 的 Word 侧接口。
- 不进入后续工作流：PDF/图片页级翻译、跨类型并行/总预算、更新与发布仍分别属于 Phase 6–9；本阶段不自行实现全局任务锁或预算。
- 外部暂缓：真实服务 Key、Windows 新版、macOS 12 双架构实机/Office 自动化发布验证。阶段内使用隔离数据、Mock provider 和固定 Word 样例。

## 四线任务与边界

| 执行线 | 任务 | 交付边界 |
| --- | --- | --- |
| L1 核心与契约 | `phase-05-core` | `core/`、`api/`、`settings.py` 中的 Word 扫描、`.doc` 高保真/回退转换、目录/域/封面保护、编号、段落批处理与严格重试、双语写入、恢复、TM 边界、文件终态与报告。 |
| L2 Tauri 与 UI | `phase-05-ui` | `ui/`（必要时 `src-tauri/`）中的 Word 清单/概况、`.doc` 风险与确认、独立输出、编号/批处理/重试/复核控件、运行/恢复状态、停止确认和结果详情。 |
| L3 测试与兼容 | `phase-05-test` | `tests/`、隔离 Mock/动态 `.docx` 与 `.doc` 样例及验收证据；覆盖正文/表格/编号/目录/域/封面/高亮、明确回退、停止/恢复、仲裁/TM 与结果契约；不修改生产代码。 |
| L4 集成与门禁 | 本体 | 接口仲裁、语言/TM/模型快照汇合、输出/诊断契约核对、质量门、动态验收与放行结论。 |

## 共享契约与隔离

- 共享 API：`/api/sources/scan`、`/api/tasks`、`/api/tasks/{id}`、`/api/tasks/{id}/events`、`/api/results/*`，及已冻结的语言、TM、模型角色、吞吐和任务快照接口。
- 共享核心模块：`core/word_document.py`、`core/word_task_runner.py`、`core/word_converter.py`、`core/word_*` 写入/恢复模块、`core/task_runner.py`、`api/task_manager.py` 和 `settings.py`。
- 动态测试目录：`.runtime/self-tests/phase-05-word/`；测试启动前隔离 `HOME`、`USERPROFILE`、`TEMP`、`TMP` 与 `TRANSLATOR_APP_DATA_DIR`。

## 阻断条件

- `.doc` 未先转临时 `.docx`、高保真不可用/授权拒绝时静默降级，或未取得用户明确兼容确认。
- 目录、域、封面、表格或自动编号被意外破坏；批处理阈值不满足、严格重试扩大到不应重试的内容，或失败/未恢复译文污染 TM。
- 转换前未知统计被伪造为零；文件终态、恢复、转换方法、编号、复核、语言/仲裁统计、报告或脱敏诊断不可解释。
- 任务未冻结 Phase 1–4 的语言、TM、模型、Key、吞吐和输出快照，或 Word 代码提前实现 Phase 7 的全局调度锁。
- UI/Tauri、质量门、阶段直接动态测试或结果字段契约失败。

本记录仅允许启动本阶段三条子 Agent 线与 L4 集成；在 Phase 5 验收记录为 `passed` 前，不得开始 Phase 6 代码变更。
