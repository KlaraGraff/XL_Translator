【macOS 用户怎么做】
1) 从 GitHub Release 下载并打开 `Translator_macOS_8.0.0.dmg` 或后续版本。
2) 将 `Translator.app` 拖入 Applications 或其他本地文件夹。
3) 双击应用会打开 Tauri 原生桌面窗口。
4) 若系统首次阻止打开，可在 Finder 中右键应用并选择“打开”。
5) 源码开发请在仓库根目录执行 `cd src-tauri && ../ui/node_modules/.bin/tauri dev`；这需要 Node、Rust 与项目 `.venv`。

【Windows 用户怎么做】
1) 从 GitHub Release 下载并运行 `Translator_Windows_8.0.0_Setup.exe` 或后续版本。
2) 安装完成后从开始菜单或桌面启动 Translator。
3) 源码开发请在 `src-tauri` 下运行 Tauri dev；这需要 Node、Rust 与项目 `.venv`。
4) Excel、Word、PDF 和文件夹选择窗口使用系统原生对话框。

【如果失败怎么办】
1) 保留终端或安装器报错截图。
2) 从应用内导出诊断归档；sidecar 日志位于应用数据目录。
3) 把截图或归档发给维护者协助排查。

【不要做什么】
1) 不要直接删除 `.venv`，除非准备重新初始化开发环境。
2) 源码目录不要在压缩包内直接运行，必须先解压。
3) 不要直接运行旧的 PySide6 启动脚本；当前版本使用 Tauri 原生窗口。
