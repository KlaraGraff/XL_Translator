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

3. 如果要测 Tauri 页面，先用隔离应用数据启动开发壳，再对页面导航、主题、配置面板和关键 DOM 状态做断言。

## 人工测试壳复用当前配置

人工打开开发壳时默认复用当时正式软件的配置（包括 API 配置），每次启动前刷新隔离副本。macOS 可直接运行 `scripts/start_tauri_macos.command`，脚本会自动完成复制。手动启动时使用：

```bash
./.venv/bin/python3 agent/testing/seed_user_config.py --target "$PWD/.runtime/manual-shell-data"
cd src-tauri && TRANSLATOR_APP_DATA_DIR="$PWD/../.runtime/manual-shell-data" ../ui/node_modules/.bin/tauri dev
```

也可用 `--source <目录>` 指定配置来源。脚本只复制 `settings.json`、`keys.json` 和 `key_origins.json`；自动化单元测试仍应自行合成隔离配置，不要调用此脚本。

## 复用到新项目时怎么搬

最简单的方式不是一个文件一个文件地找，而是直接复制：

1. 根目录 `AGENTS.md`
2. 整个 `agent/` 目录

搬过去后只需要改两类内容：

1. `quality_gate.ps1`
   - 改成新项目自己的静态检查命令。
2. 动态测试脚本
   - 按新项目 UI 技术栈实例化页面、准备隔离数据，并断言关键控件和核心流程。
