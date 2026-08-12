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

import { ApiClient, apiErrorReason, type PdfPage, type PdfPageFile, type PdfPagesSnapshot, type SseEvent, type TaskStatus } from "../api-client";
import {
  createBanner,
  createButton,
  createChip,
  createEmptyState,
  createField,
  createFold,
  createLanguageField,
  createProgressBar,
  createSelectField,
  createSwitchRow,
  createTextField,
  closeLanguagePopover,
  closeMenu,
  hideHint,
  openMenu,
  openModal,
  showToast,
  type ChipTone,
  type LanguageOption,
  type ModalHandle,
  type StatusTone,
} from "../components";
import { icon, type IconName } from "../icons";
import { navigate, type ViewParams } from "../router";
import { setTopbar } from "../shell";
import { taskStateWord } from "../task-state-labels";
// 任务中心是活动任务徽标的权威来源；新任务刚提交时要主动通知它，别等它自己巡检。
import { noteTaskStarted } from "./tasks";

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
  /** PDF「大幅面页」计数（两个方向都超过 A4 约 15%）。null 是后端明确表达的「这个文件数不出来」
   *  （加密、结构损坏），不是 0；字段缺失表示这个 surface / 这版后端不产出。三态含义见
   *  oversizedPageStats() 上方的整段说明，处理方式与 Excel 的 image_count 完全一致。 */
  oversized_page_count?: number | null;
  source_type?: string;
  needs_conversion?: boolean;
  // 以下几个是 Excel 的「单元格外内容」计数，见下方 outsideCellStats() 的整段说明。
  // null 是后端明确表达的「这个文件数不出来」，不是 0；字段缺失则是「这一版后端不产出」。
  image_count?: number | null;
  shape_text_count?: number | null;
  comment_count?: number | null;
  anchor_frozen_count?: number;
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
  { key: "protectFrontMatter", label: "保护封面和目录", hint: "从文档开头一直保留到正文第一个章节标题为止，封面、批准页、目录、前言都不翻译。章节标题按「第一章」「1 概述」「1.1 概述」「（一）」以及 Word 内置的标题样式识别，目录里的同名条目不算。识别不到正文起点时不启用保护，会在日志中说明。全译和补译都生效。", default: false, pathKind: "none" },
  { key: "translateHeadersFooters", label: "翻译页眉页脚", hint: "默认不翻。开启后页眉页脚的文字也会翻译，译文用「 / 」接在同一行原文后面，不另起一行——页眉高度是固定的，多一行会把正文顶下去。只有页码、目录域的页眉页脚仍然跳过。全译和补译都生效。", default: false, pathKind: "none" },
];

const PDF_TOGGLES: ToggleDef[] = [
  { key: "pdfReview", label: "逐页审核模型", hint: "开启后由审核模型逐页复核译文；审核模型的配置与连接状态会和任务一起冻结。", default: true, pathKind: "flat", path: "pdf.review_enabled" },
  { key: "pdfCompressed", label: "生成压缩 PDF", hint: "在原始输出之外额外生成一份体积更小的 PDF。", default: true, pathKind: "flat", path: "pdf.generate_compressed_pdf" },
  { key: "pdfImages", label: "允许独立图片", hint: "只决定 PNG、JPG/JPEG、WebP、BMP、TIF/TIFF 是否作为独立输入扫描；PDF 页面一律按版式协议处理。", default: false, pathKind: "flat", path: "pdf.include_images" },
  // 判定条件只看纸张尺寸，和内容是不是图纸无关，所以开关名必须是尺寸口径——叫「跳过图纸」
  // 会让人以为程序在识别图纸内容，遇到 A3 的宣传册被跳过时只会当成程序出错。
  { key: "skipOversizedPages", label: "跳过 A3 及更大的页面", hint: "工程图纸通常打印成 A3 或更大的幅面，这类页面一般不需要翻译。两个方向都超过 A4 约 15% 时判定为大幅面页（A3 及以上），横放竖放都算。这些页不送翻译模型，原始内容整页照搬到输出文件，清晰度不变。比 A4 稍大一点的页面（例如扫描时多出来的白边）不会被误判。", default: false, pathKind: "flat", path: "pdf.skip_oversized_pages" },
];

const TOGGLES: Record<Surface, ToggleDef[]> = { excel: EXCEL_TOGGLES, word: WORD_TOGGLES, pdf: PDF_TOGGLES };

const SURFACE_LABEL: Record<Surface, string> = { excel: "Excel", word: "Word", pdf: "PDF" };
const SURFACE_ICON: Record<Surface, IconName> = { excel: "excel", word: "word", pdf: "pdf" };
const SURFACE_PAGE_TITLE: Record<Surface, string> = { excel: "Excel 表格翻译", word: "Word 文档翻译", pdf: "PDF 与图片翻译" };
const SURFACE_FILE_NOUN: Record<Surface, string> = { excel: "表格文件", word: "文档", pdf: "PDF / 图片文件" };

/** 「5 个 PDF / 图片文件」——名词以西文字母开头时补一个空格，否则会挤成「5 个PDF」。 */
function fileNounPhrase(surface: Surface, count: number): string {
  const noun = SURFACE_FILE_NOUN[surface];
  return `${count} 个${/^[A-Za-z]/.test(noun) ? " " : ""}${noun}`;
}
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
  /** seq 是全局单调递增的行号，只用来在整页重建后认出「重建前顶在视口口沿的是哪一行」
   *  （见 captureLogScroll / restoreLogScroll）。数组索引不行——满 200 条后每来一条
   *  就从头部丢一条，同一条日志的索引会一直变小。 */
  logs: Array<{ seq: number; time: string; level: string; message: string }>;
  phaseName: string;
  percent: number;
  /** 当前阶段序号 / 总阶段数（progress 事件的 phase_index / phase_total）。用来把
   *  阶段内进度折算成整条进度，也用来在进度卡上写明「阶段 2 / 4」。 */
  phaseIndex: number;
  phaseTotal: number;
  streamState: "connected" | "reconnecting";
  watcherActive: boolean;
  /** 最后一次收到任何事件的时刻。一批请求发出去到回来之间引擎不说话，界面得自己
   *  报一句「还在等」，否则十几秒没动静看起来和卡死一模一样。 */
  lastEventAt: number;
  fileStage: Map<string, "queued" | "active" | "done" | "error">;
  wordRecovery?: JsonObject;
  pdfPageRecovery?: JsonObject;
  pdfReview?: JsonObject;
  /** 逐页拉取的快照（GET /pdf-pages）。没有逐页 SSE——收到聚合事件或任务状态变化时重新拉取一次，
   *  见 fetchPdfPagesSnapshot() 与它的调用点清单。终态后不清空，保留最后一次结果供「查看对比」使用。 */
  pdfPagesSnapshot?: PdfPagesSnapshot;
}

/** 结果的三种口气：全部产出、产出了但有事要说、没能产出。 */
type ResultTone = "ok" | "warn" | "fail";

interface BannerInfo {
  title: string;
  subtitle: string;
  tone: ResultTone;
  /** 顶栏那颗徽章的字。和横幅同一份判断，不再靠对副标题做正则猜结论。 */
  statusLabel: string;
  /** 没有任何产出时不给「打开输出目录」——那个目录里没有用户要的东西。 */
  hasOutput: boolean;
}

interface FileOutcome {
  produced: boolean;
  label: string;
  detail: string;
}

interface SurfaceState {
  sourcePath: string;
  /** 「浏览 → 选择文件」一次挑了多个文件时的原始清单；sourcePath 存它们共同的上级
   *  目录（任务的 source_path 要能被后端重扫，selected_paths 再收窄到这几个文件）。
   *  手输路径、选文件夹、只选一个文件时都是空数组。 */
  sourcePaths: string[];
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
  bannerInfo: BannerInfo | null;
  /** 上一次任务的逐文件结果，键是文件名（扫描项的 `name` 与 file_results[].name 同源，
   *  都来自 FileItem.name）。任务结束后清单不再清空，这份结果用来在「状态」列上标出
   *  哪几个文件真的产出了、哪几个没有。
   *
   *  绝对不能用 source_path 做键：它在 api/task_manager.py 的隐私过滤里被置空，
   *  经过 API 回来永远是 null，映射会是空的（9.2.6 就是这么错的）。 */
  fileOutcomes: Map<string, FileOutcome>;
  /** Excel 完成汇总里那句「哪些内容没被翻译」。finishTask 会清空 st.files，
   *  所以必须在清空前把要说的数据快照下来，跟着完成横幅一起展示。 */
  excelDoneNotice: ExcelDoneNotice | null;
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
    sourcePaths: [],
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
    fileOutcomes: new Map(),
    excelDoneNotice: null,
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
  if (!connectPromise) {
    const attempt = client.connect();
    // 与 ensureBootstrap 同理，而且这一层更致命：connect() 里那记 /health 撞上还没起来的
    // 后端就会 reject，缓存住之后**整个工作区的每一个请求**都会立刻失败，连「重试」也修不好
    // ——重试第一步就是 getClient()。失败即置空，让下一次调用真的重连。
    attempt.catch(() => {
      if (connectPromise === attempt) connectPromise = null;
    });
    connectPromise = attempt;
  }
  await connectPromise;
  return client;
}

let settings: JsonObject = {};
// 语言目录整条留着（含后端给的 aliases），可搜索选择器要靠 aliases 匹配英文名。
let languageOptions: { source: LanguageOption[]; target: LanguageOption[] } = { source: [], target: [] };
let bootstrapPromise: Promise<void> | null = null;
let bootstrapAdopted = new Set<Surface>();

async function ensureBootstrap(): Promise<void> {
  if (!bootstrapPromise) {
    const attempt = (async () => {
      const c = await getClient();
      const [settingsPayload, languagesPayload] = await Promise.all([
        c.request<JsonObject>("/api/settings"),
        c.request<{ source_options: LanguageOption[]; target_options: LanguageOption[] }>("/api/languages"),
      ]);
      settings = settingsPayload;
      languageOptions = { source: languagesPayload.source_options ?? [], target: languagesPayload.target_options ?? [] };
      applySettingsToStates();
    })();
    // 失败的 promise 绝不能留在缓存里。设置和语言目录都靠这一趟拿，缓存住一个 rejected
    // promise，等于把「后端冷启动慢了一秒」变成「这个窗口的语言选择器永久瘫痪」——而且
    // 瘫得很像功能本身有毛病：按钮回落成裸代码（en 而不是「英文」），浮层过滤空目录后
    // 显示「没有匹配的语言，换个说法试试」，把加载失败说成用户搜错了词。
    // 置空之后，下一次挂载（切个页面再切回来）或空态里的「重试」会重新拉。
    attempt.catch(() => {
      if (bootstrapPromise === attempt) bootstrapPromise = null;
    });
    bootstrapPromise = attempt;
  }
  await bootstrapPromise;
}

/** 丢掉已缓存的引导结果，下一次 ensureBootstrap 重新拉设置与语言目录。 */
function resetBootstrap(): void {
  bootstrapPromise = null;
}

/** 语言目录空态里「重试」的动作：重新引导，成功就把这一片重画出来。 */
async function reloadBootstrap(surface: Surface): Promise<void> {
  resetBootstrap();
  try {
    await ensureBootstrap();
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, "重新加载语言列表失败。"), error: true });
    return;
  }
  states[surface].renderer?.();
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
  // 中途切走再回来时任务还在跑（st.task 是模块级状态），静默计时器得跟着这一屏重新起。
  if (st.task && !st.task.task.terminal) startSilenceTicker(surface);
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
  // 离开这一屏后没人看那句「已等待 N 秒」，计时器再走就是白转（rerender 也已经是空操作）。
  // 任务本身不受影响：事件流由 watchTask 维护，回到这一屏时 startSilenceTicker 会重新起。
  stopSilenceTicker(surface);
  // 提示气泡、语言选择器浮层、「浏览」按钮的锚定菜单都挂在 document.body 上，不随 container
  // 一起被清掉。不主动关就会留下一个悬在半空的面板，而且模块级的「当前展开项」指针还指着已死的闭包。
  hideHint();
  closeLanguagePopover();
  closeMenu();
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

// 运行日志的滚动位置：旧版本没有这套逻辑，这里是新写的。整页每次重建都会把 .log
// 连同其它 DOM 一起扔掉重建，浏览器不会替你记滚动条——不记就记，不然用户往上翻看历史时
// 一来新日志就被强制拽回底部。只有「用户本来就停在底部」（容差 8px）才在重建后继续贴底，
// 否则原样恢复重建前的位置。
const LOG_SCROLL_STICK_TOLERANCE = 8;

/**
 * 还原滚动位置不能直接照抄 scrollTop。日志攒到 LOG_VIEW_LIMIT 条以后，每来一条新日志
 * 就同时丢掉最旧一条、追加最新一条：scrollHeight 一点没变，可整段内容却往上挪了一行。
 * 这时把 scrollTop 原样写回去，用户看到的内容就一行一行往上漂——他往上翻历史，日志
 * 每来一条就把他顶走一行，等于「不抢滚动位置」这条规矩在满 200 条之后彻底失效。
 * 距离底部（scrollHeight - scrollTop - clientHeight）同样救不了，因为它在这种情况下
 * 也完全不变。
 *
 * 所以改成锚点还原：记下重建前顶在视口口沿的那一行是第几条（seq 全局单调递增）、以及
 * 它的上沿相对视口上沿差多少像素；重建后把同一条日志摆回同样的位置。行高不一致
 * （长消息会折行）也不影响，因为量的是这一行本身的实际位置，不是「几行 × 行高」。
 */
interface LogScrollState {
  atBottom: boolean;
  scrollTop: number;
  /** 视口顶部那一行的日志序号；日志为空时为 null。 */
  anchorSeq: number | null;
  /** 锚点行上沿减去视口上沿，通常是 0 或负数（这一行被视口切掉了一截）。 */
  anchorOffset: number;
}

function captureLogScroll(container: HTMLElement): LogScrollState | null {
  const prev = container.querySelector<HTMLElement>(".log");
  if (!prev) return null;
  const distanceFromBottom = prev.scrollHeight - prev.scrollTop - prev.clientHeight;
  let anchorSeq: number | null = null;
  let anchorOffset = 0;
  const viewTop = prev.getBoundingClientRect().top;
  for (const line of Array.from(prev.children) as HTMLElement[]) {
    const seq = line.dataset.seq;
    if (seq === undefined) continue;
    const rect = line.getBoundingClientRect();
    // 第一条下沿还在视口里的行，就是用户眼下看到的最上面那行。
    if (rect.bottom > viewTop) {
      anchorSeq = Number(seq);
      anchorOffset = rect.top - viewTop;
      break;
    }
  }
  return { atBottom: distanceFromBottom <= LOG_SCROLL_STICK_TOLERANCE, scrollTop: prev.scrollTop, anchorSeq, anchorOffset };
}

function restoreLogScroll(container: HTMLElement, prevScroll: LogScrollState | null): void {
  const next = container.querySelector<HTMLElement>(".log");
  if (!next) return;
  // 没有旧状态（比如第一次渲染日志卡片）按贴底处理，最新的日志本来就该先看到。
  if (!prevScroll || prevScroll.atBottom) {
    next.scrollTop = next.scrollHeight;
    return;
  }
  if (prevScroll.anchorSeq !== null) {
    const anchor = next.querySelector<HTMLElement>(`[data-seq="${prevScroll.anchorSeq}"]`);
    if (anchor) {
      next.scrollTop += anchor.getBoundingClientRect().top - next.getBoundingClientRect().top - prevScroll.anchorOffset;
      return;
    }
    // 锚点行已经被 200 条上限挤掉了：用户正在看的那段日志已经不在缓冲区里，
    // 退到现存最旧的一条，而不是沿用旧 scrollTop——那会把他丢到一段没看过的位置。
    next.scrollTop = 0;
    return;
  }
  next.scrollTop = prevScroll.scrollTop;
}

function renderInto(container: HTMLElement, surface: Surface): void {
  const logScroll = captureLogScroll(container);
  while (container.firstChild) container.removeChild(container.firstChild);
  container.removeAttribute("style");
  const st = states[surface];
  const active = Boolean(st.task && !st.task.task.terminal);

  updateTopbar(surface, st, active);

  if (st.showBanner && st.bannerInfo) {
    container.style.flexDirection = "column";
    container.append(buildBanner(surface, st));
    // 完成横幅之下再补一句「哪些内容没被翻译」——横幅只说做了什么，这句说没做什么。
    if (surface === "excel" && st.excelDoneNotice) container.append(buildExcelDoneCard(st.excelDoneNotice));
    const row = el("div");
    row.style.cssText = "flex:1;display:flex;gap:16px;min-height:0";
    row.append(buildColLeft(surface, st, active), buildColRight(surface, st, active));
    container.append(row);
  } else {
    container.append(buildColLeft(surface, st, active), buildColRight(surface, st, active));
  }
  restoreLogScroll(container, logScroll);
}

function rerender(surface: Surface): void {
  const st = states[surface];
  st.renderer?.();
}

function updateTopbar(surface: Surface, st: SurfaceState, active: boolean): void {
  if (st.showBanner && st.bannerInfo) {
    // 状态直接来自任务终态和逐文件结果（见 finishTask 的 resultTone）。以前这里靠
    // 对副标题正则匹配「需复核」来决定颜色，于是失败的任务也顶着一个绿色「已完成」。
    const info = st.bannerInfo;
    setTopbar({
      title: SURFACE_PAGE_TITLE[surface],
      status: { label: info.statusLabel, tone: info.tone === "fail" ? "danger" : info.tone },
      subtitle: info.tone === "fail"
        ? "任务已归档到任务中心，可在完整报告里看失败原因"
        : "任务已归档到任务中心，可随时回看完整报告",
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
      : `${fileNounPhrase(surface, fileCount)} · ${sourceLabel(st)} → ${targetLabel(st)}${domainSuffix}`;
    setTopbar({ title: SURFACE_PAGE_TITLE[surface], status: meta, subtitle });
    return;
  }
  if (st.files.length > 0) {
    setTopbar({
      title: SURFACE_PAGE_TITLE[surface],
      status: { label: "已扫描 · 待开始", tone: "run" },
      subtitle: `已找到 ${fileNounPhrase(surface, st.files.length)}，勾选后开始翻译`,
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
  const actions: HTMLElement[] = [];
  // 一个文件都没产出时不给「打开输出目录」：点进去只有空目录，等于骗他跑一趟。
  if (info.hasOutput) {
    actions.push(createButton({
      label: "打开输出目录",
      icon: "folder",
      onClick: () => openTaskLocalFile(st.lastOutputPath, true),
    }));
  }
  actions.push(createButton({
    label: info.tone === "fail" ? "查看失败原因" : "查看完整报告",
    icon: "ext",
    onClick: () => navigate("tasks", { taskId: st.lastTaskId }),
  }));
  return createBanner({
    title: info.title,
    subtitle: info.subtitle,
    icon: info.tone === "ok" ? "check" : "warn",
    tone: info.tone,
    actions,
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
    // 单元格外内容的说明只属于 Excel：Word / PDF 没有这条限制，不该出现这段提示。
    if (surface === "excel") {
      const notice = buildOutsideCellBanner(st);
      if (notice) col.append(notice);
    }
    col.append(buildTableCard(surface, st));
  }
  return col;
}

function buildSrcBar(surface: Surface, st: SurfaceState): HTMLElement {
  const bar = el("div", "card srcbar");
  const scanning = scanBusy[surface];
  const scanBtn = createButton({
    // 扫描在飞时两个入口都锁住：再点一次只会多一个在途请求，
    // 而先发后到的那个会把清单换成上一个路径的内容（见 runScan 的请求序号）。
    label: scanning ? "扫描中…" : st.files.length > 0 ? "重新扫描" : "扫描",
    variant: "primary",
    disabled: scanning || !st.sourcePath.trim(),
    onClick: () => void runScan(surface),
  });
  const { root, input } = createTextField({
    label: "",
    value: st.sourcePath,
    placeholder: "选择或粘贴文件、文件夹路径…",
    onInput: (value) => {
      st.sourcePath = value;
      // 手输/粘贴就不再是"刚才多选的那几个文件"了，多选清单必须一起作废。
      st.sourcePaths = [];
      // 手输/粘贴路径时同步解锁「扫描」——这一栏不整页重建，按钮得自己更新。
      // 扫描在飞时仍然保持锁定，光有路径不算能点。
      scanBtn.disabled = scanBusy[surface] || !value.trim();
    },
  });
  root.style.margin = "0";
  root.style.flex = "1";
  input.addEventListener("change", () => {
    void persistSettings({ [`last_${surface}_source_folder`]: st.sourcePath });
  });
  bar.append(input);
  const browseBtn = createButton({
    label: "浏览",
    icon: "folder",
    disabled: scanning,
    title: scanning ? "正在扫描当前路径，完成后再选新的来源。" : undefined,
    onClick: () => {
      openMenu(browseBtn, [
        { label: "选择文件夹…", description: "递归扫描目录下所有可翻译文件", onSelect: () => void pickSource(surface, st, input, true) },
        { label: `选择${SURFACE_FILE_NOUN[surface]}…`, description: "可多选，只扫描并翻译选中的文件", onSelect: () => void pickSource(surface, st, input, false) },
      ]);
    },
  });
  bar.append(browseBtn);
  bar.append(scanBtn);
  return bar;
}

/** 单文件选择时的扩展名过滤，与后端 scanner 的 SUPPORTED_*_SUFFIXES 一一对应。 */
function sourceFileFilter(surface: Surface, st: SurfaceState): { name: string; extensions: string[] } {
  if (surface === "excel") return { name: "Excel 表格", extensions: ["xlsx", "xls"] };
  if (surface === "word") return { name: "Word 文档", extensions: ["docx", "doc"] };
  const images = st.toggles.get("pdfImages") ? ["png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"] : [];
  return { name: images.length ? "PDF 与图片" : "PDF", extensions: ["pdf", ...images] };
}

/** 取路径的上级目录。系统选择框一次只能在同一个文件夹里多选，取第一个的父目录即可。 */
function parentDirOf(path: string): string {
  const normalized = path.replace(/[\\/]+$/, "");
  const cut = Math.max(normalized.lastIndexOf("/"), normalized.lastIndexOf("\\"));
  return cut > 0 ? normalized.slice(0, cut) : normalized;
}

/**
 * 打开系统选择框。Tauri 的 dialog 插件把「选目录」和「选文件」做成两个互斥模式
 * （directory:true 时 filters 直接被忽略），所以这里由「浏览」菜单先定模式再调用；
 * 后端 scan 两种路径都支持，单文件走 Path.is_file() 分支；选文件时可以一次挑多个，
 * 后端按每个路径各扫一次再合并成一份清单。
 */
async function pickSource(surface: Surface, st: SurfaceState, input: HTMLInputElement, directory: boolean): Promise<void> {
  let picked: unknown;
  try {
    picked = directory
      ? await open({ title: "选择来源文件夹", directory: true, multiple: false })
      // 选文件不限数量，只限类型：filters 管类型，multiple 让用户一次挑几份。
      : await open({ title: `选择${SURFACE_FILE_NOUN[surface]}`, directory: false, multiple: true, filters: [sourceFileFilter(surface, st)] });
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, "选择来源失败。"), error: true });
    return;
  }
  const pickedList = (Array.isArray(picked) ? picked : [picked]).filter(
    (item): item is string => typeof item === "string" && item.trim().length > 0,
  );
  if (!pickedList.length) return;
  // 多选时 sourcePath 记它们共同的上级目录：任务启动要用它重扫，selected_paths 再收窄
  // 回这几个文件；清单和统计仍然只显示选中的这几份（扫描只扫这几个路径）。
  st.sourcePaths = pickedList.length > 1 ? pickedList : [];
  const displayPath = pickedList.length > 1 ? parentDirOf(pickedList[0]) : pickedList[0];
  // 先落地到界面（含解锁「扫描」），再去写设置：记住上次目录失败不该把刚选好的路径丢掉。
  st.sourcePath = displayPath;
  input.value = displayPath;
  rerender(surface);
  try {
    await persistSettings({ [`last_${surface}_source_folder`]: displayPath });
  } catch (error) {
    showToast({ message: redactedText((error as Error)?.message, "已选择来源，但记住上次目录失败。"), error: true });
  }
  // 走「浏览」选完路径就直接扫描，不用用户再点一次——手输/粘贴路径的场景不走这个函数，
  // 不受影响。复用 runScan 本身，扫描失败的提示和点按钮时完全一样。
  await runScan(surface);
}

interface StatCell {
  label: string;
  value: string;
  /** 数字标红（既有用法：旧版 .xls 计数）。 */
  warn?: boolean;
  /** 整格变黄（样张 .stat.attn）：这一格里的东西需要用户知道。 */
  attn?: boolean;
  /** 数字压成弱色（样张 .v.dash）：取值为 0 或「—」，没有信息量。 */
  dim?: boolean;
  /** 数字下面的一行小字（样张 .stat .sub）。 */
  sub?: string;
}

function buildStatsRow(surface: Surface, st: SurfaceState): HTMLElement {
  const stats = computeStats(surface, st);
  // 四格以内沿用固定 4 列；Excel 多出「图片 / 文本框」那一格后改自适应列宽，
  // 否则窄窗口下五格会被压得看不清标签。
  const wrap = el("div", stats.length > 4 ? "stats five" : "stats");
  for (const stat of stats) {
    const classes = ["stat"];
    if (!st.files.length) classes.push("dim");
    else if (stat.attn) classes.push("attn");
    const cell = el("div", classes.join(" "));
    const span = el("span");
    span.textContent = stat.label;
    const b = el("b");
    b.textContent = st.files.length ? stat.value : "—";
    if (stat.warn && st.files.length) b.style.color = "var(--warn)";
    if (stat.dim && st.files.length) b.className = "dash";
    cell.append(span, b);
    if (stat.sub && st.files.length) {
      const sub = el("div", "sub");
      sub.textContent = stat.sub;
      cell.append(sub);
    }
    wrap.append(cell);
  }
  return wrap;
}

function computeStats(surface: Surface, st: SurfaceState): StatCell[] {
  const files = st.files;
  if (surface === "excel") {
    const cells = files.reduce((sum, f) => sum + num(f.text_cell_count), 0);
    const sheets = files.reduce((sum, f) => sum + num(f.sheet_count, f.sheets?.length ?? 0), 0);
    const xls = files.filter((f) => isRisky(surface, f)).length;
    // outsideCellStat 在字段整批缺失时返回 null（不占版面），过滤掉——.five 修饰类挂在
    // buildStatsRow 里，靠的就是这里过滤后的实际格数,不需要另外同步。
    const cellStats: (StatCell | null)[] = [
      { label: "已扫描文件", value: String(files.length) },
      { label: "文本单元格", value: cells.toLocaleString("zh-CN") },
      { label: "工作表", value: String(sheets) },
      outsideCellStat(outsideCellStats(files)),
      { label: "旧版 .xls", value: String(xls), warn: xls > 0 },
    ];
    return cellStats.filter((stat): stat is StatCell => stat !== null);
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
  // 与 Excel 那格同样的约定：oversizedPageStat 在整批文件都不产出该字段时返回 null，
  // 过滤掉即可——.five 修饰类靠的就是过滤后的实际格数。
  const pdfStats: (StatCell | null)[] = [
    { label: "已扫描文件", value: String(files.length) },
    { label: "总页数", value: String(pages) },
    oversizedPageStat(oversizedPageStats(files)),
    { label: "独立图片", value: String(images) },
    { label: "跳过项", value: String(st.skipped.length) },
  ];
  return pdfStats.filter((stat): stat is StatCell => stat !== null);
}

function isRisky(surface: Surface, f: FileItem): boolean {
  if (surface === "excel") return f.format === "xls" || Boolean(f.needs_conversion);
  if (surface === "word") return f.format === "doc" || Boolean(f.needs_conversion);
  return false;
}

function fileLabel(f: FileItem): string {
  return text(f.name, f.relative_path ?? f.path.split("/").pop() ?? f.path);
}

// ---------------------------------------------------------------------------
// Excel「单元格外内容」——嵌入图片里的字、文本框/形状里的字
//
// 这些内容目前一律不翻译：写入器逐字节原样保留它们。本节不改变这个行为，只负责把它
// 说出来——用户看到「翻译完成」时，默认理解是「整个文件都翻了」，而实际上图片里的字
// 一个都没动，不说清楚就是误导。
//
// 三种状态必须区分开，绝不能合并：
//   数字   —— 扫描阶段数清楚了，例如 6 张图片；
//   null   —— 后端明说「数不出来」。目前只有 .xls：这些部件要先经 Excel COM 转成
//             .xlsx 才看得见，扫描阶段够不着。显示成「0」或「没有图片」是撒谎。
//   字段缺失 —— 这一版后端/这个 surface 根本不产出该字段，按「不适用」处理：不显示，
//             也不计入「未知」，免得在 Word/PDF 或老后端上凭空冒出一片「未知」。
// ---------------------------------------------------------------------------

type MaybeCount = number | "unknown" | "n/a";

function maybeCount(file: FileItem, key: "image_count" | "shape_text_count" | "comment_count" | "oversized_page_count"): MaybeCount {
  if (!hasOwn(file, key)) return "n/a";
  const value = file[key];
  return typeof value === "number" && Number.isFinite(value) ? value : "unknown";
}

interface OutsideCellStats {
  /** 已数清的图片总数（未知的文件不参与求和）。 */
  images: number;
  /** 已数清的含文字文本框/形状总数。 */
  shapes: number;
  /** 已数清的带批注单元格总数。批注文字同样不翻译，原样保留。 */
  comments: number;
  /** 至少有一项数不出来的文件数——对应后端 summary 的 *_unknown_files，不能吞掉。 */
  unknownFiles: number;
  /** 两个字段里至少有一个不是 n/a 的文件数。全批次都是 0，说明这批文件根本不产出这两个
   *  字段（不是「数出来是 0」），outsideCellStat 靠它把「字段缺失」跟「确认为 0」分开，
   *  不能像现在这样合并成同一个分支。 */
  presentFiles: number;
}

function outsideCellStats(files: FileItem[]): OutsideCellStats {
  const stats: OutsideCellStats = { images: 0, shapes: 0, comments: 0, unknownFiles: 0, presentFiles: 0 };
  for (const file of files) {
    const images = maybeCount(file, "image_count");
    const shapes = maybeCount(file, "shape_text_count");
    const comments = maybeCount(file, "comment_count");
    if (typeof images === "number") stats.images += images;
    if (typeof shapes === "number") stats.shapes += shapes;
    if (typeof comments === "number") stats.comments += comments;
    if (images === "unknown" || shapes === "unknown" || comments === "unknown") stats.unknownFiles += 1;
    if (images !== "n/a" || shapes !== "n/a" || comments !== "n/a") stats.presentFiles += 1;
  }
  return stats;
}

/** 有没有确凿数出来的东西。为 false 时可能是「确认没有」，也可能是「全都没数出来」。 */
function hasKnownOutside(stats: OutsideCellStats): boolean {
  return stats.images > 0 || stats.shapes > 0 || stats.comments > 0;
}

/** 「9 张图片、4 个文本框、3 条批注」；全为 0 时返回空串。 */
function outsideCellPhrase(stats: Pick<OutsideCellStats, "images" | "shapes" | "comments">): string {
  const parts: string[] = [];
  if (stats.images > 0) parts.push(`${stats.images.toLocaleString("zh-CN")} 张图片`);
  if (stats.shapes > 0) parts.push(`${stats.shapes.toLocaleString("zh-CN")} 个文本框`);
  if (stats.comments > 0) parts.push(`${stats.comments.toLocaleString("zh-CN")} 条批注`);
  return parts.join("、");
}

/** 接入点 1 · 扫描统计条里的「图片 / 文本框」格。字段在整批文件上都缺失时返回 null——
 *  这一格不该出现，不是「确认为 0」。调用方（computeStats）负责把 null 从数组里滤掉。 */
function outsideCellStat(stats: OutsideCellStats): StatCell | null {
  const label = "图片 / 文本框 / 批注";
  const unknownNote = stats.unknownFiles > 0 ? `${stats.unknownFiles} 个 .xls 未统计` : "";
  const n = (value: number) => value.toLocaleString("zh-CN");
  if (hasKnownOutside(stats)) {
    return {
      label,
      value: `${n(stats.images)} / ${n(stats.shapes)} / ${n(stats.comments)}`,
      attn: true,
      sub: unknownNote ? `不翻译，原样保留 · ${unknownNote}` : "不翻译，原样保留",
    };
  }
  // 一个都没数出来时不能写「0 / 0 / 0」——那是「确认没有」的说法。留「—」并说明原因。
  if (stats.unknownFiles > 0) {
    return { label, value: "—", dim: true, sub: `${stats.unknownFiles} 个 .xls 需转换后才能统计` };
  }
  // 这几个字段在整批文件上都不存在（不是数出来是 0）——这个 surface/这版后端根本不产出
  // 这项信息，不该显示「0 / 0 / 0」把「不知道」说成「没有」，整格直接不渲染。
  if (stats.presentFiles === 0) return null;
  return { label, value: "0 / 0 / 0", dim: true, sub: "无" };
}

/** 接入点 2 · 扫描后、任务清单上方的说明横幅。没有可说的就返回 null，不占版面。 */
function buildOutsideCellBanner(st: SurfaceState): HTMLElement | null {
  if (!st.files.length) return null;
  const stats = outsideCellStats(st.files);
  const known = hasKnownOutside(stats);
  if (!known && stats.unknownFiles === 0) return null;

  const banner = el("div", "banner warn");
  banner.append(icon("warn", { className: "ico" }));
  const tx = el("div", "tx");
  const title = el("div", "tt");
  title.textContent = known
    ? `这批文件里有 ${outsideCellPhrase(stats)}不会被翻译`
    : `有 ${stats.unknownFiles} 个 .xls 文件暂时数不出图片、文本框和批注`;
  const body = el("div", "bd");
  body.textContent = known
    ? "翻译只覆盖单元格里的文字。图片里的字需要 OCR，暂不支持；文本框、形状和批注里的字暂未接入。这些内容会原样保留，不会丢失也不会变形。"
    : "翻译只覆盖单元格里的文字，图片、文本框和批注里的字会原样保留。";
  if (stats.unknownFiles > 0) {
    body.textContent += `另有 ${stats.unknownFiles} 个 .xls 文件要先转换成 .xlsx 才能统计，转换后如果有这些内容，同样不会被翻译。`;
  }
  tx.append(title, body);
  banner.append(tx);
  banner.append(createButton({ label: "查看是哪些文件", onClick: () => showOutsideCellModal(st) }));
  return banner;
}

function showOutsideCellModal(st: SurfaceState): void {
  const lines: string[] = [];
  for (const file of st.files) {
    const images = maybeCount(file, "image_count");
    const shapes = maybeCount(file, "shape_text_count");
    const comments = maybeCount(file, "comment_count");
    if (images === "unknown" || shapes === "unknown" || comments === "unknown") {
      lines.push(`${fileLabel(file)} — 未知（.xls 需转换后才能统计）`);
      continue;
    }
    const phrase = outsideCellPhrase({
      images: typeof images === "number" ? images : 0,
      shapes: typeof shapes === "number" ? shapes : 0,
      comments: typeof comments === "number" ? comments : 0,
    });
    if (phrase) lines.push(`${fileLabel(file)} — ${phrase}`);
  }
  openModal({
    tone: "warn",
    icon: "warn",
    sourceLabel: "扫描结果 · 单元格外内容",
    title: "这些文件里有不参与翻译的内容",
    body: lines.length ? lines : ["没有检测到图片或文本框。"],
    actions: [{ label: "关闭" }],
  });
}

/** 接入点 3 · 任务清单每行的「单元格外内容」标记。 */
function buildOutsideCellChips(file: FileItem): HTMLElement[] {
  const images = maybeCount(file, "image_count");
  const shapes = maybeCount(file, "shape_text_count");
  const comments = maybeCount(file, "comment_count");
  const chips: HTMLElement[] = [];
  if (typeof images === "number" && images > 0) {
    chips.push(createChip({ label: `🖼 ${images.toLocaleString("zh-CN")} 张图片`, className: "oob" }));
  }
  if (typeof shapes === "number" && shapes > 0) {
    chips.push(createChip({ label: `▭ ${shapes.toLocaleString("zh-CN")} 个文本框`, className: "oob" }));
  }
  if (typeof comments === "number" && comments > 0) {
    chips.push(createChip({ label: `💬 ${comments.toLocaleString("zh-CN")} 条批注`, className: "oob" }));
  }
  // 未知和已知可以并存（真出现时两条都要说），所以是追加而不是二选一。
  if (images === "unknown" || shapes === "unknown" || comments === "unknown") {
    chips.push(createChip({ label: "? 未知（.xls 需转换后才能统计）", className: "oob mut" }));
  }
  if (chips.length) return chips;
  const dash = el("span", "dash");
  dash.textContent = "—";
  return [dash];
}

// ---------------------------------------------------------------------------
// PDF「大幅面页」——两个方向都超过 A4 约 15% 的页面（A3 及以上）
//
// 「跳过 A3 及更大的页面」开着时这些页不送翻译模型，原始矢量内容整页直传到输出 PDF。
// 判定只看纸张尺寸，跟页面内容是不是图纸无关——界面上的措辞一律用尺寸口径，不要写成
// 「图纸」，否则 A3 的宣传册被跳过时用户只会当成程序判错了。
//
// 三态和 Excel 的 image_count 完全同一套规矩，不再重复解释：数字 / null（数不出来，
// 例如加密或结构损坏的 PDF）/ 字段缺失（不适用）。「数不出来」永远不能渲染成 0。
// ---------------------------------------------------------------------------

interface OversizedPageStats {
  /** 已数清的大幅面页总数（数不出来的文件不参与求和）。 */
  pages: number;
  /** 幅面数不出来的文件数——对应后端 summary 的 oversized_page_count_unknown_files。 */
  unknownFiles: number;
  /** 产出了该字段的文件数。为 0 说明这批文件根本不产出（不是「数出来是 0」），整格不渲染。 */
  presentFiles: number;
}

/** 独立图片输入没有「页」的概念，后端给的 oversized_page_count 恒为 null（就是字段默认值，
 *  不是「数不出来」）。不排掉的话每张图片都会被记成一个「幅面未知的文件」。后端 summary
 *  里 pdf_items 那层过滤就是同一件事，两边必须一致。 */
function hasOversizedPageInfo(file: FileItem): boolean {
  return file.source_type !== "image" && hasOwn(file, "oversized_page_count");
}

function oversizedPageStats(files: FileItem[]): OversizedPageStats {
  const stats: OversizedPageStats = { pages: 0, unknownFiles: 0, presentFiles: 0 };
  for (const file of files) {
    if (!hasOversizedPageInfo(file)) continue;
    const count = maybeCount(file, "oversized_page_count");
    if (count === "n/a") continue;
    stats.presentFiles += 1;
    if (count === "unknown") stats.unknownFiles += 1;
    else stats.pages += count;
  }
  return stats;
}

/** 扫描统计条里的「大幅面页」格。字段整批缺失时返回 null，由 computeStats 过滤掉。 */
function oversizedPageStat(stats: OversizedPageStats): StatCell | null {
  if (stats.presentFiles === 0) return null;
  const label = "大幅面页";
  const unknownNote = stats.unknownFiles > 0 ? `其中 ${stats.unknownFiles} 个文件无法确认幅面` : "";
  if (stats.pages > 0) {
    return {
      label,
      value: stats.pages.toLocaleString("zh-CN"),
      attn: true,
      sub: unknownNote || "开启跳过后不翻译，原样保留",
    };
  }
  // 一页都没数出来时不能写 0——那是「确认没有」的说法。
  if (stats.unknownFiles > 0) return { label, value: "—", dim: true, sub: unknownNote };
  return { label, value: "0", dim: true, sub: "全部为 A4 及以下" };
}

/** 文件表「页数 / 尺寸」列里跟在页数后面的那半句；没什么可说时返回空串。 */
function oversizedPageNote(file: FileItem): { text: string; warn: boolean } | null {
  if (!hasOversizedPageInfo(file)) return null;
  const count = maybeCount(file, "oversized_page_count");
  if (count === "n/a") return null;
  if (count === "unknown") return { text: "幅面未知", warn: false };
  if (count <= 0) return null;
  // 「1 页 · 1 页 A3+」读起来像两件事。整份都是大幅面时直接说「整份都是 A3+」，
  // 部分时说「其中 N 页 A3+」，都跟在总页数后面，语义才连得上。
  const pages = num(file.page_count);
  if (pages > 0 && count >= pages) return { text: "整份都是 A3+", warn: true };
  return { text: `其中 ${count} 页 A3+`, warn: true };
}

/** 整份文件都是大幅面页——开着跳过开关选它等于什么都不会翻，扫描阶段就得说。 */
function isAllOversized(file: FileItem): boolean {
  if (!hasOversizedPageInfo(file)) return false;
  const count = maybeCount(file, "oversized_page_count");
  const pages = num(file.page_count);
  return typeof count === "number" && pages > 0 && count >= pages;
}

/** 接入点 4 · 翻译完成后的结果说明所需的数据快照。 */
interface ExcelDoneNotice {
  fileCount: number;
  cellCount: number;
  stats: OutsideCellStats;
  anchorFrozen: number;
}

/**
 * anchor_frozen_count（调行高时被冻结锚点、改成固定尺寸的悬浮图片数）只有一个真实来源：
 * file_results[].anchor_frozen_count（core/task_runner.py 约 1235 行产出，经
 * _sanitize_task_data 后 int 0 也完整保留)。不对不存在的异形 payload 形状（summary/kpi/
 * 结果根/files/results/items）做兜底——分发前不写没有对应真实用户的兼容代码，这些位置
 * 在这个仓库里本来就不产出该字段；真缺失时按 0 处理（那句话不出现),不要猜别的字段名,
 * 免得字段哪天改名时 UI 悄悄挪到别处继续显示旧值、把改名的问题藏起来。
 */
function anchorFrozenTotal(result: JsonObject): number {
  const entries = result.file_results;
  if (!Array.isArray(entries)) return 0;
  return entries.reduce((sum: number, entry) => sum + num(record(entry).anchor_frozen_count), 0);
}

function buildExcelDoneNotice(st: SurfaceState, result: JsonObject): ExcelDoneNotice | null {
  // 只统计真正送去翻译的那些文件；用户取消勾选的不该出现在完成汇总里。
  const files = st.files.filter((f) => st.selected.has(f.path));
  const stats = outsideCellStats(files);
  const anchorFrozen = anchorFrozenTotal(result);
  if (!hasKnownOutside(stats) && stats.unknownFiles === 0 && anchorFrozen === 0) return null;
  return {
    fileCount: files.length,
    cellCount: files.reduce((sum, f) => sum + num(f.text_cell_count), 0),
    stats,
    anchorFrozen,
  };
}

function buildExcelDoneCard(notice: ExcelDoneNotice): HTMLElement {
  const box = el("div", "done");
  box.append(icon("warn", { className: "ico" }));
  const tx = el("div");
  const title = el("div", "tt");
  // 「翻译完成 · N 个文件」以前写在这里，和上面那张横幅的标题说的是同一件事。这张卡
  // 只负责一件事：说清哪些内容按设定没有翻译。
  title.textContent = `按设定未翻译的内容 · 本批 ${notice.fileCount} 个文件、${notice.cellCount.toLocaleString("zh-CN")} 个文本单元格`;
  const body = el("div", "bd");
  const phrase = outsideCellPhrase(notice.stats);
  if (phrase) {
    body.append(document.createTextNode("另有 "));
    const em = el("em");
    em.textContent = phrase;
    body.append(em, document.createTextNode("中的文字未翻译，已原样保留。"));
  }
  if (notice.stats.unknownFiles > 0) {
    body.append(document.createTextNode(`${notice.stats.unknownFiles} 个 .xls 文件的图片、文本框和批注未能统计，其中的文字同样没有翻译。`));
  }
  if (notice.anchorFrozen > 0) {
    body.append(document.createTextNode(`${notice.anchorFrozen.toLocaleString("zh-CN")} 张悬浮图片已固定尺寸，避免行高变化时被拉伸。`));
  }
  tx.append(title, body);
  box.append(tx);
  return box;
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
    // 全部输入都被跳过时，清单是空的但原因是明摆着的——扫描到了文件，只是一个都不能翻。
    // 这时候还说「拖入文件开始」等于把刚发生的事抹掉，人会以为路径选错了反复重扫。
    const allSkipped = st.skipped.length > 0;
    const copy = allSkipped
      ? {
          title: `扫描到的 ${st.skipped.length} 个输入都没能纳入清单`,
          description: "下方列出了每一个的原因；处理掉原因后重新扫描即可。",
        }
      : !st.hasEverCompleted
        ? SURFACE_FIRST_EMPTY[surface]
        : st.bannerInfo?.tone === "fail"
          ? { title: "上一个任务没有顺利完成", description: "失败原因在任务中心的完整报告里；也可以重新选择来源再试一次。" }
          : SURFACE_BANNER_EMPTY[surface];
    const empty = createEmptyState({ title: copy.title, description: copy.description, icon: SURFACE_ICON[surface] });
    card.append(empty);
    const skipRow = buildSkipNoticeRow(surface, st);
    if (skipRow) card.append(skipRow);
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

  const skipRow = buildSkipNoticeRow(surface, st);
  if (skipRow) card.append(skipRow);

  return card;
}

/** 「扫描时跳过 N 项」那一条。清单有内容和一个都没纳入时都要出现，所以单独拿出来。 */
function buildSkipNoticeRow(surface: Surface, st: SurfaceState): HTMLElement | null {
  if (!st.skipped.length) return null;
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
  return skipRow;
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
    row.append(th("文件"), th("格式"), th("工作表", true), th("文本单元格", true), th("单元格外内容"), th("状态"));
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
    if (surface === "excel") {
      const outsideCell = el("td");
      for (const chip of buildOutsideCellChips(file)) outsideCell.append(chip);
      row.append(outsideCell);
    }
  } else {
    const typeCell = el("td");
    typeCell.append(buildFmtBadge((file.format ?? file.source_type ?? "").toUpperCase(), false));
    row.append(typeCell);
    const sizeCell = el("td");
    sizeCell.textContent = formatSizeKb(file.size_kb);
    row.append(sizeCell);
    const dimCell = el("td");
    if (file.source_type === "image") {
      dimCell.textContent = "—";
    } else {
      dimCell.append(document.createTextNode(`${num(file.page_count)} 页`));
      const note = oversizedPageNote(file);
      if (note) {
        dimCell.append(document.createTextNode(" · "));
        const span = el("span");
        span.style.color = note.warn ? "var(--warn)" : "var(--ink-3)";
        if (note.warn) span.style.fontWeight = "600";
        span.textContent = note.text;
        dimCell.append(span);
      }
    }
    row.append(dimCell);
  }

  const statusCell = el("td");
  const outcome = st.fileOutcomes.get(fileOutcomeKey(fileLabel(file)));
  if (!st.selected.has(file.path)) {
    statusCell.append(createChip({ label: "已排除", tone: "mute" }));
  } else if (outcome) {
    // 跑过之后这一列说的是结果，不再说开跑前的预判——那时候的「需先转换」已经成了旧闻。
    const chip = createChip({
      label: outcome.label,
      tone: outcome.produced ? "ok" : "dgr",
      icon: outcome.produced ? undefined : "warn",
    });
    if (outcome.detail) chip.title = outcome.detail;
    statusCell.append(chip);
  } else if (isRisky(surface, file)) {
    statusCell.append(createChip({ label: "需先转换", tone: "warn", icon: "warn" }));
  } else if (surface === "pdf" && isAllOversized(file)) {
    // 不看开关状态：开关在右栏，用户可能来回切，而「这份文件整份都是大幅面」是文件本身的
    // 事实。开着跳过时它一页都不会翻，提前说出来比跑完再发现强。
    statusCell.append(createChip({ label: "整份均为大幅面", tone: "warn", icon: "warn" }));
  } else {
    // 这一列以前对「一切正常、等着开跑」的文件是空白的，看上去像信息没加载出来。
    statusCell.append(createChip({ label: "未开始", tone: "mute" }));
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
  // 阶段号跟着百分比一起给：只有一个百分比时，用户没法判断「45% 之后还有几个阶段」。
  const phaseSuffix = local.phaseTotal > 0 && local.phaseIndex > 0
    ? ` · 阶段 ${local.phaseIndex} / ${local.phaseTotal}`
    : "";
  pct.textContent = `${Math.round(local.percent)}%${phaseSuffix}`;
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
  } else {
    // 一批内容发去翻译、等接口返回的这段时间里引擎不产生任何事件。界面上百分比、
    // 日志、逐文件状态全都定住不动，看起来和卡死没有区别，人只能去点停止。
    const silence = silenceSeconds(local);
    const waiting = !local.task.terminal
      && local.task.state !== "paused"
      && local.task.state !== "pausing"
      && silence >= SILENCE_NOTICE_SECONDS;
    if (waiting) {
      const note = el("p");
      note.className = "ws-note";
      note.textContent = `已等待 ${silence} 秒：请求已经发出，正在等接口把这一批的结果返回，程序没有卡住。`;
      card.append(note);
    }
  }

  return card;
}

function buildMonChips(surface: Surface, local: LocalTask): HTMLElement[] {
  const chip = (label: string, tone: ChipTone) => createChip({ label, tone });
  // 计数为 0 的临时状态不占位：一排「重试中 0 · 已恢复 0 · 未恢复 0」既说明不了
  // 任何事，又让人以为程序正在做这三件事。只有真的发生过才出现。
  const chipIfAny = (chips: HTMLElement[], value: number, label: string, tone: ChipTone) => {
    if (value > 0) chips.push(chip(label, tone));
  };
  if (surface === "word" && local.wordRecovery) {
    const r = local.wordRecovery;
    const chips: HTMLElement[] = [];
    chipIfAny(chips, num(r.retry_round), `重试轮次 ${num(r.retry_round)}`, "tint");
    chipIfAny(chips, num(r.semantic_processing_count), `仲裁处理中 ${num(r.semantic_processing_count)}`, "tint");
    chipIfAny(chips, num(r.semantic_accepted_count), `仲裁已接受 ${num(r.semantic_accepted_count)}`, "ok");
    const unresolved = num(r.retry_unresolved_count, num(r.unresolved_count));
    chipIfAny(chips, unresolved, `未恢复 ${unresolved}`, "warn");
    return chips;
  }
  if (surface === "pdf" && local.pdfPageRecovery) {
    const r = local.pdfPageRecovery;
    const chips: HTMLElement[] = [];
    // completed_pages 是「跑完的页」，占位页（生成失败后塞回原页）也算在里面。
    // 直接拿它当「已生成」会把失败页说成生成成功，所以先扣掉占位页，失败数另开一格。
    const failed = num(r.placeholder_page_count);
    const generated = Math.max(0, num(r.completed_pages) - failed);
    if (hasOwn(r, "completed_pages")) chips.push(chip(`已生成 ${generated} / ${num(r.total_pages)} 页`, "ok"));
    chipIfAny(chips, failed, `生成失败 ${failed} 页`, "dgr");
    chipIfAny(chips, num(r.retrying_page_count), `重试中 ${num(r.retrying_page_count)}`, "warn");
    chipIfAny(chips, num(r.recovered_page_count), `已恢复 ${num(r.recovered_page_count)}`, "mute");
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

/** actionable=false 时按钮全部禁用但常驻显示，这句短话解释原因。
 *
 *  只在卡片顶部说一次。早先每个按钮的 title 上也挂着同一句，一张十几行的表里
 *  就有二十多个一模一样的悬浮提示，读起来像出了二十个不同的问题。按行不同的原因
 *  （比如按幅面跳过的页）仍然挂在该行的按钮上——那才是 title 该承担的。 */
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
    onClick: () => void runPdfPageAction(surface, taskId, file, page, "regenerate"),
  }));
  row.append(createButton({
    label: "跳过该页",
    size: "mini",
    disabled: !actionable,
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
  // 审核关着的时候不能还说「审核模型逐页检查」：同一张表里每一行写的都是「未审核 · 本次
  // 没有让审核模型看这一页」，一屏之内两句话对不上。卡片本身照常用（重试、跳过都在这里）。
  span.textContent = snapshot.review_enabled
    ? "审核模型逐页检查版式与译文完整性"
    : "本次没有开启逐页审核，下面是每一页的生成结果";
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
  // 按尺寸跳过的页必须排在最前面判断，而且不能落到「待复核」上：它没有译文也没有页图，
  // 但那是按设定不翻，不是翻译失败。混进复核队列会让人挨个点开看一堆没必要看的页。
  if (page.skipped_oversize) return createChip({ label: "按幅面跳过", tone: "mute" });
  if (page.status === "failed" || page.placeholder) {
    return createChip({ label: page.user_skipped ? "已跳过占位" : "生成失败", tone: "dgr" });
  }
  // 译文页还没出来的一律不给审核结论。后端的中间态有三个：pending 还没轮到、
  // rendered 页图已渲染正在等模型、placeholder_pending 等着补占位页。9.2.6 只认识
  // pending，另外两个落到了下面的 review_status 分支上，于是页面还没生成就写「待复核」。
  if (page.status === "pending") return createChip({ label: "待处理", tone: "mute" });
  if (page.status === "rendered") return createChip({ label: "生成中", tone: "mute" });
  if (page.status === "placeholder_pending") return createChip({ label: "待补占位", tone: "mute" });
  // 审核结论要排在质检疑点前面：同一页可能既被质检挂了疑点又被审核判不通过，那时候
  // 「审核未通过」信息量更大。
  // 走到这里的页一定有译文（没输出的页在上面几个分支就被拦掉了），所以不能用「生成失败」
  // 那种深红：小结把这一页算进「已采用但建议复核」，chip 不该看起来像「这一页没有译文」。
  if (page.review_status === "failed") {
    return createChip({ label: "审核未通过 · 已采用", tone: "warn" });
  }
  if (page.review_status === "retrying") return createChip({ label: "重试中", tone: "mute" });
  // 本地质检的疑点必须压在 passed 前面。质检跑在送审之前，审核判「通过」不会清掉它，
  // 所以一页可以同时是 quality_flagged 和 review_status="passed"。9.2.6 只看审核结论，
  // 于是小结说「N 页译文有疑点」，逐页表格里却每一页都是绿色的「通过」——用户找不到
  // 是哪一页。这两个数就是小结里 suspect_adopted 的来源，必须在这里露出来。
  if (page.quality_flagged || page.emergency_ratio_normalized) {
    return createChip({ label: "建议复核", tone: "warn" });
  }
  if (page.review_status === "passed") return createChip({ label: "通过", tone: "ok" });
  // "skipped" 是 PdfPageRecord.review_status 的默认值，意思是这一页没经过审核模型
  // （关了逐页审核，或者还没轮到审核）。它是个真值字符串，不能当成「有审核结论」。
  if (!page.review_status || page.review_status === "skipped") {
    return createChip({ label: "未审核", tone: "mute" });
  }
  return createChip({ label: "待复核", tone: "warn" });
}

function reviewNote(page: PdfPage): string {
  if (page.skipped_oversize) return "幅面超过 A4，未送翻译，原始内容已原样保留在输出文件中。";
  if (page.pending_action === "regenerate") return "已排队重新生成，继续翻译后生效。";
  if (page.pending_action === "skip") return "已排队跳过，继续翻译后生效。";
  if (page.review_summary) return redactedText(page.review_summary);
  if (page.status === "failed" || page.placeholder) return redactedText(page.error, "页面生成失败。");
  if (page.status === "pending") return "尚未处理。";
  if (page.status === "rendered") return "页图已渲染，正在等模型返回这一页的译文。";
  if (page.status === "placeholder_pending") return "这一页没能生成，正在补一张占位页。";
  // 跟 reviewResultChip 同理：质检疑点要压在「通过」前面，否则这一页会摆着一句
  // 「版式一致，文本完整」，而小结正把它算进「有疑点仍采用」。
  if (page.quality_flagged || page.emergency_ratio_normalized) {
    const detail = redactedText(page.quality_message);
    if (detail) return `译文已采用，但自动检查有疑点：${detail}`;
    if (page.emergency_ratio_normalized) return "译文偏长，已强行缩排放进原位置，建议看一眼版面。";
    return "译文已采用，但自动检查发现疑点，建议看一眼这一页。";
  }
  // 「版式一致，文本完整」是一句审核结论，只有审核模型真的判过并通过了才能写。9.2.6 把它
  // 当成兜底文案，于是页面还没生成、审核还关着的时候，每一页都摆着这句凭空的好消息。
  if (page.review_status === "passed") return "版式一致，文本完整";
  if (!page.review_status || page.review_status === "skipped") {
    return "本次没有让审核模型看这一页。";
  }
  return "";
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
  const notFinished = page.status === "pending";
  const needsSkip = page.status === "failed" || page.placeholder;

  // 按幅面跳过的页既没有原页图也没有译文页图（压根没渲染过），重新生成也只会按同一条尺寸
  // 规则再跳一次。两个入口都关掉并说明原因，比让人点进一个空白对比框强。
  const oversizeSkipped = page.skipped_oversize;
  const oversizeReason = "该页按幅面跳过，没有生成页图；如需翻译请关闭「跳过 A3 及更大的页面」后重跑。";

  actionsCell.append(
    buildActionLink("查看对比", oversizeSkipped, oversizeSkipped ? oversizeReason : undefined, () =>
      openPdfPageCompareModal(surface, taskId, file, page),
    ),
  );
  actionsCell.append(document.createTextNode(" · "));

  const regenDisabled = !actionable || notFinished || oversizeSkipped;
  const regenReason = oversizeSkipped
    ? oversizeReason
    : actionable && notFinished
      ? "该页还没跑完，暂时不能重新生成。"
      : undefined;
  actionsCell.append(buildActionLink("重新生成", regenDisabled, regenReason, () => void runPdfPageAction(surface, taskId, file, page, "regenerate")));

  if (needsSkip) {
    actionsCell.append(document.createTextNode(" · "));
    actionsCell.append(buildActionLink("跳过该页", !actionable, undefined, () => void runPdfPageAction(surface, taskId, file, page, "skip")));
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

// ---------------------------------------------------------------------------
// 运行卡片文件列表上的「这个文件到底跳过了什么」标签
//
// 两个功能都在悄悄地少翻内容，用户看到「已完成」时默认理解是「整个文件都翻了」。跳了
// 多少必须逐文件说出来，而且失败要跟成功长得不一样——「开了保护但没找到正文起点」和
// 「开了保护跳过了 23 段」都表现为「已完成」，含义却相反。
//
// 不按 surface 分支，改看 payload 里有没有对应字段：Word 的条目带 front_matter，PDF 的
// 带 skipped_oversize_page_count。字段哪天没了标签自己消失，不会显示旧值。
// ---------------------------------------------------------------------------

/** file_results 里对应某个源文件的那一条。Word 给 source_path（全路径），PDF 只给 name。 */
function fileResultFor(result: JsonObject, path: string): JsonObject | null {
  const entries = result.file_results;
  if (!Array.isArray(entries)) return null;
  const base = path.split("/").pop() ?? path;
  for (const entry of entries) {
    const item = record(entry);
    if (text(item.source_path) === path || text(item.name) === base) return item;
  }
  return null;
}

/** file_results 上某个数值字段的合计。字段不存在的条目按 0 计——不猜别的字段名。 */
/** 终态结果里的逐文件行。正常完成走 `file_results`，中止和失败走 `files`——这是终态
 *  契约的键名（core/task_runner.py 的 StoppedMsg / ErrorMsg 都只给 `files`）。只认前者
 *  的话，一个中途停下来的任务在清单和小结里都看不出哪几个文件已经写出来、有几页是占位。 */
function terminalFileResults(result: JsonObject): JsonObject[] {
  for (const key of ["file_results", "files"]) {
    const entries = result[key];
    if (Array.isArray(entries) && entries.length) return entries.map(record);
  }
  return [];
}

/** 逐文件行上某个数值字段的合计，只算真的写出了文档的文件。页级小结说的是「打开输出能看到什么」：一个整份都失败的
 *  文件根本没有输出 PDF，它那几页占位图只作为失败素材留在报告里。把它们也算进「N 页已放失败
 *  占位页」，用户按这个数去输出目录里数，永远差几页——这一句本来就是为了让数字对得上才改的。
 *  没生成的文件另有「N 个文件没能生成」那一句交代，逐文件那一行还写着失败原因。 */
function producedFileResultTotal(result: JsonObject, key: string): number {
  return terminalFileResults(result)
    .filter((entry) => fileResultProduced(entry))
    .reduce((sum: number, entry) => sum + num(entry[key]), 0);
}

/** 所有文件实际被「保护封面和目录」跳过的段落合计；没找到正文起点的文件本来就是 0。
 *  同样只算写出了文档的文件——没生成的文件里「保护了几段」没有任何意义。 */
function frontMatterTotal(result: JsonObject): number {
  const entries = terminalFileResults(result).filter((entry) => fileResultProduced(entry));
  return entries.reduce((sum: number, entry) => {
    const fm = record(entry.front_matter);
    return fm.requested && fm.found ? sum + num(fm.protected_paragraph_count) : sum;
  }, 0);
}

function buildFileSkipChips(result: JsonObject, path: string): HTMLElement[] {
  const item = fileResultFor(result, path);
  if (!item) return [];
  const chips: HTMLElement[] = [];

  const frontMatter = record(item.front_matter);
  if (frontMatter.requested) {
    if (frontMatter.found) {
      chips.push(createChip({ label: `跳过开头 ${num(frontMatter.protected_paragraph_count)} 段`, tone: "tint" }));
    } else {
      // 一段都没保护，全文照常翻译了。这不是错误，但和用户开这个开关的预期不符，必须显眼。
      chips.push(createChip({ label: "未识别到正文起点", tone: "warn", icon: "warn" }));
    }
  }

  const oversized = num(item.skipped_oversize_page_count);
  if (oversized > 0) chips.push(createChip({ label: `跳过 ${oversized} 页 A3+`, tone: "tint" }));

  return chips;
}

// 界面上只需要滚动查看，不需要把整条历史都塞进 DOM——200 条封顶，够回溯最近的操作，
// 完整记录另外随诊断归档，不靠这里兜底。
const LOG_VIEW_LIMIT = 200;

/** 日志行号发号器（模块级、跨 surface 共用，只要不重号就够用）。 */
let nextLogSeq = 1;

function buildLogCard(local: LocalTask): HTMLElement {
  const card = el("div", "card");
  // flex:0 1 auto（不是 flex:1）：短任务只有三五行日志时，flex:1 会把这张卡撑到栏底，
  // 留下一大片深色空白，看着像日志丢了。改成按内容高度取，超过可用空间再收缩滚动。
  card.style.cssText = "flex:0 1 auto;min-height:0;display:flex;flex-direction:column;overflow:hidden";
  const head = el("div", "tc-head");
  const b = el("b");
  b.textContent = "运行日志";
  const span = el("span");
  span.textContent = "保留最近 200 条 · 完整日志随诊断归档";
  head.append(b, span);
  card.append(head);

  const log = el("div", "log");
  // flex:1 撑满卡片剩余空间；min-height:0 让它在内容超高时收缩而不是把卡片顶大，
  // overflow-y:auto 才能真的滚起来——否则 flex 子项默认会按内容高度撑开父容器。
  // min-height 给几行的余量，免得日志只有一两条时这块比标题还矮、跳来跳去；
  // max-height 封顶，长任务里它才是那个可滚动的区域而不是把整页顶长。
  log.style.cssText = "flex:0 1 auto;min-height:76px;max-height:340px;border:0;border-radius:0;overflow-y:auto";
  for (const entry of local.logs.slice(-LOG_VIEW_LIMIT)) {
    const line = el("div");
    // 整页重建后靠这个属性把滚动位置对回原来那一行，见 restoreLogScroll。
    line.dataset.seq = String(entry.seq);
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
  const result = record(local.task.result);
  for (const [path, stage] of local.fileStage) {
    const row = el("div", "filerow");
    // done 的含义只是「阶段名里已经不提这个文件了」（见 markActiveFile），不是「产物写出来
    // 了」：第 1 阶段刚提取完文本的文件也是 done。所以这一格不能写「已生成」——CONTEXT.md
    // 里「已生成」的定义是输出文档已经写成功，而那会儿输出目录里一个文件都没有。
    const meta: Record<string, { label: string; tone: ChipTone }> = {
      queued: { label: "排队中", tone: "mute" },
      active: { label: "进行中", tone: "tint" },
      done: { label: "已进入下一步", tone: "ok" },
      error: { label: "未完成", tone: "warn" },
    };
    row.append(createChip(meta[stage]));
    const nm = el("span", "nm");
    nm.textContent = path.split("/").pop() ?? path;
    row.append(nm);
    // 跳过标签只在文件跑完后才有数据（file_results 是终态才产出的），跑的过程中自然为空。
    for (const chip of buildFileSkipChips(result, path)) row.append(chip);
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
  // 只有「运行中」才锁定右栏。未扫描不是锁定理由：这些开关写的是长期设置，
  // 用户本来就该在挑文件之前先把它们调好。
  const card = el("div", "card runpanel");
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

  // recentKey 按 surface + 方向分开：Excel 的目标语言和 Word 的目标语言是两套习惯，
  // 混在一起会让「最近使用」总是显示另一个页面刚选过的语种。
  const targetField = createLanguageField({
    label: "目标语言",
    options: languageOptions.target,
    value: st.targetLang,
    disabled: active,
    recentKey: `${surface}-target`,
    onReload: () => void reloadBootstrap(surface),
    onChange: (value) => {
      st.targetLang = value;
      void persistSettings(surface === "pdf" ? nestedPatch("pdf.target_lang", value) : { [`${surface}_target_lang`]: value });
    },
  });
  scroll.append(targetField.root);

  if (surface !== "pdf") {
    // /api/languages 的 source_options 头一项本来就是「自动识别」，只有后端没给时才补。
    // （旧的原生 select 无条件在前面插一条「自动检测」，结果两条 auto 并排出现。）
    const sourceLangOptions: LanguageOption[] = languageOptions.source.some((o) => o.code === "auto")
      ? languageOptions.source
      : [{ code: "auto", display_name: "自动识别", aliases: ["auto", "自动识别", "自动检测"] }, ...languageOptions.source];
    const sourceField = createLanguageField({
      label: "源语言",
      options: sourceLangOptions,
      value: st.sourceLang,
      disabled: active,
      recentKey: `${surface}-source`,
      onReload: () => void reloadBootstrap(surface),
      onChange: (value) => {
        st.sourceLang = value;
        void persistSettings({ [`${surface}_source_lang`]: value });
      },
    });
    scroll.append(sourceField.root);
  }

  const typeSec = el("div", "rp-sec");
  typeSec.textContent = `本类型选项 · ${SURFACE_LABEL[surface]}`;
  scroll.append(typeSec);

  for (const toggle of TOGGLES[surface]) {
    const row = createSwitchRow({
      label: toggle.label,
      hint: toggle.hint,
      checked: Boolean(st.toggles.get(toggle.key)),
      disabled: active,
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
    // 「无」= 不指定领域，不会拼进任何领域 Prompt，也就没有可编辑的段落——
    // 选中「无」时不出「编辑 Prompt」入口，避免用户点进设置页给一个本该
    // 保持空的领域挂上覆盖文本。
    if (st.domainPreset !== "无") {
      const link = el("span", "linklike");
      link.style.fontSize = "11px";
      link.textContent = "编辑 Prompt ↗ 设置";
      link.addEventListener("click", () => navigate("settings", { page: "params" }));
      label.append(link);
    }
    domainField.append(label);
    const select = createSelectField({
      label: "",
      // 「无」放在第一项，让「不加任何领域限定」一眼可见。
      options: ["无", "同步工程场景", "资料管理场景", "行政生活化场景", "自定义"].map((v) => ({ value: v, label: v })),
      value: st.domainPreset,
      onChange: (value) => {
        st.domainPreset = value;
        void persistSettings({ [`${surface}_domain_preset`]: value });
        rerender(surface);
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
    const busy = submittingSurfaces.has(surface);
    const disabled = st.selected.size === 0 || busy;
    foot.append(createButton({
      label: busy
        ? "正在启动…"
        : disabled ? "开始翻译" : `开始翻译（${st.selected.size} 个文件）`,
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
    foot.append(createButton({ label: "安全停止", icon: "stop", variant: "danger", onClick: () => confirmStopTask(surface, st) }));
    return foot;
  }
  foot.append(createButton({ label: "安全停止", icon: "stop", variant: "danger", size: "big", onClick: () => confirmStopTask(surface, st) }));
  const note = el("div", "ws-note");
  note.style.textAlign = "center";
  note.textContent = "已完成的文件会保留，当前文件回滚为未开始";
  foot.append(note);
  return foot;
}

// ---------------------------------------------------------------------------
// 扫描
// ---------------------------------------------------------------------------

/**
 * 扫描请求序号。换目录时上一次扫描往往还在路上（「浏览」选完就自动扫，用户又点了一次
 * 「扫描」也一样），两个响应谁后到谁覆盖 st.files；先发的那个后到，界面就变成
 * 「路径栏显示 B、文件清单是 A」。用户点开始翻译，后端按 B 重扫再和前端交上来的选中
 * 路径取交集，结果只翻了两边都有的那一部分，界面上不报任何错。
 * 只有序号仍等于最新一次的响应才允许写回状态。
 */
const scanTokens: Record<Surface, number> = { excel: 0, word: 0, pdf: 0 };
/** 有扫描在飞时置灰「扫描」「浏览」，避免用户连点又多造一个在途请求。 */
const scanBusy: Record<Surface, boolean> = { excel: false, word: false, pdf: false };

async function runScan(surface: Surface): Promise<void> {
  const st = states[surface];
  const path = st.sourcePath.trim();
  if (!path) return;
  const token = ++scanTokens[surface];
  scanBusy[surface] = true;
  rerender(surface);
  try {
    const c = await getClient();
    const payload = {
      surface,
      path,
      // 多选文件时按这几个路径各扫一次再合并；清单里就只有用户挑的那几份。
      paths: st.sourcePaths,
      include_images: surface === "pdf" && Boolean(st.toggles.get("pdfImages")),
    };
    const response = await c.request<JsonObject>("/api/sources/scan", { method: "POST", body: JSON.stringify(payload) });
    if (token !== scanTokens[surface]) return; // 已被更晚的扫描取代，这份结果一个字都不能落地
    const result = record(response.result) && Object.keys(record(response.result)).length ? record(response.result) : response;
    const items = Array.isArray(result.items) ? (result.items as FileItem[]) : [];
    const skipped = Array.isArray(result.skipped) ? (result.skipped as ScanSkippedItem[]) : [];
    st.files = items;
    st.skipped = skipped;
    st.scanSummary = record(result.summary);
    st.selected = new Set(items.map((f) => f.path));
    st.showBanner = false;
    // 新清单配旧结果没有意义：上一次跑的是别的文件。
    st.fileOutcomes = new Map();
    // 「记住上次目录」失败不能牵连扫描结果：清单已经拿到了，没道理因为写设置出错就清空它。
    try {
      await persistSettings({ [`last_${surface}_source_folder`]: path });
    } catch {
      // 下次进入这个界面得重新选路径而已，不值得打断当前流程。
    }
    // 一个都没跳过时不提「跳过 0 个」；西文名词前后都要留空格。
    const skipSuffix = skipped.length > 0 ? `，跳过 ${skipped.length} 个` : "";
    const toastMessage = surface === "pdf"
      ? `已扫描到 ${items.length} 个 PDF / 图片输入${skipSuffix}。`
      : `已扫描到 ${items.length} 个 ${SURFACE_LABEL[surface]} 文件${skipSuffix}。`;
    showToast({ message: toastMessage });
  } catch (error) {
    if (token !== scanTokens[surface]) return;
    // 扫描失败必须把上一次的清单清掉：留着旧结果，用户看到的是「路径 B + B 之外的文件」，
    // 会当成 B 目录的内容直接开翻。宁可空着让他重扫，也不能拿上一次的结果冒充这一次。
    st.files = [];
    st.skipped = [];
    st.scanSummary = {};
    st.selected = new Set();
    showToast({ message: redactedText((error as Error)?.message, "扫描失败。"), error: true });
  } finally {
    if (token === scanTokens[surface]) {
      scanBusy[surface] = false;
      rerender(surface);
    }
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
    payload.protect_front_matter = Boolean(st.toggles.get("protectFrontMatter"));
    payload.translate_headers_footers = Boolean(st.toggles.get("translateHeadersFooters"));
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
    // 主按钮给「优先高保真」：它是不会损失内容的那一个。默认高亮有损选项，等于
    // 替用户默认接受丢样式、丢图片、丢宏——回车一按就丢了，而且事后看不出丢了什么。
    actions: [
      { label: "取消" },
      {
        label: "允许兼容转换",
        onClick: () => {
          if (surface === "excel") st.allowXlsFallback = true;
          else st.allowDocFallback = true;
          void preflightAndSubmit(surface, st);
        },
      },
      {
        label: "优先高保真",
        variant: "primary",
        onClick: () => {
          if (surface === "excel") st.allowXlsFallback = false;
          else st.allowDocFallback = false;
          void preflightAndSubmit(surface, st);
        },
      },
    ],
  });
}

/** 预检加提交合起来要跑好几个来回，这段时间里按钮还是可点的。第二次点击最终会被
 *  后端的串行化挡下来返回 409，用户看到的是一条看不懂的报错——所以在这里就拦住。 */
const submittingSurfaces = new Set<Surface>();

async function preflightAndSubmit(
  surface: Surface,
  st: SurfaceState,
  overrides: JsonObject = {},
): Promise<void> {
  if (submittingSurfaces.has(surface)) return;
  submittingSurfaces.add(surface);
  rerender(surface);
  const payload = { ...buildPayload(surface, st), ...overrides };
  try {
    const c = await getClient();
    const preflight = await c.preflightTask(payload);
    if (preflight.requires_confirmation) {
      // 风险确认弹窗是一个决策点：这里必须放开，否则用户在弹窗上点「仍要并行启动」
      // 会被自己的这把锁挡住。弹窗那条路径由 submitTaskStart 自己守。
      showTaskRiskModal(surface, st, payload, preflight.confirmation_token ?? "");
      return;
    }
    await sendTaskStart(surface, st, payload);
  } catch (error) {
    showStartBlockedModal(surface, st, payload, error);
  } finally {
    submittingSurfaces.delete(surface);
    rerender(surface);
  }
}

/** 前置校验把任务拦下来时给一个弹窗，不给两秒就消失的提示：任务根本没开始这件事得留在
 *  屏幕上。更要紧的是有些拦截本身是有出路的——审核模型上次测试失败那条就写着「或明确
 *  确认继续」，而 9.2.6 只在提示文字里说了这句话，界面上没有任何按钮能确认，看起来就
 *  像开始按钮坏了。 */
function showStartBlockedModal(surface: Surface, st: SurfaceState, payload: JsonObject, error: unknown): void {
  if (apiErrorReason(error) === "pdf_review_model_unavailable") {
    openModal({
      tone: "warn",
      icon: "warn",
      title: "审核模型上次测试没通过",
      body: [
        "逐页审核是开着的，但这台机器上最近一次测试 PDF 译文审核模型时失败了。照这样开始，页面会照常翻译，审核那一步很可能每页都出错。",
        "稳妥的做法是先去「设置 → 模型 → PDF 译文审核」重测一次，或者把逐页审核关掉再开始。",
        "也可以不管测试结果直接开始：这只影响这一次任务，设置不会被改动。",
      ],
      actions: [
        { label: "取消" },
        { label: "去设置里重测", onClick: () => navigate("settings", { page: "models" }) },
        {
          label: "不管测试结果，开始",
          variant: "primary",
          onClick: () => void preflightAndSubmit(surface, st, { ...payload, allow_known_review_failure: true }),
        },
      ],
    });
    return;
  }
  openModal({
    tone: "warn",
    icon: "warn",
    title: "这次任务没能开始",
    body: [redactedText((error as Error)?.message, "任务准备失败，没有更多信息。")],
    actions: [{ label: "知道了", variant: "primary" }],
  });
}

/** 简化版并行风险确认：main.ts 原版还会展示共享连接明细表、活动任务列表、候选任务快照——
 *  这里按品牌要求的措辞保留决策要点，略去表格化明细，见文件尾「已确认简化」说明。 */
function showTaskRiskModal(surface: Surface, st: SurfaceState, payload: JsonObject, token: string): void {
  openModal({
    tone: "warn",
    icon: "warn",
    title: "和正在跑的任务共用同一条连接",
    // 原文写的是「按新任务自己的默认吞吐启动」「一次性令牌原子复检」「降低共享组的
    // 运行时容量」——这些是实现细节，读的人只需要知道三件事：共用什么、可能出什么事、
    // 自己的设置会不会被改。
    body: [
      "这个任务会和正在跑的任务用到同一条 API 连接。两边都按各自的速度发请求，接口那边看到的是两份请求叠在一起。",
      "因此可能变慢或排队，个别文件可能超时失败；如果这条连接是按用量计费的，花费也会同时产生。真被接口限速时，程序会自动放慢这一次的发送速度，你在设置里填的速度不会被改。",
      "不着急的话，等前一个任务跑完再开始最稳。",
    ],
    actions: [
      { label: "取消" },
      { label: "仍要并行启动", variant: "primary", onClick: () => void submitTaskStart(surface, st, payload, token) },
    ],
  });
}

/** 弹窗按钮走这条：自己守一次，避免连点弹窗上的确认键。 */
async function submitTaskStart(surface: Surface, st: SurfaceState, payload: JsonObject, confirmationToken = ""): Promise<void> {
  if (submittingSurfaces.has(surface)) return;
  submittingSurfaces.add(surface);
  rerender(surface);
  try {
    await sendTaskStart(surface, st, payload, confirmationToken);
  } finally {
    submittingSurfaces.delete(surface);
    rerender(surface);
  }
}

async function sendTaskStart(surface: Surface, st: SurfaceState, payload: JsonObject, confirmationToken = ""): Promise<void> {
  try {
    const c = await getClient();
    const body = confirmationToken ? { ...payload, confirmation_token: confirmationToken } : payload;
    const task = await c.request<TaskStatus>("/api/tasks", { method: "POST", body: JSON.stringify(body) });
    focusTask(surface, task);
    noteTaskStarted(task);
    st.showBanner = false;
    st.fileOutcomes = new Map();
    initFileStages(st);
    rerender(surface);
    watchTask(surface);
    if (surface === "pdf") void fetchPdfPagesSnapshot(surface, task.task_id);
  } catch (error) {
    // 真正启动这一步会把前置校验再跑一遍（设置可能在弹窗开着的时候被改了），所以同一批
    // 「有出路的拦截」也会从这里出来，走同一个弹窗。
    showStartBlockedModal(surface, st, payload, error);
  }
}

function focusTask(surface: Surface, task: TaskStatus): void {
  const st = states[surface];
  st.task = {
    task,
    logs: [],
    phaseName: "正在准备任务",
    percent: 0,
    phaseIndex: 0,
    phaseTotal: 0,
    streamState: "connected",
    watcherActive: false,
    lastEventAt: Date.now(),
    fileStage: new Map(),
  };
  st.lastTaskId = task.task_id;
  startSilenceTicker(surface);
}

/** 静默计时器：任务在跑但没有事件进来时，每 5 秒重画一次，让「已等待 N 秒」这句话
 *  自己走动。任务一到终态就停——不留一个空转的 interval。 */
const silenceTickers: Record<Surface, number | undefined> = { excel: undefined, word: undefined, pdf: undefined };

function startSilenceTicker(surface: Surface): void {
  if (silenceTickers[surface] !== undefined) return;
  silenceTickers[surface] = window.setInterval(() => {
    const local = states[surface].task;
    if (!local || local.task.terminal) {
      stopSilenceTicker(surface);
      return;
    }
    // 只有真的静默下来才重画；有事件在流动时 rerender 已经被事件驱动了。
    if (silenceSeconds(local) >= SILENCE_NOTICE_SECONDS) rerender(surface);
  }, 5000);
}

function stopSilenceTicker(surface: Surface): void {
  const handle = silenceTickers[surface];
  if (handle !== undefined) {
    window.clearInterval(handle);
    silenceTickers[surface] = undefined;
  }
}

/** 多久没消息才值得说一句。低于这个数的空白属于正常节奏，报出来只是噪音。 */
const SILENCE_NOTICE_SECONDS = 20;

function silenceSeconds(local: LocalTask): number {
  return Math.max(0, Math.round((Date.now() - local.lastEventAt) / 1000));
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
  local.lastEventAt = Date.now();
  const data = event.data;
  switch (event.type) {
    case "log": {
      local.logs.push({ seq: nextLogSeq++, time: formatTime(num(data.ts, 0) || undefined), level: text(data.level, "info"), message: redactedText(data.message ?? data.text, "") });
      // 数组本身也要封顶，不然长任务跑几个小时会攒出一条越来越长的内存记录——界面只展示
      // 最近 LOG_VIEW_LIMIT 条，这里索性同步截断，省得两处上限不一致。
      if (local.logs.length > LOG_VIEW_LIMIT) local.logs.splice(0, local.logs.length - LOG_VIEW_LIMIT);
      break;
    }
    case "progress": {
      if (data.phase_name !== undefined) local.phaseName = redactedText(data.phase_name, local.phaseName);
      const done = num(data.step_done);
      const total = num(data.step_total);
      // step_done/step_total 是**当前阶段**的步数。直接拿它当总进度，扫描一结束进度条
      // 就满格，后面还有翻译和生成两个阶段要跑——用户看到 100% 却还在等，只能理解成卡住。
      // 事件里带着 phase_index/phase_total，用它把阶段内进度折算到整条进度上。
      const phaseIndex = num(data.phase_index);
      const phaseTotal = num(data.phase_total);
      if (phaseIndex > 0) local.phaseIndex = phaseIndex;
      if (phaseTotal > 0) local.phaseTotal = phaseTotal;
      const withinPhase = total > 0 ? Math.min(1, done / total) : 0;
      if (phaseTotal > 0 && phaseIndex > 0) {
        local.percent = Math.min(100, ((phaseIndex - 1 + withinPhase) / phaseTotal) * 100);
      } else if (total > 0) {
        local.percent = withinPhase * 100;
      }
      markActiveFile(local, st);
      break;
    }
    case "status": {
      local.phaseName = redactedText(data.phase_desc, local.phaseName);
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
  closeStopModal(surface);
  const result = record(task.result);
  // 终态结果契约（core/*_task_runner.py 的 DoneMsg/ErrorMsg/StoppedMsg）没有 `summary`
  // 对象，也没有顶层 generated_count/review_count/auto_fixed_count——这些字段名在后端
  // 从未存在过。真实字段：`file_results[].success`（逐文件是否成功写出，三个 surface
  // 都有）、`review.total_count`（Excel/Word 才有；PDF 的 review 契约字段留空）、
  // `kpi.auto_recovered_text_count`（仅 Word 的严格重试/语义复核有这个概念）。缺失的
  // 字段一律按 0 处理，不用占位数字冒充「全部通过」。
  const fileResults = terminalFileResults(result);
  // 「产出了」不等于 success===true：需复核的文件同样写出了双语文件（后端给的
  // status 是 needs_review、output 是真实路径），把它算成失败会让横幅报出比输出
  // 目录里少的数字。反过来，失败的条目 output 是空字符串——这才是判断依据。
  st.fileOutcomes = new Map();
  let produced = 0;
  let failed = 0;
  // 同名（不同后缀）的文件会让「哪一行是哪个结果」无法判断，例如 report.xls 和
  // report.xlsx 的 name 都是 report。这种情况下宁可让那一行留在「未开始」，也不能
  // 在状态列上蒙一个可能张冠李戴的结论。
  const ambiguousNames = new Set<string>();
  const seenNames = new Set<string>();
  for (const file of st.files) {
    const key = fileOutcomeKey(fileLabel(file));
    if (!key) continue;
    if (seenNames.has(key)) ambiguousNames.add(key);
    seenNames.add(key);
  }
  for (const entry of fileResults) {
    const item = record(entry);
    const ok = fileResultProduced(item);
    if (ok) produced += 1;
    else failed += 1;
    const key = fileOutcomeKey(text(item.name));
    if (key && !ambiguousNames.has(key)) {
      const pending = fileResultPendingReview(item);
      // 整份未翻译的文件不能只写「已生成」：文档确实写出来了，可里面一个字都没翻。
      // 这一行的说明沿用后端给的原话（`fully_skipped_oversize_message`）。
      const untranslated = ok && fileResultUntranslated(item);
      const label = !ok
        ? "未生成"
        : untranslated
          ? "已生成 · 整份未翻译"
          : pending > 0
            ? `已生成 · ${pending} 处需复核`
            : "已生成";
      st.fileOutcomes.set(key, {
        produced: ok,
        label,
        detail: ok && !untranslated
          ? ""
          : redactedText(text(item.error, text(item.detail)), "没有说明原因。"),
      });
    }
  }
  const stateFailed = task.state === "error" || task.state === "interrupted";
  const generated = fileResults.length > 0 ? produced : stateFailed ? 0 : st.selected.size;
  const review = num(record(result.review).total_count);
  const autoFixed = num(record(result.kpi).auto_recovered_text_count);
  const outputPath = text(result.output_dir, st.sourcePath);
  st.lastOutputPath = outputPath;
  const clauses: string[] = [];
  // 「按设定没翻的内容」要跟在生成结果后面一起说。横幅只给总数，逐文件的明细在运行卡片
  // 的文件列表上（buildFileSkipChips），完整边界在任务日志和报告里。
  const skippedOversizePages = producedFileResultTotal(result, "skipped_oversize_page_count");
  const protectedParagraphs = frontMatterTotal(result);
  // PDF 的问题是按页记的，而且那一路不填 review.total_count（那是 Excel / Word 的字段）。
  // 只看 review 的话，一份「4 页全是占位页」的任务小结照样会写「全部通过」，而下面的
  // 清单同时写着「已生成 · 4 处需复核」——同一屏上两句话互相打脸。
  // 相加的两个数必须互不重叠。review_failed_page_count 和 quality_flagged_page_count 都会
  // 和 placeholder_page_count 撞在同一页上（审核没通过的页有一半是直接退回占位页的，质检
  // 标记也留在退回前的最后一次尝试上），三个一相加就会把一页坏页说成两三页。后端为此单独
  // 给了 suspect_adopted_page_count——「有疑点但仍然采用」的页，与占位页严格互斥。
  const placeholderPages = producedFileResultTotal(result, "placeholder_page_count");
  const suspectPages = producedFileResultTotal(result, "suspect_adopted_page_count");
  const pageProblems = placeholderPages + suspectPages;
  // 同样只算真的写出了文档的文件：横幅那句是「其中 N 个文件整份都是 A3+」，「其中」指的是
  // 已生成的那几个。一份全是 A3+ 却在合成输出时失败的文件既没生成、又满足「整份未翻译」，
  // 不过滤就会出现「1 个文件没能生成 · 其中 1 个文件整份都是 A3+」——没有「其中」可言。
  const untranslatedFiles = terminalFileResults(result).filter(
    (entry) => fileResultProduced(entry) && fileResultUntranslated(entry),
  ).length;
  // 没能生成的文件排在最前面：其余小结说的都是「做到了什么」，这一句说的是
  // 「有东西没拿到」，它决定用户下一步要不要重跑。
  if (failed > 0) clauses.push(`${failed} 个文件没能生成`);
  // 占位页不是原页：那是程序自己画的一张失败占位页（白底红框、写着这一页没能生成译图），
  // 源页内容一个像素都不在上面。说成「已保留原页」会让用户去输出 PDF 里找原文。
  // 真正原样保留的只有「跳过 A3+」那一路——它从源 PDF 矢量直传，说法在下面那句。
  if (placeholderPages > 0) clauses.push(`${placeholderPages} 页没能生成译文，已放失败占位页`);
  if (suspectPages > 0) clauses.push(`${suspectPages} 页译文有疑点，已采用但建议复核`);
  if (skippedOversizePages > 0) clauses.push(`跳过 ${skippedOversizePages} 页 A3+，原样保留`);
  // 整份都被跳过的文件必须单独点名：它的输出文档跟源文件逐字节一样，一个字都没翻。
  // 后端为此把这种文件的 success 判成 false（见 _file_record_to_result），也在
  // _record_needs_review 里把它算作需复核；界面只说「跳过 N 页」的话，用户会以为
  // 那是一份翻好的文件里少翻了几页，而实际上整份都没动。
  if (untranslatedFiles > 0) {
    clauses.push(`其中 ${untranslatedFiles} 个文件整份都是 A3+，一个字都没翻`);
  }
  if (protectedParagraphs > 0) clauses.push(`保护开头 ${protectedParagraphs} 段未翻译`);
  if (review > 0) clauses.push(`${review} 处需复核`);
  // 中途换过连接就明说：这一半译文出自另一家服务商，用户回头比质量、查账单都要知道。
  // 详细的切换时刻和原因在任务中心的快照行里。
  const switchCount = num(record(result.connections).switch_count);
  if (switchCount > 0) {
    const finalLabel = text(record(result.connections).final_label);
    clauses.push(finalLabel ? `中途换了连接，后半程由「${finalLabel}」完成` : `中途换了 ${switchCount} 次连接`);
  }
  if (autoFixed > 0) clauses.push(`${autoFixed} 处已自动处理`);
  // 按了「安全停止」、但在飞的页刚好全部跑完时，任务照常走完成分支。不点这一句的话，
  // 屏幕上只剩「已完成 · 全部通过」，用户不知道自己那一下有没有截掉内容（只有运行日志
  // 里有一条 WARN）。
  const stopInfo = record(result.stop);
  if (stopInfo.requested === true && stopInfo.truncated === false) {
    clauses.push("你请求停止时页面都已跑完，没有内容被截断");
  }
  // 「全部通过」是一句承诺，只有真的一个问题都没有才能说。有文件没生成时一个字都不提，
  // 有需复核/占位页/已自动处理时说「其余」。
  // 「其余」也得真的有其余：4 页 PDF 四页全是占位页时，前面几句已经把每一页都点了名，
  // 再补一句「其余全部通过」是在给一个不存在的剩余部分背书。
  // 分母跟分子取同一批文件（只算写出了文档的），否则一个整份失败的文件会把总页数抬高，
  // 「问题页是否已覆盖全部页」就永远判不成立。
  // 跳过的 A3+ 页也算「这一页没翻」。少了它，一份 4 页里 2 页跳过、2 页退回占位页的文件
  // （每一页都没翻）仍会落到「其余全部通过」——正是上面这句注释要防的那件事。
  const totalPages = producedFileResultTotal(result, "page_count");
  const untranslatedPages = pageProblems + skippedOversizePages;
  const everyPageHasProblem = totalPages > 0 && untranslatedPages >= totalPages;
  if (failed === 0 && generated > 0 && !everyPageHasProblem && untranslatedFiles === 0) {
    clauses.push(review > 0 || autoFixed > 0 || pageProblems > 0 ? "其余全部通过" : "全部通过");
  }
  if (generated > 0) clauses.push(`输出至 ${outputPath}`);
  const tone = resultTone(task.state, generated, failed, review + pageProblems + untranslatedFiles, autoFixed);
  // 完成汇总要用扫描期的图片/文本框计数。任务失败时不出这张卡：那种情况下
  // 「哪些没翻」根本说不准，横幅已经说了任务未完成。
  st.excelDoneNotice = surface === "excel" && tone !== "fail" ? buildExcelDoneNotice(st, result) : null;
  const stateWord = terminalStateWord(task.state);
  const reason = redactedText(result.message, "");
  if (tone === "fail") {
    const detail = reason || (generated > 0
      ? "任务在结束前中断，已生成的文件仍保留在输出目录。"
      : "任务没有产出文件，请看下方日志或完整报告里的原因。");
    st.bannerInfo = {
      title: generated > 0 ? `${stateWord} · 已生成 ${generated} 个文件` : `${stateWord} · 没有生成文件`,
      subtitle: generated > 0 ? [detail, ...clauses].join(" · ") : detail,
      tone,
      statusLabel: generated > 0 ? `${stateWord} · 部分生成` : stateWord,
      hasOutput: generated > 0,
    };
  } else {
    const suffix = failed > 0
      ? "有文件未生成"
      : review > 0 || pageProblems > 0
        ? "需复核"
        : autoFixed > 0
          ? "有自动处理"
          : "";
    st.bannerInfo = {
      title: stateWord === "已完成"
        ? `已生成 ${generated} 个文件`
        : `${stateWord} · 已生成 ${generated} 个文件`,
      subtitle: clauses.join(" · "),
      tone,
      statusLabel: suffix ? `${stateWord} · ${suffix}` : stateWord,
      hasOutput: true,
    };
  }
  st.showBanner = true;
  // 清单、勾选、来源路径都保留：任务结束正是用户要重跑失败文件、或换个设置再来一遍的
  // 时候。以前这里全部清空，等于把「再来一次」的入口一起删了，他只能重新选路径重扫。
  if (tone === "fail") {
    showToast({ message: reason || "任务未能顺利完成。", error: true });
  }
  rerender(surface);
}

/** 逐文件结果是否真的写出了文件。 */
/** 状态列的连接键：扫描项与 file_results 两边字段不一致——PDF 的 `name` 是带扩展名的
 *  文件名，Excel / Word 的 `name` 是不含扩展名的主干名，而 Word 那一路除了 `name` 什么
 *  都没有（source_path 会被隐私过滤置空）。所以两边统一归一成「去掉已知文档扩展名的
 *  文件名」，只要算法一致就能对上。
 *
 *  只削掉认识的扩展名：`报价.v2` 这种带点的主干名不能被当成扩展名削掉，否则同一个文件
 *  两边算出来的键会不一样。 */
const KNOWN_DOC_EXT_RE = /\.(?:xlsx|xlsm|xls|docx|doc|pdf|png|jpe?g|webp|bmp|tiff?)$/i;

function fileOutcomeKey(raw: unknown): string {
  const value = text(raw).trim();
  if (!value) return "";
  let base = value.replace(/\\/g, "/").split("/").pop() ?? "";
  // 反复削到削不动为止。两边给的字符串本来就差一层扩展名（扫描项是主干名、逐文件结果是
  // 带扩展名的文件名），只削一次的话 `合同.doc.pdf` 一边算出「合同」、另一边算出「合同.doc」，
  // 键对不上，那一行跑完还写着「未开始」——正是这一版要修的老毛病本身。
  while (KNOWN_DOC_EXT_RE.test(base)) base = base.replace(KNOWN_DOC_EXT_RE, "");
  return base.trim();
}

/** 这个文件里还有多少处需要人去看。三条路的字段各不相同：
 *  - Excel：后端逐文件给 review_count（位置计数，见 core/task_runner.py）
 *  - Word：issues[] 里只有 severity=needs_review 才是待办，resolved 是已经自动恢复好的
 *  - PDF：退回占位页的页数，加上「有疑点但仍然采用」的页数（后端算好的互斥计数，
 *    见 core/pdf_image_translation.py 的 suspect_adopted_page_count）。口径跟后端判
 *    needs_review 的那套一致，否则一份有占位页的文件在这一行是干干净净的「已生成」，
 *    而任务中心的同一份文件挂着「需复核」徽章。
 *  9.2.6 判的是 `status === "needs_review"`——Excel 实际给 `succeeded`，Word 根本没有
 *  这个字段，所以这个标签从来没出现过。 */
function fileResultPendingReview(item: JsonObject): number {
  const direct = num(item.review_count);
  if (direct > 0) return direct;
  const issues = Array.isArray(item.issues) ? item.issues : [];
  let pending = 0;
  for (const raw of issues) {
    if (text(record(raw).severity) === "needs_review") pending += 1;
  }
  if (pending > 0) return pending;
  return num(item.placeholder_page_count) + num(item.suspect_adopted_page_count);
}

function fileResultProduced(item: JsonObject): boolean {
  if (item.success === true) return true;
  if (item.status === "failed") return false;
  return Boolean(text(item.output) || text(item.compressed_output));
}

/** 整份都被「跳过 A3+」跳过的文件：输出文档写出来了，但内容跟源文件逐字节一样，一个字都没翻。
 *  后端把它的 success 判成 false 并算进 needs_review（`_file_record_to_result` /
 *  `_record_is_fully_skipped_oversize`），可它有输出路径，`fileResultProduced` 仍会算「已生成」。
 *  不单独认出来的话，这一行就是干干净净一句「已生成」，而任务中心给同一份文件挂着「需复核」。 */
function fileResultUntranslated(item: JsonObject): boolean {
  if (item.all_pages_skipped_oversize === true) return true;
  const pages = num(item.page_count);
  return pages > 0 && num(item.skipped_oversize_page_count) >= pages;
}

/** 状态词只有一份，跟任务中心共用（见 ui/src/task-state-labels.ts）。这里以前另存了一份
 *  switch，四个终态里没有一个跟任务中心写的一样，同一个任务在两屏上是两种说法。 */
function terminalStateWord(state: TaskStatus["state"]): string {
  return taskStateWord(state);
}

/** 终态 + 逐文件结果一起决定横幅的口气；只看 state 会把「一个文件都没生成」画成绿色。 */
function resultTone(
  state: TaskStatus["state"],
  generated: number,
  failed: number,
  review: number,
  autoFixed: number,
): ResultTone {
  if (state === "error" || state === "interrupted") return "fail";
  if (generated === 0) return "fail";
  if (failed > 0 || state === "stopped" || state === "completed_with_issues") return "warn";
  if (review > 0 || autoFixed > 0) return "warn";
  return "ok";
}

// ---------------------------------------------------------------------------
// 停止 / 暂停 / 继续 / 结束暂停
// ---------------------------------------------------------------------------

/** 正在展示的「安全停止」确认框。任务自己走到终态时要把它关掉——见 closeStopModal。 */
const stopModals: Record<Surface, ModalHandle | null> = { excel: null, word: null, pdf: null };

function closeStopModal(surface: Surface): void {
  // 任务已经结束了，再问「要不要停止当前任务」没有意义：这个框以前会一直浮在结果横幅
  // 上面，用户只能自己去点「继续执行」把它关掉，而那个按钮的字面意思正好相反。
  stopModals[surface]?.close();
  stopModals[surface] = null;
}

function confirmStopTask(surface: Surface, st: SurfaceState): void {
  const taskId = st.task?.task.task_id;
  if (!taskId) return;
  closeStopModal(surface);
  stopModals[surface] = openModal({
    tone: "warn",
    icon: "stop",
    sourceLabel: "停止运行中的任务",
    title: "安全停止当前任务？",
    body: ["已生成的文件会保留在输出目录；Excel、Word 会结束为终态。PDF / 图片应先「暂停提交」，再选择继续或结束暂停。"],
    actions: [
      { label: "继续执行", onClick: () => { stopModals[surface] = null; } },
      { label: "安全停止", variant: "danger-solid", onClick: () => { stopModals[surface] = null; void stopTask(taskId); } },
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
