import { invoke } from "@tauri-apps/api/core";

export async function workerInvoke<T>(command: string, payload: unknown = {}): Promise<T> {
  return invoke<T>("worker_invoke", { command, payload });
}

export async function restartWorker(): Promise<void> {
  return invoke<void>("worker_restart");
}
