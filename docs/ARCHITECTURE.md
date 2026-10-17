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
| `cli/` | 命令行（`types`、`scan`、`translate`、`status`、`stop`）和 MCP 服务器（`mcp`），见下文「命令行与 MCP 入口」 |
| `core/engine_client.py`、`core/engine_file.py` | 连上正在运行的引擎或自己拉起一个；引擎写的连接文件 `run/engine.json` |
| `api/task_manager.py` | 任务生命周期：准备、互斥与资源占用、启动执行器、把执行器消息转成任务事件、历史落盘、暂停/继续/停止 |
| `api/task_contracts.py` | 执行器协议 `Runner`、`TaskOptions`、任务异常 |
| `core/runner_base.py` | 所有执行器共用的「后台线程 + 消息队列」外壳 `BaseTaskRunner` |
| `translator_adapter/` | 格式适配器要 import 的协议类型（只依赖标准库），见下文「格式适配器」 |
| `adapters/` | 随应用提供的格式适配器，一个类型一个目录；目前有 `text`（纯文本与 Markdown） |
| `core/format_adapter.py`、`core/adapter_runner.py`、`api/surfaces/adapter.py` | 适配器清单与加载、通用执行器、按清单生成的页面 |
| `core/adapter_install.py`、`core/adapter_subprocess_host.py`、`api/extension_install_job.py`、`api/routes/extensions.py` | 用户添加的扩展：安装前检查、安装登记、子进程宿主、后台安装任务和路由，见下文「安装与子进程宿主」 |
| `core/adapter_registry.py` | 扩展列表（注册中心）的客户端，见下文「扩展列表」 |
| `cli/adapter_commands.py`、`cli/adapter_template/` | `adapter new / test / install / uninstall / pack` 和扩展模板（与模板仓库逐字节一致） |
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
  `options`、`prompt`（v40：用户改过的内置规则，空表示用默认）；`pinned`、`order` 是 v39 的侧栏设置，v40 起
  由 `translation_types` 管（`order` 只在升级时读一次）。按页面取设置一律经 `core/surface_settings.py` 的
  `surface_setting` / `set_surface_setting`，内置页面读平铺字段（`excel_target_lang` …），调用方不用分辨。
- **侧栏顺序与显示**：`settings.translation_types`（`TranslationTypesSettings`，v40）一张表管全部翻译类型
  （内置四种与 `ext:`）：`order` 是类型键的顺序（没列到的按描述符顺序排在后面，空即默认顺序，所以新类型排在
  最后），`visible` 是拨过「显示在侧栏」的类型；没拨过的按 `shows()` 的默认：内置四种显示，扩展类型（随应用
  提供的「文本」也一样）不显示，在「扩展」页打开。v39 升级时调过的 `order` 换成整张表的顺序，`pinned` 不沿用。
  不显示只影响侧栏：视图照常注册，任务中心「打开工作区」、命令行和 MCP 照常可用。`PUT /api/settings` 拒绝
  让全部类型都不显示（422），界面同样拦。
- **模型与速率**：`page_key = "adapter"` 的任务只占文档翻译模型（`task_model_roles_for_page`）；
  速率与重试没有单独一组时按模型吞吐量取（`core/surface_rate.py`）。
- **文本翻译**：`TextTranslationService` 把「按翻译参数取速率与重试、建文档翻译引擎、拼提示词
  （补充要求、文档说明、固定术语）、分批请求、记忆库整段复用与词条附批」接好；只需要「抽文字 →
  翻译 → 写回」的格式，执行器用它即可，不必再抄一遍 CAD 的接线。
- **开关值**：开始任务时放在请求的 `options` 字典里（`TaskStartRequest.options` →
  `TaskOptions.options`），执行器从 `RunnerRequest.options.options` 读。
- **界面**：`app.ts` 拿到描述符后为每个 `ext:` 类型注册一个视图（`views/extension.ts`，不显示的也注册），
  再按 `translation_types` 重排侧栏「翻译」组（`refreshTranslateSideItems`，显示与否看 `isTypeVisible`，与
  后端 `shows()` 同一规则）；启动时打开的 Excel 不显示了就换到侧栏第一项。工作区模板里
  `` Surface = BuiltinSurface | `ext:${string}` ``：名称、图标、文件名词、空状态和开关经访问函数
  （`surfaceLabel`、`surfaceToggles` 等）从描述符取；统计格（文件数、总大小、跳过项）和文件表列
  （文件、格式、大小、状态）是通用的一套；运行设置显示语言和描述符里的开关（清单有 `description` 时
  「选项」标题旁加「?」），「更多设置」（补充要求、输出位置）不显示，按默认输出位置写。任务中心的图标、
  名称、可暂停和「打开工作区」也按描述符。
- **「扩展」页与翻译参数**：侧栏的「扩展」页（路由 `extensions`，`views/extensions-page.ts`）
  一张表列出全部翻译类型（内置四种与 `ext:`），每行「显示在侧栏」开关和上移、下移，写
  `translation_types`，改完即时重排侧栏；表下一张「更多文件类型」说明卡（可自制、上传分享、下载他人的扩展；
  入口按钮与安装、浏览由阶段 E/F 接在后面）。「插件」页（运行组件）是另一页。「设置 → 翻译参数」的页签是
  内置四种加每个 `ext:` 类型各一个（标签用类型名，顺序同「扩展」页，隐藏的也保留）：目标语言、源语言写
  `extensions.<id>`；提示词卡片走 `/api/domains/ext:<id>`（只认已注册的类型），其中「内置规则」可以改
  （存 `extensions.<id>.prompt`，与默认相同或清空存成空），「恢复默认」回到清单的 `prompt`，没有则是
  `CORE_TRANSLATION_PROMPT`；速率与重试只有一句说明。发给模型的基础段由 `engine_dispatcher.core_prompt_for`
  依次取：用户改过的 → 清单 → 通用的那份，再接补充要求、文档说明和固定术语；内置四种始终是通用的那份。
  翻译参数配置包带 `extensions` 节（不含侧栏的 `pinned` / `order`），导入时按类型逐项合并；单独导出的
  提示词文件只带各类型的语言和提示词。
- **测试**：`tests/fixtures/sample_surface.py` 注册仅测试用的 `ext:sample`（扫描 `.txt`，原样写出，
  开关「转为大写」，可暂停），`tests/test_runtime_surface.py` 用真实路由走通整条路径。它不随应用发布。

还没有的（后续阶段）：从外部安装格式适配器（「扩展」页的「添加扩展…」）。

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
  页面「?」里的说明）、`prompt`（这种文件的内置规则，可省，最长 4000 字，省略时用通用的那份；描述符带出，
  用户可在翻译参数里改写）、`icon`（`ui/src/icons.ts` 的图标名）、`extensions`、`capabilities`（`bilingual`、
  `translation_only`，至少一个）、`options`（开关；`bilingual` 留给应用）、`license`、`repo`、`min_app_version`。
  两种输出都支持时，应用给页面加「双语对照」开关（默认关），执行器据此选 `mode`。
- **加载**：`create_app` 调 `register_bundled_adapters()`（`api/surfaces/adapter.py`），把 `adapters/` 下每个合格
  目录注册成一个 `AdapterSurface`；清单不合格或缺 `adapter.py` 的写日志跳过，不影响启动。`InProcessAdapterHost`
  第一次用到时从源码加载 `adapter.py`，不读也不写 `__pycache__`（安装包签过名，不能往里写文件）；适配器抛出
  的异常一律换成 `AdapterError`，返回值按协议类型核对（`id` 非空且不重复，`protected` 只留原文里确有的片段）。
  打包时 spec 只收每个目录的 `adapter.json` 与 `adapter.py`。用户装进来的适配器走子进程，见下文「安装与子进程宿主」。
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
  | 未获得译文（接口放弃，`⟦n⟧` 标记丢失、重复，或模型原样还回要翻的原文、质量校验退回原文；判据同 Excel、Word 的未译源文 `looks_like_source_text`） | 原文 |
  | 受保护片段没能原样保留 | 原文 |
  | 译文里仍有原文 | 译文 |
  | 记忆库设定的译法没被采用 | 译文 |
- **测试**：`tests/fixtures/adapters/echo`（每行一个单元，`#!` 行锁定，`{{name}}` 受保护，仅测试用）；
  `tests/test_format_adapter.py` 测清单、宿主和描述符，`tests/test_adapter_runner.py` 用假引擎测执行器，
  末尾一项经真实路由跑到结束。
- **随应用提供的「文本」**（`adapters/text/`，只用标准库）：`.txt` 按空行分段（超过 1500 字的段按行分成几个单元）；
  `.md` 的 YAML 头、代码块（围栏和缩进）、HTML 块、表格分隔行、链接定义锁定，标题、列表项、表格单元格、
  脚注各成一个单元（单元里不含 `#`、列表符号），行内代码、链接地址、网址、邮箱受保护。读入时识别
  UTF-8（带不带 BOM）、UTF-16 和 GB18030，写回保持原换行符与 BOM（GB18030 写成 UTF-8 带 BOM）。
  双语对照把译文放在原文下一行：段落和引用另起一段；列表项和脚注紧挨原文、用行末「\」硬换行，不空行，紧凑列表保持紧凑（原文本身以「\」结尾时改用两个空格）；表格单元格和标题内用「原文 / 译文」。`tests/test_text_adapter.py`
  对 `samples/` 做往返测试，并用假引擎经执行器跑通。

### 安装与子进程宿主

用户也可以自己添加格式适配器（界面上叫「扩展」）：在「扩展」页选一个 zip 或文件夹，或从扩展列表安装，
或用命令行 `adapter install`。作者说明见 [EXTENSIONS.md](EXTENSIONS.md)。

- **安装前检查**（`core/adapter_install.py` 的 `inspect_package` / `preview`，什么都不改）：zip 先解到临时目录
  （拒绝穿越路径和符号链接，zip 上限 32 MB）；读清单；`id` 不得与随应用提供的类型相同；只允许 `.py`、`.json`、
  `.md`、`.txt` 和 `LICENSE`（`samples/` 下不限），拒绝原生库和可执行文件；合计 ≤ 2 MB、≤ 200 个文件；静态扫描
  全部 `.py` 的 import 和调用，联网、启动其他程序、加载原生代码、动态执行代码的写法一律拒绝（`FORBIDDEN_MODULES`、
  `FORBIDDEN_BUILTINS`、`os.system` 等）；`adapter_api_version` 和 `min_app_version` 须兼容。没写许可证、没有样例只是告警。
  `preview` 返回确认框要显示的内容和 `sha256`（zip 是文件哈希，文件夹是按文件名和内容算的树哈希）。
  这些检查只拦明显越界的写法，**不是沙箱**。
- **安装**（`install(confirmed=True, expected_sha256=…)`）：须先确认；文件在 `preview` 之后变了就拒绝。复制到
  `<应用数据>/plugins/adapters/<id>/<版本>-<随机串>/`（先复制到同一目录下的临时文件夹、核对哈希再改名换上，
  登记成功后删掉旧版本的目录；`samples/` 不复制），登记在 `plugins/adapters/installed.json`
  （id、版本、三语名称、目录、来源、`origin`（local / registry）、哈希、大小、安装时间）。同一 id 再装一次就是
  更新或重装；类型还有任务在进行或已暂停时拒绝更新和卸载（`api/extension_install_job.py` 的 `_busy_check`）。
  卸载删目录和登记项，保留 `settings.extensions[<id>]`，重装后恢复。
- **注册**：`api/extension_install_job.py` 的 `activate` 把装好的扩展注册成 `AdapterSurface(origin="installed")`，
  立即出现在 `GET /api/surfaces`；应用启动时 `register_installed_adapters()` 逐个注册，坏的跳过并写日志，
  `GET /api/extensions/installed` 带 `ok: false` 和原因列出来。第一次装上的类型写
  `translation_types.visible["ext:<id>"] = true`，用户拨过它的开关时不动。
- **子进程宿主**（`core/adapter_subprocess_host.py` 的 `SubprocessAdapterHost`，与 `InProcessAdapterHost` 同一个接口，
  只有 `make_host` 一处决定用它）：每次 `scan` / `extract` / `write` 起一个
  `translator-sidecar --adapter-host <目录> <动作>`（源码运行时是 `scripts/launch_sidecar.py`），请求 JSON 走 stdin，
  结果是 stdout 上带 `RESULT_PREFIX` 的一行 JSON；适配器自己 `print` 的内容转到 stderr，写进日志（「[扩展 名称]」）。
  时限：`scan` 30 秒，`extract`、`write` 每个文件 120 秒，超时杀进程。异常、非零退出、超时都只让这个文件失败，
  原因写进任务结果（如「扩展进程异常退出（代码 7）」）。子进程的环境变量只留 `PATH`、语言和 Windows 系统变量，
  没有应用数据目录和任何密钥；工作目录和 `TMPDIR` 是用完即删的临时目录；Windows 上不弹控制台窗口。
  这是崩溃隔离：子进程仍以用户身份运行，能读写用户的文件。翻译引擎的安装包收了几乎全部标准库
  （`packaging/sidecar/translator_sidecar.spec`），扩展能 import 常见的标准库模块；`scripts/frozen_smoke.py`
  在打包产物上跑一遍子进程宿主和 `adapter new / test / pack`。
- **接口**（`api/routes/extensions.py`）：`GET installed`、`POST preview {path}`、`POST install {path, sha256, confirmed}`、
  `GET install-progress`、`POST cancel-install`、`POST {id}/uninstall`，以及下文扩展列表的三个。安装在后台
  （`ExtensionInstallJob`，同一时间只装一个），界面和命令行轮询 `install-progress`，`result` 是登记项。

### 扩展列表

审核过的扩展列在 [XL_Translator-extensions](https://github.com/KlaraGraff/XL_Translator-extensions)：每个扩展一个
`extensions/<id>.json`（字段见那里的 `schema/entry.schema.json`），合并到 main 后工作流重建 `index.json`。
扩展包（zip）放在作者自己仓库的 Release 里；随应用提供的「文本」作为写法示例也列在里面（`packages/` 下）。

- **客户端**（`core/adapter_registry.py`）：`fetch_index` 先取
  `https://cdn.jsdelivr.net/gh/KlaraGraff/XL_Translator-extensions@main/index.json`；它失败或 3 秒内没有
  结果（`FALLBACK_DELAY_SECONDS`）就同时取 raw.githubusercontent.com 上的同一文件，先拿到的为准，只走 HTTPS。结果缓存在 `<应用数据>/cache/extensions-index.json`，
  24 小时内不联网（「刷新」强制联网）；联网失败而有缓存时返回缓存并带 `stale: true`；都没有时返回空列表和
  一句原因，不抛错。不合规的条目跳过，只列 `status` 为 `official` / `reviewed` 的。每个条目另带 `state`：
  `available`、`installed`、`update`、`incompatible`（原因在 `incompatible_reason`）、`bundled`（随应用提供，
  不能再装一份）。
- **从列表安装**（`install_from_registry`）：按条目的 `download` 地址依次下载到
  `<应用数据>/cache/extension-downloads/`（`core/component_download.py`，大小和 SHA-256 必须与条目一致；
  一个地址 15 秒没有新数据就换下一个），
  再走与本地添加相同的 `preview`（id、版本须与条目一致）和安装。确认在前端，与本地添加是同一个确认框。
- **接口**：`GET /api/extensions/registry`、`POST /api/extensions/registry/refresh`、
  `POST /api/extensions/registry/install {id, confirmed}`（与本地安装共用 `install-progress`）。
- **扩展列表这一侧**：PR 上的检查（`scripts/check_entries.py`）下载每个地址、核对大小和 SHA-256，用 Translator
  main 的 `python -m cli adapter test <zip> --json` 做与安装相同的检查并在样例上试运行，再核对 id、版本、扩展名；
  维护者阅读源码后合并并填写审核信息。作者那边的 `scripts/pack.py`（模板仓库，与 `cli/adapter_template/` 逐字节一致，
  由 `tests/test_cli_adapter.py` 钉住）打出 zip、`.sha256` 和条目。

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
- **MCP**（`cli/mcp_server.py`，官方 `mcp` SDK 的 `MCPServer`，只有 stdio）：`translator-sidecar mcp`
  把命令行的动作做成七个工具：`list_types`、`scan_sources`、`start_translation`、`get_task`、
  `wait_task`（最多等 `timeout_seconds`，默认 50 秒，没结束就再调用）、`stop_task`、
  `get_task_result`（输出文件、复核数和前 50 处复核位置）。全部经 `cli/flow.py` 和
  `EngineClient`，不另写逻辑。引擎在第一次需要时才连接或拉起（列工具不起引擎），父进程是 MCP 进程，
  会话结束引擎随之退出；连着的应用关掉后，只读工具重连一次，开始任务不重发。`start_translation`
  遇到需要确认的拦截不自动接受：返回 `needs_confirmation`、`message` 和 `accept`，智能体问过用户后带上
  `accept` 再调一次，或在用户事先同意时传 `accept_all`。服务期间 SDK 把文件描述符 1 指到 stderr，
  stdout 只走协议。
- **测试**：`tests/fixtures/cli_engine.py` 是注册了 `ext:sample`、用回声引擎代替模型的真实
  `api.launcher`，`tests/test_cli.py` 和 `tests/test_mcp_server.py` 用它充当已打开的应用走通各子命令和
  七个工具；冻结冒烟（`scripts/frozen_smoke.py`）在冻结包里跑一次 `types`，再用 SDK 客户端拉起
  `mcp`、列出七个工具并调一次 `list_types`。跑批测试用
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
