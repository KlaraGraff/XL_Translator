import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";

import { ApiClient, type SseEvent, type TaskPreflight, type TaskStatus } from "./api-client";
import "./tokens.css";

type Surface = "excel" | "word" | "pdf";
type TaskSurface = TaskStatus["surface"];
type View = Surface | "tm" | "tasks";
type JsonObject = Record<string, unknown>;
type FileItem = JsonObject & {
  path: string;
  name: string;
  size_kb?: number;
  sheets?: string[];
  sheet_count?: number;
  relative_path?: string;
  format?: string;
  conversion_risks?: string[];
  risk_flags?: string[];
  risk?: JsonObject;
  needs_conversion?: boolean;
  stats_pending_conversion?: boolean;
  statistics_status?: string;
  page_count?: number;
  source_type?: "pdf" | "image" | string;
  width?: number;
  height?: number;
  width_px?: number;
  height_px?: number;
  image_width?: number;
  image_height?: number;
  paragraph_count?: number;
  table_count?: number;
};
type ScanSkippedItem = {
  path?: string;
  relative_path?: string;
  name?: string;
  reason?: string;
};
type ScanReport = {
  skipped: ScanSkippedItem[];
  summary: JsonObject;
  risk?: JsonObject;
};
type OutputDirectoryInspection = {
  state: "idle" | "empty" | "available" | "will_create" | "blocked";
  path: string;
  message: string;
};
type TmEntry = {
  id: number;
  source_text: string;
  target_text: string;
  pinned: number;
  word_type: string;
};
type TmImportEntry = {
  source_text: string;
  target_text: string;
  word_type?: string;
  pinned?: number | boolean | string;
  updated_at?: string;
};
type TmImportPreview = {
  fileName: string;
  format: "json" | "csv";
  langPair: string;
  entries: TmImportEntry[];
  mode: "skip" | "overwrite" | "keep_both";
  syncReverse: boolean;
};
type TmFullImportPreview = {
  fileName: string;
  payload: JsonObject;
  mode: "skip" | "overwrite" | "keep_both";
  codeMap: Record<string, string>;
};
type TmConflict = {
  id: number;
  source_text: string;
  existing_target: string;
  candidate_target: string;
  lang_pair: string;
  source_engine?: string;
  status: string;
};
type ModelImportPreview = {
  fileName: string;
  payload: JsonObject;
  roles: Array<{ role: string; fields: string[] }>;
  throughput_profile_count: number;
  api_key_count: number;
};
type TranslationSurface = "excel" | "word";
type LanguageOption = {
  code: string;
  display_name: string;
  aliases?: string[];
  description?: string;
  builtin?: boolean;
  can_source?: boolean;
  can_target?: boolean;
};
type ManagedTask = {
  task: TaskStatus;
  logs: Array<{ level: string; message: string }>;
  phaseName: string;
  stepDone: number;
  stepTotal: number;
  lastEventId: number;
  streamState: "idle" | "connected" | "reconnecting" | "interrupted";
  watcherActive: boolean;
};
type PendingTaskRisk = {
  surface: TaskSurface;
  payload: JsonObject;
  preflight: TaskPreflight;
};
type Modal = "tm-add" | "tm-edit" | "tm-delete" | "tm-clean" | "tm-import" | "tm-full-import" | "custom-language" | "model-import" | "migration" | "source-picker" | "stop-task" | "task-risk" | "xls-compatibility" | "doc-compatibility" | "pdf-review-unavailable" | "notice" | "update" | null;
type ModalNotice = { title: string; message: string };

const appRoot = document.querySelector<HTMLDivElement>("#app");
if (!appRoot) {
  throw new Error("Application root is unavailable.");
}
const app: HTMLDivElement = appRoot;

const client = new ApiClient();
const state: {
  connected: boolean;
  view: View;
  settings: JsonObject | null;
  panelOpen: boolean;
  files: Record<Surface, FileItem[]>;
  scanReports: Record<Surface, ScanReport>;
  selectedPaths: Record<Surface, string[]>;
  sourcePaths: Record<Surface, string>;
  excelOutputInspection: OutputDirectoryInspection;
  excelFileProgress: Record<string, JsonObject>;
  pendingExcelStart: "high_fidelity" | "compatibility" | null;
  wordOutputInspection: OutputDirectoryInspection;
  wordFileProgress: Record<string, JsonObject>;
  wordRecovery: JsonObject;
  pendingWordStart: "high_fidelity" | "compatibility" | null;
  pdfOutputInspection: OutputDirectoryInspection;
  pdfFileProgress: Record<string, JsonObject>;
  pdfPageRecovery: JsonObject;
  pdfReview: JsonObject;
  tasks: Record<string, ManagedTask>;
  taskOrder: string[];
  workspaceTaskIds: Record<Surface, string | null>;
  taskCenterFilter: "all" | "active" | "terminal";
  pendingTaskRisk: PendingTaskRisk | null;
  pendingStopTaskId: string | null;
  tmEntries: TmEntry[];
  tmStats: JsonObject;
  tmKeyword: string;
  tmTotal: number;
  tmPage: number;
  tmPageSize: number;
  tmSelectedIds: number[];
  tmRecentPairs: string[];
  tmSourceOptions: LanguageOption[];
  tmTargetOptions: LanguageOption[];
  tmImportPreview: TmImportPreview | null;
  tmFullImportPreview: TmFullImportPreview | null;
  tmConflicts: TmConflict[];
  tmCleaningState: "idle" | "running" | "ready" | "error";
  tmConflictMessage: string;
  toast: { message: string; error: boolean } | null;
  modal: Modal;
  sourcePickerSurface: Surface | null;
  tmEditing: TmEntry | null;
  tmSuggestions: JsonObject[];
  modalNotice: ModalNotice | null;
  updateResult: JsonObject | null;
  customLanguageEditing: LanguageOption | null;
  languages: LanguageOption[];
  sourceOptions: LanguageOption[];
  targetOptions: LanguageOption[];
  languageSearch: Record<Surface, string>;
  sourceSelections: Record<Surface, string>;
  targetSelections: Record<Surface, string>;
  tmSourceLang: string;
  tmTargetLang: string;
  modelRole: string;
  modelRoles: Record<string, JsonObject>;
  modelCatalog: Record<string, string[]>;
  modelCatalogMessage: Record<string, string>;
  modelCatalogConnection: Record<string, string>;
  modelThroughput: Record<string, JsonObject>;
  modelImportPreview: ModelImportPreview | null;
} = {
  connected: false,
  view: "excel",
  settings: null,
  panelOpen: false,
  files: { excel: [], word: [], pdf: [] },
  scanReports: {
    excel: { skipped: [], summary: {} },
    word: { skipped: [], summary: {} },
    pdf: { skipped: [], summary: {} },
  },
  selectedPaths: { excel: [], word: [], pdf: [] },
  sourcePaths: { excel: "", word: "", pdf: "" },
  excelOutputInspection: { state: "idle", path: "", message: "选择自定义目录后会在开始前检查；检查不会创建目录。" },
  excelFileProgress: {},
  pendingExcelStart: null,
  wordOutputInspection: { state: "idle", path: "", message: "选择自定义目录后会在开始前检查；检查不会创建目录。" },
  wordFileProgress: {},
  wordRecovery: {},
  pendingWordStart: null,
  pdfOutputInspection: { state: "idle", path: "", message: "选择自定义目录后会在开始前检查；检查不会创建目录。" },
  pdfFileProgress: {},
  pdfPageRecovery: {},
  pdfReview: {},
  tasks: {},
  taskOrder: [],
  workspaceTaskIds: { excel: null, word: null, pdf: null },
  taskCenterFilter: "all",
  pendingTaskRisk: null,
  pendingStopTaskId: null,
  tmEntries: [],
  tmStats: {},
  tmKeyword: "",
  tmTotal: 0,
  tmPage: 1,
  tmPageSize: 25,
  tmSelectedIds: [],
  tmRecentPairs: [],
  tmSourceOptions: [],
  tmTargetOptions: [],
  tmImportPreview: null,
  tmFullImportPreview: null,
  tmConflicts: [],
  tmCleaningState: "idle",
  tmConflictMessage: "",
  toast: null,
  modal: null,
  sourcePickerSurface: null,
  tmEditing: null,
  tmSuggestions: [],
  modalNotice: null,
  updateResult: null,
  customLanguageEditing: null,
  languages: [],
  sourceOptions: [],
  targetOptions: [],
  languageSearch: { excel: "", word: "", pdf: "" },
  sourceSelections: { excel: "auto", word: "auto", pdf: "" },
  targetSelections: { excel: "en", word: "en", pdf: "zh" },
  tmSourceLang: "zh",
  tmTargetLang: "en",
  modelRole: "translation",
  modelRoles: {},
  modelCatalog: {},
  modelCatalogMessage: {},
  modelCatalogConnection: {},
  modelThroughput: {},
  modelImportPreview: null,
};

const pageMeta: Record<View, { title: string; description: string }> = {
  excel: {
    title: "Excel 翻译",
    description: "扫描 .xlsx / .xls，生成任务清单并批量输出双语表格。",
  },
  word: {
    title: "Word 翻译",
    description: "扫描 .docx / .doc，保留版式生成双语对照文档。",
  },
  pdf: {
    title: "PDF 翻译",
    description: "逐页渲染并翻译，输出可复核的译图和诊断归档。",
  },
  tm: {
    title: "记忆库管理",
    description: "搜索、固定与清理译文，提升复用与一致性。",
  },
  tasks: {
    title: "任务中心",
    description: "集中查看运行、暂停和最近任务；任务离开原页面后仍会持续监控。",
  },
};

// The server remains the source of truth for the final prompt.  These copies
// are deliberately limited to the visible, editable built-in domain text so a
// user can inspect it before creating a page+domain override.  The protected
// JSON, placeholder, target-language, and source-language protocol is never
// exposed here and is appended by the translation engine.
const BUILTIN_DOMAIN_PROMPTS: Record<string, Record<string, string>> = {
  "同步工程场景": {
    _base: "你是一名面向工程同步场景的专业翻译助手。\n请优先采用工程资料与项目沟通中的常用表达，保持术语前后一致。\n原文中的编号、日期、计量单位、规格参数、版本号与符号必须原样保留。\n输出应简洁、准确、可直接用于工程过程文件、往来沟通与进度同步材料。",
    fr: "Tu es un assistant de traduction professionnel pour la synchronisation de projets d’ingénierie.\nUtiliser des formulations courantes dans les documents techniques et la communication de projet, avec une terminologie cohérente.\nConserver strictement inchangés les numéros, dates, unités, paramètres, versions et symboles du texte source.\nLa traduction doit être concise, précise et directement exploitable dans les documents de suivi et de coordination.",
  },
  "资料管理场景": {
    _base: "你是一名面向资料管理场景的专业翻译助手。\n请使用资料整理、归档、送审、台账与表单语境下的规范表达，保证字段名称一致。\n涉及编号、文号、日期、版本、附件标识时必须完整保留，不得改写结构。\n输出应便于资料员直接用于整理、流转、归档与审查。",
    fr: "Tu es un assistant de traduction professionnel pour la gestion documentaire.\nEmployer des formulations normalisées adaptées au classement, à l’archivage, à la soumission, aux registres et aux formulaires, avec cohérence des champs.\nConserver intégralement les numéros, références, dates, versions et identifiants de pièces jointes sans modifier la structure.\nLe résultat doit être directement réutilisable pour le tri, la circulation, l’archivage et la revue documentaire.",
  },
  "行政生活化场景": {
    _base: "你是一名面向行政与日常办公场景的翻译助手。\n请使用自然、清晰、礼貌且易理解的通用表达，避免过强行业术语。\n保留原文中的数字、时间、地址、联系人、编号等关键信息，不改变事实含义。\n输出应适用于通知、邮件、流程说明、日常沟通与生活化文本。",
    fr: "Tu es un assistant de traduction pour l’administration et le bureau au quotidien.\nUtiliser un style naturel, clair, poli et facile à comprendre, sans surcharge de jargon technique.\nConserver les informations clés du texte source (chiffres, dates, heures, adresses, contacts, références) sans altérer le sens factuel.\nLa traduction doit convenir aux notifications, e-mails, consignes de processus, communications courantes et contenus de vie quotidienne.",
  },
};

const iconPaths = {
  translate: '<path d="M4 6h7M7.5 4v2c0 3.6-1.7 6.2-4 7.4"/><path d="M6 9.2c.7 2 2.4 3.6 4.6 4.4"/><path d="M12.4 20l3.8-8.5L20 20M13.9 16.6h4.6"/>',
  excel: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.2"/><path d="M3.5 9.5h17M9.2 9.5v10M14.6 9.5v10"/>',
  word: '<path d="M6.5 3.5h7l4 4V19a1.5 1.5 0 0 1-1.5 1.5h-9.5A1.5 1.5 0 0 1 5 19V5A1.5 1.5 0 0 1 6.5 3.5z"/><path d="M13 3.5V8h4M8.5 12h7M8.5 15.2h7M8.5 18.4h4"/>',
  pdf: '<path d="M7 3.5h6.5l4 4V19a1.5 1.5 0 0 1-1.5 1.5H7A1.5 1.5 0 0 1 5.5 19V5A1.5 1.5 0 0 1 7 3.5z"/><path d="M13 3.5V8h4"/><path d="M8.6 16.6c1.4-.5 2.3-2.1 2.8-3.6"/>',
  memory: '<ellipse cx="12" cy="6" rx="7" ry="2.6"/><path d="M5 6v6c0 1.45 3.13 2.6 7 2.6s7-1.15 7-2.6V6M5 12v6c0 1.45 3.13 2.6 7 2.6s7-1.15 7-2.6v-6"/>',
  sliders: '<path d="M4 7h9M4 12h5M4 17h12"/><circle cx="17" cy="7" r="2.3"/><circle cx="13" cy="12" r="2.3"/><circle cx="20" cy="17" r="2.3"/>',
  folder: '<path d="M3.5 6.8A1.6 1.6 0 0 1 5.1 5.2h3.6l2 2.4h8.2a1.6 1.6 0 0 1 1.6 1.6v8.2a1.6 1.6 0 0 1-1.6 1.6H5.1a1.6 1.6 0 0 1-1.6-1.6z"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4-4"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="2.5"/>',
  warn: '<path d="M12 3.7l9 16H3l9-16z"/><path d="M12 9v4.7M12 17.1h.01"/>',
  check: '<path d="M5 12.5l4.6 4.5L19 7"/>',
  chevron: '<path d="M6 9.5l6 6 6-6"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 3v2.2M12 18.8V21M3 12h2.2M18.8 12H21M5.6 5.6l1.6 1.6M16.8 16.8l1.6 1.6M18.4 5.6l-1.6 1.6M7.2 16.8l-1.6 1.6"/>',
  moon: '<path d="M20 13.6A7.5 7.5 0 0 1 10.4 4 7.5 7.5 0 1 0 20 13.6z"/>',
  refresh: '<path d="M4.6 12a7.4 7.4 0 0 1 12.6-5.2L20 9"/><path d="M20 4.6V9h-4.4M19.4 12a7.4 7.4 0 0 1-12.6 5.2L4 15"/>',
  cloud: '<path d="M7.2 18.2A4.2 4.2 0 0 1 7 9.8a5.6 5.6 0 0 1 10.8-1.3A4.1 4.1 0 0 1 17 18.2z"/>',
  key: '<circle cx="8" cy="12" r="3.6"/><path d="M11.6 12H20M17 12v3M20 12v2.6"/>',
  globe: '<circle cx="12" cy="12" r="8.4"/><path d="M3.6 12h16.8M12 3.6c2.5 2.3 3.9 5.3 3.9 8.4S14.5 18.1 12 20.4C9.5 18.1 8.1 15.1 8.1 12S9.5 5.9 12 3.6z"/>',
  pin: '<path d="M9 3.5h6l-1 6 3 2.8V15H7v-2.7l3-2.8z"/><path d="M12 15v5.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  sparkle: '<path d="M10 3.5l1.4 4 4 1.4-4 1.4L10 14.3l-1.4-4-4-1.4 4-1.4z"/><path d="M17.5 13l.8 2.3 2.3.8-2.3.8-.8 2.3-.8-2.3-2.3-.8 2.3-.8z"/>',
  download: '<path d="M12 4v10M8.2 10.4l3.8 3.8 3.8-3.8M5 19h14"/>',
  file: '<path d="M7 3.5h6l4 4V19a1.5 1.5 0 0 1-1.5 1.5H7A1.5 1.5 0 0 1 5.5 19V5A1.5 1.5 0 0 1 7 3.5z"/><path d="M12.5 3.5V8H17"/>',
  tasks: '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>',
  play: '<path d="M8 5.5v13l10-6.5z"/>',
  copy: '<rect x="8" y="8" width="11" height="11" rx="1.5"/><path d="M5 15V5a1.5 1.5 0 0 1 1.5-1.5H15"/>',
  reveal: '<path d="M4 12s2.7-5 8-5 8 5 8 5-2.7 5-8 5-8-5-8-5z"/><circle cx="12" cy="12" r="2.1"/>',
} as const;
type IconName = keyof typeof iconPaths;

function icon(name: IconName, size = ""): string {
  return `<svg class="icon ${size}" viewBox="0 0 24 24" aria-hidden="true">${iconPaths[name]}</svg>`;
}

function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function record(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function number(value: unknown, fallback = 0): number {
  return typeof value === "number" ? value : fallback;
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    : [];
}

function hasOwn(value: JsonObject | null | undefined, key: string): boolean {
  return Boolean(value && Object.prototype.hasOwnProperty.call(value, key));
}

function excelOutputSettings(): JsonObject {
  const isolated = record(state.settings?.excel_output);
  // The Phase 4 core contract introduces excel_output.  Keep this fallback
  // while opening an existing new-baseline settings file before it has been
  // persisted once, so the Excel page never shows a blank form.
  return Object.keys(isolated).length ? isolated : record(state.settings?.output);
}

function excelOutputSettingPath(key: string): string {
  return hasOwn(state.settings, "excel_output") ? `excel_output.${key}` : `output.${key}`;
}

function excelReviewSettings(): JsonObject {
  return record(state.settings?.excel_review);
}

function wordOutputSettings(): JsonObject {
  const isolated = record(state.settings?.word_output);
  // Phase 5 moves Word away from the shared legacy output object.  The
  // fallback keeps the current new baseline usable until the isolated object
  // has been persisted for the first time.
  return Object.keys(isolated).length ? isolated : record(state.settings?.output);
}

function wordOutputSettingPath(key: string): string {
  return hasOwn(state.settings, "word_output") ? `word_output.${key}` : `output.${key}`;
}

function pdfOutputSettings(): JsonObject {
  return record(state.settings?.pdf_output);
}

function pdfOutputSettingPath(key: string): string {
  return `pdf_output.${key}`;
}

function pdfSettings(): JsonObject {
  return record(state.settings?.pdf);
}

function pdfIncludeImagesEnabled(): boolean {
  // "include_images" controls only independent image inputs.  It must not be
  // coupled to the visual translation protocol for pages inside a PDF.
  return Boolean(pdfSettings().include_images);
}

function pdfFileType(file: FileItem): "pdf" | "image" {
  if (text(file.source_type).toLowerCase() === "image") return "image";
  const format = fileFormat(file);
  return ["png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"].includes(format) ? "image" : "pdf";
}

function pdfImageDimensions(file: FileItem): string {
  const width = firstNumber(file, ["width", "width_px", "image_width"]);
  const height = firstNumber(file, ["height", "height_px", "image_height"]);
  return width !== null && height !== null ? `${width} × ${height}` : "尺寸未返回";
}

function pdfPageUnits(file: FileItem): number {
  return pdfFileType(file) === "image" ? 1 : Math.max(0, number(file.page_count));
}

function pdfScanSummary(): { files: number; selected: number; pdfs: number; images: number; units: number } {
  const files = state.files.pdf;
  return {
    files: files.length,
    selected: state.selectedPaths.pdf.length,
    pdfs: files.filter((file) => pdfFileType(file) === "pdf").length,
    images: files.filter((file) => pdfFileType(file) === "image").length,
    units: files.reduce((total, file) => total + pdfPageUnits(file), 0),
  };
}

function fileFormat(file: FileItem): string {
  const supplied = text(file.format).trim().replace(/^\./, "");
  if (supplied) return supplied.toLowerCase();
  const extension = file.path.split(".").pop()?.toLowerCase() || "";
  return extension;
}

function excelSheetCount(file: FileItem): number {
  return number(file.sheet_count, file.sheets?.length ?? 0);
}

function excelScanSummary(): { files: number; selected: number; sheets: number; xls: number } {
  const files = state.files.excel;
  return {
    files: files.length,
    selected: state.selectedPaths.excel.length,
    sheets: files.reduce((total, file) => total + excelSheetCount(file), 0),
    xls: files.filter((file) => fileFormat(file) === "xls").length,
  };
}

function displayPath(file: FileItem): string {
  return text(file.relative_path) || file.path;
}

function selectedExcelFiles(): FileItem[] {
  const selected = new Set(state.selectedPaths.excel);
  return state.files.excel.filter((file) => selected.has(file.path));
}

function selectedExcelXlsCount(): number {
  return selectedExcelFiles().filter((file) => fileFormat(file) === "xls").length;
}

function wordFileFormat(file: FileItem): string {
  return fileFormat(file) || "docx";
}

function wordNeedsConversion(file: FileItem): boolean {
  return Boolean(
    file.needs_conversion
    || file.stats_pending_conversion
    || text(file.statistics_status) === "conversion_required"
    || wordFileFormat(file) === "doc",
  );
}

function wordScanSummary(): { files: number; selected: number; paragraphs: number; tables: number; docs: number } {
  const files = state.files.word;
  const knownFiles = files.filter((file) => !wordNeedsConversion(file));
  const reported = state.scanReports.word.summary;
  return {
    files: files.length,
    selected: state.selectedPaths.word.length,
    paragraphs: number(reported.paragraph_count, knownFiles.reduce((total, file) => total + number(file.paragraph_count), 0)),
    tables: number(reported.table_count, knownFiles.reduce((total, file) => total + number(file.table_count), 0)),
    docs: number(reported.doc_unknown_count, files.filter(wordNeedsConversion).length),
  };
}

function selectedWordFiles(): FileItem[] {
  const selected = new Set(state.selectedPaths.word);
  return state.files.word.filter((file) => selected.has(file.path));
}

function selectedWordDocCount(): number {
  return selectedWordFiles().filter(wordNeedsConversion).length;
}

function modelCatalogConnectionKey({
  role,
  mode,
  provider,
  baseUrl,
}: {
  role: string;
  mode: string;
  provider: string;
  baseUrl: string;
}): string {
  // The secret fingerprint deliberately stays server-side.  The UI keeps only
  // enough non-sensitive identity to avoid showing a directory from a prior
  // provider/Base URL while the process-local server cache handles key scope.
  return [role, mode, provider, baseUrl.trim().replace(/\/$/, "")].join("|");
}

function modelCatalogConnectionForRole(role: string): string {
  const payload = record(state.modelRoles[role]);
  return modelCatalogConnectionKey({
    role,
    mode: text(payload.mode, "cloud"),
    provider: text(payload.provider),
    baseUrl: text(payload.base_url),
  });
}

function formatCheckedAt(value: string): string {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return `测试于 ${new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(parsed)}`;
}

function domainBuiltInPrompt(preset: string, targetLang: string): string {
  const prompts = BUILTIN_DOMAIN_PROMPTS[preset];
  if (!prompts) return "";
  return prompts[targetLang] || prompts._base || "";
}

function domainSettings(surface: TranslationSurface): {
  preset: string;
  customPrompt: string;
  promptOverrides: Record<string, string>;
  nameOverrides: Record<string, string>;
} {
  const prefix = surface === "excel" ? "excel" : "word";
  return {
    preset: text(state.settings?.[`${prefix}_domain_preset`], "同步工程场景"),
    customPrompt: text(state.settings?.[`${prefix}_custom_prompt`]),
    promptOverrides: Object.fromEntries(
      Object.entries(record(state.settings?.[`${prefix}_domain_prompt_overrides`]))
        .filter((entry): entry is [string, string] => typeof entry[1] === "string"),
    ),
    nameOverrides: Object.fromEntries(
      Object.entries(record(state.settings?.[`${prefix}_domain_name_overrides`]))
        .filter((entry): entry is [string, string] => typeof entry[1] === "string"),
    ),
  };
}

function engineSettings(): JsonObject {
  return record(state.settings?.engine);
}

function appearance(): JsonObject {
  return record(state.settings?.appearance);
}

function selectedTheme(): string {
  return text(appearance().theme, "system");
}

function applyTheme(theme: string): void {
  const resolved = theme === "system"
    ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
    : theme;
  document.documentElement.dataset.theme = resolved;
}

function isTaskActive(task: TaskStatus): boolean {
  return !task.terminal && !["done", "completed_with_issues", "error", "stopped", "interrupted"].includes(task.state);
}

function taskSurfaceLabel(surface: TaskSurface): string {
  return surface === "cleaner" || surface === "tm_clean" ? "TM 清洗" : surfaceLabel(surface);
}

function taskStateMeta(task: TaskStatus, streamState?: ManagedTask["streamState"]): { label: string; tone: string } {
  const labels: Record<string, string> = {
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
  if (streamState === "reconnecting" && isTaskActive(task)) {
    return { label: "正在补拉事件", tone: "running" };
  }
  if (streamState === "interrupted") {
    return { label: "应用中断", tone: "error" };
  }
  const tone = task.state === "completed_with_issues" ? "warn" : task.state;
  return { label: labels[task.state] ?? task.state, tone };
}

function activeTasks(): ManagedTask[] {
  return state.taskOrder
    .map((taskId) => state.tasks[taskId])
    .filter((entry): entry is ManagedTask => Boolean(entry) && isTaskActive(entry.task));
}

function workspaceTask(surface: Surface): ManagedTask | null {
  const taskId = state.workspaceTaskIds[surface];
  const selected = taskId ? state.tasks[taskId] : null;
  if (selected) return selected;
  const fallback = state.taskOrder
    .map((id) => state.tasks[id])
    .find((entry) => entry && entry.task.surface === surface);
  return fallback ?? null;
}

function taskStatus(): { label: string; tone: string } {
  const active = activeTasks();
  if (!active.length) return { label: "待执行", tone: "" };
  if (active.length === 1) return taskStateMeta(active[0].task, active[0].streamState);
  return { label: `${active.length} 个活动任务`, tone: "running" };
}

function newManagedTask(task: TaskStatus): ManagedTask {
  const previous = state.tasks[task.task_id];
  return {
    task: { ...previous?.task, ...task },
    logs: previous?.logs ?? (task.logs ?? []).map((entry) => ({
      level: text(entry.level, "INFO"),
      message: text(entry.message),
    })),
    phaseName: previous?.phaseName ?? "正在准备任务",
    stepDone: previous?.stepDone ?? 0,
    stepTotal: previous?.stepTotal ?? 0,
    lastEventId: previous?.lastEventId ?? 0,
    streamState: previous?.streamState ?? "idle",
    watcherActive: previous?.watcherActive ?? false,
  };
}

function upsertTask(task: TaskStatus, focusWorkspace = false): ManagedTask {
  const entry = newManagedTask(task);
  state.tasks[task.task_id] = entry;
  state.taskOrder = [task.task_id, ...state.taskOrder.filter((id) => id !== task.task_id)];
  if ((task.surface === "excel" || task.surface === "word" || task.surface === "pdf") && focusWorkspace) {
    state.workspaceTaskIds[task.surface] = task.task_id;
  }
  return entry;
}

function render(): void {
  applyTheme(selectedTheme());
  const meta = pageMeta[state.view];
  const task = taskStatus();
  const model = text(engineSettings().cloud_model, "未选择模型");
  app.innerHTML = `
    <div class="app-shell">
      <nav class="rail" aria-label="主导航">
        <div class="logo">${icon("translate", "large")}</div>
        ${navButton("excel", "Excel", "excel")}
        ${navButton("word", "Word", "word")}
        ${navButton("pdf", "PDF", "pdf")}
        <div class="rail-divider"></div>
        ${navButton("tm", "记忆库", "memory")}
        ${navButton("tasks", "任务", "tasks")}
        <button class="rail-button ${state.panelOpen ? "active" : ""}" data-action="toggle-panel" data-tip="模型配置：全局应用于所有翻译页面">${icon("sliders")}<span>配置</span></button>
        <div class="rail-spacer"></div>
        <button class="rail-button utility" data-action="cycle-theme" data-tip="主题：浅色、深色或跟随系统">${icon(selectedTheme() === "dark" ? "sun" : "moon")}</button>
      </nav>
      ${renderConfigPanel()}
      <main class="stage">
        <header class="topbar">
          <div class="topbar-copy">
            <div class="topbar-title-row"><h1>${meta.title}</h1><span class="status ${task.tone}"><span class="led"></span>${task.label}</span></div>
            <p>${meta.description}</p>
          </div>
          <div class="topbar-actions">
            <button class="model-button ${activeTasks().length ? "task-activity" : ""}" data-action="open-task-center" data-tip="任务中心：查看运行、暂停和最近任务"><span class="model-dot ${activeTasks().length ? "active" : ""}"></span>${activeTasks().length ? `${activeTasks().length} 个活动任务` : "任务中心"}</button>
            <button class="model-button" data-action="toggle-panel" data-tip="当前翻译模型；点击展开配置"><span class="model-dot"></span>${escapeHtml(model)}</button>
            <button class="icon-button" data-action="check-update" data-tip="检查 GitHub 最新版本">${icon("refresh", "small")}</button>
            <button class="icon-button" data-action="cycle-theme" data-tip="切换主题">${icon(selectedTheme() === "dark" ? "sun" : "moon", "small")}</button>
          </div>
        </header>
        <div class="content">
          ${state.view === "tm" ? renderTmView() : state.view === "tasks" ? renderTaskCenter() : renderTranslateView(state.view)}
        </div>
      </main>
    </div>
    ${renderModal()}
    ${state.toast ? `<div class="toast ${state.toast.error ? "error" : ""}">${escapeHtml(state.toast.message)}</div>` : ""}
  `;
}

function navButton(view: View, label: string, iconName: IconName): string {
  return `<button class="rail-button ${state.view === view ? "active" : ""}" data-action="navigate" data-view="${view}" data-tip="${label}">${icon(iconName)}<span>${label}</span></button>`;
}

function renderConfigPanel(): string {
  const role = state.modelRole;
  const rolePayload = record(state.modelRoles[role]);
  const engine = engineSettings();
  const cloudMode = role !== "translation" || text(engine.mode, "cloud") === "cloud";
  const provider = role === "translation"
    ? (cloudMode ? text(engine.cloud_provider, "custom_openai") : text(engine.local_provider, "ollama"))
    : text(rolePayload.provider, "custom_openai");
  const baseUrl = role === "translation"
    ? (cloudMode ? text(engine.cloud_base_url) : text(engine.local_base_url))
    : text(rolePayload.base_url);
  const model = role === "translation"
    ? (cloudMode ? text(engine.cloud_model) : text(engine.local_model))
    : text(rolePayload.model);
  const providers = cloudMode
    ? ["custom_openai", "openai", "claude", "zhipu", "dashscope", "siliconflow"]
    : ["ollama", "lm_studio", "custom_local"];
  const roleLabels: Record<string, string> = { translation: "翻译模型（Excel / Word）", cleaner: "深度清洗模型", image: "PDF 翻译模型（图像生成）", pdf_review: "PDF 翻译审核模型" };
  const sourceRoles: Record<string, string[]> = { cleaner: ["independent", "translation"], image: ["independent", "translation"], pdf_review: ["independent", "translation", "image"] };
  const sourceRole = text(rolePayload.source_role, "independent");
  const availability = text(rolePayload.availability_status, "unknown");
  const availabilityMessage = text(rolePayload.availability_message, "当前配置尚未测试。");
  const throughput = record(state.modelThroughput[role] || rolePayload.throughput);
  const bounds = record(rolePayload.throughput_bounds);
  const batchBounds = Array.isArray(bounds.batch_size) ? bounds.batch_size as unknown[] : [];
  const concurrencyBounds = Array.isArray(bounds.concurrency) ? bounds.concurrency as unknown[] : [];
  const catalogConnection = modelCatalogConnectionForRole(role);
  const catalogMatchesConnection = state.modelCatalogConnection[role] === catalogConnection;
  const catalog = catalogMatchesConnection ? state.modelCatalog[role] || [] : [];
  const catalogMessage = catalogMatchesConnection
    ? state.modelCatalogMessage[role] || "尚未读取当前连接的模型目录。"
    : "当前连接尚未读取模型目录。保存连接后可手动刷新。";
  const hasApiKey = Boolean(rolePayload.has_api_key);
  const checkedAt = formatCheckedAt(text(rolePayload.availability_checked_at));
  const modelListId = `model-catalog-${role}`;
  const throughputControls = role === "translation" || role === "cleaner"
    ? `<div class="field-row throughput-row"><div><label class="field-label" for="throughputBatch">批次大小</label><input id="throughputBatch" type="number" data-throughput="batch_size" value="${number(throughput.batch_size, 8)}" min="${number(batchBounds[0], 1)}" max="${number(batchBounds[1], 128)}"/></div><div><label class="field-label" for="throughputConcurrency">并发数</label><input id="throughputConcurrency" type="number" data-throughput="concurrency" value="${number(throughput.concurrency, 1)}" min="${number(concurrencyBounds[0], 1)}" max="${number(concurrencyBounds[1], 32)}"/></div></div>`
    : `<label class="field-label" for="throughputConcurrency">并发数</label><input id="throughputConcurrency" type="number" data-throughput="concurrency" value="${number(throughput.concurrency, 1)}" min="${number(concurrencyBounds[0], 1)}" max="${number(concurrencyBounds[1], 32)}"/>`;
  return `<aside class="config-panel ${state.panelOpen ? "" : "closed"}">
    <div class="config-inner">
      <div class="config-header"><div class="config-icon">${icon("sliders", "small")}</div><div><h2>模型配置</h2><p>四个角色 · ${escapeHtml(roleLabels[role] || role)}</p></div><button class="icon-button" data-action="toggle-panel" data-tip="折叠模型配置">${icon("chevron", "small")}</button></div>
      <div class="config-body">
        <div class="config-group"><label class="field-label" for="modelRole">模型角色</label><select id="modelRole" data-model-role>${Object.entries(roleLabels).map(([key, label]) => `<option value="${key}" ${key === role ? "selected" : ""}>${label}</option>`).join("")}</select></div>
        <div class="config-group">
          <p class="note">Excel 与 Word 的专业领域和 Prompt 在各自页面独立保存；PDF 使用固定版式协议。</p>
        </div>
        <div class="config-group">
          <span class="field-label">接入方式</span>
          ${role === "translation" ? `<select id="engineMode" data-engine="mode"><option value="cloud" ${cloudMode ? "selected" : ""}>云端 API</option><option value="local" ${cloudMode ? "" : "selected"}>本地模型</option></select>` : `<select id="roleSource" data-role-source>${(sourceRoles[role] || ["independent"]).map((item) => `<option value="${item}" ${sourceRole === item ? "selected" : ""}>${item === "independent" ? "独立配置" : `跟随${roleLabels[item] || item}`}</option>`).join("")}</select>`}
          ${role !== "translation" ? `<p class="note">复用只共享服务商、Base URL 和 Key；本角色的模型名称、吞吐和测试状态始终独立。不能链式复用。</p>` : ""}
          <label class="field-label" for="provider" style="margin-top:9px">${cloudMode ? "服务商" : "本地运行器"}</label>
          <select id="provider" data-engine="cloud_provider" ${role !== "translation" && sourceRole !== "independent" ? "disabled" : ""}>
            ${providers.map((item) => `<option value="${item}" ${provider === item ? "selected" : ""}>${providerLabel(item)}</option>`).join("")}
          </select>
          <label class="field-label" for="baseUrl" style="margin-top:9px">Base URL</label>
          <input id="baseUrl" value="${escapeHtml(baseUrl)}" placeholder="https://.../v1" data-engine="cloud_base_url" ${role !== "translation" && sourceRole !== "independent" ? "disabled" : ""}/>
          <label class="field-label" for="modelName" style="margin-top:9px">模型名称</label>
          <input id="modelName" list="${modelListId}" value="${escapeHtml(model)}" placeholder="例如 ${cloudMode ? "gpt-4o-mini" : "qwen2.5:7b"}" data-engine="cloud_model" />
          <datalist id="${modelListId}">${catalog.map((item) => `<option value="${escapeHtml(item)}"></option>`).join("")}</datalist>
          ${cloudMode ? `<label class="field-label" for="apiKey" style="margin-top:9px">API Key</label><input id="apiKey" type="password" placeholder="留空则保留当前密钥" />` : `<p class="note">本地模型不保存云端 API Key；请确认本地服务已启动。</p>`}
          <div class="field-row" style="margin-top:10px"><button class="button" data-action="save-model">${icon("check", "small")}保存角色</button><button class="button" data-action="fetch-models">${icon("refresh", "small")}刷新模型目录</button><button class="button" data-action="test-model">测试当前角色</button></div><p class="note">模型目录：${escapeHtml(catalogMessage)}${catalog.length ? `（${catalog.length} 个，可手动填写未列出的模型）` : ""}</p><p class="note">测试状态：${escapeHtml(availability === "available" ? "通过" : availability === "unavailable" ? "失败" : "未测试")} · ${escapeHtml(availabilityMessage)}${checkedAt ? ` · ${escapeHtml(checkedAt)}` : ""}${hasApiKey ? " · 已保存连接密钥" : " · 未检测到连接密钥"}。未测试不会阻止启动任务。</p>
          <div class="config-subgroup"><span class="field-label">角色吞吐</span>${throughputControls}<div class="field-row"><button class="button" data-action="save-throughput">保存吞吐设置</button><button class="button" data-action="restore-throughput">恢复推荐值</button></div><p class="note">运行时 429/超时降速只影响当前任务，不会修改此长期档案。</p></div>
        </div>
        <div class="config-group">
          <span class="field-label">配置文件</span>
          <div class="field-row"><button class="button" data-action="export-model-config">导出（不含 Key）</button><button class="button" data-action="export-model-config-sensitive">导出含 Key</button><button class="button" data-action="import-model-config">导入 v3</button></div><p class="note">导入只接受 v3，先预览；导入后所有角色需重新测试。含 Key 导出会显示二次确认。</p>
        </div>
        <div class="config-group">
          <span class="field-label">维护</span>
          <div class="field-row"><button class="button" data-action="download-diagnostics">诊断归档</button><button class="button" data-action="migration">数据迁移</button></div>
        </div>
        <div class="config-group">
          <span class="field-label">并发风险</span>
          <p class="note">不同类型任务可能共用同一 API 连接，并发会叠加，可能触发服务商限流或费用增长。此面板不会全局锁定其他页面；已启动任务保留自己的配置快照，后续任务会在任务中心进行风险确认。</p>
        </div>
      </div>
    </div>
  </aside>`;
}

function renderTranslateView(surface: Surface): string {
  const files = state.files[surface];
  const selectedPaths = state.selectedPaths[surface];
  const sourcePath = state.sourcePaths[surface];
  const isExcel = surface === "excel";
  const isWord = surface === "word";
  const isPdf = surface === "pdf";
  const excelSummary = excelScanSummary();
  const wordSummary = wordScanSummary();
  const pdfSummary = pdfScanSummary();
  const target = state.targetSelections[surface] || (isPdf
    ? text(record(state.settings?.pdf).target_lang, "zh")
    : text(state.settings?.target_lang, "en"));
  const source = state.sourceSelections[surface] || "auto";
  const running = workspaceTask(surface);
  const activeTask = Boolean(running && isTaskActive(running.task));
  const percent = running && running.stepTotal > 0
    ? Math.round((running.stepDone / running.stepTotal) * 100)
    : 0;
  return `<section class="view active"><div class="two-column">
    <div class="left-column">
      <div class="card source-bar">
        <div class="source-icon">${icon("folder")}</div>
        <div class="source-meta"><div class="source-key">源路径</div><input class="source-input" data-source="${surface}" value="${escapeHtml(sourcePath)}" placeholder="选择文件或文件夹" /></div>
        <button class="button" data-action="choose-source" data-surface="${surface}" data-tip="选择${surfaceLabel(surface)}文件或文件夹" ${activeTask ? "disabled" : ""}>${icon("folder", "small")}浏览</button>
        <button class="button primary" data-action="scan" data-surface="${surface}" ${activeTask ? "disabled" : ""}>${icon("search", "small")}扫描</button>
      </div>
      <div class="stats">
        ${isExcel
          ? `${stat("file", "已扫描文件", String(excelSummary.files))}
             ${stat("translate", "已选文件", String(excelSummary.selected))}
             ${stat("excel", "总工作表", String(excelSummary.sheets))}
             ${stat("warn", ".xls 文件", String(excelSummary.xls))}`
          : isWord
            ? `${stat("file", "已扫描文件", String(wordSummary.files))}
               ${stat("translate", "已选文件", String(wordSummary.selected))}
               ${stat("word", "已知正文段落", String(wordSummary.paragraphs))}
               ${stat("file", "已知表格", String(wordSummary.tables))}
               ${stat("warn", ".doc 待转换", String(wordSummary.docs))}`
          : `${stat("pdf", "PDF 文件", String(pdfSummary.pdfs))}
             ${stat("file", "独立图片", String(pdfSummary.images))}
             ${stat("translate", "页 / 图片", String(pdfSummary.units))}
             ${stat("warn", "扫描跳过", String(state.scanReports.pdf.skipped.length))}`}
      </div>
      <div class="card table-card"><div class="table-header"><h2>任务清单</h2><span class="table-count">已选 ${selectedPaths.length} / ${files.length}</span><span class="header-spacer"></span><button class="mini-button" data-action="select-all-files" data-surface="${surface}" ${activeTask ? "disabled" : ""}>全选</button><button class="mini-button" data-action="select-no-files" data-surface="${surface}" ${activeTask ? "disabled" : ""}>全不选</button></div><div class="table-scroll">${renderFiles(files, surface, selectedPaths, activeTask)}</div></div>
      ${isExcel ? renderExcelSkippedItems() : ""}
      ${isWord ? renderWordSkippedItems() : ""}
      ${isPdf ? renderPdfScanReport() : ""}
    </div>
    <aside class="card right-column">
      <span class="section-label">运行设置</span>
      <div class="setting-card"><label class="field-label" for="language-search-${surface}">搜索语言</label><input id="language-search-${surface}" data-language-search="${surface}" value="${escapeHtml(state.languageSearch[surface])}" placeholder="中文名、English、ISO 代码" ${activeTask ? "disabled" : ""}/><label class="field-label" for="target-${surface}" style="margin-top:8px">目标语言</label><select id="target-${surface}" data-target="${surface}" ${activeTask ? "disabled" : ""}>${languageOptions(target, state.languageSearch[surface])}</select><div class="field-row" style="margin-top:8px"><button class="mini-button" data-action="custom-language-add" ${activeTask ? "disabled" : ""}>＋ 自定义语言</button><button class="mini-button" data-action="custom-language-manage" ${activeTask ? "disabled" : ""}>管理自定义</button></div></div>
      ${surface === "excel" || surface === "word" ? renderDomainSettings(surface, activeTask) : ""}
      ${isPdf ? `<p class="note">PDF 目标语言独立保存；页图翻译由模型识别原文，无需指定源语言。</p>` : `<label class="field-label" style="margin-top:10px" for="source-${surface}">源语言</label><select id="source-${surface}" data-source-language="${surface}" ${activeTask ? "disabled" : ""}>${sourceLanguageOptions(source, state.languageSearch[surface])}</select><p class="note">自动识别会在每个有候选文本的文件开始翻译前发送一次抽样预检。</p>`}
      ${isPdf ? `<div class="toggle-row"><input id="pdfReview" type="checkbox" ${record(state.settings?.pdf).review_enabled ? "checked" : ""} data-pdf-review ${activeTask ? "disabled" : ""}/><label for="pdfReview">启用逐页审核模型</label></div>` : `<div class="toggle-row"><input id="untranslated-${surface}" type="checkbox" data-untranslated ${activeTask ? "disabled" : ""}/><label for="untranslated-${surface}">仅补译未翻译内容</label></div>`}
      ${renderDetailedSettings(surface, activeTask)}
      ${isExcel ? renderExcelStartPreflight(source, target) : ""}
      ${isWord ? renderWordStartPreflight(source, target) : ""}
      ${isPdf ? renderPdfStartPreflight() : ""}
      <hr class="divider" />
      ${running ? renderRunningPanel(running, percent) : `<div class="push"><button class="button primary block large" data-action="start-task" data-surface="${surface}" ${selectedPaths.length ? "" : "disabled"}>${icon("translate", "small")}开始${surfaceLabel(surface)}翻译</button><p class="note">可执行 ${selectedPaths.length} / ${files.length} 个文件；任务启动后，日志与进度将通过 SSE 实时显示。</p></div>`}
    </aside>
  </div></section>`;
}

function renderDetailedSettings(surface: Surface, disabled: boolean): string {
  const output = surface === "excel"
    ? excelOutputSettings()
    : surface === "word"
      ? wordOutputSettings()
      : pdfOutputSettings();
  const excelReview = excelReviewSettings();
  const wordBatch = record(state.settings?.word_batch);
  const wordReview = record(state.settings?.word_review);
  const wordConversion = record(state.settings?.word_conversion);
  const pdf = record(state.settings?.pdf);
  const inputDisabled = disabled ? "disabled" : "";
  const checked = (value: unknown) => value ? "checked" : "";
  const outputMode = output.use_custom_output_dir ? "custom" : "source";
  const outputPrefix = surface === "excel"
    ? excelOutputSettingPath
    : surface === "word"
      ? wordOutputSettingPath
      : pdfOutputSettingPath;
  const outputInspectionState = surface === "excel"
    ? state.excelOutputInspection
    : surface === "word"
      ? state.wordOutputInspection
      : state.pdfOutputInspection;
  const outputInspection = outputMode === "custom"
    ? `<p id="${surface}-output-state" class="output-path-state ${outputInspectionState.state}">${escapeHtml(outputInspectionState.message)}</p>`
    : "";
  const outputPicker = surface === "excel" || surface === "word" || surface === "pdf"
    ? `<button class="mini-button" type="button" data-action="choose-${surface}-output" ${inputDisabled}>${icon("folder", "small")}选择文件夹</button>`
    : "";
  const inspectionAttribute = surface === "excel"
    ? "data-excel-output-inspect"
    : surface === "word"
      ? "data-word-output-inspect"
      : "data-pdf-output-inspect";
  const common = `<label class="field-label" for="output-${surface}">输出位置</label><select id="output-${surface}" data-setting-path="${outputPrefix("use_custom_output_dir")}" data-value-kind="custom-output" ${inputDisabled}><option value="source" ${outputMode === "source" ? "selected" : ""}>源目录内</option><option value="custom" ${outputMode === "custom" ? "selected" : ""}>自定义目录</option></select><div class="output-path-row"><input id="${surface}-output-path" value="${escapeHtml(text(output.custom_output_dir))}" placeholder="自定义输出根目录（运行时才创建）" data-setting-path="${outputPrefix("custom_output_dir")}" ${inspectionAttribute} ${inputDisabled}/>${outputPicker}</div>${outputInspection}`;
  const excel = `<div class="toggle-row"><input type="checkbox" id="keepOriginal" data-setting-path="${outputPrefix("keep_original_sheets")}" ${checked(output.keep_original_sheets)} ${inputDisabled}/><label for="keepOriginal">保留每个工作表的“_原文”副本</label></div><div class="toggle-row"><input type="checkbox" id="formulaBackfill" data-setting-path="${outputPrefix("formula_display_value_backfill")}" ${checked(output.formula_display_value_backfill)} ${inputDisabled}/><label for="formulaBackfill">公式显示值按静态双语文本回填</label></div><div class="toggle-row"><input type="checkbox" id="excelAutofit" data-setting-path="${outputPrefix("enable_excel_autofit")}" ${checked(output.enable_excel_autofit)} ${inputDisabled}/><label for="excelAutofit">使用 Excel 精调行高（需本机 Excel）</label></div><div class="toggle-row"><input type="checkbox" id="lockRowHeight" data-setting-path="${outputPrefix("lock_row_height")}" ${checked(output.lock_row_height)} ${inputDisabled}/><label for="lockRowHeight">锁定行高并缩小字号（与精调行高互斥）</label></div><p class="note">默认使用 Python 估算行高；精调不可用时保留该结果并在文件结果中提示。最小字号仍可能溢出的单元格会进入复核。</p><div class="toggle-row"><input type="checkbox" id="reviewMark" data-setting-path="excel_review.mark_review_items" ${checked(excelReview.mark_review_items)} ${inputDisabled}/><label for="reviewMark">标记需复核内容</label></div><label class="field-label">已有底色处理</label><select data-setting-path="excel_review.existing_fill_policy" ${inputDisabled}><option value="skip" ${text(excelReview.existing_fill_policy) === "skip" ? "selected" : ""}>不覆盖已有底色</option><option value="red_font" ${text(excelReview.existing_fill_policy) === "red_font" ? "selected" : ""}>保留底色并使用红字（默认）</option><option value="overwrite" ${text(excelReview.existing_fill_policy) === "overwrite" ? "selected" : ""}>以复核色覆盖底色</option></select>${renderReviewColors(inputDisabled)}`;
  const word = `<div class="toggle-row"><input type="checkbox" id="wordNativePreprocessing" data-setting-path="word_conversion.use_native_preprocessing" ${checked(wordConversion.use_native_preprocessing)} ${inputDisabled}/><label for="wordNativePreprocessing">启用本地 Word / LibreOffice 自动编号预处理</label></div><p class="note">开启时依次尝试本机 Microsoft Word 和 LibreOffice；不可用时自动以 Python 保守物化编号。关闭时全程只使用 Python。所有预处理都发生在临时副本。</p><div class="toggle-row"><input type="checkbox" id="wordHighlight" data-setting-path="word_review.highlight_unresolved" ${checked(wordReview.highlight_unresolved)} ${inputDisabled}/><label for="wordHighlight">标记需复核内容</label></div><label class="field-label">已有高亮处理</label><select data-setting-path="word_review.existing_highlight_policy" ${inputDisabled}><option value="skip" ${text(wordReview.existing_highlight_policy) === "skip" ? "selected" : ""}>不覆盖已有高亮</option><option value="red_underline" ${text(wordReview.existing_highlight_policy) === "red_underline" ? "selected" : ""}>保留已有高亮并使用红字下划线（默认）</option><option value="overwrite" ${text(wordReview.existing_highlight_policy) === "overwrite" ? "selected" : ""}>以复核色覆盖已有高亮</option></select>${renderWordReviewColors(inputDisabled)}<div class="toggle-row"><input type="checkbox" id="protectSchemeCover" ${inputDisabled}/><label for="protectSchemeCover">补译时保护方案封面与目录</label></div><p class="note">仅在“仅补译未翻译内容”开启时生效；默认关闭。目录和域代码始终保护，不作为普通正文翻译。</p><label class="field-label" for="wordBatchParagraphs">每批最大段落数</label><input id="wordBatchParagraphs" type="number" min="1" data-setting-path="word_batch.max_paragraphs_per_batch" data-value-kind="number" value="${number(wordBatch.max_paragraphs_per_batch, 30)}" ${inputDisabled}/><label class="field-label" for="wordBatchChars">每批字符上限</label><input id="wordBatchChars" type="number" min="1" data-setting-path="word_batch.max_chars_per_batch" data-value-kind="number" value="${number(wordBatch.max_chars_per_batch, 3000)}" ${inputDisabled}/><label class="field-label" for="wordSplitThreshold">长段拆分阈值</label><input id="wordSplitThreshold" type="number" min="1" data-setting-path="word_batch.split_paragraph_chars" data-value-kind="number" value="${number(wordBatch.split_paragraph_chars, 3000)}" ${inputDisabled}/><p class="note">拆分只发生在模型请求层，响应后按原顺序回写，不会新增 Word 段落或破坏编号、数字和单位。拆分阈值会自动校正为不低于字符上限。</p><label class="field-label" for="wordStrictRetry">单段严格重试次数</label><input id="wordStrictRetry" type="number" min="1" max="8" data-setting-path="word_batch.strict_retry_attempts" data-value-kind="number" value="${number(wordBatch.strict_retry_attempts, 3)}" ${inputDisabled}/><p class="note">仅对空译文、明显不完整或质量校验失败段落重试；合格内容不会重复请求。未恢复内容保留原文并进入复核。</p>`;
  const pdfControls = `<div class="toggle-row"><input type="checkbox" id="pdfCompressed" data-setting-path="pdf.generate_compressed_pdf" ${checked(pdf.generate_compressed_pdf)} ${inputDisabled}/><label for="pdfCompressed">生成压缩 PDF</label></div><div class="toggle-row"><input type="checkbox" id="pdfImages" data-setting-path="pdf.include_images" ${checked(pdf.include_images)} ${inputDisabled}/><label for="pdfImages">允许选择独立图片文件</label></div><p class="note">此开关只决定 PNG、JPG/JPEG、WebP、BMP、TIF/TIFF 是否作为独立输入扫描；PDF 页面一律按版式协议处理。</p><label class="field-label">单页重试次数</label><input type="number" min="0" max="10" data-setting-path="pdf.page_retry_attempts" data-value-kind="number" value="${number(pdf.page_retry_attempts, 2)}" ${inputDisabled}/><label class="field-label">页图并发（留空自动）</label><input type="number" min="1" data-setting-path="pdf.page_generation_concurrency" data-value-kind="optional-number" value="${escapeHtml(text(pdf.page_generation_concurrency === null ? "" : pdf.page_generation_concurrency))}" ${inputDisabled}/>`;
  return `<details class="advanced-settings"><summary>更多参数</summary><div class="advanced-settings-body">${common}${surface === "excel" ? excel : surface === "word" ? word : pdfControls}</div></details>`;
}

function renderDomainSettings(surface: TranslationSurface, disabled: boolean): string {
  const prefix = surface === "excel" ? "excel" : "word";
  const preset = text(state.settings?.[`${prefix}_domain_preset`], "同步工程场景");
  const customPrompt = text(state.settings?.[`${prefix}_custom_prompt`]);
  const overrides = record(state.settings?.[`${prefix}_domain_prompt_overrides`]);
  const targetLang = state.targetSelections[surface];
  const builtInPrompt = domainBuiltInPrompt(preset, targetLang);
  const isCustom = preset === "自定义";
  const hasOverride = !isCustom && Object.prototype.hasOwnProperty.call(overrides, preset);
  const prompt = isCustom ? customPrompt : hasOverride ? text(overrides[preset]) : builtInPrompt;
  const disabledAttr = disabled ? "disabled" : "";
  const options = ["同步工程场景", "资料管理场景", "行政生活化场景", "自定义"];
  const promptLabel = isCustom
    ? "自定义领域 Prompt"
    : hasOverride ? "当前领域覆盖 Prompt" : "内置领域 Prompt（可查看、可编辑为覆盖）";
  const saveLabel = isCustom ? "保存自定义 Prompt" : "保存覆盖";
  const restore = !isCustom
    ? `<button class="mini-button" data-action="restore-domain-prompt" data-surface="${surface}" ${disabledAttr}>恢复内置默认</button>`
    : "";
  return `<div class="setting-card domain-settings"><label class="field-label" for="${prefix}DomainPreset">专业领域（${surface === "excel" ? "Excel" : "Word"} 独立）</label><select id="${prefix}DomainPreset" data-domain-preset="${surface}" ${disabledAttr}>${options.map((item) => `<option value="${item}" ${item === preset ? "selected" : ""}>${item}</option>`).join("")}</select><label class="field-label" for="${prefix}DomainPrompt" style="margin-top:8px">${promptLabel}</label><textarea id="${prefix}DomainPrompt" data-domain-prompt="${surface}" ${disabledAttr} placeholder="${isCustom ? "请输入完整领域 Prompt" : "内置 Prompt 会在此显示"}">${escapeHtml(prompt)}</textarea><div class="field-row domain-actions"><button class="mini-button primary" data-action="save-domain-prompt" data-surface="${surface}" ${disabledAttr}>${saveLabel}</button>${restore}</div><p class="note">固定输出 JSON、格式/占位符保护、目标语言与逐条原文语言回报由应用追加，不能被领域 Prompt 覆盖。</p></div>`;
}

function renderReviewColors(disabled: string): string {
  const colors = record(excelReviewSettings().mark_colors);
  const colorField = (mark: string, label: string, fallback: string) => {
    const value = text(colors[mark], fallback).replace(/^#/, "");
    return `<label class="field-label">${label}</label><input type="color" value="#${escapeHtml(value)}" data-review-color="${mark}" ${disabled}/>`;
  };
  return `<div class="review-colors">${colorField("semantic", "语义校验接受", "FFF2CC")}${colorField("unresolved", "保留原文复核", "FCE4D6")}${colorField("foreign_noise", "疑似原文异常", "F4CCCC")}</div>`;
}

function renderWordReviewColors(disabled: string): string {
  const colors = record(record(state.settings?.word_review).mark_colors);
  const colorField = (mark: string, label: string, fallback: string) => {
    const value = text(colors[mark], fallback).replace(/^#/, "");
    return `<label class="field-label">${label}（Word 高亮）</label><input type="color" value="#${escapeHtml(value)}" data-word-review-color="${mark}" ${disabled}/>`;
  };
  return `<div class="review-colors">${colorField("semantic", "语义校验接受", "FFF2CC")}${colorField("unresolved", "保留原文复核", "FCE4D6")}${colorField("foreign_noise", "疑似原文异常", "F4CCCC")}</div>`;
}

function renderRunningPanel(running: ManagedTask, percent: number): string {
  const logs = running.logs.slice(-10).map((item) => `<div class="log-${logTone(item.level)}">› ${escapeHtml(item.message)}</div>`).join("");
  const terminal = running.task.terminal;
  const resultMessage = text(running.task.result?.message, terminal ? "任务已结束。" : "");
  const resultDetail = terminal
    ? running.task.surface === "excel"
      ? renderExcelResultDetails(running.task.result ?? {}, running.task.state)
      : running.task.surface === "word"
        ? renderWordResultDetails(running.task.result ?? {}, running.task.state)
        : renderPdfResultDetails(running.task.result ?? {}, running.task.state)
    : "";
  const recovery = !terminal && running.task.surface === "word" ? renderWordRecoveryPanel() : "";
  const pdfStatus = !terminal && running.task.surface === "pdf" ? `${renderPdfRecoveryPanel()}${renderPdfReviewPanel()}` : "";
  const taskId = running.task.task_id;
  const controls = terminal
    ? `<div class="field-row" style="margin-top:10px"><button class="button" data-action="open-task-center" data-task-id="${escapeHtml(taskId)}">${icon("tasks", "small")}在任务中心查看</button><button class="button primary" data-action="reset-task" data-task-id="${escapeHtml(taskId)}">${icon("refresh", "small")}开始新任务</button></div>`
    : running.task.surface === "pdf" && running.task.state === "paused"
      ? `<div class="field-row" style="margin-top:10px"><button class="button primary" data-action="resume-pdf-task" data-task-id="${escapeHtml(taskId)}">${icon("refresh", "small")}继续翻译</button><button class="button danger" data-action="end-paused-pdf-task" data-task-id="${escapeHtml(taskId)}">${icon("stop", "small")}结束暂停</button></div><p class="note">结束暂停会保留已完成页面、页面素材、清单和报告，任务不能再次继续。</p>`
      : running.task.surface === "pdf"
        ? `<button class="button block large" style="margin-top:10px" data-action="pause-pdf-task" data-task-id="${escapeHtml(taskId)}">暂停提交</button>`
        : `<button class="button danger block large" style="margin-top:10px" data-action="stop-task" data-task-id="${escapeHtml(taskId)}">${icon("stop", "small")}安全停止</button>`;
  const streamNote = running.streamState === "reconnecting" ? `<p class="note">事件流暂时断开，正在从事件 ${running.lastEventId} 补拉，不会重复处理已有进度。</p>` : "";
  return `<div class="push"><div class="run-summary"><span>${escapeHtml(running.phaseName || (terminal ? resultMessage : "正在准备任务"))}</span><span>${terminal && running.task.state === "done" ? "100" : percent}%</span></div><div class="progress" style="--progress:${terminal && running.task.state === "done" ? 100 : percent}%"><i></i></div><div class="logbox">${logs || (terminal ? escapeHtml(resultMessage) : "等待引擎事件…")}</div>${streamNote}${recovery}${pdfStatus}${resultDetail}${controls}</div>`;
}

function redactedText(value: unknown, fallback = ""): string {
  const raw = text(value, fallback);
  if (!raw) return raw;
  return raw
    .replace(/(authorization\s*[:=]\s*)([^\s,;]+)/ig, "$1[redacted]")
    .replace(/\b(sk|rk|pk|api)[-_][a-z0-9_-]{8,}\b/ig, "[redacted]")
    .replace(/\bBearer\s+[^\s,;]+/ig, "Bearer [redacted]");
}

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
  add("打开输出目录", ["output_dir", "output_directory"], false);
  add("在 Finder 中显示主输出", ["output_path", "result_path", "output"], true);
  add("打开报告", ["report_path", "word_translation_report_path", "pdf_translation_report_path"], false);
  add("打开清单", ["manifest_path", "pdf_translation_manifest_path"], false);
  add("打开诊断", ["diagnostics_path", "diagnostic_path"], false);
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
  const outputPath = firstText({ ...snapshot, ...output, ...configuredOutput }, ["output_dir", "output_directory", "custom_output_dir"])
    || (configuredOutput.use_custom_output_dir === false ? "与源文件相邻的唯一输出目录" : "任务唯一输出目录");
  const throughput = record(snapshot.throughput);
  const connectionSummary = resources
    .map((group) => {
      const value = record(group);
      const summary = record(value.summary);
      return redactedText(value.label || value.connection_summary || value.id || [summary.provider || value.provider, summary.base_url || value.base_url].filter(Boolean).join(" @ "));
    })
    .filter(Boolean)
    .join("；");
  const rows: Array<[string, string]> = [
    ["语言", [sourceLang, targetLang].filter(Boolean).join(" → ")],
    ["模型角色", models],
    ["连接摘要", connectionSummary],
    ["领域 / Prompt", [domain, promptVersion].filter(Boolean).join(" · ")],
    ["输出位置", outputPath],
    ["吞吐", Object.keys(throughput).length ? Object.entries(throughput).map(([key, value]) => `${key} ${value}`).join("；") : Object.entries(modelSnapshot).map(([role, value]) => `${role} ${number(record(record(value).throughput).concurrency, 1)}`).join("；")],
  ];
  return rows.filter(([, value]) => Boolean(value));
}

function taskKpiRows(task: TaskStatus): Array<[string, string]> {
  const result = record(task.result);
  const summary = record(result.summary);
  const source = { ...summary, ...result };
  const pairs: Array<[string, string[], string]> = [
    ["已选", ["selected_count", "selected_file_count", "file_count", "total_files"], ""],
    ["成功", ["success_count", "successful_files", "succeeded_file_count", "completed_count"], ""],
    ["失败", ["failed_count", "failed_file_count", "error_count"], ""],
    ["未开始", ["unstarted_count", "unstarted_file_count", "not_started_count"], ""],
    ["耗时", ["elapsed_sec", "elapsed_seconds", "duration_seconds"], " 秒"],
  ];
  return pairs
    .map(([label, keys, suffix]) => {
      const value = firstNumber(source, keys);
      return value === null ? null : [label, `${value}${suffix}`] as [string, string];
    })
    .filter((item): item is [string, string] => item !== null);
}

function renderTaskCenter(): string {
  const active = activeTasks().length;
  const entries = state.taskOrder
    .map((id) => state.tasks[id])
    .filter((entry): entry is ManagedTask => Boolean(entry))
    .filter((entry) => state.taskCenterFilter === "all" || (state.taskCenterFilter === "active" ? isTaskActive(entry.task) : !isTaskActive(entry.task)));
  const cards = entries.map(renderTaskCard).join("");
  return `<section class="view active task-center"><div class="task-center-header card card-pad"><div><span class="section-label">统一任务中心</span><h2>活动任务 ${active}</h2><p class="note">事件按任务 ID 独立补拉。页面切换、重新聚焦和短断流不会停止其他任务。</p></div><div class="task-filter" role="group" aria-label="任务筛选"><button class="mini-button ${state.taskCenterFilter === "all" ? "active" : ""}" data-action="task-filter" data-filter="all">全部</button><button class="mini-button ${state.taskCenterFilter === "active" ? "active" : ""}" data-action="task-filter" data-filter="active">活动</button><button class="mini-button ${state.taskCenterFilter === "terminal" ? "active" : ""}" data-action="task-filter" data-filter="terminal">最近结果</button></div></div><div class="task-grid">${cards || `<div class="card card-pad muted">当前没有可显示的任务。启动 Excel、Word、PDF/图片或 TM 深度清洗后，状态会保留在这里。</div>`}</div></section>`;
}

function renderTaskCard(entry: ManagedTask): string {
  const { task } = entry;
  const stateMeta = taskStateMeta(task, entry.streamState);
  const percent = entry.stepTotal > 0 ? Math.round((entry.stepDone / entry.stepTotal) * 100) : 0;
  const references = taskResultReferences(task);
  const snapshots = taskSnapshotRows(task);
  const kpis = taskKpiRows(task);
  const logRows = entry.logs.map((item) => `<div class="log-${logTone(item.level)}">› ${escapeHtml(redactedText(item.message))}</div>`).join("");
  const taskId = task.task_id;
  const canOpenWorkspace = task.surface === "excel" || task.surface === "word" || task.surface === "pdf";
  const controls = task.terminal
    ? `<div class="task-card-actions">${canOpenWorkspace ? `<button class="mini-button" data-action="show-task-workspace" data-task-id="${escapeHtml(taskId)}">${icon("play", "small")}打开工作区</button>` : ""}${references.map((reference) => `<button class="mini-button" data-action="task-local-file" data-path="${escapeHtml(reference.path)}" data-reveal="${reference.reveal ? "1" : "0"}">${icon(reference.reveal ? "reveal" : "folder", "small")}${escapeHtml(reference.label)}</button><button class="mini-button" data-action="task-copy-path" data-path="${escapeHtml(reference.path)}" data-tip="复制此产物路径">${icon("copy", "small")}</button>`).join("")}</div>`
    : task.surface === "pdf" && task.state === "paused"
      ? `<div class="task-card-actions"><button class="mini-button primary" data-action="resume-pdf-task" data-task-id="${escapeHtml(taskId)}">继续</button><button class="mini-button danger-text" data-action="end-paused-pdf-task" data-task-id="${escapeHtml(taskId)}">结束暂停</button></div>`
      : task.surface === "pdf"
        ? `<div class="task-card-actions"><button class="mini-button" data-action="pause-pdf-task" data-task-id="${escapeHtml(taskId)}">暂停提交</button></div>`
        : `<div class="task-card-actions"><button class="mini-button danger-text" data-action="stop-task" data-task-id="${escapeHtml(taskId)}">安全停止</button></div>`;
  return `<article class="card task-card"><header><div><span class="section-label">${escapeHtml(taskSurfaceLabel(task.surface))}</span><h3>${escapeHtml(text(task.source_label, `任务 ${taskId.slice(0, 8)}`))}</h3></div><span class="status ${stateMeta.tone}"><span class="led"></span>${escapeHtml(stateMeta.label)}</span></header><div class="task-progress"><div class="run-summary"><span>${escapeHtml(entry.phaseName || "等待事件")}</span><span>${percent}%</span></div><div class="progress" style="--progress:${percent}%"><i></i></div></div>${snapshots.length ? `<dl class="task-snapshot">${snapshots.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`).join("")}</dl>` : ""}${kpis.length ? `<div class="result-kpis task-kpis">${kpis.map(([label, value]) => `<span><b>${escapeHtml(value)}</b>${escapeHtml(label)}</span>`).join("")}</div>` : ""}<details class="task-log" ${isTaskActive(task) ? "open" : ""}><summary>脱敏事件日志（${entry.logs.length}）</summary><div class="logbox">${logRows || "尚未收到可见事件。"}</div></details>${controls}</article>`;
}

function firstNumber(payload: JsonObject, keys: string[]): number | null {
  for (const key of keys) {
    if (typeof payload[key] === "number") return payload[key] as number;
  }
  return null;
}

function firstText(payload: JsonObject, keys: string[]): string {
  for (const key of keys) {
    const value = text(payload[key]);
    if (value) return value;
  }
  return "";
}

function resultEntries(result: JsonObject, keys: string[]): JsonObject[] {
  for (const key of keys) {
    const value = result[key];
    if (Array.isArray(value) && value.length) return value.map(record);
  }
  return [];
}

function renderExcelResultDetails(result: JsonObject, taskState = ""): string {
  const summary = record(result.summary);
  const kpi = record(result.kpi);
  const source = { ...result, ...summary, ...kpi };
  const files = resultEntries(result, ["files", "file_results", "file_records"]);
  const reviewPayload = record(result.review);
  const languagePayload = record(result.language);
  const reviews = resultEntries(result, ["review_items", "review_locations", "review_details"])
    .concat(resultEntries(reviewPayload, ["items", "locations", "details"]));
  const kpis: Array<[string, number | null]> = [
    ["已选", firstNumber(source, ["selected_count", "selected_files", "selected_file_count", "total_files"])],
    ["成功", firstNumber(source, ["success_count", "completed_count", "successful_files", "succeeded_file_count"])],
    ["失败", firstNumber(source, ["failed_count", "error_count", "failed_files", "failed_file_count"])],
    ["未开始", firstNumber(source, ["unstarted_count", "not_started_count", "unstarted_file_count"])],
    ["TM 命中", firstNumber(source, ["tm_hit_count", "tm_hits"])],
    ["送模型文本", firstNumber(source, ["model_translation_text_count", "model_text_count", "translated_text_count"])],
  ].filter((item): item is [string, number] => item[1] !== null);
  const outputPath = firstText(source, ["output_dir", "output_directory"]);
  const duration = firstNumber(source, ["duration_seconds", "elapsed_seconds", "elapsed_sec"]);
  const languages = resultEntries(result, ["language_preflights", "language_reports", "language_statistics"])
    .concat(resultEntries(languagePayload, ["files", "preflights", "reports", "statistics"]));
  const fileRows = files.map((entry) => {
    const sourcePath = firstText(entry, ["source_relative_path", "relative_path", "name"]);
    const format = firstText(entry, ["format", "source_format"]);
    const status = firstText(entry, ["status", "state", "terminal_state"]) || "结果未知";
    const output = firstText(entry, ["output_path", "result_path"]);
    const conversion = firstText(entry, ["conversion_method", "conversion", "conversion_mode"]);
    const reviewCount = firstNumber(entry, ["review_count", "review_items_count", "review_total"]);
    const error = firstText(entry, ["error", "error_message", "message"]);
    return `<tr><td>${escapeHtml(sourcePath)}</td><td>${escapeHtml(format)}</td><td>${escapeHtml(status)}</td><td>${escapeHtml(conversion || "—")}</td><td>${escapeHtml(output || "—")}</td><td>${reviewCount ?? 0}</td><td>${escapeHtml(error || "—")}</td></tr>`;
  }).join("");
  const reviewRows = reviews.slice(0, 30).map((entry) => `<tr><td>${escapeHtml(firstText(entry, ["file", "source_relative_path", "relative_path"]))}</td><td>${escapeHtml(firstText(entry, ["worksheet", "sheet", "sheet_name"]))}</td><td>${escapeHtml(firstText(entry, ["cell", "cell_reference", "location"]))}</td><td>${escapeHtml(firstText(entry, ["category", "mark", "type"]))}</td><td>${escapeHtml(firstText(entry, ["action", "applied_action", "message"]))}</td></tr>`).join("");
  const languageRows = languages.slice(0, 30).map((entry) => {
    const preflight = record(entry.preflight);
    const languages = strings(entry.detected_languages).concat(strings(preflight.source_langs));
    return `<li>${escapeHtml(firstText(entry, ["file", "source_relative_path", "relative_path", "path"]))}：${escapeHtml(languages.join(" / ") || firstText(entry, ["source_lang", "language"]) || "未返回")}</li>`;
  }).join("");
  const terminalLabel = taskState === "stopped" || result.stopped
    ? "用户中止（已完成产物已保留）"
    : taskState === "error"
      ? "任务失败（保留已知输出和文件结果）"
      : firstNumber(source, ["failed_file_count", "failed_count"]) ? "完成但部分文件失败" : "全部完成";
  return `<details class="excel-result-details" open><summary>Excel 结果详情${terminalLabel ? ` · ${escapeHtml(terminalLabel)}` : ""}</summary>${outputPath ? `<p class="result-output">输出目录：<code>${escapeHtml(outputPath)}</code>${duration !== null ? ` · 耗时 ${duration.toFixed(1)} 秒` : ""}</p>` : ""}${kpis.length ? `<div class="result-kpis">${kpis.map(([label, value]) => `<span><b>${value}</b>${label}</span>`).join("")}</div>` : ""}${files.length ? `<div class="result-table"><table><thead><tr><th>源相对路径</th><th>格式</th><th>状态</th><th>转换方式</th><th>输出</th><th>复核</th><th>错误原因</th></tr></thead><tbody>${fileRows}</tbody></table></div>` : `<p class="note">终态未返回文件明细；请查看结构化诊断记录定位文件级结果。</p>`}${languageRows ? `<details class="result-section"><summary>语言识别与实际语言统计</summary><ul>${languageRows}</ul></details>` : ""}${reviewRows ? `<details class="result-section"><summary>复核定位（${reviews.length}）</summary><div class="result-table"><table><thead><tr><th>文件</th><th>工作表</th><th>单元格</th><th>类别</th><th>采取动作</th></tr></thead><tbody>${reviewRows}</tbody></table></div></details>` : ""}</details>`;
}

function wordKpi(source: JsonObject, label: string, keys: string[]): [string, number | null] {
  return [label, firstNumber(source, keys)];
}

function renderWordRecoveryPanel(): string {
  const recovery = state.wordRecovery;
  const retryRound = firstNumber(recovery, ["retry_round"]);
  const retryTotal = firstNumber(recovery, ["retry_total"]);
  const cards: Array<[string, string]> = [
    ["严格重试", retryRound !== null || retryTotal !== null ? `${retryRound ?? 0} / ${retryTotal ?? 0} 轮` : "等待"],
    ["正在恢复", String(firstNumber(recovery, ["retry_processing_count", "processing_count"]) ?? 0)],
    ["已恢复", String(firstNumber(recovery, ["retry_recovered_count", "recovered_count"]) ?? 0)],
    ["未恢复", String(firstNumber(recovery, ["retry_unresolved_count", "unresolved_count"]) ?? 0)],
    ["仲裁处理中", String(firstNumber(recovery, ["semantic_processing_count"]) ?? 0)],
    ["仲裁已检查", String(firstNumber(recovery, ["semantic_checked_count"]) ?? 0)],
    ["仲裁已接受", String(firstNumber(recovery, ["semantic_accepted_count"]) ?? 0)],
    ["仲裁不确定", String(firstNumber(recovery, ["semantic_uncertain_count"]) ?? 0)],
  ];
  return `<details class="word-recovery" open><summary>恢复与语义仲裁</summary><div class="result-kpis">${cards.map(([label, value]) => `<span><b>${escapeHtml(value)}</b>${escapeHtml(label)}</span>`).join("")}</div><p class="note">严格重试只处理空译文、明显不完整或质量校验失败内容；语义仲裁接受的边界译文不会自动写入记忆库。</p></details>`;
}

function renderPdfRecoveryPanel(): string {
  const recovery = state.pdfPageRecovery;
  const total = firstNumber(recovery, ["total_pages"]);
  const completed = firstNumber(recovery, ["completed_pages"]);
  const submitted = firstNumber(recovery, ["submitted_page_count"]);
  const pending = firstNumber(recovery, ["pending_submitted_page_count"]);
  const cards: Array<[string, string]> = [
    ["页面进度", total !== null ? `${completed ?? 0} / ${total}` : "等待"],
    ["已提交", String(submitted ?? 0)],
    ["待收尾", String(pending ?? 0)],
    ["重试中", String(firstNumber(recovery, ["retrying_page_count"]) ?? 0)],
    ["已重试", String(firstNumber(recovery, ["retried_page_count"]) ?? 0)],
    ["已恢复", String(firstNumber(recovery, ["recovered_page_count"]) ?? 0)],
    ["失败占位", String(firstNumber(recovery, ["placeholder_page_count"]) ?? 0)],
  ];
  return `<details class="word-recovery pdf-recovery" open><summary>页面恢复状态</summary><div class="result-kpis">${cards.map(([label, value]) => `<span><b>${escapeHtml(value)}</b>${escapeHtml(label)}</span>`).join("")}</div><p class="note">暂停时不会再提交新页；“待收尾”归零后可继续同一任务，或结束暂停并写入部分结果证据。</p></details>`;
}

function renderPdfReviewPanel(): string {
  const review = state.pdfReview;
  if (!Object.keys(review).length && !pdfSettings().review_enabled) return "";
  const enabled = review.enabled === true || Boolean(pdfSettings().review_enabled);
  const cards: Array<[string, string]> = [
    ["审核", enabled ? "已开启" : "未开启"],
    ["当前轮次", String(firstNumber(review, ["review_round"]) ?? 0)],
    ["处理中", String(firstNumber(review, ["review_processing_count"]) ?? 0)],
    ["通过", String(firstNumber(review, ["review_passed_count"]) ?? 0)],
    ["未通过", String(firstNumber(review, ["review_failed_count"]) ?? 0)],
  ];
  return `<details class="word-recovery pdf-review-status" open><summary>逐页审核状态</summary><div class="result-kpis">${cards.map(([label, value]) => `<span><b>${escapeHtml(value)}</b>${escapeHtml(label)}</span>`).join("")}</div><p class="note">审核候选图与页面结论将随本次任务写入输出目录；模型响应正文不会显示或保存到界面结果。</p></details>`;
}

function renderPdfResultDetails(result: JsonObject, taskState = ""): string {
  const summary = record(result.summary);
  const source = { ...result, ...summary };
  const files = resultEntries(result, ["files", "file_results", "file_records"]);
  const outputPath = firstText(source, ["output_dir", "output_directory"]);
  const reportPath = firstText(source, ["report_path"]);
  const manifestPath = firstText(source, ["manifest_path"]);
  const duration = firstNumber(source, ["elapsed_sec", "elapsed_seconds", "duration_seconds"]);
  const terminalLabel = taskState === "stopped" || result.stopped
    ? "任务已停止，已完成页面和证据已保留"
    : taskState === "error"
      ? "任务失败，保留已知输出和诊断信息"
      : "任务已完成";
  const kpis: Array<[string, number | null]> = [
    ["文件", firstNumber(source, ["file_count", "selected_file_count"])],
    ["页 / 图片", firstNumber(source, ["total_page_count", "page_count"])],
    ["高清 PDF", firstNumber(source, ["generated_pdf_count"])],
    ["译图", firstNumber(source, ["generated_image_count"])],
    ["失败占位", firstNumber(source, ["placeholder_page_count"])],
    ["审核未通过", firstNumber(source, ["review_failed_page_count"])],
  ].filter((item): item is [string, number] => item[1] !== null);
  const rows = files.map((entry) => {
    const kind = firstText(entry, ["source_type"]) === "image" ? "图片" : "PDF";
    const status = firstText(entry, ["status", "state"]) || (entry.success === true ? "completed" : entry.success === false ? "failed" : "结果未知");
    const output = firstText(entry, ["output", "output_path", "translated_image_path"]);
    const compressed = firstText(entry, ["compressed_output"]);
    const review = [
      firstNumber(entry, ["reviewed_page_count"]),
      firstNumber(entry, ["review_passed_page_count"]),
      firstNumber(entry, ["review_failed_page_count"]),
    ].some((value) => value !== null)
      ? `${firstNumber(entry, ["reviewed_page_count"]) ?? 0} 审核 / ${firstNumber(entry, ["review_passed_page_count"]) ?? 0} 通过 / ${firstNumber(entry, ["review_failed_page_count"]) ?? 0} 未通过`
      : "—";
    const pageState = `${firstNumber(entry, ["page_count"]) ?? 0} 页；占位 ${firstNumber(entry, ["placeholder_page_count"]) ?? 0}；应急比例 ${firstNumber(entry, ["emergency_ratio_normalized_count"]) ?? 0}`;
    return `<tr><td>${escapeHtml(firstText(entry, ["name", "relative_path", "source_relative_path"]))}</td><td>${kind}</td><td>${escapeHtml(status)}</td><td>${escapeHtml(pageState)}</td><td>${escapeHtml(review)}</td><td>${escapeHtml(output || "—")}${compressed ? `<small class="file-location">压缩版：${escapeHtml(compressed)}</small>` : ""}</td><td>${escapeHtml(firstText(entry, ["error", "error_message", "detail"]) || "—")}</td></tr>`;
  }).join("");
  return `<details class="word-result-details pdf-result-details" open><summary>PDF / 图片结果详情 · ${escapeHtml(terminalLabel)}</summary>${outputPath ? `<p class="result-output">输出目录：<code>${escapeHtml(outputPath)}</code>${duration !== null ? ` · 耗时 ${duration.toFixed(1)} 秒` : ""}</p>` : ""}${reportPath ? `<p class="result-output">报告：<code>${escapeHtml(reportPath)}</code></p>` : ""}${manifestPath ? `<p class="result-output">清单：<code>${escapeHtml(manifestPath)}</code></p>` : ""}${kpis.length ? `<div class="result-kpis">${kpis.map(([label, value]) => `<span><b>${value}</b>${label}</span>`).join("")}</div>` : ""}${files.length ? `<div class="result-table"><table><thead><tr><th>源文件</th><th>类型</th><th>状态</th><th>页面结果</th><th>审核</th><th>输出</th><th>问题</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="note">终态未返回文件级清单；请查看输出目录中的清单与报告。</p>`}</details>`;
}

function renderWordResultDetails(result: JsonObject, taskState = ""): string {
  const summary = record(result.summary);
  const kpi = record(result.kpi);
  const recovery = record(result.recovery);
  const coverage = record(result.coverage);
  const reviewPayload = record(result.review);
  const languagePayload = record(result.language);
  const source = { ...result, ...summary, ...kpi };
  const files = resultEntries(result, ["files", "file_results", "file_records"]);
  const reviews = resultEntries(result, ["review_items", "review_locations", "review_details", "issues"])
    .concat(resultEntries(reviewPayload, ["items", "locations", "details", "issues"]));
  const languages = resultEntries(result, ["language_preflights", "language_reports", "language_statistics"])
    .concat(resultEntries(languagePayload, ["files", "preflights", "reports", "statistics"]));
  const kpis = [
    wordKpi(source, "已选", ["selected_count", "selected_files", "selected_file_count", "total_files"]),
    wordKpi(source, "成功", ["success_count", "completed_count", "successful_files", "succeeded_file_count"]),
    wordKpi(source, "失败", ["failed_count", "error_count", "failed_files", "failed_file_count"]),
    wordKpi(source, "未开始", ["unstarted_count", "not_started_count", "unstarted_file_count"]),
    wordKpi(source, "TM 命中", ["tm_hit_count", "tm_hits"]),
    wordKpi(source, "送模型文本", ["model_translation_text_count", "model_text_count", "translated_text_count", "api_call_count"]),
    wordKpi(source, "需复核", ["review_count", "review_items_count", "review_total", "review_text_count"]),
    wordKpi({ ...source, ...recovery }, "严格恢复", ["retry_recovered_count", "recovered_count"]),
    wordKpi({ ...source, ...recovery }, "仲裁接受", ["semantic_accepted_count"]),
  ].filter((item): item is [string, number] => item[1] !== null);
  const duration = firstNumber(source, ["duration_seconds", "elapsed_seconds", "elapsed_sec"]);
  const outputPath = firstText(source, ["output_dir", "output_directory"]);
  const reportPath = firstText(source, ["report_path", "word_translation_report_path"]);
  const reportWarning = firstText(source, ["report_warning", "report_error"]);
  const terminalLabel = taskState === "stopped" || result.stopped
    ? "用户中止（已完成产物已保留）"
    : taskState === "error"
      ? "任务失败（保留已知输出和文件结果）"
      : firstNumber(source, ["failed_file_count", "failed_count", "error_count"])
        ? "完成但部分文件失败"
        : "全部完成";
  const fileRows = files.map((entry) => {
    const preprocess = record(entry.preprocess);
    const conversionPayload = record(entry.conversion);
    const numberingPayload = record(entry.numbering);
    const sourcePath = firstText(entry, ["source_relative_path", "relative_path", "name"]);
    const format = firstText(entry, ["format", "source_format", "original_format"]);
    const status = firstText(entry, ["status", "state", "terminal_state"]) || (entry.success === true ? "成功" : entry.success === false ? "失败" : "结果未知");
    const output = firstText(entry, ["output_path", "result_path", "output"]);
    const conversion = firstText({ ...entry, ...preprocess, ...conversionPayload }, ["conversion_method", "conversion", "conversion_mode", "method"]);
    const conversionFidelity = firstText(conversionPayload, ["fidelity"]);
    const conversionFallback = strings(conversionPayload.fallback_messages).concat(strings(preprocess.conversion_fallback_messages)).join("；") || firstText({ ...entry, ...preprocess }, ["conversion_fallback_reason", "fallback_reason", "conversion_warning"]);
    const numbering = firstText({ ...entry, ...preprocess, ...numberingPayload }, ["numbering_method", "numbering_preprocess_method", "preprocess_method", "method"]);
    const numberingFallback = strings(numberingPayload.fallback_messages).concat(strings(preprocess.numbering_fallback_messages)).join("；") || firstText({ ...entry, ...preprocess }, ["numbering_fallback_reason", "numbering_warning"]);
    const labelsFound = firstNumber({ ...entry, ...preprocess, ...numberingPayload }, ["numbering_found_count", "labels_seen", "numbering_labels_seen"]);
    const labelsMaterialized = firstNumber({ ...entry, ...preprocess, ...numberingPayload }, ["numbering_materialized_count", "labels_prepended", "numbering_labels_prepended", "labels_materialized"]);
    const error = firstText(entry, ["error", "error_message", "message"]);
    const conversionDetail = [conversion, conversionFidelity && `保真：${conversionFidelity}`, conversionFallback && `回退：${conversionFallback}`].filter(Boolean).join("；") || "—";
    const numberingDetail = [numbering, numberingFallback && `回退：${numberingFallback}`, labelsFound !== null ? `发现 ${labelsFound}` : "", labelsMaterialized !== null ? `物化 ${labelsMaterialized}` : ""].filter(Boolean).join("；") || "—";
    return `<tr><td>${escapeHtml(sourcePath)}</td><td>${escapeHtml(format || "—")}</td><td>${escapeHtml(status)}</td><td>${escapeHtml(conversionDetail)}</td><td>${escapeHtml(numberingDetail)}</td><td>${escapeHtml(output || "—")}</td><td>${escapeHtml(error || "—")}</td></tr>`;
  }).join("");
  const reviewRows = reviews.slice(0, 50).map((entry) => {
    const excerpt = firstText(entry, ["excerpt", "snippet", "source_excerpt", "text", "source_text"]);
    const boundedExcerpt = excerpt.length > 160 ? `${excerpt.slice(0, 157)}…` : excerpt;
    return `<tr><td>${escapeHtml(firstText(entry, ["file", "source_relative_path", "relative_path", "path"]))}</td><td>${escapeHtml(firstText(entry, ["section", "chapter", "section_path", "heading"]))}</td><td>${escapeHtml(firstText(entry, ["location", "paragraph", "paragraph_index", "cell", "table_cell"]))}</td><td>${escapeHtml(boundedExcerpt || "—")}</td><td>${escapeHtml(firstText(entry, ["issue", "category", "mark", "type", "problem"]) || "—")}</td><td>${escapeHtml(firstText(entry, ["action", "applied_action", "review_status", "message"]) || "—")}</td></tr>`;
  }).join("");
  const languageRows = languages.slice(0, 50).map((entry) => {
    const preflight = record(entry.preflight);
    const detected = strings(entry.detected_languages).concat(strings(preflight.source_langs));
    const actual = record(entry.actual_source_counts);
    const actualLabel = Object.keys(actual).length ? Object.entries(actual).map(([language, count]) => `${language} ${count}`).join("；") : firstText(entry, ["actual_source_lang", "source_lang", "language"]);
    return `<li>${escapeHtml(firstText(entry, ["file", "source_relative_path", "relative_path", "path", "name"]))}：预检 ${escapeHtml(detected.join(" / ") || firstText(entry, ["source_lang", "language"]) || "未返回")}；实际 ${escapeHtml(actualLabel || "未返回")}</li>`;
  }).join("");
  const recoveryRows = [
    ["严格重试", firstNumber({ ...source, ...recovery }, ["retry_round"]), firstNumber({ ...source, ...recovery }, ["retry_total"])],
    ["已恢复", firstNumber({ ...source, ...recovery }, ["retry_recovered_count", "recovered_count"]), firstNumber({ ...source, ...recovery }, ["retry_unresolved_count", "unresolved_count"])],
    ["语义仲裁", firstNumber({ ...source, ...recovery }, ["semantic_accepted_count"]), firstNumber({ ...source, ...recovery }, ["semantic_uncertain_count", "semantic_unaccepted_count"])],
  ].filter(([, first, second]) => first !== null || second !== null).map(([label, first, second]) => `<li>${escapeHtml(label)}：${first ?? 0} / ${second ?? 0}</li>`).join("");
  const coverageRows = Object.entries(coverage).filter(([, value]) => typeof value === "number").map(([key, value]) => `<li>${escapeHtml(key)}：${escapeHtml(String(value))}</li>`).join("");
  return `<details class="word-result-details" open><summary>Word 结果详情 · ${escapeHtml(terminalLabel)}</summary>${outputPath ? `<p class="result-output">输出目录：<code>${escapeHtml(outputPath)}</code>${duration !== null ? ` · 耗时 ${duration.toFixed(1)} 秒` : ""}</p>` : ""}${reportPath ? `<p class="result-output">质量报告：<code>${escapeHtml(reportPath)}</code></p>` : reportWarning ? `<p class="note">质量报告未能写入：${escapeHtml(reportWarning)}（不影响已生成的 Word 文件）</p>` : ""}${kpis.length ? `<div class="result-kpis">${kpis.map(([label, value]) => `<span><b>${value}</b>${label}</span>`).join("")}</div>` : ""}${files.length ? `<div class="result-table"><table><thead><tr><th>源相对路径</th><th>原格式</th><th>状态</th><th>.doc 转换</th><th>编号预处理</th><th>输出</th><th>错误原因</th></tr></thead><tbody>${fileRows}</tbody></table></div>` : `<p class="note">终态未返回文件明细；请查看结构化诊断记录定位文件级结果。</p>`}${languageRows ? `<details class="result-section"><summary>语言预检与实际语言统计</summary><ul>${languageRows}</ul></details>` : ""}${recoveryRows ? `<details class="result-section"><summary>恢复与语义仲裁汇总</summary><ul>${recoveryRows}</ul></details>` : ""}${coverageRows ? `<details class="result-section"><summary>补译覆盖与封面保护</summary><ul>${coverageRows}</ul></details>` : ""}${reviewRows ? `<details class="result-section"><summary>质量问题定位（${reviews.length}）</summary><div class="result-table"><table><thead><tr><th>文件</th><th>章节</th><th>位置</th><th>短摘录</th><th>问题</th><th>动作 / 复核</th></tr></thead><tbody>${reviewRows}</tbody></table></div></details>` : ""}</details>`;
}

function renderFiles(files: FileItem[], surface: Surface, selectedPaths: string[], disabled: boolean): string {
  if (!files.length) {
    return `<div class="card-pad muted">选择源路径后点击“扫描”，这里会显示可处理文件。</div>`;
  }
  if (surface === "excel") {
    return `<table class="excel-file-table"><thead><tr><th class="selection-column">选择</th><th>文件与相对位置</th><th class="number">大小</th><th>格式</th><th class="number">工作表</th><th>预检/执行状态</th></tr></thead><tbody>${files.map((file) => {
      const progress = record(state.excelFileProgress[file.path]);
      const status = text(progress.status) || text(progress.stage) || text(progress.phase_name) || "待启动";
      const statusTone = /失败|错误|error|failed/i.test(status) ? "error" : /完成|成功|done|translated/i.test(status) ? "ok" : /预检|处理|转换|翻译|写入|running/i.test(status) ? "running" : "";
      const risks = [...new Set([
        ...strings(file.conversion_risks),
        ...strings(file.risk_flags),
        ...strings(progress.risks),
        text(record(file.risk).message),
      ].filter(Boolean))];
      return `<tr><td><input class="file-check" type="checkbox" data-file-path="${escapeHtml(file.path)}" data-surface="${surface}" ${selectedPaths.includes(file.path) ? "checked" : ""} ${disabled ? "disabled" : ""}/></td><td><span class="file-name">${icon("excel", "small")}${escapeHtml(file.name)}</span><small class="file-location">${escapeHtml(displayPath(file))}</small>${risks.length ? `<small class="file-risk">${icon("warn", "small")}${escapeHtml(risks.join("；"))}</small>` : ""}</td><td class="number">${number(file.size_kb).toFixed(1)} KB</td><td><span class="format-pill ${fileFormat(file) === "xls" ? "legacy" : ""}">.${escapeHtml(fileFormat(file) || "xlsx")}</span></td><td class="number">${excelSheetCount(file)}</td><td><span class="file-status ${statusTone}">${escapeHtml(status)}</span></td></tr>`;
    }).join("")}</tbody></table>`;
  }
  if (surface === "word") {
    return `<table class="word-file-table"><thead><tr><th class="selection-column">选择</th><th>文件与相对位置</th><th class="number">大小</th><th>格式</th><th class="number">正文段落</th><th class="number">表格</th><th>预检/执行状态</th></tr></thead><tbody>${files.map((file) => {
      const progress = record(state.wordFileProgress[file.path]);
      const status = text(progress.status) || text(progress.stage) || text(progress.phase_name) || "待启动";
      const statusTone = /失败|错误|error|failed|未恢复/i.test(status)
        ? "error"
        : /完成|成功|done|translated|已恢复/i.test(status)
          ? "ok"
          : /预检|处理|转换|翻译|写入|编号|恢复|仲裁|running/i.test(status)
            ? "running"
            : "";
      const conversionPending = wordNeedsConversion(file);
      const risks = [...new Set([
        ...strings(file.conversion_risks),
        ...strings(file.risk_flags),
        ...strings(progress.risks),
        text(record(file.risk).message),
        conversionPending ? ".doc 需在执行时转换后统计" : "",
      ].filter(Boolean))];
      const format = wordFileFormat(file);
      return `<tr><td><input class="file-check" type="checkbox" data-file-path="${escapeHtml(file.path)}" data-surface="${surface}" ${selectedPaths.includes(file.path) ? "checked" : ""} ${disabled ? "disabled" : ""}/></td><td><span class="file-name">${icon("word", "small")}${escapeHtml(file.name)}</span><small class="file-location">${escapeHtml(displayPath(file))}</small>${risks.length ? `<small class="file-risk">${icon("warn", "small")}${escapeHtml(risks.join("；"))}</small>` : ""}</td><td class="number">${number(file.size_kb).toFixed(1)} KB</td><td><span class="format-pill ${conversionPending ? "legacy" : ""}">.${escapeHtml(format)}${conversionPending ? " · 需转换" : ""}</span></td><td class="number">${conversionPending ? "转换后统计" : number(file.paragraph_count)}</td><td class="number">${conversionPending ? "转换后统计" : number(file.table_count)}</td><td><span class="file-status ${statusTone}">${escapeHtml(status)}</span></td></tr>`;
    }).join("")}</tbody></table>`;
  }
  return `<table class="pdf-file-table"><thead><tr><th class="selection-column">选择</th><th>文件与相对位置</th><th>类型</th><th class="number">大小</th><th class="number">页 / 图片</th><th>尺寸</th><th>执行状态</th></tr></thead><tbody>${files.map((file) => {
    const progress = record(state.pdfFileProgress[file.path]);
    const sourceType = pdfFileType(file);
    const status = text(progress.status) || text(progress.stage) || text(progress.phase_name) || "待启动";
    const statusTone = /失败|错误|error|failed/i.test(status)
      ? "error"
      : /完成|成功|done|translated|completed/i.test(status)
        ? "ok"
        : /预处理|处理|翻译|审核|写入|running/i.test(status)
          ? "running"
          : "";
    const sourceLabel = sourceType === "image" ? "图片" : "PDF";
    const size = sourceType === "image" ? pdfImageDimensions(file) : "—";
    return `<tr><td><input class="file-check" type="checkbox" data-file-path="${escapeHtml(file.path)}" data-surface="${surface}" ${selectedPaths.includes(file.path) ? "checked" : ""} ${disabled ? "disabled" : ""}/></td><td><span class="file-name">${icon(sourceType === "image" ? "file" : "pdf", "small")}${escapeHtml(file.name)}</span><small class="file-location">${escapeHtml(displayPath(file))}</small></td><td><span class="format-pill">${sourceLabel} · .${escapeHtml(fileFormat(file) || (sourceType === "image" ? "png" : "pdf"))}</span></td><td class="number">${number(file.size_kb).toFixed(1)} KB</td><td class="number">${sourceType === "image" ? "1 图" : `${pdfPageUnits(file)} 页`}</td><td>${escapeHtml(size)}</td><td><span class="file-status ${statusTone}">${escapeHtml(status)}</span></td></tr>`;
  }).join("")}</tbody></table>`;
}

function renderExcelSkippedItems(): string {
  const report = state.scanReports.excel;
  const skipped = report.skipped;
  if (!skipped.length) return "";
  const rows = skipped.map((item) => {
    const path = text(item.relative_path) || text(item.path) || text(item.name, "未命名文件");
    return `<li><span>${escapeHtml(path)}</span><small>${escapeHtml(text(item.reason, "扫描时无法读取"))}</small></li>`;
  }).join("");
  return `<details class="scan-skipped"><summary>${icon("warn", "small")}跳过项目（${skipped.length}）</summary><ul>${rows}</ul></details>`;
}

function renderWordSkippedItems(): string {
  const report = state.scanReports.word;
  const skipped = report.skipped;
  if (!skipped.length) return "";
  const rows = skipped.map((item) => {
    const path = text(item.relative_path) || text(item.path) || text(item.name, "未命名文件");
    return `<li><span>${escapeHtml(path)}</span><small>${escapeHtml(text(item.reason, "扫描时无法读取"))}</small></li>`;
  }).join("");
  return `<details class="scan-skipped"><summary>${icon("warn", "small")}跳过项目（${skipped.length}）</summary><ul>${rows}</ul></details>`;
}

function renderPdfScanReport(): string {
  const report = state.scanReports.pdf;
  const summary = record(report.summary);
  const risk = record(report.risk);
  const skipped = report.skipped;
  const pdfs = firstNumber(summary, ["pdf_count"]) ?? pdfScanSummary().pdfs;
  const images = firstNumber(summary, ["image_count"]) ?? pdfScanSummary().images;
  const units = firstNumber(summary, ["total_page_or_image_count"]) ?? pdfScanSummary().units;
  const includeImages = typeof risk.include_images === "boolean"
    ? risk.include_images
    : pdfIncludeImagesEnabled();
  const skippedRows = skipped.map((item) => {
    const path = text(item.relative_path) || text(item.path) || text(item.name, "未命名文件");
    return `<li><span>${escapeHtml(path)}</span><small>${escapeHtml(text(item.reason, "扫描时无法读取"))}</small></li>`;
  }).join("");
  const riskMessage = text(risk.message);
  const mixed = risk.mixed_input_supported === true;
  return `${state.files.pdf.length || skipped.length ? `<details class="word-recovery pdf-scan-overview" open><summary>PDF / 图片扫描概况</summary><div class="result-kpis"><span><b>${pdfs}</b>PDF</span><span><b>${images}</b>独立图片</span><span><b>${units}</b>页 / 图片</span><span><b>${skipped.length}</b>跳过</span></div><p class="note">独立图片输入：${includeImages ? "已开启" : "未开启"}；${mixed ? "当前清单包含 PDF 与图片，会在同一任务中按各自输出规则处理。" : "PDF 与独立图片均可扫描，图片需在更多参数中开启。"}</p>${riskMessage ? `<p class="note">${escapeHtml(riskMessage)}</p>` : ""}</details>` : ""}${skipped.length ? `<details class="scan-skipped" open><summary>${icon("warn", "small")}PDF / 图片跳过项目（${skipped.length}）</summary><ul>${skippedRows}</ul></details>` : ""}`;
}

function renderExcelStartPreflight(source: string, target: string): string {
  const xlsCount = selectedExcelXlsCount();
  const manualSameLanguage = source !== "auto" && source === target;
  const output = excelOutputSettings();
  const outputState = Boolean(output.use_custom_output_dir)
    ? state.excelOutputInspection.message
    : "将在每次任务的源目录根下创建唯一时间戳输出子目录，不会覆盖源文件或历史结果。";
  return `<details class="excel-preflight" open><summary>启动前检查</summary><ul>
    <li class="${manualSameLanguage ? "error" : ""}">${manualSameLanguage ? "源语言与目标语言相同，不能启动翻译。" : source === "auto" ? "自动模式会对每个有候选文本的文件单独抽样预检一次；仅发送去重的代表性文本，不上传完整工作簿。" : "手动源语言不发送预检，并以当前选择作为 TM 查询和正常自动入库的权威语言。"}</li>
    <li>${escapeHtml(outputState)}</li>
    ${xlsCount ? `<li class="warn">已选 ${xlsCount} 个 .xls：启动时会优先使用 Excel 自动化高保真转换；若不可用，必须由你明确确认兼容转换，绝不会静默降级。</li>` : ""}
    <li>模型未测试只会显示提醒，不阻断专业用户启动；无效语言对、输出目录或模型基本配置会在任务创建前阻止请求。</li>
  </ul></details>`;
}

function renderWordStartPreflight(source: string, target: string): string {
  const docCount = selectedWordDocCount();
  const manualSameLanguage = source !== "auto" && source === target;
  const output = wordOutputSettings();
  const outputState = Boolean(output.use_custom_output_dir)
    ? state.wordOutputInspection.message
    : "将在每次任务的源目录根下创建唯一时间戳输出子目录，不会覆盖源文件或历史结果。";
  return `<details class="word-preflight" open><summary>启动前检查</summary><ul>
    <li class="${manualSameLanguage ? "error" : ""}">${manualSameLanguage ? "源语言与目标语言相同，不能启动翻译。" : source === "auto" ? "自动模式会在每个有候选文本的 Word 文件开始前发送一次代表性文本预检；不上传完整文档，预检语言会作为 TM 查询与自动入库边界。" : "手动源语言不发送预检，并以当前选择作为 TM 查询和正常自动入库的权威语言。"}</li>
    <li>${escapeHtml(outputState)}</li>
    ${docCount ? `<li class="warn">已选 ${docCount} 个 .doc：最终产物始终为 .docx。开始前必须明确选择“优先高保真”或“允许兼容转换”；选择高保真后，单文件失败不会静默降级。</li>` : ""}
    <li>模型未测试只会显示提醒，不阻断专业用户启动；无效语言对、输出目录或模型基本配置会在任务创建前阻止请求。</li>
  </ul></details>`;
}

function renderPdfStartPreflight(): string {
  const output = pdfOutputSettings();
  const reviewEnabled = Boolean(pdfSettings().review_enabled);
  const outputState = Boolean(output.use_custom_output_dir)
    ? state.pdfOutputInspection.message
    : "将在源目录根下创建唯一时间戳输出子目录，不会覆盖源文件或历史结果。";
  const reviewRole = record(state.modelRoles.pdf_review);
  const availability = text(reviewRole.availability_status, "unknown");
  const reviewWarning = reviewEnabled && availability === "unavailable"
    ? "审核模型当前配置已测试失败；开始时必须明确确认继续，或关闭审核。"
    : reviewEnabled
      ? "逐页审核已开启，审核模型的配置、连接状态与本次任务一同冻结。"
      : "逐页审核未开启。";
  return `<details class="word-preflight pdf-preflight" open><summary>启动前检查</summary><ul><li>PDF/图片不读写记忆库；页图翻译由模型识别原文，目标语言、输出策略与模型配置均在启动时冻结。</li><li>${escapeHtml(outputState)}</li><li class="${reviewEnabled && availability === "unavailable" ? "warn" : ""}">${escapeHtml(reviewWarning)}</li><li>暂停只停止提交新页面，已提交页面会安全收尾；可继续同一任务，或结束暂停并保留素材、清单和报告。</li></ul></details>`;
}

function renderTmView(): string {
  const stats = state.tmStats;
  const totalPages = Math.max(1, Math.ceil(state.tmTotal / state.tmPageSize));
  const customTarget = tmTargetIsCustom();
  const cleaningState = state.tmCleaningState === "running"
    ? `<div class="tm-state running"><span class="led"></span>正在分析未固定条目…</div>`
    : state.tmCleaningState === "ready"
      ? `<div class="tm-state ready"><span class="led"></span>清洗建议已生成，请复核后写入。</div>`
      : state.tmCleaningState === "error"
        ? `<div class="tm-state error"><span class="led"></span>清洗失败，请检查模型连接后重试。</div>`
        : "";
  const conflictState = state.tmConflictMessage
    ? `<div class="tm-state error"><span class="led"></span>${escapeHtml(state.tmConflictMessage)}<button class="mini-button" data-action="tm-clear-conflict">关闭</button></div>`
    : "";
  const conflictRows = state.tmConflicts.map((item) => `<div class="tm-conflict-row"><div><strong>${escapeHtml(item.source_text)}</strong><span class="muted">${escapeHtml(item.lang_pair)} · ${escapeHtml(item.existing_target)} → ${escapeHtml(item.candidate_target)}</span></div><span class="table-actions"><button class="mini-button" data-action="tm-conflict-resolve" data-conflict-id="${item.id}" data-conflict-resolution="keep_existing">保留当前</button><button class="mini-button primary" data-action="tm-conflict-resolve" data-conflict-id="${item.id}" data-conflict-resolution="use_candidate">采用候选</button><button class="mini-button danger-text" data-action="tm-conflict-resolve" data-conflict-id="${item.id}" data-conflict-resolution="reject">拒绝</button></span></div>`).join("");
  return `<section class="view active"><div class="left-column">
    <div class="card tm-controls">
      <div class="tm-pair-row"><div class="tm-pair-field"><label class="field-label" for="tmSourceLang">源语言</label><select id="tmSourceLang" data-tm-source-lang>${tmSourceLanguageOptions()}</select></div><div class="tm-pair-arrow" aria-hidden="true">→</div><div class="tm-pair-field"><label class="field-label" for="tmTargetLang">目标语言</label><select id="tmTargetLang" data-tm-target-lang>${tmTargetLanguageOptions()}</select></div><div class="tm-pair-current"><span class="field-label">当前语言对</span><strong>${escapeHtml(tmPairLabel(tmLangPair()))}</strong>${customTarget ? `<small>自定义目标语言</small>` : ""}</div></div>
      ${state.tmRecentPairs.length ? `<div class="tm-recent-row"><span class="field-label">最近使用</span>${state.tmRecentPairs.slice(0, 8).map((pair) => `<button class="mini-button ${pair === tmLangPair() ? "active" : ""}" data-action="tm-recent-pair" data-pair="${escapeHtml(pair)}">${escapeHtml(tmPairLabel(pair))}</button>`).join("")}</div>` : ""}
      <div class="tm-search-row"><div class="source-icon">${icon("search")}</div><input class="source-input" id="tmKeyword" value="${escapeHtml(state.tmKeyword)}" placeholder="按原文或译文筛选" /><button class="button" data-action="tm-search">搜索</button><button class="button" data-action="tm-add">${icon("plus", "small")}新增</button><button class="button" data-action="tm-import">导入</button><button class="button" data-action="tm-export-json">导出 JSON</button><button class="button" data-action="tm-export-csv">导出 CSV</button><button class="button" data-action="tm-export-full">导出全库</button><button class="button" data-action="tm-import-full">恢复全库</button><button class="button primary" data-action="tm-clean" ${state.tmCleaningState === "running" ? "disabled" : ""}>${icon("sparkle", "small")}深度清洗</button></div>
      ${cleaningState}${conflictState}${state.tmConflicts.length ? `<div class="tm-conflicts"><div class="tm-conflicts-header"><strong>待裁决冲突 ${state.tmConflicts.length}</strong><button class="mini-button" data-action="tm-refresh-conflicts">刷新</button></div>${conflictRows}</div>` : ""}
    </div>
    <div class="stats">${stat("memory", "总条目", String(number(stats.total)))}${stat("pin", "已固定", String(number(stats.pinned)))}${stat("check", "手动维护", String(number(stats.manual)))}${stat("translate", "未固定", String(number(stats.unpinned)))}</div>
    <div class="card table-card"><div class="table-header"><h2>记忆条目</h2><span class="table-count">${state.tmTotal} 条 · 第 ${state.tmPage} / ${totalPages} 页</span><span class="header-spacer"></span><button class="mini-button" data-action="tm-select-page">${state.tmSelectedIds.length === state.tmEntries.length && state.tmEntries.length ? "取消全选" : "全选本页"}</button><button class="mini-button" data-action="tm-bulk-pin" ${state.tmSelectedIds.length ? "" : "disabled"}>固定</button><button class="mini-button" data-action="tm-bulk-unpin" ${state.tmSelectedIds.length ? "" : "disabled"}>解除固定</button><button class="mini-button danger-text" data-action="tm-bulk-delete" ${state.tmSelectedIds.length ? "" : "disabled"}>删除</button></div><div class="table-scroll">${renderTmEntries()}</div><div class="tm-pagination"><span class="muted">已选 ${state.tmSelectedIds.length} 条</span><span class="header-spacer"></span><select class="tm-page-size" data-tm-page-size aria-label="每页条数"><option value="25" ${state.tmPageSize === 25 ? "selected" : ""}>25 / 页</option><option value="50" ${state.tmPageSize === 50 ? "selected" : ""}>50 / 页</option><option value="100" ${state.tmPageSize === 100 ? "selected" : ""}>100 / 页</option></select><button class="mini-button" data-action="tm-page-prev" ${state.tmPage <= 1 ? "disabled" : ""}>上一页</button><button class="mini-button" data-action="tm-page-next" ${state.tmPage >= totalPages ? "disabled" : ""}>下一页</button></div></div>
  </div></section>`;
}

function renderTmEntries(): string {
  if (!state.tmEntries.length) {
    return `<div class="card-pad muted">当前语言对没有记忆条目。</div>`;
  }
  return `<table><thead><tr><th class="selection-column"><input type="checkbox" class="file-check" data-tm-select-page ${state.tmSelectedIds.length === state.tmEntries.length ? "checked" : ""} aria-label="选择本页" /></th><th>原文</th><th>译文</th><th>来源</th><th class="number">操作</th></tr></thead><tbody>${state.tmEntries.map((entry) => `<tr><td><input type="checkbox" class="file-check" data-tm-entry-id="${entry.id}" ${state.tmSelectedIds.includes(entry.id) ? "checked" : ""} aria-label="选择 ${escapeHtml(entry.source_text)}" /></td><td>${escapeHtml(entry.source_text)}</td><td>${escapeHtml(entry.target_text)}</td><td class="muted">${escapeHtml(entry.word_type)}</td><td class="number"><span class="table-actions"><button class="mini-button" data-action="tm-edit" data-entry-id="${entry.id}">编辑</button><button class="pin-button ${entry.pinned ? "pinned" : ""}" data-action="tm-pin" data-entry-id="${entry.id}" data-pinned="${entry.pinned ? "0" : "1"}" data-tip="${entry.pinned ? "解除固定" : "固定词条"}">${icon("pin", "small")}</button><button class="mini-button danger-text" data-action="tm-delete" data-entry-id="${entry.id}">删除</button></span></td></tr>`).join("")}</tbody></table>`;
}

function renderModal(): string {
  if (state.modal === "custom-language") {
    const editing = state.customLanguageEditing;
    return `<div class="modal-backdrop"><section class="modal"><h2>${editing ? "编辑自定义语言" : "新增自定义目标语言"}</h2><p class="note">自定义语言只能作为目标语言使用；内部代码创建后不可变。</p><label class="field-label" for="customLanguageName">显示名称</label><input id="customLanguageName" value="${escapeHtml(editing?.display_name)}" ${editing ? "disabled" : ""} autofocus/><label class="field-label" for="customLanguageDescription" style="margin-top:10px">语言说明</label><textarea id="customLanguageDescription">${escapeHtml(editing?.description)}</textarea><div class="modal-actions"><button class="button" data-action="close-modal">取消</button>${editing ? `<button class="button danger" data-action="custom-language-delete">删除</button>` : ""}<button class="button primary" data-action="${editing ? "custom-language-update" : "custom-language-create"}">保存</button></div></section></div>`;
  }
  if (state.modal === "tm-add" || state.modal === "tm-edit") {
    const editing = state.tmEditing;
    const isEdit = state.modal === "tm-edit";
    const reverseAllowed = tmPairAllowsReverse();
    return `<div class="modal-backdrop"><section class="modal"><h2>${isEdit ? "编辑记忆条目" : "新增记忆条目"}</h2><label class="field-label" for="tmSource">原文</label><input id="tmSource" autofocus value="${escapeHtml(editing?.source_text)}" /><label class="field-label" for="tmTarget" style="margin-top:10px">译文</label><input id="tmTarget" value="${escapeHtml(editing?.target_text)}" /><label class="field-label" for="tmPair" style="margin-top:10px">语言对</label><input id="tmPair" value="${escapeHtml(tmLangPair())}" ${isEdit ? "disabled" : ""}/><div class="toggle-row" style="margin-top:10px"><input id="tmSyncReverse" type="checkbox" ${reverseAllowed ? "" : "disabled"}/><label for="tmSyncReverse">同时创建/更新反向术语（默认关闭）</label></div>${reverseAllowed ? "" : `<p class="note">自定义目标语言不能生成反向语言对。</p>`}<div class="modal-actions"><button class="button" data-action="close-modal">取消</button><button class="button primary" data-action="${isEdit ? "tm-update" : "tm-create"}">保存</button></div></section></div>`;
  }
  if (state.modal === "tm-delete" && state.tmEditing) {
    return `<div class="modal-backdrop"><section class="modal"><h2>删除记忆条目</h2><p class="note">将永久删除“${escapeHtml(state.tmEditing.source_text)}”。此操作不能撤销。</p><div class="modal-actions"><button class="button" data-action="close-modal">取消</button><button class="button danger" data-action="tm-delete-confirm">删除</button></div></section></div>`;
  }
  if (state.modal === "source-picker" && state.sourcePickerSurface) {
    const surface = state.sourcePickerSurface;
    return `<div class="modal-backdrop"><section class="modal"><h2>选择源路径</h2><p class="note">可扫描整个文件夹，或仅处理单个 ${surfaceLabel(surface)} 文件。</p><div class="modal-actions modal-actions-spread"><button class="button" data-action="close-modal">取消</button><span><button class="button" data-action="choose-source-file" data-surface="${surface}">选择单个文件</button><button class="button primary" data-action="choose-source-folder" data-surface="${surface}">选择文件夹</button></span></div></section></div>`;
  }
  if (state.modal === "task-risk" && state.pendingTaskRisk) {
    const { preflight, surface } = state.pendingTaskRisk;
    const risk = record(preflight.risk);
    const sharedConnections = Array.isArray(risk.shared_connections) ? risk.shared_connections.map(record) : [];
    const active = Array.isArray(risk.active_tasks) ? risk.active_tasks.map(record) : [];
    const warnings = strings(risk.warnings);
    const candidate = record(preflight.candidate_snapshot);
    const connectionRows = sharedConnections.map((item) => {
      const summary = record(item.summary);
      const label = redactedText(item.label || item.connection_summary || item.resource_group || [summary.provider, summary.base_url].filter(Boolean).join(" @ "), "共享连接");
      return `<li><strong>${escapeHtml(label)}</strong> · 角色 ${escapeHtml(strings(item.roles).join("、") || "未返回")} · 活动 / 新任务并发 ${escapeHtml(String(number(item.active_concurrency, 0)))} / ${escapeHtml(String(number(item.candidate_concurrency, 0)))} · 合计潜在 ${escapeHtml(String(number(item.total_potential_concurrency, number(item.potential_concurrency, 0))))}</li>`;
    }).join("");
    const activeRows = active.map((item) => `<li>${escapeHtml(taskSurfaceLabel(text(item.surface, "excel") as TaskSurface))} · ${escapeHtml(redactedText(item.source_label || item.task_label || item.task_id, "活动任务"))} · 并发 ${escapeHtml(String(number(item.concurrency, 0)))}</li>`).join("");
    const candidateRows = [
      ["任务类型", taskSurfaceLabel(surface)],
      ["目标语言", firstText(candidate, ["target_lang"])],
      ["冻结吞吐", Object.entries(record(candidate.throughput)).map(([key, value]) => `${key} ${value}`).join("；")],
    ].filter(([, value]) => Boolean(value));
    return `<div class="modal-backdrop"><section class="modal wide-modal task-risk-modal"><h2>共享 API 并行风险</h2><p class="note">此任务将与现有活动任务共用至少一个实际 API 连接。继续后会按新任务自己的默认吞吐启动，不会自动减半；服务端会在启动时用一次性令牌原子复检。</p><div class="risk-callout"><strong>${icon("warn", "small")}可能出现 429、排队、超时、失败或额外费用</strong><p>同一连接的并发会累加。上游返回并发限制时，只会降低当前共享组的运行时容量，不会修改长期模型吞吐设置。</p></div>${connectionRows ? `<div class="task-risk-section"><h3>共同连接与预算</h3><ul>${connectionRows}</ul></div>` : ""}${activeRows ? `<div class="task-risk-section"><h3>活动任务</h3><ul>${activeRows}</ul></div>` : ""}${candidateRows.length ? `<dl class="task-snapshot">${candidateRows.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(String(value))}</dd>`).join("")}</dl>` : ""}${warnings.length ? `<div class="task-risk-section"><h3>额外提示</h3><ul>${warnings.map((warning) => `<li>${escapeHtml(redactedText(warning))}</li>`).join("")}</ul></div>` : ""}<div class="modal-actions"><button class="button" data-action="cancel-task-risk">取消</button><button class="button primary" data-action="confirm-task-risk">仍要并行启动</button></div></section></div>`;
  }
  if (state.modal === "stop-task") {
    return `<div class="modal-backdrop"><section class="modal"><h2>安全停止任务？</h2><p class="note">已生成的文件会保留在输出目录；Excel、Word 会结束为终态。PDF/图片应先“暂停提交”，再选择继续或结束暂停。</p><div class="modal-actions"><button class="button" data-action="close-modal">继续执行</button><button class="button danger" data-action="stop-task-confirm">安全停止</button></div></section></div>`;
  }
  if (state.modal === "xls-compatibility") {
    const count = selectedExcelXlsCount();
    return `<div class="modal-backdrop"><section class="modal wide-modal"><h2>.xls 转换方式确认</h2><p class="note">已选择 ${count} 个旧版 .xls 文件。最终结果统一输出为 .xlsx，源文件不会被改写。</p><div class="risk-callout"><strong>${icon("warn", "small")}高保真与兼容模式</strong><p>优先高保真会通过本机 Microsoft Excel 自动化转换；若 Excel 未安装、自动化被拒绝或单文件转换失败，该文件会明确失败，其他文件仍可继续，绝不静默改用兼容模式。</p><p>允许兼容转换会在高保真不可用时继续处理，但复杂样式、合并单元格、图片、图表和宏可能无法完整保留；这项选择只冻结到本次任务。</p></div><details class="permission-help"><summary>Excel 自动化权限说明</summary><p>macOS 12 Monterey：打开“系统偏好设置 → 安全性与隐私 → 隐私 → 自动化”，允许 Translator 控制 Microsoft Excel。</p><p>macOS 13 及以上：打开“系统设置 → 隐私与安全性 → 自动化”，允许 Translator 控制 Microsoft Excel。</p><p>若 Excel 未安装或权限已拒绝，可取消后完成授权，或明确选择兼容转换。</p></details><div class="modal-actions modal-actions-spread"><button class="button" data-action="close-modal">取消</button><span><button class="button" data-action="excel-start-high-fidelity">优先高保真</button><button class="button primary" data-action="excel-start-compatibility">允许兼容转换</button></span></div></section></div>`;
  }
  if (state.modal === "doc-compatibility") {
    const count = selectedWordDocCount();
    return `<div class="modal-backdrop"><section class="modal wide-modal"><h2>.doc 转换方式确认</h2><p class="note">已选择 ${count} 个旧版 .doc 文件。它们会先在临时位置转换为 .docx；最终产物始终为 .docx，源文件不会被改写。</p><div class="risk-callout"><strong>${icon("warn", "small")}高保真与兼容模式</strong><p>优先高保真会通过本机 Microsoft Word 自动化转换。若 Word 未安装、自动化被拒绝或单文件转换失败，该文件会明确失败，其他文件仍可继续，绝不静默改用兼容模式。</p><p>允许兼容转换会在高保真不可用时尝试 LibreOffice 或 macOS textutil；复杂版式、域、图文内容和宏可能无法完整保留。这项选择只冻结到本次任务。</p></div><details class="permission-help"><summary>Word 自动化权限说明</summary><p>macOS 12 Monterey：打开“系统偏好设置 → 安全性与隐私 → 隐私 → 自动化”，允许 Translator 控制 Microsoft Word。</p><p>macOS 13 及以上：打开“系统设置 → 隐私与安全性 → 自动化”，允许 Translator 控制 Microsoft Word。</p><p>若 Word 未安装或权限已拒绝，可取消后完成授权，或明确选择兼容转换。</p></details><div class="modal-actions modal-actions-spread"><button class="button" data-action="close-modal">取消</button><span><button class="button" data-action="word-start-high-fidelity">优先高保真</button><button class="button primary" data-action="word-start-compatibility">允许兼容转换</button></span></div></section></div>`;
  }
  if (state.modal === "tm-clean") {
    const rows = state.tmSuggestions.map((suggestion, index) => `<div class="suggestion-row"><input id="tmSuggestion-${index}" type="checkbox" checked/><div><b>${escapeHtml(text(suggestion.source_text))}</b><p>${escapeHtml(text(suggestion.old_target))} → <input id="tmSuggestedTarget-${index}" value="${escapeHtml(text(suggestion.new_target))}"/></p></div></div>`).join("");
    return `<div class="modal-backdrop"><section class="modal wide-modal"><h2>清洗建议</h2><p class="note">检查建议译文，取消勾选的条目不会写回。固定策略沿用当前设置。</p><div class="suggestion-list">${rows || "未生成可写入的建议。"}</div><div class="modal-actions"><button class="button" data-action="close-modal">取消</button><button class="button primary" data-action="tm-clean-apply" ${state.tmSuggestions.length ? "" : "disabled"}>写入已勾选建议</button></div></section></div>`;
  }
  if (state.modal === "tm-import" && state.tmImportPreview) {
    const preview = state.tmImportPreview;
    const rows = preview.entries.slice(0, 8).map((entry) => `<tr><td>${escapeHtml(entry.source_text)}</td><td>${escapeHtml(entry.target_text)}</td><td>${escapeHtml(text(entry.word_type, "-"))}</td></tr>`).join("");
    const customTarget = tmPairTargetIsCustom(preview.langPair);
    return `<div class="modal-backdrop"><section class="modal wide-modal"><h2>导入预览</h2><p class="note"><strong>${escapeHtml(preview.fileName)}</strong> · ${preview.format.toUpperCase()} · 共 ${preview.entries.length} 条。请确认字段映射和重复项策略后再写入。</p><label class="field-label" for="tmImportPair">目标语言对</label><input id="tmImportPair" value="${escapeHtml(preview.langPair)}" placeholder="例如 zh-en"/><div class="field-row" style="margin-top:10px"><div><label class="field-label" for="tmImportMode">重复项</label><select id="tmImportMode" data-tm-import-mode><option value="skip" ${preview.mode === "skip" ? "selected" : ""}>跳过重复项</option><option value="overwrite" ${preview.mode === "overwrite" ? "selected" : ""}>覆盖重复项</option><option value="keep_both" ${preview.mode === "keep_both" ? "selected" : ""}>保留两份</option></select></div><div><label class="field-label">同步</label><div class="toggle-row"><input id="tmImportSyncReverse" type="checkbox" ${preview.syncReverse ? "checked" : ""} ${customTarget ? "disabled" : ""}/><label for="tmImportSyncReverse">同时写入反向语言对</label></div></div></div>${customTarget ? `<p class="note">当前目标语言为自定义语言，反向同步已禁用。</p>` : ""}<div class="table-scroll tm-preview-table"><table><thead><tr><th>原文</th><th>译文</th><th>来源</th></tr></thead><tbody>${rows || `<tr><td colspan="3" class="muted">没有可导入的有效行。</td></tr>`}</tbody></table></div>${preview.entries.length > 8 ? `<p class="note">仅显示前 8 条预览。</p>` : ""}<div class="modal-actions"><button class="button" data-action="close-modal">取消</button><button class="button primary" data-action="tm-import-confirm" ${preview.entries.length ? "" : "disabled"}>确认导入</button></div></section></div>`;
  }
  if (state.modal === "tm-full-import" && state.tmFullImportPreview) {
    const preview = state.tmFullImportPreview;
    const entries = Array.isArray(preview.payload.entries) ? preview.payload.entries.length : 0;
    const conflicts = Array.isArray(preview.payload.conflict_candidates) ? preview.payload.conflict_candidates.length : 0;
    const customLanguages = Array.isArray(preview.payload.custom_target_langs) ? preview.payload.custom_target_langs.length : 0;
    return `<div class="modal-backdrop"><section class="modal wide-modal"><h2>恢复完整记忆库</h2><p class="note"><strong>${escapeHtml(preview.fileName)}</strong> · 当前格式 tm-full-v1。将校验并恢复 ${entries} 条词条、${conflicts} 条冲突候选和 ${customLanguages} 个自定义目标语言。</p><label class="field-label" for="tmFullImportMode">重复项</label><select id="tmFullImportMode"><option value="skip" ${preview.mode === "skip" ? "selected" : ""}>跳过重复项</option><option value="overwrite" ${preview.mode === "overwrite" ? "selected" : ""}>覆盖低等级重复项</option><option value="keep_both" ${preview.mode === "keep_both" ? "selected" : ""}>保留冲突候选</option></select><label class="field-label" for="tmFullCodeMap" style="margin-top:10px">自定义代码映射（可选 JSON）</label><input id="tmFullCodeMap" value="${escapeHtml(JSON.stringify(preview.codeMap))}" placeholder='例如 {"x-custom-old":"x-custom-new"}'/><p class="note">完整备份包含原文和译文；代码定义冲突时必须填写映射，无法映射则取消恢复。</p><div class="modal-actions"><button class="button" data-action="close-modal">取消</button><button class="button primary" data-action="tm-full-import-confirm" ${entries ? "" : "disabled"}>确认恢复</button></div></section></div>`;
  }
  if (state.modal === "model-import" && state.modelImportPreview) {
    const preview = state.modelImportPreview;
    const roleRows = preview.roles.map((item) => `<li><strong>${escapeHtml(item.role)}</strong>：${escapeHtml(item.fields.join("、") || "无显式字段")}</li>`).join("");
    return `<div class="modal-backdrop"><section class="modal wide-modal"><h2>预览导入 v3 模型配置</h2><p class="note"><strong>${escapeHtml(preview.fileName)}</strong> · 仅合并文件明确字段，不删除未提及配置。</p><ul class="import-summary">${roleRows || "<li>没有角色配置变更。</li>"}</ul><p class="note">吞吐档案：${preview.throughput_profile_count} 项；文件中包含的密钥作用域：${preview.api_key_count} 个。导入后受影响角色全部变为“未测试”，不会自动请求服务。</p><div class="modal-actions"><button class="button" data-action="close-modal">取消</button><button class="button primary" data-action="model-import-confirm">确认合并</button></div></section></div>`;
  }
  if (state.modal === "migration") {
    return `<div class="modal-backdrop"><section class="modal"><h2>旧数据迁移</h2><p class="note">检查旧版数据目录，并且只在你确认后迁移不冲突的数据。</p><div id="migrationResult" class="note">准备检查…</div><div class="modal-actions"><button class="button" data-action="close-modal">关闭</button><button class="button primary" data-action="migration-apply">迁移不冲突数据</button></div></section></div>`;
  }
  if (state.modal === "update") {
    const result = state.updateResult ?? {};
    return `<div class="modal-backdrop"><section class="modal"><h2>${escapeHtml(text(result.message, "检查更新"))}</h2><p class="note">当前版本：${escapeHtml(text(result.current_version, "V8.0.0"))}</p><p class="note">${escapeHtml(text(result.release_notes, text(result.detail, "没有可用的更新说明。")))}</p><div class="modal-actions"><button class="button" data-action="close-modal">关闭</button><button class="button" data-action="ignore-update" ${text(result.latest_version) ? "" : "disabled"}>忽略此版本</button></div></section></div>`;
  }
  if (state.modal === "notice" && state.modalNotice) {
    return `<div class="modal-backdrop"><section class="modal"><h2>${escapeHtml(state.modalNotice.title)}</h2><p class="note">${escapeHtml(state.modalNotice.message)}</p><div class="modal-actions"><button class="button primary" data-action="close-modal">知道了</button></div></section></div>`;
  }
  return "";
}

function stat(iconName: IconName, label: string, value: string): string {
  return `<div class="stat"><div class="stat-icon">${icon(iconName, "small")}</div><div><div class="stat-label">${label}</div><div class="stat-value">${escapeHtml(value)}</div></div></div>`;
}

function languageMatches(option: LanguageOption, query: string): boolean {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return true;
  return [option.code, option.display_name, ...(option.aliases ?? [])]
    .some((value) => value.toLocaleLowerCase().includes(needle));
}

function languageOptions(selected: string, query = ""): string {
  const options = state.targetOptions.filter((option) => languageMatches(option, query));
  return options.map((option) => `<option value="${escapeHtml(option.code)}" ${option.code === selected ? "selected" : ""}>${escapeHtml(option.display_name)}${option.builtin === false ? "（自定义）" : ""}</option>`).join("");
}

function sourceLanguageOptions(selected: string, query = ""): string {
  const options = state.sourceOptions.filter((option) => languageMatches(option, query));
  return options.map((option) => `<option value="${escapeHtml(option.code)}" ${option.code === selected ? "selected" : ""}>${escapeHtml(option.display_name)}</option>`).join("");
}

function providerLabel(provider: string): string {
  return ({ custom_openai: "OpenAI 兼容", openai: "OpenAI", claude: "Claude", zhipu: "智谱 GLM", dashscope: "阿里百炼", siliconflow: "硅基流动", ollama: "Ollama", lm_studio: "LM Studio", custom_local: "自定义本地服务" } as Record<string, string>)[provider] ?? provider;
}

function surfaceLabel(surface: Surface): string {
  return ({ excel: "Excel", word: "Word", pdf: "PDF" } as Record<Surface, string>)[surface];
}

function logTone(level: string): string {
  if (level === "ERROR") return "error";
  if (level === "WARN") return "warn";
  if (level === "OK") return "ok";
  return "";
}

function tmLangPair(): string {
  return `${state.tmSourceLang || "zh"}-${state.tmTargetLang || "en"}`;
}

function tmPairAllowsReverse(): boolean {
  return !tmTargetIsCustom();
}

function tmTargetIsCustom(): boolean {
  return state.tmTargetOptions.find((option) => option.code === state.tmTargetLang)?.builtin === false || state.tmTargetLang.startsWith("x-custom-");
}

function tmPairTargetIsCustom(pair: string): boolean {
  const parsed = splitTmPair(pair);
  return parsed ? state.tmTargetOptions.find((option) => option.code === parsed.target)?.builtin === false || parsed.target.startsWith("x-custom-") : false;
}

function tmSourceLanguageOptions(): string {
  return state.tmSourceOptions
    .filter((option) => option.builtin !== false && option.can_source !== false)
    .map((option) => `<option value="${escapeHtml(option.code)}" ${option.code === state.tmSourceLang ? "selected" : ""}>${escapeHtml(option.display_name)}</option>`)
    .join("");
}

function tmTargetLanguageOptions(): string {
  return state.tmTargetOptions
    .filter((option) => option.can_target !== false)
    .map((option) => `<option value="${escapeHtml(option.code)}" ${option.code === state.tmTargetLang ? "selected" : ""}>${escapeHtml(option.display_name)}${option.builtin === false ? "（自定义）" : ""}</option>`)
    .join("");
}

function tmPairLabel(pair: string): string {
  const [source, ...targetParts] = pair.split("-");
  const target = targetParts.join("-");
  const sourceOption = state.tmSourceOptions.find((option) => option.code === source);
  const targetOption = state.tmTargetOptions.find((option) => option.code === target);
  return `${sourceOption?.display_name ?? source} → ${targetOption?.display_name ?? target}`;
}

function splitTmPair(pair: string): { source: string; target: string } | null {
  const [source, ...targetParts] = pair.split("-");
  const target = targetParts.join("-");
  return source && target ? { source, target } : null;
}

function showToast(message: string, error = false): void {
  state.toast = { message, error };
  render();
  window.setTimeout(() => {
    if (state.toast?.message === message) {
      state.toast = null;
      render();
    }
  }, 4200);
}

async function refreshSettings(): Promise<void> {
  state.settings = await client.request<JsonObject>("/api/settings");
  state.panelOpen = Boolean(appearance().model_config_panel_open);
  state.sourcePaths.excel = text(state.settings?.last_excel_source_folder, state.sourcePaths.excel);
  state.sourcePaths.word = text(state.settings?.last_word_source_folder, state.sourcePaths.word);
  state.sourcePaths.pdf = text(state.settings?.last_pdf_source_folder, state.sourcePaths.pdf);
  state.targetSelections.excel = text(state.settings?.excel_target_lang, text(state.settings?.target_lang, "en"));
  state.targetSelections.word = text(state.settings?.word_target_lang, text(state.settings?.target_lang, "en"));
  state.targetSelections.pdf = text(record(state.settings?.pdf).target_lang, "zh");
  state.sourceSelections.excel = text(state.settings?.excel_source_lang, "auto");
  state.sourceSelections.word = text(state.settings?.word_source_lang, "auto");
  state.tmSourceLang = text(state.settings?.tm_source_lang, "zh");
  state.tmTargetLang = text(state.settings?.tm_target_lang, "en");
  applyTheme(selectedTheme());
  const excelOutput = excelOutputSettings();
  if (excelOutput.use_custom_output_dir) {
    void inspectExcelOutputDirectory(text(excelOutput.custom_output_dir));
  }
  const wordOutput = wordOutputSettings();
  if (wordOutput.use_custom_output_dir) {
    void inspectWordOutputDirectory(text(wordOutput.custom_output_dir));
  }
  const pdfOutput = pdfOutputSettings();
  if (pdfOutput.use_custom_output_dir) {
    void inspectPdfOutputDirectory(text(pdfOutput.custom_output_dir));
  }
  await refreshModelRoles();
}

async function refreshModelRoles(): Promise<void> {
  const payload = await client.request<{ roles: Record<string, JsonObject> }>("/api/models/roles");
  state.modelRoles = payload.roles || {};
  await Promise.all(Object.keys(state.modelRoles).map((role) => refreshModelThroughput(role)));
}

async function refreshModelThroughput(role: string): Promise<void> {
  try {
    state.modelThroughput[role] = await client.request<JsonObject>(`/api/models/throughput/${encodeURIComponent(role)}`);
  } catch {
    state.modelThroughput[role] = record(state.modelRoles[role]?.throughput);
  }
}

async function refreshLanguages(): Promise<void> {
  const payload = await client.request<{
    languages: LanguageOption[];
    source_options: LanguageOption[];
    target_options: LanguageOption[];
  }>("/api/languages");
  state.languages = payload.languages;
  state.sourceOptions = payload.source_options;
  state.targetOptions = payload.target_options;
}

async function refreshTmLanguagePairs(): Promise<void> {
  const payload = await client.request<{
    source_options: LanguageOption[];
    target_options: LanguageOption[];
    selected?: { source_lang?: string; target_lang?: string };
    recent?: string[];
  }>("/api/tm/language-pairs");
  state.tmSourceOptions = payload.source_options.filter((option) => option.builtin !== false && option.can_source !== false);
  state.tmTargetOptions = payload.target_options.filter((option) => option.can_target !== false);
  state.tmRecentPairs = Array.isArray(payload.recent) ? payload.recent.filter((pair): pair is string => typeof pair === "string") : [];
  const source = text(payload.selected?.source_lang, state.tmSourceLang);
  const target = text(payload.selected?.target_lang, state.tmTargetLang);
  state.tmSourceLang = state.tmSourceOptions.some((option) => option.code === source)
    ? source
    : (state.tmSourceOptions[0]?.code ?? "zh");
  state.tmTargetLang = state.tmTargetOptions.some((option) => option.code === target)
    ? target
    : (state.tmTargetOptions[0]?.code ?? "en");
}

async function refreshTm(): Promise<void> {
  const payload = await client.request<{ entries: TmEntry[]; stats: JsonObject; total?: number }>(
    `/api/tm/entries?lang_pair=${encodeURIComponent(tmLangPair())}&keyword=${encodeURIComponent(state.tmKeyword)}&page=${state.tmPage}&page_size=${state.tmPageSize}`,
  );
  state.tmEntries = payload.entries;
  state.tmStats = payload.stats;
  state.tmTotal = number(payload.total, state.tmEntries.length);
  const totalPages = Math.max(1, Math.ceil(state.tmTotal / state.tmPageSize));
  if (state.tmPage > totalPages) {
    state.tmPage = totalPages;
    await refreshTm();
  }
}

async function refreshTmConflicts(): Promise<void> {
  const payload = await client.request<{ conflicts: TmConflict[] }>(
    `/api/tm/conflicts?lang_pair=${encodeURIComponent(tmLangPair())}`,
  );
  state.tmConflicts = Array.isArray(payload.conflicts) ? payload.conflicts : [];
}

async function saveTmLanguagePair(source: string, target: string): Promise<void> {
  if (source === target) throw new Error("TM 源语言和目标语言不能相同。");
  state.tmSourceLang = source;
  state.tmTargetLang = target;
  state.tmPage = 1;
  state.tmSelectedIds = [];
  state.tmCleaningState = "idle";
  state.tmConflictMessage = "";
  const pair = tmLangPair();
  state.tmRecentPairs = [pair, ...state.tmRecentPairs.filter((item) => item !== pair)].slice(0, 8);
  await persistSettings({
    tm_source_lang: source,
    tm_target_lang: target,
    recent_tm_lang_pairs: state.tmRecentPairs,
  });
  await refreshTm();
  await refreshTmConflicts();
  render();
}

async function persistSettings(patch: JsonObject): Promise<void> {
  state.settings = await client.request<JsonObject>("/api/settings", {
    method: "PUT",
    body: JSON.stringify(patch),
  });
}

let excelOutputInspectionSequence = 0;
let wordOutputInspectionSequence = 0;
let pdfOutputInspectionSequence = 0;

async function inspectExcelOutputDirectory(path: string): Promise<void> {
  const sequence = ++excelOutputInspectionSequence;
  try {
    const inspection = await invoke<OutputDirectoryInspection>("inspect_output_directory", { path });
    if (sequence !== excelOutputInspectionSequence) return;
    state.excelOutputInspection = inspection;
  } catch (error) {
    if (sequence !== excelOutputInspectionSequence) return;
    state.excelOutputInspection = {
      state: "blocked",
      path,
      message: `无法检查输出目录：${errorMessage(error)}`,
    };
  }
  const status = document.querySelector<HTMLElement>("#excel-output-state");
  if (status) {
    status.className = `output-path-state ${state.excelOutputInspection.state}`;
    status.textContent = state.excelOutputInspection.message;
  }
}

async function chooseExcelOutputDirectory(): Promise<void> {
  const selected = await open({ title: "选择 Excel 输出根目录", directory: true, multiple: false });
  if (typeof selected !== "string") return;
  await persistSettings(nestedPatch(excelOutputSettingPath("custom_output_dir"), selected));
  await persistSettings(nestedPatch(excelOutputSettingPath("use_custom_output_dir"), true));
  await inspectExcelOutputDirectory(selected);
  render();
}

async function inspectWordOutputDirectory(path: string): Promise<void> {
  const sequence = ++wordOutputInspectionSequence;
  try {
    const inspection = await invoke<OutputDirectoryInspection>("inspect_output_directory", { path });
    if (sequence !== wordOutputInspectionSequence) return;
    state.wordOutputInspection = inspection;
  } catch (error) {
    if (sequence !== wordOutputInspectionSequence) return;
    state.wordOutputInspection = {
      state: "blocked",
      path,
      message: `无法检查输出目录：${errorMessage(error)}`,
    };
  }
  const status = document.querySelector<HTMLElement>("#word-output-state");
  if (status) {
    status.className = `output-path-state ${state.wordOutputInspection.state}`;
    status.textContent = state.wordOutputInspection.message;
  }
}

async function chooseWordOutputDirectory(): Promise<void> {
  const selected = await open({ title: "选择 Word 输出根目录", directory: true, multiple: false });
  if (typeof selected !== "string") return;
  await persistSettings(nestedPatch(wordOutputSettingPath("custom_output_dir"), selected));
  await persistSettings(nestedPatch(wordOutputSettingPath("use_custom_output_dir"), true));
  await inspectWordOutputDirectory(selected);
  render();
}

async function inspectPdfOutputDirectory(path: string): Promise<void> {
  const sequence = ++pdfOutputInspectionSequence;
  try {
    const inspection = await invoke<OutputDirectoryInspection>("inspect_output_directory", { path });
    if (sequence !== pdfOutputInspectionSequence) return;
    state.pdfOutputInspection = inspection;
  } catch (error) {
    if (sequence !== pdfOutputInspectionSequence) return;
    state.pdfOutputInspection = {
      state: "blocked",
      path,
      message: `无法检查输出目录：${errorMessage(error)}`,
    };
  }
  const status = document.querySelector<HTMLElement>("#pdf-output-state");
  if (status) {
    status.className = `output-path-state ${state.pdfOutputInspection.state}`;
    status.textContent = state.pdfOutputInspection.message;
  }
}

async function choosePdfOutputDirectory(): Promise<void> {
  const selected = await open({ title: "选择 PDF / 图片输出根目录", directory: true, multiple: false });
  if (typeof selected !== "string") return;
  await persistSettings(nestedPatch(pdfOutputSettingPath("custom_output_dir"), selected));
  await persistSettings(nestedPatch(pdfOutputSettingPath("use_custom_output_dir"), true));
  await inspectPdfOutputDirectory(selected);
  render();
}

async function chooseSource(surface: Surface, directory: boolean): Promise<void> {
  const pdfExtensions = pdfIncludeImagesEnabled()
    ? ["pdf", "png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"]
    : ["pdf"];
  const selected = await open({
    title: directory ? `选择${surfaceLabel(surface)}文件夹` : `选择${surfaceLabel(surface)}文件`,
    directory,
    multiple: false,
    filters: directory ? undefined : [{ name: surface === "pdf" ? "PDF / 图片" : surfaceLabel(surface), extensions: surface === "excel" ? ["xlsx", "xls"] : surface === "word" ? ["docx", "doc"] : pdfExtensions }],
  });
  if (typeof selected === "string") {
    state.sourcePaths[surface] = selected;
    render();
  }
}

async function scan(surface: Surface): Promise<void> {
  const path = state.sourcePaths[surface];
  if (!path) {
    throw new Error("请先选择源文件或文件夹。");
  }
  type SourceScanPayload = {
    items?: FileItem[];
    skipped?: ScanSkippedItem[];
    summary?: JsonObject;
    risk?: JsonObject;
    result?: { items?: FileItem[]; skipped?: ScanSkippedItem[]; summary?: JsonObject; risk?: JsonObject };
  };
  const payload = await client.request<SourceScanPayload>("/api/sources/scan", {
    method: "POST",
    body: JSON.stringify({ surface, path, include_images: surface === "pdf" && pdfIncludeImagesEnabled() }),
  });
  const grouped = payload.result ?? {};
  const items = Array.isArray(payload.items) ? payload.items : Array.isArray(grouped.items) ? grouped.items : [];
  const skipped = Array.isArray(payload.skipped) ? payload.skipped : Array.isArray(grouped.skipped) ? grouped.skipped : [];
  const summary = record(payload.summary ?? grouped.summary);
  const risk = record(payload.risk ?? grouped.risk);
  state.files[surface] = items;
  state.scanReports[surface] = {
    skipped: skipped.map((item) => record(item) as ScanSkippedItem),
    summary,
    risk,
  };
  state.selectedPaths[surface] = items.map((item) => item.path);
  await persistSettings({ [`last_${surface}_source_folder`]: path });
  render();
  showToast(surface === "pdf"
    ? `已扫描到 ${items.length} 个 PDF / 图片输入，跳过 ${skipped.length} 个。`
    : `已扫描到 ${items.length} 个${surfaceLabel(surface)}文件。`);
}

async function startTask(
  surface: Surface,
  allowXlsFallback = false,
  xlsConfirmed = false,
  allowDocFallback = false,
  docConfirmed = false,
): Promise<void> {
  if (surface === "excel" || surface === "word") {
    // A task snapshot must receive the prompt the user can currently see, not
    // a stale settings value left behind by an unsaved textarea edit.
    await saveDomainPrompt(surface, false);
  }
  const path = state.sourcePaths[surface];
  if (!path) {
    throw new Error("请先选择源文件或文件夹。");
  }
  const selectedPaths = state.selectedPaths[surface];
  if (!selectedPaths.length) {
    throw new Error("请至少选择一个待翻译文件。");
  }
  if (surface === "excel") {
    const source = state.sourceSelections.excel;
    const target = state.targetSelections.excel;
    if (source !== "auto" && source === target) {
      throw new Error("源语言与目标语言相同，不能启动翻译。");
    }
    if (selectedExcelXlsCount() > 0 && !xlsConfirmed) {
      state.modal = "xls-compatibility";
      render();
      return;
    }
  }
  if (surface === "word") {
    const source = state.sourceSelections.word;
    const target = state.targetSelections.word;
    if (source !== "auto" && source === target) {
      throw new Error("源语言与目标语言相同，不能启动翻译。");
    }
    if (selectedWordDocCount() > 0 && !docConfirmed) {
      state.modal = "doc-compatibility";
      render();
      return;
    }
  }
  const untranslated = Boolean(document.querySelector<HTMLInputElement>(`#untranslated-${surface}`)?.checked);
  const protectSchemeCover = surface === "word" && untranslated
    ? Boolean(document.querySelector<HTMLInputElement>("#protectSchemeCover")?.checked)
    : false;
  const includeImages = surface === "pdf" && pdfIncludeImagesEnabled();
  const reviewRole = record(state.modelRoles.pdf_review);
  const reviewKnownUnavailable = surface === "pdf"
    && Boolean(pdfSettings().review_enabled)
    && text(reviewRole.availability_status) === "unavailable";
  const allowKnownReviewFailure = reviewKnownUnavailable
    ? window.confirm("PDF 审核模型当前配置已经测试失败。继续会使用本次冻结配置尝试审核；失败页面会保留为可复核结果。是否继续？")
    : false;
  if (reviewKnownUnavailable && !allowKnownReviewFailure) return;
  const payload: JsonObject = {
    surface,
    source_path: path,
    selected_paths: selectedPaths,
    untranslated_only: untranslated,
    protect_scheme_cover: protectSchemeCover,
    allow_xls_fallback: surface === "excel" && allowXlsFallback,
    allow_doc_fallback: surface === "word" && allowDocFallback,
    include_images: includeImages,
    source_lang: surface === "pdf" ? undefined : state.sourceSelections[surface],
    target_lang: state.targetSelections[surface],
    allow_known_review_failure: allowKnownReviewFailure,
  };
  const preflight = await client.preflightTask(payload);
  if (preflight.requires_confirmation) {
    state.pendingTaskRisk = { surface, payload, preflight };
    state.modal = "task-risk";
    render();
    return;
  }
  await submitTaskStart(surface, payload);
}

async function submitTaskStart(
  surface: TaskSurface,
  payload: JsonObject,
  confirmationToken = "",
): Promise<void> {
  const requestPayload = confirmationToken ? { ...payload, confirmation_token: confirmationToken } : payload;
  const task = await client.request<TaskStatus>("/api/tasks", {
    method: "POST",
    body: JSON.stringify(requestPayload),
  });
  state.excelFileProgress = surface === "excel" ? {} : state.excelFileProgress;
  state.wordFileProgress = surface === "word" ? {} : state.wordFileProgress;
  state.wordRecovery = surface === "word" ? {} : state.wordRecovery;
  state.pdfFileProgress = surface === "pdf" ? {} : state.pdfFileProgress;
  state.pdfPageRecovery = surface === "pdf" ? {} : state.pdfPageRecovery;
  state.pdfReview = surface === "pdf" ? {} : state.pdfReview;
  state.tmCleaningState = surface === "tm_clean" || surface === "cleaner" ? "running" : state.tmCleaningState;
  const entry = upsertTask(task, true);
  entry.phaseName = "正在准备任务";
  state.pendingTaskRisk = null;
  render();
  void watchTask(task.task_id);
}

async function watchTask(taskId: string): Promise<void> {
  const running = state.tasks[taskId];
  if (!running || running.watcherActive || running.task.terminal) return;
  running.watcherActive = true;
  try {
    running.lastEventId = await client.streamTask(
      taskId,
      (event) => handleTaskEvent(taskId, event),
      {
        lastEventId: running.lastEventId,
        onConnectionState: (streamState) => {
          const entry = state.tasks[taskId];
          if (!entry) return;
          entry.streamState = streamState;
          render();
        },
      },
    );
    const latest = state.tasks[taskId];
    if (latest && !latest.task.terminal) {
      const refreshed = await client.getTask(taskId);
      const entry = upsertTask(refreshed);
      entry.streamState = "connected";
    }
  } catch (error) {
    const latest = state.tasks[taskId];
    if (!latest) return;
    try {
      const refreshed = await client.getTask(taskId);
      const entry = upsertTask(refreshed);
      entry.streamState = "connected";
      if (!refreshed.terminal) {
        window.setTimeout(() => void watchTask(taskId), 0);
        return;
      }
    } catch {
      latest.task = { ...latest.task, state: "interrupted", terminal: true };
      latest.streamState = "interrupted";
      latest.phaseName = "sidecar 已重启或应用异常退出；本任务不能继续，请依据已生成产物或清单新建任务。";
      showToast("任务监控无法恢复，已标记为应用中断。", true);
    }
    showToast(`任务事件流中断：${errorMessage(error)}`, true);
  } finally {
    const latest = state.tasks[taskId];
    if (latest) latest.watcherActive = false;
    render();
  }
}

function handleTaskEvent(taskId: string, event: SseEvent): void {
  const running = state.tasks[taskId];
  if (!running) return;
  running.lastEventId = Math.max(running.lastEventId, event.id);
  const data = event.data;
  if (event.type === "log") {
    running.logs.push({ level: text(data.level, "INFO"), message: redactedText(data.message) });
  }
  if (event.type === "progress") {
    running.phaseName = text(data.phase_name, "正在处理");
    running.stepDone = number(data.step_done);
    running.stepTotal = number(data.step_total);
  }
  if (event.type === "status") {
    running.phaseName = text(data.phase_desc, running.phaseName);
    if (running.task.surface === "excel" || running.task.surface === "word") {
      const surface = running.task.surface;
      const current = state.files[surface].find((file) => running.phaseName.includes(file.name));
      if (current) {
        const progress = surface === "excel" ? state.excelFileProgress : state.wordFileProgress;
        progress[current.path] = {
          ...progress[current.path],
          stage: running.phaseName,
        };
      }
    }
  }
  if (event.type === "stopping") {
    running.task = { ...running.task, state: "stopping" };
  }
  if (event.type === "paused") {
    running.task = { ...running.task, state: "paused" };
    running.phaseName = "已暂停提交新页面";
  }
  if (event.type === "resumed") {
    running.task = { ...running.task, state: "running" };
    running.phaseName = "正在继续提交页面";
  }
  if (event.type === "word_recovery" && running.task.surface === "word") {
    state.wordRecovery = { ...data };
  }
  if (event.type === "pdf_page_recovery" && running.task.surface === "pdf") {
    state.pdfPageRecovery = { ...data };
  }
  if (event.type === "pdf_review" && running.task.surface === "pdf") {
    state.pdfReview = { ...data };
  }
  if (running.task.surface === "excel" || running.task.surface === "word") {
    const surface = running.task.surface;
    const filePath = firstText(data, ["file_path", "source_path", "path", "relative_path"]);
    if (filePath && ["file", "file_status", "preflight", "progress", "result"].includes(event.type)) {
      const knownPath = state.files[surface].find((file) => file.path === filePath || displayPath(file) === filePath)?.path || filePath;
      const progress = surface === "excel" ? state.excelFileProgress : state.wordFileProgress;
      progress[knownPath] = { ...progress[knownPath], ...data };
    }
  }
  if (running.task.surface === "pdf") {
    const filePath = firstText(data, ["file_path", "source_path", "path", "relative_path"]);
    if (filePath && ["file", "file_status", "preflight", "progress", "result"].includes(event.type)) {
      const knownPath = state.files.pdf.find((file) => file.path === filePath || displayPath(file) === filePath)?.path || filePath;
      state.pdfFileProgress[knownPath] = { ...state.pdfFileProgress[knownPath], ...data };
    }
  }
  if (["done", "completed_with_issues", "error", "stopped", "interrupted"].includes(event.type)) {
    running.task = { ...running.task, state: event.type as TaskStatus["state"], terminal: true, result: data };
    if (running.task.surface === "excel" || running.task.surface === "word") {
      const surface = running.task.surface;
      const progress = surface === "excel" ? state.excelFileProgress : state.wordFileProgress;
      for (const entry of resultEntries(data, ["files", "file_results", "file_records"])) {
        const filePath = firstText(entry, ["source_path", "path", "source_relative_path", "relative_path"]);
        const knownPath = state.files[surface].find((file) => file.path === filePath || displayPath(file) === filePath)?.path;
        if (knownPath) progress[knownPath] = { ...progress[knownPath], ...entry };
      }
    }
    if (running.task.surface === "pdf") {
      for (const entry of resultEntries(data, ["files", "file_results", "file_records"])) {
        const filePath = firstText(entry, ["source_path", "path", "source_relative_path", "relative_path"]);
        const knownPath = state.files.pdf.find((file) => file.path === filePath || displayPath(file) === filePath)?.path;
        if (knownPath) state.pdfFileProgress[knownPath] = { ...state.pdfFileProgress[knownPath], ...entry };
      }
    }
    if (event.type === "done") {
      showToast(running.task.surface === "word"
        ? "Word 翻译任务已完成。文件级结果、质量报告和输出目录已显示。"
        : running.task.surface === "pdf"
          ? "PDF / 图片翻译任务已完成。文件结果、页面复核与输出证据已显示。"
          : running.task.surface === "tm_clean"
            ? "TM 清洗已完成，正在载入建议审核。"
          : "翻译任务已完成。文件级结果和输出目录已显示在 Excel 页面。 ");
      if (running.task.surface === "tm_clean") void loadTmCleanSuggestions(taskId);
    } else {
      if (running.task.surface === "tm_clean") state.tmCleaningState = "error";
      showToast(text(data.message, "任务未完成。"), true);
    }
  }
  render();
}

async function stopTask(taskId: string): Promise<void> {
  const entry = state.tasks[taskId];
  if (!entry) return;
  entry.task = await client.request<TaskStatus>(`/api/tasks/${taskId}/stop`, { method: "POST" });
  render();
}

async function pausePdfTask(taskId: string): Promise<void> {
  const entry = state.tasks[taskId];
  if (!entry || entry.task.surface !== "pdf") return;
  entry.task = await client.request<TaskStatus>(`/api/tasks/${taskId}/pause`, { method: "POST" });
  render();
}

async function resumePdfTask(taskId: string): Promise<void> {
  const entry = state.tasks[taskId];
  if (!entry || entry.task.surface !== "pdf") return;
  entry.task = await client.request<TaskStatus>(`/api/tasks/${taskId}/resume`, { method: "POST" });
  render();
}

async function endPausedPdfTask(taskId: string): Promise<void> {
  const entry = state.tasks[taskId];
  if (!entry || entry.task.surface !== "pdf") return;
  if (!window.confirm("结束暂停任务将不再提交未处理页面，但会写入并保留已完成页面、素材、清单和报告。是否结束？")) return;
  entry.task = await client.request<TaskStatus>(`/api/tasks/${taskId}/end-paused`, { method: "POST" });
  render();
}

async function refreshTaskRegistry(): Promise<void> {
  const payload = await client.listTasks();
  const entries = [...(Array.isArray(payload.active) ? payload.active : []), ...(Array.isArray(payload.recent) ? payload.recent : [])];
  for (const task of entries) {
    const entry = upsertTask(task);
    if (isTaskActive(task)) void watchTask(task.task_id);
    else entry.watcherActive = false;
  }
}

function reconnectActiveTasks(): void {
  for (const entry of activeTasks()) {
    if (!entry.watcherActive) void watchTask(entry.task.task_id);
  }
}

async function openTaskLocalFile(path: string, reveal: boolean): Promise<void> {
  if (!path.trim()) throw new Error("任务没有可用的本地文件引用。");
  await invoke("open_local_path", { path, reveal });
}

async function copyTaskPath(path: string): Promise<void> {
  if (!path.trim()) throw new Error("没有可复制的路径。");
  if (!navigator.clipboard?.writeText) throw new Error("当前系统 WebView 不支持复制路径。");
  await navigator.clipboard.writeText(path);
  showToast("路径已复制。");
}

function nestedPatch(path: string, value: unknown): JsonObject {
  const parts = path.split(".").filter(Boolean);
  const patch: JsonObject = {};
  let cursor = patch;
  for (const part of parts.slice(0, -1)) {
    const next: JsonObject = {};
    cursor[part] = next;
    cursor = next;
  }
  if (parts.length) {
    cursor[parts[parts.length - 1]] = value;
  }
  return patch;
}

async function saveSettingPath(
  path: string,
  value: string | number | boolean | null,
): Promise<void> {
  await persistSettings(nestedPatch(path, value));
  render();
}

async function saveDomainPreset(surface: TranslationSurface, preset: string): Promise<void> {
  const prefix = surface === "excel" ? "excel" : "word";
  await persistSettings({ [`${prefix}_domain_preset`]: preset });
  render();
}

async function saveDomainPrompt(surface: TranslationSurface, showMessage = true): Promise<void> {
  const prefix = surface === "excel" ? "excel" : "word";
  const current = domainSettings(surface);
  const preset = inputValue(`${prefix}DomainPreset`, current.preset);
  const prompt = document.querySelector<HTMLTextAreaElement>(`#${prefix}DomainPrompt`)?.value ?? "";
  const promptOverrides = { ...current.promptOverrides };
  let customPrompt = current.customPrompt;

  if (preset === "自定义") {
    if (!prompt.trim()) {
      throw new Error("自定义领域必须填写完整 Prompt，不能启动任务或保存空配置。");
    }
    customPrompt = prompt;
  } else {
    const defaultPrompt = domainBuiltInPrompt(preset, state.targetSelections[surface]);
    if (prompt === defaultPrompt) delete promptOverrides[preset];
    else promptOverrides[preset] = prompt;
  }

  await client.request(`/api/domains/${surface}`, {
    method: "PUT",
    body: JSON.stringify({
      preset,
      custom_prompt: customPrompt,
      prompt_overrides: promptOverrides,
      name_overrides: current.nameOverrides,
    }),
  });
  await refreshSettings();
  if (showMessage) {
    render();
    showToast(preset === "自定义" ? "自定义领域 Prompt 已保存。" : "当前页面的领域 Prompt 覆盖已保存。");
  }
}

async function restoreDomainPrompt(surface: TranslationSurface): Promise<void> {
  const prefix = surface === "excel" ? "excel" : "word";
  const current = domainSettings(surface);
  const preset = inputValue(`${prefix}DomainPreset`, current.preset);
  if (preset === "自定义") return;
  const promptOverrides = { ...current.promptOverrides };
  delete promptOverrides[preset];
  await client.request(`/api/domains/${surface}`, {
    method: "PUT",
    body: JSON.stringify({
      preset,
      custom_prompt: current.customPrompt,
      prompt_overrides: promptOverrides,
      name_overrides: current.nameOverrides,
    }),
  });
  await refreshSettings();
  render();
  showToast("已恢复当前页面与领域的内置 Prompt。 ");
}

async function saveReviewColor(mark: string, color: string): Promise<void> {
  const colors = record(excelReviewSettings().mark_colors);
  await persistSettings({ excel_review: { mark_colors: { ...colors, [mark]: color.replace("#", "").toUpperCase() } } });
  render();
}

async function saveWordReviewColor(mark: string, color: string): Promise<void> {
  const wordReview = record(state.settings?.word_review);
  const colors = record(wordReview.mark_colors);
  await persistSettings({ word_review: { mark_colors: { ...colors, [mark]: color.replace("#", "").toUpperCase() } } });
  render();
}

async function saveModel(): Promise<void> {
  const role = state.modelRole;
  const mode = inputValue("engineMode", "cloud");
  const provider = inputValue("provider", mode === "cloud" ? "custom_openai" : "ollama");
  const baseUrl = inputValue("baseUrl");
  const model = inputValue("modelName");
  const key = inputValue("apiKey");
  const roleSource = document.querySelector<HTMLSelectElement>("#roleSource")?.value;
  const payload = role === "translation"
    ? { mode, provider, base_url: baseUrl, model }
    : { source_role: roleSource || "independent", provider, base_url: baseUrl, model };
  await client.request(`/api/models/roles/${role}`, { method: "PUT", body: JSON.stringify(payload) });
  if (key && mode === "cloud") {
    await client.request(`/api/keys/${provider}`, { method: "PUT", body: JSON.stringify({ api_key: key, base_url: baseUrl }) });
  }
  clearModelCatalog(role, "连接已变更，请刷新模型目录。 ");
  await refreshSettings();
  render();
  showToast("模型角色配置已保存。密钥仅写入本机密钥存储。" );
}

function clearModelCatalog(role: string, message = "尚未读取当前连接的模型目录。 "): void {
  delete state.modelCatalog[role];
  delete state.modelCatalogConnection[role];
  state.modelCatalogMessage[role] = message;
}

function ensureSavedModelForm(): void {
  const role = state.modelRole;
  const saved = record(state.modelRoles[role]);
  const mode = role === "translation" ? inputValue("engineMode", text(saved.mode, "cloud")) : text(saved.mode, "cloud");
  const provider = inputValue("provider", text(saved.provider));
  const baseUrl = inputValue("baseUrl", text(saved.base_url));
  const sourceRole = document.querySelector<HTMLSelectElement>("#roleSource")?.value;
  if (
    mode !== text(saved.mode, "cloud")
    || provider !== text(saved.provider)
    || baseUrl !== text(saved.base_url)
    || (role !== "translation" && sourceRole !== text(saved.source_role, "independent"))
    || Boolean(inputValue("apiKey"))
  ) {
    throw new Error("请先保存当前角色配置，再刷新目录或测试连接。");
  }
}

async function fetchModels(): Promise<void> {
  const role = state.modelRole;
  ensureSavedModelForm();
  const result = await client.request<{ ok: boolean; models: string[]; message: string }>(`/api/models/catalog/${encodeURIComponent(role)}`, {
    method: "POST",
    body: JSON.stringify({ refresh: true }),
  });
  state.modelCatalog[role] = result.models;
  state.modelCatalogMessage[role] = result.message;
  state.modelCatalogConnection[role] = modelCatalogConnectionForRole(role);
  render();
  showToast(result.models.length ? `已刷新 ${result.models.length} 个模型，可从模型名称输入框选择。` : result.message, !result.ok);
}

async function testModel(): Promise<void> {
  ensureSavedModelForm();
  const result = await client.request<{ ok: boolean; message: string }>(`/api/models/connectivity/${state.modelRole}`, { method: "POST" });
  showToast(result.message, !result.ok);
  await refreshModelRoles();
  render();
}

async function saveThroughput(): Promise<void> {
  const role = state.modelRole;
  const payload: JsonObject = {
    concurrency: Number(inputValue("throughputConcurrency", "1")),
  };
  if (role === "translation" || role === "cleaner") {
    payload.batch_size = Number(inputValue("throughputBatch", "8"));
  }
  const result = await client.request<JsonObject>(`/api/models/throughput/${encodeURIComponent(role)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  state.modelThroughput[role] = result;
  render();
  showToast("当前角色吞吐设置已保存。运行中的任务不受影响。");
}

async function restoreThroughput(): Promise<void> {
  const role = state.modelRole;
  const result = await client.request<JsonObject>(`/api/models/throughput/${encodeURIComponent(role)}`, {
    method: "DELETE",
  });
  state.modelThroughput[role] = result;
  render();
  showToast("已恢复当前角色与有效模型的推荐吞吐值。运行中的任务不受影响。");
}

async function saveLanguage(surface: Surface, target: string): Promise<void> {
  state.targetSelections[surface] = target;
  const patch = surface === "pdf"
    ? { pdf: { target_lang: target } }
    : surface === "excel" ? { excel_target_lang: target } : { word_target_lang: target };
  await persistSettings(patch);
  render();
}

async function saveSourceLanguage(surface: Surface, value: string): Promise<void> {
  if (surface === "pdf") return;
  state.sourceSelections[surface] = value;
  await persistSettings(surface === "excel" ? { excel_source_lang: value } : { word_source_lang: value });
  render();
}

async function savePdfReview(enabled: boolean): Promise<void> {
  await persistSettings({ pdf: { review_enabled: enabled } });
  render();
}

async function refreshLanguageCatalog(): Promise<void> {
  await refreshLanguages();
  await refreshSettings();
}

async function createCustomLanguage(): Promise<void> {
  const name = inputValue("customLanguageName");
  const description = document.querySelector<HTMLTextAreaElement>("#customLanguageDescription")?.value.trim() ?? "";
  if (!name) throw new Error("请输入自定义语言名称。");
  await client.request("/api/languages/custom", {
    method: "POST",
    body: JSON.stringify({ name, description }),
  });
  await refreshLanguageCatalog();
  state.modal = null;
  render();
  showToast("自定义目标语言已添加。");
}

async function updateCustomLanguage(): Promise<void> {
  const editing = state.customLanguageEditing;
  if (!editing) return;
  const description = document.querySelector<HTMLTextAreaElement>("#customLanguageDescription")?.value.trim() ?? "";
  await client.request(`/api/languages/custom/${encodeURIComponent(editing.code)}`, {
    method: "PUT",
    body: JSON.stringify({ name: editing.display_name, description }),
  });
  await refreshLanguageCatalog();
  state.modal = null;
  state.customLanguageEditing = null;
  render();
  showToast("自定义语言说明已更新。");
}

async function deleteCustomLanguage(): Promise<void> {
  const editing = state.customLanguageEditing;
  if (!editing) return;
  await client.request(`/api/languages/custom/${encodeURIComponent(editing.code)}`, { method: "DELETE" });
  await refreshLanguageCatalog();
  state.modal = null;
  state.customLanguageEditing = null;
  render();
  showToast("自定义目标语言已删除。");
}

async function tmPin(entryId: number, pinned: boolean): Promise<void> {
  await client.request(`/api/tm/entries/${entryId}/pin`, { method: "POST", body: JSON.stringify({ pinned }) });
  await refreshTm();
  render();
}

async function tmBulkPin(pinned: boolean): Promise<void> {
  if (!state.tmSelectedIds.length) return;
  await client.request("/api/tm/entries/bulk/pin", {
    method: "POST",
    body: JSON.stringify({ ids: state.tmSelectedIds, pinned }),
  });
  state.tmSelectedIds = [];
  await refreshTm();
  render();
  showToast(pinned ? "已固定所选记忆条目。" : "已解除所选记忆条目的固定。 ");
}

async function tmBulkDelete(): Promise<void> {
  if (!state.tmSelectedIds.length) return;
  const ids = [...state.tmSelectedIds];
  const result = await client.request<{ deleted: number; protected: number; missing: number }>("/api/tm/entries/bulk/delete", {
    method: "POST",
    body: JSON.stringify({ ids }),
  });
  state.tmSelectedIds = [];
  await refreshTm();
  render();
  const detail = result.protected ? `，${result.protected} 条固定词条未删除` : "";
  showToast(`已删除 ${result.deleted} 条记忆条目${detail}。`, Boolean(result.protected));
}

function toggleTmPageSelection(): void {
  const pageIds = state.tmEntries.map((entry) => entry.id);
  const allSelected = pageIds.length > 0 && pageIds.every((id) => state.tmSelectedIds.includes(id));
  state.tmSelectedIds = allSelected
    ? state.tmSelectedIds.filter((id) => !pageIds.includes(id))
    : [...new Set([...state.tmSelectedIds, ...pageIds])];
  render();
}

async function tmCreate(): Promise<void> {
  const syncReverse = tmPairAllowsReverse() && Boolean(document.querySelector<HTMLInputElement>("#tmSyncReverse")?.checked);
  await client.request("/api/tm/entries", {
    method: "POST",
    body: JSON.stringify({ source_text: inputValue("tmSource"), target_text: inputValue("tmTarget"), lang_pair: inputValue("tmPair", tmLangPair()), sync_reverse: syncReverse }),
  });
  state.tmConflictMessage = "";
  state.modal = null;
  await refreshTm();
  render();
  showToast(syncReverse ? "记忆条目已保存，并同步反向语言对。" : "记忆条目已保存。" );
}

async function tmUpdate(): Promise<void> {
  if (!state.tmEditing) return;
  const syncReverse = tmPairAllowsReverse() && Boolean(document.querySelector<HTMLInputElement>("#tmSyncReverse")?.checked);
  await client.request(`/api/tm/entries/${state.tmEditing.id}`, {
    method: "PUT",
    body: JSON.stringify({ source_text: inputValue("tmSource"), target_text: inputValue("tmTarget"), sync_reverse: syncReverse }),
  });
  state.tmConflictMessage = "";
  state.modal = null;
  state.tmEditing = null;
  await refreshTm();
  render();
  showToast("记忆条目已更新。");
}

async function tmDelete(): Promise<void> {
  if (!state.tmEditing) return;
  await client.request(`/api/tm/entries/${state.tmEditing.id}`, { method: "DELETE" });
  state.modal = null;
  state.tmEditing = null;
  await refreshTm();
  render();
  showToast("记忆条目已删除。");
}

async function tmClean(): Promise<void> {
  state.tmCleaningState = "running";
  state.tmConflictMessage = "";
  render();
  try {
    const payload: JsonObject = {
      surface: "tm_clean",
      source_path: tmLangPair(),
      lang_pair: tmLangPair(),
    };
    const preflight = await client.preflightTask(payload);
    if (preflight.requires_confirmation) {
      state.pendingTaskRisk = { surface: "tm_clean", payload, preflight };
      state.modal = "task-risk";
      render();
      return;
    }
    await submitTaskStart("tm_clean", payload);
  } catch (error) {
    state.tmCleaningState = "error";
    render();
    throw error;
  }
}

async function loadTmCleanSuggestions(taskId: string): Promise<void> {
  const task = state.tasks[taskId]?.task;
  const snapshot = record(task?.task_snapshot);
  const result = record(task?.result);
  const language = record(result.language);
  const langPair = firstText({ ...snapshot, ...language, ...result }, ["lang_pair"]) || tmLangPair();
  let suggestions: JsonObject[] = [];
  try {
    const resultStatus = await client.getTaskResult(taskId);
    const payload = record(resultStatus.result);
    suggestions = resultEntries(payload, ["suggestions", "tm_suggestions", "review_suggestions"]);
  } catch {
    // A task result may deliberately exclude TM content from the persisted
    // summary.  The dedicated review route is allowed to return it only for
    // the live local review workflow.
  }
  if (!suggestions.length) {
    try {
      const payload = await client.request<JsonObject>(`/api/tm/clean/suggestions?lang_pair=${encodeURIComponent(langPair)}`);
      suggestions = resultEntries(payload, ["suggestions", "items"]);
    } catch {
      // The task may have produced no suggestions, which is a valid outcome.
    }
  }
  state.tmSuggestions = suggestions;
  state.tmCleaningState = "ready";
  state.modal = "tm-clean";
  render();
}

async function applyTmCleanSuggestions(): Promise<void> {
  const suggestions = state.tmSuggestions.map((suggestion, index) => ({
    entry_id: number(suggestion.entry_id),
    source_text: text(suggestion.source_text),
    old_target: text(suggestion.old_target),
    new_target: inputValue(`tmSuggestedTarget-${index}`, text(suggestion.new_target)),
    accepted: Boolean(document.querySelector<HTMLInputElement>(`#tmSuggestion-${index}`)?.checked),
  }));
  const result = await client.request<{ applied: number }>("/api/tm/clean/apply", {
    method: "POST",
    body: JSON.stringify({ suggestions, auto_pin: false }),
  });
  state.tmSuggestions = [];
  state.tmCleaningState = "idle";
  state.modal = null;
  await refreshTm();
  render();
  showToast(`已写入 ${result.applied} 条清洗建议。`);
}

async function exportTm(format: "json" | "csv" = "json"): Promise<void> {
  const payload = await client.request<JsonObject>(`/api/tm/export?lang_pair=${encodeURIComponent(tmLangPair())}`);
  if (format === "csv") {
    const entries = Array.isArray(payload.entries) ? payload.entries as JsonObject[] : [];
    downloadBlob(`translator-tm-${tmLangPair()}.csv`, toTmCsv(entries), "text/csv;charset=utf-8");
  } else {
    downloadJson(`translator-tm-${tmLangPair()}.json`, payload);
  }
  state.modalNotice = { title: "记忆库已导出", message: `已导出当前语言对的 ${format.toUpperCase()} 文件，共 ${Array.isArray(payload.entries) ? payload.entries.length : 0} 条。` };
  state.modal = "notice";
  render();
}

async function exportFullTm(): Promise<void> {
  const payload = await client.request<JsonObject>("/api/tm/export/full");
  downloadJson("translator-tm-full.json", payload);
  state.modalNotice = { title: "完整记忆库已导出", message: "已导出当前新基线的全部语言对、可信等级、冲突候选和自定义目标语言定义。" };
  state.modal = "notice";
  render();
}

async function importTm(): Promise<void> {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "application/json,.json,text/csv,.csv";
  input.onchange = async () => {
    const file = input.files?.[0];
    if (!file) return;
    try {
      const raw = await file.text();
      const isCsv = file.name.toLocaleLowerCase().endsWith(".csv") || file.type.includes("csv");
      let entries: TmImportEntry[];
      let langPair = tmLangPair();
      if (isCsv) {
        entries = parseTmCsv(raw);
      } else {
        const payload = JSON.parse(raw) as JsonObject;
        if (text(payload.format_version) === "tm-full-v1") {
          state.tmFullImportPreview = { fileName: file.name, payload, mode: "skip", codeMap: {} };
          state.modal = "tm-full-import";
          render();
          return;
        }
        entries = Array.isArray(payload.entries) ? payload.entries.filter((entry): entry is TmImportEntry => Boolean(entry && typeof entry === "object")) : [];
        langPair = text(payload.lang_pair, tmLangPair());
      }
      state.tmImportPreview = { fileName: file.name, format: isCsv ? "csv" : "json", langPair, entries, mode: "skip", syncReverse: false };
      state.modal = "tm-import";
      render();
    } catch (error) {
      showToast(`无法读取 TM 导入文件：${errorMessage(error)}`, true);
    }
  };
  input.click();
}

async function importFullTm(): Promise<void> {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "application/json,.json";
  input.onchange = async () => {
    const file = input.files?.[0];
    if (!file) return;
    try {
      const payload = JSON.parse(await file.text()) as JsonObject;
      if (text(payload.format_version) !== "tm-full-v1") throw new Error("这不是当前格式的完整 TM 备份。");
      state.tmFullImportPreview = { fileName: file.name, payload, mode: "skip", codeMap: {} };
      state.modal = "tm-full-import";
      render();
    } catch (error) {
      showToast(`无法读取完整 TM 备份：${errorMessage(error)}`, true);
    }
  };
  input.click();
}

async function confirmTmFullImport(): Promise<void> {
  const preview = state.tmFullImportPreview;
  if (!preview) return;
  let codeMap: Record<string, string> = {};
  const rawMap = inputValue("tmFullCodeMap", "{}");
  try {
    const parsed = JSON.parse(rawMap) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("代码映射必须是 JSON 对象。");
    codeMap = Object.fromEntries(Object.entries(parsed as Record<string, unknown>).map(([key, value]) => [key, String(value)]));
  } catch (error) {
    throw new Error(`代码映射格式无效：${errorMessage(error)}`);
  }
  const result = await client.request<{ inserted?: number; skipped?: number; duplicates?: number; conflicts?: number }>("/api/tm/import/full", {
    method: "POST",
    body: JSON.stringify({ ...preview.payload, mode: preview.mode, code_map: codeMap, sync_reverse: false }),
  });
  state.tmFullImportPreview = null;
  state.modal = "notice";
  await refreshTm();
  await refreshTmConflicts();
  state.modalNotice = { title: "完整记忆库已恢复", message: `新增或更新 ${number(result.inserted)} 条，跳过 ${number(result.skipped)} 条，重复 ${number(result.duplicates)} 条，恢复冲突候选 ${number(result.conflicts)} 条。` };
  render();
}

async function resolveTmConflict(candidateId: number, action: string): Promise<void> {
  await client.request(`/api/tm/conflicts/${candidateId}/resolve`, {
    method: "POST",
    body: JSON.stringify({ action }),
  });
  await refreshTmConflicts();
  await refreshTm();
  render();
}

async function confirmTmImport(): Promise<void> {
  const preview = state.tmImportPreview;
  if (!preview) return;
  const pairInput = inputValue("tmImportPair", preview.langPair);
  const syncReverse = !tmPairTargetIsCustom(pairInput) && Boolean(document.querySelector<HTMLInputElement>("#tmImportSyncReverse")?.checked);
  const result = await client.request<{ inserted?: number; skipped?: number; duplicates?: number }>("/api/tm/import", {
    method: "POST",
    body: JSON.stringify({ lang_pair: pairInput, mode: preview.mode, entries: preview.entries, sync_reverse: syncReverse }),
  });
  state.tmImportPreview = null;
  state.modal = "notice";
  await refreshTm();
  state.modalNotice = { title: "记忆库已导入", message: `已完成导入：新增或更新 ${number(result.inserted)}，跳过 ${number(result.skipped)}，重复 ${number(result.duplicates)}。` };
  render();
}

function parseTmCsv(raw: string): TmImportEntry[] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let index = 0; index < raw.length; index += 1) {
    const char = raw[index];
    if (char === '"') {
      if (quoted && raw[index + 1] === '"') {
        field += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
    } else if (char === "," && !quoted) {
      row.push(field);
      field = "";
    } else if ((char === "\n" || char === "\r") && !quoted) {
      if (char === "\r" && raw[index + 1] === "\n") index += 1;
      row.push(field);
      if (row.some((value) => value.trim())) rows.push(row);
      row = [];
      field = "";
    } else {
      field += char;
    }
  }
  if (field || row.length) {
    row.push(field);
    if (row.some((value) => value.trim())) rows.push(row);
  }
  if (!rows.length) return [];
  const headers = rows.shift()!.map((header) => header.trim().toLocaleLowerCase());
  const findColumn = (names: string[]) => headers.findIndex((header) => names.includes(header));
  const sourceIndex = findColumn(["source_text", "source", "原文"]);
  const targetIndex = findColumn(["target_text", "target", "译文"]);
  const wordTypeIndex = findColumn(["word_type", "type", "来源"]);
  const pinnedIndex = findColumn(["pinned", "固定"]);
  return rows.map((values) => ({
    source_text: values[sourceIndex >= 0 ? sourceIndex : 0]?.trim() ?? "",
    target_text: values[targetIndex >= 0 ? targetIndex : 1]?.trim() ?? "",
    ...(wordTypeIndex >= 0 ? { word_type: values[wordTypeIndex]?.trim() } : {}),
    ...(pinnedIndex >= 0 ? { pinned: values[pinnedIndex]?.trim() } : {}),
  })).filter((entry) => entry.source_text && entry.target_text);
}

function csvCell(value: unknown): string {
  const textValue = String(value ?? "");
  return /[",\r\n]/.test(textValue) ? `"${textValue.replaceAll('"', '""')}"` : textValue;
}

function toTmCsv(entries: JsonObject[]): string {
  const headers = ["source_text", "target_text", "word_type", "pinned", "updated_at"];
  const lines = [headers.join(",")];
  for (const entry of entries) {
    lines.push(headers.map((header) => csvCell(entry[header])).join(","));
  }
  return `\ufeff${lines.join("\r\n")}\r\n`;
}

async function exportModelConfig(includeApiKey = false): Promise<void> {
  if (includeApiKey && !window.confirm("导出的文件将包含 API Key。请确认只保存到受保护的位置。")) return;
  const query = includeApiKey ? "?include_api_key=true&confirm_sensitive=true" : "";
  const payload = await client.request<JsonObject>(`/api/model-config/export${query}`);
  downloadJson("translator-model-config.json", payload);
  state.modalNotice = { title: "模型配置已导出", message: includeApiKey ? "已导出 v3 模型配置和明确勾选的 API Key，请立即移入受保护的位置。" : "已导出 v3 模型配置；默认不包含 API Key。" };
  state.modal = "notice";
  render();
}

async function importModelConfig(): Promise<void> {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "application/json,.json";
  input.onchange = async () => {
    const file = input.files?.[0];
    if (!file) return;
    try {
      const payload = JSON.parse(await file.text()) as JsonObject;
      const preview = await client.request<Omit<ModelImportPreview, "fileName" | "payload">>("/api/model-config/import/preview", { method: "POST", body: JSON.stringify(payload) });
      state.modelImportPreview = { fileName: file.name, payload, ...preview };
      state.modal = "model-import";
      render();
    } catch (error) {
      showToast(`模型配置导入预览失败：${errorMessage(error)}`, true);
    }
  };
  input.click();
}

async function confirmModelConfigImport(): Promise<void> {
  const preview = state.modelImportPreview;
  if (!preview) return;
  const result = await client.request<{ imported_key_count: number }>("/api/model-config/import", {
    method: "POST",
    body: JSON.stringify(preview.payload),
  });
  state.modelImportPreview = null;
  state.modal = "notice";
  await refreshSettings();
  for (const role of Object.keys(state.modelRoles)) clearModelCatalog(role, "导入后请刷新当前连接的模型目录。 ");
  state.modalNotice = { title: "模型配置已导入", message: `已合并 v3 配置与 ${result.imported_key_count} 个密钥作用域；所有受影响角色均需重新测试。` };
  render();
}

async function checkUpdate(): Promise<void> {
  state.updateResult = await client.request<JsonObject>("/api/updates/check");
  state.modal = "update";
  render();
}

async function ignoreCurrentUpdate(): Promise<void> {
  const release = text(state.updateResult?.latest_version);
  if (!release) return;
  await client.request("/api/updates/preferences", {
    method: "PUT",
    body: JSON.stringify({ ignore_updates: true, ignored_release_version: release }),
  });
  state.modal = null;
  state.updateResult = null;
  render();
  showToast(`已忽略版本 ${release}。`);
}

async function downloadDiagnostics(): Promise<void> {
  const response = await fetchWithToken("/api/diagnostics/history.zip");
  if (!response.ok) throw new Error("导出诊断归档失败。");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "translator-diagnostics.zip";
  anchor.click();
  URL.revokeObjectURL(url);
}

async function inspectMigration(): Promise<void> {
  const payload = await client.request<JsonObject>("/api/migration/inspect");
  const node = document.querySelector<HTMLElement>("#migrationResult");
  if (node) node.textContent = `状态：${text(payload.status)}；可迁移主数据：${Array.isArray(payload.primary_items) ? payload.primary_items.length : 0} 项。`;
}

async function applyMigration(): Promise<void> {
  const payload = await client.request<JsonObject>("/api/migration/apply", { method: "POST", body: JSON.stringify({ action: "non_conflicting", include_support_files: false }) });
  showToast(`迁移完成：${Array.isArray(payload.migrated) ? payload.migrated.length : 0} 项。`);
  state.modal = null;
  render();
}

async function fetchWithToken(path: string): Promise<Response> {
  const info = await import("@tauri-apps/api/core").then(({ invoke }) => invoke<{ port: number; token: string }>("sidecar_info"));
  return fetch(`http://127.0.0.1:${info.port}${path}`, { headers: { "X-Translator-Token": info.token } });
}

function downloadJson(filename: string, payload: JsonObject): void {
  downloadBlob(filename, JSON.stringify(payload, null, 2), "application/json");
}

function downloadBlob(filename: string, content: string, mimeType: string): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function inputValue(id: string, fallback = ""): string {
  return document.querySelector<HTMLInputElement | HTMLSelectElement>(`#${id}`)?.value.trim() || fallback;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

app.addEventListener("click", (event) => {
  const target = (event.target as HTMLElement).closest<HTMLElement>("[data-action]");
  if (!target) return;
  void handleAction(target).catch((error) => {
    const message = errorMessage(error);
    if (state.view === "tm" && (/409|conflict|冲突|重复/i.test(message))) {
      state.tmConflictMessage = message;
      render();
    }
    showToast(message, true);
  });
});

app.addEventListener("change", (event) => {
  const target = event.target as HTMLInputElement | HTMLSelectElement;
  if (target.id === "engineMode") {
    state.settings = {
      ...(state.settings ?? {}),
      engine: { ...engineSettings(), mode: target.value },
    };
    clearModelCatalog("translation", "接入方式草稿已变更；保存后可刷新目录。 ");
    render();
    return;
  }
  if (target.dataset.modelRole) {
    state.modelRole = target.value;
    void refreshModelThroughput(state.modelRole).then(render).catch(() => render());
    return;
  }
  if (target.dataset.roleSource) {
    const independent = target.value === "independent";
    const provider = document.querySelector<HTMLSelectElement>("#provider");
    const baseUrl = document.querySelector<HTMLInputElement>("#baseUrl");
    if (provider) provider.disabled = !independent;
    if (baseUrl) baseUrl.disabled = !independent;
    clearModelCatalog(state.modelRole, "连接复用方式已变更；保存后可刷新目录。 ");
    return;
  }
  if (target.dataset.domainPreset) {
    void saveDomainPreset(target.dataset.domainPreset as TranslationSurface, target.value).catch((error) => showToast(errorMessage(error), true));
    return;
  }
  if (["provider", "baseUrl", "apiKey"].includes(target.id)) {
    clearModelCatalog(state.modelRole, "连接草稿已变更；保存后可刷新目录。 ");
  }
  if (target.dataset.source) {
    state.sourcePaths[target.dataset.source as Surface] = target.value;
  }
  if (target.dataset.filePath && target.dataset.surface) {
    const surface = target.dataset.surface as Surface;
    const current = new Set(state.selectedPaths[surface]);
    if ((target as HTMLInputElement).checked) current.add(target.dataset.filePath);
    else current.delete(target.dataset.filePath);
    state.selectedPaths[surface] = [...current];
    render();
    return;
  }
  if (target.dataset.tmEntryId) {
    const entryId = Number(target.dataset.tmEntryId);
    const selected = new Set(state.tmSelectedIds);
    if ((target as HTMLInputElement).checked) selected.add(entryId);
    else selected.delete(entryId);
    state.tmSelectedIds = [...selected];
    render();
    return;
  }
  if (target.dataset.tmSelectPage !== undefined) {
    toggleTmPageSelection();
    return;
  }
  if (target.dataset.tmSourceLang) {
    void saveTmLanguagePair(target.value, state.tmTargetLang).catch((error) => showToast(errorMessage(error), true));
    return;
  }
  if (target.dataset.tmTargetLang) {
    void saveTmLanguagePair(state.tmSourceLang, target.value).catch((error) => showToast(errorMessage(error), true));
    return;
  }
  if (target.dataset.tmPageSize) {
    state.tmPageSize = Number(target.value) || 25;
    state.tmPage = 1;
    state.tmSelectedIds = [];
    void refreshTm().then(render).catch((error) => showToast(errorMessage(error), true));
    return;
  }
  if (target.dataset.tmImportMode && state.tmImportPreview) {
    state.tmImportPreview.mode = target.value as TmImportPreview["mode"];
    return;
  }
  if (target.id === "tmFullImportMode" && state.tmFullImportPreview) {
    state.tmFullImportPreview.mode = target.value as TmFullImportPreview["mode"];
    return;
  }
  if (target.id === "tmImportSyncReverse" && state.tmImportPreview) {
    state.tmImportPreview.syncReverse = (target as HTMLInputElement).checked;
    return;
  }
  if (target.id === "tmImportPair" && state.tmImportPreview) {
    state.tmImportPreview.langPair = target.value.trim() || tmLangPair();
    render();
    return;
  }
  if ((target.id === "excelAutofit" || target.id === "lockRowHeight") && target instanceof HTMLInputElement) {
    const selected = target.checked;
    const primaryPath = target.id === "excelAutofit"
      ? excelOutputSettingPath("enable_excel_autofit")
      : excelOutputSettingPath("lock_row_height");
    const conflictingPath = target.id === "excelAutofit"
      ? excelOutputSettingPath("lock_row_height")
      : excelOutputSettingPath("enable_excel_autofit");
    void (async () => {
      await saveSettingPath(primaryPath, selected);
      if (selected) await saveSettingPath(conflictingPath, false);
    })().catch((error) => showToast(errorMessage(error), true));
    return;
  }
  if ((target.id === "wordBatchChars" || target.id === "wordSplitThreshold") && target instanceof HTMLInputElement) {
    const wordBatch = record(state.settings?.word_batch);
    const maxChars = target.id === "wordBatchChars"
      ? Math.max(1, Number(target.value) || 1)
      : Math.max(1, number(wordBatch.max_chars_per_batch, 3000));
    const requestedSplit = target.id === "wordSplitThreshold"
      ? Math.max(1, Number(target.value) || 1)
      : Math.max(1, number(wordBatch.split_paragraph_chars, maxChars));
    const split = Math.max(maxChars, requestedSplit);
    if (target.id === "wordSplitThreshold") target.value = String(split);
    void persistSettings({
      word_batch: {
        max_chars_per_batch: maxChars,
        split_paragraph_chars: split,
      },
    }).then(render).catch((error) => showToast(errorMessage(error), true));
    return;
  }
  if (target.id === "wordStrictRetry" && target instanceof HTMLInputElement) {
    const attempts = Math.min(8, Math.max(1, Number(target.value) || 1));
    target.value = String(attempts);
    void saveSettingPath("word_batch.strict_retry_attempts", attempts).catch((error) => showToast(errorMessage(error), true));
    return;
  }
  if (target.dataset.settingPath) {
    const kind = target.dataset.valueKind;
    let value: string | number | boolean | null;
    if (kind === "custom-output") value = target.value === "custom";
    else if (kind === "number") value = Number(target.value);
    else if (kind === "optional-number") value = target.value.trim() ? Number(target.value) : null;
    else if (target instanceof HTMLInputElement && target.type === "checkbox") value = target.checked;
    else value = target.value;
    void saveSettingPath(target.dataset.settingPath, value)
      .then(async () => {
        if (target.dataset.excelOutputInspect !== undefined || target.id === "output-excel") {
          const path = target.id === "output-excel"
            ? text(excelOutputSettings().custom_output_dir)
            : target.value;
          await inspectExcelOutputDirectory(path);
        }
        if (target.dataset.wordOutputInspect !== undefined || target.id === "output-word") {
          const path = target.id === "output-word"
            ? text(wordOutputSettings().custom_output_dir)
            : target.value;
          await inspectWordOutputDirectory(path);
        }
        if (target.dataset.pdfOutputInspect !== undefined || target.id === "output-pdf") {
          const path = target.id === "output-pdf"
            ? text(pdfOutputSettings().custom_output_dir)
            : target.value;
          await inspectPdfOutputDirectory(path);
        }
      })
      .catch((error) => showToast(errorMessage(error), true));
  }
  if (target.dataset.reviewColor) {
    void saveReviewColor(target.dataset.reviewColor, target.value).catch((error) => showToast(errorMessage(error), true));
  }
  if (target.dataset.wordReviewColor) {
    void saveWordReviewColor(target.dataset.wordReviewColor, target.value).catch((error) => showToast(errorMessage(error), true));
  }
  if (target.dataset.setting === "domain_preset") {
    void persistSettings({ domain_preset: target.value }).then(render).catch((error) => showToast(errorMessage(error), true));
  }
  if (target.dataset.target) void saveLanguage(target.dataset.target as Surface, target.value).catch((error) => showToast(errorMessage(error), true));
  if (target.dataset.sourceLanguage !== undefined) void saveSourceLanguage(target.dataset.sourceLanguage as Surface, target.value).catch((error) => showToast(errorMessage(error), true));
  if (target.dataset.pdfReview !== undefined) void savePdfReview((target as HTMLInputElement).checked).catch((error) => showToast(errorMessage(error), true));
});

let excelOutputInspectionTimer: number | undefined;
let wordOutputInspectionTimer: number | undefined;
let pdfOutputInspectionTimer: number | undefined;

app.addEventListener("input", (event) => {
  const target = event.target as HTMLInputElement;
  if (target.dataset.excelOutputInspect !== undefined) {
    if (excelOutputInspectionTimer !== undefined) window.clearTimeout(excelOutputInspectionTimer);
    excelOutputInspectionTimer = window.setTimeout(() => {
      void inspectExcelOutputDirectory(target.value);
    }, 180);
  }
  if (target.dataset.wordOutputInspect !== undefined) {
    if (wordOutputInspectionTimer !== undefined) window.clearTimeout(wordOutputInspectionTimer);
    wordOutputInspectionTimer = window.setTimeout(() => {
      void inspectWordOutputDirectory(target.value);
    }, 180);
  }
  if (target.dataset.pdfOutputInspect !== undefined) {
    if (pdfOutputInspectionTimer !== undefined) window.clearTimeout(pdfOutputInspectionTimer);
    pdfOutputInspectionTimer = window.setTimeout(() => {
      void inspectPdfOutputDirectory(target.value);
    }, 180);
  }
  if (!target.dataset.languageSearch) return;
  const surface = target.dataset.languageSearch as Surface;
  state.languageSearch[surface] = target.value;
  render();
  const next = document.querySelector<HTMLInputElement>(`#language-search-${surface}`);
  next?.focus();
  next?.setSelectionRange(target.value.length, target.value.length);
});

async function handleAction(target: HTMLElement): Promise<void> {
  const action = target.dataset.action;
  const surface = target.dataset.surface as Surface | undefined;
  const taskId = text(target.dataset.taskId);
  if (action === "navigate" && target.dataset.view) { state.view = target.dataset.view as View; if (state.view === "tm") { await refreshTmLanguagePairs(); await refreshTm(); await refreshTmConflicts(); } if (state.view === "tasks") await refreshTaskRegistry(); render(); return; }
  if (action === "open-task-center") { state.view = "tasks"; if (taskId && state.tasks[taskId]) { const task = state.tasks[taskId].task; if (task.surface === "excel" || task.surface === "word" || task.surface === "pdf") state.workspaceTaskIds[task.surface] = taskId; } await refreshTaskRegistry(); render(); return; }
  if (action === "task-filter") { state.taskCenterFilter = (target.dataset.filter as typeof state.taskCenterFilter) || "all"; render(); return; }
  if (action === "show-task-workspace" && taskId && state.tasks[taskId]) { const task = state.tasks[taskId].task; if (task.surface === "excel" || task.surface === "word" || task.surface === "pdf") { state.workspaceTaskIds[task.surface] = taskId; state.view = task.surface; render(); } return; }
  if (action === "task-local-file") return openTaskLocalFile(text(target.dataset.path), target.dataset.reveal === "1");
  if (action === "task-copy-path") return copyTaskPath(text(target.dataset.path));
  if (action === "toggle-panel") { state.panelOpen = !state.panelOpen; await persistSettings({ appearance: { model_config_panel_open: state.panelOpen } }); render(); return; }
  if (action === "cycle-theme") { const next = selectedTheme() === "system" ? "light" : selectedTheme() === "light" ? "dark" : "system"; await persistSettings({ appearance: { theme: next } }); render(); return; }
  if (action === "choose-source" && surface) { state.sourcePickerSurface = surface; state.modal = "source-picker"; render(); return; }
  if (action === "choose-source-file" && surface) { await chooseSource(surface, false); state.sourcePickerSurface = null; state.modal = null; render(); return; }
  if (action === "choose-source-folder" && surface) { await chooseSource(surface, true); state.sourcePickerSurface = null; state.modal = null; render(); return; }
  if (action === "choose-excel-output") return chooseExcelOutputDirectory();
  if (action === "choose-word-output") return chooseWordOutputDirectory();
  if (action === "choose-pdf-output") return choosePdfOutputDirectory();
  if (action === "scan" && surface) return scan(surface);
  if (action === "start-task" && surface) return startTask(surface);
  if (action === "excel-start-high-fidelity") { state.modal = null; return startTask("excel", false, true); }
  if (action === "excel-start-compatibility") { state.modal = null; return startTask("excel", true, true); }
  if (action === "word-start-high-fidelity") { state.modal = null; return startTask("word", false, false, false, true); }
  if (action === "word-start-compatibility") { state.modal = null; return startTask("word", false, false, true, true); }
  if (action === "stop-task" && taskId) { state.pendingStopTaskId = taskId; state.modal = "stop-task"; render(); return; }
  if (action === "stop-task-confirm") { const id = state.pendingStopTaskId; state.pendingStopTaskId = null; state.modal = null; if (id) return stopTask(id); return; }
  if (action === "pause-pdf-task" && taskId) return pausePdfTask(taskId);
  if (action === "resume-pdf-task" && taskId) return resumePdfTask(taskId);
  if (action === "end-paused-pdf-task" && taskId) return endPausedPdfTask(taskId);
  if (action === "reset-task" && taskId) { const task = state.tasks[taskId]?.task; if (task && (task.surface === "excel" || task.surface === "word" || task.surface === "pdf")) state.workspaceTaskIds[task.surface] = null; render(); return; }
  if (action === "cancel-task-risk") { state.pendingTaskRisk = null; state.modal = null; render(); return; }
  if (action === "confirm-task-risk") { const pending = state.pendingTaskRisk; state.modal = null; if (!pending?.preflight.confirmation_token) throw new Error("风险确认令牌已失效，请重新启动任务。"); return submitTaskStart(pending.surface, pending.payload, pending.preflight.confirmation_token); }
  if (action === "select-all-files" && surface) { state.selectedPaths[surface] = state.files[surface].map((file) => file.path); render(); return; }
  if (action === "select-no-files" && surface) { state.selectedPaths[surface] = []; render(); return; }
  if (action === "save-model") return saveModel();
  if (action === "fetch-models") return fetchModels();
  if (action === "test-model") return testModel();
  if (action === "save-throughput") return saveThroughput();
  if (action === "restore-throughput") return restoreThroughput();
  if (action === "save-domain-prompt" && (surface === "excel" || surface === "word")) return saveDomainPrompt(surface);
  if (action === "restore-domain-prompt" && (surface === "excel" || surface === "word")) return restoreDomainPrompt(surface);
  if (action === "tm-search") { state.tmKeyword = inputValue("tmKeyword"); state.tmPage = 1; state.tmSelectedIds = []; await refreshTm(); render(); return; }
  if (action === "tm-recent-pair") {
    const pair = splitTmPair(text(target.dataset.pair));
    if (pair) await saveTmLanguagePair(pair.source, pair.target);
    return;
  }
  if (action === "tm-select-page") {
    const pageIds = state.tmEntries.map((entry) => entry.id);
    const allSelected = pageIds.length > 0 && pageIds.every((id) => state.tmSelectedIds.includes(id));
    state.tmSelectedIds = allSelected ? state.tmSelectedIds.filter((id) => !pageIds.includes(id)) : [...new Set([...state.tmSelectedIds, ...pageIds])];
    render();
    return;
  }
  if (action === "tm-page-prev") { state.tmPage = Math.max(1, state.tmPage - 1); state.tmSelectedIds = []; await refreshTm(); render(); return; }
  if (action === "tm-page-next") { state.tmPage += 1; state.tmSelectedIds = []; await refreshTm(); render(); return; }
  if (action === "tm-bulk-pin") return tmBulkPin(true);
  if (action === "tm-bulk-unpin") return tmBulkPin(false);
  if (action === "tm-bulk-delete") return tmBulkDelete();
  if (action === "tm-clear-conflict") { state.tmConflictMessage = ""; render(); return; }
  if (action === "tm-refresh-conflicts") { await refreshTmConflicts(); render(); return; }
  if (action === "tm-conflict-resolve") return resolveTmConflict(Number(target.dataset.conflictId), text(target.dataset.conflictResolution));
  if (action === "tm-add") { state.modal = "tm-add"; render(); return; }
  if (action === "tm-create") return tmCreate();
  if (action === "tm-edit") { state.tmEditing = state.tmEntries.find((entry) => entry.id === Number(target.dataset.entryId)) ?? null; state.modal = "tm-edit"; render(); return; }
  if (action === "tm-update") return tmUpdate();
  if (action === "tm-delete") { state.tmEditing = state.tmEntries.find((entry) => entry.id === Number(target.dataset.entryId)) ?? null; state.modal = "tm-delete"; render(); return; }
  if (action === "tm-delete-confirm") return tmDelete();
  if (action === "tm-pin") return tmPin(Number(target.dataset.entryId), target.dataset.pinned === "1");
  if (action === "tm-clean") return tmClean();
  if (action === "tm-clean-apply") return applyTmCleanSuggestions();
  if (action === "custom-language-add") { state.customLanguageEditing = null; state.modal = "custom-language"; render(); return; }
  if (action === "custom-language-manage") {
    state.customLanguageEditing = state.targetOptions.find((option) => option.builtin === false) ?? null;
    if (!state.customLanguageEditing) throw new Error("当前还没有自定义目标语言。");
    state.modal = "custom-language";
    render();
    return;
  }
  if (action === "custom-language-create") return createCustomLanguage();
  if (action === "custom-language-update") return updateCustomLanguage();
  if (action === "custom-language-delete") return deleteCustomLanguage();
  if (action === "tm-import") return importTm();
  if (action === "tm-import-full") return importFullTm();
  if (action === "tm-import-confirm") return confirmTmImport();
  if (action === "tm-full-import-confirm") return confirmTmFullImport();
  if (action === "tm-export" || action === "tm-export-json") return exportTm("json");
  if (action === "tm-export-csv") return exportTm("csv");
  if (action === "tm-export-full") return exportFullTm();
  if (action === "close-modal") { state.modal = null; state.pendingExcelStart = null; state.pendingStopTaskId = null; state.pendingTaskRisk = null; state.sourcePickerSurface = null; state.tmEditing = null; state.customLanguageEditing = null; state.modalNotice = null; state.tmImportPreview = null; state.tmFullImportPreview = null; state.modelImportPreview = null; render(); return; }
  if (action === "export-model-config") return exportModelConfig();
  if (action === "export-model-config-sensitive") return exportModelConfig(true);
  if (action === "import-model-config") return importModelConfig();
  if (action === "model-import-confirm") return confirmModelConfigImport();
  if (action === "check-update") return checkUpdate();
  if (action === "ignore-update") return ignoreCurrentUpdate();
  if (action === "download-diagnostics") return downloadDiagnostics();
  if (action === "migration") { state.modal = "migration"; render(); await inspectMigration(); return; }
  if (action === "migration-apply") return applyMigration();
}

async function bootstrap(): Promise<void> {
  render();
  try {
    await client.connect();
    state.connected = true;
    await refreshSettings();
    await refreshLanguages();
    await refreshTmLanguagePairs();
    await refreshTm();
    await refreshTmConflicts();
    await refreshTaskRegistry();
  } catch (error) {
    showToast(`无法连接翻译引擎：${errorMessage(error)}`, true);
  }
  render();
}

window.addEventListener("focus", reconnectActiveTasks);
window.addEventListener("online", reconnectActiveTasks);

void bootstrap();
