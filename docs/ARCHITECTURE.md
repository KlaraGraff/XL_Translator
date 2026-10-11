# 架构说明

给第一次接手这个仓库的人看：代码分几层、一次翻译任务怎么从按钮走到写出文件、
改某类东西该去哪里找。具体到「新增一个翻译板块」的步骤见
[ADDING_A_TRANSLATION_PAGE.md](ADDING_A_TRANSLATION_PAGE.md)。

## 三层

```
┌──────────────── Tauri 壳（src-tauri/，Rust） ────────────────┐
│  启动、退出、文件对话框、拉起并看护 Python 侧车                  │
│   ┌──────────── 界面（ui/，原生 TypeScript + Vite） ─────────┐  │
│   │  views/*.ts 页面  ·  api-client.ts 唯一的 HTTP 出口       │  │
│   └───────────────────────────┬─────────────────────────────┘  │
└───────────────────────────────┼────────────────────────────────┘
                                │ HTTP + 每次启动生成的令牌（SSE 推进度）
┌───────────────────────────────▼────────────────────────────────┐
│  Python 侧车（api/ → core/ → engines/）                          │
│   api/   FastAPI 路由、任务管理、板块注册表                       │
│   core/  翻译流程本身：读文档、记忆库、调模型、修复、写文件          │
│   engines/ 各家模型接口的薄封装                                   │
│   settings.py / config.py  用户设置 / 不可变常量                  │
└─────────────────────────────────────────────────────────────────┘
```

- 界面不直接读写文件，也不直接调模型；一切都经 `ui/src/api-client.ts` 发给侧车。
- 侧车由 `api/launcher.py` 启动，Rust 侧在 `src-tauri/src/main.rs` 里拉起它、传端口和令牌，
  退出时按 `main.rs` 顶部注释里的预算等它收尾。两边的超时数值是成对的，改一边要改另一边。
- 用户数据（`settings.json`、`keys.json`、`tm.db`、任务历史）都在 `APP_DATA_DIR`，
  测试或本地调试用环境变量 `TRANSLATOR_APP_DATA_DIR` 指到隔离目录（`core/app_paths.py`）。

## 目录速查

| 位置 | 内容 |
|---|---|
| `api/app.py` | 只做装配：生命周期、CORS、令牌中间件、异常处理，然后按顺序调用各路由模块的 `register(app)` |
| `api/routes/` | 按领域分的路由：`tasks`、`settings`、`keys`、`models`、`tm`、`domains`、`cad`、`config_transfer`、`maintenance`。路由的声明顺序有意义（Starlette 按顺序匹配），`app.py` 里的注册顺序不要随意调换 |
| `api/schemas.py` | 所有请求体的 Pydantic 模型 |
| `api/surfaces/` | **板块注册表**：每个翻译页面一个 `Surface` 子类（excel、word、pdf、cad，外加 TM 清洗） |
| `api/task_manager.py` | 任务生命周期：准备、互斥与资源占用、启动执行器、把执行器消息转成任务事件、历史落盘、暂停/继续/停止 |
| `api/task_contracts.py` | 执行器协议 `Runner`、`TaskOptions`、任务异常 |
| `core/runner_base.py` | 所有执行器共用的「后台线程 + 消息队列」外壳 `BaseTaskRunner` |
| `core/task_runner_messages.py` | 执行器发给任务管理器的消息类型（进度、状态、日志、完成、出错、已停止……） |
| `core/task_runner.py` | Excel 执行器 |
| `core/word_task_runner.py` | Word 执行器 |
| `core/pdf_image_translation.py` | PDF 与图片执行器（按页出图、复核、单页重跑） |
| `core/cad_task_runner.py` + `core/cad_translation.py` + `api/cad_plugin.py` | CAD 执行器、DWG/DXF 处理、外部 CAD 插件探测 |
| `core/tm_manager.py`、`tm_hygiene.py`、`tm_cleaner.py` | 翻译记忆库：查询、入库前清洗、AI 清洗 |
| `core/residual_*.py` | 译文残留中文体检、确定性修复、修复阶梯 |
| `core/language_preflight.py`、`mixed_language.py` | 自动识别源语言、混合语言段落 |
| `core/model_*.py`、`engine_dispatcher.py`、`failover_engine.py`、`api_scheduler.py` | 模型配置、角色、吞吐量、失败切换、全局并发调度 |
| `engines/` | `TranslationEngine` 基类和各厂商实现 |
| `ui/src/views/workspace.ts` | 四个翻译页共用的页面模板；`excel.ts`、`word.ts`、`pdf.ts`、`cad.ts` 只是一行封装 |
| `ui/src/components.ts`、`task-panels.ts` | 共享界面组件 |
| `tests/` | pytest；`test_*_contracts.py` 是「结构约定」测试，失败时提示信息会说该补哪里 |

## 一次任务怎么跑

以在 Word 页点「开始」为例：

1. **扫描**：选好来源后界面自动调 `POST /api/sources/scan`。路由按请求里的 `surface`
   找到注册表里的 `WordSurface`，调用它的 `scan_sources()` 列出可翻译文件。
2. **预检与启动**：`POST /api/tasks` → `TaskManager.start_task()` → `_prepare_task()`：
   - `surface.validate_preflight()` 检查目标语言、模型配置等，不合格直接拒绝；
   - `surface.snapshot()` 把这次用到的设置冻结进任务快照（之后改设置不影响已开始的任务）；
   - 算任务指纹（同样的输入不会重复开两个任务），占用连接与文件资源。
3. **构建执行器**：`surface.build_runner(RunnerRequest)` 返回 `WordTaskRunner`。
   任务管理器调用 `runner.start()`，执行器在自己的后台线程里跑 `_run()`。
4. **回传进度**：执行器只往队列里放消息（`core/task_runner_messages.py`）。任务管理器的
   泵线程（`_pump_runner`）循环 `get_message()`，在 `_handle_message()` 里转成任务事件，
   按 `surface.phase_weights` 折算总进度，追加进事件流并定期写历史。
5. **界面显示**：界面订阅 `GET /api/tasks/{id}/events`（SSE），按事件更新进度卡片、
   逐文件状态和日志。
6. **结束**：执行器最后发 `DoneMsg` / `StoppedMsg` / `ErrorMsg` 之一，任务管理器写最终历史、
   释放资源；除 PDF（要保留执行器做单页复核和重跑）外，执行器随即释放。

暂停、继续、结束暂停：只有 `Surface.can_pause` 等标志为真的板块才开放，执行器需要提供
同名方法（`pause()`、`resume()`、`end_paused()`）。

## 执行器内部的阶段

Excel 和 Word 的 `_run()` 只做编排，每个阶段是一个独立方法，按顺序调用：

| 阶段 | Excel（`core/task_runner.py`） | Word（`core/word_task_runner.py`） |
|---|---|---|
| 1 提取 | `_collect_file_texts`（每个文件一次） | `_collect_file_texts`（每个文件一次） |
| 自动识别源语言 | `_preflight_source_languages` | `_preflight_source_languages` |
| 2 记忆库 + 模型翻译 | 记忆库匹配在 `_run` 内；`_translate_misses_via_api` | 记忆库匹配与模型翻译都在 `_run` 内（停止说明靠闭包回写，暂不外提） |
| 残留修复 | `_repair_residuals` | `_repair_residuals` |
| 全篇标题统一 | `_normalize_heading_style` | `_normalize_heading_style` |
| 写记忆库 | `_store_final_translations_in_tm` | `_store_final_translations_in_tm` |
| 页眉页脚 | — | `_resolve_header_footer_lines` |
| 3 写文件 | `_write_file_output`（每个文件一次） | `_write_file_output`（每个文件一次） |
| 4 自动列宽 | `_run_autofit_phase`（需要 Excel） | — |

几条贯穿各执行器的规矩：

- **先定稿再入库**：记忆库只存最终译文（修复、标题统一之后的版本），所以写库排在修复之后。
- **停止不丢已付费结果**：模型返回之后才收到停止的，照样写库、写文件，只是不再发新请求。
- **阶段方法的参数全部写成关键字参数**，传进去什么、返回什么在签名里一目了然；
  共享的列表和字典是按引用传入、就地修改的，与拆分前的行为一致。

## 改什么去哪里

| 想做的事 | 去哪里 |
|---|---|
| 新增一个翻译板块 | [ADDING_A_TRANSLATION_PAGE.md](ADDING_A_TRANSLATION_PAGE.md) |
| 新增一个 HTTP 接口 | 对应领域的 `api/routes/<领域>.py`，请求体放 `api/schemas.py` |
| 新增一个设置项 | `settings.py`，必须带默认值（见 `AGENTS.md` 旧数据兼容规则） |
| 新增一家模型厂商 | `engines/` 加一个 `TranslationEngine` 子类，再在 `core/engine_dispatcher.py` 里按厂商构建它。厂商名还散在 `config.py`、`core/model_catalog.py`、`model_config.py`、`api_config_check.py`、`connectivity_check.py` 和设置页 `ui/src/views/settings-models.ts`，拿已有厂商名（如 `ollama`）全仓搜一遍逐处补上 |
| 改进度条各阶段占比 | 对应 `api/surfaces/<板块>.py` 的 `phase_weights` |
| 改某个板块开始前的检查 | 对应 `Surface.validate_preflight()` |
| 改界面通用组件 | `ui/src/components.ts`、`task-panels.ts`，颜色间距用 `styles/tokens.css` 变量 |

## 检查与测试

- 本地：`powershell -ExecutionPolicy Bypass -File .\quality_gate.ps1`（ruff 静态检查），
  然后 `./.venv/bin/python3 -m pytest tests`，界面 `cd ui && npm run check`。
- 每个 PR 和推到 `main` 时，GitHub Actions（`.github/workflows/pr-checks.yml`）在 Python 3.11
  上跑 ruff、pytest，在 Node 24 上跑 TypeScript 检查。
- 打版本标签前另跑 `bash scripts/run_release_env_tests.sh`（见 `AGENTS.md` 第 8 条）。

## 其他文档

`docs/` 下按日期命名的审计、方案文件是当时的记录，描述的是写作时的代码，可能已经过时；
以本文、`ADDING_A_TRANSLATION_PAGE.md`、`KNOWN_ISSUES.md`、`CHANGELOG.md` 和 `docs/adr/`
为准。
