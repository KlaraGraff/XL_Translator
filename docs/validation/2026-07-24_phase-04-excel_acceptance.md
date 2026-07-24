# Phase 4 验收记录：Excel 翻译工作流

状态：`passed`

执行窗口：`2026-07-24 03:14–03:55 +0100`

上一阶段：[2026-07-24_phase-03-models_acceptance.md](2026-07-24_phase-03-models_acceptance.md)（`passed`）
决策范围：`E4A-01–08`、`E4B-01–11`、`E4C-01–10`、`E4D-01–08`，及 `X-01`、`X-02`、`X-10`、`X-13` 的 Excel 侧接口。

## 四条线交付记录

| 执行线 | 任务 | 完成内容 | 可复现证据 | 未覆盖项 |
| --- | --- | --- | --- | --- |
| L1 核心与契约 | `phase-04-core` | 递归扫描、跳过原因/概况/风险、独立 Excel 输出与复核设置、每文件一次有界预检、实际语言对 TM、`.xls` 高保真/兼容边界、文件终态/KPI/复核/语言契约和快照。 | 未提交补丁摘要：`api/app.py` `e7ecacc2…`；`api/task_manager.py` `f05a81ad…`；`core/task_runner.py` `15f881ce…`；`core/engine_dispatcher.py` `1e83aa51…`；`core/file_scanner.py` `af6cbddf…`；`core/bilingual_writer.py` `d4c48f9f…`；`core/xls_converter.py` `91161c26…`；`settings.py` `019e4d068…`。 | 真实服务 Key；真实 Office `.xls`；Windows 新版。 |
| L2 Tauri 与 UI | `phase-04-ui` | Excel 清单、递归概况、跳过项、`.xls` 风险、独立输出目录检查、覆盖保护、公式/行高/复核/已有底色/补译控件、显式兼容确认、运行进度/停止/结果详情；新增无副作用输出目录检查 Rust 命令。 | 未提交补丁摘要：`ui/src/main.ts` `92a534e0…`；`ui/src/tokens.css` `ebb678db…`；`src-tauri/src/main.rs` `bd69f894…`。 | macOS 当前锁屏，未取得手工 DOM/点击断言；隔离 Tauri 壳已通过源码启动和认证 health 门。 |
| L3 测试与兼容 | `phase-04-test` | 公式、样式、合并单元格、已有底色、扫描跳过/输出排除、按文件预检、实际 TM 语言对、逐条 `source_lang` 入库门、`.xls` 授权/禁止静默降级/明确回退、停止和结果定位。 | [tests/test_phase4_excel_contracts.py](../../tests/test_phase4_excel_contracts.py) `3369f43e…`；`.runtime/self-tests/phase-04-excel/artifacts/phase4-excel-regression.txt`；最终合同 10/10，合并回归 32/32。 | 真实服务 Key、真实 Office/macOS 12 实机、Windows。 |
| L4 集成与门禁 | 本体 | 发现并修复既有 API/SSE 资源锁测试与新启动前配置校验的兼容阻断；对最小适配器缺少 `path` 元数据增加安全格式判定；完成最终全套质量门和交叉回归。 | 最终动态回归 38 tests，`OK`；本记录的“验证证据”。 | 真实 Key、macOS 12 双架构发布门仍留在 Phase 8/9。 |

以上摘要均为 SHA-256；提交前可对当前文件使用 `shasum -a 256 <file>` 重算。提交 SHA 将在 Git 历史中提供最终追溯证据。

## 已验证行为

- Excel 支持单文件和目录递归扫描；Office 临时文件、旧翻译输出目录和损坏/不可读文件不会进入可选清单，跳过项目、相对路径、格式、工作表数和 `.xls` 风险可解释。
- Excel 源路径、目标/输出、公式、版式、复核和补译设置独立保存；源文件不被写回，输出创建唯一子目录并保护历史结果。
- 自动源语言模式对每个有候选文本的文件仅发起一次有界预检，不上传完整工作簿，最多返回两个实际语言；TM 查询使用文件实际语言对，正式翻译逐条携带 `source_lang`，不产生 `auto-*`。
- `mixed`、`und`、预检范围外或冲突的逐条语言结果仍可输出，但不自动写入普通 TM；手动模式不预检并以用户选择为权威语言。
- `.xls` 优先走本机 Excel 高保真转换；权限/自动化失败不会静默兼容降级。只有用户明确确认兼容模式后才调用回退，风险和 macOS 12/13+ 授权路径可见。
- 文件终态区分成功、失败、未开始和停止；任务结果包含源相对路径、格式、输出/转换方式、KPI、语言统计、TM/模型计数、复核定位、错误与脱敏日志引用。停止不损坏源文件，其他文件可继续。
- Phase 4 不实现跨类型全局锁或总预算；Excel 任务继续消费 Phase 1–3 的语言、TM、模型、Key、吞吐和输出快照，跨类型并发由 Phase 7 接管。

## 验证证据

所有 Python 动态测试均在导入业务模块前隔离 `HOME`、`USERPROFILE`、`TEMP`、`TMP` 与 `TRANSLATOR_APP_DATA_DIR`，并使用仓库 `./.venv/bin/python3`。

| 检查 | 实际命令 | 结果 |
| --- | --- | --- |
| 质量门与差异检查 | `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1 && git diff --check` | 通过：`All checks passed!` |
| Excel/API 动态回归 | `./.venv/bin/python3 -m unittest -v tests.test_phase4_excel_contracts tests.test_excel_api_scheduling tests.test_excel_coverage tests.test_excel_automation tests.test_language_preflight tests.test_mixed_language tests.test_api_app` | 38 tests，`OK` |
| TypeScript 检查/生产构建 | `cd ui && npm run check && npm run build` | 通过 |
| Rust 测试/检查 | `cd src-tauri && cargo test && cargo check` | 2 tests，`OK`；检查通过 |
| 隔离 Tauri 冒烟 | `cd src-tauri && ../ui/node_modules/.bin/tauri dev`（设置隔离 `TRANSLATOR_APP_DATA_DIR`） | 通过；旧进程已关闭并由当前源码重启。 |

动态产物目录：`.runtime/self-tests/phase-04-excel/`，包括 `artifacts/phase4-excel-regression.txt`、`artifacts/phase4-contracts-post-review.txt` 和 `tauri-isolated/tauri-smoke.txt`。

隔离 Tauri 审计：当前源码壳 PID `31389`，启动时间 `2026-07-24 03:47:26 +0100`，路径 `src-tauri/target/debug/translator`；sidecar PID `31396`，监听 `127.0.0.1:51309`。Rust 启动器在窗口创建前使用一次性握手 token 请求 `/health`，未取得 `200` 会终止启动；本次壳持续运行，证明认证 health 门通过。应用数据目录为 `.runtime/self-tests/phase-04-excel/tauri-isolated`，未触碰真实用户目录。

## 未执行项与风险范围

- 未调用真实 API Key；本阶段只证明 Mock/隔离请求和协议边界。按最终决策属于外部暂缓，不阻断本地阶段通过。
- 未在 Windows 执行新版本测试或打包；Windows 继续使用旧版本。
- 未在 macOS 12 实机执行 Office 自动化、双架构安装/签名/公证或完整应用流程；这些属于 Phase 8/9 发布门。
- 未取得手工 UI DOM/截图断言，原因是当前 macOS 锁屏；生产构建、Rust 壳、隔离数据、源码路径和 sidecar health 已验证。该缺口仅影响人工控件操作证据，不改变本阶段 Mock/API/构建门结论，后续 UI 验收仍需补做。

## 结论与放行

阻断项：`none`。Phase 4 已满足实施方案 §3.4 本地交付门，状态为 `passed`。允许创建下一阶段开工单：`phase-05-core`、`phase-05-ui`、`phase-05-test`；不得提前进入 Phase 6。
