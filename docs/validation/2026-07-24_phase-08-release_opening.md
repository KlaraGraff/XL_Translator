# Phase 8 开工记录：更新、帮助、维护、诊断与 macOS 发布

状态：`in_progress`

## 前置放行

- 上一阶段验收：[2026-07-24_phase-07-scheduler_acceptance.md](2026-07-24_phase-07-scheduler_acceptance.md)，状态 `passed`，阻断项 `none`。
- 决策范围：`U8A-01–17`、`H8B-01–12`、`D8C-01–14`、`X-09`、`X-10`、`X-11`、`X-12`、`X-13`。
- 本阶段实现 macOS-only 更新与发布流水线、快速开始和本地帮助、维护与脱敏诊断、当前新基线的未来 schema 保护，并彻底下线旧数据迁移。
- 不触碰旧版目录，不读取、复制、迁移、修复或删除旧版设置、Key、TM、模型或自定义语言数据。
- 外部发布门：正式 tag 必须在原生 macOS 12 arm64 与 x86_64 环境通过安装冒烟，且使用 Apple Developer ID 完成签名、公证、staple 与 Gatekeeper 验证；本地 Mock 只能覆盖实现契约，不能替代此门。真实服务 Key 和真实翻译验收继续暂缓。

## 四线登记

| 执行线 | 任务名 | 文件边界与交付 |
| --- | --- | --- |
| L1 核心与契约 | `phase-08-core` | `core/`、`api/`、`settings.py`、`config.py` 的更新检查、维护统计/清理/重置、诊断隐私、当前 schema 保护与旧数据迁移 API/实现下线。不得改 UI。 |
| L2 Tauri 与 UI | `phase-08-ui` | `ui/`、`src-tauri/` 的快速开始、帮助、更新、维护、诊断、重置及 macOS Apple Events 配置。不得改变后端业务语义。 |
| L3 测试与兼容 | `phase-08-test` | `tests/`、`scripts/`、`.github/`、`.runtime/self-tests/phase-08-release/` 的隔离 Mock 发布/更新/维护验证、macOS 12/Mach-O/架构门与 CI/文档证据。不得修改核心业务或 UI。 |
| L4 主线集成与门禁 | 本体 | 汇合跨线接口，审查隐私/重置/发布边界，运行质量门、动态回归、UI/Rust、源码壳与本地发布门；记录外部签名、公证和 macOS 12 双架构实机门，并决定是否允许进入 Phase 9。 |

## 共享契约与隔离

- Release 仅接受稳定 `vX.Y.Z` tag；标签、`app_meta.py`、`src-tauri/tauri.conf.json`、`src-tauri/Cargo.toml` 和 `ui/package.json` 版本必须一致。正式 Release 只在两个原生 macOS 架构 DMG、各自 SHA-256、签名和公证均成功后创建。
- 更新检查仅针对 macOS，按本机架构严格匹配 DMG 与 `.sha256` 资产；缺少任一资产时是“发布包未就绪”，不是可下载更新。后台检查只在快速开始完成/跳过后、最多 24 小时一次；手动检查不受提醒设置限制。
- 维护范围严格限于 `~/Library/Application Support/Translator` 的受控类别。源文件、输出目录、已生成翻译文件和 PDF 页面素材不可被维护清理；完整重置只能在无活动任务、确认勾选且输入 `RESET` 后进行。
- 诊断和日志不得保存或导出 API Key、原文、译文、完整 Prompt、模型原始响应、原始文件名、绝对路径、工作表名或整段 `app.log`。所有涉及用户状态的动态测试须在导入业务模块前隔离 `HOME`、`USERPROFILE`、`TEMP`、`TMP` 与 `TRANSLATOR_APP_DATA_DIR`。
- 动态测试目录：`.runtime/self-tests/phase-08-release/`。UI/Tauri 验收必须停止旧源码/安装包进程后，用隔离 `TRANSLATOR_APP_DATA_DIR` 重新启动当前源码。

## 阻断条件

- 任何 Windows 构建、安装说明、更新资产选择或旧数据迁移入口仍存在于新版产品路径。
- 更新检查把错误架构、缺 SHA-256 的资产或不完整 Release 当作可下载更新；正式 tag 在缺失签名/公证凭据时仍发布。
- 维护/重置可以触及用户输入或输出，活动任务期间可删除 Key 或完整重置，或诊断导出泄露正文、密钥、路径等敏感数据。
- `minimumSystemVersion`、Mach-O `minos`、目标架构、Safari 15.1 回退、Apple Events 配置任一静态门失败；或将未完成的 macOS 12 双架构签名/公证/实机验证写为通过。
- 任一执行线缺少变更、验证、未覆盖项或风险证据；`quality_gate.ps1`、阶段动态测试、UI/Rust 验证或隔离源码壳失败。

本记录仅允许 `phase-08-core`、`phase-08-ui`、`phase-08-test` 和 L4 在 Phase 8 范围内并行工作。Phase 8 验收记录未为 `passed` 前，不得开始 Phase 9 的任何代码变更。
