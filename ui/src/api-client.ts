import { invoke } from "@tauri-apps/api/core";

export type SidecarInfo = {
  port: number;
  token: string;
};

export type TaskStatus = {
  task_id: string;
  surface: "excel" | "word" | "pdf";
  source_path: string;
  state: "running" | "paused" | "stopping" | "done" | "error" | "stopped";
  terminal: boolean;
  result: Record<string, unknown> | null;
};

export type SseEvent = {
  id: number;
  type: string;
  data: Record<string, unknown>;
};

export class ApiClient {
  #baseUrl = "";
  #token = "";

  async connect(): Promise<void> {
    const info = await invoke<SidecarInfo>("sidecar_info");
    this.#baseUrl = `http://127.0.0.1:${info.port}`;
    this.#token = info.token;
    await this.request("/health");
  }

  async request<T>(
    path: string,
    options: RequestInit = {},
  ): Promise<T> {
    const headers = new Headers(options.headers);
    headers.set("X-Translator-Token", this.#token);
    if (options.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    const response = await fetch(`${this.#baseUrl}${path}`, {
      ...options,
      headers,
    });
    if (!response.ok) {
      const fallback = `${response.status} ${response.statusText}`;
      const payload = await response.json().catch(() => ({ detail: fallback }));
      throw new Error(String(payload.detail ?? fallback));
    }
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  async streamTask(
    taskId: string,
    onEvent: (event: SseEvent) => void,
  ): Promise<void> {
    let lastEventId = 0;
    for (let attempt = 0; attempt < 4; attempt += 1) {
      const headers = new Headers({ "X-Translator-Token": this.#token });
      if (lastEventId) {
        headers.set("Last-Event-ID", String(lastEventId));
      }
      try {
        const response = await fetch(`${this.#baseUrl}/api/tasks/${taskId}/events`, {
          headers,
        });
        if (!response.ok || !response.body) {
          throw new Error("Could not open the task event stream.");
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let eventId = 0;
        let eventType = "message";
        let dataLines: string[] = [];
        let terminal = false;
        const emit = () => {
          if (!dataLines.length) {
            return;
          }
          const event = {
            id: eventId,
            type: eventType,
            data: JSON.parse(dataLines.join("\n")) as Record<string, unknown>,
          };
          lastEventId = Math.max(lastEventId, event.id);
          onEvent(event);
          terminal = ["done", "error", "stopped"].includes(event.type);
          eventId = 0;
          eventType = "message";
          dataLines = [];
        };

        while (!terminal) {
          const chunk = await reader.read();
          if (chunk.done) {
            emit();
            break;
          }
          buffer += decoder.decode(chunk.value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? "";
          for (const rawLine of lines) {
            const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
            if (!line) {
              emit();
            } else if (line.startsWith("id: ")) {
              eventId = Number(line.slice(4));
            } else if (line.startsWith("event: ")) {
              eventType = line.slice(7);
            } else if (line.startsWith("data: ")) {
              dataLines.push(line.slice(6));
            }
          }
        }
        if (terminal) {
          return;
        }
      } catch (error) {
        if (attempt === 3) {
          throw error;
        }
      }
      await new Promise((resolve) => window.setTimeout(resolve, 250 * (attempt + 1)));
    }
    throw new Error("Task event stream closed before a terminal event.");
  }
}
