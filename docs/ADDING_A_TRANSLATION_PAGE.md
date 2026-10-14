# 新增一个翻译板块

侧栏「翻译」下面的每一页（Excel、Word、PDF 与图片、CAD 图纸）都由同一个模板
`ui/src/views/workspace.ts` 生成。这几页只在几张按板块分开的表里有区别，布局、文案和
流程都一样。

为什么要这样做：以前每加一页都是手写一套界面，写着写着就和已有的几页不一样了。比如
来源栏少了「全选」、完成横幅关不掉、选完文件不会自动扫描。CAD 页就是这样走偏的。
现在的规则是：**新板块只能作为 `Surface` 加进模板，不另写页面。**
`tests/test_workspace_surface_contracts.py` 会检查这条规则，违反就会报错。

本文讲的是**随应用发布的内置板块**。不随应用发布、在运行时注册的文件类型（键为 `ext:<id>`）
不走下面的步骤 1、2，前端不为它写任何代码，见文末「类型描述符与通用路径」。

## 一个板块包含什么

所有板块的页面骨架都一样，从上到下依次是：

1. 来源栏：浏览文件或文件夹。选好后**自动扫描**，没有单独的「扫描」按钮。
2. 统计行：文件数，以及该板块自己的几个统计数字。
3. 任务清单：文件表，支持全选，显示「已选 N/M」。
4. 运行设置：语言、开关、更多设置。
5. 底栏：开始、当前文件完成后暂停（支持的板块才有）、安全停止。
6. 运行中：进度卡片、逐文件状态、日志。
7. 结束后：完成横幅，带「打开输出目录」，停止后还有「从断点继续」。

新板块在这些位置放同样的东西，用同样的说法。

## 步骤

### 1. 前端：把板块加进 `BuiltinSurface`

在 `ui/src/views/workspace.ts` 里：

```ts
export type BuiltinSurface = "excel" | "word" | "pdf" | "cad" | "新板块";
```

然后运行 `cd ui && npx tsc --noEmit`。所有 `Record<BuiltinSurface, …>` 表都会因为缺少新键而报错，
照着报错逐个补上就行：

| 表 | 读取它的访问函数 | 作用 |
|---|---|---|
| `SURFACE_LABEL` / `SURFACE_ICON` / `SURFACE_SHORT_NAME` / `SURFACE_PAGE_TITLE` | `surfaceLabel` / `surfaceIcon` / `surfaceShortName` / `surfacePageTitle` | 标签、图标、任务名里的简称、页标题 |
| `SURFACE_FILE_NOUN` | `surfaceFileNoun` | 文件叫什么（「表格文件」「图纸」……），各处文案都会用到 |
| `SURFACE_EMPTY_SUBTITLE` / `SURFACE_FIRST_EMPTY` / `SURFACE_BANNER_EMPTY` | `surfaceEmptySubtitle` / `surfaceFirstEmpty` / `surfaceBannerEmpty` | 空状态文案 |
| `TOGGLES` | `surfaceToggles` | 运行设置里的开关列表 |

这些表只在各自的访问函数里读一次，别处一律调访问函数（契约测试会数直接下标的次数）：访问函数对
内置板块查表，对 `ext:` 键按描述符取，所以模板里同一处代码对两种板块都成立。另外
`ui/src/task-state-labels.ts` 的 `STOP_CONFIRM_BODY` 是停止确认框的说明，不填就用通用的一句。

每个板块一份的运行时状态（`stateOf(surface)` 取到的 `SurfaceState`、请求序号、扫描中标记、
计时器、停止确认框）都是按需创建的 `Map`，新板块不用填初始值。

**不要**把这些表改成 `Partial<Record<…>>` 来省事。那样漏填的表不会报错，界面上
只会悄悄少一块，契约测试也会拒绝这种写法。

确实只有这个板块才有的东西，比如文件表的某一列、某个统计数字、提交参数，写成
`if (surface === "新板块")` 分支，放在对应的构建函数里，可以参考 `surface === "cad"` 的写法。
如果某个分支开始复制已有逻辑，说明它应该变成表里的一项，或者变成一个共享函数。

### 2. 前端：视图文件只是一层封装

新建 `ui/src/views/新板块.ts`，内容和 `cad.ts` 一样，只调用 `mountWorkspace(container, params, "新板块")`。
不要写样式，也不要新建 `.css` 文件。契约测试要求视图文件不超过 12 行代码、不引入样式。

还要登记到下面这些地方：

- `ui/src/shell.ts`：`TRANSLATE_ITEMS` 加一项，`id` 与 `Surface` 一致。
- `ui/src/router.ts`：`BuiltinViewId` 和 `VIEW_IDS`。
- `ui/src/app.ts`：`registerView`。
- `ui/src/icons.ts`：如果需要新图标。
- `ui/src/api-client.ts`：任务的 `surface` 联合类型。
- `ui/src/views/tasks.ts`：`SURFACE_ICON`、`SURFACE_TASK_LABEL`。如果支持暂停，
  还要加进 `PAUSABLE_SURFACES`。任务中心靠这些表认出新板块的任务。

### 3. 优先用共享组件

需要某个界面元素时，先在下面几个文件里找，没有合适的再考虑新写：

- `ui/src/components.ts`：卡片、按钮、开关行、下拉框、语言选择、折叠区、进度条、
  横幅、空状态、弹窗、提示。
- `ui/src/task-panels.ts`：任务进度、日志、任务清单、底栏、完成横幅、更多设置。
- `ui/src/file-progress.ts`：逐文件状态的文案和颜色。
- `ui/src/task-state-labels.ts`：任务状态的用词。

颜色、间距、字号一律用 `ui/src/styles/tokens.css` 里的变量，不要写死数值。

### 4. 后端

后端也按「注册表」组织：任务管理器不认识具体页面，只向板块对象提问。整体结构见
[ARCHITECTURE.md](ARCHITECTURE.md)。新板块要做四件事：

**① 写执行器** `core/新板块_task_runner.py`

```python
from core.runner_base import BaseTaskRunner
from core.task_runner_messages import DoneMsg, ErrorMsg, ProgressMsg, StoppedMsg

class 新板块TaskRunner(BaseTaskRunner):
    _thread_name = "新板块-translation"

    def __init__(self, files, settings, ...):
        self._init_runner_shell()      # 队列、线程、停止信号
        ...

    def _run(self) -> None:            # 在后台线程里跑
        ...
```

- 线程、消息队列、`stop()`、`needs_poll()`、`get_message()` 都由 `BaseTaskRunner` 提供，
  只写 `_run()`。
- 只通过 `self._queue.put(...)` 汇报，消息类型都在 `core/task_runner_messages.py`：
  `ProgressMsg`（带 `phase_index` / `phase_total`）、`StatusMsg`、`LogMsg`（用 `self._log()`）；
  逐文件状态用 `core/file_progress.py`（参考已有执行器里的 `self._file_progress.emit(...)`）。
- 最后**必须**以 `DoneMsg`、`StoppedMsg`、`ErrorMsg` 之一结束，否则任务会一直显示运行中。
- 在循环和每次调模型前检查 `self._stop_event.is_set()`；已经拿到的译文照常写出（见架构说明里的
  「停止不丢已付费结果」）。
- `_run()` 只做编排，每个阶段写成一个方法，参数用关键字参数，参考 Excel / Word 执行器。
- 要支持暂停，就实现 `pause()`、`resume()`，需要时再加 `end_paused()`。

**② 写板块声明** `api/surfaces/新板块.py`，参考 `cad.py`：

| 必填 | 说明 |
|---|---|
| `key` | 与前端 `Surface` 的键一致 |
| `label` | 任务历史和调度里显示的名字 |
| `page_key` | 决定任务占用哪类模型；新值要在 `core/model_api_identity.task_model_roles_for_page` 里登记 |
| `scan()` | 列出来源下可翻译的文件（任务启动时用） |
| `scan_sources()` | 给 `/api/sources/scan` 的文件清单；结构与其他板块不同时改写 `scan_response()` |
| `validate_preflight()` | 开始前的检查，不合格抛 `TaskInputError`；常用 `require_target_lang()`、`require_translation_model()` |
| `build_runner()` | 用 `RunnerRequest` 构建 ① 的执行器 |

按需覆盖的类属性和方法：`phase_weights`（各阶段占进度条的比例，不填按等分）、`can_pause` /
`pause_mode` / `can_resume` / `can_end_paused`、`uses_translation_memory`、
`target_lang_setting`、`snapshot()`、`fingerprint_extras()`、`owner_label()`。
每一项的含义写在 `api/surfaces/base.py` 的注释里。

**③ 登记**：在 `api/surfaces/__init__.py` 的 `_SURFACES` 里加一个实例。扫描和启动请求能接受的
`surface` 值、任务名称、进度权重、暂停按钮都从这里取，不用再改 `api/app.py` 或 `api/task_manager.py`。

**④ 板块自己的运行选项**：在 `api/task_contracts.py` 的 `TaskOptions` 和 `api/schemas.py` 的
`TaskStartRequest` 里各加一个**同名**、带默认值的字段，路由会按名字自动带过去。
新的持久化设置项放 `settings.py`，也必须带默认值，让旧的 `settings.json` 不用迁移就能读
（见 `AGENTS.md` 旧数据兼容规则）。

`tests/test_surface_registry_contracts.py` 会核对前后端是否对得上：前端 `BuiltinSurface` 与后端的内置注册表、
任务中心的可暂停列表与 `can_pause`、必填钩子是否都实现、`page_key` 是否登记了模型角色、
`TaskOptions` 字段是否都有同名请求字段。漏了哪一处，报错会直接点名。

### 5. 自测

- `cd ui && npx tsc --noEmit && npm run build`
- `.venv/bin/ruff check .`
- `.venv/bin/python -m pytest tests/test_workspace_surface_contracts.py tests/test_surface_registry_contracts.py`
- 在浏览器或 Tauri 壳里把完整流程走一遍，和已有板块逐项对照：选择来源、自动扫描、
  开始、暂停或停止、继续、完成横幅、任务中心。

## 类型描述符与通用路径

不随应用发布的文件类型（以后的格式适配器）不按上面的步骤加，而是在运行时注册，前端不为它写
任何代码。整体说明见 [ARCHITECTURE.md](ARCHITECTURE.md) 的同名一节，这里只列和内置板块的区别：

| | 内置板块 | 运行时注册的类型 |
|---|---|---|
| 键 | `"excel"` 这样的固定名 | `ext:<id>` |
| 后端登记 | `api/surfaces/__init__.py` 的 `_SURFACES` | `register_surface(surface)`，`builtin = False` |
| 名称、图标、文件名词、开关 | 前端的表（`SURFACE_LABEL`、`TOGGLES` …） | `Surface` 的 `display_label`、`icon`、`file_noun`、`options`，经 `GET /api/surfaces` 给前端 |
| 侧栏、路由、任务中心 | `TRANSLATE_ITEMS`、`registerView`、`tasks.ts` 的表 | 描述符到达后自动注册视图、放进侧栏（`settings.extensions[<id>].pinned` / `order`），任务中心按描述符显示 |
| 语言、补充要求等设置 | `settings.py` 上的平铺字段 | `settings.extensions[<id>]`，经 `core/surface_settings.py` 读写 |
| 开关值 | `TaskOptions` 上的同名字段 | 请求的 `options` 字典 |
| 统计格、文件表列 | 板块自己的 `if (surface === "…")` 分支 | 通用的一套（文件数、总大小、跳过项；文件、格式、大小、状态） |
| 只需「抽文字 → 翻译 → 写回」时 | — | 执行器用 `core/text_translation_service.py` 的 `TextTranslationService` |

一个运行时类型最少要写：`key`、`builtin = False`、`label`、`page_key = "adapter"`、`display_label`、
`file_noun`、`file_extensions`、`scan()` / `scan_sources()`、`validate_preflight()`、`build_runner()`；
其余按需（`options`、`can_pause` 等）。可运行的例子是 `tests/fixtures/sample_surface.py`
（仅测试用，键 `ext:sample`），`tests/test_runtime_surface.py` 演示了从注册到任务完成的整条路径。

想在浏览器里看它：起一个注册了样例类型的侧车，例如在一个隔离数据目录下运行

```python
from tests.fixtures.sample_surface import register_sample_surface
register_sample_surface()          # 在 create_app() 之前
```

再按 `ui/src/dev-tauri-shim.ts` 顶部的说明让 `vite` 连上这个侧车。侧栏「翻译」组会多出「示例文本」。
