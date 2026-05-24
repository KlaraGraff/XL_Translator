【macOS 用户怎么做】
1) 如果下载的是 GitHub Release 里的 macOS 安装包，请先打开“Translator_macOS_<版本>.dmg”。
2) 将“Translator.app”拖入 Applications 或其他本地文件夹。
3) 双击“Translator.app”，程序会打开原生桌面界面。
4) 若系统首次阻止打开，可在 Finder 中右键“Translator.app”并选择“打开”。
5) 如果你拿到的是源码式 macOS 包，请双击根目录“启动应用.command”；这种模式需要本机已有 Python 3.10+。

【Windows 用户怎么做】
1) 如果下载的是 GitHub Release 里的 Windows 安装包，请直接运行“Translator_Windows_<版本>_Setup.exe”。
2) 安装完成后可从开始菜单或桌面启动“Translator”。
3) 如果你拿到的是源码式 Windows 包，请双击根目录“启动应用.bat”。这种模式需要本机已有 Python 3.10+，或包内带有 runtime\python。
4) Windows 文件夹、Excel 文件、Word 文件选择窗口会使用系统原生窗口。

【如果失败怎么办】
1) 第一次启动失败时，请保留终端/命令行窗口中的报错信息并截图。
2) 源码包启动失败时，请保留终端/命令行窗口中的完整报错信息。
3) 安装版启动失败时，macOS 请保留 `~/Library/Application Support/Translator/desktop_launcher.log`，Windows 请保留 `%LOCALAPPDATA%\Translator\desktop_launcher.log`。
4) 把截图或日志发给我，我会协助你排查。
5) 如需手动查看源码包的完整启动过程，macOS 可运行 `scripts/start_macos.command`，Windows 源码包可运行 `scripts\start_windows.bat`。

【不要做什么】
1) 不要直接删除 `.venv`，除非你准备重新初始化运行环境。
2) 源码包不要在压缩包内直接运行，必须先解压后再启动。
3) macOS 标准发布包不再使用 zip；若直接从其他系统拷贝源码目录，`.command` 可能缺少执行权限。
