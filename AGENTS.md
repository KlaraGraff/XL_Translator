# Agent Instructions

本项目默认启用“改完先自测再交付”。

先读入口：
- `agent/README.md`
- `agent/SELF_TESTING_PLAYBOOK.md`
- `agent/testing/README.md`

硬性规则：
1. 任何代码改动交付前，必须先运行 `powershell -ExecutionPolicy Bypass -File .\quality_gate.ps1`。
2. 静态检查通过后，必须再执行至少 1 个与本次改动直接相关的动态测试。
3. 一律优先使用 `./.venv/bin/python3`；不要混用系统 Python。首次 bootstrap 仅允许用于创建 `.venv`。
4. 涉及 `settings.json`、`keys.json`、TM 数据库、临时目录或用户目录的测试，必须先做隔离。
5. 动态测试产物统一放到 `.runtime\self-tests\<task-slug>\`。
6. Tauri/vanilla TypeScript 界面改动优先执行 `ui/` TypeScript 构建、`src-tauri/` Rust 检查，并以隔离应用数据启动开发壳验证；必要时补充截图或 DOM 状态断言。
7. 如果无法完成测试，交付前必须明确说明未执行项、阻塞原因和风险范围。

本地 Tauri 界面测试收尾动作：
- 只要本地修改了 `ui/`、`src-tauri/`、Tauri 启动路径或会影响界面状态的代码，完成自测后必须主动关闭旧的 `Translator` / `tauri dev` / `api.launcher` 进程，再用当前源码启动一个新进程。
- macOS 在仓库根目录执行 `cd src-tauri && ../ui/node_modules/.bin/tauri dev`，并设置隔离的 `TRANSLATOR_APP_DATA_DIR`。
- 启动后必须确认新进程 PID、启动时间、启动路径和 sidecar 健康检查，确保不是 `/Applications/Translator.app` 旧安装包或旧内存进程。
- 该动作只属于本地测试交付流程。除非用户明确要求，不要因为记录或执行这个本地测试规则而提交、推送或上传到云端。

复用这套规则到新项目时，优先复制根目录 `AGENTS.md` 和整个 `agent/` 目录，再按新项目实际结构调整 `quality_gate.ps1` 与动态测试脚本。

GitHub 同步规则：
- 本项目远程仓库：`https://github.com/KlaraGraff/XL_Translator`
- 默认分支：`main`
- 不要创建新的 GitHub 仓库；除非用户明确要求，也不要新建无关分支。
- 如果当前目录不是 Git 仓库，先提醒用户确认工作目录，不要自行初始化到别处。
- 完成修改并通过自测后，按顺序执行：`git status`、`git add ...`、`git commit -m "<message>"`、`git push origin main`。
- 推送前确认 `git remote -v` 指向 `https://github.com/KlaraGraff/XL_Translator.git`。
