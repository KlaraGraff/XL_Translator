【macOS 用户怎么做】
1) 新版只支持 macOS 12.0 Monterey 及以上。从 GitHub Release 下载与本机芯片对应的原生 DMG：Apple Silicon 使用 `Translator_macOS_arm64_<版本>.dmg`，Intel Mac 使用 `Translator_macOS_x64_<版本>.dmg`。
2) 下载同名 `.sha256` 文件，在终端运行 `shasum -a 256 -c <DMG 文件名>.sha256`，确认结果为 `OK` 后再安装。
3) 打开 DMG，将 `Translator.app` 拖入 Applications，然后从“应用程序”启动。
4) 正式 Release 使用 Developer ID 签名和 Apple 公证。若 Gatekeeper 因本机策略阻止打开，请在“系统偏好设置/系统设置 -> 隐私与安全性”中确认“仍要打开”。
5) 首次启动只显示快速开始，不会读取、导入、迁移、修复或删除旧版本数据。源码开发请在仓库根目录执行 `cd src-tauri && ../ui/node_modules/.bin/tauri dev`；这需要 Node、Rust 与项目 `.venv`。

新版不提供 Windows 安装包、Windows 更新或 Windows 开发入口。Windows 用户请继续使用旧版本。

【如果失败怎么办】
1) 保留终端或安装器报错截图。
2) 从应用内导出诊断归档。诊断不会包含 API Key、原文、译文、完整 Prompt、文件名或绝对路径。
3) 把截图或归档发给维护者协助排查。

【不要做什么】
1) 不要直接删除 `.venv`，除非准备重新初始化开发环境。
2) 源码目录不要在压缩包内直接运行，必须先解压。
3) 不要直接运行旧的 PySide6 启动脚本；当前版本使用 Tauri 原生窗口。
