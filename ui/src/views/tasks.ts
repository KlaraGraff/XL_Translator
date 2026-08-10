// 任务中心视图 —— 列表 + 任务详情，唯一全量任务视图（对应样张屏⑥）。
// 从 main.ts 移植：isTaskActive / taskStateMeta / taskSnapshotRows / taskResultReferences /
// upsertTask / watchTask / handleTaskEvent 等任务生命周期语义（按原样语义移植，非逐行照抄）。
//
// 架构要点：本模块在 app.ts 启动时被 import 一次（顶层代码立即执行），因此下面的
// “后台巡检”（ensureBackgroundLoop）在应用启动的第一时间就开始运行，与是否正挂载在
// 屏幕上无关——这样活动任务徽标（shell.setTaskBadge/setTaskPill）才能在用户停留在
// 其它视图时依然保持准确，满足“任务中心数据是徽标权威来源”的要求。凡是会触碰当前
// 视图 DOM 的渲染函数都额外用 `mounted` 门控，避免后台事件在别的视图挂载期间乱写 DOM。

import type { ViewParams } from "../router";
import { navigate } from "../router";
import { setTopbar, setTaskBadge, setTaskPill } from "../shell";
import {
  createChip,
  createButton,
  createEmptyState,
  openModal,
  showToast,
  type ChipTone,
} from "../components";
import { icon, type IconName } from "../icons";
import { ApiClient, type SseEvent, type TaskStatus } from "../api-client";
import { invoke } from "@tauri-apps/api/core";

// ---------------------------------------------------------------------------
// 基础类型 / 工具函数（与 main.ts 同名函数语义一致，独立实现以保持视图自包含）
// ---------------------------------------------------------------------------

type JsonObject = Record<string, unknown>;
type TaskSurface = TaskStatus["surface"];
type StreamState = "idle" | "connected" | "reconnecting" | "interrupted";

const TERMINAL_STATES = ["done", "completed_with_issues", "error", "stopped", "interrupted"];

function record(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" ? value : fallback;
}

function firstText(payload: JsonObject, keys: string[]): string {
  for (const key of keys) {
    const value = text(payload[key]);
    if (value) return value;
  }
  return "";
}

function firstNumber(payload: JsonObject, keys: string[]): number | null {
  for (const key of keys) {
    if (typeof payload[key] === "number") return payload[key] as number;
  }
  return null;
}

function resultEntries(result: JsonObject, keys: string[]): JsonObject[] {
  for (const key of keys) {
    const value = result[key];
    if (Array.isArray(value) && value.length) return value.map(record);
  }
  return [];
}

/** 脱敏：授权头 / sk-/rk-/pk-/api- 前缀密钥 / Bearer token 一律替换。 */
function redactedText(value: unknown, fallback = ""): string {
  const raw = text(value, fallback);
  if (!raw) return raw;
  return raw
    .replace(/(authorization\s*[:=]\s*)([^\s,;]+)/gi, "$1[redacted]")
    .replace(/\b(sk|rk|pk|api)[-_][a-z0-9_-]{8,}\b/gi, "[redacted]")
    .replace(/\bBearer\s+[^\s,;]+/gi, "Bearer [redacted]");
}

function isTaskActive(task: TaskStatus): boolean {
  return !task.terminal && !TERMINAL_STATES.includes(task.state);
}

function surfaceIcon(surface: TaskSurface): IconName {
  if (surface === "excel") return "excel";
  if (surface === "word") return "word";
  if (surface === "pdf") return "pdf";
  return "book"; // cleaner / tm_clean：图标集里没有专属图标，用记忆库图标近似。
}

function taskSurfaceLabel(surface: TaskSurface): string {
  if (surface === "excel") return "Excel";
  if (surface === "word") return "Word";
  if (surface === "pdf") return "PDF";
  return "TM 清洗";
}

const STATE_LABELS: Record<string, string> = {
  preflight: "等待确认",
  running: "执行中",
  pausing: "暂停提交中",
  paused: "已暂停提交",
  stopping: "安全停止中",
  finalizing: "正在收尾",
  done: "已完成",
  completed_with_issues: "完成但有问题",
  error: "发生错误",
  stopped: "已中止",
  interrupted: "应用中断",
};

const TONE_MAP: Record<string, ChipTone> = {
  preflight: "tint",
  running: "tint",
  pausing: "tint",
  paused: "warn",
  stopping: "tint",
  finalizing: "tint",
  done: "ok",
  completed_with_issues: "warn",
  error: "dgr",
  stopped: "mute",
  interrupted: "dgr",
};

function taskStateMeta(task: TaskStatus, streamState?: StreamState): { label: string; tone: ChipTone } {
  if (streamState === "reconnecting" && isTaskActive(task)) {
    return { label: "正在补拉事件", tone: "tint" };
  }
  if (streamState === "interrupted") {
    return { label: "应用中断", tone: "dgr" };
  }
  return { label: STATE_LABELS[task.state] ?? task.state, tone: TONE_MAP[task.state] ?? "mute" };
}

function formatClock(epoch?: number): string {
  if (!epoch) return "--:--";
  return new Date(epoch * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
}

function formatDay(epoch?: number): string {
  if (!epoch) return "";
  const date = new Date(epoch * 1000);
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return formatClock(epoch);
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return `昨天 ${formatClock(epoch)}`;
  return `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${formatClock(epoch)}`;
}

// ---------------------------------------------------------------------------
// 任务快照 / KPI / 结果定位清单 / 产物引用 —— 移植自 main.ts 的同名函数
// ---------------------------------------------------------------------------

function taskResultReferences(task: TaskStatus): Array<{ label: string; path: string; reveal: boolean }> {
  const result = record(task.result);
  const summary = record(result.summary);
  const source = { ...summary, ...result };
  const refs: Array<{ label: string; path: string; reveal: boolean }> = [];
  const actionLabels: Record<string, string> = {
    open_output: "打开输出目录",
    reveal_output: "在 Finder 中显示主输出",
    open_report: "打开报告",
    open_manifest: "打开清单",
    copy_output_path: "复制输出路径",
  };
  for (const operation of resultEntries(result, ["local_operations"])) {
    const action = text(operation.action);
    const path = firstText(operation, ["path"]);
    if (path && path !== "[path]" && !refs.some((entry) => entry.path === path)) {
      refs.push({ label: actionLabels[action] ?? "打开本地结果", path, reveal: action === "reveal_output" });
    }
  }
  const add = (label: string, keys: string[], reveal = false) => {
    const path = firstText(source, keys);
    if (path && !refs.some((entry) => entry.path === path)) refs.push({ label, path, reveal });
  };
  add("打开输出目录", ["output_dir", "output_directory"]);
  add("在 Finder 中显示主输出", ["output_path", "result_path", "output"], true);
  add("打开报告", ["report_path", "word_translation_report_path", "pdf_translation_report_path"]);
  add("打开清单", ["manifest_path", "pdf_translation_manifest_path"]);
  return refs;
}

function taskSnapshotRows(task: TaskStatus): Array<[string, string]> {
  const snapshot = record(task.task_snapshot);
  const modelSnapshot = record(task.model_snapshot);
  const language = record(snapshot.language);
  const output = record(snapshot.output);
  const resources = [
    ...(Array.isArray(task.resource_groups) ? task.resource_groups : []),
    ...(Array.isArray(snapshot.connections) ? snapshot.connections : []),
  ];
  const models = Object.entries(modelSnapshot)
    .map(([role, value]) => `${role}: ${redactedText(record(value).model, "已冻结")}`)
    .filter(Boolean)
    .join("；");
  const sourceLang = firstText({ ...snapshot, ...language }, ["source_lang", "source_selection"]);
  const targetLang = firstText({ ...snapshot, ...language }, ["target_lang"]);
  const domain = firstText(snapshot, ["domain_preset", "domain"]);
  const promptVersion = firstText(snapshot, ["prompt_version", "domain_prompt_version"]);
  const configuredOutput = record(snapshot.excel_output || snapshot.word_output || snapshot.pdf_output);
  const outputPath =
    firstText({ ...snapshot, ...output, ...configuredOutput }, ["output_dir", "output_directory", "custom_output_dir"]) ||
    (configuredOutput.use_custom_output_dir === false ? "与源文件相邻的唯一输出目录" : "任务唯一输出目录");
  const throughput = record(snapshot.throughput);
  const connectionSummary = resources
    .map((group) => {
      const value = record(group);
      const summary = record(value.summary);
      return redactedText(
        value.label ||
          value.connection_summary ||
          value.id ||
          [summary.provider || value.provider, summary.base_url || value.base_url].filter(Boolean).join(" @ "),
      );
    })
    .filter(Boolean)
    .join("；");
  const rows: Array<[string, string]> = [
    ["语言", [sourceLang, targetLang].filter(Boolean).join(" → ")],
    ["模型", [models, connectionSummary].filter(Boolean).join(" · ")],
    ["领域 / Prompt", [domain, promptVersion].filter(Boolean).join(" · ")],
    ["输出位置", outputPath],
    [
      "吞吐",
      Object.keys(throughput).length
        ? Object.entries(throughput).map(([key, value]) => `${key} ${value}`).join("；")
        : Object.entries(modelSnapshot)
            .map(([role, value]) => `${role} ${num(record(record(value).throughput).concurrency, 1)}`)
            .join("；"),
    ],
  ];
  return rows.filter(([, value]) => Boolean(value));
}

function pickKpis(pairs: Array<[string, string[]]>, source: JsonObject): Array<[string, number]> {
  const out: Array<[string, number]> = [];
  for (const [label, keys] of pairs) {
    const value = firstNumber(source, keys);
    if (value !== null) out.push([label, value]);
  }
  return out;
}

/** 富 KPI 集合：按 surface 采用与 main.ts renderXxxResultDetails 相同的字段回退表。 */
function richKpiRows(task: TaskStatus): Array<[string, number]> {
  const result = record(task.result);
  const summary = record(result.summary);
  const kpi = record(result.kpi);
  const source = { ...result, ...summary, ...kpi };
  if (task.surface === "excel") {
    return pickKpis(
      [
        ["已选", ["selected_count", "selected_files", "selected_file_count", "total_files"]],
        ["成功", ["success_count", "completed_count", "successful_files", "succeeded_file_count"]],
        ["失败", ["failed_count", "error_count", "failed_files", "failed_file_count"]],
        ["未开始", ["unstarted_count", "not_started_count", "unstarted_file_count"]],
        ["TM 命中", ["tm_hit_count", "tm_hits"]],
        ["送模型文本", ["model_translation_text_count", "model_text_count", "translated_text_count"]],
      ],
      source,
    );
  }
  if (task.surface === "word") {
    const recovery = record(result.recovery);
    const withRecovery = { ...source, ...recovery };
    return pickKpis(
      [
        ["已选", ["selected_count", "selected_files", "selected_file_count", "total_files"]],
        ["成功", ["success_count", "completed_count", "successful_files", "succeeded_file_count"]],
        ["失败", ["failed_count", "error_count", "failed_files", "failed_file_count"]],
        ["需复核", ["review_count", "review_items_count", "review_total", "review_text_count"]],
        ["TM 命中", ["tm_hit_count", "tm_hits"]],
      ],
      source,
    ).concat(
      pickKpis(
        [
          ["严格恢复", ["retry_recovered_count", "recovered_count"]],
          ["仲裁接受", ["semantic_accepted_count"]],
        ],
        withRecovery,
      ),
    );
  }
  if (task.surface === "pdf") {
    return pickKpis(
      [
        ["文件", ["file_count", "selected_file_count"]],
        ["页 / 图片", ["total_page_count", "page_count"]],
        ["高清 PDF", ["generated_pdf_count"]],
        ["译图", ["generated_image_count"]],
        ["失败占位", ["placeholder_page_count"]],
        ["审核未通过", ["review_failed_page_count"]],
      ],
      source,
    );
  }
  return pickKpis(
    [
      ["已选", ["selected_count", "selected_file_count", "file_count", "total_files"]],
      ["成功", ["success_count", "successful_files", "succeeded_file_count", "completed_count"]],
      ["失败", ["failed_count", "failed_file_count", "error_count"]],
      ["未开始", ["unstarted_count", "unstarted_file_count", "not_started_count"]],
    ],
    source,
  );
}

const WARN_KPI_LABELS = new Set(["需复核", "失败", "审核未通过", "失败占位"]);

interface ReviewRow {
  file: string;
  location: string;
  excerpt: string;
  issue: string;
  action: string;
  needsReview: boolean;
}

/** 结果定位清单：合并 Excel（工作表/单元格）与 Word（章节/位置/摘录）两种复核行形状。
 *  main.ts 里 Excel 复核行本没有摘录列，这里仍尝试常见摘录键，取不到就留空——
 *  这是为了让同一张表能承接两种 surface 的数据，而不改变各自后端已产出的字段。 */
function reviewRows(task: TaskStatus): ReviewRow[] {
  const result = record(task.result);
  const reviewPayload = record(result.review);
  const raw = resultEntries(result, ["review_items", "review_locations", "review_details", "issues"]).concat(
    resultEntries(reviewPayload, ["items", "locations", "details", "issues"]),
  );
  const rows = raw.map((entry): ReviewRow => {
    const file = firstText(entry, ["file", "source_relative_path", "relative_path", "path"]);
    const location = [
      firstText(entry, ["worksheet", "sheet", "sheet_name", "section", "chapter", "section_path", "heading"]),
      firstText(entry, ["cell", "cell_reference", "location", "paragraph", "paragraph_index", "table_cell"]),
    ]
      .filter(Boolean)
      .join(" · ");
    const excerptRaw = firstText(entry, ["excerpt", "snippet", "source_excerpt", "text", "source_text"]);
    const excerpt = excerptRaw.length > 80 ? `${excerptRaw.slice(0, 77)}…` : excerptRaw;
    const issue = firstText(entry, ["category", "mark", "type", "issue", "problem"]) || "—";
    const action = firstText(entry, ["action", "applied_action", "review_status", "message"]) || "—";
    const needsReview = /复核|未通过|待/.test(action) || /复核|未通过|待/.test(issue);
    return { file, location, excerpt, issue, action, needsReview };
  });
  return rows.sort((a, b) => Number(b.needsReview) - Number(a.needsReview));
}

/** 「处理」列的短结论标签 + 配色。
 *  措辞只从后端实际写下的 action 文案里归纳，标签与颜色同一处产出，
 *  避免两套规则各说各话；action 为空或「—」时返回 null，只显示一个「—」。 */
function reviewActionMeta(action: string): { label: string; tone: ChipTone } | null {
  const value = action.trim();
  if (!value || value === "—") return null;
  if (/保留原文/.test(value)) return { label: "已保留原文", tone: "dgr" };
  if (/失败|拒绝/.test(value)) return { label: "未处理", tone: "dgr" };
  if (/复核|未通过|待/.test(value)) return { label: "建议复核", tone: "warn" };
  if (/接受|通过|恢复|采用/.test(value)) return { label: "已接受", tone: "ok" };
  if (/写入|应用/.test(value)) return { label: "已写入", tone: "ok" };
  return { label: "已处理", tone: "mute" };
}

interface FileRow {
  name: string;
  status: string;
  output: string;
  error: string;
}

function fileRows(task: TaskStatus): FileRow[] {
  const result = record(task.result);
  const files = resultEntries(result, ["files", "file_results", "file_records"]);
  return files.map((entry) => ({
    name: firstText(entry, ["source_relative_path", "relative_path", "name"]),
    status:
      firstText(entry, ["status", "state", "terminal_state"]) ||
      (entry.success === true ? "成功" : entry.success === false ? "失败" : "结果未知"),
    output: firstText(entry, ["output_path", "result_path", "output", "translated_image_path"]),
    error: firstText(entry, ["error", "error_message", "message", "detail"]),
  }));
}

// ---------------------------------------------------------------------------
// 模块状态（顶层单例——整个应用生命周期只有一份，视图挂载/卸载不清空）
// ---------------------------------------------------------------------------

interface TaskEntry {
  task: TaskStatus;
  phaseName: string;
  stepDone: number;
  stepTotal: number;
  lastEventId: number;
  streamState: StreamState;
  watcherActive: boolean;
}

const tasks = new Map<string, TaskEntry>();
let order: string[] = [];
let filter: "all" | "active" | "terminal" = "all";
let selectedId: string | null = null;

let mounted = false;
let listRootEl: HTMLDivElement | null = null;
let filtersEl: HTMLDivElement | null = null;
let detailRootEl: HTMLDivElement | null = null;
let fastPollTimer: number | null = null;

let connectPromise: Promise<ApiClient> | null = null;

async function getClient(): Promise<ApiClient> {
  if (!connectPromise) {
    const instance = new ApiClient();
    connectPromise = instance
      .connect()
      .then(() => instance)
      .catch((error) => {
        connectPromise = null;
        throw error;
      });
  }
  return connectPromise;
}

function upsert(task: TaskStatus): TaskEntry {
  const previous = tasks.get(task.task_id);
  const entry: TaskEntry = {
    task: { ...previous?.task, ...task },
    phaseName: previous?.phaseName ?? "正在准备任务",
    stepDone: previous?.stepDone ?? 0,
    stepTotal: previous?.stepTotal ?? 0,
    lastEventId: previous?.lastEventId ?? 0,
    streamState: previous?.streamState ?? "idle",
    watcherActive: previous?.watcherActive ?? false,
  };
  tasks.set(task.task_id, entry);
  order = [task.task_id, ...order.filter((id) => id !== task.task_id)];
  return entry;
}

function activeCount(): number {
  let count = 0;
  for (const id of order) {
    const entry = tasks.get(id);
    if (entry && isTaskActive(entry.task)) count += 1;
  }
  return count;
}

/** 徽标是全局状态（侧栏 + 顶栏右侧药丸），无论任务中心是否正挂载都要保持准确。 */
function updateBadge(): void {
  const count = activeCount();
  setTaskBadge(count);
  setTaskPill({ count });
}

function refreshTopbarStatus(): void {
  if (!mounted) return;
  const count = activeCount();
  setTopbar({
    title: "任务中心",
    status: count > 0 ? { label: `${count} 个活动任务`, tone: "run" } : { label: "空闲", tone: "idle" },
    subtitle: "运行中的任务与最近结果都在这里",
  });
}

function touch(taskId?: string): void {
  updateBadge();
  refreshTopbarStatus();
  if (!mounted) return;
  renderList();
  if (!taskId || selectedId === taskId) renderDetail();
}

// ---------------------------------------------------------------------------
// 事件流 / 轮询
// ---------------------------------------------------------------------------

async function watchTask(taskId: string): Promise<void> {
  const entry = tasks.get(taskId);
  if (!entry || entry.watcherActive || entry.task.terminal) return;
  entry.watcherActive = true;
  try {
    const client = await getClient();
    entry.lastEventId = await client.streamTask(taskId, (event) => handleEvent(taskId, event), {
      lastEventId: entry.lastEventId,
      onConnectionState: (state) => {
        const live = tasks.get(taskId);
        if (!live) return;
        live.streamState = state;
        touch(taskId);
      },
    });
    const latest = tasks.get(taskId);
    if (latest && !latest.task.terminal) {
      const refreshed = await client.getTask(taskId);
      upsert(refreshed);
      tasks.get(taskId)!.streamState = "connected";
    }
  } catch (error) {
    const latest = tasks.get(taskId);
    if (!latest) return;
    try {
      const client = await getClient();
      const refreshed = await client.getTask(taskId);
      upsert(refreshed);
      tasks.get(taskId)!.streamState = "connected";
      if (!refreshed.terminal) {
        window.setTimeout(() => void watchTask(taskId), 0);
        return;
      }
    } catch {
      latest.task = { ...latest.task, state: "interrupted", terminal: true };
      latest.streamState = "interrupted";
      latest.phaseName = "sidecar 已重启或应用异常退出；本任务不能继续，请依据已生成产物或清单新建任务。";
      if (mounted) showToast({ message: "任务监控无法恢复，已标记为应用中断。", error: true });
    }
  } finally {
    const latest = tasks.get(taskId);
    if (latest) latest.watcherActive = false;
    touch(taskId);
  }
}

function handleEvent(taskId: string, event: SseEvent): void {
  const entry = tasks.get(taskId);
  if (!entry) return;
  entry.lastEventId = Math.max(entry.lastEventId, event.id);
  const data = event.data;
  if (event.type === "progress") {
    entry.phaseName = text(data.phase_name, "正在处理");
    entry.stepDone = num(data.step_done);
    entry.stepTotal = num(data.step_total);
  }
  if (event.type === "status") {
    entry.phaseName = text(data.phase_desc, entry.phaseName);
  }
  if (event.type === "stopping") entry.task = { ...entry.task, state: "stopping" };
  if (event.type === "paused") {
    entry.task = { ...entry.task, state: "paused" };
    entry.phaseName = "已暂停提交新页面";
  }
  if (event.type === "resumed") {
    entry.task = { ...entry.task, state: "running" };
    entry.phaseName = "正在继续提交页面";
  }
  if (TERMINAL_STATES.includes(event.type)) {
    entry.task = { ...entry.task, state: event.type as TaskStatus["state"], terminal: true, result: data };
    if (mounted) {
      const label = taskSurfaceLabel(entry.task.surface);
      showToast({
        message: event.type === "done" || event.type === "completed_with_issues" ? `${label} 任务已完成。` : text(data.message, "任务未完成。"),
        error: event.type === "error" || event.type === "stopped" || event.type === "interrupted",
      });
    }
  }
  touch(taskId);
}

async function refreshRegistry(): Promise<void> {
  try {
    const client = await getClient();
    const payload = await client.listTasks();
    const combined = [...(Array.isArray(payload.active) ? payload.active : []), ...(Array.isArray(payload.recent) ? payload.recent : [])];
    for (const task of combined) {
      upsert(task);
      if (isTaskActive(task)) void watchTask(task.task_id);
    }
    if (!selectedId && order.length) selectedId = order[0];
    touch();
  } catch {
    // sidecar 尚未就绪或临时不可达；下一次巡检自然会重试，这里不打扰用户。
  }
}

let backgroundStarted = false;

/** 后台巡检：应用启动时立即开始（模块被 app.ts 顶层 import 时触发），
 *  与任务中心视图是否挂载无关——这样徽标在其它视图上也能保持准确。 */
function ensureBackgroundLoop(): void {
  if (backgroundStarted) return;
  backgroundStarted = true;
  void refreshRegistry();
  window.setInterval(() => void refreshRegistry(), 12_000);
}
ensureBackgroundLoop();

// ---------------------------------------------------------------------------
// 任务操作（停止 / PDF 暂停恢复 / 打开本地文件 / 复制路径）
// ---------------------------------------------------------------------------

async function stopTask(taskId: string): Promise<void> {
  const entry = tasks.get(taskId);
  if (!entry) return;
  const client = await getClient();
  entry.task = await client.request<TaskStatus>(`/api/tasks/${taskId}/stop`, { method: "POST" });
  touch(taskId);
}

async function pausePdfTask(taskId: string): Promise<void> {
  const entry = tasks.get(taskId);
  if (!entry || entry.task.surface !== "pdf") return;
  const client = await getClient();
  entry.task = await client.request<TaskStatus>(`/api/tasks/${taskId}/pause`, { method: "POST" });
  touch(taskId);
}

async function resumePdfTask(taskId: string): Promise<void> {
  const entry = tasks.get(taskId);
  if (!entry || entry.task.surface !== "pdf") return;
  const client = await getClient();
  entry.task = await client.request<TaskStatus>(`/api/tasks/${taskId}/resume`, { method: "POST" });
  touch(taskId);
}

async function endPausedPdfTask(taskId: string): Promise<void> {
  const entry = tasks.get(taskId);
  if (!entry || entry.task.surface !== "pdf") return;
  if (!window.confirm("结束暂停任务将不再提交未处理页面，但会写入并保留已完成页面、素材、清单和报告。是否结束？")) return;
  const client = await getClient();
  entry.task = await client.request<TaskStatus>(`/api/tasks/${taskId}/end-paused`, { method: "POST" });
  touch(taskId);
}

async function openTaskLocalFile(path: string, reveal: boolean): Promise<void> {
  if (!path.trim()) return;
  try {
    await invoke("open_local_path", { path, reveal });
  } catch (error) {
    showToast({ message: `无法打开：${error instanceof Error ? error.message : String(error)}`, error: true });
  }
}

async function exportTaskDiagnostic(taskId: string): Promise<void> {
  try {
    const client = await getClient();
    await client.downloadBinary(
      `/api/diagnostics/task/${encodeURIComponent(taskId)}.zip`,
      `translator-diagnostic-${taskId.slice(0, 8)}.zip`,
    );
  } catch (error) {
    showToast({ message: `导出诊断失败：${error instanceof Error ? error.message : String(error)}`, error: true });
  }
}

async function copyTaskPath(path: string): Promise<void> {
  if (!path.trim() || !navigator.clipboard?.writeText) return;
  await navigator.clipboard.writeText(path);
  showToast({ message: "路径已复制。" });
}

function confirmStopTask(taskId: string): void {
  openModal({
    tone: "warn",
    icon: "warn",
    title: "安全停止当前任务？",
    body: ["已生成的文件会保留在输出目录；Excel、Word 会结束为终态。PDF / 图片应先“暂停提交”，再选择继续或结束暂停。"],
    actions: [
      { label: "继续执行", variant: "default" },
      { label: "安全停止", variant: "danger-solid", onClick: () => stopTask(taskId) },
    ],
  });
}

function confirmDeleteTask(taskId: string): void {
  openModal({
    tone: "warn",
    icon: "trash",
    title: "删除这条任务记录？",
    body: ["只从任务中心移除这条记录。已经生成的译文、报告和诊断包都留在原处，不会被删除。"],
    actions: [
      { label: "取消", variant: "default" },
      { label: "删除记录", variant: "danger-solid", onClick: () => void deleteTaskRecord(taskId) },
    ],
  });
}

async function deleteTaskRecord(taskId: string): Promise<void> {
  try {
    const client = await getClient();
    await client.request(`/api/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
  } catch (error) {
    showToast({ message: `删除失败：${error instanceof Error ? error.message : String(error)}`, error: true });
    return;
  }
  // 只有终态任务能删（后端 409 挡住运行中的），所以这里没有事件流要收——
  // watchTask() 对 terminal 任务直接返回，不会有 watcher 把记录 upsert 回来。
  tasks.delete(taskId);
  order = order.filter((id) => id !== taskId);
  if (selectedId === taskId) selectedId = order[0] ?? null;
  renderList();
  renderDetail();
  showToast({ message: "记录已删除；输出文件没有被动过。" });
}

// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------

const FILTERS: Array<["all" | "active" | "terminal", string]> = [
  ["all", "全部"],
  ["active", "活动"],
  ["terminal", "最近结果"],
];

function visibleOrder(): string[] {
  return order.filter((id) => {
    const entry = tasks.get(id);
    if (!entry) return false;
    if (filter === "all") return true;
    return filter === "active" ? isTaskActive(entry.task) : !isTaskActive(entry.task);
  });
}

/**
 * completed_with_issues 这一个状态的统一措辞：一律「需复核」，数得出条数就带上条数。
 * 之前列表卡片有条目时说「需复核 11」、没条目时说「完成但有问题」，详情页又说
 * 「完成但有问题 · 需复核 11」——同一个状态在三处三种说法，用户没法判断是不是三件事。
 */
function reviewChip(task: TaskStatus): { label: string; tone: ChipTone } | null {
  if (task.state !== "completed_with_issues") return null;
  const count = reviewRows(task).filter((row) => row.needsReview).length;
  return { label: count > 0 ? `需复核 ${count}` : "需复核", tone: "warn" };
}

function cardChip(entry: TaskEntry): { label: string; tone: ChipTone } {
  const meta = taskStateMeta(entry.task, entry.streamState);
  if (isTaskActive(entry.task)) {
    const percent = entry.stepTotal > 0 ? Math.round((entry.stepDone / entry.stepTotal) * 100) : null;
    return { label: percent !== null ? `${percent}%` : meta.label, tone: "tint" };
  }
  return reviewChip(entry.task) ?? meta;
}

function cardSubtitle(entry: TaskEntry): string {
  const meta = taskStateMeta(entry.task, entry.streamState);
  if (isTaskActive(entry.task)) {
    return `${meta.label} · ${entry.phaseName || "正在准备任务"} · ${formatClock(entry.task.created_at)} 开始`;
  }
  const successCount = firstNumber(record({ ...record(entry.task.result), ...record(record(entry.task.result).summary) }), [
    "success_count",
    "completed_count",
    "successful_files",
  ]);
  const detail = successCount !== null ? `${successCount} 个文件已生成` : meta.label;
  return `${detail} · ${formatDay(entry.task.updated_at ?? entry.task.created_at)}`;
}

function renderCard(id: string): HTMLDivElement {
  const entry = tasks.get(id)!;
  const card = document.createElement("div");
  card.className = id === selectedId ? "card tkcard sel" : "card tkcard";
  card.addEventListener("click", () => {
    selectedId = id;
    renderList();
    renderDetail();
  });

  const r1 = document.createElement("div");
  r1.className = "r1";
  const ic = icon(surfaceIcon(entry.task.surface), { size: "sm" });
  r1.append(ic);
  r1.append(
    document.createTextNode(
      `${taskSurfaceLabel(entry.task.surface)} · ${text(entry.task.source_label, `任务 ${entry.task.task_id.slice(0, 8)}`)}`,
    ),
  );
  const chipInfo = cardChip(entry);
  const chip = createChip({ label: chipInfo.label, tone: chipInfo.tone });
  chip.style.marginLeft = "auto";
  r1.append(chip);
  card.append(r1);

  const r2 = document.createElement("div");
  r2.className = "r2";
  r2.textContent = cardSubtitle(entry);
  card.append(r2);

  if (isTaskActive(entry.task) && entry.stepTotal > 0) {
    const bar = document.createElement("div");
    bar.className = "bar";
    bar.style.marginTop = "9px";
    const fill = document.createElement("i");
    fill.style.width = `${Math.round((entry.stepDone / entry.stepTotal) * 100)}%`;
    bar.append(fill);
    card.append(bar);
  }
  return card;
}

function renderList(): void {
  if (!listRootEl) return;
  while (listRootEl.children.length > 1) listRootEl.removeChild(listRootEl.lastChild!);
  const ids = visibleOrder();
  if (!ids.length) {
    const empty = createEmptyState({
      title: "当前没有可显示的任务",
      description: "启动 Excel、Word、PDF / 图片翻译或记忆库深度清洗后，状态会保留在这里。",
      icon: "tasks",
    });
    empty.style.flex = "1";
    listRootEl.append(empty);
    return;
  }
  for (const id of ids) {
    listRootEl.append(renderCard(id));
  }
}

function buildActionButton(reference: { label: string; path: string; reveal: boolean }): HTMLButtonElement {
  return createButton({
    label: reference.label,
    icon: "folder",
    size: "mini",
    onClick: () => void openTaskLocalFile(reference.path, reference.reveal),
  });
}

function renderDetail(): void {
  if (!detailRootEl) return;
  detailRootEl.innerHTML = "";
  const entry = selectedId ? tasks.get(selectedId) : null;
  if (!entry) {
    const empty = createEmptyState({
      title: "选择左侧任务查看详情",
      description: "结果快照、KPI、结果定位清单与产物入口都在这里。",
      icon: "tasks",
    });
    empty.style.flex = "1";
    detailRootEl.append(empty);
    return;
  }
  const { task } = entry;

  const header = document.createElement("div");
  header.style.display = "flex";
  header.style.alignItems = "center";
  header.style.gap = "10px";
  header.style.flexWrap = "wrap";
  const title = document.createElement("h2");
  title.style.fontSize = "15px";
  title.style.fontWeight = "650";
  title.textContent = `${taskSurfaceLabel(task.surface)} · ${text(task.source_label, `任务 ${task.task_id.slice(0, 8)}`)}`;
  header.append(title);

  const meta = taskStateMeta(task, entry.streamState);
  const reviews = reviewRows(task);
  header.append(createChip(reviewChip(task) ?? meta));

  const actions = document.createElement("div");
  actions.style.marginLeft = "auto";
  actions.style.display = "flex";
  actions.style.gap = "8px";
  actions.style.flexWrap = "wrap";
  const references = taskResultReferences(task);
  for (const reference of references) {
    actions.append(buildActionButton(reference));
  }
  if (task.terminal) {
    actions.append(
      createButton({
        label: "导出诊断",
        size: "mini",
        // 详情页上的这个按钮说的是「这一次任务」，就该直接把这一次的诊断包存下来。
        // 之前它把人扔到设置页去自己找——找的还是全部任务的合集。
        onClick: () => void exportTaskDiagnostic(task.task_id),
      }),
    );
  }
  if (task.terminal && (task.surface === "tm_clean" || task.surface === "cleaner")) {
    actions.append(
      createButton({
        label: "查看清洗建议",
        icon: "book",
        size: "mini",
        onClick: () => navigate("library", { reviewCleanTaskId: task.task_id }),
      }),
    );
  }
  const canOpenWorkspace = task.surface === "excel" || task.surface === "word" || task.surface === "pdf";
  if (canOpenWorkspace) {
    actions.append(
      createButton({
        label: "打开工作区",
        icon: "play",
        size: "mini",
        onClick: () => navigate(task.surface as "excel" | "word" | "pdf"),
      }),
    );
  }
  if (isTaskActive(task)) {
    if (task.surface === "pdf" && task.state === "paused") {
      actions.append(createButton({ label: "继续翻译", variant: "primary", size: "mini", onClick: () => void resumePdfTask(task.task_id) }));
      actions.append(createButton({ label: "结束暂停", variant: "danger", size: "mini", onClick: () => void endPausedPdfTask(task.task_id) }));
    } else if (task.surface === "pdf") {
      actions.append(createButton({ label: "暂停提交", size: "mini", onClick: () => void pausePdfTask(task.task_id) }));
    } else {
      actions.append(createButton({ label: "安全停止", variant: "danger", size: "mini", onClick: () => confirmStopTask(task.task_id) }));
    }
  }
  if (task.terminal) {
    // 只删任务中心的这条记录；译文、报告、诊断包都在磁盘上原样留着。
    actions.append(createButton({ label: "删除记录", icon: "trash", variant: "danger", size: "mini", onClick: () => confirmDeleteTask(task.task_id) }));
  }
  header.append(actions);
  detailRootEl.append(header);

  const snapshots = taskSnapshotRows(task);
  if (snapshots.length) {
    const kv = document.createElement("dl");
    kv.className = "kv";
    for (const [label, value] of snapshots) {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      dd.title = value;
      if (label === "输出位置") {
        const copyLink = document.createElement("span");
        copyLink.className = "linklike";
        copyLink.textContent = " 复制路径";
        copyLink.addEventListener("click", (event) => {
          event.stopPropagation();
          void copyTaskPath(value);
        });
        dd.append(copyLink);
      }
      kv.append(dt, dd);
    }
    detailRootEl.append(kv);
  }

  const kpis = richKpiRows(task);
  if (kpis.length) {
    const grid = document.createElement("div");
    grid.className = "kpis";
    for (const [label, value] of kpis) {
      const tile = document.createElement("div");
      tile.className = WARN_KPI_LABELS.has(label) && value > 0 ? "kpi warn" : "kpi";
      const span = document.createElement("span");
      span.textContent = label;
      const b = document.createElement("b");
      b.textContent = String(value);
      tile.append(span, b);
      grid.append(tile);
    }
    detailRootEl.append(grid);
  }

  if (reviews.length) {
    const sectionTitle = document.createElement("div");
    sectionTitle.style.fontSize = "13px";
    sectionTitle.style.fontWeight = "650";
    sectionTitle.style.margin = "6px 0 8px";
    sectionTitle.textContent = "结果定位清单 ";
    const hint = document.createElement("span");
    hint.style.fontWeight = "400";
    hint.style.color = "var(--ink-3)";
    hint.style.fontSize = "12px";
    hint.textContent = "需复核在前 · 按行定位到对应文件与位置";
    sectionTitle.append(hint);
    detailRootEl.append(sectionTitle);

    const table = document.createElement("table");
    table.className = "tbl review-tbl";
    // 固定列宽：任务详情是窄面板，交给浏览器按最小内容宽度分配会把文件名压成竖排单字。
    const cols = document.createElement("colgroup");
    for (const width of ["20%", "14%", "26%", "12%", "28%"]) {
      const col = document.createElement("col");
      col.style.width = width;
      cols.append(col);
    }
    table.append(cols);
    const headRow = document.createElement("tr");
    for (const label of ["文件", "位置", "原文摘录", "问题", "处理"]) {
      const th = document.createElement("th");
      th.textContent = label;
      headRow.append(th);
    }
    table.append(headRow);
    for (const row of reviews.slice(0, 50)) {
      const tr = document.createElement("tr");
      // 文件名单行显示、超出省略；完整名字放 title，悬停可看全。
      const fileTd = document.createElement("td");
      fileTd.className = "fname";
      fileTd.textContent = row.file || "—";
      if (row.file) fileTd.title = row.file;
      tr.append(fileTd);
      for (const value of [row.location || "—", row.excerpt || "—", row.issue]) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.append(td);
      }
      const actionTd = document.createElement("td");
      const actionMeta = reviewActionMeta(row.action);
      if (!actionMeta) {
        actionTd.textContent = "—";
      } else {
        // 徽章按两三个字设计，整句说明塞进去会直接溢出：
        // 第一行只放短结论，完整说明降级成第二行的灰色小字。
        const stack = document.createElement("div");
        stack.className = "review-act";
        stack.append(createChip({ label: actionMeta.label, tone: actionMeta.tone }));
        if (row.action.trim() !== actionMeta.label) {
          const note = document.createElement("div");
          note.className = "note";
          note.textContent = row.action;
          stack.append(note);
        }
        actionTd.append(stack);
      }
      tr.append(actionTd);
      table.append(tr);
    }
    detailRootEl.append(table);
    if (reviews.length > 50) {
      const note = document.createElement("p");
      note.style.fontSize = "12px";
      note.style.color = "var(--ink-3)";
      note.style.margin = "6px 0";
      note.textContent = `仅显示前 50 条，共 ${reviews.length} 条。`;
      detailRootEl.append(note);
    }
  }

  const files = fileRows(task);
  if (files.length) {
    const sectionTitle = document.createElement("div");
    sectionTitle.style.fontSize = "13px";
    sectionTitle.style.fontWeight = "650";
    sectionTitle.style.margin = "16px 0 8px";
    sectionTitle.textContent = "产物文件";
    detailRootEl.append(sectionTitle);

    const table = document.createElement("table");
    table.className = "tbl";
    const headRow = document.createElement("tr");
    for (const label of ["文件", "状态", "输出", "错误"]) {
      const th = document.createElement("th");
      th.textContent = label;
      headRow.append(th);
    }
    table.append(headRow);
    for (const file of files.slice(0, 30)) {
      const tr = document.createElement("tr");
      for (const value of [file.name || "—", file.status || "—", file.output || "—", file.error || "—"]) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.append(td);
      }
      table.append(tr);
    }
    detailRootEl.append(table);
    if (files.length > 30) {
      const note = document.createElement("p");
      note.style.fontSize = "12px";
      note.style.color = "var(--ink-3)";
      note.style.margin = "6px 0";
      note.textContent = `仅显示前 30 条，共 ${files.length} 条。`;
      detailRootEl.append(note);
    }
  }

  if (!task.terminal) {
    const logNote = document.createElement("p");
    logNote.style.fontSize = "12px";
    logNote.style.color = "var(--ink-3)";
    logNote.style.marginTop = "10px";
    logNote.textContent =
      entry.streamState === "reconnecting"
        ? `事件流暂时断开，正在从事件 ${entry.lastEventId} 补拉，不会重复处理已有进度。`
        : "任务运行中，快照与产物会在收尾后补全。";
    detailRootEl.append(logNote);
  }
}

// ---------------------------------------------------------------------------
// View 生命周期
// ---------------------------------------------------------------------------

export function mount(container: HTMLElement, params: ViewParams): void {
  mounted = true;
  if (typeof params.taskId === "string") selectedId = params.taskId;
  else if (!selectedId && order.length) selectedId = order[0];

  refreshTopbarStatus();

  listRootEl = document.createElement("div");
  listRootEl.className = "tk-list";
  filtersEl = document.createElement("div");
  filtersEl.className = "tk-filters";
  for (const [key, label] of FILTERS) {
    const f = document.createElement("span");
    f.className = key === filter ? "f on" : "f";
    f.textContent = label;
    f.addEventListener("click", () => {
      filter = key;
      renderFilters();
      renderList();
    });
    filtersEl.append(f);
  }
  listRootEl.append(filtersEl);

  detailRootEl = document.createElement("div");
  detailRootEl.className = "card tk-detail";

  container.append(listRootEl, detailRootEl);

  renderList();
  renderDetail();

  // 前台快速轮询：任务中心可见时更快发现新任务/刷新列表；后台巡检（12s）在任何视图下都跑。
  fastPollTimer = window.setInterval(() => void refreshRegistry(), 4000);
}

function renderFilters(): void {
  if (!filtersEl) return;
  const children = Array.from(filtersEl.children) as HTMLSpanElement[];
  FILTERS.forEach(([key], index) => {
    children[index]?.classList.toggle("on", key === filter);
  });
}

export function unmount(): void {
  mounted = false;
  if (fastPollTimer !== null) {
    window.clearInterval(fastPollTimer);
    fastPollTimer = null;
  }
  listRootEl = null;
  filtersEl = null;
  detailRootEl = null;
}
