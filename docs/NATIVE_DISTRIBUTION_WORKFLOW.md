# Native Distribution Workflow

当前主线只维护 PySide6 原生桌面应用。

## 入口

- 开发/源码启动：`scripts/launch_native.py`
- macOS 源码包入口：`启动应用.command`
- Windows 源码包入口：`启动应用.bat`
- macOS 打包配置：`packaging/macos/app_macos.spec`
- Windows 打包配置：`packaging/windows/app_windows.spec`

`启动原生应用.command` 保留为 macOS 明确命名入口；`启动应用.command`
现在也是同一条原生启动线路。

## 源码包

源码包由 `scripts/build_distribution.py` 生成，包含：

- `assets/`
- `core/`
- `engines/`
- `native_app/`
- `scripts/`
- 根目录启动脚本、配置和说明文件

源码包不再包含网页页面目录、WebView 启动器或 Streamlit 配置。

## 运行依赖

运行依赖以 `requirements.txt` 为准。主线 UI 依赖为
`PySide6-Essentials`，不再安装 Streamlit、streamlit-extras 或 pywebview。

## 验证

每次改动后至少运行：

```powershell
powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1
```

涉及原生界面的改动，还应补充 PySide6/offscreen 动态测试，验证页面默认值、
表格、按钮状态和关键交互。
