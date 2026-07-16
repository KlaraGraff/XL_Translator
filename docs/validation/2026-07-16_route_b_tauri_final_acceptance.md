# Route B Tauri V8 最终验收记录

验收日期：2026-07-16

范围：`docs/refactor/2026-07-16_route_b_tauri_implementation.md` 的 Phase 2、
Phase 3 与 V8.0.0 最终验收。本文记录可在当前 macOS 开发环境中复现的证据；
需要真实服务凭据、Windows 主机或 Apple 签名身份的项目明确列为外部验收项，
不以模拟或未签名构建替代。

## 已验证的实现

| 范围 | 证据 | 结果 |
| --- | --- | --- |
| Tauri 壳与 UI | `ui/` 为 Vite + vanilla TypeScript，提供 Excel、Word、PDF、记忆库四个视图、模型配置抽屉、主题、文件/文件夹选择、任务 SSE、TM、更新、迁移和诊断对话框；`src-tauri/` 负责单实例、sidecar 启动与退出。 | PASS |
| loopback 安全 | sidecar 绑定随机 `127.0.0.1` 端口；握手给出随机 token；所有实际 API（含 `/health`）要求 `X-Translator-Token`。运行时验证无 token `/health` 返回 401，带 token 返回健康响应；带 `X-Translator-Token` 的 `tauri://localhost` CORS 预检返回 200，并只放行预检、不放行实际未认证请求。 | PASS |
| 任务生命周期 | `TranslationTaskManager` 复用 `TaskResourceRegistry`，API 测试覆盖冲突锁、停止、可重放 SSE 的 start/progress/log/done 流；发布应用退出后其 frozen sidecar 进程已确认清理。 | PASS |
| V8 数据兼容 | `tests/test_v8_upgrade_migration.py` 在隔离目录构造 schema v24 的 settings、keys 和 TM SQLite，验证迁移后语言、模型、密钥、TM 数据及 settings 备份均存在，且新增 appearance 字段使用默认值。 | PASS（模拟 V7.4 数据） |
| 无 Qt sidecar | `scripts/build_tauri_sidecar.py` 产出 PyInstaller onedir sidecar；`--smoke-test` 通过，且构建目录未发现 `PySide6` 或 `shiboken` 文件。 | PASS |
| macOS 发布物 | `scripts/build_macos_package.sh` 成功构建 `Translator_macOS_8.0.0.dmg`；`hdiutil verify` 和 SHA-256 校验通过，实际大小 53MB（目标 ≤70MB）。发布 `.app` 启动后，冻结 sidecar 通过 Rust 内部 token 健康检查。 | PASS（未签名验证构建） |
| Windows 发行链路 | `tauri.conf.json` 使用 NSIS `currentUser` 安装和 WebView2 `downloadBootstrapper`；`scripts/build_windows_package.ps1` 使用 frozen sidecar、NSIS、签名钩子和 80MB 门槛，并已通过 PowerShell 语法解析。 | PASS（静态/脚本级） |
| 回归与构建 | `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1`；隔离 `pytest tests -q` 为 `271 passed, 9 subtests passed`；`ui/` 的 `npm run check && npm run build`；`src-tauri/` 的 `cargo test && cargo check`。 | PASS |

界面检查补充：已用隔离数据目录启动实际 Tauri 发布应用，确认 Excel/Word 页面、模型配置抽屉、文件任务控件与 TM 初始化可用。macOS 的 ScreenCaptureKit 在本环境中无法提供截图流，因此未把截图作为验收证据；可访问性树和运行时 sidecar/CORS 验证替代了该截图步骤。

## 外部验收项（已获准暂缓）

| 项目 | 未执行原因 | 完成所需条件 |
| --- | --- | --- |
| 四家真实服务的连通性 | 没有授权使用真实 API key。 | 用户授权的 OpenAI、Claude、智谱、通义有效 key。 |
| Excel、Word、PDF 各一单真实翻译与 V7.4 输出对照 | 依赖真实 key 与可用于比较的 V7.4 基线。 | 真实 key、输入文件及 V7.4 对照产物。 |
| 真实 V7.4 用户数据目录升级 | 当前只具备隔离的 schema v24/keys/TM 演练数据，未提供真实用户数据目录。 | 已脱敏或获授权的 V7.4 数据目录。 |
| Windows 安装、卸载、重装与启动冒烟 | 当前环境为 macOS，不能替代 Windows 实机。 | Windows 主机或 Windows CI 产物后的实机验证。 |
| macOS 签名与公证 | 未提供 Developer ID identity 与 notary profile。构建脚本在提供二者后会签名 sidecar、应用和 DMG，并提交公证。 | Apple Developer 签名身份与公证凭据。 |

## 结论

Route B 的代码、Tauri UI、Python sidecar、迁移、Qt 下线及 macOS 未签名分发构建均已完成并验证。正式发布前仍必须完成上表列出的外部验收项；这些项目没有被本记录或自动化测试视为已通过。
