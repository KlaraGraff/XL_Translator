import { invoke } from "@tauri-apps/api/core";
import { saveBinaryFile } from "./save-file";
import type { FileProgressSnapshot } from "./file-progress";
import { tx } from "./i18n";

export type SidecarInfo = {
  port: number;
  token: string;
};

export type TaskStatus = {
  task_id: string;
  surface: "excel" | "word" | "pdf" | "cad" | "cleaner" | "tm_clean";
  source_label?: string;
  state: "preflight" | "running" | "pausing" | "paused" | "stopping" | "finalizing" | "done" | "completed_with_issues" | "error" | "stopped" | "interrupted";
  terminal: boolean;
  created_at?: number;
  updated_at?: number;
  model_snapshot?: Record<string, unknown>;
  task_snapshot?: Record<string, unknown>;
  resource_groups?: Array<Record<string, unknown>>;
  logs?: Array<Record<string, unknown>>;
  // list_tasks() 的 active 数组走 _status_payload(include_result=False)（api/task_manager.py），
  // 报文里根本没有 "result" 这个键——不是 null，是键缺失，JS 里读到的就是 undefined。
  // 只有 recent（历史记录）和单任务详情接口才总带这个键。之前这里写死非可选、只留 null，
  // 等于告诉调用方“这个字段永远在”，把“缺键”和“存在但为 null”混成一回事；调用方一旦按
  // `!== null` 做类型收窄就会在 undefined 上直接调用属性而炸。全仓库现有读取都经过下面
  // 的 record() 兜底、对 undefined/null 一视同仁，运行时没受影响，但类型声明本身是假的。
  result?: Record<string, unknown> | null;
  /** Current per-file status snapshot; absent on older or non-file tasks. */
  file_progress?: FileProgressSnapshot;
  progress?: Record<string, unknown>;
  /** 用户经接口按过停止。缺键（旧历史记录）按「是」处理；false 表示 runner 自行收尾
   *  （如 Word 持续限流），界面要显示后端给的原因。 */
  stop_requested?: boolean;
};

/** CAD 插件与 ODA 能力状态；后端未提供时前端按 unavailable 处理。 */
export type CadCapabilityStatus = {
  plugin: "missing" | "installing" | "enabled" | "error";
  oda: "missing" | "connected" | "incompatible";
  platform?: { system: string; arch: string };
  platform_supported?: boolean;
  enabled?: boolean;
  installed?: boolean;
  converter?: string | null;
  converter_found?: boolean;
  official_download_url?: string;
  oda_bundled?: boolean;
  license_action?: string;
  version?: string;
  detail?: string;
};

/** 文字识别组件（Tesseract OCR）状态；后端未提供时前端按 unavailable 处理。 */
export type OcrPluginStatus = {
  state: "connected" | "missing" | "invalid";
  path: string;
  version?: string | null;
  languages: string[];
  has_latin: boolean;
  message?: string;
};

/** PDF 转 Word 组件（pdf2docx 插件）状态。 */
export type Pdf2DocxPluginStatus = {
  state: "connected" | "missing" | "invalid";
  path: string;
  message?: string;
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
  /** 「原位替换文字（Beta）」：这一页按设置走了原位替换。done = 已替换并装进输出；
   *  kept_original = 没成功（出错/自检不过/没有可替换的字），输出里是原页。 */
  inplace_requested?: boolean;
  inplace_status?: "" | "done" | "kept_original" | "stopped";
  inplace_written?: number;
  inplace_kept?: number;
  inplace_review?: number;
  /** 译文已写入、但未采用记忆库中设定的译法的处数（也在 inplace_review_items 里，kind 为 term）。 */
  inplace_term_review?: number;
  inplace_review_items?: { text: string; reason: string; detail?: string; kind?: string; box_px?: number[] }[];
  inplace_message?: string;
  /** 首选方式用不了、保底也用不了而保留原样的原因（比如没有设置图像翻译模型）。 */
  kept_reason?: string;
  /** 首选方式用不了、改用整页图像翻译的原因。 */
  route_note?: string;
  /** 混合文件里转 Word 翻译的正文页（Beta）："" | done | failed | stopped。 */
  word_requested?: boolean;
  word_status?: string;
  word_message?: string;
  /** 本地质检给这一页挂了疑点（版式/文本比例异常之类），译文仍然采用了。跟 review_status 无关：
   *  质检在送审之前跑，审核判「通过」也不会把它清掉，所以两个信号要分别显示。 */
  quality_flagged: boolean;
  quality_message: string;
  /** 译文太长、按应急比例强行缩排过。同样是「采用了但建议看一眼」，跟质检疑点并列。 */
  emergency_ratio_normalized: boolean;
  has_source_image: boolean;
  has_translated_image: boolean;
  /** 这一页留着上一版译文（上一次单页重生成把旧译图顶下来时留的，只留紧邻一版）。
   *  对比弹窗靠它决定要不要出「当前版／上一版」切换和「换回这一版」。 */
  has_previous_image: boolean;
};

/** PDF 翻译方式（Beta）：设置面板用的本机条件。 */
export type PdfRouteCapabilities = {
  image_model_ready: boolean;
  image_model_message: string;
  word_component: boolean;
  office_engine: string;
  office_message: string;
  inplace_target_ok: boolean;
};

export type PdfPageFile = {
  name: string;
  relative_path: string;
  source_type: string;
  status: string;
  error: string;
  page_count: number;
  /** 翻译方式（Beta 分支）：读文件时的判断 text / drawing / scan / mixed（整页图像翻译时为空）。 */
  document_kind?: string;
  translated_docx_path?: string;
  pages: PdfPage[];
};

/** CAD 译文对照表（任务中心「查看与修改译文」）。同一段原文在图上出现多次时只列一行。 */
export type CadReviewRow = {
  source: string;
  /** 第一次（或上次写回）写进图纸的译文；未译出、保留原文时为空。 */
  original: string;
  /** 当前译文：改过就是改后的。 */
  translation: string;
  /** review：译文已写入，但未采用记忆库中设定的译法，需复核。 */
  status: "translated" | "kept" | "unresolved" | "review";
  /** 需复核的原因（设定译法和重译次数）。 */
  reason?: string;
  count: number;
  crowded: boolean;
  edited: boolean;
};

export type CadReviewFile = {
  key: string;
  name: string;
  output_name?: string;
  rows: CadReviewRow[];
  available: boolean;
  rewritable: boolean;
  /** 不能重新写回时说清原因（原图已移走、旧版本翻译的……）。 */
  reason: string;
  edited?: number;
};

export type CadReviewRewriteResult = {
  file: string;
  name: string;
  ok: boolean;
  message: string;
  changed?: number;
  untranslated?: number;
  layout_unresolved?: number;
};

export type CadReviewRewrite = {
  active: boolean;
  done?: number;
  total?: number;
  current?: string;
  results?: CadReviewRewriteResult[];
};

export type CadReviewSnapshot = {
  task_id: string;
  terminal: boolean;
  target_lang: string;
  rewrite: CadReviewRewrite;
  files: CadReviewFile[];
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
  /** 终态任务的单页重生成走的是另一条路：不排队、立刻跑、跑完覆盖输出文件。所以可用性
   *  也另算——actionable 说的是「暂停中可以排队页操作」，这一条说的是「已结束，但还能
   *  把某一页重跑一遍」。两个开关不会同时为真。 */
  rerun_actionable: boolean;
  /** 终态没有 SSE 可用（事件流在终态就结束了），这份快照是界面唯一的重生成进度来源。 */
  rerun: PdfPageRerunState;
  files: PdfPageFile[];
};

export type PdfPageRerunState = {
  active: boolean;
  relative_path: string;
  page_number: number;
  error: string;
};

export type StreamOptions = {
  lastEventId?: number;
  onConnectionState?: (state: "connected" | "reconnecting") => void;
  signal?: AbortSignal;
};

/** 错误响应的 detail 一般是一句中文；但 FastAPI 自己的请求校验失败（422）给的是一个
 *  对象数组，直接 String() 出来就是「[object Object]」。那种情况换成一句人话。 */
function errorDetail(payload: { detail?: unknown }, fallback: string): string {
  const detail = payload.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) return "提交的内容格式不正确，本次操作未生效。"; // i18n-ignore 作为后端原话进 ApiError，构造时由 tx() 翻译，rawMessage 保留中文
  return fallback;
}

/** 后端的错误响应除了 detail 还可能带一个 reason（稳定的机器码）。有出路的拦截靠它区分：
 *  比对中文句子太脆，改一个字界面就认不出来了。 */
export class ApiError extends Error {
  readonly status: number;
  readonly reason: string;
  /** 后端原话（中文）。message 已按界面语言翻好；要按句子内容判断时用这个。 */
  readonly rawMessage: string;

  constructor(message: string, status: number, reason = "") {
    super(tx(message));
    this.name = "ApiError";
    this.status = status;
    this.reason = reason;
    this.rawMessage = message;
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
        errorDetail(payload, fallback),
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
   * `init` 用于需要 POST 请求体才能生成的包（例如反馈页「保存为 ZIP」）。
   *
   * 不能用 `<a download>` 交给 WebView 下载：macOS 的 WKWebView 会静默丢弃它，
   * 原因见 save-file.ts 顶部注释。
   */
  async saveBinaryDownload(path: string, fallbackFilename: string, init: RequestInit = {}): Promise<string | null> {
    const headers = new Headers(init.headers);
    headers.set("X-Translator-Token", this.#token);
    if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    const response = await fetch(`${this.#baseUrl}${path}`, { ...init, headers });
    if (!response.ok) {
      const fallback = `${response.status} ${response.statusText}`;
      const payload = await response.json().catch(() => ({ detail: fallback }));
      throw new Error(errorDetail(payload, fallback));
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
        // 干净的 200 但连一个终态事件都没见到（比如快照竞态把本地状态短暂打回非终态，
        // 调用方拿着这份「还在跑」的判断重新打开流，而服务端这边任务其实已经结束，
        // 于是连接一建立就直接关闭、body 是空的）——这条路径不抛异常，如果这里不检查
        // 重试上限，就会绕开下面 catch 块里 attempt >= 7 的封顶，变成每隔至多 5 秒
        // 重连一次、永不停止的死循环。抛出后交给下面同一个 catch 走同样的封顶逻辑。
        if (attempt >= 7) {
          throw new Error("Task event stream closed without ever reaching a terminal event.");
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

  async getCadCapability(): Promise<CadCapabilityStatus> {
    return this.request<CadCapabilityStatus>("/api/plugins/cad");
  }

  async installCadPlugin(): Promise<CadCapabilityStatus> {
    return this.request<CadCapabilityStatus>("/api/plugins/cad/install", { method: "POST", body: JSON.stringify({}) });
  }
  async uninstallCadPlugin(): Promise<CadCapabilityStatus> {
    return this.request<CadCapabilityStatus>("/api/plugins/cad/uninstall", { method: "POST", body: JSON.stringify({}) });
  }
  async refreshCadCapability(): Promise<CadCapabilityStatus> { return this.getCadCapability(); }

  async officialCadDownload(): Promise<{ authorization_url?: string; url?: string }> {
    return this.request<{ authorization_url?: string; url?: string }>("/api/plugins/cad/official-download", { method: "POST", body: JSON.stringify({}) });
  }

  async connectCadOda(path: string): Promise<CadCapabilityStatus> {
    return this.request<CadCapabilityStatus>("/api/plugins/cad/oda", { method: "POST", body: JSON.stringify({ path }) });
  }

  pdfRouteCapabilities(): Promise<PdfRouteCapabilities> {
    return this.request<PdfRouteCapabilities>("/api/pdf/route-capabilities");
  }

  async getOcrCapability(): Promise<OcrPluginStatus> {
    return this.request<OcrPluginStatus>("/api/plugins/ocr");
  }

  async refreshOcrCapability(): Promise<OcrPluginStatus> { return this.getOcrCapability(); }

  async officialOcrDownload(): Promise<{ authorization_url?: string; url?: string }> {
    return this.request<{ authorization_url?: string; url?: string }>("/api/plugins/ocr/official-download", { method: "POST", body: JSON.stringify({}) });
  }

  async connectOcrTesseract(path: string): Promise<OcrPluginStatus> {
    return this.request<OcrPluginStatus>("/api/plugins/ocr/install", { method: "POST", body: JSON.stringify({ path }) });
  }

  async uninstallOcrPlugin(): Promise<OcrPluginStatus> {
    return this.request<OcrPluginStatus>("/api/plugins/ocr/uninstall", { method: "POST", body: JSON.stringify({}) });
  }

  async getPdf2DocxCapability(): Promise<Pdf2DocxPluginStatus> {
    return this.request<Pdf2DocxPluginStatus>("/api/plugins/pdf2docx");
  }

  async officialPdf2DocxDownload(): Promise<{ authorization_url?: string; url?: string }> {
    return this.request<{ authorization_url?: string; url?: string }>("/api/plugins/pdf2docx/official-download", { method: "POST", body: JSON.stringify({}) });
  }

  async connectPdf2Docx(path: string): Promise<Pdf2DocxPluginStatus> {
    return this.request<Pdf2DocxPluginStatus>("/api/plugins/pdf2docx/install", { method: "POST", body: JSON.stringify({ path }) });
  }

  async uninstallPdf2DocxPlugin(): Promise<Pdf2DocxPluginStatus> {
    return this.request<Pdf2DocxPluginStatus>("/api/plugins/pdf2docx/uninstall", { method: "POST", body: JSON.stringify({}) });
  }

  async controlTask(taskId: string, action: "pause" | "resume" | "stop" | "retry"): Promise<TaskStatus> {
    return this.request<TaskStatus>(`/api/tasks/${taskId}/${action}`, { method: "POST", body: JSON.stringify({}) });
  }

  async getTask(taskId: string): Promise<TaskStatus> {
    return this.request<TaskStatus>(`/api/tasks/${taskId}`);
  }

  async getTaskResult(taskId: string): Promise<TaskStatus> {
    return this.request<TaskStatus>(`/api/tasks/${taskId}/results`);
  }

  async getCadReview(taskId: string): Promise<CadReviewSnapshot> {
    return this.request<CadReviewSnapshot>(`/api/tasks/${taskId}/cad-review`);
  }

  /** 一张图纸里改过的译文；值为空表示改回第一次的译文。改过的会顺手存进记忆库。 */
  async saveCadReviewEdits(taskId: string, file: string, edits: Record<string, string>): Promise<{ saved: number; remembered: number }> {
    return this.request(`/api/tasks/${taskId}/cad-review/edits`, {
      method: "PUT",
      body: JSON.stringify({ file, edits }),
    });
  }

  /** 导回改好的 Excel；data 为 base64（不带 data: 前缀）。 */
  async importCadReview(taskId: string, data: string): Promise<{ files: number; rows: number; remembered: number }> {
    return this.request(`/api/tasks/${taskId}/cad-review/import`, {
      method: "POST",
      body: JSON.stringify({ data }),
    });
  }

  /** 按对照表重新写回这些图纸：后台进行，进度从 getCadReview().rewrite 读。 */
  async rewriteCadReview(taskId: string, files: string[]): Promise<{ accepted: number }> {
    return this.request(`/api/tasks/${taskId}/cad-review/rewrite`, {
      method: "POST",
      body: JSON.stringify({ files }),
    });
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

  /** 终态任务专用：regeneratePdfPage 是「排队，继续翻译时生效」，这一条是「现在就重跑
   *  这一页，并把输出文件重新合成一遍」。 */
  async rerunPdfPage(taskId: string, file: string, page: number): Promise<void> {
    await this.request(`/api/tasks/${taskId}/pdf-pages/rerun`, {
      method: "POST",
      body: JSON.stringify({ file, page }),
    });
  }

  /** 终态任务专用：把这一页换回上一版译文，并按新的页图重新装配输出文件。
   *  不调用模型、不计费；后端做完才返回，所以这个 await 会一直等到输出文件换好。 */
  async restorePdfPagePrevious(taskId: string, file: string, page: number): Promise<void> {
    await this.request(`/api/tasks/${taskId}/pdf-pages/restore-previous`, {
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
  async getPdfPageImage(taskId: string, file: string, page: number, kind: "source" | "translated" | "previous"): Promise<Blob> {
    const url = `${this.#baseUrl}/api/tasks/${taskId}/pdf-pages/image?file=${encodeURIComponent(file)}&page=${page}&kind=${kind}`;
    const response = await fetch(url, { headers: { "X-Translator-Token": this.#token } });
    if (!response.ok) {
      const fallback = `${response.status} ${response.statusText}`;
      const payload = await response.json().catch(() => ({ detail: fallback }));
      throw new Error(errorDetail(payload, fallback));
    }
    return response.blob();
  }
}
