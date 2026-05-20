# Translator Windows 分发说明

最后更新：2026-05-14

## 目标

Windows 版本与 macOS 版本共用同一套核心代码。翻译逻辑、记忆库、
引擎配置、Word/Excel 处理流程都在主线维护；平台差异只保留在启动、
文件选择、进程管理和打包入口。

## 本地启动入口

- Windows 根目录入口：`启动应用.bat`
- Windows 显性启动脚本：`scripts/start_windows.bat`
- Windows 静默辅助脚本：`scripts/launch_silent_windows.ps1`
- 共用启动器：`scripts/launcher.py`

`scripts/launcher.py` 会按当前系统自动选择：

- macOS: `.venv/bin/python3`、`ps`、`os.kill`
- Windows: `.venv/Scripts/python.exe`、`tasklist`、`taskkill`

## Windows 原生选择窗口

`ui/native_dialogs.py` 现在按平台分流：

- macOS 使用 AppleScript
- Windows 使用 PowerShell + `System.Windows.Forms`

Excel、Word、文件夹选择入口保持同名函数，因此页面代码不需要分叉。

## GitHub Actions 分发

工作流文件：

- `.github/workflows/build-distributions.yml`

触发方式：

- 手动运行 `Build distributions`
- 推送 `v*` tag 时自动构建并上传到 GitHub Release

Windows 构建方式：

1. 在 `windows-latest` runner 安装 Python 3.11
2. 安装 `requirements-build.txt`
3. 运行 `quality_gate.ps1`
4. 运行 `python -m unittest discover -s tests`
5. 生成 `app-icon.ico`
6. 使用 `packaging/windows/app_windows.spec` 构建 one-folder 可执行包
7. 使用 `packaging/windows/app_windows.iss` 构建安装器
8. 输出 `dist/Translator_Windows_<版本>_Setup.exe` 和对应的 SHA256 文件

最终用户直接运行安装器即可。

## 本地源码分发

如果只需要生成源码式 Windows 分发目录，可运行：

```powershell
python scripts/build_distribution.py --platform windows --zip --version-zip
```

这会生成带 `启动应用.bat` 的源码包。该模式适合内部测试或需要用户本机
已有 Python 3.10+ 的场景；面向普通用户优先使用 GitHub Actions 生成的
安装器。

## 维护原则

- 不维护独立 Windows 业务分支。
- 平台差异集中在启动、打包、原生系统能力适配。
- 修改 `core/`、`engines/`、`ui/` 的共用逻辑后，macOS 和 Windows 随主线一起升级。
