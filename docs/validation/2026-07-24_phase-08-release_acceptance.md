# Phase 8 验收记录：更新、帮助、维护、诊断与 macOS 发布

状态：`blocked-by-gate`

执行日期：`2026-07-24`

实现提交：`0bdc2ea feat: complete phase 8 macOS release maintenance`；CI 环境修复：`87c054a`、`c2a8e6d`；临时签名回退：`f4b4ac3 ci: fall back to temporary ad-hoc signing`

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

## GitHub 原生双架构未签名构建证据

手动运行 `30090148206`（提交 `c2a8e6d`）在原生 `macos-14` arm64 和 `macos-15-intel` x86_64 runner 上均成功；两个任务均通过 Python 3.11 受控虚拟环境、全量 Python 回归、UI/Rust 检查、原生打包和签名前 Mach-O 扫描。手动运行的正式签名/公证发布 job 按设计跳过。

| 架构 | 工件 | SHA-256 | Mach-O 报告 |
| --- | --- | --- | --- |
| arm64 | `Translator_macOS_arm64_8.0.0_UNSIGNED_TEST.dmg` | 通过 `.sha256` 复核 | `ok=true`，119 个，架构仅 `arm64`，`minos` 为 `11.0/12.0` |
| x86_64 | `Translator_macOS_x64_8.0.0_UNSIGNED_TEST.dmg` | 通过 `.sha256` 复核 | `ok=true`，119 个，架构仅 `x86_64`，`minos` 为 `10.9/10.10/10.12/10.13/12.0` |

两份 DMG 均通过本机 `hdiutil imageinfo` 的 UDZO 镜像检查。完整工件和报告保存在隔离目录 `.runtime/self-tests/phase-08-release/github-run-30090148206/`；这些证据证明 CI 原生构建与二进制下限门通过，但不等同于签名发布或 macOS 12 实机安装验收。

## GitHub 原生双架构临时 ad-hoc 签名构建证据

稳定标签 `v8.0.1` 指向 `f4b4ac3` 后，GitHub Actions 运行 [`30108715239`](https://github.com/KlaraGraff/XL_Translator/actions/runs/30108715239) 成功。由于仓库没有配置完整的 Apple Developer ID 和公证 Secrets，资格检查将该标签明确分类为 `temporary-test`：arm64 与 x86_64 构建任务均成功，正式发布任务按设计跳过。`gh release view v8.0.1` 返回 `release not found`，因此不存在 GitHub Release 或正式下载资产。

| 架构 | 临时工件 | 本机复核 |
| --- | --- | --- |
| arm64 | `Translator_macOS_arm64_8.0.1_TEMP_SIGNED_TEST.dmg` | `.sha256` 通过；挂载后 `codesign --verify --deep --strict Translator.app` 通过；主应用和 `translator-sidecar` 均显示 `Signature=adhoc`、`TeamIdentifier=not set` |
| x86_64 | `Translator_macOS_x64_8.0.1_TEMP_SIGNED_TEST.dmg` | `.sha256` 通过；挂载后 `codesign --verify --deep --strict Translator.app` 通过；主应用和 `translator-sidecar` 均显示 `Signature=adhoc`、`TeamIdentifier=not set` |

复核所用的已下载工件位于隔离目录 `.runtime/self-tests/phase-08-temporary-signing/github-run-30108715239/`，并使用只读 `hdiutil` 挂载。该回退验证的是双架构 DMG、完整性与 ad-hoc 签名封装；它不包含 Developer ID 身份、Apple 公证、staple 或 Gatekeeper 放行，因此不能称为正式 Release，也不能代替 macOS 12 实机验收。

## GitHub 临时签名 Pre-release 发布

按确认的临时签名发布策略，`v8.0.1` 已创建为 [GitHub Pre-release](https://github.com/KlaraGraff/XL_Translator/releases/tag/v8.0.1)，标题为 `Translator v8.0.1 (temporary-test)`，不标记为 stable/latest。该 Release 包含且仅包含以下四个资产：

| 架构 | DMG | SHA-256 |
| --- | --- | --- |
| arm64 | `Translator_macOS_arm64_8.0.1_TEMP_SIGNED_TEST.dmg` | `1d3128e258281cc4d22f53471234335fa7f1135a73179e2736c1bb67f830cfcf` |
| x86_64 | `Translator_macOS_x64_8.0.1_TEMP_SIGNED_TEST.dmg` | `6d941113193d4a3e856d7bf87087608f68599508466c588dfa550a69ff2136a8` |

两份工件来自 GitHub Actions 运行 [`30112996512`](https://github.com/KlaraGraff/XL_Translator/actions/runs/30112996512) 的原生 arm64/x86_64 成功构建。下载后在隔离目录 `.runtime/self-tests/release-pre-release-publish/` 中完成 `.sha256`、只读 DMG 挂载和应用/sidecar `codesign --verify --strict` 复核；两种架构均显示 `Signature=adhoc`、`TeamIdentifier=not set`。本次 CI 的 Release job 首次因临时通道仍引用正式包校验和文件名而失败，工作流已修正为按当前通道选择校验和，并在下载的实际工件上用同一 Bash 校验块通过；在修正进入后续发布路径前，以上已验证的四个资产由 `gh release create --prerelease` 创建到该 Release。

## 外部门禁与阻断原因

本机为 macOS `26.5.2` / `arm64`，项目虚拟环境为 Python `3.13.13`。CI 已提供原生 arm64/x86_64 的受控 Python 3.11 未签名构建证据，但这不能替代正式门要求的签名发布、公证和 macOS 12.0 实机。以下正式证据尚不存在：

1. 两架构的 Developer ID 签名正式 DMG 及其签名状态；未签名 CI 工件的 `minos <= 12.0` 和目标架构扫描记录已具备，但不能替代签名包。
2. Apple Developer ID 签名、Hardened Runtime、notarize、staple、Gatekeeper 验证凭据及结果。
3. macOS 12 arm64 与 x86_64 上的安装、首次启动、sidecar、Mock 标准 Excel/Word/PDF/图片流程、Apple Events 允许/拒绝路径的实机证据。

阻断条件的当前环境证据：`security find-identity -v -p codesigning` 返回 `0 valid identities found`；`gh secret list` 没有配置 Developer ID 或 Apple notarization secrets；本机没有可用的 Parallels、UTM、VMware 或 QEMU macOS 12 虚拟化运行时。上述检查不读取或输出任何凭据值。

真实 API Key 和真实翻译验收仍按已确认例外暂缓；本阶段没有将 Mock 结果表述为真实服务验收。

## 结论与放行

本地实现和可重复 Mock 验收均通过；临时签名双架构 Pre-release 已按确认例外发布，但 Phase 8 的正式发布门仍未通过，状态必须保持 `blocked-by-gate`。依据实施方案，不创建 Phase 9 的三个子 Agent 任务、不进入 Phase 9 编码。待上述三类外部证据齐备后，重跑 Phase 8 正式发布门；只有验收更新为 `passed`，才允许建立 Phase 9 开工记录。
