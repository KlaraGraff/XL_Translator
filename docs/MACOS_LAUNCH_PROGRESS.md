# macOS 启动改造中间状态

最后更新：2026-04-09

## 1. 本次确认后的目标

- 已确认范围：`2 + .command`
- 当前改造方向已经收敛为 macOS 单平台。
- 本次只处理 macOS 启动链路与分发入口，不扩展到整套业务功能的 macOS 兼容。
- 目标效果：
  - 首次启动显性初始化
  - 后续启动尽量静默
  - 自动清理旧实例
  - 自动选取可用端口
  - 健康检查通过后再打开浏览器
  - 静默模式日志落到 `.runtime/launcher.log`

## 2. 当前收敛后的方向

- 当前保留的根入口是 `启动应用.command`。
- 当前显性启动脚本是 `scripts/start_macos.command`。
- 当前静默辅助脚本是 `scripts/launch_silent_macos.sh`。
- 当前核心启动逻辑在 `scripts/launcher.py`。
- 当前分发脚本 `scripts/build_distribution.py` 已按 macOS 单平台产物组织。

## 3. 已识别的关键风险

- 本次交付不等于“整个应用已完全 macOS 可用”，而是“当前仓库已收敛为 macOS 单平台启动与分发”。
- Excel 深度处理仍依赖 `xlwings` 与本机 Excel。
- `.command` 文件在 macOS 上通常依赖可执行权限；分发 zip 仍需保证权限位正确写入。

## 4. 当前实施计划

1. 新增中间状态文档并持续更新
2. 新增 macOS 根入口与脚本：
   - `启动应用.command`
   - `scripts/start_macos.command`
   - `scripts/launch_silent_macos.sh`
3. 改造 `scripts/launcher.py`：
   - 使用本机 `python3` 完成首启 bootstrap
   - 支持 macOS 的旧实例探测与结束
   - 去掉 Windows runtime 依赖
4. 改造 `scripts/build_distribution.py`：
   - 仅保留 macOS 启动入口
   - zip 时为 `.command` / `.sh` 写入可执行权限
5. 更新用户文档与分发文档
6. 安装并补齐 `powershell` 命令链路
7. 跑 `quality_gate.ps1`
8. 跑至少 1 个与本次改动直接相关的动态测试

## 5. 进度记录

- [x] 需求范围确认完成
- [x] 现有 Windows 启动链路梳理完成
- [x] macOS 脚本创建完成
- [x] `launcher.py` 跨平台改造完成
- [x] 分发脚本更新完成
- [x] 文档更新完成
- [x] `powershell` 命令链路已补齐
- [x] 静态检查已通过
- [x] 动态测试已通过

## 6. 下一步

- 做最终交付说明，明确这次已覆盖的启动能力、已执行测试与仍保留的业务前提。

## 7. 已执行验证

- 已安装 PowerShell 7.6.0，并补齐 `powershell` 命令入口
- 已运行 `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1`
- 已运行 `macos-launcher-smoke` 动态测试
- 已运行 `startup-visible-silent` 动态测试
- 已运行 `macos-user-runtime-bridge` 动态测试
- 已运行 `distribution-package-audit` 动态测试
- 已运行 `doc-code-alignment` 动态测试
- 动态测试已验证：
  - macOS 首启 python / `.venv` 路径解析
  - `.command` / `.sh` zip 权限位写入
  - 分发脚本仅复制 macOS 启动入口
  - 显性/静默启动分流
  - 解包分发后可自动 bootstrap、启动并通过健康检查
