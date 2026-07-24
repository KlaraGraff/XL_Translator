import { open } from "@tauri-apps/plugin-dialog";

import { ApiClient, type SseEvent, type TaskStatus } from "./api-client";
import "./tokens.css";

type Surface = "excel" | "word" | "pdf";
type View = Surface | "tm";
type JsonObject = Record<string, unknown>;
type FileItem = JsonObject & {
  path: string;
  name: string;
  size_kb?: number;
  sheets?: string[];
  page_count?: number;
  paragraph_count?: number;
  table_count?: number;
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
type RunningTask = {
  task: TaskStatus;
  logs: Array<{ level: string; message: string }>;
  phaseName: string;
  stepDone: number;
  stepTotal: number;
};
type Modal = "tm-add" | "tm-edit" | "tm-delete" | "tm-clean" | "tm-import" | "tm-full-import" | "custom-language" | "model-import" | "migration" | "source-picker" | "stop-task" | "notice" | "update" | null;
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
  selectedPaths: Record<Surface, string[]>;
  sourcePaths: Record<Surface, string>;
  running: RunningTask | null;
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
  selectedPaths: { excel: [], word: [], pdf: [] },
  sourcePaths: { excel: "", word: "", pdf: "" },
  running: null,
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

function taskStatus(): { label: string; tone: string } {
  const running = state.running;
  if (!running) {
    return { label: "待执行", tone: "" };
  }
  const current = running.task.state;
  const labels: Record<string, string> = {
    running: "执行中",
    stopping: "正在终止",
    done: "已完成",
    error: "发生错误",
    stopped: "已终止",
  };
  return { label: labels[current] ?? current, tone: current };
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
            <button class="model-button" data-action="toggle-panel" data-tip="当前翻译模型；点击展开配置"><span class="model-dot"></span>${escapeHtml(model)}</button>
            <button class="icon-button" data-action="check-update" data-tip="检查 GitHub 最新版本">${icon("refresh", "small")}</button>
            <button class="icon-button" data-action="cycle-theme" data-tip="切换主题">${icon(selectedTheme() === "dark" ? "sun" : "moon", "small")}</button>
          </div>
        </header>
        <div class="content">
          ${state.view === "tm" ? renderTmView() : renderTranslateView(state.view)}
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
  const isPdf = surface === "pdf";
  const target = state.targetSelections[surface] || (isPdf
    ? text(record(state.settings?.pdf).target_lang, "zh")
    : text(state.settings?.target_lang, "en"));
  const source = state.sourceSelections[surface] || "auto";
  const running = state.running?.task.surface === surface ? state.running : null;
  const activeTask = Boolean(running && !running.task.terminal);
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
        ${stat("file", "已扫描", String(files.length))}
        ${stat("translate", "已选择", String(selectedPaths.length))}
        ${stat("memory", "TM 命中", activeTask ? "进行中" : "—")}
        ${stat("refresh", "API 请求", activeTask ? "进行中" : "—")}
      </div>
      <div class="card table-card"><div class="table-header"><h2>任务清单</h2><span class="table-count">已选 ${selectedPaths.length} / ${files.length}</span><span class="header-spacer"></span><button class="mini-button" data-action="select-all-files" data-surface="${surface}" ${activeTask ? "disabled" : ""}>全选</button><button class="mini-button" data-action="select-no-files" data-surface="${surface}" ${activeTask ? "disabled" : ""}>全不选</button></div><div class="table-scroll">${renderFiles(files, surface, selectedPaths, activeTask)}</div></div>
    </div>
    <aside class="card right-column">
      <span class="section-label">运行设置</span>
      <div class="setting-card"><label class="field-label" for="language-search-${surface}">搜索语言</label><input id="language-search-${surface}" data-language-search="${surface}" value="${escapeHtml(state.languageSearch[surface])}" placeholder="中文名、English、ISO 代码" ${activeTask ? "disabled" : ""}/><label class="field-label" for="target-${surface}" style="margin-top:8px">目标语言</label><select id="target-${surface}" data-target="${surface}" ${activeTask ? "disabled" : ""}>${languageOptions(target, state.languageSearch[surface])}</select><div class="field-row" style="margin-top:8px"><button class="mini-button" data-action="custom-language-add" ${activeTask ? "disabled" : ""}>＋ 自定义语言</button><button class="mini-button" data-action="custom-language-manage" ${activeTask ? "disabled" : ""}>管理自定义</button></div></div>
      ${surface === "excel" || surface === "word" ? renderDomainSettings(surface, activeTask) : ""}
      ${isPdf ? `<p class="note">PDF 目标语言独立保存；页图翻译由模型识别原文，无需指定源语言。</p>` : `<label class="field-label" style="margin-top:10px" for="source-${surface}">源语言</label><select id="source-${surface}" data-source-language="${surface}" ${activeTask ? "disabled" : ""}>${sourceLanguageOptions(source, state.languageSearch[surface])}</select><p class="note">自动识别会在每个有候选文本的文件开始翻译前发送一次抽样预检。</p>`}
      ${isPdf ? `<div class="toggle-row"><input id="pdfReview" type="checkbox" ${record(state.settings?.pdf).review_enabled ? "checked" : ""} data-pdf-review ${activeTask ? "disabled" : ""}/><label for="pdfReview">启用逐页审核模型</label></div>` : `<div class="toggle-row"><input id="untranslated-${surface}" type="checkbox" data-untranslated ${activeTask ? "disabled" : ""}/><label for="untranslated-${surface}">仅补译未翻译内容</label></div>`}
      ${renderDetailedSettings(surface, activeTask)}
      <hr class="divider" />
      ${running ? renderRunningPanel(running, percent) : `<div class="push"><button class="button primary block large" data-action="start-task" data-surface="${surface}" ${selectedPaths.length ? "" : "disabled"}>${icon("translate", "small")}开始${surfaceLabel(surface)}翻译</button><p class="note">可执行 ${selectedPaths.length} / ${files.length} 个文件；任务启动后，日志与进度将通过 SSE 实时显示。</p></div>`}
    </aside>
  </div></section>`;
}

function renderDetailedSettings(surface: Surface, disabled: boolean): string {
  const output = record(state.settings?.output);
  const excelReview = record(state.settings?.excel_review);
  const wordBatch = record(state.settings?.word_batch);
  const wordReview = record(state.settings?.word_review);
  const pdf = record(state.settings?.pdf);
  const inputDisabled = disabled ? "disabled" : "";
  const checked = (value: unknown) => value ? "checked" : "";
  const outputMode = output.use_custom_output_dir ? "custom" : "source";
  const common = `<label class="field-label" for="output-${surface}">输出位置</label><select id="output-${surface}" data-setting-path="output.use_custom_output_dir" data-value-kind="custom-output" ${inputDisabled}><option value="source" ${outputMode === "source" ? "selected" : ""}>源目录内</option><option value="custom" ${outputMode === "custom" ? "selected" : ""}>自定义目录</option></select><input value="${escapeHtml(text(output.custom_output_dir))}" placeholder="自定义输出目录（不存在会创建）" data-setting-path="output.custom_output_dir" ${inputDisabled}/>`;
  const excel = `<div class="toggle-row"><input type="checkbox" id="keepOriginal" data-setting-path="output.keep_original_sheets" ${checked(output.keep_original_sheets)} ${inputDisabled}/><label for="keepOriginal">保留原始表格</label></div><div class="toggle-row"><input type="checkbox" id="formulaBackfill" data-setting-path="output.formula_display_value_backfill" ${checked(output.formula_display_value_backfill)} ${inputDisabled}/><label for="formulaBackfill">公式文本按显示值回填</label></div><div class="toggle-row"><input type="checkbox" id="excelAutofit" data-setting-path="output.enable_excel_autofit" ${checked(output.enable_excel_autofit)} ${inputDisabled}/><label for="excelAutofit">Excel 精调行高</label></div><div class="toggle-row"><input type="checkbox" id="lockRowHeight" data-setting-path="output.lock_row_height" ${checked(output.lock_row_height)} ${inputDisabled}/><label for="lockRowHeight">锁定行高，缩小字号</label></div><div class="toggle-row"><input type="checkbox" id="reviewMark" data-setting-path="excel_review.mark_review_items" ${checked(excelReview.mark_review_items)} ${inputDisabled}/><label for="reviewMark">标记需复核内容</label></div><label class="field-label">已有底色处理</label><select data-setting-path="excel_review.existing_fill_policy" ${inputDisabled}><option value="skip" ${text(excelReview.existing_fill_policy) === "skip" ? "selected" : ""}>不覆盖原有底色</option><option value="overwrite" ${text(excelReview.existing_fill_policy) === "overwrite" ? "selected" : ""}>覆盖原有底色</option><option value="red_font" ${text(excelReview.existing_fill_policy) === "red_font" ? "selected" : ""}>保留底色并使用红字</option></select>${renderReviewColors(inputDisabled)}`;
  const word = `<div class="toggle-row"><input type="checkbox" id="wordHighlight" data-setting-path="word_review.highlight_unresolved" ${checked(wordReview.highlight_unresolved)} ${inputDisabled}/><label for="wordHighlight">高亮未解决内容</label></div><div class="toggle-row"><input type="checkbox" id="protectSchemeCover" ${inputDisabled}/><label for="protectSchemeCover">保护封面与目录</label></div><label class="field-label">批处理段落上限</label><input type="number" min="1" data-setting-path="word_batch.max_paragraphs_per_batch" data-value-kind="number" value="${number(wordBatch.max_paragraphs_per_batch, 30)}" ${inputDisabled}/><label class="field-label">批处理字符上限</label><input type="number" min="1" data-setting-path="word_batch.max_chars_per_batch" data-value-kind="number" value="${number(wordBatch.max_chars_per_batch, 3000)}" ${inputDisabled}/>`;
  const pdfControls = `<div class="toggle-row"><input type="checkbox" id="pdfCompressed" data-setting-path="pdf.generate_compressed_pdf" ${checked(pdf.generate_compressed_pdf)} ${inputDisabled}/><label for="pdfCompressed">生成压缩 PDF</label></div><div class="toggle-row"><input type="checkbox" id="pdfImages" data-setting-path="pdf.image_translation_enabled" ${checked(pdf.image_translation_enabled)} ${inputDisabled}/><label for="pdfImages">翻译页内图片文字</label></div><label class="field-label">单页重试次数</label><input type="number" min="0" max="10" data-setting-path="pdf.page_retry_attempts" data-value-kind="number" value="${number(pdf.page_retry_attempts, 2)}" ${inputDisabled}/><label class="field-label">页图并发（留空自动）</label><input type="number" min="1" data-setting-path="pdf.page_generation_concurrency" data-value-kind="optional-number" value="${escapeHtml(text(pdf.page_generation_concurrency === null ? "" : pdf.page_generation_concurrency))}" ${inputDisabled}/>`;
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
  const colors = record(record(state.settings?.word_review).mark_colors);
  const colorField = (mark: string, label: string, fallback: string) => {
    const value = text(colors[mark], fallback).replace(/^#/, "");
    return `<label class="field-label">${label}</label><input type="color" value="#${escapeHtml(value)}" data-review-color="${mark}" ${disabled}/>`;
  };
  return `<div class="review-colors">${colorField("semantic", "语义校验接受", "FFF2CC")}${colorField("unresolved", "保留原文复核", "FCE4D6")}${colorField("foreign_noise", "疑似原文异常", "F4CCCC")}</div>`;
}

function renderRunningPanel(running: RunningTask, percent: number): string {
  const logs = running.logs.slice(-10).map((item) => `<div class="log-${logTone(item.level)}">› ${escapeHtml(item.message)}</div>`).join("");
  const terminal = running.task.terminal;
  const resultMessage = text(running.task.result?.message, terminal ? "任务已结束。" : "");
  return `<div class="push"><div class="run-summary"><span>${escapeHtml(running.phaseName || (terminal ? resultMessage : "正在准备任务"))}</span><span>${terminal && running.task.state === "done" ? "100" : percent}%</span></div><div class="progress" style="--progress:${terminal && running.task.state === "done" ? 100 : percent}%"><i></i></div><div class="logbox">${logs || (terminal ? escapeHtml(resultMessage) : "等待引擎事件…")}</div>${terminal ? `<button class="button primary block large" style="margin-top:10px" data-action="reset-task">${icon("refresh", "small")}返回并开始新任务</button>` : `<button class="button danger block large" style="margin-top:10px" data-action="stop-task">${icon("stop", "small")}终止翻译</button>`}</div>`;
}

function renderFiles(files: FileItem[], surface: Surface, selectedPaths: string[], disabled: boolean): string {
  if (!files.length) {
    return `<div class="card-pad muted">选择源路径后点击“扫描”，这里会显示可处理文件。</div>`;
  }
  return `<table><thead><tr><th class="selection-column">选择</th><th>文件名</th><th class="number">大小</th><th class="number">${surface === "pdf" ? "页数" : surface === "word" ? "段落" : "工作表"}</th></tr></thead><tbody>${files.map((file) => `<tr><td><input class="file-check" type="checkbox" data-file-path="${escapeHtml(file.path)}" data-surface="${surface}" ${selectedPaths.includes(file.path) ? "checked" : ""} ${disabled ? "disabled" : ""}/></td><td><span class="file-name">${icon(surface === "excel" ? "excel" : surface === "word" ? "word" : "pdf", "small")}${escapeHtml(file.name)}</span></td><td class="number">${number(file.size_kb).toFixed(1)} KB</td><td class="number">${surface === "pdf" ? number(file.page_count) : surface === "word" ? number(file.paragraph_count) : (file.sheets?.length ?? 0)}</td></tr>`).join("")}</tbody></table>`;
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
  if (state.modal === "stop-task") {
    return `<div class="modal-backdrop"><section class="modal"><h2>终止翻译任务？</h2><p class="note">已生成的文件会保留在输出目录；当前任务将发送安全停止请求。</p><div class="modal-actions"><button class="button" data-action="close-modal">继续执行</button><button class="button danger" data-action="stop-task-confirm">终止翻译</button></div></section></div>`;
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
  state.targetSelections.excel = text(state.settings?.excel_target_lang, text(state.settings?.target_lang, "en"));
  state.targetSelections.word = text(state.settings?.word_target_lang, text(state.settings?.target_lang, "en"));
  state.targetSelections.pdf = text(record(state.settings?.pdf).target_lang, "zh");
  state.sourceSelections.excel = text(state.settings?.excel_source_lang, "auto");
  state.sourceSelections.word = text(state.settings?.word_source_lang, "auto");
  state.tmSourceLang = text(state.settings?.tm_source_lang, "zh");
  state.tmTargetLang = text(state.settings?.tm_target_lang, "en");
  applyTheme(selectedTheme());
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

async function chooseSource(surface: Surface, directory: boolean): Promise<void> {
  const selected = await open({
    title: directory ? `选择${surfaceLabel(surface)}文件夹` : `选择${surfaceLabel(surface)}文件`,
    directory,
    multiple: false,
    filters: directory ? undefined : [{ name: surfaceLabel(surface), extensions: surface === "excel" ? ["xlsx", "xls"] : surface === "word" ? ["docx", "doc"] : ["pdf"] }],
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
  const payload = await client.request<{ items: FileItem[] }>("/api/sources/scan", {
    method: "POST",
    body: JSON.stringify({ surface, path, include_images: false }),
  });
  state.files[surface] = payload.items;
  state.selectedPaths[surface] = payload.items.map((item) => item.path);
  await persistSettings({ [`last_${surface}_source_folder`]: path });
  render();
  showToast(`已扫描到 ${payload.items.length} 个${surfaceLabel(surface)}文件。`);
}

async function startTask(surface: Surface): Promise<void> {
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
  const untranslated = Boolean(document.querySelector<HTMLInputElement>(`#untranslated-${surface}`)?.checked);
  const protectSchemeCover = Boolean(document.querySelector<HTMLInputElement>("#protectSchemeCover")?.checked);
  const includeImages = Boolean(record(state.settings?.pdf).image_translation_enabled);
  const payload = await client.request<TaskStatus>("/api/tasks", {
    method: "POST",
    body: JSON.stringify({
      surface,
      source_path: path,
      selected_paths: selectedPaths,
      untranslated_only: untranslated,
      protect_scheme_cover: protectSchemeCover,
      include_images: includeImages,
      source_lang: surface === "pdf" ? undefined : state.sourceSelections[surface],
      target_lang: state.targetSelections[surface],
    }),
  });
  state.running = { task: payload, logs: [], phaseName: "正在准备任务", stepDone: 0, stepTotal: 0 };
  render();
  try {
    await client.streamTask(payload.task_id, handleTaskEvent);
  } catch (error) {
    showToast(`任务事件流中断：${errorMessage(error)}`, true);
  }
}

function handleTaskEvent(event: SseEvent): void {
  if (!state.running) return;
  const data = event.data;
  if (event.type === "log") {
    state.running.logs.push({ level: text(data.level, "INFO"), message: text(data.message) });
  }
  if (event.type === "progress") {
    state.running.phaseName = text(data.phase_name, "正在处理");
    state.running.stepDone = number(data.step_done);
    state.running.stepTotal = number(data.step_total);
  }
  if (["done", "error", "stopped"].includes(event.type)) {
    state.running.task = { ...state.running.task, state: event.type as TaskStatus["state"], terminal: true, result: data };
    if (event.type === "done") {
      showToast("翻译任务已完成。输出目录可在诊断归档中查看。");
    } else {
      showToast(text(data.message, "任务未完成。"), true);
    }
  }
  render();
}

async function stopTask(): Promise<void> {
  if (!state.running) return;
  state.running.task = await client.request<TaskStatus>(`/api/tasks/${state.running.task.task_id}/stop`, { method: "POST" });
  render();
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
  const colors = record(record(state.settings?.word_review).mark_colors);
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
    const payload = await client.request<{ suggestions: JsonObject[] }>("/api/tm/clean", { method: "POST", body: JSON.stringify({ lang_pair: tmLangPair() }) });
    state.tmSuggestions = payload.suggestions;
    state.tmCleaningState = "ready";
    state.modal = "tm-clean";
    render();
  } catch (error) {
    state.tmCleaningState = "error";
    render();
    throw error;
  }
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
  if (target.dataset.settingPath) {
    const kind = target.dataset.valueKind;
    let value: string | number | boolean | null;
    if (kind === "custom-output") value = target.value === "custom";
    else if (kind === "number") value = Number(target.value);
    else if (kind === "optional-number") value = target.value.trim() ? Number(target.value) : null;
    else if (target instanceof HTMLInputElement && target.type === "checkbox") value = target.checked;
    else value = target.value;
    void saveSettingPath(target.dataset.settingPath, value).catch((error) => showToast(errorMessage(error), true));
  }
  if (target.dataset.reviewColor) {
    void saveReviewColor(target.dataset.reviewColor, target.value).catch((error) => showToast(errorMessage(error), true));
  }
  if (target.dataset.setting === "domain_preset") {
    void persistSettings({ domain_preset: target.value }).then(render).catch((error) => showToast(errorMessage(error), true));
  }
  if (target.dataset.target) void saveLanguage(target.dataset.target as Surface, target.value).catch((error) => showToast(errorMessage(error), true));
  if (target.dataset.sourceLanguage !== undefined) void saveSourceLanguage(target.dataset.sourceLanguage as Surface, target.value).catch((error) => showToast(errorMessage(error), true));
  if (target.dataset.pdfReview !== undefined) void savePdfReview((target as HTMLInputElement).checked).catch((error) => showToast(errorMessage(error), true));
});

app.addEventListener("input", (event) => {
  const target = event.target as HTMLInputElement;
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
  if (action === "navigate" && target.dataset.view) { state.view = target.dataset.view as View; if (state.view === "tm") { await refreshTmLanguagePairs(); await refreshTm(); await refreshTmConflicts(); } render(); return; }
  if (action === "toggle-panel") { state.panelOpen = !state.panelOpen; await persistSettings({ appearance: { model_config_panel_open: state.panelOpen } }); render(); return; }
  if (action === "cycle-theme") { const next = selectedTheme() === "system" ? "light" : selectedTheme() === "light" ? "dark" : "system"; await persistSettings({ appearance: { theme: next } }); render(); return; }
  if (action === "choose-source" && surface) { state.sourcePickerSurface = surface; state.modal = "source-picker"; render(); return; }
  if (action === "choose-source-file" && surface) { await chooseSource(surface, false); state.sourcePickerSurface = null; state.modal = null; render(); return; }
  if (action === "choose-source-folder" && surface) { await chooseSource(surface, true); state.sourcePickerSurface = null; state.modal = null; render(); return; }
  if (action === "scan" && surface) return scan(surface);
  if (action === "start-task" && surface) return startTask(surface);
  if (action === "stop-task") { state.modal = "stop-task"; render(); return; }
  if (action === "stop-task-confirm") { state.modal = null; return stopTask(); }
  if (action === "reset-task") { state.running = null; render(); return; }
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
  if (action === "close-modal") { state.modal = null; state.sourcePickerSurface = null; state.tmEditing = null; state.customLanguageEditing = null; state.modalNotice = null; state.tmImportPreview = null; state.tmFullImportPreview = null; state.modelImportPreview = null; render(); return; }
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
  } catch (error) {
    showToast(`无法连接翻译引擎：${errorMessage(error)}`, true);
  }
  render();
}

void bootstrap();
