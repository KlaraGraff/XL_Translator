# tauri 大版本升级检查单

有些修复依赖的是 tauri（及其插件）的**实现事实**，不是公开契约。升级时这些内部行为随时可能变，变了也不会有编译错误，只会让对应的老问题在退出、更新这类难复现的时刻悄悄回来。

所以：升级 `tauri` 或任何 `tauri-plugin-*` 的**大版本或次版本**时，逐条过一遍这张单子；补丁版本至少跑一次 `cargo test`。以后凡是依赖 tauri 内部行为的修复，都往这里追加一条，写清依赖什么、在哪、怎么验。

上次逐条核对：tauri 2.11.5、tauri-plugin-updater 2.10.1、tauri-plugin-process 2.3.1（2026-10-11）。

## 1. `cleanup_before_exit()` 会清空 app 级资源表并 drop 表内条目

**依赖它的代码**：`src-tauri/src/main.rs` 的 `SidecarShutdownHook`（审计 高-13）。sidecar 的收尾挂在这个钩子的 `Drop` 上。

**为什么要紧**：有两条退出路径绕开事件循环，只调 `AppHandle::cleanup_before_exit()`、不发 `RunEvent::Exit`：

- Windows 装更新：tauri-plugin-updater 的 `on_before_exit` 钩子里调 `cleanup_before_exit()`（插件 `src/lib.rs`），随后 `ShellExecuteW` 拉起 NSIS，再 `std::process::exit(0)`（插件 `src/updater.rs` 的 Windows `install_inner`）。
- macOS 更新后「立即重启」：`AppHandle::restart()` 在主线程上调用时先 `cleanup_before_exit()` 再 exec 新进程（tauri `src/app.rs`）。

这两条路上，资源表被清空时 drop 掉的 `SidecarShutdownHook` 是唯一还会执行的关闭逻辑。哪天 `cleanup_before_exit()` 不再清资源表（或清了但不 drop），sidecar 就会在退出/更新时又被直接杀掉：Windows 上 NSIS 撞上被占用的文件，两个平台上进行中的任务都来不及收尾。

**怎么验**：

1. `cd src-tauri && cargo test`。`cleanup_before_exit_still_drops_app_resources` 用 tauri 的 mock 运行时把这条事实钉住了，变了会直接红。
2. 读新版源码，确认上面两条路径仍然只走 `cleanup_before_exit()` 而不发 `RunEvent::Exit`：
   - tauri `src/app.rs` 的 `restart()` 与 `cleanup_before_exit()`；
   - tauri-plugin-updater `src/lib.rs` 里 `on_before_exit` 的注册，和 `src/updater.rs` Windows 版 `install_inner` 里 `on_before_exit()` → `ShellExecuteW` → `exit(0)` 的顺序。
   如果升级后它们改成了会发 `RunEvent::Exit`，钩子仍然无害（`stop_running_sidecar` 走两次只生效一次），但这一条的说明要跟着改。
3. 实机走一次带任务的模拟更新：开一个 Word 或 PDF 任务跑到一半，触发应用内更新安装。确认 sidecar 收到了收尾请求（macOS 是 SIGTERM，Windows 是 `POST /api/system/shutdown`），任务历史落成「已中断」，Windows 上 NSIS 没有报文件被占用。

## 2. Windows 上 sidecar 只能靠 HTTP 请求优雅收尾

**依赖它的代码**：`src-tauri/src/main.rs` 的 `request_child_termination`（Windows 版）与 `post_shutdown_request`，Python 侧 `api/routes/maintenance.py` 的 `POST /api/system/shutdown` 和 `api/launcher.py` 的 `_shutdown_requester`（issue #1）。

**为什么要紧**：sidecar 是带 `CREATE_NO_WINDOW` 起的，`GenerateConsoleCtrlEvent` 落不到它头上，所以 Windows 不发信号，改发 HTTP 请求。这不依赖 tauri 内部行为，但升级时如果 tauri 换了子进程的启动方式（比如改用 tauri-plugin-shell 的 sidecar 机制），这条路径要重新核对。

**怎么验**：`cargo test` 里的 `the_windows_shutdown_request_reaches_the_sidecar_with_its_token`，以及 `pytest tests/test_api_launcher.py`。Windows 实机：带任务退出，任务历史落「已中断」、输出目录里没有半截文件。
