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
  退出时先请它自己收尾（Unix 发 SIGTERM，Windows 调 `POST /api/system/shutdown`），再按
  `main.rs` 顶部注释里的预算等它退出。两边的超时数值是成对的，改一边要改另一边。
- 用户数据（`settings.json`、`keys.json`、`tm.db`、任务历史）都在 `APP_DATA_DIR`，
  测试或本地调试用环境变量 `TRANSLATOR_APP_DATA_DIR` 指到隔离目录（`core/app_paths.py`）。

## 目录速查

| 位置 | 内容 |
|---|---|
| `api/app.py` | 只做装配：生命周期、CORS、令牌中间件、异常处理，然后按顺序调用各路由模块的 `register(app)` |
| `api/routes/` | 按领域分的路由：`tasks`、`settings`、`keys`、`models`、`tm`、`domains`、`cad`、`config_transfer`、`maintenance`。路由的声明顺序有意义（Starlette 按顺序匹配），`app.py` 里的注册顺序不要随意调换 |
| `api/schemas.py` | 所有请求体的 Pydantic 模型 |
| `api/surfaces/` | **板块注册表**：每个翻译页面一个 `Surface` 子类（excel、word、pdf、cad，外加 TM 清洗）；运行时注册的类型（`ext:<id>`）也登记在这里，见下文「类型描述符与通用路径」 |
| `api/routes/surfaces.py` | `GET /api/surfaces`：每个已注册页面的类型描述符 |
| `cli/` | 命令行（`types`、`scan`、`translate`、`status`、`stop`），见下文「命令行与 MCP 入口」 |
| `core/engine_client.py`、`core/engine_file.py` | 连上正在运行的引擎或自己拉起一个；引擎写的连接文件 `run/engine.json` |
| `api/task_manager.py` | 任务生命周期：准备、互斥与资源占用、启动执行器、把执行器消息转成任务事件、历史落盘、暂停/继续/停止 |
| `api/task_contracts.py` | 执行器协议 `Runner`、`TaskOptions`、任务异常 |
| `core/runner_base.py` | 所有执行器共用的「后台线程 + 消息队列」外壳 `BaseTaskRunner` |
| `translator_adapter/` | 格式适配器要 import 的协议类型（只依赖标准库），见下文「格式适配器」 |
| `core/format_adapter.py`、`core/adapter_runner.py`、`api/surfaces/adapter.py` | 适配器清单与加载、通用执行器、按清单生成的页面 |
| `core/text_translation_service.py` | 共享的文本翻译接线 `TextTranslationService`（速率与重试、引擎、提示词、分批请求、记忆库），CAD、PDF 原位替换和格式适配器在用 |
| `core/surface_settings.py` | 按页面取设置：内置页面读平铺字段，运行时注册的类型读 `settings.extensions` |
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
| `ui/src/views/workspace.ts` | 四个翻译页共用的页面模板；`excel.ts`、`word.ts`、`pdf.ts`、`cad.ts` 只是一行封装，运行时注册的类型由 `extension.ts` 按键生成同样的封装 |
| `ui/src/surface-descriptors.ts` | 启动时拉一次类型描述符，按当前界面语言取名称、图标、文件名词 |
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

## 类型描述符与通用路径

内置的四个翻译页面随应用发布，前端为它们写了编译期的表和 `surface === "…"` 分支。以后
新增的文件类型（格式适配器）不随应用发布，在运行时注册，前后端都不为它写专门代码，走一条
通用路径。格式适配器（见下文「格式适配器」）就是按这条路径注册的。

- **注册**：`api/surfaces/__init__.py` 的 `register_surface(surface)`，只接受键为 `ext:<id>`、
  `builtin = False` 的 `Surface`，不能顶替或冒充内置页面；`unregister_surface(key)` 移除，
  对内置键无效。注册之后，扫描、开始任务、进度权重、暂停 / 继续都和内置页面一样从注册表取；
  请求里的 `surface` 按已注册的键精确匹配。续译检测（`previous_output`）只对内置页面做。
- **描述符**：`GET /api/surfaces` 按注册顺序列出每个页面的 `Surface.describe()`：
  `key`、`builtin`、`label`（中英法三语）、`file_noun`（三语）、`icon`（`ui/src/icons.ts` 的图标名）、
  `extensions`（小写、不带点）、`capabilities`（`requires_source`、`can_pause`、`pause_mode`、
  `can_resume`、`can_end_paused`、`bilingual`、`translation_only`）、`phases`（各阶段的三语名称，
  与 `phase_weights` 一一对应）、`options`（运行设置里的开关：`key`、`kind`（目前只有 `"toggle"`）、
  `label`、`help`、`default`）。
- **设置**：每个运行时类型一份 `settings.extensions[<id>]`（`ExtensionSettings`，schema v39）：
  `target_lang`、`source_lang`、`domain_preset`、`smart_context`、`domain_custom_prompts`、
  `options`、`pinned`、`order`。按页面取设置一律经 `core/surface_settings.py` 的
  `surface_setting` / `set_surface_setting`，内置页面读平铺字段（`excel_target_lang` …），调用方不用分辨。
- **模型与速率**：`page_key = "adapter"` 的任务只占文档翻译模型（`task_model_roles_for_page`）；
  速率与重试没有单独一组时按模型吞吐量取（`core/surface_rate.py`）。
- **文本翻译**：`TextTranslationService` 把「按翻译参数取速率与重试、建文档翻译引擎、拼提示词
  （补充要求、文档说明、固定术语）、分批请求、记忆库整段复用与词条附批」接好；只需要「抽文字 →
  翻译 → 写回」的格式，执行器用它即可，不必再抄一遍 CAD 的接线。
- **开关值**：开始任务时放在请求的 `options` 字典里（`TaskStartRequest.options` →
  `TaskOptions.options`），执行器从 `RunnerRequest.options.options` 读。
- **界面**：`app.ts` 拿到描述符后为每个 `ext:` 类型注册一个视图（`views/extension.ts`），设置里
  `pinned` 的按 `order` 放进侧栏「翻译」组，排在内置四项之后。工作区模板里
  `` Surface = BuiltinSurface | `ext:${string}` ``：名称、图标、文件名词、空状态和开关经访问函数
  （`surfaceLabel`、`surfaceToggles` 等）从描述符取；统计格（文件数、总大小、跳过项）和文件表列
  （文件、格式、大小、状态）是通用的一套；运行设置显示语言和描述符里的开关，「更多设置」（补充要求、
  输出位置）暂不显示，按默认输出位置写。任务中心的图标、名称、可暂停和「打开工作区」也按描述符。
- **测试**：`tests/fixtures/sample_surface.py` 注册仅测试用的 `ext:sample`（扫描 `.txt`，原样写出，
  开关「转为大写」，可暂停），`tests/test_runtime_surface.py` 用真实路由走通整条路径。它不随应用发布。

还没有的（后续阶段）：从外部安装格式适配器、插件页卡片、「翻译参数」里给扩展类型的页签、
MCP 入口。

### 格式适配器

只需要「从文件里取出文字、把译文写回去」的格式，不写 `Surface` 子类，也不写执行器，而是写一个
格式适配器：一个目录、两个文件。页面、模型调用、记忆库、重试、复核、进度和任务中心都由应用负责。

```
adapters/<id>/
  adapter.json   清单：是什么类型、处理哪些文件、页面上显示什么
  adapter.py     scan（可省）、extract、write 三个函数，只用标准库和 translator_adapter
```

- **协议**（`translator_adapter/`，只依赖标准库，不 import 应用；`ADAPTER_API_VERSION = 1`，以后只做加法）：
  - `extract(path) -> list[TextUnit]`。`TextUnit` 的字段：`id`（文件内唯一，`write` 靠它放回译文）、
    `text`、`context`（给人看的位置，如「第 12 行」，写进需复核清单；空着时清单写 `id`）、`kind`
    （`paragraph`、`heading`、`list_item`、`table_cell` 等，目前只作记录）、`locked`（原样写回、不送模型，
    如代码块）、`protected`（`text` 里必须原样回来的片段，如行内代码、链接地址）。
  - `scan(path) -> ScanItem(unit_count, warnings)`：扫描时显示的单元数；省略时按 `extract` 数未锁定的单元。
  - `write(path, units, mode, out_path, options) -> OutputFile(path, review_count)`：`units` 是
    `TranslatedUnit(id, text, translation, status)`（`translation` 与 `text` 相同表示保留原文），`mode` 为
    `translation_only` 或 `bilingual`，`options` 是本次任务的开关值；返回 `None` 视为已写到 `out_path`。
  - 适配器不调模型、不碰记忆库、不读密钥、不联网。
- **清单**（`core/format_adapter.py` 的 `AdapterManifest`，未知字段一律拒绝）：`id`（小写字母、数字、`_`、`-`，
  页面键为 `ext:<id>`，设置在 `settings.extensions[<id>]`）、`version`、`adapter_api_version`（须在
  `SUPPORTED_ADAPTER_API_VERSIONS` 内）、`label` 与 `file_noun`（中英法三语必填）、`description`（三语，可省，
  页面「?」里的说明）、`icon`（`ui/src/icons.ts` 的图标名）、`extensions`、`capabilities`（`bilingual`、
  `translation_only`，至少一个）、`options`（开关；`bilingual` 留给应用）、`license`、`repo`、`min_app_version`。
  两种输出都支持时，应用给页面加「双语对照」开关（默认关），执行器据此选 `mode`。
- **加载**：`create_app` 调 `register_bundled_adapters()`（`api/surfaces/adapter.py`），把 `adapters/` 下每个合格
  目录注册成一个 `AdapterSurface`；清单不合格或缺 `adapter.py` 的写日志跳过，不影响启动。`InProcessAdapterHost`
  第一次用到时从源码加载 `adapter.py`，不读也不写 `__pycache__`（安装包签过名，不能往里写文件）；适配器抛出
  的异常一律换成 `AdapterError`，返回值按协议类型核对（`id` 非空且不重复，`protected` 只留原文里确有的片段）。
  打包时 spec 只收每个目录的 `adapter.json` 与 `adapter.py`。从外部安装适配器（子进程执行）是下一阶段。
- **执行器**（`core/adapter_runner.py` 的 `AdapterTaskRunner`，不认识任何具体格式）对每个文件：
  1. `extract` 取单元；锁定的和不含文字的不送模型。
  2. 原文语言为「自动」时识别这个文件的原文语言（用于记忆库语言对和日志，提示词仍按「自动」）。
  3. 「自动识别场景」开启时读一遍生成文档说明。
  4. 记忆库整段命中的直接采用；其余把 `protected` 片段换成 `⟦n⟧` 标记，经 `TextTranslationService`
     分批送模型（速率与重试按文档翻译模型的吞吐量，`core/surface_rate.py` 的 `extension_rate`）。
  5. 还原标记，做残留原文体检（`run_residual_pass`），按单元记进 `UnitLedger`。
  6. `write` 到 `{来源文件夹}_翻译输出_{时间}/` 下与来源相同的相对路径，文件名
     `{原名}_{目标语言}[_双语].{扩展名}`。

  进度按文件大小加权直接给总百分比，不会每换一个文件就从头走一遍；文件之间、批次之间都能暂停；停止时
  正在翻的文件已有译文就照样写出，后面的文件记为「未开始」；一个文件读写出错只算它失败，全部失败才报错。
- **复核口径**：下面几种单元记为需复核，写进输出目录的 `需复核清单.txt`（UTF-8 带 BOM，制表符分隔四列：
  文件、单元、原文、原因），任务结果的 `review.items` 带前 200 条给任务中心的结果表，`review.report_path`
  是清单路径。停止时还没翻的不算复核。

  | 情况 | 写回 |
  |---|---|
  | 未获得译文（接口放弃，或 `⟦n⟧` 标记丢失、重复） | 原文 |
  | 受保护片段没能原样保留 | 原文 |
  | 译文里仍有原文 | 译文 |
  | 记忆库设定的译法没被采用 | 译文 |
- **测试**：`tests/fixtures/adapters/echo`（每行一个单元，`#!` 行锁定，`{{name}}` 受保护，仅测试用）；
  `tests/test_format_adapter.py` 测清单、宿主和描述符，`tests/test_adapter_runner.py` 用假引擎测执行器，
  末尾一项经真实路由跑到结束。

## 命令行与 MCP 入口

窗口之外，同一个侧车可执行文件带子命令时是命令行：`translator-sidecar types | scan | translate |
status | stop`（源码里 `./.venv/bin/python3 -m cli …`）。`scripts/launch_sidecar.py` 看到
`cli.main.COMMANDS` 里的子命令就交给 `cli/main.py`，否则照旧作为翻译引擎运行。命令行自己不翻译，
和界面一样只调侧车的 HTTP 接口，所以任务进任务中心，互斥、记忆库、历史都与窗口里发起的相同。

- **连接文件**：引擎打出 `PORT=… TOKEN=…` 握手后写 `APP_DATA_DIR/run/engine.json`：`port`、`token`、
  `pid`、`version`、`started_at`（`core/engine_file.py`；目录 0700、文件 0600，先写临时文件再替换）。
  `begin_shutdown` 和 `server.run` 返回后删除，只删 `pid` 是自己的那份。写不进去只记一条警告，
  命令行改为自己起引擎。
- **连上或拉起**（`core/engine_client.py` 的 `connect`）：读连接文件 → 进程还在 → 带令牌
  `GET /health` 通过，就连上正在运行的应用；否则起一个引擎子进程（冻结时是侧车自身，源码时
  `python -m api.launcher`），解析同一行握手（规则与 `main.rs` 一致，测试钉住），并把
  `TRANSLATOR_SIDECAR_PARENT_PID` 设为命令行自己，命令结束引擎随之退出；这时 `translate` 不带
  `--wait` 也会等任务结束。自起引擎的 stderr 在 `run/cli-engine.log`。`EngineClient` 封装
  `surfaces`、`scan`、`preflight`、`start`、`tasks`、`task`、`results`、`events`（SSE，断线按
  `Last-Event-ID` 续）、`stop`。
- **类型与选项**（`cli/flow.py`，MCP 共用）：全部来自 `GET /api/surfaces`。单个文件按扩展名取第一个
  声明它的类型，文件夹或多个文件取文件最多的类型，`--type` 指定；`--option 名称=值` 对内置类型按
  `TaskStartRequest` 的字段类型转换，对 `ext:` 类型按描述符里开关的类型放进 `options`。运行时注册的
  类型不需要为命令行写任何代码。
- **已保存的默认值**：命令行的请求总带 `apply_saved_defaults: true`，引擎把没给的字段补成界面会发的值：
  各页面的 `Surface.saved_request_defaults` / `saved_scan_defaults`（Word 保护封面目录、PDF 是否含图片、
  CAD 整组输出设置；`ext:` 类型取描述符开关的默认值），只补请求里没有的字段，`options` 逐键合并，
  预检响应里的 `options` 是实际要用的值。界面不带这个字段，行为不变；
  `tests/test_saved_request_defaults.py` 把各页面的钩子钉在 `workspace.ts` 的 `buildPayload` /
  `scanPayload` 上，改一边要改另一边。
- **需要确认的拦截**：窗口里能点「继续」的三种预检拦截（无 Excel 的 .xls、无 Word 的 .doc、PDF 复核
  模型不可用）在 422 里带 `accept`，即再发一次时加上就能继续的字段；前两种 `reason` 为
  `needs_confirmation`，PDF 那条保留界面在用的 `pdf_review_model_unavailable`。与正在运行的任务共用
  模型连接走预检的 `requires_confirmation` + `confirmation_token`。命令行默认停下（退出码 4），
  `--yes` 逐个接受，最多四种；`translate --dry-run` 只扫描和预检，输出 JSON。
- **退出码**：0 任务完成（部分文件失败也算），1 什么都没生成（全部失败、出错或被停止），2 参数或
  输入被拒，3 连不上也起不来引擎，4 需要确认。结果写 stdout，诊断写 stderr；`--json` 输出一个对象，
  `--json-lines` 每个事件一行。
- **测试**：`tests/fixtures/cli_engine.py` 是注册了 `ext:sample`、用回声引擎代替模型的真实
  `api.launcher`，`tests/test_cli.py` 用它充当已打开的应用走通各子命令；冻结冒烟
  （`scripts/frozen_smoke.py`）在冻结包里跑一次 `types`，确认四个内置类型都在。跑批测试用
  `agent/testing/seed_test_model.py` 在隔离数据目录里指定测试模型，命令行没有换模型的参数。

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
| 新增一个内置翻译板块 | [ADDING_A_TRANSLATION_PAGE.md](ADDING_A_TRANSLATION_PAGE.md) |
| 支持一种新文件格式（只需取字和写回） | 上文「格式适配器」：在 `adapters/<id>/` 写 `adapter.json` 和 `adapter.py`，不新增内置板块 |
| 让一种需要特殊流程的格式在运行时出现在界面上 | 上文「类型描述符与通用路径」：写一个 `builtin = False`、键为 `ext:<id>` 的 `Surface`，用 `register_surface` 注册 |
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
- 升级 tauri 或其插件时按 `TAURI_UPGRADE_CHECKLIST.md` 逐条复核。

## 其他文档

`docs/` 下按日期命名的审计、方案文件是当时的记录，描述的是写作时的代码，可能已经过时；
以本文、`ADDING_A_TRANSLATION_PAGE.md`、`KNOWN_ISSUES.md`、`CHANGELOG.md` 和 `docs/adr/`
为准。
