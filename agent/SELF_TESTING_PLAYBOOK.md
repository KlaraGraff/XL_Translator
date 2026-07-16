# Agent Self-Testing Playbook

## 1. 默认工作顺序

任何代码改动完成后，交付前都按这个顺序走：

1. 用项目虚拟环境跑静态检查：

```powershell
powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1
```

2. 再补至少 1 个与改动直接相关的动态测试。
3. 触及用户目录、TM 数据库、`settings.json`、`keys.json`、日志或临时文件时，优先使用隔离运行器。
4. 所有动态测试产物统一写到 `.runtime/self-tests/<task-slug>/`。
5. 交付时说明实际执行内容、覆盖范围，以及未覆盖项和风险。

只跑 `ruff check` 不算完成。

## 2. Python 与环境规则

- 优先使用 `./.venv/bin/python3`。
- 不要把测试数据写回真实用户目录。
- 设置 `HOME`、`USERPROFILE`、`TEMP`、`TMP` 或 `TRANSLATOR_APP_DATA_DIR` 后，再导入项目模块。

## 3. 隔离运行器

```powershell
powershell -ExecutionPolicy Bypass -File ./agent/testing/Run-IsolatedVenvPython.ps1 `
  -TaskSlug api-smoke `
  -ScriptPath .runtime/self-tests/api-smoke/check_api.py
```

运行器会使用项目 `.venv`，隔离用户目录和临时目录，并将产物目录暴露为 `PRODUCT_TRANSLATE_SELF_TEST_ARTIFACTS`。

## 4. Tauri 界面动态测试

当前主线使用 Tauri 2 壳与 vanilla TypeScript。界面改动至少应覆盖：

1. 在 `ui/` 运行 `npm run check && npm run build`。
2. 在 `src-tauri/` 运行 `cargo test && cargo check`。
3. 用隔离的 `TRANSLATOR_APP_DATA_DIR` 启动 `tauri dev`。
4. 验证 sidecar 随机 loopback 健康检查、页面导航、主题、配置面板持久化和相关页面关键状态。

完成后关闭旧的 `Translator`、`tauri dev` 和 `api.launcher` 进程；确认新进程 PID、启动路径和 `/health`，避免用户测到旧安装包或旧内存进程。

## 5. TM / settings / 数据文件相关测试

- `settings.py` 会落盘 `settings.json`。
- `core/tm_manager.py` 会连接 TM 数据库。
- 测试中使用隔离应用数据目录，且在模块导入前完成环境隔离。

## 6. 最低交付标准

每次交付必须说明：

1. 执行的静态检查。
2. 执行的动态测试。
3. 动态测试覆盖的改动点。
4. 未执行项、原因和风险。
