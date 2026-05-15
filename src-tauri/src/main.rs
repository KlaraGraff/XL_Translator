use serde_json::{json, Value};
use std::{
    collections::HashMap,
    env,
    io::{BufRead, BufReader, Write},
    path::PathBuf,
    process::{Child, ChildStdin, Command, Stdio},
    sync::{mpsc, Arc, Mutex},
    thread,
    time::Duration,
};
use tauri::{AppHandle, Emitter, Manager, State};
use uuid::Uuid;

type PendingMap = Arc<Mutex<HashMap<String, mpsc::Sender<Result<Value, String>>>>>;

struct WorkerProcess {
    child: Child,
    stdin: ChildStdin,
    pending: PendingMap,
}

impl Drop for WorkerProcess {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}

struct WorkerState {
    process: Mutex<Option<WorkerProcess>>,
}

#[tauri::command]
fn worker_invoke(
    app: AppHandle,
    state: State<WorkerState>,
    command: String,
    payload: Value,
) -> Result<Value, String> {
    let request_id = Uuid::new_v4().to_string();
    let (sender, receiver) = mpsc::channel();

    let mut guard = state
        .process
        .lock()
        .map_err(|_| "Worker state lock poisoned.".to_string())?;
    if guard.is_none() {
        *guard = Some(start_worker(&app)?);
    }
    let worker = guard
        .as_mut()
        .ok_or_else(|| "Worker failed to start.".to_string())?;

    worker
        .pending
        .lock()
        .map_err(|_| "Worker pending map lock poisoned.".to_string())?
        .insert(request_id.clone(), sender);

    let message = json!({
        "id": request_id,
        "command": command,
        "payload": payload,
    });
    writeln!(worker.stdin, "{}", message).map_err(|error| error.to_string())?;
    worker.stdin.flush().map_err(|error| error.to_string())?;
    drop(guard);

    receiver
        .recv_timeout(Duration::from_secs(3600))
        .map_err(|_| "Worker request timed out.".to_string())?
}

#[tauri::command]
fn worker_restart(app: AppHandle, state: State<WorkerState>) -> Result<(), String> {
    let mut guard = state
        .process
        .lock()
        .map_err(|_| "Worker state lock poisoned.".to_string())?;
    if let Some(mut process) = guard.take() {
        let _ = process.child.kill();
    }
    *guard = Some(start_worker(&app)?);
    Ok(())
}

fn start_worker(app: &AppHandle) -> Result<WorkerProcess, String> {
    let launch = resolve_worker_launch(app)?;
    let mut command = Command::new(&launch.program);
    command.args(&launch.args);
    command.stdin(Stdio::piped());
    command.stdout(Stdio::piped());
    command.stderr(Stdio::piped());
    command.current_dir(launch.cwd);

    let mut child = command.spawn().map_err(|error| {
        format!(
            "Unable to start Python worker at {}: {}",
            launch.program.display(),
            error
        )
    })?;

    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| "Worker stdin is unavailable.".to_string())?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "Worker stdout is unavailable.".to_string())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "Worker stderr is unavailable.".to_string())?;

    let pending: PendingMap = Arc::new(Mutex::new(HashMap::new()));
    spawn_stdout_reader(app.clone(), pending.clone(), stdout);
    spawn_stderr_reader(app.clone(), stderr);

    Ok(WorkerProcess {
        child,
        stdin,
        pending,
    })
}

struct WorkerLaunch {
    program: PathBuf,
    args: Vec<String>,
    cwd: PathBuf,
}

fn resolve_worker_launch(app: &AppHandle) -> Result<WorkerLaunch, String> {
    if let Ok(root) = env::var("XL_TRANSLATOR_DEV_ROOT") {
        return resolve_dev_worker(PathBuf::from(root));
    }

    #[cfg(debug_assertions)]
    {
        let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        return resolve_dev_worker(
            manifest_dir
                .parent()
                .ok_or_else(|| "Unable to resolve project root.".to_string())?
                .to_path_buf(),
        );
    }

    #[cfg(not(debug_assertions))]
    {
        let resource_dir = app
            .path()
            .resource_dir()
            .map_err(|error| error.to_string())?;
        let worker_dir = resource_dir.join("workers").join("xl-translator-worker");
        let executable = worker_dir.join(worker_executable_name());
        if executable.exists() {
            return Ok(WorkerLaunch {
                program: executable,
                args: vec![],
                cwd: worker_dir,
            });
        }
        Err(format!(
            "Packaged worker was not found: {}",
            executable.display()
        ))
    }
}

fn resolve_dev_worker(root: PathBuf) -> Result<WorkerLaunch, String> {
    let script = root.join("scripts").join("tauri_worker.py");
    if !script.exists() {
        return Err(format!("Worker script was not found: {}", script.display()));
    }

    let python = [
        root.join(".venv").join("bin").join("python3"),
        root.join(".venv").join("bin").join("python"),
        root.join(".venv").join("Scripts").join("python.exe"),
    ]
    .into_iter()
    .find(|candidate| candidate.exists())
    .unwrap_or_else(|| PathBuf::from("python3"));

    Ok(WorkerLaunch {
        program: python,
        args: vec![script.to_string_lossy().to_string()],
        cwd: root,
    })
}

#[cfg(target_os = "windows")]
fn worker_executable_name() -> &'static str {
    "xl-translator-worker.exe"
}

#[cfg(not(target_os = "windows"))]
fn worker_executable_name() -> &'static str {
    "xl-translator-worker"
}

fn spawn_stdout_reader(app: AppHandle, pending: PendingMap, stdout: impl std::io::Read + Send + 'static) {
    thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines().map_while(Result::ok) {
            let parsed: Result<Value, _> = serde_json::from_str(&line);
            let Ok(message) = parsed else {
                let _ = app.emit("worker-log", json!({"level": "WARN", "message": line}));
                continue;
            };

            if let Some(event_name) = message.get("event").and_then(Value::as_str) {
                let payload = message.get("payload").cloned().unwrap_or(Value::Null);
                let _ = app.emit(event_name, payload.clone());
                let _ = app.emit("worker-event", message);
                continue;
            }

            if let Some(id) = message.get("id").and_then(Value::as_str) {
                let sender = pending.lock().ok().and_then(|mut map| map.remove(id));
                if let Some(sender) = sender {
                    let response = if message.get("ok").and_then(Value::as_bool).unwrap_or(false) {
                        Ok(message.get("result").cloned().unwrap_or(Value::Null))
                    } else {
                        Err(message
                            .get("error")
                            .and_then(|error| error.get("message"))
                            .and_then(Value::as_str)
                            .unwrap_or("Worker request failed.")
                            .to_string())
                    };
                    let _ = sender.send(response);
                }
            }
        }
        let _ = app.emit("worker-log", json!({"level": "WARN", "message": "Worker stdout closed."}));
    });
}

fn spawn_stderr_reader(app: AppHandle, stderr: impl std::io::Read + Send + 'static) {
    thread::spawn(move || {
        let reader = BufReader::new(stderr);
        for line in reader.lines().map_while(Result::ok) {
            let _ = app.emit("worker-log", json!({"level": "INFO", "message": line}));
        }
    });
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(WorkerState {
            process: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![worker_invoke, worker_restart])
        .run(tauri::generate_context!())
        .expect("error while running XL Translator");
}
