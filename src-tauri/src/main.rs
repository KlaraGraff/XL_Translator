#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::{SocketAddr, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{mpsc, Mutex},
    thread,
    time::{Duration, Instant},
};

use serde::Serialize;
use tauri::{ipc::InvokeBody, Manager, Resource, RunEvent, State};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

// First launch is the slow case: Gatekeeper (macOS) and Defender/AV real-time
// scanning (Windows) both scan the entire freshly-extracted PyInstaller onedir
// before letting the sidecar's first instruction execute, and 12s was tight
// enough to trip on ordinary hardware. A failed launch is no longer a silent
// crash (see the `setup` error path below), so trading a longer worst-case
// wait for fewer false "engine failed to start" reports is a clear win.
const SIDECAR_START_TIMEOUT: Duration = Duration::from_secs(30);
const SIDECAR_HEALTH_TIMEOUT: Duration = Duration::from_secs(8);

// ── 退出预算：外层（这里）必须严格包住内层（sidecar 自己的那条链）──────────
//
// sidecar 的收尾预算是**串行叠加**的，不是并行的，三段首尾相接：
//   ① uvicorn 等连接关完                    ≤ GRACEFUL_SHUTDOWN_SECONDS = 10s
//   ② 连接关完之后 lifespan 才调
//      `task_manager.shutdown()`，等运行中的任务走到 terminal ≤ 12s
//   ③ **等完 ② 之后**才轮到 `mark_active_tasks_interrupted()` 和
//      `flush_history()`——历史记录不再卡在「运行中」全靠这一步
// 而这里的超时是从 Rust 发出 SIGTERM 那一刻起算的**总**预算，覆盖 ①+②+③。
//
// 上一版把它写成 12s「对齐」②，这正是缺陷本身：外层截止时间 ≤ 内层截止时间，
// 这场竞争外层必输——SIGKILL 恰好落在 Python 刚要开始 ③ 记账的那一刻，
// headless soffice、`word_translator_temp`、PDF 分页工作区照样残留，历史记录
// 照样卡在「运行中」。所以外层取 ①+②+③ 的和：**包住**，不是对齐。
//
// ①② 的数值是 Python 侧的镜像，两边必须成对改；`the_mirrored_sidecar_budgets_
// still_match_the_python_side` 这个测试会去读那两个文件，改了一侧不改另一侧
// 直接变红。
/// ① 的镜像：api/launcher.py 的 `GRACEFUL_SHUTDOWN_SECONDS`，uvicorn 排空连接的上限。
const SIDECAR_DRAIN_BUDGET_SECS: u64 = 10;
/// ② 的镜像：api/task_manager.py 里 `TranslationTaskManager.shutdown` 的默认
/// `timeout`（launcher 没有传别的值），留给运行中任务自己 unwind 的上限——runner
/// 的 finally 就在这一段里删 LibreOffice profile、`word_translator_temp`、PDF 分页
/// 工作区。
const SIDECAR_TASK_UNWIND_BUDGET_SECS: u64 = 12;
/// ③ 的余量：`mark_active_tasks_interrupted()` + `flush_history()` 落盘。这一段
/// Python 侧没有自己的超时，纯粹是几次文件写入，3s 是宽松估计；它同时也是安全带，
/// 保证内外两个 deadline 不会再次贴到一起。
const SIDECAR_SHUTDOWN_MARGIN_SECS: u64 = 3;
/// 正常退出时留给 sidecar 自己收尾的时间上限 = ① 10 + ② 12 + ③ 3 = 25s。
/// 故意写成加法而不是字面量：谁改了任何一段，总额自己跟着走，不会再退化成「对齐」。
///
/// 这是**最坏情况**上限，不是常态：`begin_shutdown` 在信号处理里就把 SSE 唤醒了，
/// ① 基本不花时间；没有任务在跑时 ② 立刻返回，整个退出是几十毫秒。只有「Word/PDF
/// 任务跑到一半按 Cmd+Q，且某个 runner 正卡在一次云端请求里（单次上限 120s，
/// `begin_shutdown` 只置位、掐不断在途的 httpx 调用）」才会用满。
const SIDECAR_STOP_TIMEOUT: Duration = Duration::from_secs(
    SIDECAR_DRAIN_BUDGET_SECS + SIDECAR_TASK_UNWIND_BUDGET_SECS + SIDECAR_SHUTDOWN_MARGIN_SECS,
);
// 启动失败路径上的等待上限：这时错误对话框还没弹，用户在干等，收尾也没什么可收
// （握手都没完成），所以给一个很短的窗口就上 SIGKILL。
const SIDECAR_ABORT_TIMEOUT: Duration = Duration::from_secs(2);
const SIDECAR_STOP_POLL: Duration = Duration::from_millis(50);

// std 没有暴露「给子进程发信号」的 API，而 libc 早就被链进每个 Unix 上的 Rust
// 二进制里，为这一个调用多加一条直接依赖不划算，所以直接声明它。
// SIGTERM 的值由 POSIX 固定为 15，不随平台变化。
#[cfg(unix)]
extern "C" {
    fn kill(pid: i32, sig: i32) -> i32;
}
#[cfg(unix)]
const SIGTERM: i32 = 15;

// Prevents Windows from allocating a console window for the sidecar, which is
// a console-subsystem executable (see packaging/sidecar/translator_sidecar.spec's
// `console=True`) started from this GUI-subsystem (`windows_subsystem = "windows"`)
// parent. This only suppresses the *window*; it does not affect the piped
// stdout / inherited stderr handles set up in `spawn_sidecar` below, since those
// are wired up via explicit handles in the process's STARTUPINFO regardless of
// whether a console is allocated.
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct SidecarInfo {
    port: u16,
    token: String,
}

struct RunningSidecar {
    child: Child,
    info: SidecarInfo,
}

struct SidecarState(Mutex<Option<RunningSidecar>>);

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct OutputDirectoryInspection {
    state: String,
    path: String,
    message: String,
}

#[tauri::command]
fn sidecar_info(state: State<'_, SidecarState>) -> Result<SidecarInfo, String> {
    state
        .0
        .lock()
        .map_err(|_| "Sidecar state is unavailable.".to_string())?
        .as_ref()
        .map(|sidecar| sidecar.info.clone())
        .ok_or_else(|| "Translator engine sidecar is not running.".to_string())
}

#[tauri::command]
fn inspect_output_directory(path: String) -> OutputDirectoryInspection {
    let supplied = path.trim();
    if supplied.is_empty() {
        return OutputDirectoryInspection {
            state: "empty".to_string(),
            path: String::new(),
            message: "自定义输出目录不能为空。".to_string(),
        };
    }

    let expanded = if supplied == "~" || supplied.starts_with("~/") {
        std::env::var_os("HOME")
            .map(PathBuf::from)
            .map(|home| home.join(supplied.strip_prefix("~/").unwrap_or("")))
            .unwrap_or_else(|| PathBuf::from(supplied))
    } else {
        PathBuf::from(supplied)
    };
    let display_path = expanded.display().to_string();

    match fs::metadata(&expanded) {
        Ok(metadata) if metadata.is_dir() => {
            if metadata.permissions().readonly() {
                OutputDirectoryInspection {
                    state: "blocked".to_string(),
                    path: display_path,
                    message: "该目录没有可用写入权限。".to_string(),
                }
            } else {
                OutputDirectoryInspection {
                    state: "available".to_string(),
                    path: display_path,
                    message: "目录当前可用；任务仍会在其中创建唯一时间戳子目录。".to_string(),
                }
            }
        }
        Ok(_) => OutputDirectoryInspection {
            state: "blocked".to_string(),
            path: display_path,
            message: "输出路径是文件，不能作为目录使用。".to_string(),
        },
        Err(_) => {
            let mut ancestor = expanded.as_path();
            while !ancestor.exists() {
                let Some(parent) = ancestor.parent() else {
                    break;
                };
                ancestor = parent;
            }
            match fs::metadata(ancestor) {
                Ok(metadata) if metadata.is_dir() && !metadata.permissions().readonly() => {
                    OutputDirectoryInspection {
                        state: "will_create".to_string(),
                        path: display_path,
                        message: "目录将在任务启动后创建；当前检查不会产生任何目录。".to_string(),
                    }
                }
                Ok(metadata) if !metadata.is_dir() => OutputDirectoryInspection {
                    state: "blocked".to_string(),
                    path: display_path,
                    message: "上级路径被文件占用，无法创建输出目录。".to_string(),
                },
                _ => OutputDirectoryInspection {
                    state: "blocked".to_string(),
                    path: display_path,
                    message: "无法确认上级目录的写入权限；请更换输出目录。".to_string(),
                },
            }
        }
    }
}

#[tauri::command]
fn open_local_path(path: String, reveal: bool) -> Result<(), String> {
    let supplied = path.trim();
    if supplied.is_empty() {
        return Err("未提供本地路径。".to_string());
    }
    let candidate = PathBuf::from(supplied);
    if !candidate.exists() {
        return Err("引用的输出内容已不存在，可能已被移动或删除。".to_string());
    }

    #[cfg(target_os = "macos")]
    {
        let mut command = Command::new("open");
        if reveal && candidate.is_file() {
            command.arg("-R");
        }
        command
            .arg(candidate)
            .spawn()
            .map(|_| ())
            .map_err(|error| format!("无法打开本地路径：{error}"))
    }

    #[cfg(target_os = "windows")]
    {
        // `explorer /select,<path>` highlights the file in its parent folder;
        // plain `explorer <path>` opens a directory, or launches a file with
        // its associated app (mirroring macOS `open`'s dual behaviour).
        // `explorer.exe` commonly exits non-zero even on success, so only the
        // spawn itself is treated as the success/failure signal.
        let mut command = Command::new("explorer");
        if reveal && candidate.is_file() {
            command.arg(format!("/select,{}", candidate.display()));
        } else {
            command.arg(&candidate);
        }
        command
            .spawn()
            .map(|_| ())
            .map_err(|error| format!("无法打开本地路径：{error}"))
    }

    #[cfg(target_os = "linux")]
    {
        // No universal "reveal in file manager" verb exists across Linux file
        // managers, so fall back to opening the containing directory.
        let target = if reveal && candidate.is_file() {
            candidate
                .parent()
                .map(Path::to_path_buf)
                .unwrap_or(candidate)
        } else {
            candidate
        };
        Command::new("xdg-open")
            .arg(target)
            .spawn()
            .map(|_| ())
            .map_err(|error| format!("无法打开本地路径：{error}"))
    }

    #[cfg(not(any(target_os = "macos", target_os = "windows", target_os = "linux")))]
    {
        let _ = reveal;
        Err("当前操作系统不支持打开本地路径。".to_string())
    }
}

// ---------------------------------------------------------------------------
// 导出落盘
//
// 所有「导出」按钮都走这两个命令，前端先用 plugin-dialog 的 save() 拿到用户选定
// 的路径，再把内容交过来写。
//
// 为什么不能用网页那套 `URL.createObjectURL()` + `<a download>` + `anchor.click()`：
// 那是一次真正的浏览器下载请求，而在 macOS 的 WKWebView 里下载必须由宿主应用
// 实现下载代理（WKDownloadDelegate）来回答「存到哪里」。这个应用从未注册过下载
// 处理器（Tauri 的 `on_download` 全项目零命中），WKWebView 于是把请求静默丢弃：
// 不报错、不弹框、文件哪儿都不落地——正是 9.2.x 之前所有导出按钮点了没反应的原因。
// 改回 `<a download>` 就会把这个 bug 原样带回来。
// ---------------------------------------------------------------------------

/// 原子写：先写同目录下的临时文件，再 rename 到目标路径。
///
/// 同一文件系统内的 rename 是原子的，所以中途失败（磁盘写满、进程被杀、断电）时
/// 用户看到的要么是原来的文件、要么什么都没有，绝不会是一个写了一半、打开就报
/// 损坏的导出文件——记忆库全量备份和诊断包都属于「以为存下来了」代价很大的东西。
fn write_file_atomically(target: &Path, bytes: &[u8]) -> Result<(), String> {
    let file_name = target
        .file_name()
        .ok_or_else(|| "保存路径不是有效的文件名。".to_string())?;
    // 保存框给的一定是绝对路径；`parent()` 为空只可能出现在被手工构造的路径上。
    let parent = target
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .ok_or_else(|| "保存路径缺少上级目录。".to_string())?;

    // 临时文件必须和目标同目录：跨文件系统的 rename 会退化成「复制 + 删除」，
    // 原子性也就没了（macOS 上用户完全可能把导出存到外接盘或网络卷）。
    let mut temp_name = std::ffi::OsString::from(".");
    temp_name.push(file_name);
    temp_name.push(format!(".{}.partial", std::process::id()));
    let temp = parent.join(temp_name);

    let written = (|| -> std::io::Result<()> {
        let mut file = fs::File::create(&temp)?;
        file.write_all(bytes)?;
        // rename 之前先落盘：否则掉电后可能留下一个长度正确、内容是空洞的文件。
        file.sync_all()
    })();
    if let Err(error) = written {
        let _ = fs::remove_file(&temp);
        return Err(format!("写入导出文件失败：{error}"));
    }

    fs::rename(&temp, target).map_err(|error| {
        let _ = fs::remove_file(&temp);
        format!("保存导出文件失败：{error}")
    })
}

/// 文本类导出（JSON / CSV）。内容不大，直接走普通的 JSON 参数即可。
#[tauri::command]
fn save_text_file(path: String, contents: String) -> Result<(), String> {
    let supplied = path.trim();
    if supplied.is_empty() {
        return Err("未提供保存路径。".to_string());
    }
    write_file_atomically(Path::new(supplied), contents.as_bytes())
}

/// 拆开 `save_binary_file` 的请求体：`[u32 小端 路径字节数][UTF-8 路径][文件内容]`。
///
/// 路径没有走 IPC header：header 值是字节串，前端 `new Headers()` 遇到码位大于 255
/// 的字符会直接抛 TypeError，而保存路径带中文是常态（用户存到「桌面」「下载」这类
/// 中文目录，或者自己把文件名改成中文）。塞进同一段字节里就不存在编码问题。
fn split_save_frame(frame: &[u8]) -> Result<(&str, &[u8]), String> {
    const PREFIX: usize = 4;
    let header: [u8; PREFIX] = frame
        .get(..PREFIX)
        .and_then(|slice| slice.try_into().ok())
        .ok_or_else(|| "导出请求体不完整。".to_string())?;
    let path_end = PREFIX
        .checked_add(u32::from_le_bytes(header) as usize)
        .ok_or_else(|| "导出请求体里的路径长度越界。".to_string())?;
    let path_bytes = frame
        .get(PREFIX..path_end)
        .ok_or_else(|| "导出请求体里的路径长度越界。".to_string())?;
    let path = std::str::from_utf8(path_bytes)
        .map_err(|_| "保存路径不是有效的 UTF-8 文本。".to_string())?;
    if path.trim().is_empty() {
        return Err("未提供保存路径。".to_string());
    }
    Ok((path.trim(), &frame[path_end..]))
}

/// 二进制导出（诊断包、任务产物，可能几十 MB）。
///
/// 命令签名接 `tauri::ipc::Request` 而不是 `Vec<u8>`：后者会让字节数组走 JSON
/// 序列化，每个字节膨胀成「十进制数字 + 逗号」，两端还各做一次 JSON 编解码——
/// 那是 Tauri IPC 上最慢的一条路，几十 MB 的诊断包能把界面卡死好几秒。前端直接
/// invoke 一个 ArrayBuffer，body 以 `InvokeBody::Raw` 原样送达，零转码。
#[tauri::command]
fn save_binary_file(request: tauri::ipc::Request<'_>) -> Result<(), String> {
    let InvokeBody::Raw(frame) = request.body() else {
        return Err("二进制导出需要原始字节请求体。".to_string());
    };
    let (path, contents) = split_save_frame(frame)?;
    write_file_atomically(Path::new(path), contents)
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct UpdateEnvironment {
    can_self_update: bool,
    /// Machine-readable cause when `can_self_update` is false; the UI maps it to
    /// user-facing copy and falls back to "download the installer yourself".
    reason: String,
    /// How the install step behaves, which decides what the UI can promise:
    ///
    /// * `in_place` (macOS) -- the plugin swaps the .app bundle and returns. The
    ///   running app is untouched until the user chooses to relaunch, so a
    ///   translation job in flight is safe and the UI can offer "restart later".
    /// * `installer_restart` (Windows) -- the plugin hands off to the NSIS
    ///   installer and terminates this process; the installer reopens the app.
    ///   There is no "installed, awaiting restart" state to show, and any
    ///   running task dies with the process, so the UI must confirm beforehand.
    install_behavior: String,
}

#[cfg(target_os = "windows")]
const INSTALL_BEHAVIOR: &str = "installer_restart";
#[cfg(not(target_os = "windows"))]
const INSTALL_BEHAVIOR: &str = "in_place";

fn supported(reason: &str) -> UpdateEnvironment {
    UpdateEnvironment {
        can_self_update: true,
        reason: reason.to_string(),
        install_behavior: INSTALL_BEHAVIOR.to_string(),
    }
}

fn unsupported(reason: &str) -> UpdateEnvironment {
    UpdateEnvironment {
        can_self_update: false,
        reason: reason.to_string(),
        install_behavior: INSTALL_BEHAVIOR.to_string(),
    }
}

/// Probe a directory for real write access.
///
/// `Permissions::readonly()` only looks at mode bits, which says nothing about
/// ownership -- /Applications is `drwxrwxr-x root:admin`, so a standard (non-admin)
/// user sees "writable" there and would only learn otherwise when the updater
/// fails halfway through replacing the bundle. Creating and immediately removing
/// a dot-file is the only answer that matches what the updater will actually hit.
fn directory_is_writable(directory: &Path) -> bool {
    let probe = directory.join(".translator-update-probe");
    match fs::File::create(&probe) {
        Ok(_) => {
            let _ = fs::remove_file(&probe);
            true
        }
        Err(_) => false,
    }
}

/// Report whether this particular installation can replace itself in place.
///
/// The About page needs this *before* the user clicks "download and install":
/// showing a button that is guaranteed to fail is worse than routing the user to
/// the manual installer up front (mockup screen ⑦).
#[tauri::command]
fn update_environment() -> UpdateEnvironment {
    if cfg!(debug_assertions) {
        // `tauri dev` runs a bare executable, not a bundle -- there is nothing
        // for the updater to swap out.
        return unsupported("dev_build");
    }

    #[cfg(target_os = "macos")]
    {
        if !cfg!(target_arch = "aarch64") {
            return unsupported("unsupported_architecture");
        }
        let Ok(executable) = std::env::current_exe() else {
            return unsupported("install_location_unknown");
        };
        let bundle = executable
            .ancestors()
            .find(|path| path.extension().is_some_and(|ext| ext == "app"));
        let Some(bundle) = bundle else {
            return unsupported("not_a_bundle");
        };
        // A DMG is mounted read-only under /Volumes; the app has to be dragged
        // into Applications before it can update itself.
        if bundle.starts_with("/Volumes/") {
            return unsupported("running_from_dmg");
        }
        let Some(parent) = bundle.parent() else {
            return unsupported("install_location_unknown");
        };
        if !directory_is_writable(parent) {
            return unsupported("install_location_read_only");
        }
        supported("ok")
    }

    #[cfg(target_os = "windows")]
    {
        if !cfg!(target_arch = "x86_64") {
            return unsupported("unsupported_architecture");
        }
        // The NSIS bundle installs per-user (tauri.windows.conf.json's
        // `installMode: "currentUser"`), so the target directory is writable by
        // construction and needs no elevation.
        supported("ok")
    }

    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    {
        unsupported("unsupported_platform")
    }
}

#[tauri::command]
fn open_external_url(url: String) -> Result<(), String> {
    let supplied = url.trim();
    let is_allowed_github_url = [
        "https://github.com/",
        "https://www.github.com/",
        "https://objects.githubusercontent.com/",
        "https://github-releases.githubusercontent.com/",
    ]
    .iter()
    .any(|prefix| supplied.starts_with(prefix));
    if !is_allowed_github_url {
        return Err("只能打开官方 GitHub Release 与支持链接。".to_string());
    }

    #[cfg(target_os = "macos")]
    {
        Command::new("open")
            .arg(supplied)
            .spawn()
            .map(|_| ())
            .map_err(|error| format!("无法打开外部链接：{error}"))
    }

    #[cfg(target_os = "windows")]
    {
        // Routed through `rundll32 url.dll,FileProtocolHandler` rather than
        // `cmd /c start`: it hands the URL to the system default browser
        // without any shell/`cmd.exe` involved, so there is no metacharacter
        // or quoting surface for command injection -- on top of `supplied`
        // already being constrained to the GitHub prefixes checked above.
        Command::new("rundll32")
            .args(["url.dll,FileProtocolHandler", supplied])
            .spawn()
            .map(|_| ())
            .map_err(|error| format!("无法打开外部链接：{error}"))
    }

    #[cfg(target_os = "linux")]
    {
        Command::new("xdg-open")
            .arg(supplied)
            .spawn()
            .map(|_| ())
            .map_err(|error| format!("无法打开外部链接：{error}"))
    }

    #[cfg(not(any(target_os = "macos", target_os = "windows", target_os = "linux")))]
    {
        Err("当前操作系统不支持打开外部链接。".to_string())
    }
}

fn project_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri must have a project-root parent")
        .to_path_buf()
}

fn python_command(root: &Path) -> PathBuf {
    if let Ok(explicit) = std::env::var("TRANSLATOR_SIDECAR_PYTHON") {
        let candidate = PathBuf::from(explicit);
        if candidate.is_file() {
            return candidate;
        }
    }

    let bundled = root.join(".venv").join("bin").join("python3");
    if bundled.is_file() {
        return bundled;
    }
    PathBuf::from("python3")
}

fn bundled_sidecar_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| format!("Could not resolve bundled resources: {error}"))?;
    let executable_name = if cfg!(target_os = "windows") {
        "translator-sidecar.exe"
    } else {
        "translator-sidecar"
    };
    let executable = resource_dir
        .join("sidecar")
        .join("translator-sidecar")
        .join(executable_name);
    if executable.is_file() {
        Ok(executable)
    } else {
        Err(format!(
            "Bundled Translator engine sidecar is missing: {}",
            executable.display()
        ))
    }
}

/// Origin the dev server serves the UI from, read out of tauri.conf.json's `devUrl`.
///
/// This has to match the webview's origin byte for byte: to a browser
/// `http://localhost:1420` and `http://127.0.0.1:1420` are two different origins,
/// so hardcoding one while devUrl says the other makes the sidecar's CORS allowlist
/// reject every request the app sends — silently, and only in dev.
fn dev_server_origin(app: &tauri::AppHandle) -> String {
    app.config()
        .build
        .dev_url
        .as_ref()
        .map(|url| url.origin().ascii_serialization())
        .unwrap_or_else(|| "http://127.0.0.1:1420".to_string())
}

fn spawn_sidecar(app: &tauri::AppHandle) -> Result<RunningSidecar, String> {
    let mut command = if cfg!(debug_assertions) {
        let root = project_root();
        let python = python_command(&root);
        let mut command = Command::new(python);
        command.args(["-m", "api.launcher"]);
        command.current_dir(&root);
        command.env("PYTHONUNBUFFERED", "1");
        // `tauri dev` serves the UI from the vite server, so every request to
        // the sidecar is cross-origin and the production allowlist rejects it.
        // Only debug builds pass this through; release builds never set it.
        command.env("TRANSLATOR_DEV_ORIGIN", dev_server_origin(app));
        command
    } else {
        let executable = bundled_sidecar_path(app)?;
        let working_directory = executable.parent().ok_or_else(|| {
            format!(
                "Bundled Translator engine has no parent directory: {}",
                executable.display()
            )
        })?;
        let mut command = Command::new(&executable);
        command.current_dir(working_directory);
        command
    };
    #[cfg(windows)]
    command.creation_flags(CREATE_NO_WINDOW);
    let mut child = command
        .env(
            "TRANSLATOR_SIDECAR_PARENT_PID",
            std::process::id().to_string(),
        )
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .map_err(|error| format!("Could not start Translator engine: {error}"))?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "Translator engine did not expose stdout.".to_string())?;
    let (sender, receiver) = mpsc::sync_channel(1);
    thread::spawn(move || {
        let mut line = String::new();
        let result = BufReader::new(stdout)
            .read_line(&mut line)
            .map_err(|error| format!("Could not read engine handshake: {error}"))
            .and_then(|count| {
                if count == 0 {
                    Err("Translator engine exited before its handshake.".to_string())
                } else {
                    parse_handshake(&line)
                }
            });
        let _ = sender.send(result);
    });

    // 握手失败/超时也必须亲手收掉子进程。`?` 提前返回只是 drop 掉 `Child`，而
    // drop 既不发信号也不 wait：sidecar 会继续跑，一直活到用户点掉那个「翻译引擎
    // 未能启动」的对话框、进程 exit 为止。首启撞上 Gatekeeper / Defender 扫描
    // 超过 30 秒就会走到这条路径。紧邻的健康检查失败路径本来就 kill，这里是遗漏。
    let handshake = receiver
        .recv_timeout(SIDECAR_START_TIMEOUT)
        .unwrap_or_else(|_| Err("Translator engine startup timed out.".to_string()));
    let info = match handshake {
        Ok(info) => info,
        Err(error) => {
            stop_child(&mut child, SIDECAR_ABORT_TIMEOUT);
            return Err(error);
        }
    };
    if let Err(error) = wait_for_health(info.port, &info.token) {
        stop_child(&mut child, SIDECAR_ABORT_TIMEOUT);
        return Err(error);
    }
    Ok(RunningSidecar { child, info })
}

fn parse_handshake(line: &str) -> Result<SidecarInfo, String> {
    let mut port = None;
    let mut token = None;
    for segment in line.split_whitespace() {
        if let Some(value) = segment.strip_prefix("PORT=") {
            port = value.parse::<u16>().ok();
        }
        if let Some(value) = segment.strip_prefix("TOKEN=") {
            token = Some(value.to_string());
        }
    }
    match (port, token) {
        (Some(port), Some(token)) if !token.is_empty() => Ok(SidecarInfo { port, token }),
        _ => Err("Translator engine returned an invalid handshake.".to_string()),
    }
}

fn wait_for_health(port: u16, token: &str) -> Result<(), String> {
    let deadline = std::time::Instant::now() + SIDECAR_HEALTH_TIMEOUT;
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    while std::time::Instant::now() < deadline {
        if let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(250)) {
            let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
            let request = format!(
                "GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Translator-Token: {token}\r\nConnection: close\r\n\r\n"
            );
            if stream.write_all(request.as_bytes()).is_ok() {
                let mut response = String::new();
                if stream.read_to_string(&mut response).is_ok()
                    && response.starts_with("HTTP/1.1 200")
                {
                    return Ok(());
                }
            }
        }
        thread::sleep(Duration::from_millis(100));
    }
    Err("Translator engine did not pass its health check.".to_string())
}

/// sidecar 是怎么停下来的。返回值只为把行为钉进测试，调用方不需要分支。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SidecarStopOutcome {
    /// 收到 SIGTERM 后自己退干净了——lifespan、`task_manager.shutdown`、
    /// runner 的 finally 都跑过。
    Graceful,
    /// 等到超时还在跑，只能 SIGKILL；临时目录和 soffice 子进程可能留下来。
    Forced,
}

/// 请子进程自己退出；返回「信号发出去了，值得再等一会儿」。
///
/// Unix 上是 SIGTERM：uvicorn 的 `handle_exit` 只认 SIGINT/SIGTERM，`Child::kill()`
/// 发的 SIGKILL 不可捕获，三层收尾（launcher 的 GracefulSidecarServer、app.py 的
/// lifespan、task_manager 的 shutdown）一层都执行不到。
#[cfg(unix)]
fn request_child_termination(child: &Child) -> bool {
    // 这里还没有 `wait()` 过，已退出的子进程仍是僵尸进程占着 pid，不存在 pid
    // 被复用后误杀别人的问题。
    unsafe { kill(child.id() as i32, SIGTERM) == 0 }
}

/// Windows 上没有能落到控制台子进程头上的优雅信号：`GenerateConsoleCtrlEvent`
/// 需要 `CREATE_NEW_PROCESS_GROUP`，而 sidecar 是带 `CREATE_NO_WINDOW` 起的
/// GUI 子进程，本机也无法实测。所以这里不假装能优雅，直接走 TerminateProcess，
/// 保证的是「安装器动手之前文件句柄一定已经释放」。
#[cfg(not(unix))]
fn request_child_termination(_child: &Child) -> bool {
    false
}

/// 先请子进程自己退，限时等待，超时才 SIGKILL 兜底。
///
/// 「限时」是硬要求：退出不能被一个卡死的 sidecar 拖住，否则 Cmd+Q 变成假死。
fn stop_child(child: &mut Child, timeout: Duration) -> SidecarStopOutcome {
    if request_child_termination(child) {
        let deadline = Instant::now() + timeout;
        loop {
            match child.try_wait() {
                Ok(Some(_)) => return SidecarStopOutcome::Graceful,
                Ok(None) => {}
                // 拿不到状态就别再等了，直接走兜底。
                Err(_) => break,
            }
            if Instant::now() >= deadline {
                break;
            }
            thread::sleep(SIDECAR_STOP_POLL);
        }
    }
    let _ = child.kill();
    // 必须 wait：不回收就留一个僵尸进程，而且 `kill()` 只是发信号，不等它真死。
    let _ = child.wait();
    SidecarStopOutcome::Forced
}

/// 停掉 state 里的 sidecar。take 过之后再调是空操作——退出路径上这个函数会被
/// 走到两次（`RunEvent::Exit` 一次，`cleanup_before_exit` 清资源表时又一次）。
fn stop_running_sidecar(state: &SidecarState, timeout: Duration) -> Option<SidecarStopOutcome> {
    let mut guard = state.0.lock().ok()?;
    let mut sidecar = guard.take()?;
    Some(stop_child(&mut sidecar.child, timeout))
}

fn stop_sidecar(app: &tauri::AppHandle) {
    let _ = stop_running_sidecar(&app.state::<SidecarState>(), SIDECAR_STOP_TIMEOUT);
}

/// 挂在 Tauri 资源表上的关闭钩子，专治「进程不经 `RunEvent::Exit` 就没了」。
///
/// updater 插件在 Windows 上装更新时的顺序是：`on_before_exit()` →
/// `AppHandle::cleanup_before_exit()` → `ShellExecuteW` 拉起 NSIS →
/// `std::process::exit(0)`（tauri-plugin-updater 的 updater.rs）。整条路上事件
/// 循环从没收到过退出事件，所以挂在 `RunEvent::Exit` 上的 `stop_sidecar` 一次
/// 都不执行，安装器开始覆盖文件时 sidecar 和它的 DLL 还被占着。
///
/// `cleanup_before_exit()` 做的第一件事就是清空 app 级资源表，清空即 drop 掉表
/// 里的 `Arc`，于是这个钩子的 `Drop` 成了那条路径上唯一还会执行的代码。它是同步
/// 的，返回之前 sidecar 一定已经退出，NSIS 拿到的是干净的目录。
/// 同一个机制顺带盖住 `AppHandle::restart()`（macOS 就地更新后「立即重启」走的
/// 就是它，同样只调 `cleanup_before_exit` 就 exec 新进程）。
struct SidecarShutdownHook {
    handle: tauri::AppHandle,
}

impl Resource for SidecarShutdownHook {}

impl Drop for SidecarShutdownHook {
    fn drop(&mut self) {
        // 注意：drop 发生在资源表的锁里，这里只碰 `SidecarState`，不要回头再取
        // 资源表，否则就是自锁。
        stop_sidecar(&self.handle);
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .setup(|app| {
            match spawn_sidecar(app.handle()) {
                Ok(sidecar) => {
                    app.manage(SidecarState(Mutex::new(Some(sidecar))));
                    // 见 `SidecarShutdownHook`：绕开事件循环的退出路径（Windows
                    // 更新安装、macOS 更新后重启）只会走 `cleanup_before_exit`，
                    // 资源表被清空时这个钩子是唯一还会执行到的关闭逻辑。
                    app.resources_table().add(SidecarShutdownHook {
                        handle: app.handle().clone(),
                    });
                }
                Err(error) => {
                    // No running sidecar to hand out, but commands that pull
                    // `State<'_, SidecarState>` still need the type managed or
                    // Tauri rejects the IPC call outright.
                    app.manage(SidecarState(Mutex::new(None)));
                    let handle = app.handle().clone();
                    // `blocking_show()` hops the actual dialog work onto the main
                    // thread via `run_on_main_thread` and blocks the *calling*
                    // thread until that hop finishes. `setup` runs on the main
                    // thread itself, before the event loop starts, so calling it
                    // here directly would have the main thread post a task to
                    // itself and then wait forever for a loop that never got a
                    // chance to start. Doing it from a background thread instead
                    // lets `.run()` below start the event loop normally, so the
                    // hop has somewhere to land.
                    thread::spawn(move || {
                        handle
                            .dialog()
                            .message(format!(
                                "翻译引擎未能启动，Translator 即将退出。\n\n{error}\n\n可以尝试：重新打开应用；确认安全软件或杀毒软件未拦截、隔离本应用的安装目录；如果仍反复出现，请到帮助页查看支持渠道。"
                            ))
                            .kind(MessageDialogKind::Error)
                            .title("Translator 无法启动")
                            .blocking_show();
                        std::process::exit(1);
                    });
                }
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            sidecar_info,
            inspect_output_directory,
            open_local_path,
            open_external_url,
            save_text_file,
            save_binary_file,
            update_environment
        ])
        .build(tauri::generate_context!())
        .expect("error while building Translator shell")
        .run(|app, event| {
            if matches!(event, RunEvent::Exit) {
                stop_sidecar(app);
            }
        });
}

#[cfg(test)]
mod tests {
    use super::{
        directory_is_writable, open_external_url, parse_handshake, split_save_frame,
        update_environment, write_file_atomically, SIDECAR_ABORT_TIMEOUT, SIDECAR_DRAIN_BUDGET_SECS,
        SIDECAR_SHUTDOWN_MARGIN_SECS, SIDECAR_STOP_TIMEOUT, SIDECAR_TASK_UNWIND_BUDGET_SECS,
    };
    #[cfg(unix)]
    use super::{
        stop_child, stop_running_sidecar, RunningSidecar, SidecarInfo, SidecarState,
        SidecarStopOutcome,
    };
    #[cfg(unix)]
    use std::{
        io::{BufRead, BufReader},
        process::{Child, Command, Stdio},
        sync::Mutex,
        time::{Duration, Instant},
    };

    /// 起一个假 sidecar，并且**等到它真的装好信号处理**才返回。
    ///
    /// 这个握手不是摆设：直接 spawn 完就发信号的话，SIGTERM 会赶在 `trap` 执行
    /// 之前到达，子进程按默认处置被杀掉，两个测试都会「通过」但什么都没验证到。
    #[cfg(unix)]
    fn spawn_test_child(trap: &str) -> Child {
        let script = format!("{trap}; echo ready; while :; do sleep 0.05; done");
        let mut child = Command::new("/bin/sh")
            .args(["-c", &script])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("failed to spawn the test child");
        let stdout = child.stdout.as_mut().expect("test child has no stdout");
        let mut line = String::new();
        BufReader::new(stdout)
            .read_line(&mut line)
            .expect("test child never reported readiness");
        assert_eq!(line.trim(), "ready");
        child
    }

    /// 一个「会自己收尾」的假 sidecar：收到 SIGTERM 就干净退出。
    #[cfg(unix)]
    fn spawn_well_behaved_child() -> Child {
        spawn_test_child("trap 'exit 0' TERM")
    }

    /// 一个卡死的假 sidecar：SIGTERM 被忽略，只有 SIGKILL 弄得死它。
    #[cfg(unix)]
    fn spawn_wedged_child() -> Child {
        spawn_test_child("trap '' TERM")
    }

    #[test]
    #[cfg(unix)]
    fn stopping_a_sidecar_lets_it_unwind_instead_of_sigkilling_it() {
        // 这是高-11 的回归点：以前退出走的是 `Child::kill()`（SIGKILL），
        // sidecar 三层收尾一层都跑不到，headless soffice 被 reparent 后残留。
        let mut child = spawn_well_behaved_child();
        let started = Instant::now();

        let outcome = stop_child(&mut child, Duration::from_secs(5));

        assert_eq!(outcome, SidecarStopOutcome::Graceful);
        // 正常收尾不该等满超时。
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[test]
    #[cfg(unix)]
    fn a_wedged_sidecar_is_killed_after_the_timeout_instead_of_blocking_exit() {
        // 优雅关闭必须带兜底：卡死的 sidecar 不许把退出拖住。
        let mut child = spawn_wedged_child();
        let started = Instant::now();

        let outcome = stop_child(&mut child, Duration::from_millis(300));
        let elapsed = started.elapsed();

        assert_eq!(outcome, SidecarStopOutcome::Forced);
        assert!(elapsed >= Duration::from_millis(300));
        // 兜底之后立刻返回，不会再多等。
        assert!(elapsed < Duration::from_secs(5), "等待超时了：{elapsed:?}");
    }

    #[test]
    #[cfg(unix)]
    fn stopping_the_sidecar_twice_is_a_no_op_the_second_time() {
        // 退出路径上关闭逻辑会被走到两次：`RunEvent::Exit` 一次，
        // `cleanup_before_exit()` 清空资源表触发 `SidecarShutdownHook` 又一次。
        // 第二次必须是空操作，不能去 wait 一个已经回收掉的子进程。
        let state = SidecarState(Mutex::new(Some(RunningSidecar {
            child: spawn_well_behaved_child(),
            info: SidecarInfo {
                port: 43123,
                token: "test-token".to_string(),
            },
        })));

        let first = stop_running_sidecar(&state, Duration::from_secs(5));
        let second = stop_running_sidecar(&state, Duration::from_secs(5));

        assert_eq!(first, Some(SidecarStopOutcome::Graceful));
        assert_eq!(second, None);
    }

    /// 从 Python 源码里取出一个锚点行后面的数字。
    ///
    /// 找不到锚点就直接 panic：那说明 api/ 那边动过结构，此时**必须**有人回来重新
    /// 核对内外两层的预算关系，静默放过才是真的危险。
    fn python_budget_secs(relative_path: &str, anchor: &str) -> f64 {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("src-tauri 没有上级目录？");
        let path = root.join(relative_path);
        let source = std::fs::read_to_string(&path)
            .unwrap_or_else(|err| panic!("读不到 {}：{err}", path.display()));
        let tail = source.split(anchor).nth(1).unwrap_or_else(|| {
            panic!(
                "在 {} 里找不到锚点 `{anchor}`。api/ 那边改过结构，请重新核对 \
                 main.rs 里的退出预算镜像常量，确认外层仍然包住内层。",
                path.display()
            )
        });
        let digits: String = tail
            .chars()
            .take_while(|c| c.is_ascii_digit() || *c == '.')
            .collect();
        digits
            .parse()
            .unwrap_or_else(|err| panic!("锚点 `{anchor}` 后面不是数字（{digits:?}）：{err}"))
    }

    #[test]
    fn the_exit_budget_outlasts_the_whole_sidecar_shutdown_chain() {
        // 高-11 的真正回归点：只把 SIGKILL 换成 SIGTERM 不够，外层预算还必须**包住**
        // 内层那条串行的链，否则 SIGKILL 恰好落在 Python 刚要写 history 的那一刻，
        // 临时目录和 headless soffice 照样残留、历史照样卡在「运行中」。
        // 上一版两边都是 12s、首尾相接、零余量，这个断言会红。
        let inner_chain = SIDECAR_DRAIN_BUDGET_SECS + SIDECAR_TASK_UNWIND_BUDGET_SECS;
        let outer = SIDECAR_STOP_TIMEOUT.as_secs();

        assert!(
            outer > inner_chain,
            "外层退出预算 {outer}s 必须严格大于内层链条 {inner_chain}s\
             （uvicorn 排空 {SIDECAR_DRAIN_BUDGET_SECS}s + 任务 unwind \
             {SIDECAR_TASK_UNWIND_BUDGET_SECS}s），否则 SIGKILL 会打断 Python 的收尾记账"
        );
        assert!(
            outer - inner_chain >= SIDECAR_SHUTDOWN_MARGIN_SECS,
            "余量只剩 {}s，不够 mark_active_tasks_interrupted + flush_history 落盘\
             （至少要 {SIDECAR_SHUTDOWN_MARGIN_SECS}s）",
            outer - inner_chain
        );
        // 启动失败那条路径是另一回事：用户正对着还没弹出来的错误对话框干等，
        // 握手都没完成也没什么可收尾的，短就是对的。
        assert!(SIDECAR_ABORT_TIMEOUT < SIDECAR_STOP_TIMEOUT);
    }

    #[test]
    fn the_mirrored_sidecar_budgets_still_match_the_python_side() {
        // 这两个数跨语言、跨文件，注释拴不住；改了 Python 一侧不改这里，
        // 外层就会重新缩回内层里去。让它变红，而不是等用户的临时目录残留。
        let drain = python_budget_secs("api/launcher.py", "GRACEFUL_SHUTDOWN_SECONDS = ");
        let unwind = python_budget_secs(
            "api/task_manager.py",
            "def shutdown(self, *, timeout: float = ",
        );

        assert_eq!(
            drain, SIDECAR_DRAIN_BUDGET_SECS as f64,
            "api/launcher.py 的 GRACEFUL_SHUTDOWN_SECONDS 现在是 {drain}s，和 main.rs 的\
             镜像常量对不上：请同步 SIDECAR_DRAIN_BUDGET_SECS，并确认 SIDECAR_STOP_TIMEOUT\
             仍然包得住内层"
        );
        assert_eq!(
            unwind, SIDECAR_TASK_UNWIND_BUDGET_SECS as f64,
            "api/task_manager.py 的 shutdown(timeout=) 现在是 {unwind}s，和 main.rs 的\
             镜像常量对不上：请同步 SIDECAR_TASK_UNWIND_BUDGET_SECS，并确认 \
             SIDECAR_STOP_TIMEOUT 仍然包得住内层"
        );
    }

    #[test]
    fn parses_launcher_handshake() {
        let info = parse_handshake("PORT=43123 TOKEN=one-time-token\n").unwrap();

        assert_eq!(info.port, 43123);
        assert_eq!(info.token, "one-time-token");
    }

    #[test]
    fn rejects_incomplete_launcher_handshake() {
        assert!(parse_handshake("PORT=43123\n").is_err());
        assert!(parse_handshake("TOKEN=one-time-token\n").is_err());
    }

    #[test]
    fn rejects_non_github_external_urls() {
        assert!(open_external_url("https://example.invalid/download".to_string()).is_err());
    }

    #[test]
    fn detects_writable_and_unwritable_directories() {
        let writable = std::env::temp_dir();
        assert!(directory_is_writable(&writable));
        assert!(!directory_is_writable(&writable.join("translator-missing-dir")));
    }

    #[test]
    fn splits_the_binary_save_frame() {
        // 路径带中文，正是这个自定义帧格式存在的理由（见 split_save_frame 的注释）。
        let path = "/tmp/导出.zip";
        let mut frame = (path.len() as u32).to_le_bytes().to_vec();
        frame.extend_from_slice(path.as_bytes());
        frame.extend_from_slice(&[0x50, 0x4b, 0x03, 0x04]);

        let (parsed_path, contents) = split_save_frame(&frame).unwrap();

        assert_eq!(parsed_path, path);
        assert_eq!(contents, &[0x50, 0x4b, 0x03, 0x04]);
    }

    #[test]
    fn rejects_truncated_binary_save_frames() {
        assert!(split_save_frame(&[1, 2]).is_err());
        // 长度前缀声称有 64 字节路径，实际只跟了 3 个字节。
        let mut frame = 64u32.to_le_bytes().to_vec();
        frame.extend_from_slice(b"abc");
        assert!(split_save_frame(&frame).is_err());
    }

    #[test]
    fn atomic_write_leaves_no_partial_file_on_failure() {
        let directory = std::env::temp_dir().join("translator-atomic-write-test");
        let _ = std::fs::remove_dir_all(&directory);
        std::fs::create_dir_all(&directory).unwrap();
        let target = directory.join("导出.json");

        write_file_atomically(&target, b"{\"ok\":true}").unwrap();
        assert_eq!(std::fs::read(&target).unwrap(), b"{\"ok\":true}");

        // 目标是一个已存在的目录 -> rename 必然失败，临时文件不许留下来。
        let blocked = directory.join("occupied");
        std::fs::create_dir_all(&blocked).unwrap();
        assert!(write_file_atomically(&blocked, b"x").is_err());
        let leftovers: Vec<_> = std::fs::read_dir(&directory)
            .unwrap()
            .filter_map(|entry| entry.ok())
            .filter(|entry| entry.file_name().to_string_lossy().contains(".partial"))
            .collect();
        assert!(leftovers.is_empty());

        let _ = std::fs::remove_dir_all(&directory);
    }

    #[test]
    fn debug_builds_never_claim_self_update_support() {
        // The whole test suite is a debug build, so this also pins the guard
        // that keeps `tauri dev` from offering an install that cannot work.
        let environment = update_environment();

        assert!(!environment.can_self_update);
        assert_eq!(environment.reason, "dev_build");
    }
}
