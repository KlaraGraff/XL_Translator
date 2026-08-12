import { invoke } from "@tauri-apps/api/core";
import { saveBinaryFile } from "./save-file";

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
  /** 按「跳过 A3 及更大的页面」的尺寸判定被跳过——原始矢量内容整页直传到输出，没有译文也没有页图。
   *  和 user_skipped（用户在逐页面板里手动跳过）是两回事，展示上也必须分开：这个不是异常。 */
  skipped_oversize: boolean;
  /** 本地质检给这一页挂了疑点（版式/文本比例异常之类），译文仍然采用了。跟 review_status 无关：
   *  质检在送审之前跑，审核判「通过」也不会把它清掉，所以两个信号要分别显示。 */
  quality_flagged: boolean;
  quality_message: string;
  /** 译文太长、按应急比例强行缩排过。同样是「采用了但建议看一眼」，跟质检疑点并列。 */
  emergency_ratio_normalized: boolean;
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
  /** 这次任务有没有开逐页审核。卡片副标题按它改口，否则关着审核时会写「审核模型逐页检查」
   *  而同一张表里每一行都是「未审核」。 */
  review_enabled: boolean;
  files: PdfPageFile[];
};

export type StreamOptions = {
  lastEventId?: number;
  onConnectionState?: (state: "connected" | "reconnecting") => void;
  signal?: AbortSignal;
};

/** 后端的错误响应除了 detail 还可能带一个 reason（稳定的机器码）。有出路的拦截靠它区分：
 *  比对中文句子太脆，改一个字界面就认不出来了。 */
export class ApiError extends Error {
  readonly status: number;
  readonly reason: string;

  constructor(message: string, status: number, reason = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.reason = reason;
  }
}

/** 从任意 catch 到的东西里取 reason，取不到就是空串。 */
export function apiErrorReason(error: unknown): string {
  return error instanceof ApiError ? error.reason : "";
}

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
      throw new ApiError(
        String(payload.detail ?? fallback),
        response.status,
        typeof payload.reason === "string" ? payload.reason : "",
      );
    }
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  /**
   * 取一个二进制响应，弹原生保存框写到用户选定的路径。返回写入的路径，null = 用户取消。
   *
   * 保存框里的默认文件名优先用后端 Content-Disposition 里的那个，拿不到才退回调用方
   * 给的兜底名——诊断包的名字带时间戳和场景，是后端算出来的。
   *
   * 不能用 `<a download>` 交给 WebView 下载：macOS 的 WKWebView 会静默丢弃它，
   * 原因见 save-file.ts 顶部注释。
   */
  async saveBinaryDownload(path: string, fallbackFilename: string): Promise<string | null> {
    const response = await fetch(`${this.#baseUrl}${path}`, {
      headers: { "X-Translator-Token": this.#token },
    });
    if (!response.ok) {
      const fallback = `${response.status} ${response.statusText}`;
      const payload = await response.json().catch(() => ({ detail: fallback }));
      throw new Error(String(payload.detail ?? fallback));
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || fallbackFilename;
    return saveBinaryFile(filename, await response.arrayBuffer());
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
