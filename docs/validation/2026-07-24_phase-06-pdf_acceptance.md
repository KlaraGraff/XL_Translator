# Phase 6 验收记录：PDF 与图片翻译

状态：`passed`

执行日期：`2026-07-24`

实现提交：`d5a9d61 feat: complete PDF and image workflow phase`

## 范围与四线证据

- 前置阶段：[Phase 5 Word 验收](2026-07-24_phase-05-word_acceptance.md)，状态 `passed`。
- L1 核心与契约：PDF/图片递归扫描、混合清单、坏输入可见性、独立 PDF 输出、页面素材与版式保护、失败占位、审核/重试、暂停/继续/结束暂停、manifest/report 与 TM 边界。
- L2 Tauri/UI：独立图片范围、PDF 输出目录预检、扫描概况和跳过项、审核与页面恢复 SSE、暂停/继续/结束暂停、PDF 文件和页面结果展示。
- L3 测试与兼容：隔离 Mock PDF/图片/审核客户端、动态测试脚本和阶段证据；未使用真实服务 Key。
- L4 主线集成：契约汇合、隐私边界复核、质量门、UI/Rust 构建和当前源码 Tauri 壳动态启动核验。

## 已验收行为

- PDF 始终可扫描；开启“PDF 与图片”后，PNG、JPG/JPEG、WebP、BMP、TIF/TIFF 可作为独立图片输入，并可和 PDF 在同一任务清单中处理。
- 损坏 PDF、不可读图片、动画或不支持图片格式会进入可见的跳过项目；系统临时文件、应用生成的输出目录和既有 manifest/report 不会递归回收为输入。
- PDF/图片使用独立 `pdf_output` 设置和任务唯一输出目录；不复用 Excel/Word 输出设置，不覆盖源文件，PDF 不读写 TM。
- PDF 页面按源页一页对一页保存源页、候选译图、译后页图和审核证据；成功 PDF 生成高清版本，压缩版本失败只产生警告；独立图片输出为单张译图。
- 比例异常、生成失败、审核多轮阻断和不可恢复错误均保留可定位素材与解释性状态；PDF 失败页写占位，图片失败不伪造成功译图。
- 审核默认关闭；启用审核时检查视觉审核模型，已知配置失败需要用户明确确认继续；审核 JSON 仅保存结构化结论，不保存 Key、Prompt 或完整模型原始响应。
- 暂停只停止提交新页面并等待已提交页面安全完成；同一 sidecar 内可继续。结束暂停即使最后已提交页面已完成，也始终以 `stopped` 终态保留 manifest/report 和素材，终止原因记录为 `end_paused`。
- 任务快照冻结目标语言、输出、压缩/素材、审核和模型配置；SSE 提供 `pdf_page_recovery`、`pdf_review`、暂停/继续/结束暂停与终态事件。

## 验证记录

| 命令/检查 | 结果 | 覆盖 |
| --- | --- | --- |
| `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1` | 通过（All checks passed） | Python 静态检查 |
| 隔离运行器 `Run-IsolatedVenvPython.ps1 -TaskSlug phase-06-pdf` | 通过，49 tests | Phase 6 合同、PDF 回归、审核解析；产物见 `.runtime/self-tests/phase-06-pdf/` |
| `./.venv/bin/python3 -m unittest tests.test_phase6_pdf_contracts` | 通过，10 tests | 扫描、独立快照、审核前置、暂停/继续/结束暂停、SSE、隐私 |
| `./.venv/bin/python3 -m unittest tests.test_pdf_image_translation` | 通过，35 tests | 页面渲染、译图、压缩/高清、重试、占位、停止与结果契约 |
| `cd ui && npm run check && npm run build` | 通过 | PDF/图片 UI 类型检查和生产构建；仅有既有 Vite 动态导入提示 |
| `cd src-tauri && cargo test && cargo check` | 通过（2 Rust tests） | Tauri 启动握手与壳层编译 |
| 隔离 `TRANSLATOR_APP_DATA_DIR` 的 `tauri dev` | 通过 | 当前源码 `target/debug/translator` PID 64622、sidecar `api.launcher` PID 64657；sidecar 监听 `127.0.0.1:56825`，未带 token 的 `/health` 返回 401，Tauri 内部带 token 启动健康门通过；收尾已关闭进程 |

## 未执行项与范围

- 真实 API Key、真实图像生成/审核服务和真实翻译验收按已确认例外暂缓；本阶段使用隔离 Mock。
- Windows 新版、Windows 冒烟和 Windows 发布不在新版范围内；Windows 继续使用旧版。
- macOS 12 `arm64`/`x86_64` 实机安装、双架构打包、签名、公证和 Apple Events 权限属于 Phase 8/9 发布门。
- 当前 macOS 会话未执行可交互 WebView DOM/截图断言；TypeScript 构建、源码壳启动路径、PID、sidecar 健康门及 API/SSE 动态测试已覆盖。该项不构成 Phase 6 阻断，发布候选仍需按 Phase 8/9 补齐。

阻断项：`none`。

Phase 6 满足放行条件，允许建立 Phase 7 开工记录；在 Phase 7 开工记录建立前，不开始 Phase 7 代码变更。
