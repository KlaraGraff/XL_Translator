# Testing Assets

这个目录放的是长期保留、可复用的测试资产。以后别人在仓库里找“Agent 应该怎么自测”时，除了看根目录 `AGENTS.md`，就直接看这里。

## 文件说明

- `Run-IsolatedVenvPython.ps1`
  - 用项目 `.venv` 启动一个隔离好的 Python 测试进程。
  - 自动隔离 `HOME` / `USERPROFILE` / `TEMP` / `TMP`。
  - 自动建立 `.runtime/self-tests/<task-slug>/artifacts`。
  - 自动把项目根目录注入 `PYTHONPATH`，让 `.runtime` 下的测试脚本也能直接导入项目模块。

## 推荐用法

1. 先在 `.runtime/self-tests/<task-slug>/` 下写一次性验证脚本。
2. 再用下面的命令在隔离环境里执行：

```powershell
powershell -ExecutionPolicy Bypass -File ./agent/testing/Run-IsolatedVenvPython.ps1 `
  -TaskSlug tm-import-header `
  -ScriptPath .runtime/self-tests/tm-import-header/check_tm_import.py
```

3. 如果要测原生页面，脚本里优先设置 `QT_QPA_PLATFORM=offscreen`，再直接实例化目标 PySide6 页面或主窗口并断言控件状态。

## 复用到新项目时怎么搬

最简单的方式不是一个文件一个文件地找，而是直接复制：

1. 根目录 `AGENTS.md`
2. 整个 `agent/` 目录

搬过去后只需要改两类内容：

1. `quality_gate.ps1`
   - 改成新项目自己的静态检查命令。
2. 动态测试脚本
   - 按新项目 UI 技术栈实例化页面、准备隔离数据，并断言关键控件和核心流程。
