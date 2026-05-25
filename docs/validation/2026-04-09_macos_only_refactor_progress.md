# macOS Only Refactor Progress

历史说明：本文记录 2026-04-09 当时的 macOS 单平台收敛过程。V5.0 当前主线已经重新回到 Windows/macOS 双平台原生安装包路线，本文不代表当前发布目标或打包要求。

最后更新：2026-04-09

## 目标

把当前仓库从“Windows 运行时残留 + macOS 启动脚本”的混合状态，收敛为可在 macOS 上启动、自测和分发的单平台版本，并删掉明显过时的 Windows 启动产物与相关依赖假设。

## 已完成

- 已确认原始报错根因：
  - `启动应用.command` / `scripts/start_macos.command` 期待的是 macOS 结构的解释器路径。
  - 仓库原先携带的是 Windows 风格的 `runtime/python` 与 `.venv/Scripts/python.exe`。
- 已改造 macOS 启动链路：
  - `启动应用.command`
  - `scripts/start_macos.command`
  - `scripts/launch_silent_macos.sh`
  - `scripts/launcher.py`
- 已移除对仓库内置 `runtime/python` 的依赖。
- 已把首启 bootstrap 改为：
  - 自动查找本机 `python3`
  - 创建 `.venv`
  - 后续统一使用 `.venv/bin/python3`
- 已将 `scripts/build_distribution.py` 改为只打包 macOS 单平台分发内容。
- 已删除旧 Windows 启动与分发入口：
  - `启动应用.bat`
  - `分发应用.bat`
  - `scripts/start_windows.bat`
  - `scripts/launch_silent_pythonw.ps1`
  - `scripts/启动应用_已验证可运行基线.bat`
  - `docs/DISTRIBUTION_RELEASE_WORKFLOW.md`
  - `docs/WINDOWS_DEPENDENCY_AUDIT.md`
- 已删除旧 Windows 运行时与旧产物目录：
  - `.venv`（旧 Windows 版）
  - `runtime`
  - `dist`
- 已重新创建 macOS `.venv` 并安装 `requirements.txt`。
- 已安装 PowerShell 7.6.0，并补齐 `powershell` 命令入口。
- 已同步修改协作文档，避免后续继续引用 `.venv\Scripts\python.exe`：
  - `AGENTS.md`
  - `agent/SELF_TESTING_PLAYBOOK.md`
  - `agent/testing/README.md`
- 已更新用户文档与说明文档：
  - `README_首次使用.txt`
  - `docs/MACOS_DISTRIBUTION_RELEASE_WORKFLOW.md`
  - `docs/MACOS_LAUNCH_PROGRESS.md`
  - `docs/DB_REPAIR_GUIDE.md`
- 已同步修改或清理启动相关自测：
  - `macos-launcher-smoke`
  - `macos-user-runtime-bridge`
  - `startup-visible-silent`
  - `distribution-package-audit`
  - `doc-code-alignment`
  - 删除 `batch-console-ascii` 的 Windows 专属检查脚本
- 已完成静态检查：
  - `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1`
- 已完成动态测试：
  - `macos-launcher-smoke`
  - `startup-visible-silent`
  - `macos-user-runtime-bridge`
  - `distribution-package-audit`
  - `doc-code-alignment`
- 已生成真实分发包：
  - `dist/XL_Translator_Distribution`
  - `dist/XL_Translator_Distribution_2.1.0.zip`

## 正在进行

- 做最后一轮文档整理与交付说明。
- 保留进度文档，避免后续上下文遗忘。

## 待完成

- 如需继续增强，可选项包括：
  - 进一步清理 `.runtime/self-tests/` 下历史 artifacts
  - 增补一次真实 Finder 双击场景的人工验收
  - 视需要再补一轮非启动层面的 macOS 业务兼容检查

## 关键决策

- 不再在仓库内保留可分发的 `runtime/python`。
- 不再保留 Windows 启动脚本与 Windows 分发工作流文档。
- 首启解释器不再来自仓库 runtime，而是来自本机可用的 `python3`。
- 当前仓库目标是“macOS 单平台可启动与可维护”，不是“继续保持 Windows/macOS 双平台共存”。

## 风险与注意事项

- 本地 Excel 深度处理仍依赖 `xlwings` 和本机 Excel。
- 部分 `.runtime/self-tests/` 下的历史 artifacts 仍可能带有旧 Windows 痕迹；这些属于历史产物，不代表当前源码仍依赖它们。
- 当前已经补齐 `powershell` 命令链路，但它来自 Homebrew 安装的 PowerShell 7.6.0。

## 防睡眠状态

- 已开启 `caffeinate`
- 当前后台 PID：`10463`
- 目的：避免长时间安装依赖、打包与测试时因自动睡眠中断

## 本轮已执行的关键命令

- `python3 -m venv .venv`
- `./.venv/bin/python3 -m pip install --upgrade pip`
- `./.venv/bin/python3 -m pip install -r requirements.txt`
- `/opt/homebrew/bin/brew install powershell`
- `ln -sfn /opt/homebrew/bin/pwsh /opt/homebrew/bin/powershell`
- `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1`
- `./.venv/bin/python3 ./scripts/build_distribution.py --zip --version-zip`

## 当前结论

1. 仓库已从混合的 Windows/macOS 启动状态收敛为 macOS 单平台启动与分发版本。
2. 静态检查已通过，且已完成多项与启动链路直接相关的动态测试。
3. 当前可以进入交付说明阶段；若需要，我可以继续做下一轮更深的 macOS 业务兼容清理。
