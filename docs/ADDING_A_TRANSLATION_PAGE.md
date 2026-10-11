# 新增一个翻译板块

侧栏「翻译」下面的每一页（Excel、Word、PDF 与图片、CAD 图纸）都由同一个模板
`ui/src/views/workspace.ts` 生成。这几页只在几张按板块分开的表里有区别，布局、文案和
流程都一样。

为什么要这样做：以前每加一页都是手写一套界面，写着写着就和已有的几页不一样了。比如
来源栏少了「全选」、完成横幅关不掉、选完文件不会自动扫描。CAD 页就是这样走偏的。
现在的规则是：**新板块只能作为 `Surface` 加进模板，不另写页面。**
`tests/test_workspace_surface_contracts.py` 会检查这条规则，违反就会报错。

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

### 1. 前端：把板块加进 `Surface`

在 `ui/src/views/workspace.ts` 里：

```ts
export type Surface = "excel" | "word" | "pdf" | "cad" | "新板块";
```

然后运行 `cd ui && npx tsc --noEmit`。所有 `Record<Surface, …>` 表都会因为缺少新键而报错，
照着报错逐个补上就行：

| 表 | 作用 |
|---|---|
| `SURFACE_LABEL` / `SURFACE_ICON` / `SURFACE_PAGE_TITLE` | 标签、图标、页标题 |
| `SURFACE_FILE_NOUN` | 文件叫什么（「表格文件」「图纸」……），各处文案都会用到 |
| `SURFACE_EMPTY_SUBTITLE` / `SURFACE_FIRST_EMPTY` / `SURFACE_BANNER_EMPTY` | 空状态文案 |
| `TOGGLES` | 运行设置里的开关列表 |
| `STOP_MODAL_BODY` | 停止确认框的说明 |
| `states` / `mountTokens` / `scanTokens` / `scanBusy` 等 | 每个板块一份的运行时状态，按已有板块的写法填初始值 |

**不要**把这些表改成 `Partial<Record<Surface, …>>` 来省事。那样漏填的表不会报错，界面上
只会悄悄少一块，契约测试也会拒绝这种写法。

确实只有这个板块才有的东西，比如文件表的某一列、某个统计数字、提交参数，写成
`if (surface === "新板块")` 分支，放在对应的构建函数里，可以参考 `surface === "cad"` 的写法。
如果某个分支开始复制已有逻辑，说明它应该变成表里的一项，或者变成一个共享函数。

### 2. 前端：视图文件只是一层封装

新建 `ui/src/views/新板块.ts`，内容和 `cad.ts` 一样，只调用 `mountWorkspace(container, params, "新板块")`。
不要写样式，也不要新建 `.css` 文件。契约测试要求视图文件不超过 12 行代码、不引入样式。

还要登记到下面这些地方：

- `ui/src/shell.ts`：`TRANSLATE_ITEMS` 加一项，`id` 与 `Surface` 一致。
- `ui/src/router.ts`：`ViewId` 和 `VIEW_IDS`。
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

- `api/app.py`：扫描和启动请求里的 `surface` 字面量类型都要加上新板块。
- `api/task_manager.py`：
  - `TaskSurface` 和任务类型映射要加上新板块。
  - 如果各阶段耗时差别很大，在 `_PHASE_WEIGHTS` 里加一行权重，进度条才会平稳。
    不加的话按各阶段等权处理。
- 任务运行器要发出和已有板块相同的事件：进度、逐文件状态、日志、结束。前端只认这几种事件。
- 新的设置项必须带默认值，让旧的 `settings.json` 不用迁移就能读（见 `AGENTS.md` 旧数据兼容规则）。

### 5. 自测

- `cd ui && npx tsc --noEmit && npm run build`
- `.venv/bin/ruff check .`
- `.venv/bin/python -m pytest tests/test_workspace_surface_contracts.py`
- 在浏览器或 Tauri 壳里把完整流程走一遍，和已有板块逐项对照：选择来源、自动扫描、
  开始、暂停或停止、继续、完成横幅、任务中心。
