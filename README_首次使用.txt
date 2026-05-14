【macOS 用户怎么做】
1) 先把压缩包完整解压到本地文件夹。
2) 第一次启动时，请在根目录双击“启动应用.command”。
3) 第一次启动会打开终端窗口并显示初始化过程，因为程序需要创建 `.venv` 并安装依赖；这是正常现象，请等待浏览器自动打开。
4) 首次初始化完成后，后续再双击根目录“启动应用.command”时，会优先尝试静默启动，终端通常只会短暂出现。
5) 若系统首次阻止打开，可在 Finder 中右键“启动应用.command”并选择“打开”。

【Windows 用户怎么做】
1) 如果下载的是 GitHub Release 里的 Windows 可执行包，请先完整解压 zip。
2) 解压后双击“XL Translator.exe”，程序会自动启动本地服务并打开浏览器。
3) 如果下载的是源码式 Windows 包，请双击根目录“启动应用.bat”。这种模式需要本机已有 Python 3.10+，或包内带有 runtime\python。
4) Windows 文件夹、Excel 文件、Word 文件选择窗口会使用系统原生窗口。

【如果失败怎么办】
1) 第一次启动失败时，请保留终端/命令行窗口中的报错信息并截图。
2) 后续静默启动失败时，请优先保留 `.runtime/launcher.log`；Windows 可执行包请保留 `%LOCALAPPDATA%\XL Translator\desktop_launcher.log`。
3) 把截图或日志发给我，我会协助你排查。
4) 如需手动查看完整启动过程，macOS 可运行 `scripts/start_macos.command`，Windows 源码包可运行 `scripts\start_windows.bat`。

【不要做什么】
1) 不要直接删除 `.venv`，除非你准备重新初始化运行环境。
2) 不要在压缩包内直接运行，必须先解压后再启动。
3) 请优先运行 zip 解压后的文件；若直接从其他系统拷贝目录，`.command` 可能缺少执行权限。
