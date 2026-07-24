# Phase 0 基础契约与兼容性验收

状态：`passed；macOS 12 实机发布门保留至 Phase 8/9`

本记录对应实施方案的 Phase 0，作为后续阶段的入口证据。样例和测试不得访问真实用户目录、Key 或 TM 数据库。

## 已执行

## 四条线交付记录（历史补录）

Phase 0 在当前四线登记模板建立前完成，以下按提交和测试证据补录；当时没有单独保存每个子 Agent 的启动/完成时间，后续阶段不得沿用这一缺项。

| 执行线 | 任务名 | 证据提交 | 结果 | 未覆盖项 |
| --- | --- | --- | --- | --- |
| L1 核心与契约 | `phase-00-core` | `a19b068`、`b18eacd` | 完成 | 真实 Key、真实翻译 |
| L2 Tauri 与 UI | `phase-00-ui` | `1b51b3c`、`9aff829` | 完成 | macOS 12 实机签名/公证 |
| L3 测试与兼容 | `phase-00-test` | `a19b068`、`b18eacd` | 完成 | Windows 新版 |
| L4 主线集成与门禁 | 本体 | `9aff829` + 本记录 | `passed` | 外部暂缓项见下表 |

| 类别 | 命令/证据 | 结果 |
| --- | --- | --- |
| 静态质量门 | `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1` | PASS |
| 基础契约动态测试 | `./.venv/bin/python3 -m unittest tests.test_phase0_foundation tests.test_release_verification` | PASS；12 tests |
| 样例夹具 | `tests/phase0_foundation.py` | Excel、Word、PDF、图片、TM JSON、自定义目标语言 |
| Mock API | `MockTranslationProvider` + `create_mock_translation_api` | 不使用真实 Key，返回实际源/目标语言对；HTTP health/translate 契约 PASS |
| 应用目录隔离 | `TRANSLATOR_APP_DATA_DIR` | 旧目录哨兵保持不变；隔离目录可重复生成 |
| 版本/架构门 | `scripts/verify_macos_minimum_version.py` | 无 Mach-O 或版本/架构不满足时失败；arm64/x86_64 断言 PASS |
| WebView 基线 | `ui/vite.config.ts` + `ui/src/tokens.css` | Vite `safari15.1`；Safari 15 静态 CSS 回退 PASS |
| UI/Rust 编译 | `npm run check --prefix ui`; `npm run build --prefix ui`; `cargo test/check --manifest-path src-tauri/Cargo.toml` | PASS；仅有既有动态 import 非阻断警告 |
| Tauri 隔离开发壳 | `env TRANSLATOR_APP_DATA_DIR=.runtime/self-tests/phase-00-foundation/tauri-app-data ../ui/node_modules/.bin/tauri dev` | PASS；PID 85362，启动时间 2026-07-24 00:07:14，路径 `src-tauri/target/debug/translator`；sidecar PID 85379，回环端口 64349；Tauri 启动流程已完成 handshake 与 `/health`，退出后进程均已清理 |
| CI 配置 | Ruby YAML 解析 + release 聚合逻辑审查 | PASS；仅 macOS arm64/x86_64 发布，聚合 job 在两个构建成功后上传 |

## 外部暂缓项

- 真实服务 Key 连通性和真实翻译：按最终决策继续暂缓。
- macOS 12 arm64/x86_64 实机安装、Gatekeeper、公证和 Office Apple Events：留到 Phase 8/9 的发布门。
- Windows 新版构建和验收：不属于本次发布范围，继续使用旧版本。

## 通过条件

进入 Phase 1 前已确认：隔离夹具可重复生成、Mock API 契约包含实际语言代码、旧目录哨兵未被读取或写入、macOS 12 声明与 WebView 目标已被测试覆盖，并完成质量门。真实服务 Key 和 macOS 12 实机安装仍按最终决策暂缓到发布阶段，不阻断本地 Phase 0。

阻断项：`none`。下一阶段开工单：`phase-01-core`、`phase-01-ui`、`phase-01-test`；仅在本记录保持 `passed` 时创建。
