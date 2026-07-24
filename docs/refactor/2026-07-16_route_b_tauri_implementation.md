# 方案 B 实施交接文档：Tauri 2 壳 + Python 引擎 Sidecar

- **日期**：2026-07-16
- **读者**：原始 Route B 重构执行记录的维护者。
- **性质**：本文件保留 2026-07-16 的路线背景。其后续功能、数据基线、平台与发布要求已由 [升级后功能迁移决策](../upgrade-functional-migration-decisions.md) 和 [分阶段实施计划](../upgrade-functional-migration-implementation-plan.md) 覆盖；两份最终文档优先级更高。

> 2026-07-24 修订：本文件中关于 Windows 新版、旧数据迁移、旧 Qt 验收和真实 Key 发布门的表述均已失效，不得据此恢复代码、API、UI 或发布流程。新版仅发布 macOS 12+ 的 arm64/x64 原生 DMG，且不读取、迁移、修复或删除本次新基线之前的数据。

---

## 0. 必读材料（按顺序）

1. 本文档（怎么做）
2. `docs/refactor/2026-07-16_stack_refactor_assessment.md`（为什么选方案 B；Phase 0 三项瘦身的依据与实测数据）
3. `docs/redesign/2026-07-16_ui_redesign_progress.md`（UI 设计的 9 条已拍板决策，勿推翻）
4. `docs/mockups/2026-07-16_redesign_prototype.html`（**设计唯一真相源**。浏览器直接打开，可交互：左侧导航切 5 个视图、顶栏开配置面板、右上角切深浅主题；页面底部附完整设计规范——色彩 token / 字阶 / 27 个 SVG 图标 / 组件样式）

环境速查：

```bash
# 仓库根目录已有 .venv（PySide6 6.11.1 等已装，gitignore 内）
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ -q     # 全量测试
TRANSLATOR_APP_DATA_DIR=/tmp/xlt-test .venv/bin/python scripts/launch_native.py  # 隔离试跑现有 Qt 版
```

## 1. 目标与不变量（硬性要求）

| # | 不变量 |
|---|---|
| 1 | 用户可感知功能与 V7.4 **等价**（逐控件对照，见 §5 Phase 2 验收） |
| 2 | **API 连接行为完全不变**：`engines/`、密钥存取、cloud_base_url、本地 Ollama/LM Studio 一行不动，全部留在 Python 引擎内 |
| 3 | **数据兼容**：沿用同一应用数据目录（`core/app_paths.py`）与 settings schema（只增不破坏）；老用户升级后 TM / 密钥 / 设置无缝 |
| 4 | 安装包目标 **≤70MB**；超过 80MB 触发 §7 |
| 5 | UI 按原型落地；原型未覆盖处按 §4 规则延伸 |
| 6 | 平台：Windows + macOS（Linux 不在本次范围） |
| 7 | 交付版本号 **V8.0.0**（大版本：壳层更换） |

## 2. 架构定案（勿重新发明）

```
┌─ Tauri 2 壳（Rust 层保持最小）─────────────┐
│  窗口 / 单实例(tauri-plugin-single-instance) │
│  sidecar 生命周期：spawn → 健康检查 → 退出必杀 │
│  WebView（系统内置：WebView2 / WKWebView）    │
│    └─ 前端：Vite + vanilla TypeScript        │
│       原型 HTML/CSS/JS 直接演进，禁止引入      │
│       React/Vue 等框架（触发 §7 才可讨论）     │
└──────────────┬───────────────────────────┘
               │ loopback HTTP（127.0.0.1:随机端口）
               │ 请求头带启动时生成的 token；SSE 推日志/进度
┌──────────────┴───────────────────────────┐
│  Python 引擎 sidecar（PyInstaller onedir，  │
│  无 PySide6）：FastAPI + uvicorn            │
│  core/ + engines/ + settings.py 原样复用    │
└──────────────────────────────────────────┘
```

定案理由速记：前端选 vanilla TS 是因为原型本身就是 vanilla（1:1 迁移、零框架税、本应用交互复杂度低）；IPC 选 loopback HTTP + SSE 是因为 Python 侧实现琐碎、curl 可调试、日志/进度天然流式；端口随机 + token 防本机其他进程访问。

## 3. 现有代码地图

**原样保留**（sidecar 内直接复用）：`core/`、`engines/`、`settings.py`、`config.py`、`app_meta.py`，以及 tests/ 中不依赖 `native_app` 的全部测试。

**被替换**（Phase 3 删除）：`native_app/` 全部、`scripts/launch_native.py`、PySide6 依赖。

**关键事实**（省你的勘查时间）：

- core 已可无头运行：`tests/test_headless_translate.py` 是证据。Qt 层只是壳。
- `core/task_runner.py` 的消息类型（`ProgressMsg / LogMsg / StatusMsg / DoneMsg / ErrorMsg / StoppedMsg`）就是 SSE 事件契约的底子，直接序列化即可。
- `native_app/workers.py` 的 `TaskResourceRegistry` 实现"跨页面同时只跑一个翻译任务"的锁语义——**必须在 API 层重建**（页面间互斥 + 锁状态可查询，Qt 版的 `external_task_lock` 提示行为要等价）。
- Qt 版的富文本 tooltip 体系（各页 `_set_tooltip` 的 title/summary/items 三段结构文案）是产品的一部分，web 端要重建组件并**迁移全部文案**。
- 模型配置导入/导出（`native_app/main_window.py` 中 `_build_model_config_export_payload` / `_extract_imported_model_config` 一族，含 scoped api keys 与吞吐 profile）是纯数据逻辑，**迁移为 core 或 api 层的纯函数**，JSON 格式保持兼容（用户手里有导出的配置文件）。
- **parked 分支 `redesign/design-system`：不要合并。** 它是 Qt 专用主题层（方案 B 下作废）；其设计 token 已存在于原型 CSS 中。分支上的 docs 提交已摘到 main。

## 4. 设计对齐

**设计稿在哪**：`docs/mockups/2026-07-16_redesign_prototype.html`（本仓库内，持久）。线上副本 https://claude.ai/code/artifact/43052d58-5492-404b-b6f1-ab163c937ee8 （可能失效，以 repo 文件为准）。

**已对齐、直接照抄的部分**（决策细节与理由见 progress 文档 §2 的 9 条决策表）：

| 部分 | 状态 |
|---|---|
| 色彩 token（长春花靛蓝 #5468FF 体系、深浅双主题、深色对比度修正值） | ✅ 定稿，原型 CSS `:root` 变量即真相 |
| 字体栈、字阶 | ✅ 定稿 |
| 27 个线性 SVG 图标（1.8px 描边 / currentColor） | ✅ 定稿，原型内 `<symbol>` 直接可取 |
| 主版式：等高双栏（左表内滚 / 右满高锚底） | ✅ 定稿 |
| 侧栏：图标窄栏，上=Excel/Word/PDF，线下=记忆库/配置 | ✅ 定稿 |
| 模型配置：左侧可折叠停靠面板，默认折叠+记状态；顶栏只显模型名药丸 | ✅ 定稿 |
| 顶栏：标题+状态徽章+一行简介；无页内大标题 | ✅ 定稿 |
| 视图覆盖：Excel(idle)、Word(running)、PDF(done)、记忆库(通栏)、配置面板 | ✅ 原型内可交互查看 |

**原型未覆盖、需要补设计的部分**——规则：**只用原型底部规范文档里的既有 token 与组件延伸，禁止新造样式**：

- error / stopped 两种任务终态视图
- 各类对话框：更新提示（含 release notes 预览）、旧数据迁移向导、浏览源路径的文件夹/文件二选一、模型配置导入/导出结果、终止任务确认
- Excel 复核标记颜色控件（预设色 + 自定义 #RRGGBB + 选色器 + 已有底色处理策略）
- PDF 翻译审核模型区块、吞吐调优（批次/并发 spinbox 及其上下界提示）
- 记忆库的编辑/固定/清洗交互细节

**产品行为注意**（已与用户对齐，勿改逻辑）：Excel 与 Word **共享** `settings.source_lang / target_lang`；PDF 用独立 `settings.pdf.target_lang` 且**没有**源语言（页图翻译由视觉模型识别原文）。UI 上用小字标注共享关系（原型 Word 页已示范），逻辑保持现状。

## 5. 分阶段计划与验收门

每个 Phase 一个分支（`refactor/phase-N-*`），完成即合入 main，不留长期分支。**每个 Phase 结束必须过验收门才能进下一个。**

### Phase 0 · 瘦身（与壳层无关，可独立发 V7.5）

按 assessment 文档执行：① PyMuPDF→pypdfium2+Pillow（仅 `core/pdf_image_translation.py`，8 种调用）；② 四家云 SDK 收敛为 OpenAI 兼容 httpx 客户端（智谱/通义走兼容端点；Anthropic 薄客户端或保留官方 SDK 二选一）+ 删 dashscope/zhipuai 依赖；③ PyInstaller 排除未用 Qt 模块/翻译文件；④ 仓库补 LICENSE（licence 类型问用户，见 §7）。

**验收门**：全量测试通过（引擎层测试按新客户端调整）；双平台 `--smoke-test` 通过；安装包 ≤100MB；用真实 key 对四家服务商各发一次连通性测试。

### Phase 1 · API 层（纯加法，Qt 版继续可用）

新建 `api/`（FastAPI 应用 + uvicorn 启动器，绑 127.0.0.1:0，stdout 打印 `PORT=<n> TOKEN=<t>` 握手行）。端点族 → 现有实现的映射：

| 端点族 | 包装的现有代码 |
|---|---|
| settings GET/PUT、密钥 CRUD（scoped） | `settings.py`（`load/save_settings`、`save_key/get_key`、`api_key_scope`） |
| 源路径扫描（excel/word/pdf） | `core/file_scanner.py` + 各页 ScanWorker 的输入校验逻辑 |
| 任务 start/stop、状态、**SSE 事件流** | `core/task_runner.py`（消息类型直译为 SSE data） |
| 任务互斥锁查询 | 重建 `TaskResourceRegistry` 语义（见 §3） |
| TM：搜索/增删改/固定/清洗/导入导出/指标 | `core/tm_manager.py` |
| 模型列表 / 连通性×3 / 吞吐上下界 | `core/model_catalog.py`、`core/connectivity_check.py`、`core/image_generation.py`、`core/pdf_review.py`、`core/model_throughput.py` |
| 模型配置导入/导出 | 从 `main_window.py` 迁出的纯函数（见 §3） |
| 更新检查 / 诊断导出 / 维护与重置 | `core/update_checker.py`、`core/diagnostics.py`、`core/maintenance.py` |

**验收门**：FastAPI TestClient 覆盖每个端点族（含 SSE 至少一条全生命周期流：start→progress→log→done）；一次真实 Excel 小文件翻译经 curl 全程走通;Qt 应用零回归（`pytest tests/ -q` 全绿）。

### Phase 2 · Tauri 壳 + UI 移植

脚手架：`src-tauri/`（Rust：窗口、单实例插件、sidecar spawn/健康检查/退出必杀）+ `ui/`（Vite + vanilla TS，把原型拆为模块：tokens.css / components / views / api-client / sse）。

移植顺序与页级验收：**壳层**（导航/顶栏/主题切换+跟随系统/配置面板折叠记忆）→ **Excel** → **Word** → **PDF** → **记忆库** → **对话框族**。每页验收 = ① 与 Qt 版逐控件功能对照（打开两边对着点）；② 与原型视觉对照（双主题）；③ running 态 SSE 日志/进度实时且不丢尾部；④ 中文 tooltip 文案迁移完成。

**Phase 验收门**：五页全过页级验收；开发模式（`tauri dev`）下从扫描到产出双语文件的三种翻译全流程各走通一次。

### Phase 3 · 打包、建立新基线、下线 Qt

- Tauri bundler：仅 macOS 原生 DMG（Apple Silicon `arm64` 与 Intel `x64`）；**壳与 sidecar 两个可执行体都要签名、公证并通过 Gatekeeper 验证**。sidecar 以 external binary 打进资源，注意 onedir 在 `.app/Contents/Resources` 下的路径解析。
- 新版建立独立数据基线：不得读取、复制、迁移、修复或删除旧版本的设置、TM、Key、模型或自定义语言数据。
- `core/update_checker.py` 资产命名适配新安装包文件名。
- 删除 `native_app/`、PySide6 依赖、旧 spec 中 Qt 相关项;`scripts/launch_native.py` 改指新入口或移除;随之退役 native_app 相关测试。
- `docs/CHANGELOG.md` 记 V8.0.0；ADR 新增一条记录壳层更换决策（引用 assessment 文档）。

**最终验收门**：见 §8。

## 6. 已知坑（提前踩过的）

1. **sidecar 孤儿进程**：壳崩溃/强杀时 Python 进程残留。双保险：Rust 侧 kill-on-drop/进程组；Python 侧 watchdog（stdin EOF 或父 pid 消失即自杀）。
2. **端口与安全**：绑 `127.0.0.1:0` 取随机口；所有请求校验启动 token；CORS 只放 tauri origin。
3. **PyInstaller 路径**：`sys._MEIPASS` 与资源相对路径在 onedir/.app 内的差异（现有 `core/app_paths.py` 与 `theme` 的 asset_url 写法可参考）。
4. **SSE**：uvicorn 默认可用，但注意反缓冲（响应头 `X-Accel-Buffering: no` 不必要但无害）；前端 EventSource 断线重连要幂等。
5. **xlwings / .xls**：依赖本机安装 Excel 的运行时探测逻辑照旧（`core/xls_converter.py` 有 availability 检查），UI 的禁用态提示要等价迁移。
6. **杀软误报**：PyInstaller 产物在 Windows 偶发误报，是现状不是回归；不要为此改打包方式。
7. **中文字体**：WebView 用系统字体栈（原型已写好 PingFang/微软雅黑），无需打包字体。
8. **380 个测试的构成**：其中 native_app（Qt）相关测试在 Phase 3 随 Qt 层退役属正常，core 测试必须全程全绿。

## 7. 何时停下来问用户（除此之外自主推进）

- 安装包超过 80MB，且无明显可裁项
- settings schema 不得不做破坏性变更
- 某个 Qt 功能在 web 壳找不到等价实现
- 认为必须引入前端框架
- LICENSE 类型选择；代码签名/公证证书账号事宜
- 每个 Phase 完成时汇报一次（验收门结果 + 安装包当前体积），发版时机由用户定

## 8. 最终验收清单（V8.0.0 发布前）

- [ ] 从真实 V7.4 数据目录升级启动：设置 / TM / 密钥 / 领域 prompt 覆盖全部还在
- [ ] Excel / Word / PDF 各完成一单真实翻译任务（用户提供 key），产物与 V7.4 对照无差异
- [ ] 任务互斥、终止、error/stopped 态、诊断导出行为与 V7.4 等价
- [ ] 深浅主题切换 + 跟随系统；配置面板折叠状态记忆
- [ ] 更新检查、忽略更新、单实例激活可用
- [ ] Windows + macOS 安装包均 ≤70MB（>80 触发 §7），双平台安装→卸载→重装干净
- [ ] `pytest`（core + api 层）全绿；CHANGELOG 与 ADR 已写
