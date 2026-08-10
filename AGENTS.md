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
8. 打发布标签前，必须先运行 `bash scripts/run_release_env_tests.sh`（热跑约 8 秒）。
   日常改动不强制。构建机是 Python 3.11 且没有装 Microsoft Office，开发机两者
   通常都不同——`.venv` 全绿不等于构建机能过：依赖对象生命周期的写法（典型是
   拿 `id()` 当身份用）在不同版本的内存分配器下表现可以完全相反，而任何真去
   拉起 Excel 的测试在本地有 Office 的机器上会悄悄通过。V9.1.0 就是各栽了一次
   才发现的。
9. 打 tag 后工作流产出的是**草稿 Release**，不是已发布的 Release。正文由人手工
   写好、替换掉那句「整理中」的占位之后，才执行 `gh release edit <tag>
   --draft=false` 正式发布。顺序不许颠倒：草稿对外不可见，写说明写到一半断了也
   不会有人看到半成品；一旦先发布再补正文，中间那段时间挂在 Latest 上的就是一
   句「发布说明整理中」，而且没人保证补得上。V9.2.0 就这样露了 11 分钟。
   不要拿 `docs/CHANGELOG.md` 直接拼——changelog 是写给开发者看的，搬上去就是
   一堆术语。写法：
   - 读者是普通用户。每条一个粗体小标题，跟一两句「原来什么样、现在什么样」。
     连接池、SSE、TM、锚点、DISPIMG 这类词一律换成人话或者干脆不提。
   - 中文在前，英文在后，两边逐条对应。
   - 结尾列下载：每个平台一行文件名，加一句安装提示。
   - 签名和公证状态必须查当次构建的实际结果（macOS 任务里 `Configure Developer
     ID` 那步是 skipped 还是 success），不许照抄上一版。V8.1.0 和 V8.0.1 的旧说明
     写着 `formal-release`，实物其实既没签名也没公证。

本地 Tauri 界面测试收尾动作：
- 只要本地修改了 `ui/`、`src-tauri/`、Tauri 启动路径或会影响界面状态的代码，完成自测后必须主动关闭旧的 `Translator` / `tauri dev` / `api.launcher` 进程，再用当前源码启动一个新进程。
- macOS 在仓库根目录执行 `cd src-tauri && ../ui/node_modules/.bin/tauri dev`，并设置隔离的 `TRANSLATOR_APP_DATA_DIR`。
- 启动后必须确认新进程 PID、启动时间、启动路径和 sidecar 健康检查，确保不是 `/Applications/Translator.app` 旧安装包或旧内存进程。
- 该动作只属于本地测试交付流程。除非用户明确要求，不要因为记录或执行这个本地测试规则而提交、推送或上传到云端。

旧数据兼容规则（自 V9.2.1 起生效）：
- 任何 schema 升版必须能读旧数据：加法改动自动补齐并盖版本号；破坏性改动必须写显式迁移 + 迁移前自动备份。
- 任何情况下都不允许「拒绝写入且不提供出路」。读不动就备份旧文件、新建可用的，并在界面上说清备份在哪，绝不能停在报错不动。
- 发布前必须用上一版的真实数据目录跑一次升级验证。
- 覆盖范围：`settings.json`、`keys.json`、`tm.db`、任务历史，以及后续任何写进 `APP_DATA_DIR` 的持久化数据。
- 此规则取代 V9.2.1 之前「未分发、不写任何旧版兼容」的旧约定。

复用这套规则到新项目时，优先复制根目录 `AGENTS.md` 和整个 `agent/` 目录，再按新项目实际结构调整 `quality_gate.ps1` 与动态测试脚本。

GitHub 同步规则：
- 本项目远程仓库：`https://github.com/KlaraGraff/XL_Translator`
- 默认分支：`main`
- 不要创建新的 GitHub 仓库；除非用户明确要求，也不要新建无关分支。
- 如果当前目录不是 Git 仓库，先提醒用户确认工作目录，不要自行初始化到别处。
- 完成修改并通过自测后，按顺序执行：`git status`、`git add ...`、`git commit -m "<message>"`、`git push origin main`。
- 推送前确认 `git remote -v` 指向 `https://github.com/KlaraGraff/XL_Translator.git`。
