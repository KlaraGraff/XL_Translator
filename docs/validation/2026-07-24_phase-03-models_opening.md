# Phase 3 开工记录：模型、专业领域与提示词

状态：`in_progress`

## 入口与范围

- 上一阶段验收：[2026-07-24_phase-02-tm_acceptance.md](2026-07-24_phase-02-tm_acceptance.md)，状态 `passed`。
- 决策范围：M3A-01–09、M3B-01–10、M3C-01–11，以及 X-05、X-06、X-10、X-11 的本阶段接口。
- 不进入后续工作流：Excel、Word、PDF/图片的完整执行与多任务预算仍分别属于 Phase 4–7。
- 外部暂缓：真实服务 Key 连通性、Windows 新版；测试使用隔离数据与 Mock provider。

## 四线任务与边界

| 执行线 | 任务 | 交付边界 |
| --- | --- | --- |
| L1 核心与契约 | `phase-03-core` | `core/`、`api/`、`settings.py` 中的角色、连接、Prompt、吞吐、快照和 v3 交换契约。 |
| L2 Tauri 与 UI | `phase-03-ui` | `ui/`（必要时 `src-tauri/`）中的四角色配置、领域/Prompt、吞吐和导入导出交互。 |
| L3 测试与兼容 | `phase-03-test` | `tests/`、隔离 Mock/动态测试与验收证据；不修改生产代码。 |
| L4 集成与门禁 | 本体 | 接口仲裁、跨线整合、质量门、动态验收与放行结论。 |

## 共享契约与隔离

- 共享 API：`/api/models/roles`、`/api/models/roles/{role}`、`/api/models/throughput/{role}`、`/api/models/fetch`、`/api/model-config/*`、`/api/domains/{excel|word}`。
- 共享核心模块：`core/model_roles.py`、`core/model_config.py`、`core/model_catalog.py`、`core/model_throughput.py`、`core/model_api_identity.py`。
- 动态测试目录：`.runtime/self-tests/phase-03-models/`；测试启动前隔离 `HOME`、`USERPROFILE`、`TEMP`、`TMP` 与 `TRANSLATOR_APP_DATA_DIR`。

## 阻断条件

- 任一角色违反能力限制或出现链式/循环连接复用。
- 配置变化未使对应角色测试状态失效，或运行中任务配置/吞吐/Key 作用域发生漂移。
- 模型目录、导出、诊断或日志持久化/暴露 API Key。
- 领域或清洗 Prompt 能绕过固定 JSON、语言、格式、TM 审核或 PDF 内置协议。
- UI/Tauri、质量门或阶段直接动态测试失败。

本记录仅允许启动本阶段三条子 Agent 线与 L4 集成；在 Phase 3 验收记录为 `passed` 前，不得开始 Phase 4 代码变更。
