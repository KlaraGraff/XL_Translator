# Phase 7 验收记录：任务调度、并发与统一结果

状态：`passed`

执行日期：`2026-07-24`

实现提交：`bd8521f feat: add multi-task scheduler and task center`

## 范围与四线证据

- 前置阶段：[Phase 6 PDF/图片验收](2026-07-24_phase-06-pdf_acceptance.md)，状态 `passed`。
- L1 核心与契约：实现 Excel、Word、PDF/图片、TM 清洗四类任务；同类互斥、跨类型共享连接预检与一次性令牌、原子复检、冻结吞吐合计、公平排队和组级 429 降速。
- L2 Tauri/UI：移除全局单任务状态，改为按任务 ID 管理；增加统一任务中心、风险确认、SSE 补拉、任务控制、结果入口和本地文件操作。
- L3 测试与兼容：建立隔离黑箱合同，覆盖资源、令牌、SSE、重启、TM 清洗、结果操作和隐私边界。
- L4 主线集成：复核 X-05、X-06、X-08、X-10、X-11；补齐 `app.log` 脱敏和 PDF 输出快照兼容；完成质量门、回归、UI/Rust 与隔离源码壳验证。

## 已验收行为

- 每种任务类型最多一个活动任务；不同类型可并行。共享实际 API 连接时，第二任务必须经过预检风险提示、一次性确认令牌和服务端原子复检；令牌不可重用，资源版本变化会要求重新预检。
- PDF 生成和审核角色若解析到同一连接，冻结并发会合并进入同一资源组；共享组采用任务 FIFO 等待，429 仅临时降低该活跃组容量，不写回长期吞吐配置。
- TM 清洗成为第四种统一后台任务，保持“只生成建议、用户显式审核后写入”；旧 `/api/tm/clean` 已变为同一调度模型的兼容入口，不能绕过共享 API 风险确认。
- 任务状态按 ID 保存。SSE 事件递增并支持 `Last-Event-ID` 补拉；页面切换、重新聚焦和短断流不丢失监控。完整 sidecar 重启将旧活动任务标记为 `interrupted`，不伪造可继续状态。
- 任务中心和持久化历史不保存原文、译文、完整 Prompt、Key、原始模型响应或绝对源路径；本地 `app.log` 同样仅记录结构化脱敏事件。用户定位所需的输出/报告/清单路径仅以受控本地操作描述提供给 Tauri。
- Excel、Word、PDF/图片和 TM 清洗均显示在统一任务中心；Excel/Word 支持安全停止，PDF 保持暂停提交、继续、结束暂停的独立语义；清洗完成后进入既有建议审核流程。

## 验证记录

| 命令/检查 | 结果 | 覆盖 |
| --- | --- | --- |
| `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1` | 通过（All checks passed） | 当前工作树静态检查 |
| 隔离运行器 `Run-IsolatedVenvPython.ps1 -TaskSlug phase-07-scheduler` | 通过，14 tests | 同类互斥、跨类共享、令牌/竞态、PDF 双角色、TM 清洗、429/FIFO、SSE、重启、结果操作与任务/API/持久日志脱敏；产物见 `.runtime/self-tests/phase-07-scheduler/` |
| 隔离运行器 `Run-IsolatedVenvPython.ps1 -TaskSlug phase-07-regressions` | 通过，31 tests | API、任务选择、Word 与 PDF/图片受影响回归；产物见 `.runtime/self-tests/phase-07-regressions/` |
| `cd ui && npm run check && npm run build` | 通过 | TypeScript 类型检查、任务中心生产构建；仅有既有 Vite 动态导入提示 |
| `cd src-tauri && cargo test && cargo check` | 通过（2 Rust tests） | Tauri 启动握手与本地文件操作壳层编译 |
| 隔离 `TRANSLATOR_APP_DATA_DIR` 的当前源码 `tauri dev` | 通过 | `target/debug/translator` PID 76144、sidecar PID 76186；均为仓库源码路径，sidecar 监听 `127.0.0.1:58117`；未带 token 的 `/health` 返回 401，内部带 token 健康门通过；窗口级截图确认 UI 非空白 |

## 未执行项与范围

- 真实 API Key、真实上游并发限制及真实模型吞吐按既定例外暂缓；本阶段使用隔离 Mock/runner。
- macOS GUI 的 Finder 实际打开操作、macOS 12 双架构实机安装、签名、公证与发布资产验证属于 Phase 8/9 发布门。
- Windows 新版、Windows 冒烟和 Windows 发布不在新版范围内；Windows 用户继续使用旧版。
- 本阶段开始前，L3 曾误以非隔离方式运行两项既有 API/TM 测试，触发默认用户目录中的旧 TM 数据库迁移/备份。未删除、修复或回滚任何用户数据；此项不计入验收证据，后续全部动态验证均通过隔离运行器完成。

阻断项：`none`。

Phase 7 满足放行条件，允许建立 Phase 8 开工记录；在 Phase 8 开工记录建立前，不开始 Phase 8 代码变更。
