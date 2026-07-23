# Phase 1 语言与语言对验收

状态：`通过；真实服务 Key 和 macOS 12 实机发布门按最终决策暂缓`

## 范围

- 59 种内置语言同时可作为源语言和目标语言，前端从 `/api/languages` 获取目录。
- Excel/Word 源语言首项为 `auto`（自动识别）；PDF 只选择目标语言。
- 自动模式对每个有候选文本的文件最多发送一次抽样预检，最多返回两个实际 ISO 源语言；TM 只使用真实语言对。
- 自定义语言只作为目标语言，创建后使用不可变内部代码，显示名和说明可编辑；存在 TM 引用时拒绝删除。
- Excel、Word、PDF 的页面选择状态独立；全局最近目标语言仅用于推荐，不覆盖当前页面选择。

## 已执行

## 四条线完成记录

- L1 核心/API：语言目录、自动预检、实际语言对和任务启动快照已完成。
- L2 Tauri/UI：三个页面独立选择状态、完整目录搜索、自定义目标语言管理已完成。
- L3 测试/兼容：预检、API、目录、自定义生命周期和隔离动态测试已完成。
- L4 主线/门禁：全量回归、质量门、UI/Rust 构建和隔离 Tauri smoke 已完成。

| 类别 | 命令/证据 | 结果 |
| --- | --- | --- |
| 语言注册表 | `core/language_registry.py` | 59 内置语言；自动源选项；英文名/别名/ISO 搜索；自定义目标代码稳定 |
| 预检契约 | `core/language_preflight.py` | 候选过滤、每文件一次请求、最多两种代码、`mixed/und/auto` 不进 TM |
| API 契约 | `GET/POST/PUT/DELETE /api/languages*` | 目录、创建/编辑/引用保护和预检响应结构已实现 |
| 动态测试 | `./.venv/bin/python3 -m unittest tests.test_language_registry tests.test_language_preflight tests.test_phase1_language_module` | PASS；12 项；日志 `.runtime/self-tests/phase-01-language/language-tests.log` |
| 全量回归 | `./.venv/bin/python3 -m unittest discover -s tests` | PASS；296 项 |
| UI 静态/构建 | `npm run check --prefix ui && npm run build --prefix ui` | PASS；仅既有动态 import 非阻断警告 |
| Rust 基线 | `cargo test --manifest-path src-tauri/Cargo.toml && cargo check --manifest-path src-tauri/Cargo.toml` | PASS |
| 质量门 | `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1` | PASS |
| 隔离 Tauri 壳 | `env TRANSLATOR_APP_DATA_DIR=.runtime/self-tests/phase-01-language/tauri-app-data ../ui/node_modules/.bin/tauri dev` | PASS；PID 91464，启动时间 2026-07-24 00:30:20，路径 `src-tauri/target/debug/translator`；sidecar PID 91489，监听 `127.0.0.1:64841`；Tauri setup 已完成随机端口 handshake 与带 token `/health`，退出后进程清理完成 |

## 外部暂缓

- 真实服务 Key 和真实翻译请求按最终决策继续暂缓，使用 Mock/契约测试。
- macOS 12 双架构实机安装、公证和 Office Apple Events 归入 Phase 8/9 发布门。
- Windows 新版不实施，继续使用旧版本。

## 阻断条件

自动识别若产生 `auto-*` TM 语言对、预检请求超过每文件一次、自定义语言进入源语言选择、页面状态互相覆盖、或出现语言代码失联，Phase 1 不得通过。本阶段已满足所有本地门禁；真实 Key、Windows 和 macOS 12 实机项不属于当前阶段阻断范围。
