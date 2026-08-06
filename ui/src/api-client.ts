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

/** GET /api/tasks/{task_id}/pdf-pages 里单页快照。pending_action 为空串表示没有排队中的操作；
 *  排队的操作只在恢复任务时才真正生效——见 PdfPagesSnapshot.actionable 的说明。 */
export type PdfPage = {
  page_number: number;
  status: string;
  review_status: string;
  attempts: number;
  placeholder: boolean;
  error: string;
  review_summary: string;
  pending_action: "" | "regenerate" | "skip";
  user_skipped: boolean;
  has_source_image: boolean;
  has_translated_image: boolean;
};

export type PdfPageFile = {
  name: string;
  relative_path: string;
  source_type: string;
  status: string;
  error: string;
  page_count: number;
  pages: PdfPage[];
};

/** actionable（= 暂停中且未终态）是页级操作按钮唯一的可用性开关，不要用 task 本身的 state/terminal
 *  代替——这份快照的 state/terminal/actionable 是后端针对页操作单独算出来的。 */
export type PdfPagesSnapshot = {
  task_id: string;
  state: string;
  terminal: boolean;
  actionable: boolean;
  files: PdfPageFile[];
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
          // Only actual stream data counts as progress. Resetting on any 200
          // would turn a stream that dies right after the headers (e.g. the
          // task is gone server-side) into an infinite fast reconnect loop.
          if (chunk.value?.length) {
            attempt = 0;
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

  async getPdfPages(taskId: string): Promise<PdfPagesSnapshot> {
    return this.request<PdfPagesSnapshot>(`/api/tasks/${taskId}/pdf-pages`);
  }

  async regeneratePdfPage(taskId: string, file: string, page: number): Promise<void> {
    await this.request(`/api/tasks/${taskId}/pdf-pages/regenerate`, {
      method: "POST",
      body: JSON.stringify({ file, page }),
    });
  }

  async skipPdfPage(taskId: string, file: string, page: number): Promise<void> {
    await this.request(`/api/tasks/${taskId}/pdf-pages/skip`, {
      method: "POST",
      body: JSON.stringify({ file, page }),
    });
  }

  /** 页图是二进制响应，走独立 fetch（而不是 request<T>，它固定 response.json()）；
   *  鉴权头与 request() 保持一致。file 是任务内相对路径，可能含斜杠/中文，调用方不必自行编码。 */
  async getPdfPageImage(taskId: string, file: string, page: number, kind: "source" | "translated"): Promise<Blob> {
    const url = `${this.#baseUrl}/api/tasks/${taskId}/pdf-pages/image?file=${encodeURIComponent(file)}&page=${page}&kind=${kind}`;
    const response = await fetch(url, { headers: { "X-Translator-Token": this.#token } });
    if (!response.ok) {
      const fallback = `${response.status} ${response.statusText}`;
      const payload = await response.json().catch(() => ({ detail: fallback }));
      throw new Error(String(payload.detail ?? fallback));
    }
    return response.blob();
  }
}
