#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::{SocketAddr, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{mpsc, Mutex},
    thread,
    time::Duration,
};

use serde::Serialize;
use tauri::{ipc::InvokeBody, Manager, RunEvent, State};
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

    let info = receiver
        .recv_timeout(SIDECAR_START_TIMEOUT)
        .map_err(|_| "Translator engine startup timed out.".to_string())??;
    if let Err(error) = wait_for_health(info.port, &info.token) {
        let _ = child.kill();
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

fn stop_sidecar(app: &tauri::AppHandle) {
    let state = app.state::<SidecarState>();
    if let Ok(mut state) = state.0.lock() {
        if let Some(mut sidecar) = state.take() {
            let _ = sidecar.child.kill();
            let _ = sidecar.child.wait();
        }
    };
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
        update_environment, write_file_atomically,
    };

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
