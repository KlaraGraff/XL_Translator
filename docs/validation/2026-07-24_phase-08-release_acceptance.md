# Phase 8 验收记录：更新、帮助、维护、诊断与 macOS 发布

状态：`blocked-by-gate`

执行日期：`2026-07-24`

实现提交：`0bdc2ea feat: complete phase 8 macOS release maintenance`

## 范围与四线证据

- 前置阶段：[Phase 7 调度验收](2026-07-24_phase-07-scheduler_acceptance.md) 状态为 `passed`，阻断项为 `none`。
- L1 `phase-08-core`：实现仅 macOS 的稳定 tag/架构/DMG/SHA-256 更新匹配；新数据基线、维护概览和受控清理/`RESET` 完整重置；匿名诊断；移除旧数据迁移路由、实现和旧路径探测。
- L2 `phase-08-ui`：实现快速开始、离线帮助、更新提示、维护/诊断页面和 macOS 12 Apple Events 配置；隔离当前源码壳启动完成。
- L3 `phase-08-test`：实现正式 tag、版本一致性、macOS 12、Safari 15.1、Mach-O、签名/公证前置条件、更新、维护和诊断的合同测试及发布脚本门。
- L4：汇合四线，修复全量回归中暴露的 TM 隔离钩子、匿名诊断旧断言和任务模型快照脱敏错误；确认不会恢复旧数据迁移或放宽 API Key 预检。

## 本地验收通过项

- 新版仅发布 macOS；更新检查只接受稳定 `vX.Y.Z`，且只提供本机 `arm64` 或 `x86_64` 的 DMG 与同名 SHA-256。Windows 新版入口、构建与发布配置均已下线。
- `minimumSystemVersion=12.0`、Safari `15.1` 构建目标、Apple Events 用途说明/entitlement、版本一致性和签名前 Mach-O 扫描均被接入代码与合同测试。
- 快速开始、离线帮助、后台更新 24 小时门、手动检查、版本级忽略、全局提醒暂停、维护概览、分类清理、Key/设置/TM 重置和 `RESET` 完整重置可用。
- 维护不会删除用户源文件、输出目录或翻译产物；活动任务会阻止会影响凭据、任务记录和 TM 的清理。
- 诊断仅保留匿名计数、阶段、运行环境和脱敏连接摘要；不导出 API Key、原文、译文、Prompt、模型响应、文件名、绝对路径或 `app.log`。
- 当前数据基线拒绝旧、未来或损坏的 settings/TM schema，不读取、复制、迁移、修复或删除旧版目录数据。

## 验证记录

| 命令/检查 | 结果 | 覆盖 |
| --- | --- | --- |
| `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1` | 通过（`All checks passed!`） | 当前工作树静态检查 |
| 隔离 `run_all_tests.py` | 通过，396 tests，0 failures，0 errors | 全量 Python 回归；产物 `.runtime/self-tests/phase-08-release-l4/artifacts/python-regression-l4.txt` |
| 隔离 `run_phase8_contracts.py` | 通过，23 tests | macOS-only 发布、版本/签名门、更新匹配、维护、重置、诊断和旧迁移下线 |
| 隔离 `check_phase8_core.py` | 通过 | 快速开始/更新 Mock、诊断匿名导出、完整重置及旧目录不受影响 |
| `cd ui && npm run check && npm run build` | 通过 | TypeScript 与生产构建；仅有既有 Vite 动态导入提示 |
| `cd src-tauri && cargo test && cargo check` | 通过，3 Rust tests | sidecar 握手、外链限制与 Rust 编译 |
| 隔离当前源码 `tauri dev` | 通过 | 关闭旧项目进程后启动 `target/debug/translator` PID `89609`（`2026-07-24 11:54:30`），sidecar PID `89617`（`11:54:32`），工作目录为仓库 `src-tauri`；监听 `127.0.0.1:59916`，无 token 的 `/health` 返回 `401`，启动时的内部 token 健康门已通过 |

## 外部门禁与阻断原因

本机为 macOS `26.5.2` / `arm64`，项目虚拟环境为 Python `3.13.13`。这不能替代正式门要求的 macOS 12.0 原生 `arm64` 与原生 `x86_64` 实机，以及受控 Python 3.11 发布环境。以下证据尚不存在：

1. 两架构的正式 DMG、每个二进制的 `minos <= 12.0` 和目标架构扫描记录。
2. Apple Developer ID 签名、Hardened Runtime、notarize、staple、Gatekeeper 验证凭据及结果。
3. macOS 12 arm64 与 x86_64 上的安装、首次启动、sidecar、Mock 标准 Excel/Word/PDF/图片流程、Apple Events 允许/拒绝路径的实机证据。

真实 API Key 和真实翻译验收仍按已确认例外暂缓；本阶段没有将 Mock 结果表述为真实服务验收。

## 结论与放行

本地实现和可重复 Mock 验收均通过，但 Phase 8 的正式发布门未通过，状态必须保持 `blocked-by-gate`。依据实施方案，不创建 Phase 9 的三个子 Agent 任务、不进入 Phase 9 编码、不创建 Release tag 或发布资产。待上述三类外部证据齐备后，重跑 Phase 8 发布门；只有验收更新为 `passed`，才允许建立 Phase 9 开工记录。
