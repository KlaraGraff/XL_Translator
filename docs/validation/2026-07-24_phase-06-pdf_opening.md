# Phase 6 开工记录：PDF 与图片翻译

状态：`completed；验收结果见 [2026-07-24_phase-06-pdf_acceptance.md](2026-07-24_phase-06-pdf_acceptance.md)`

## 前置放行

- 上一阶段验收：[2026-07-24_phase-05-word_acceptance.md](2026-07-24_phase-05-word_acceptance.md)，状态 `passed`，阻断项 `none`。
- 决策范围：`P6A-01–08`、`P6B-01–10`、`P6C-01–11`、`P6D-01–09`，以及 `X-06`、`X-07`、`X-08`、`X-10` 的 PDF/图片侧接口。
- 本阶段只实现 PDF/图片工作流；跨类型并发风险确认、总资源预算、统一任务中心、跨类型 Finder 操作与通用日志详情仍属于 Phase 7，更新、诊断、正式发布属于 Phase 8–9。
- 外部暂缓：真实服务 Key/真实模型翻译、Windows 新版，以及 macOS 12 双架构实机安装、签名和公证。阶段内使用隔离应用数据、Mock 图像/审核客户端与固定 PDF、图片样例。

## 四线登记

| 执行线 | 任务名 | 文件边界与交付 |
| --- | --- | --- |
| L1 核心与契约 | `phase-06-core` | `core/pdf_image_translation.py`、`core/pdf_review.py`、`api/`、`settings.py` 及必要共享契约：PDF/图片递归扫描、跳过项、独立设置与任务快照、页面素材/版式/失败语义、审核、重试、暂停/继续、manifest/报告与文件/页面终态。不得改 UI。 |
| L2 Tauri 与 UI | `phase-06-ui` | `ui/`（必要时 `src-tauri/`）中的 PDF/图片路径、图片范围、扫描概况、独立输出、审核前置、页级进度/恢复、结果与页面证据呈现。不得修改核心翻译语义或 Phase 7 调度。 |
| L3 测试与兼容 | `phase-06-test` | `tests/`、`.runtime/self-tests/phase-06-pdf/` 与验收证据：隔离 Mock PDF/图片、页面比例、失败占位、审核、重试、暂停/继续、manifest/报告和敏感数据边界；不修改生产代码。 |
| L4 主线集成与门禁 | 本体 | 汇合 API/UI/结果契约，解决跨线冲突，运行质量门、直接动态回归、UI/Rust 构建及隔离 Tauri 源码壳验证，写入验收记录并决定是否放行 Phase 7。 |

## 共享契约与隔离

- 共享接口：`/api/sources/scan`（PDF/图片返回 `items`、`skipped`、`summary`、`risk`）、`/api/tasks`、任务状态与 SSE 的 `pdf_page_recovery` / `pdf_review` 事件；结果和 manifest 不得含 Key、完整 Prompt、原始模型响应或图片二进制。
- PDF/图片不读写 TM；目标语言、输出目录、压缩/素材/审核策略和模型角色在启动时冻结，运行中编辑只影响未来任务。
- 动态测试目录：`.runtime/self-tests/phase-06-pdf/`；导入业务模块前隔离 `HOME`、`USERPROFILE`、`TEMP`、`TMP` 与 `TRANSLATOR_APP_DATA_DIR`。
- 共享 API 预算/跨任务锁只消费已有接口，不在 Phase 6 提前实现 Phase 7 全局策略。

## 阻断条件

- 未能同时证明 PDF 与独立图片输入、混合清单、跳过可见性和输出递归排除。
- PDF 未做到一页对一页、失败占位、素材可追溯，或图片失败伪装为成功输出。
- 审核开关、重试/临时异常/不可恢复错误、暂停提交/继续/结束暂停的终态与证据不符合 `P6C`。
- manifest/报告、文件/页面/审核数据契约缺失，或泄露 Key、Prompt、原始响应、图片二进制。
- 任一执行线缺少变更、验证、未覆盖项或风险证据；`quality_gate.ps1`、阶段直接动态测试、UI/Rust 验证或隔离源码壳失败。

本记录允许仅本阶段的 `phase-06-core`、`phase-06-ui`、`phase-06-test` 与 L4 并行工作。Phase 6 验收记录未为 `passed` 前，不得开始 Phase 7 的任何代码变更。
