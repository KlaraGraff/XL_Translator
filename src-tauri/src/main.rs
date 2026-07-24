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
use tauri::{Manager, RunEvent, State};

const SIDECAR_START_TIMEOUT: Duration = Duration::from_secs(12);
const SIDECAR_HEALTH_TIMEOUT: Duration = Duration::from_secs(8);

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

fn spawn_sidecar(app: &tauri::AppHandle) -> Result<RunningSidecar, String> {
    let root = project_root();
    let mut command = if cfg!(debug_assertions) {
        let python = python_command(&root);
        let mut command = Command::new(python);
        command.args(["-m", "api.launcher"]);
        command.current_dir(&root);
        command.env("PYTHONUNBUFFERED", "1");
        command
    } else {
        let executable = bundled_sidecar_path(app)?;
        let mut command = Command::new(executable);
        command.current_dir(&root);
        command
    };
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
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .setup(|app| {
            let sidecar = spawn_sidecar(app.handle()).map_err(std::io::Error::other)?;
            app.manage(SidecarState(Mutex::new(Some(sidecar))));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![sidecar_info, inspect_output_directory])
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
    use super::parse_handshake;

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
}
