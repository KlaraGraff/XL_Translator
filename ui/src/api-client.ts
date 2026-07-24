import { invoke } from "@tauri-apps/api/core";

export type SidecarInfo = {
  port: number;
  token: string;
};

export type TaskStatus = {
  task_id: string;
  surface: "excel" | "word" | "pdf" | "cleaner" | "tm_clean";
  source_label?: string;
  state: "preflight" | "running" | "pausing" | "paused" | "stopping" | "finalizing" | "done" | "completed_with_issues" | "error" | "stopped" | "interrupted";
  terminal: boolean;
  created_at?: number;
  updated_at?: number;
  model_snapshot?: Record<string, unknown>;
  task_snapshot?: Record<string, unknown>;
  resource_groups?: Array<Record<string, unknown>>;
  logs?: Array<Record<string, unknown>>;
  result: Record<string, unknown> | null;
};

export type SseEvent = {
  id: number;
  type: string;
  data: Record<string, unknown>;
};

export type TaskPreflight = {
  requires_confirmation: boolean;
  confirmation_token?: string;
  risk?: Record<string, unknown>;
  candidate_snapshot?: Record<string, unknown>;
};

export type TaskList = {
  active: TaskStatus[];
  recent: TaskStatus[];
};

export type StreamOptions = {
  lastEventId?: number;
  onConnectionState?: (state: "connected" | "reconnecting") => void;
  signal?: AbortSignal;
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
    options: StreamOptions = {},
  ): Promise<number> {
    let lastEventId = options.lastEventId ?? 0;
    let attempt = 0;
    while (!options.signal?.aborted) {
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

        options.onConnectionState?.("connected");
        attempt = 0;

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
          terminal = ["done", "completed_with_issues", "error", "stopped", "interrupted"].includes(event.type);
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
          return lastEventId;
        }
      } catch (error) {
        if (options.signal?.aborted) {
          return lastEventId;
        }
        if (attempt >= 7) {
          throw error;
        }
      }
      options.onConnectionState?.("reconnecting");
      await new Promise((resolve) => window.setTimeout(resolve, Math.min(5_000, 300 * (2 ** attempt))));
      attempt += 1;
    }
    return lastEventId;
  }

  async preflightTask(payload: Record<string, unknown>): Promise<TaskPreflight> {
    return this.request<TaskPreflight>("/api/tasks/preflight", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async listTasks(): Promise<TaskList> {
    return this.request<TaskList>("/api/tasks");
  }

  async getTask(taskId: string): Promise<TaskStatus> {
    return this.request<TaskStatus>(`/api/tasks/${taskId}`);
  }

  async getTaskResult(taskId: string): Promise<TaskStatus> {
    return this.request<TaskStatus>(`/api/tasks/${taskId}/results`);
  }
}
