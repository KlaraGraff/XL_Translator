# Testing Assets

这个目录放的是长期保留、可复用的测试资产。以后别人在仓库里找“Agent 应该怎么自测”时，除了看根目录 `AGENTS.md`，就直接看这里。

## 文件说明

- `Run-IsolatedVenvPython.ps1`
  - 用项目 `.venv` 启动一个隔离好的 Python 测试进程。
  - 自动隔离 `HOME` / `USERPROFILE` / `TEMP` / `TMP`。
  - 自动建立 `.runtime/self-tests/<task-slug>/artifacts`。
  - 自动把项目根目录注入 `PYTHONPATH`，让 `.runtime` 下的测试脚本也能直接导入项目模块。

- `wrappers/page_translate_render_app.py`
  - 翻译主页的 Streamlit AppTest 包装器。

- `wrappers/page_tm_render_app.py`
  - TM 管理页的 Streamlit AppTest 包装器。

- `wrappers/app_main_render_app.py`
  - 完整应用壳的 AppTest 包装器。

- `wrappers/page_translate_visual_app.py`
  - 翻译页的视觉检查包装器（带全局 CSS 与侧边栏外壳）。

- `wrappers/page_tm_visual_app.py`
  - TM 页的视觉检查包装器（带全局 CSS 与侧边栏外壳）。

## 推荐用法

1. 先在 `.runtime/self-tests/<task-slug>/` 下写一次性验证脚本。
2. 再用下面的命令在隔离环境里执行：

```powershell
powershell -ExecutionPolicy Bypass -File ./agent/testing/Run-IsolatedVenvPython.ps1 `
  -TaskSlug tm-import-header `
  -ScriptPath .runtime/self-tests/tm-import-header/check_tm_import.py
```

3. 如果要测页面，脚本里优先通过 `AppTest.from_file(...)` 加载 `page_*_render_app.py`；只有在需要完整壳或视觉检查时，再使用其他包装器。

## 复用到新项目时怎么搬

最简单的方式不是一个文件一个文件地找，而是直接复制：

1. 根目录 `AGENTS.md`
2. 整个 `agent/` 目录

搬过去后只需要改两类内容：

1. `quality_gate.ps1`
   - 改成新项目自己的静态检查命令。
2. `wrappers/*.py`
   - 把当前项目里的 `render_page(...)` 导入改成新项目页面的入口。

如果新项目也是 Streamlit，多数情况下直接复制一个包装器，再改两行 import 就能复用。
