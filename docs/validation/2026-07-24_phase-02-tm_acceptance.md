# Phase 2 验收记录：记忆库与知识库

状态：`passed`

## 范围

- 本阶段的“知识库”仅指按真实语言对隔离的 TM；不引入独立 RAG，PDF/图片翻译不读写 TM。
- 定向 TM 查询、命中解释和真实语言对隔离。
- 自动入库长度、质量、混合语言和冲突门。
- 五级可信状态、固定保护、人工反向同步和反向冲突保护。
- 深度清洗建议、确认写入、取消/失败原子性和版本保护。
- TM 工作台的独立语言对、分页、批量操作、JSON/CSV 交换。
- `tm-full-v1` 全库备份、当前格式恢复、自定义目标语言定义和代码映射。
- 自动冲突候选持久化、列表和人工裁决。

## 四条线完成记录

| 执行线 | 任务名 | 证据提交/产物 | 结果 | 未覆盖项 |
| --- | --- | --- | --- | --- |
| L1 核心与契约 | `phase-02-core` | `58f3451`、`c006853`、`26114fe` | 完成 | 真实服务 Key |
| L2 Tauri 与 UI | `phase-02-ui` | 当前工作树 UI 改动；`npm run check/build` | 完成 | Windows 新版 |
| L3 测试与兼容 | `phase-02-test` | `58f3451`、`26114fe`；`.runtime/self-tests/phase-02-tm/` | 完成 | macOS 12 实机 |
| L4 主线集成与门禁 | 本体 | 本记录 + 全套质量门/构建/冒烟证据 | `passed` | 外部暂缓项见下表 |

Phase 2 的三条子 Agent 任务在本验收记录创建前已完成；其启动/完成时间未单独保留，Phase 3 起改用严格的开工登记模板。

## 验证证据

| 检查 | 结果 | 产物/命令 |
|---|---|---|
| Phase 2 动态测试 | 通过，32 tests | `.runtime/self-tests/phase-02-tm/full_phase2_tm.log` |
| UI TypeScript 检查 | 通过 | `npm run check --prefix ui` |
| UI 生产构建 | 通过 | `npm run build --prefix ui` |
| Tauri Rust 测试 | 通过，2 tests | `cargo test --manifest-path src-tauri/Cargo.toml` |
| Tauri Rust 检查 | 通过 | `cargo check --manifest-path src-tauri/Cargo.toml` |
| Python 静态检查 | 通过 | `ruff check`、`py_compile` |
| 项目质量门 | 通过 | `.runtime/self-tests/phase-02-tm/quality_gate.log` |
| 隔离 Tauri 冒烟 | 通过 | `.runtime/self-tests/phase-02-tm/tauri-smoke.log`、`sidecar-health.log` |

隔离冒烟审计字段：当前源码启动路径为 `src-tauri/target/debug/translator`；新进程 PID、启动时间、sidecar PID、监听地址、握手 token 和 `/health` 响应均记录在上述日志中；退出后旧 `Translator`、`tauri dev`、`api.launcher` 和 sidecar 进程已清理。

## 结论

Phase 2 已满足实施文档的进入下一阶段门禁。完整备份只支持当前 `tm-full-v1` 格式；旧版本数据兼容不在范围内。真实 API Key 连通性和 Windows 新版本验收按最终决策暂缓，Windows 继续使用旧版本。

阻断项：`none`。未执行项：真实 Key、Windows 新版、macOS 12 实机；这些是最终决策允许的外部暂缓项，不阻断本地 Phase 2。下一阶段开工单：`phase-03-core`、`phase-03-ui`、`phase-03-test`，已满足创建条件。
