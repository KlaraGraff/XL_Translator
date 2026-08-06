// V9 翻译工作区 —— Excel / Word / PDF 三个视图共用的布局模板 + 任务生命周期状态机。
// 三页「同构」：col-l/col-r 的板块顺序、位置、命名完全一致，只有内容（开关列表、统计
// 字段、文件表列）按 surface 不同。excel.ts / word.ts / pdf.ts 只提供这些差异化配置，
// 具体渲染、扫描、启动/停止/暂停、SSE 订阅、设置持久化全部在这里实现一次。
//
// 状态机（每个 surface 独立一份，模块级 Map 持久化，跨 mount/unmount 保留）：
//   setup（未扫描 / 已扫描待开始） --scan--> setup（文件已填充）
//   setup --start（可能经 xls/doc 兼容确认、并行风险确认）--> running
//   running --stop--> running（等终态事件）--终态事件--> setup（横幅显示）
//   running --pause（仅 PDF）--> paused
//   paused --resume（仅 PDF）--> running
//   paused --end-paused（仅 PDF）--> running（等终态事件，同上落回 setup+横幅）
// 「right column 运行时锁定」「完成横幅 + 复位」等细节见下方实现与文件尾的架构说明。

import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";

import { ApiClient, type PdfPage, type PdfPageFile, type PdfPagesSnapshot, type SseEvent, type TaskStatus } from "../api-client";
import {
  createBanner,
  createButton,
  createChip,
  createEmptyState,
  createField,
  createFold,
  createProgressBar,
  createSelectField,
  createSwitchRow,
  createTextField,
  openModal,
  showToast,
  type ChipTone,
  type StatusTone,
} from "../components";
import { icon, type IconName } from "../icons";
import { navigate, type ViewParams } from "../router";
import { setTopbar } from "../shell";

import "./workspace.css";

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------

export type Surface = "excel" | "word" | "pdf";

type JsonObject = Record<string, unknown>;

/** 扫描接口返回的文件条目；字段是后端各 surface 联合，具体用到哪些看 surface。 */
type FileItem = JsonObject & {
  path: string;
  name?: string;
  relative_path?: string;
  size_kb?: number;
  format?: string;
  sheet_count?: number;
  sheets?: string[];
  text_cell_count?: number;
  paragraph_count?: number;
  table_count?: number;
  page_count?: number;
  source_type?: string;
  needs_conversion?: boolean;
};

type ScanSkippedItem = { path?: string; relative_path?: string; name?: string; reason?: string };

type TogglePathKind = "none" | "flat" | "output";

interface ToggleDef {
  key: string;
  label: string;
  hint: string;
  default: boolean;
  pathKind: TogglePathKind;
  /** flat: 完整点路径（如 "excel_review.mark_review_items"）；output: 只给 key 本身，前缀由 outputSettingPath() 解析。 */
  path?: string;
  /** 与另一个开关互斥：本开关打开时另一个自动关闭，反之亦然（Excel 的行高精调/锁定缩字号）。 */
  exclusiveWith?: string;
  /** 只有当另一个开关为 true 时本开关才可用（Word 的「补译时保护封面与目录」依赖「仅补译未翻译内容」）。 */
  requiresKey?: string;
}

const EXCEL_TOGGLES: ToggleDef[] = [
  { key: "untranslated", label: "仅补译未翻译内容", hint: "只翻译还没有译文的内容，已翻译部分保持不变。", default: false, pathKind: "none" },
  { key: "keepOriginal", label: "保留「_原文」副本", hint: "输出文件里为每个工作表额外保留一份未翻译的原始副本。", default: true, pathKind: "output", path: "keep_original_sheets" },
  { key: "formulaBackfill", label: "公式显示值回填", hint: "公式单元格按当前显示值写成静态双语文本，公式本身不再参与计算。", default: false, pathKind: "output", path: "formula_display_value_backfill" },
  { key: "excelAutofit", label: "Excel 精调行高", hint: "需要本机安装 Excel。默认用 Python 估算行高；精调不可用时保留估算结果，并在文件结果中提示。", default: true, pathKind: "output", path: "enable_excel_autofit", exclusiveWith: "lockRowHeight" },
  { key: "lockRowHeight", label: "锁定行高时缩字号", hint: "与「使用 Excel 精调行高」互斥。缩到最小字号仍会溢出的单元格将进入复核。", default: false, pathKind: "output", path: "lock_row_height", exclusiveWith: "excelAutofit" },
  { key: "reviewMark", label: "标记需复核内容", hint: "为语义校验接受、保留原文和疑似原文异常的单元格标注底色，便于人工复核。", default: true, pathKind: "flat", path: "excel_review.mark_review_items" },
];

const WORD_TOGGLES: ToggleDef[] = [
  { key: "untranslated", label: "仅补译未翻译内容", hint: "只翻译还没有译文的内容，已翻译部分保持不变。", default: false, pathKind: "none" },
  { key: "wordNativePreprocessing", label: "本地自动编号预处理", hint: "依次尝试本机 Microsoft Word 和 LibreOffice；不可用时自动用 Python 保守物化编号，关闭时全程只用 Python。所有预处理都发生在临时副本。", default: true, pathKind: "flat", path: "word_conversion.use_native_preprocessing" },
  { key: "wordHighlight", label: "标记需复核内容", hint: "为保留原文或质量校验未通过的段落加高亮，便于人工复核。", default: true, pathKind: "flat", path: "word_review.highlight_unresolved" },
  { key: "protectSchemeCover", label: "补译时保护方案封面与目录", hint: "仅在「仅补译未翻译内容」开启时生效，默认关闭。目录和域代码始终受保护，不会作为普通正文翻译。", default: false, pathKind: "none", requiresKey: "untranslated" },
];

const PDF_TOGGLES: ToggleDef[] = [
  { key: "pdfReview", label: "逐页审核模型", hint: "开启后由审核模型逐页复核译文；审核模型的配置与连接状态会和任务一起冻结。", default: true, pathKind: "flat", path: "pdf.review_enabled" },
  { key: "pdfCompressed", label: "生成压缩 PDF", hint: "在原始输出之外额外生成一份体积更小的 PDF。", default: true, pathKind: "flat", path: "pdf.generate_compressed_pdf" },
  { key: "pdfImages", label: "允许独立图片", hint: "只决定 PNG、JPG/JPEG、WebP、BMP、TIF/TIFF 是否作为独立输入扫描；PDF 页面一律按版式协议处理。", default: false, pathKind: "flat", path: "pdf.include_images" },
];

const TOGGLES: Record<Surface, ToggleDef[]> = { excel: EXCEL_TOGGLES, word: WORD_TOGGLES, pdf: PDF_TOGGLES };

const SURFACE_LABEL: Record<Surface, string> = { excel: "Excel", word: "Word", pdf: "PDF" };
const SURFACE_ICON: Record<Surface, IconName> = { excel: "excel", word: "word", pdf: "pdf" };
const SURFACE_PAGE_TITLE: Record<Surface, string> = { excel: "Excel 表格翻译", word: "Word 文档翻译", pdf: "PDF 与图片翻译" };
const SURFACE_FILE_NOUN: Record<Surface, string> = { excel: "表格文件", word: "文档", pdf: "PDF / 图片文件" };
const SURFACE_EMPTY_SUBTITLE: Record<Surface, string> = {
  excel: "选择文件或文件夹，扫描后勾选要翻译的表格",
  word: "选择文件或文件夹，扫描后勾选要翻译的文档",
  pdf: "选择文件或文件夹，扫描后勾选要翻译的 PDF 与图片",
};
const SURFACE_FIRST_EMPTY: Record<Surface, { title: string; description: string }> = {
  excel: { title: "还没有可翻译的文件", description: "选择来源并点击「扫描」，找到的表格会列在这里，可逐个勾选。" },
  word: { title: "还没有可翻译的文件", description: "选择来源并点击「扫描」，找到的文档会列在这里，可逐个勾选。" },
  pdf: { title: "还没有可翻译的文件", description: "选择来源并点击「扫描」，找到的 PDF 与图片会列在这里，可逐个勾选。" },
};
const SURFACE_BANNER_EMPTY: Record<Surface, { title: string; description: string }> = {
  excel: { title: "可以开始下一批了", description: "上一任务的结果已归档，选择新来源即可继续。" },
  word: { title: "可以开始下一批了", description: "上一任务的结果已归档，选择新来源即可继续。" },
  pdf: { title: "可以开始下一批了", description: "上一任务的结果已归档，选择新来源即可继续。" },
};

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  return node;
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" && value ? value : fallback;
}

function record(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function hasOwn(obj: JsonObject, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

/** 点路径写入：nestedPatch("a.b.c", 1) => {a:{b:{c:1}}}。与 main.ts 的 nestedPatch verbatim 同构。 */
function nestedPatch(path: string, value: unknown): JsonObject {
  const parts = path.split(".");
  const last = parts.pop() as string;
  let root: JsonObject = {};
  let cursor = root;
  for (const part of parts) {
    const next: JsonObject = {};
    cursor[part] = next;
    cursor = next;
  }
  cursor[last] = value;
  return root;
}

/** 后端自由文本可能带凭证碎片，展示前一律脱敏。三处（main.ts / tasks.ts / 这里）各自独立实现，故意不共享，见文件尾说明。 */
function redactedText(value: unknown, fallback = ""): string {
  const raw = text(value, fallback);
  if (!raw) return raw;
  return raw
    .replace(/(authorization\s*[:=]\s*)([^\s,;]+)/gi, "$1[redacted]")
    .replace(/\b(sk|rk|pk|api)[-_][a-z0-9_-]{8,}\b/gi, "[redacted]")
    .replace(/\bBearer\s+[^\s,;]+/gi, "Bearer [redacted]");
}

function formatTime(ts?: number): string {
  const date = ts ? new Date(ts * (ts < 2e10 ? 1000 : 1)) : new Date();
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function formatSizeKb(kb: unknown): string {
  const value = num(kb, 0);
  if (value >= 1024) return `${(value / 1024).toFixed(1)} MB`;
  return `${Math.round(value)} KB`;
}

// ---------------------------------------------------------------------------
// 每个 surface 的本地状态（模块级，跨 mount/unmount 存活，与 tasks.ts 的后台徽标循环
// 各自独立——见文件尾「架构偏离说明」）。
// ---------------------------------------------------------------------------

interface LocalTask {
  task: TaskStatus;
  logs: Array<{ time: string; level: string; message: string }>;
  phaseName: string;
  percent: number;
  streamState: "connected" | "reconnecting";
  watcherActive: boolean;
  fileStage: Map<string, "queued" | "active" | "done" | "error">;
  wordRecovery?: JsonObject;
  pdfPageRecovery?: JsonObject;
  pdfReview?: JsonObject;
  /** 逐页拉取的快照（GET /pdf-pages）。没有逐页 SSE——收到聚合事件或任务状态变化时重新拉取一次，
   *  见 fetchPdfPagesSnapshot() 与它的调用点清单。终态后不清空，保留最后一次结果供「查看对比」使用。 */
  pdfPagesSnapshot?: PdfPagesSnapshot;
}

interface SurfaceState {
  sourcePath: string;
  files: FileItem[];
  skipped: ScanSkippedItem[];
  scanSummary: JsonObject;
  selected: Set<string>;
  toggles: Map<string, boolean>;
  targetLang: string;
  sourceLang: string;
  domainPreset: string;
  useCustomOutputDir: boolean;
  customOutputDir: string;
  task: LocalTask | null;
  hasEverCompleted: boolean;
  showBanner: boolean;
  bannerInfo: { title: string; subtitle: string } | null;
  allowXlsFallback: boolean;
  allowDocFallback: boolean;
  renderer: (() => void) | null;
  lastTaskId?: string;
  lastOutputPath?: string;
}

function freshToggles(surface: Surface): Map<string, boolean> {
  const map = new Map<string, boolean>();
  for (const toggle of TOGGLES[surface]) map.set(toggle.key, toggle.default);
  return map;
}

function freshState(surface: Surface): SurfaceState {
  return {
    sourcePath: "",
    files: [],
    skipped: [],
    scanSummary: {},
    selected: new Set(),
    toggles: freshToggles(surface),
    targetLang: surface === "pdf" ? "zh" : "en",
    sourceLang: "auto",
    domainPreset: "同步工程场景",
    useCustomOutputDir: false,
    customOutputDir: "",
    task: null,
    hasEverCompleted: false,
    showBanner: false,
    bannerInfo: null,
    allowXlsFallback: false,
    allowDocFallback: false,
    renderer: null,
  };
}

const states: Record<Surface, SurfaceState> = {
  excel: freshState("excel"),
  word: freshState("word"),
  pdf: freshState("pdf"),
};

// ---------------------------------------------------------------------------
// API 客户端 + 全局设置/语言目录（本视图独立持有一份，不复用 tasks.ts 的实例——
// 各视图自包含，减少跨模块耦合，见文件尾说明）。
// ---------------------------------------------------------------------------

const client = new ApiClient();
let connectPromise: Promise<void> | null = null;
async function getClient(): Promise<ApiClient> {
  if (!connectPromise) connectPromise = client.connect();
  await connectPromise;
  return client;
}

let settings: JsonObject = {};
let languageOptions: { source: Array<{ code: string; display_name: string }>; target: Array<{ code: string; display_name: string }> } = { source: [], target: [] };
let bootstrapPromise: Promise<void> | null = null;
let bootstrapAdopted = new Set<Surface>();

async function ensureBootstrap(): Promise<void> {
  if (!bootstrapPromise) {
    bootstrapPromise = (async () => {
      const c = await getClient();
      const [settingsPayload, languagesPayload] = await Promise.all([
        c.request<JsonObject>("/api/settings"),
        c.request<{ source_options: Array<{ code: string; display_name: string }>; target_options: Array<{ code: string; display_name: string }> }>("/api/languages"),
      ]);
      settings = settingsPayload;
      languageOptions = { source: languagesPayload.source_options ?? [], target: languagesPayload.target_options ?? [] };
      applySettingsToStates();
    })();
  }
  await bootstrapPromise;
}

function applySettingsToStates(): void {
  for (const surface of ["excel", "word", "pdf"] as Surface[]) {
    const st = states[surface];
    st.sourcePath = st.sourcePath || text(settings[`last_${surface}_source_folder`]);
    if (surface === "pdf") {
      const pdf = record(settings.pdf);
      st.targetLang = text(pdf.target_lang, "zh");
    } else {
      st.targetLang = text(settings[`${surface}_target_lang`], text(settings.target_lang, "en"));
      st.sourceLang = text(settings[`${surface}_source_lang`], "auto");
    }
    if (surface !== "pdf") {
      st.domainPreset = text(settings[`${surface}_domain_preset`], "同步工程场景");
    }
    for (const toggle of TOGGLES[surface]) {
      if (toggle.pathKind === "flat" && toggle.path) {
        st.toggles.set(toggle.key, Boolean(readPath(settings, toggle.path)));
      } else if (toggle.pathKind === "output") {
        st.toggles.set(toggle.key, Boolean(readOutputPath(surface, toggle.path as string)));
      }
    }
    const outputSettings = outputRecord(surface);
    st.useCustomOutputDir = Boolean(outputSettings.use_custom_output_dir);
    st.customOutputDir = text(outputSettings.custom_output_dir);
  }
}

function readPath(obj: JsonObject, path: string): unknown {
  let cursor: unknown = obj;
  for (const part of path.split(".")) {
    if (!cursor || typeof cursor !== "object") return undefined;
    cursor = (cursor as JsonObject)[part];
  }
  return cursor;
}

/** excel_output/word_output 有旧版 output 兜底（main.ts 的 excelOutputSettings 同款逻辑）；pdf_output 没有。 */
function outputRecord(surface: Surface): JsonObject {
  if (surface === "pdf") return record(settings.pdf_output);
  const isolated = record(settings[`${surface}_output`]);
  return Object.keys(isolated).length ? isolated : record(settings.output);
}

function outputSettingPathPrefix(surface: Surface): string {
  if (surface === "pdf") return "pdf_output";
  return hasOwn(settings, `${surface}_output`) ? `${surface}_output` : "output";
}

function readOutputPath(surface: Surface, key: string): unknown {
  return outputRecord(surface)[key];
}

async function persistSettings(patch: JsonObject): Promise<void> {
  const c = await getClient();
  settings = await c.request<JsonObject>("/api/settings", { method: "PUT", body: JSON.stringify(patch) });
}

// ---------------------------------------------------------------------------
// mount / unmount
// ---------------------------------------------------------------------------

export function mountWorkspace(container: HTMLElement, _params: ViewParams, surface: Surface): void {
  const st = states[surface];
  st.renderer = () => renderInto(container, surface);
  renderLoading(container, surface);
  ensureBootstrap()
    .then(() => adoptExistingTask(surface))
    .then(() => {
      if (st.renderer) renderInto(container, surface);
    })
    .catch((error) => {
      showToast({ message: redactedText((error as Error)?.message, "初始化工作区失败。"), error: true });
      if (st.renderer) renderInto(container, surface);
    });
}

export function unmountWorkspace(surface: Surface): void {
  states[surface].renderer = null;
}

function renderLoading(container: HTMLElement, surface: Surface): void {
  setTopbar({ title: SURFACE_PAGE_TITLE[surface], status: { label: "加载中", tone: "idle" }, subtitle: "正在连接后台服务…" });
  const card = createEmptyState({ title: "正在加载工作区", description: "首次连接后台可能需要几秒。", icon: SURFACE_ICON[surface] });
  card.style.flex = "1";
  container.append(card);
}

/** 首次打开某 surface 时，如果后端已有一个仍在跑的该类任务且本地还没聚焦任何任务，
 *  自动接管并订阅——对应 main.ts 的 workspaceTask() 在无显式聚焦时退回"最近一个活动任务"。 */
async function adoptExistingTask(surface: Surface): Promise<void> {
  if (bootstrapAdopted.has(surface)) return;
  bootstrapAdopted.add(surface);
  const st = states[surface];
  if (st.task) return;
  try {
    const c = await getClient();
    const list = await c.listTasks();
    const candidate = list.active.find((t) => t.surface === surface);
    if (candidate) {
      focusTask(surface, candidate);
      watchTask(surface);
      if (surface === "pdf") void fetchPdfPagesSnapshot(surface, candidate.task_id);
    }
  } catch {
    // 接管失败不影响正常使用——用户可以照常扫描发起新任务。
  }
}

// ---------------------------------------------------------------------------
// 顶部渲染入口：整页按当前 phase 重建（DOM 规模很小，重建比增量 patch 更简单可靠）。
// ---------------------------------------------------------------------------

function renderInto(container: HTMLElement, surface: Surface): void {
  while (container.firstChild) container.removeChild(container.firstChild);
  container.removeAttribute("style");
  const st = states[surface];
  const active = Boolean(st.task && !st.task.task.terminal);

  updateTopbar(surface, st, active);

  if (st.showBanner && st.bannerInfo) {
    container.style.flexDirection = "column";
    container.append(buildBanner(surface, st));
    const row = el("div");
    row.style.cssText = "flex:1;display:flex;gap:16px;min-height:0";
    row.append(buildColLeft(surface, st, active), buildColRight(surface, st, active));
    container.append(row);
  } else {
    container.append(buildColLeft(surface, st, active), buildColRight(surface, st, active));
  }
}

function rerender(surface: Surface): void {
  const st = states[surface];
  st.renderer?.();
}

function updateTopbar(surface: Surface, st: SurfaceState, active: boolean): void {
  if (st.showBanner && st.bannerInfo) {
    const warn = /需复核/.test(st.bannerInfo.subtitle);
    setTopbar({
      title: SURFACE_PAGE_TITLE[surface],
      status: { label: warn ? "已完成 · 需复核" : "已完成", tone: warn ? "warn" : "ok" },
      subtitle: "任务已归档到任务中心，可随时回看完整报告",
    });
    return;
  }
  if (active && st.task) {
    const state = st.task.task.state;
    const meta = runningStatusMeta(state);
    const fileCount = st.selected.size || st.files.length;
    const domainSuffix = surface !== "pdf" ? ` · ${st.domainPreset}` : "";
    const subtitle = surface === "pdf"
      ? `${st.sourcePath.split("/").pop() || "PDF / 图片任务"} · → ${targetLabel(st)}`
      : `${fileCount} 个${SURFACE_FILE_NOUN[surface]} · ${sourceLabel(st)} → ${targetLabel(st)}${domainSuffix}`;
    setTopbar({ title: SURFACE_PAGE_TITLE[surface], status: meta, subtitle });
    return;
  }
  if (st.files.length > 0) {
    setTopbar({
      title: SURFACE_PAGE_TITLE[surface],
      status: { label: "已扫描 · 待开始", tone: "run" },
      subtitle: `已找到 ${st.files.length} 个${SURFACE_FILE_NOUN[surface]}，勾选后开始翻译`,
    });
    return;
  }
  setTopbar({ title: SURFACE_PAGE_TITLE[surface], status: { label: "待选择来源", tone: "idle" }, subtitle: SURFACE_EMPTY_SUBTITLE[surface] });
}

function runningStatusMeta(state: TaskStatus["state"]): { label: string; tone: StatusTone } {
  switch (state) {
    case "paused":
      return { label: "已暂停提交", tone: "pause" };
    case "pausing":
      return { label: "正在暂停", tone: "pause" };
    case "stopping":
      return { label: "正在停止", tone: "warn" };
    case "finalizing":
      return { label: "正在收尾", tone: "warn" };
    default:
      return { label: "翻译中", tone: "run" };
  }
}

function sourceLabel(st: SurfaceState): string {
  if (st.sourceLang === "auto") return "自动检测";
  return languageOptions.source.find((o) => o.code === st.sourceLang)?.display_name ?? st.sourceLang;
}
function targetLabel(st: SurfaceState): string {
  return languageOptions.target.find((o) => o.code === st.targetLang)?.display_name ?? st.targetLang;
}

// ---------------------------------------------------------------------------
// 完成横幅
// ---------------------------------------------------------------------------

function buildBanner(surface: Surface, st: SurfaceState): HTMLElement {
  const info = st.bannerInfo!;
  const openDir = createButton({
    label: "打开输出目录",
    icon: "folder",
    onClick: () => openTaskLocalFile(st.lastOutputPath, true),
  });
  const openReport = createButton({
    label: "查看完整报告",
    icon: "ext",
    onClick: () => navigate("tasks", { taskId: st.lastTaskId }),
  });
  return createBanner({
    title: info.title,
    subtitle: info.subtitle,
    icon: "check",
    actions: [openDir, openReport],
    onClose: () => {
      st.showBanner = false;
      rerender(surface);
    },
  });
}

// ---------------------------------------------------------------------------
// 左栏
// ---------------------------------------------------------------------------

function buildColLeft(surface: Surface, st: SurfaceState, active: boolean): HTMLElement {
  const col = el("div", "col-l");
  const local = st.task;
  if (active && local) {
    col.append(buildProgressCard(surface, st));
    if (surface === "word") {
      const recoveryCard = buildWordRecoveryCard(local);
      if (recoveryCard) col.append(recoveryCard);
    }
    if (surface === "pdf") {
      const recoveryCard = buildPdfRecoveryCard(surface, local);
      if (recoveryCard) col.append(recoveryCard);
      const reviewCard = buildPdfReviewCard(surface, local);
      if (reviewCard) col.append(reviewCard);
    }
    col.append(buildLogCard(local));
  } else {
    col.append(buildSrcBar(surface, st));
    col.append(buildStatsRow(surface, st));
    col.append(buildTableCard(surface, st));
  }
  return col;
}

function buildSrcBar(surface: Surface, st: SurfaceState): HTMLElement {
  const bar = el("div", "card srcbar");
  const { root, input } = createTextField({
    label: "",
    value: st.sourcePath,
    placeholder: "选择或粘贴文件、文件夹路径…",
    onInput: (value) => {
      st.sourcePath = value;
    },
  });
  root.style.margin = "0";
  root.style.flex = "1";
  input.addEventListener("change", () => {
    void persistSettings({ [`last_${surface}_source_folder`]: st.sourcePath });
  });
  bar.append(input);
  bar.append(createButton({
    label: "浏览",
    icon: "folder",
    onClick: async () => {
      try {
        const filters = surface === "pdf"
          ? [{ name: "PDF 与图片", extensions: ["pdf", ...(st.toggles.get("pdfImages") ? ["png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"] : [])] }]
          : undefined;
        const picked = await open({ title: "选择来源", directory: true, multiple: false, filters });
        if (typeof picked === "string") {
          st.sourcePath = picked;
          input.value = picked;
          await persistSettings({ [`last_${surface}_source_folder`]: picked });
        }
      } catch (error) {
        showToast({ message: redactedText((error as Error)?.message, "选择来源失败。"), error: true });
      }
    },
  }));
  bar.append(createButton({
    label: st.files.length > 0 ? "重新扫描" : "扫描",
    variant: "primary",
    disabled: !st.sourcePath.trim(),
    onClick: () => void runScan(surface),
  }));
  return bar;
}

function buildStatsRow(surface: Surface, st: SurfaceState): HTMLElement {
  const wrap = el("div", "stats");
  const stats = computeStats(surface, st);
  for (const stat of stats) {
    const cell = el("div", st.files.length ? "stat" : "stat dim");
    const span = el("span");
    span.textContent = stat.label;
    const b = el("b");
    b.textContent = st.files.length ? stat.value : "—";
    if (stat.warn && st.files.length) b.style.color = "var(--warn)";
    cell.append(span, b);
    wrap.append(cell);
  }
  return wrap;
}

function computeStats(surface: Surface, st: SurfaceState): Array<{ label: string; value: string; warn?: boolean }> {
  const files = st.files;
  if (surface === "excel") {
    const cells = files.reduce((sum, f) => sum + num(f.text_cell_count), 0);
    const sheets = files.reduce((sum, f) => sum + num(f.sheet_count, f.sheets?.length ?? 0), 0);
    const xls = files.filter((f) => isRisky(surface, f)).length;
    return [
      { label: "已扫描文件", value: String(files.length) },
      { label: "文本单元格", value: cells.toLocaleString("zh-CN") },
      { label: "工作表", value: String(sheets) },
      { label: "旧版 .xls", value: String(xls), warn: xls > 0 },
    ];
  }
  if (surface === "word") {
    const paragraphs = files.reduce((sum, f) => sum + num(f.paragraph_count), 0);
    const tables = files.reduce((sum, f) => sum + num(f.table_count), 0);
    const doc = files.filter((f) => isRisky(surface, f)).length;
    return [
      { label: "已扫描文件", value: String(files.length) },
      { label: "段落", value: paragraphs.toLocaleString("zh-CN") },
      { label: "表格", value: String(tables) },
      { label: "旧版 .doc", value: String(doc), warn: doc > 0 },
    ];
  }
  const pages = files.reduce((sum, f) => sum + num(f.page_count), 0);
  const images = files.filter((f) => f.source_type === "image").length;
  return [
    { label: "已扫描文件", value: String(files.length) },
    { label: "总页数", value: String(pages) },
    { label: "独立图片", value: String(images) },
    { label: "跳过项", value: String(st.skipped.length) },
  ];
}

function isRisky(surface: Surface, f: FileItem): boolean {
  if (surface === "excel") return f.format === "xls" || Boolean(f.needs_conversion);
  if (surface === "word") return f.format === "doc" || Boolean(f.needs_conversion);
  return false;
}

function fileLabel(f: FileItem): string {
  return text(f.name, f.relative_path ?? f.path.split("/").pop() ?? f.path);
}

function buildFmtBadge(label: string, warn: boolean): HTMLElement {
  const span = el("span", warn ? "fmt warn" : "fmt");
  span.textContent = label;
  return span;
}

function buildTableCard(surface: Surface, st: SurfaceState): HTMLElement {
  const card = el("div", "card tablecard");
  if (!st.files.length) {
    const head = el("div", "tc-head");
    const b = el("b");
    b.textContent = "任务清单";
    head.append(b);
    card.append(head);
    const copy = st.hasEverCompleted ? SURFACE_BANNER_EMPTY[surface] : SURFACE_FIRST_EMPTY[surface];
    const empty = createEmptyState({ title: copy.title, description: copy.description, icon: SURFACE_ICON[surface] });
    card.append(empty);
    return card;
  }

  const head = el("div", "tc-head");
  const b = el("b");
  b.textContent = "任务清单";
  const countSpan = el("span");
  countSpan.textContent = `已选 ${st.selected.size} / ${st.files.length}`;
  const tools = el("div", "tc-tools");
  const selectAll = el("span", "linklike");
  selectAll.textContent = "全选";
  selectAll.addEventListener("click", () => {
    st.selected = new Set(st.files.map((f) => f.path));
    rerender(surface);
  });
  const selectNone = el("span", "linklike");
  selectNone.textContent = "全不选";
  selectNone.addEventListener("click", () => {
    st.selected = new Set();
    rerender(surface);
  });
  tools.append(selectAll, selectNone);
  head.append(b, countSpan, tools);
  card.append(head);

  const tableWrap = el("div");
  tableWrap.style.cssText = "flex:1;overflow:auto";
  const table = el("table", "tbl");
  table.append(buildTableHeadRow(surface));
  for (const file of st.files) {
    table.append(buildTableRow(surface, st, file));
  }
  tableWrap.append(table);
  card.append(tableWrap);

  if (st.skipped.length) {
    const skipRow = el("div", "tc-head");
    skipRow.style.cssText = "border-top:1px solid var(--line);border-bottom:0";
    const warnIcon = icon("warn", { size: "sm" });
    warnIcon.style.color = "var(--warn)";
    const label = el("span");
    const names = st.skipped.slice(0, 2).map((s) => text(s.name, text(s.relative_path, s.path))).filter(Boolean);
    const extra = st.skipped.length > names.length ? `等 ${st.skipped.length} 项` : names.join("、");
    label.textContent = `扫描时跳过 ${st.skipped.length} 项${names.length ? `：${extra}` : ""}`;
    const skipTools = el("div", "tc-tools");
    const reportLink = el("span", "linklike");
    reportLink.textContent = "查看扫描报告";
    reportLink.addEventListener("click", () => showSkipReportModal(surface, st));
    skipTools.append(reportLink);
    skipRow.append(warnIcon, label, skipTools);
    card.append(skipRow);
  }

  return card;
}

function buildTableHeadRow(surface: Surface): HTMLTableRowElement {
  const row = el("tr");
  const check = el("th");
  check.style.width = "34px";
  row.append(check);
  const th = (label: string, numeric = false) => {
    const cell = el("th");
    if (numeric) cell.className = "num";
    cell.textContent = label;
    return cell;
  };
  if (surface === "excel") {
    row.append(th("文件"), th("格式"), th("工作表", true), th("文本单元格", true), th("状态"));
  } else if (surface === "word") {
    row.append(th("文件"), th("格式"), th("段落", true), th("表格", true), th("状态"));
  } else {
    row.append(th("文件"), th("类型"), th("大小"), th("页数 / 尺寸"), th("状态"));
  }
  return row;
}

function buildTableRow(surface: Surface, st: SurfaceState, file: FileItem): HTMLTableRowElement {
  const row = el("tr");
  const checkCell = el("td");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.className = "ck";
  checkbox.checked = st.selected.has(file.path);
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) st.selected.add(file.path);
    else st.selected.delete(file.path);
    rerender(surface);
  });
  checkCell.append(checkbox);
  row.append(checkCell);

  const nameCell = el("td");
  nameCell.textContent = fileLabel(file);
  row.append(nameCell);

  if (surface === "excel" || surface === "word") {
    const fmtCell = el("td");
    fmtCell.append(buildFmtBadge((file.format ?? "").toUpperCase(), isRisky(surface, file)));
    row.append(fmtCell);
    const numCell1 = el("td", "num");
    numCell1.textContent = surface === "excel" ? String(num(file.sheet_count, file.sheets?.length ?? 0)) : String(num(file.paragraph_count));
    const numCell2 = el("td", "num");
    numCell2.textContent = surface === "excel" ? num(file.text_cell_count).toLocaleString("zh-CN") : String(num(file.table_count));
    row.append(numCell1, numCell2);
  } else {
    const typeCell = el("td");
    typeCell.append(buildFmtBadge((file.format ?? file.source_type ?? "").toUpperCase(), false));
    row.append(typeCell);
    const sizeCell = el("td");
    sizeCell.textContent = formatSizeKb(file.size_kb);
    row.append(sizeCell);
    const dimCell = el("td");
    dimCell.textContent = file.source_type === "image" ? "—" : `${num(file.page_count)} 页`;
    row.append(dimCell);
  }

  const statusCell = el("td");
  if (!st.selected.has(file.path)) {
    statusCell.append(createChip({ label: "已排除", tone: "mute" }));
  } else if (isRisky(surface, file)) {
    statusCell.append(createChip({ label: "需先转换", tone: "warn", icon: "warn" }));
  }
  row.append(statusCell);
  return row;
}

function showSkipReportModal(surface: Surface, st: SurfaceState): void {
  const body: (string | HTMLElement)[] = st.skipped.length
    ? st.skipped.map((s) => `${text(s.name, text(s.relative_path, s.path))} — ${redactedText(s.reason, "原因未知")}`)
    : ["没有被跳过的项目。"];
  openModal({
    tone: "warn",
    icon: "warn",
    title: `扫描报告 · ${SURFACE_LABEL[surface]}`,
    body,
    actions: [{ label: "关闭" }],
  });
}

// ---------------------------------------------------------------------------
// 左栏 · 运行中：进度卡 / 恢复卡（聚合数据，PDF 见下方数据缺口说明）/ 日志+逐文件卡
// ---------------------------------------------------------------------------

function buildProgressCard(surface: Surface, st: SurfaceState): HTMLElement {
  const card = el("div", "card");
  card.style.padding = "16px 18px 14px";
  const local = st.task!;
  const stage = el("div", "prog-stage");
  const b = el("b");
  b.textContent = redactedText(local.phaseName, "正在准备任务");
  const pct = el("span", "pct");
  pct.textContent = `${Math.round(local.percent)}%`;
  if (local.task.state === "paused" || local.task.state === "pausing") pct.style.color = "var(--warn)";
  stage.append(b, pct);
  card.append(stage);

  const bar = createProgressBar({ percent: local.percent, tone: local.task.state === "paused" || local.task.state === "pausing" ? "warn" : "accent" });
  card.append(bar.root);

  const monChips = buildMonChips(surface, local);
  if (monChips.length) {
    const mon = el("div", "mon");
    for (const chip of monChips) mon.append(chip);
    card.append(mon);
  }

  if (local.streamState === "reconnecting") {
    const note = el("p");
    note.className = "ws-note";
    note.textContent = "事件流暂时断开，正在自动重连，不会重复处理已有进度。";
    card.append(note);
  }

  return card;
}

function buildMonChips(surface: Surface, local: LocalTask): HTMLElement[] {
  const chip = (label: string, tone: ChipTone) => createChip({ label, tone });
  if (surface === "word" && local.wordRecovery) {
    const r = local.wordRecovery;
    const chips: HTMLElement[] = [];
    if (hasOwn(r, "retry_round")) chips.push(chip(`重试轮次 ${num(r.retry_round)}`, "tint"));
    if (hasOwn(r, "semantic_processing_count")) chips.push(chip(`仲裁处理中 ${num(r.semantic_processing_count)}`, "tint"));
    if (hasOwn(r, "semantic_accepted_count")) chips.push(chip(`仲裁已接受 ${num(r.semantic_accepted_count)}`, "ok"));
    if (hasOwn(r, "retry_unresolved_count") || hasOwn(r, "unresolved_count")) chips.push(chip(`未恢复 ${num(r.retry_unresolved_count, num(r.unresolved_count))}`, "warn"));
    return chips;
  }
  if (surface === "pdf" && local.pdfPageRecovery) {
    const r = local.pdfPageRecovery;
    const chips: HTMLElement[] = [];
    if (hasOwn(r, "completed_pages")) chips.push(chip(`已生成 ${num(r.completed_pages)} / ${num(r.total_pages)} 页`, "ok"));
    if (hasOwn(r, "submitted_page_count")) chips.push(chip(`已提交 ${num(r.submitted_page_count)}`, "tint"));
    if (hasOwn(r, "retrying_page_count")) chips.push(chip(`重试中 ${num(r.retrying_page_count)}`, "warn"));
    if (hasOwn(r, "recovered_page_count")) chips.push(chip(`已恢复 ${num(r.recovered_page_count)}`, "mute"));
    return chips;
  }
  return [];
}

function buildWordRecoveryCard(local: LocalTask): HTMLElement | null {
  const r = local.wordRecovery;
  if (!r || !Object.keys(r).length) return null;
  const card = el("div", "card");
  card.style.padding = "13px 16px";
  const head = el("div", "tc-head");
  head.style.cssText = "padding:0 0 8px;border:0";
  const b = el("b");
  b.textContent = "严格重试与语义仲裁";
  head.append(b);
  card.append(head);
  const note = el("p", "ws-note");
  note.textContent = "严格重试只处理空译文、明显不完整或质量校验失败内容；语义仲裁接受的边界译文不会自动写入记忆库。";
  card.append(note);
  return card;
}

// ---------------------------------------------------------------------------
// PDF 页恢复 / 逐页审核 —— 真正的逐页交互面板（GET /pdf-pages 拉取式快照 + 三个操作端点）。
// 样张④只画了单文件、少量示例行；这里额外处理多文件分组与几百页的可滚动表格，
// 具体拉取触发点集中在 fetchPdfPagesSnapshot() 的调用方（watchTask 事件流 / adopt / 任务启动）。
// ---------------------------------------------------------------------------

type PdfPageRow = { file: PdfPageFile; page: PdfPage };

/** 需要出现在「页恢复」卡片里的页：失败/占位、已排队操作、或用户已接受跳过占位——
 *  纯 pending（还没跑到）或已通过的页不算，那些在「逐页审核」表里露出即可。 */
function collectRecoveryRows(snapshot: PdfPagesSnapshot): PdfPageRow[] {
  const rows: PdfPageRow[] = [];
  for (const file of snapshot.files) {
    for (const page of file.pages) {
      if (page.status === "failed" || page.placeholder || page.pending_action || page.user_skipped) {
        rows.push({ file, page });
      }
    }
  }
  return rows;
}

/** actionable=false 时按钮全部禁用但常驻显示，这句短话解释原因，挂在卡片顶部和每个按钮的 title 上。 */
function pdfActionsDisabledReason(snapshot: PdfPagesSnapshot): string {
  return snapshot.terminal ? "任务已结束，仅可查看对比页图，不能再触发操作。" : "暂停任务后才能重新生成或跳过页面；操作会在继续翻译时生效。";
}

async function runPdfPageAction(surface: Surface, taskId: string, file: PdfPageFile, page: PdfPage, action: "regenerate" | "skip"): Promise<void> {
  try {
    const c = await getClient();
    if (action === "regenerate") await c.regeneratePdfPage(taskId, file.relative_path, page.page_number);
    else await c.skipPdfPage(taskId, file.relative_path, page.page_number);
    showToast({ message: `第 ${page.page_number} 页已排队${action === "regenerate" ? "重新生成" : "跳过"}，继续翻译后生效。` });
    await fetchPdfPagesSnapshot(surface, taskId);
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, "操作失败。"), error: true });
  }
}

function buildPdfRecoveryCard(surface: Surface, local: LocalTask): HTMLElement | null {
  const snapshot = local.pdfPagesSnapshot;
  if (!snapshot) return null;
  const rows = collectRecoveryRows(snapshot);
  if (!rows.length) return null;
  const taskId = local.task.task_id;
  const multiFile = snapshot.files.length > 1;

  const card = el("div", "card");
  card.style.cssText = "padding:0;overflow:hidden";
  const head = el("div", "tc-head");
  const b = el("b");
  b.textContent = "页恢复";
  const span = el("span");
  span.textContent = "失败页在此重试，不影响其他页";
  head.append(b, span);
  card.append(head);

  if (!snapshot.actionable) {
    const note = el("p", "ws-note");
    note.style.cssText = "text-align:left;padding:8px 14px 0";
    note.textContent = pdfActionsDisabledReason(snapshot);
    card.append(note);
  }

  const list = el("div");
  list.style.cssText = "max-height:220px;overflow:auto";
  rows.forEach(({ file, page }, index) => {
    const row = buildRecoveryRow(surface, taskId, snapshot, file, page, multiFile);
    if (index === 0) row.style.borderTop = "0";
    list.append(row);
  });
  card.append(list);
  return card;
}

function buildRecoveryRow(surface: Surface, taskId: string, snapshot: PdfPagesSnapshot, file: PdfPageFile, page: PdfPage, multiFile: boolean): HTMLElement {
  const row = el("div", "filerow");
  const actionable = snapshot.actionable;
  const reason = pdfActionsDisabledReason(snapshot);

  if (page.pending_action === "regenerate") {
    row.append(createChip({ label: "已排队 · 重新生成", tone: "tint" }));
  } else if (page.pending_action === "skip") {
    row.append(createChip({ label: "已排队 · 跳过", tone: "tint" }));
  } else if (page.user_skipped) {
    row.append(createChip({ label: "已跳过", tone: "mute" }));
  } else if (page.attempts > 0) {
    row.append(createChip({ label: `重试 ${page.attempts}`, tone: "warn" }));
  } else {
    row.append(createChip({ label: "失败", tone: "dgr" }));
  }

  const nm = el("span", "nm");
  const filePrefix = multiFile ? `${file.name} · ` : "";
  nm.textContent = `${filePrefix}第 ${page.page_number} 页 · ${redactedText(page.error, page.placeholder ? "页图生成失败" : "待处理")}`;
  row.append(nm);

  row.append(createButton({
    label: "立即重试",
    size: "mini",
    disabled: !actionable,
    title: actionable ? undefined : reason,
    onClick: () => void runPdfPageAction(surface, taskId, file, page, "regenerate"),
  }));
  row.append(createButton({
    label: "跳过该页",
    size: "mini",
    disabled: !actionable,
    title: actionable ? undefined : reason,
    onClick: () => void runPdfPageAction(surface, taskId, file, page, "skip"),
  }));
  return row;
}

function buildPdfReviewCard(surface: Surface, local: LocalTask): HTMLElement | null {
  const snapshot = local.pdfPagesSnapshot;
  if (!snapshot || !snapshot.files.length) return null;
  const taskId = local.task.task_id;
  const multiFile = snapshot.files.length > 1;

  const card = el("div", "card");
  card.style.cssText = "flex:3 1 0%;min-height:0;display:flex;flex-direction:column;overflow:hidden";
  const head = el("div", "tc-head");
  const b = el("b");
  b.textContent = "逐页审核";
  const span = el("span");
  span.textContent = "审核模型逐页检查版式与译文完整性";
  head.append(b, span);
  card.append(head);

  if (!snapshot.actionable) {
    const note = el("p", "ws-note");
    note.style.cssText = "text-align:left;padding:8px 14px 0";
    note.textContent = pdfActionsDisabledReason(snapshot);
    card.append(note);
  }

  const tableWrap = el("div");
  tableWrap.style.cssText = "flex:1;overflow:auto";
  const table = el("table", "tbl");
  const headRow = el("tr");
  const headLabels = ["页", "审核结果", "说明", ""];
  headLabels.forEach((label, i) => {
    const th = el("th");
    th.textContent = label;
    if (i === 3) th.style.width = "190px";
    headRow.append(th);
  });
  table.append(headRow);

  for (const file of snapshot.files) {
    if (multiFile) {
      const groupRow = el("tr", "pdf-file-group");
      const cell = el("td");
      cell.colSpan = 4;
      cell.textContent = `${file.name} · ${file.page_count} 页`;
      groupRow.append(cell);
      table.append(groupRow);
    }
    for (const page of file.pages) {
      table.append(buildReviewRow(surface, taskId, snapshot, file, page));
    }
  }
  tableWrap.append(table);
  card.append(tableWrap);
  return card;
}

function reviewResultChip(page: PdfPage): HTMLElement {
  if (page.status === "failed" || page.placeholder) {
    return createChip({ label: page.user_skipped ? "已跳过占位" : "生成失败", tone: "dgr" });
  }
  if (page.status === "pending") return createChip({ label: "待处理", tone: "mute" });
  if (page.review_status === "passed") return createChip({ label: "通过", tone: "ok" });
  if (page.review_status) return createChip({ label: "待复核", tone: "warn" });
  return createChip({ label: "未审核", tone: "mute" });
}

function reviewNote(page: PdfPage): string {
  if (page.pending_action === "regenerate") return "已排队重新生成，继续翻译后生效。";
  if (page.pending_action === "skip") return "已排队跳过，继续翻译后生效。";
  if (page.review_summary) return redactedText(page.review_summary);
  if (page.status === "failed" || page.placeholder) return redactedText(page.error, "页面生成失败。");
  if (page.status === "pending") return "尚未处理。";
  return "版式一致，文本完整";
}

function buildReviewRow(surface: Surface, taskId: string, snapshot: PdfPagesSnapshot, file: PdfPageFile, page: PdfPage): HTMLTableRowElement {
  const row = el("tr");
  const pageCell = el("td");
  pageCell.textContent = `第 ${page.page_number} 页`;
  row.append(pageCell);

  const resultCell = el("td");
  resultCell.append(reviewResultChip(page));
  row.append(resultCell);

  const noteCell = el("td");
  noteCell.style.color = "var(--ink-2)";
  noteCell.textContent = reviewNote(page);
  row.append(noteCell);

  const actionsCell = el("td");
  const actionable = snapshot.actionable;
  const reason = pdfActionsDisabledReason(snapshot);
  const notFinished = page.status === "pending";
  const needsSkip = page.status === "failed" || page.placeholder;

  actionsCell.append(buildActionLink("查看对比", false, undefined, () => openPdfPageCompareModal(surface, taskId, file, page)));
  actionsCell.append(document.createTextNode(" · "));

  const regenDisabled = !actionable || notFinished;
  const regenReason = !actionable ? reason : notFinished ? "该页还没跑完，暂时不能重新生成。" : undefined;
  actionsCell.append(buildActionLink("重新生成", regenDisabled, regenReason, () => void runPdfPageAction(surface, taskId, file, page, "regenerate")));

  if (needsSkip) {
    actionsCell.append(document.createTextNode(" · "));
    actionsCell.append(buildActionLink("跳过该页", !actionable, actionable ? undefined : reason, () => void runPdfPageAction(surface, taskId, file, page, "skip")));
  }
  row.append(actionsCell);
  return row;
}

function buildActionLink(label: string, disabled: boolean, title: string | undefined, onClick: () => void): HTMLElement {
  const span = el("span", disabled ? "linklike disabled" : "linklike");
  span.textContent = label;
  if (title) span.title = title;
  if (!disabled) span.addEventListener("click", onClick);
  return span;
}

// ---------------------------------------------------------------------------
// 页对比模态：原文/译文页图并排。图片走鉴权 fetch + blob（<img src> 加不了自定义头），
// 关闭时统一 revoke，避免泄漏；缺图/加载失败给占位文案，不留空白。
// ---------------------------------------------------------------------------

interface CompareColHandle {
  root: HTMLElement;
  setImage(url: string): void;
  setError(message: string): void;
}

function buildCompareCol(label: string): CompareColHandle {
  const root = el("div", "pdf-compare-col");
  const labelEl = el("div", "pdf-compare-label");
  labelEl.textContent = label;
  const imgWrap = el("div", "pdf-compare-imgwrap");
  const loading = el("div", "ph");
  loading.textContent = "加载中…";
  imgWrap.append(loading);
  root.append(labelEl, imgWrap);

  const clear = () => {
    while (imgWrap.firstChild) imgWrap.removeChild(imgWrap.firstChild);
  };
  return {
    root,
    setImage: (url: string) => {
      clear();
      const img = document.createElement("img");
      img.alt = label;
      img.addEventListener("error", () => {
        clear();
        const err = el("div", "ph");
        err.textContent = "图片加载失败";
        imgWrap.append(err);
      });
      img.src = url;
      imgWrap.append(img);
    },
    setError: (message: string) => {
      clear();
      const err = el("div", "ph");
      err.textContent = message;
      imgWrap.append(err);
    },
  };
}

function openPdfPageCompareModal(surface: Surface, taskId: string, file: PdfPageFile, page: PdfPage): void {
  const wrap = el("div", "pdf-compare");
  const sourceCol = buildCompareCol(`原文 · 第 ${page.page_number} 页`);
  const translatedCol = buildCompareCol(`译文 · 第 ${page.page_number} 页`);
  wrap.append(sourceCol.root, translatedCol.root);

  const objectUrls: string[] = [];
  const load = async (kind: "source" | "translated", has: boolean, col: CompareColHandle) => {
    if (!has) {
      col.setError(kind === "source" ? "该页尚无原文页图" : "该页尚无译文页图");
      return;
    }
    try {
      const c = await getClient();
      const blob = await c.getPdfPageImage(taskId, file.relative_path, page.page_number, kind);
      const url = URL.createObjectURL(blob);
      objectUrls.push(url);
      col.setImage(url);
    } catch (error) {
      col.setError(redactedText((error as Error)?.message, "图片加载失败"));
    }
  };
  void load("source", page.has_source_image, sourceCol);
  void load("translated", page.has_translated_image, translatedCol);

  openModal({
    tone: "warn",
    icon: "pdf",
    sourceLabel: `逐页审核 · ${SURFACE_LABEL[surface]}`,
    title: `第 ${page.page_number} 页对比 · ${file.name}`,
    body: [wrap],
    actions: [
      {
        label: "关闭",
        onClick: () => {
          for (const url of objectUrls) URL.revokeObjectURL(url);
        },
      },
    ],
  });
}

// ---------------------------------------------------------------------------
// 逐页快照拉取：没有逐页 SSE，靠聚合事件/任务状态变化触发的一次性 GET。
// 调用点：handleTaskEvent 的 pdf_page_recovery / pdf_review / pdf_page_action / paused / resumed /
// 终态分支，adoptExistingTask 接管已有任务时，submitTaskStart 启动新任务后，以及 refetchTask 兜底刷新时。
// ---------------------------------------------------------------------------

async function fetchPdfPagesSnapshot(surface: Surface, taskId: string): Promise<void> {
  if (surface !== "pdf") return;
  const st = states[surface];
  if (!st.task || st.task.task.task_id !== taskId) return;
  try {
    const c = await getClient();
    const snapshot = await c.getPdfPages(taskId);
    if (st.task?.task.task_id !== taskId) return;
    st.task.pdfPagesSnapshot = snapshot;
    rerender(surface);
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, "刷新逐页状态失败。"), error: true });
  }
}

function buildLogCard(local: LocalTask): HTMLElement {
  const card = el("div", "card");
  card.style.cssText = "flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden";
  const head = el("div", "tc-head");
  const b = el("b");
  b.textContent = "运行日志";
  const span = el("span");
  span.textContent = "保留最近 10 条 · 完整日志随诊断归档";
  head.append(b, span);
  card.append(head);

  const log = el("div", "log");
  log.style.cssText = "flex:1;border:0;border-radius:0";
  for (const entry of local.logs.slice(-10)) {
    const line = el("div");
    const t = el("span", "t");
    t.textContent = entry.time;
    line.append(t);
    const tone = entry.level.toUpperCase();
    const textSpan = document.createElement("span");
    if (tone === "ERROR" || tone === "WARN") textSpan.className = "w";
    else if (tone === "OK" || tone === "SUCCESS" || tone === "GOOD") textSpan.className = "g";
    textSpan.textContent = entry.message;
    line.append(textSpan);
    log.append(line);
  }
  if (!local.logs.length) {
    const line = el("div");
    line.textContent = "等待引擎事件…";
    log.append(line);
  }
  card.append(log);

  const fileList = el("div");
  for (const [path, stage] of local.fileStage) {
    const row = el("div", "filerow");
    const meta: Record<string, { label: string; tone: ChipTone }> = {
      queued: { label: "排队中", tone: "mute" },
      active: { label: "进行中", tone: "tint" },
      done: { label: "已生成", tone: "ok" },
      error: { label: "未完成", tone: "warn" },
    };
    row.append(createChip(meta[stage]));
    const nm = el("span", "nm");
    nm.textContent = path.split("/").pop() ?? path;
    row.append(nm);
    fileList.append(row);
  }
  card.append(fileList);
  return card;
}

// ---------------------------------------------------------------------------
// 右栏
// ---------------------------------------------------------------------------

function buildColRight(surface: Surface, st: SurfaceState, active: boolean): HTMLElement {
  const col = el("div", "col-r");
  const card = el("div", active ? "card runpanel" : st.files.length ? "card runpanel" : "card runpanel dis");
  const scroll = el("div", active ? "rp-scroll dis" : "rp-scroll");

  const title = el("div", "rp-title");
  title.textContent = "运行设置";
  if (active) {
    const lockChip = createChip({ label: "任务中锁定", tone: "mute" });
    lockChip.style.marginLeft = "auto";
    title.append(lockChip);
  }
  scroll.append(title);

  const langSec = el("div", "rp-sec");
  langSec.textContent = "语言";
  scroll.append(langSec);

  const targetSelect = createSelectField({
    label: "目标语言",
    options: languageOptions.target.map((o) => ({ value: o.code, label: o.display_name })),
    value: st.targetLang,
    disabled: active,
    onChange: (value) => {
      st.targetLang = value;
      void persistSettings(surface === "pdf" ? nestedPatch("pdf.target_lang", value) : { [`${surface}_target_lang`]: value });
    },
  });
  scroll.append(targetSelect.root);

  if (surface !== "pdf") {
    const sourceOptions = [{ value: "auto", label: "自动检测" }, ...languageOptions.source.map((o) => ({ value: o.code, label: o.display_name }))];
    const sourceSelect = createSelectField({
      label: "源语言",
      options: sourceOptions,
      value: st.sourceLang,
      disabled: active,
      onChange: (value) => {
        st.sourceLang = value;
        void persistSettings({ [`${surface}_source_lang`]: value });
      },
    });
    scroll.append(sourceSelect.root);
  }

  const typeSec = el("div", "rp-sec");
  typeSec.textContent = `本类型选项 · ${SURFACE_LABEL[surface]}`;
  scroll.append(typeSec);

  for (const toggle of TOGGLES[surface]) {
    const dependsOff = toggle.requiresKey ? !st.toggles.get(toggle.requiresKey) : false;
    const row = createSwitchRow({
      label: toggle.label,
      hint: toggle.hint,
      checked: Boolean(st.toggles.get(toggle.key)),
      disabled: active || dependsOff,
      onChange: (checked) => void handleToggleChange(surface, st, toggle, checked),
    });
    scroll.append(row);
  }

  if (!active) {
    const foldContent = buildTaskFold(surface, st);
    const fold = createFold({ title: "本次任务", content: foldContent, open: st.files.length > 0 });
    scroll.append(fold.root);
  }

  card.append(scroll);
  card.append(buildRightFoot(surface, st, active));
  col.append(card);
  return col;
}

async function handleToggleChange(surface: Surface, st: SurfaceState, toggle: ToggleDef, checked: boolean): Promise<void> {
  st.toggles.set(toggle.key, checked);
  if (toggle.exclusiveWith && checked) {
    st.toggles.set(toggle.exclusiveWith, false);
  }
  if (toggle.key === "untranslated" && !checked) {
    // 依赖「仅补译未翻译内容」的开关（Word 的保护封面）一并复位。
    for (const other of TOGGLES[surface]) {
      if (other.requiresKey === toggle.key) st.toggles.set(other.key, false);
    }
  }
  if (toggle.pathKind === "flat" && toggle.path) {
    await persistSettings(nestedPatch(toggle.path, checked));
  } else if (toggle.pathKind === "output" && toggle.path) {
    await persistSettings(nestedPatch(`${outputSettingPathPrefix(surface)}.${toggle.path}`, checked));
  }
  rerender(surface);
}

function buildTaskFold(surface: Surface, st: SurfaceState): HTMLElement {
  const wrap = el("div");
  if (surface !== "pdf") {
    const domainField = el("div", "field");
    const label = el("label");
    label.textContent = "专业领域 ";
    const link = el("span", "linklike");
    link.style.fontSize = "11px";
    link.textContent = "编辑 Prompt ↗ 设置";
    link.addEventListener("click", () => navigate("settings", { page: "params" }));
    label.append(link);
    domainField.append(label);
    const select = createSelectField({
      label: "",
      options: ["同步工程场景", "资料管理场景", "行政生活化场景", "自定义"].map((v) => ({ value: v, label: v })),
      value: st.domainPreset,
      onChange: (value) => {
        st.domainPreset = value;
        void persistSettings({ [`${surface}_domain_preset`]: value });
      },
    });
    domainField.append(select.select);
    wrap.append(domainField);
  }

  const outputField = createField("输出位置", buildOutputRadioRow(surface, st));
  wrap.append(outputField);
  return wrap;
}

function buildOutputRadioRow(surface: Surface, st: SurfaceState): HTMLElement {
  const row = el("div", "radio-row");
  const name = `out-${surface}`;
  const makeOption = (value: boolean, label: string) => {
    const wrapLabel = el("label");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = name;
    radio.checked = st.useCustomOutputDir === value;
    radio.addEventListener("change", () => {
      st.useCustomOutputDir = value;
      void persistSettings(nestedPatch(`${outputSettingPathPrefix(surface)}.use_custom_output_dir`, value));
      rerender(surface);
    });
    wrapLabel.append(radio, document.createTextNode(` ${label}`));
    return wrapLabel;
  };
  row.append(makeOption(false, "源目录内"), makeOption(true, "自定义"));
  const container = el("div");
  container.append(row);
  if (st.useCustomOutputDir) {
    const { root, input } = createTextField({
      label: "",
      value: st.customOutputDir,
      placeholder: "自定义输出目录路径…",
      onInput: (value) => {
        st.customOutputDir = value;
      },
    });
    input.addEventListener("change", () => {
      void persistSettings(nestedPatch(`${outputSettingPathPrefix(surface)}.custom_output_dir`, st.customOutputDir));
    });
    root.style.marginTop = "6px";
    container.append(root);
  }
  return container;
}

function buildRightFoot(surface: Surface, st: SurfaceState, active: boolean): HTMLElement {
  const foot = el("div", "rp-foot");
  if (!active) {
    const disabled = st.selected.size === 0;
    foot.append(createButton({
      label: disabled ? "开始翻译" : `开始翻译（${st.selected.size} 个文件）`,
      icon: disabled ? undefined : "play",
      variant: "primary",
      size: "big",
      disabled,
      onClick: () => void startTask(surface, st),
    }));
    return foot;
  }

  const task = st.task!;
  if (surface === "pdf" && task.task.state === "paused") {
    foot.append(createButton({ label: "继续翻译", icon: "play", variant: "primary", size: "big", onClick: () => void resumePdfTask(st) }));
    foot.append(createButton({ label: "结束暂停并收尾", icon: "stop", size: "big", onClick: () => void confirmEndPaused(st) }));
    const note = el("div", "ws-note");
    note.style.textAlign = "center";
    note.textContent = "收尾会保存已完成页并生成部分结果报告";
    foot.append(note);
    return foot;
  }
  if (surface === "pdf") {
    foot.append(createButton({ label: "暂停提交", icon: "pause", size: "big", onClick: () => void pausePdfTask(st) }));
    foot.append(createButton({ label: "安全停止", icon: "stop", variant: "danger", onClick: () => confirmStopTask(st) }));
    return foot;
  }
  foot.append(createButton({ label: "安全停止", icon: "stop", variant: "danger", size: "big", onClick: () => confirmStopTask(st) }));
  const note = el("div", "ws-note");
  note.style.textAlign = "center";
  note.textContent = "已完成的文件会保留，当前文件回滚为未开始";
  foot.append(note);
  return foot;
}

// ---------------------------------------------------------------------------
// 扫描
// ---------------------------------------------------------------------------

async function runScan(surface: Surface): Promise<void> {
  const st = states[surface];
  const path = st.sourcePath.trim();
  if (!path) return;
  try {
    const c = await getClient();
    const payload = { surface, path, include_images: surface === "pdf" && Boolean(st.toggles.get("pdfImages")) };
    const response = await c.request<JsonObject>("/api/sources/scan", { method: "POST", body: JSON.stringify(payload) });
    const result = record(response.result) && Object.keys(record(response.result)).length ? record(response.result) : response;
    const items = Array.isArray(result.items) ? (result.items as FileItem[]) : [];
    const skipped = Array.isArray(result.skipped) ? (result.skipped as ScanSkippedItem[]) : [];
    st.files = items;
    st.skipped = skipped;
    st.scanSummary = record(result.summary);
    st.selected = new Set(items.map((f) => f.path));
    st.showBanner = false;
    await persistSettings({ [`last_${surface}_source_folder`]: path });
    const toastMessage = surface === "pdf"
      ? `已扫描到 ${items.length} 个 PDF / 图片输入，跳过 ${skipped.length} 个。`
      : `已扫描到 ${items.length} 个${SURFACE_LABEL[surface]}文件。`;
    showToast({ message: toastMessage });
    rerender(surface);
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, "扫描失败。"), error: true });
  }
}

// ---------------------------------------------------------------------------
// 启动任务（含 xls/doc 兼容确认、并行风险确认）
// ---------------------------------------------------------------------------

function buildPayload(surface: Surface, st: SurfaceState): JsonObject {
  const payload: JsonObject = {
    surface,
    source_path: st.sourcePath,
    selected_paths: st.files.filter((f) => st.selected.has(f.path)).map((f) => f.path),
    untranslated_only: Boolean(st.toggles.get("untranslated")),
    target_lang: st.targetLang,
  };
  if (surface !== "pdf") payload.source_lang = st.sourceLang;
  if (surface === "excel") payload.allow_xls_fallback = st.allowXlsFallback;
  if (surface === "word") {
    payload.allow_doc_fallback = st.allowDocFallback;
    payload.protect_scheme_cover = Boolean(st.toggles.get("protectSchemeCover"));
  }
  if (surface === "pdf") payload.include_images = Boolean(st.toggles.get("pdfImages"));
  return payload;
}

async function startTask(surface: Surface, st: SurfaceState): Promise<void> {
  if (!st.sourcePath.trim() || !st.selected.size) return;
  if (surface !== "pdf" && st.sourceLang !== "auto" && st.sourceLang === st.targetLang) {
    showToast({ message: "源语言与目标语言相同，请重新选择。", error: true });
    return;
  }
  if (surface === "excel") {
    const riskyCount = st.files.filter((f) => st.selected.has(f.path) && isRisky("excel", f)).length;
    if (riskyCount > 0 && !st.allowXlsFallback) {
      showCompatibilityModal(surface, st, riskyCount);
      return;
    }
  }
  if (surface === "word") {
    const riskyCount = st.files.filter((f) => st.selected.has(f.path) && isRisky("word", f)).length;
    if (riskyCount > 0 && !st.allowDocFallback) {
      showCompatibilityModal(surface, st, riskyCount);
      return;
    }
  }
  await preflightAndSubmit(surface, st);
}

function showCompatibilityModal(surface: Surface, st: SurfaceState, count: number): void {
  const legacyExt = surface === "excel" ? ".xls" : ".doc";
  const finalExt = surface === "excel" ? ".xlsx" : ".docx";
  const appName = surface === "excel" ? "Excel" : "Word";
  openModal({
    tone: "warn",
    icon: "warn",
    title: `${legacyExt} 转换方式确认`,
    body: [
      `已选择 ${count} 个旧版 ${legacyExt} 文件。最终结果统一输出为 ${finalExt}，源文件不会被改写。`,
      `优先高保真会通过本机 ${appName} 自动化转换；若 ${appName} 未安装、自动化被拒绝或单文件转换失败，该文件会明确失败，其他文件仍可继续，绝不静默改用兼容模式。`,
      "允许兼容转换会在高保真不可用时继续处理，但复杂样式、合并单元格、图片、图表和宏可能无法完整保留；这项选择只冻结到本次任务。",
    ],
    actions: [
      { label: "取消" },
      {
        label: "优先高保真",
        onClick: () => {
          if (surface === "excel") st.allowXlsFallback = false;
          else st.allowDocFallback = false;
          void preflightAndSubmit(surface, st);
        },
      },
      {
        label: "允许兼容转换",
        variant: "primary",
        onClick: () => {
          if (surface === "excel") st.allowXlsFallback = true;
          else st.allowDocFallback = true;
          void preflightAndSubmit(surface, st);
        },
      },
    ],
  });
}

async function preflightAndSubmit(surface: Surface, st: SurfaceState): Promise<void> {
  const payload = buildPayload(surface, st);
  try {
    const c = await getClient();
    const preflight = await c.preflightTask(payload);
    if (preflight.requires_confirmation) {
      showTaskRiskModal(surface, st, payload, preflight.confirmation_token ?? "");
      return;
    }
    await submitTaskStart(surface, st, payload);
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, "任务准备失败。"), error: true });
  }
}

/** 简化版并行风险确认：main.ts 原版还会展示共享连接明细表、活动任务列表、候选任务快照——
 *  这里按品牌要求的措辞保留决策要点，略去表格化明细，见文件尾「已确认简化」说明。 */
function showTaskRiskModal(surface: Surface, st: SurfaceState, payload: JsonObject, token: string): void {
  openModal({
    tone: "warn",
    icon: "warn",
    title: "共享 API 并行风险",
    body: [
      "此任务将与现有活动任务共用至少一个实际 API 连接。继续后会按新任务自己的默认吞吐启动，不会自动减半；服务端会在启动时用一次性令牌原子复检。",
      "可能出现 429、排队、超时、失败或额外费用。同一连接的并发会累加。上游返回并发限制时，只会降低当前共享组的运行时容量，不会修改长期模型吞吐设置。",
    ],
    actions: [
      { label: "取消" },
      { label: "仍要并行启动", variant: "primary", onClick: () => void submitTaskStart(surface, st, payload, token) },
    ],
  });
}

async function submitTaskStart(surface: Surface, st: SurfaceState, payload: JsonObject, confirmationToken = ""): Promise<void> {
  try {
    const c = await getClient();
    const body = confirmationToken ? { ...payload, confirmation_token: confirmationToken } : payload;
    const task = await c.request<TaskStatus>("/api/tasks", { method: "POST", body: JSON.stringify(body) });
    focusTask(surface, task);
    st.showBanner = false;
    initFileStages(st);
    rerender(surface);
    watchTask(surface);
    if (surface === "pdf") void fetchPdfPagesSnapshot(surface, task.task_id);
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, "启动任务失败。"), error: true });
  }
}

function focusTask(surface: Surface, task: TaskStatus): void {
  const st = states[surface];
  st.task = {
    task,
    logs: [],
    phaseName: "正在准备任务",
    percent: 0,
    streamState: "connected",
    watcherActive: false,
    fileStage: new Map(),
  };
  st.lastTaskId = task.task_id;
}

function initFileStages(st: SurfaceState): void {
  const local = st.task;
  if (!local) return;
  for (const file of st.files) {
    if (st.selected.has(file.path)) local.fileStage.set(file.path, "queued");
  }
}

// ---------------------------------------------------------------------------
// SSE 订阅
// ---------------------------------------------------------------------------

function watchTask(surface: Surface): void {
  const st = states[surface];
  const local = st.task;
  if (!local || local.watcherActive || local.task.terminal) return;
  local.watcherActive = true;
  const taskId = local.task.task_id;
  void (async () => {
    try {
      const c = await getClient();
      await c.streamTask(taskId, (event) => handleTaskEvent(surface, taskId, event), {
        onConnectionState: (state) => {
          if (states[surface].task?.task.task_id !== taskId) return;
          states[surface].task!.streamState = state;
          rerender(surface);
        },
      });
      if (states[surface].task?.task.task_id === taskId && !states[surface].task!.task.terminal) {
        await refetchTask(surface, taskId);
      }
    } catch {
      await refetchTask(surface, taskId).catch(() => {
        markTaskInterrupted(surface, taskId);
      });
    } finally {
      if (states[surface].task?.task.task_id === taskId) states[surface].task!.watcherActive = false;
    }
  })();
}

async function refetchTask(surface: Surface, taskId: string): Promise<void> {
  const c = await getClient();
  const task = await c.getTask(taskId);
  if (states[surface].task?.task.task_id !== taskId) return;
  states[surface].task!.task = task;
  // 终态前把最后一次逐页快照拉齐，保证「查看对比」在任务结束后仍能用上最新数据。
  if (surface === "pdf") await fetchPdfPagesSnapshot(surface, taskId);
  if (task.terminal) finishTask(surface, task);
  else rerender(surface);
}

function markTaskInterrupted(surface: Surface, taskId: string): void {
  const st = states[surface];
  const local = st.task;
  if (!local || local.task.task_id !== taskId) return;
  local.task = { ...local.task, state: "interrupted", terminal: true };
  local.phaseName = "sidecar 已重启或应用异常退出；本任务不能继续，请依据已生成产物或清单新建任务。";
  showToast({ message: "与后台的连接已中断，任务标记为已中断。", error: true });
  finishTask(surface, local.task);
}

function handleTaskEvent(surface: Surface, taskId: string, event: SseEvent): void {
  const st = states[surface];
  const local = st.task;
  if (!local || local.task.task_id !== taskId) return;
  const data = event.data;
  switch (event.type) {
    case "log": {
      local.logs.push({ time: formatTime(num(data.ts, 0) || undefined), level: text(data.level, "info"), message: redactedText(data.message ?? data.text, "") });
      break;
    }
    case "progress": {
      if (data.phase !== undefined || data.stage !== undefined) local.phaseName = redactedText(data.phase ?? data.stage, local.phaseName);
      const done = num(data.step_done, num(data.done));
      const total = num(data.step_total, num(data.total));
      if (total > 0) local.percent = Math.min(100, (done / total) * 100);
      markActiveFile(local, st);
      break;
    }
    case "status": {
      local.phaseName = redactedText(data.message ?? data.phase, local.phaseName);
      markActiveFile(local, st);
      break;
    }
    case "stopping":
      local.phaseName = "正在安全停止…";
      break;
    case "paused":
      local.phaseName = "已暂停提交新页面";
      if (surface === "pdf") void fetchPdfPagesSnapshot(surface, taskId);
      break;
    case "resumed":
      local.phaseName = "正在继续提交页面";
      if (surface === "pdf") void fetchPdfPagesSnapshot(surface, taskId);
      break;
    case "word_recovery":
      local.wordRecovery = data;
      break;
    case "pdf_page_recovery":
      local.pdfPageRecovery = data;
      if (surface === "pdf") void fetchPdfPagesSnapshot(surface, taskId);
      break;
    case "pdf_review":
      local.pdfReview = data;
      if (surface === "pdf") void fetchPdfPagesSnapshot(surface, taskId);
      break;
    case "pdf_page_action":
      // 没有聚合本地状态需要更新——这个事件纯粹是「去拉一次逐页快照」的信号。
      if (surface === "pdf") void fetchPdfPagesSnapshot(surface, taskId);
      break;
    case "done":
    case "completed_with_issues":
    case "error":
    case "stopped":
    case "interrupted": {
      local.task = { ...local.task, state: event.type as TaskStatus["state"], terminal: true, result: (data as JsonObject) ?? local.task.result };
      for (const path of local.fileStage.keys()) {
        if (local.fileStage.get(path) !== "error") local.fileStage.set(path, "done");
      }
      if (surface === "pdf") void fetchPdfPagesSnapshot(surface, taskId);
      finishTask(surface, local.task);
      return;
    }
    default:
      break;
  }
  rerender(surface);
}

function markActiveFile(local: LocalTask, st: SurfaceState): void {
  if (!local.phaseName) return;
  for (const file of st.files) {
    if (!local.fileStage.has(file.path)) continue;
    if (local.phaseName.includes(fileLabel(file))) {
      for (const [path, stage] of local.fileStage) {
        if (path === file.path) local.fileStage.set(path, "active");
        else if (stage === "active") local.fileStage.set(path, "done");
      }
      break;
    }
  }
}

function finishTask(surface: Surface, task: TaskStatus): void {
  const st = states[surface];
  st.hasEverCompleted = true;
  const result = record(task.result);
  const summary = record(result.summary);
  const generated = num(summary.generated_count, num(result.generated_count, st.selected.size));
  const review = num(summary.review_count, num(result.review_count, 0));
  const autoFixed = num(summary.auto_fixed_count, num(result.auto_fixed_count, 0));
  const outputPath = text(summary.output_dir, text(result.output_dir, st.sourcePath));
  st.lastOutputPath = outputPath;
  const clauses: string[] = [];
  if (review > 0) clauses.push(`${review} 处需复核`);
  if (autoFixed > 0) clauses.push(`${autoFixed} 处已自动处理`);
  clauses.push(review > 0 || autoFixed > 0 ? "其余全部通过" : "全部通过");
  clauses.push(`输出至 ${outputPath}`);
  const isError = task.state === "error" || task.state === "interrupted";
  st.bannerInfo = isError
    ? { title: "任务未完成", subtitle: redactedText(result.message, "任务在结束前中断，已生成的文件仍保留在输出目录。") }
    : { title: `已生成 ${generated} 个文件`, subtitle: clauses.join(" · ") };
  st.showBanner = true;
  st.files = [];
  st.skipped = [];
  st.selected = new Set();
  st.sourcePath = "";
  if (isError) showToast({ message: redactedText(result.message, "任务未能顺利完成。"), error: true });
  rerender(surface);
}

// ---------------------------------------------------------------------------
// 停止 / 暂停 / 继续 / 结束暂停
// ---------------------------------------------------------------------------

function confirmStopTask(st: SurfaceState): void {
  const taskId = st.task?.task.task_id;
  if (!taskId) return;
  openModal({
    tone: "warn",
    icon: "stop",
    sourceLabel: "停止运行中的任务",
    title: "安全停止当前任务？",
    body: ["已生成的文件会保留在输出目录；Excel、Word 会结束为终态。PDF / 图片应先「暂停提交」，再选择继续或结束暂停。"],
    actions: [
      { label: "继续执行" },
      { label: "安全停止", variant: "danger-solid", onClick: () => void stopTask(taskId) },
    ],
  });
}

async function stopTask(taskId: string): Promise<void> {
  try {
    const c = await getClient();
    await c.request(`/api/tasks/${taskId}/stop`, { method: "POST" });
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, "停止任务失败。"), error: true });
  }
}

async function pausePdfTask(st: SurfaceState): Promise<void> {
  const taskId = st.task?.task.task_id;
  if (!taskId) return;
  try {
    const c = await getClient();
    await c.request(`/api/tasks/${taskId}/pause`, { method: "POST" });
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, "暂停失败。"), error: true });
  }
}

async function resumePdfTask(st: SurfaceState): Promise<void> {
  const taskId = st.task?.task.task_id;
  if (!taskId) return;
  try {
    const c = await getClient();
    await c.request(`/api/tasks/${taskId}/resume`, { method: "POST" });
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, "继续任务失败。"), error: true });
  }
}

/** 与 main.ts / tasks.ts 一致：结束暂停用原生 window.confirm，而不是 openModal——两处既有实现
 *  都这么做，这里保持同一先例，不额外造一种新的确认样式。 */
function confirmEndPaused(st: SurfaceState): void {
  const taskId = st.task?.task.task_id;
  if (!taskId) return;
  const ok = window.confirm("结束暂停任务将不再提交未处理页面，但会写入并保留已完成页面、素材、清单和报告。是否结束？");
  if (!ok) return;
  void (async () => {
    try {
      const c = await getClient();
      await c.request(`/api/tasks/${taskId}/end-paused`, { method: "POST" });
    } catch (error) {
      showToast({ message: redactedText((error as Error)?.message, "结束暂停失败。"), error: true });
    }
  })();
}

async function openTaskLocalFile(path: string | undefined, reveal: boolean): Promise<void> {
  if (!path) {
    showToast({ message: "没有可打开的输出路径。", error: true });
    return;
  }
  try {
    await invoke("open_local_path", { path, reveal });
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, "打开路径失败。"), error: true });
  }
}
