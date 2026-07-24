# Phase 3 验收记录：模型、专业领域与提示词

状态：`passed`

执行窗口：`2026-07-24 02:26:43–03:13:35 +0100`

上一阶段：[2026-07-24_phase-02-tm_acceptance.md](2026-07-24_phase-02-tm_acceptance.md)（`passed`）
决策范围：`M3A-01–09`、`M3B-01–10`、`M3C-01–11`，及 `X-05`、`X-06`、`X-10`、`X-11` 的本阶段接口。

## 四条线交付记录

| 执行线 | 任务 | 完成内容 | 可复现证据 | 未覆盖项 |
| --- | --- | --- | --- | --- |
| L1 核心与契约 | `phase-03-core` | 四角色能力与一层复用图、有效连接签名、测试状态失效、会话模型目录、角色吞吐档案、领域/Prompt 边界、任务快照与 v3 稀疏配置交换。 | 未提交补丁摘要：`api/app.py` `fac8e129…`；`core/model_roles.py` `784c8c7e…`；`core/model_config.py` `434a7a1f…`；`core/model_catalog.py` `08bab56a…`；`core/model_throughput.py` `07ef686a…`；`core/model_api_identity.py` `6498ba7c…`；`core/connectivity_check.py` `be006c3c…`；`core/engine_dispatcher.py` `a612fca0…`；`core/task_runner.py` `574f071d…`；`settings.py` `f19765a9…`。 | 真实服务 Key；Windows 新版。 |
| L2 Tauri 与 UI | `phase-03-ui` | 四角色配置、主动测试状态、目录刷新、吞吐恢复、Excel/Word 独立领域与 Prompt 恢复、v3 导入预览/敏感导出确认、共享 API 风险说明。 | 未提交补丁摘要：`ui/src/main.ts` `8f57591a…`；`ui/src/tokens.css` `e37ed9b6…`。`npm run check`、`npm run build`、`cargo test`、`cargo check` 均通过。 | 当前 macOS 锁屏，未取得可操作窗口的 DOM/点击断言；隔离开发壳启动与 sidecar 健康握手已通过。 |
| L3 测试与兼容 | `phase-03-test` | Mock provider、能力限制、角色协议、连接/Key 变化失效、目录缓存、Prompt 边界、吞吐、快照和 v3 交换回归。 | [2026-07-24_phase-03-test-acceptance.md](2026-07-24_phase-03-test-acceptance.md)；`tests/test_phase3_acceptance.py` `bd673990…`；本阶段回归相关测试文件摘要为 `3ce9b2d3…`、`61f7bc51…`、`6d966481…`、`46ebe5b3…`。 | 真实服务 Key；Windows 新版。 |
| L4 集成与门禁 | 本体 | 复核共享接口及边界，复跑质量门、67 项模型回归、UI/Rust 构建，并从当前源码以隔离数据重启 Tauri 壳。 | 本记录的“验证证据”。 | macOS 锁屏导致无 UI 手动 DOM 证据；不影响本阶段的静态构建、Mock 动态测试和隔离壳验证。 |

以上摘要均为 SHA-256；完整文件清单和摘要在本阶段工作树中可用 `shasum -a 256 <file>` 重算。提交本阶段时，提交 SHA 将补入 Git 历史；该补丁摘要满足实施方案 §3.3.1 的未提交变更追溯要求。

## 已验证行为

- 翻译、清洗、PDF 图片生成和 PDF 审核四个角色拥有独立能力限制；只允许冻结的一层连接复用图，拒绝链式/循环跟随和云端专用角色跟随本地翻译模型。
- 连接测试结果绑定能力、服务商、Base URL、模型和 API Key 指纹；任一有效连接变化后，相应角色重新变为未测试。模型目录仅按有效连接作会话缓存，不能代替主动能力测试。
- Excel 和 Word 的领域/自定义 Prompt 独立保存、可恢复；固定 JSON、格式、占位符、目标语言、逐条 `source_lang`、清洗 JSON 及 PDF 内置协议不能被页面 Prompt 覆盖。
- 吞吐档案按角色和有效连接隔离；文本角色有批次与并发，图片/审核角色仅并发；启动任务冻结模型、吞吐、Key 作用域、目标语言和领域快照。
- 仅接受 `translator_model_config v3`；导入先预览、只合并明确字段、先验证完整角色图，成功导入后四角色都回到未测试。默认导出不含 Key，敏感导出要求明确参数和二次确认。

## 验证证据

所有 Python 动态测试均在导入业务模块前隔离了 `HOME`、`USERPROFILE`、`TEMP`、`TMP` 与 `TRANSLATOR_APP_DATA_DIR`；运行环境统一为仓库 `./.venv/bin/python3`。

| 检查 | 实际命令 | 结果 |
| --- | --- | --- |
| 质量门 | `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1` | 通过：`All checks passed!` |
| Phase 3 Mock/API 回归 | `./.venv/bin/python3 -m unittest -v tests.test_phase3_acceptance tests.test_phase3_model_contracts tests.test_model_roles tests.test_connectivity_check tests.test_model_catalog tests.test_model_api_identity tests.test_api_app` | 67 tests，`OK` |
| TypeScript 检查/生产构建 | `cd ui && npm run check && npm run build` | 通过 |
| Rust 测试/检查 | `cd src-tauri && cargo test && cargo check` | 2 tests，`OK`；检查通过 |
| 隔离 Tauri 冒烟 | `cd src-tauri && TRANSLATOR_APP_DATA_DIR=../.runtime/self-tests/phase-03-l4/tauri/app-data ../ui/node_modules/.bin/tauri dev` | 通过；先关闭旧 `Translator`、`tauri dev`、`api.launcher`，再由当前源码启动新壳。 |

动态测试产物位于：

- `.runtime/self-tests/phase-03-model-acceptance/`
- `.runtime/self-tests/phase-03-model-regression/`
- `.runtime/self-tests/phase-03-l4/tauri/`

隔离 Tauri 冒烟审计：当前源码壳 PID `24752`，启动时间 `2026-07-24 03:10:24 +0100`，启动路径 `src-tauri/target/debug/translator`；sidecar PID `24777`，监听 `127.0.0.1:50696`。Rust 启动器会在窗口创建前使用一次性握手 token 请求 `/health`，未取得 `200` 会杀死 sidecar 并使启动失败；本次进程持续运行，因此该健康握手已通过。应用数据目录解析后为 `.runtime/self-tests/phase-03-l4/tauri/app-data`，不涉及真实用户数据。

## 未执行项与风险范围

- 未调用真实 API Key；本阶段仅验证 Mock 请求构造、协议、快照和敏感信息边界。此项按最终决策属于外部暂缓，不阻断本地验收。
- 未执行 Windows 新版本测试或打包；Windows 继续使用旧版本，按冻结范围暂缓。
- 未在 macOS 12 实机上构建/安装，也未做双架构 DMG、签名或公证；这些属于 Phase 8/9 发布门。
- 因当前 macOS 处于锁屏，无法读取或点击 Translator 窗口，故没有手工 UI DOM/截图断言。生产构建、Rust 壳、隔离数据和 sidecar 健康握手均已通过；该缺口的风险仅限于本阶段新增控件的人工可操作性，后续 Phase 4 启动前如机器解锁将补做，不得以此替代后续阶段的 UI 验收。

## 结论与放行

阻断项：`none`。Phase 3 已满足实施方案 §3.4 的本地交付门，状态为 `passed`。允许创建下一阶段开工单：`phase-04-core`、`phase-04-ui`、`phase-04-test`；不得提前进入 Phase 5。
