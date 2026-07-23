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
type Modal = "tm-add" | "tm-edit" | "tm-delete" | "tm-clean" | "custom-language" | "migration" | "source-picker" | "stop-task" | "notice" | "update" | null;
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
  const engine = engineSettings();
  const cloudMode = text(engine.mode, "cloud") === "cloud";
  const provider = cloudMode
    ? text(engine.cloud_provider, "custom_openai")
    : text(engine.local_provider, "ollama");
  const baseUrl = cloudMode
    ? text(engine.cloud_base_url)
    : text(engine.local_base_url);
  const model = cloudMode
    ? text(engine.cloud_model)
    : text(engine.local_model);
  const providers = cloudMode
    ? ["custom_openai", "openai", "claude", "zhipu", "dashscope", "siliconflow"]
    : ["ollama", "lm_studio", "custom_local"];
  return `<aside class="config-panel ${state.panelOpen ? "" : "closed"}">
    <div class="config-inner">
      <div class="config-header"><div class="config-icon">${icon("sliders", "small")}</div><div><h2>模型配置</h2><p>全局 · 应用于所有页面</p></div><button class="icon-button" data-action="toggle-panel" data-tip="折叠模型配置">${icon("chevron", "small")}</button></div>
      <div class="config-body">
        <div class="config-group">
          <label class="field-label" for="domainPreset">专业领域</label>
          <input id="domainPreset" value="${escapeHtml(text(state.settings?.domain_preset, "同步工程场景"))}" data-setting="domain_preset" />
        </div>
        <div class="config-group">
          <span class="field-label">接入方式</span>
          <select id="engineMode" data-engine="mode"><option value="cloud" ${cloudMode ? "selected" : ""}>云端 API</option><option value="local" ${cloudMode ? "" : "selected"}>本地模型</option></select>
          <label class="field-label" for="provider" style="margin-top:9px">${cloudMode ? "服务商" : "本地运行器"}</label>
          <select id="provider" data-engine="cloud_provider">
            ${providers.map((item) => `<option value="${item}" ${provider === item ? "selected" : ""}>${providerLabel(item)}</option>`).join("")}
          </select>
          <label class="field-label" for="baseUrl" style="margin-top:9px">Base URL</label>
          <input id="baseUrl" value="${escapeHtml(baseUrl)}" placeholder="https://.../v1" data-engine="cloud_base_url" />
          <label class="field-label" for="modelName" style="margin-top:9px">模型名称</label>
          <input id="modelName" value="${escapeHtml(model)}" placeholder="例如 ${cloudMode ? "gpt-4o-mini" : "qwen2.5:7b"}" data-engine="cloud_model" />
          ${cloudMode ? `<label class="field-label" for="apiKey" style="margin-top:9px">API Key</label><input id="apiKey" type="password" placeholder="留空则保留当前密钥" />` : `<p class="note">本地模型不保存云端 API Key；请确认本地服务已启动。</p>`}
          <div class="field-row" style="margin-top:10px"><button class="button" data-action="save-model">${icon("check", "small")}保存</button><button class="button" data-action="fetch-models">获取模型</button><button class="button" data-action="test-model">测试连接</button></div>
        </div>
        <div class="config-group">
          <span class="field-label">配置文件</span>
          <div class="field-row"><button class="button" data-action="export-model-config">导出</button><button class="button" data-action="import-model-config">导入</button></div>
        </div>
        <div class="config-group">
          <span class="field-label">维护</span>
          <div class="field-row"><button class="button" data-action="download-diagnostics">诊断归档</button><button class="button" data-action="migration">数据迁移</button></div>
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
  return `<section class="view active"><div class="left-column">
    <div class="card source-bar"><div class="source-icon">${icon("search")}</div><div class="source-meta"><div class="source-key">搜索</div><input class="source-input" id="tmKeyword" value="${escapeHtml(state.tmKeyword)}" placeholder="按原文或译文筛选" /></div><button class="button" data-action="tm-search">搜索</button><button class="button" data-action="tm-add">${icon("plus", "small")}新增</button><button class="button" data-action="tm-import">导入</button><button class="button" data-action="tm-export">导出</button><button class="button primary" data-action="tm-clean">${icon("sparkle", "small")}深度清洗</button></div>
    <div class="stats">${stat("memory", "总条目", String(number(stats.total)))}${stat("pin", "已固定", String(number(stats.pinned)))}${stat("check", "手动维护", String(number(stats.manual)))}${stat("translate", "未固定", String(number(stats.unpinned)))}</div>
    <div class="card table-card"><div class="table-header"><h2>记忆条目</h2><span class="table-count">${state.tmEntries.length} 条</span></div><div class="table-scroll">${renderTmEntries()}</div></div>
  </div></section>`;
}

function renderTmEntries(): string {
  if (!state.tmEntries.length) {
    return `<div class="card-pad muted">当前语言对没有记忆条目。</div>`;
  }
  return `<table><thead><tr><th>原文</th><th>译文</th><th>来源</th><th class="number">操作</th></tr></thead><tbody>${state.tmEntries.map((entry) => `<tr><td>${escapeHtml(entry.source_text)}</td><td>${escapeHtml(entry.target_text)}</td><td class="muted">${escapeHtml(entry.word_type)}</td><td class="number"><span class="table-actions"><button class="mini-button" data-action="tm-edit" data-entry-id="${entry.id}">编辑</button><button class="pin-button ${entry.pinned ? "pinned" : ""}" data-action="tm-pin" data-entry-id="${entry.id}" data-pinned="${entry.pinned ? "0" : "1"}" data-tip="${entry.pinned ? "解除固定" : "固定词条"}">${icon("pin", "small")}</button><button class="mini-button danger-text" data-action="tm-delete" data-entry-id="${entry.id}">删除</button></span></td></tr>`).join("")}</tbody></table>`;
}

function renderModal(): string {
  if (state.modal === "custom-language") {
    const editing = state.customLanguageEditing;
    return `<div class="modal-backdrop"><section class="modal"><h2>${editing ? "编辑自定义语言" : "新增自定义目标语言"}</h2><p class="note">自定义语言只能作为目标语言使用；内部代码创建后不可变。</p><label class="field-label" for="customLanguageName">显示名称</label><input id="customLanguageName" value="${escapeHtml(editing?.display_name)}" ${editing ? "disabled" : ""} autofocus/><label class="field-label" for="customLanguageDescription" style="margin-top:10px">语言说明</label><textarea id="customLanguageDescription">${escapeHtml(editing?.description)}</textarea><div class="modal-actions"><button class="button" data-action="close-modal">取消</button>${editing ? `<button class="button danger" data-action="custom-language-delete">删除</button>` : ""}<button class="button primary" data-action="${editing ? "custom-language-update" : "custom-language-create"}">保存</button></div></section></div>`;
  }
  if (state.modal === "tm-add" || state.modal === "tm-edit") {
    const editing = state.tmEditing;
    const isEdit = state.modal === "tm-edit";
    return `<div class="modal-backdrop"><section class="modal"><h2>${isEdit ? "编辑记忆条目" : "新增记忆条目"}</h2><label class="field-label" for="tmSource">原文</label><input id="tmSource" autofocus value="${escapeHtml(editing?.source_text)}" /><label class="field-label" for="tmTarget" style="margin-top:10px">译文</label><input id="tmTarget" value="${escapeHtml(editing?.target_text)}" /><label class="field-label" for="tmPair" style="margin-top:10px">语言对</label><input id="tmPair" value="${escapeHtml(tmLangPair())}" ${isEdit ? "disabled" : ""}/><div class="modal-actions"><button class="button" data-action="close-modal">取消</button><button class="button primary" data-action="${isEdit ? "tm-update" : "tm-create"}">保存</button></div></section></div>`;
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
  return `${text(state.settings?.source_lang, "zh")}-${text(state.settings?.target_lang, "en")}`;
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
  applyTheme(selectedTheme());
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

async function refreshTm(): Promise<void> {
  const payload = await client.request<{ entries: TmEntry[]; stats: JsonObject }>(
    `/api/tm/entries?lang_pair=${encodeURIComponent(tmLangPair())}&keyword=${encodeURIComponent(state.tmKeyword)}`,
  );
  state.tmEntries = payload.entries;
  state.tmStats = payload.stats;
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

async function saveReviewColor(mark: string, color: string): Promise<void> {
  const colors = record(record(state.settings?.word_review).mark_colors);
  await persistSettings({ word_review: { mark_colors: { ...colors, [mark]: color.replace("#", "").toUpperCase() } } });
  render();
}

async function saveModel(): Promise<void> {
  const mode = inputValue("engineMode", "cloud");
  const provider = inputValue("provider", mode === "cloud" ? "custom_openai" : "ollama");
  const baseUrl = inputValue("baseUrl");
  const model = inputValue("modelName");
  const key = inputValue("apiKey");
  const engine = mode === "cloud"
    ? { mode, cloud_provider: provider, cloud_base_url: baseUrl, cloud_model: model }
    : { mode, local_provider: provider, local_base_url: baseUrl, local_model: model };
  await persistSettings({ engine });
  if (key && mode === "cloud") {
    await client.request(`/api/keys/${provider}`, { method: "PUT", body: JSON.stringify({ api_key: key, base_url: baseUrl }) });
  }
  render();
  showToast("模型配置已保存。密钥仅写入本机密钥存储。" );
}

async function fetchModels(): Promise<void> {
  const result = await client.request<{ models: string[]; message: string }>("/api/models/fetch", {
    method: "POST",
    body: JSON.stringify({ provider: inputValue("provider", "custom_openai"), base_url: inputValue("baseUrl"), api_key: inputValue("apiKey") }),
  });
  showToast(result.models.length ? `可用模型：${result.models.slice(0, 6).join("、")}` : result.message);
}

async function testModel(): Promise<void> {
  const result = await client.request<{ ok: boolean; message: string }>("/api/models/connectivity/text", { method: "POST" });
  showToast(result.message, !result.ok);
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

async function tmCreate(): Promise<void> {
  await client.request("/api/tm/entries", {
    method: "POST",
    body: JSON.stringify({ source_text: inputValue("tmSource"), target_text: inputValue("tmTarget"), lang_pair: inputValue("tmPair", tmLangPair()) }),
  });
  state.modal = null;
  await refreshTm();
  render();
  showToast("记忆条目已保存，并同步反向语言对。" );
}

async function tmUpdate(): Promise<void> {
  if (!state.tmEditing) return;
  await client.request(`/api/tm/entries/${state.tmEditing.id}`, {
    method: "PUT",
    body: JSON.stringify({ source_text: inputValue("tmSource"), target_text: inputValue("tmTarget") }),
  });
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
  const payload = await client.request<{ suggestions: JsonObject[] }>("/api/tm/clean", { method: "POST", body: JSON.stringify({ lang_pair: tmLangPair(), overwrite: false }) });
  state.tmSuggestions = payload.suggestions;
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
  state.modal = null;
  await refreshTm();
  render();
  showToast(`已写入 ${result.applied} 条清洗建议。`);
}

async function exportTm(): Promise<void> {
  const payload = await client.request<JsonObject>(`/api/tm/export?lang_pair=${encodeURIComponent(tmLangPair())}`);
  downloadJson(`translator-tm-${tmLangPair()}.json`, payload);
  state.modalNotice = { title: "记忆库已导出", message: "导出文件已交给系统下载目录。请妥善保存含有专有术语的文件。" };
  state.modal = "notice";
  render();
}

async function importTm(): Promise<void> {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "application/json,.json";
  input.onchange = async () => {
    const file = input.files?.[0];
    if (!file) return;
    const payload = JSON.parse(await file.text()) as JsonObject;
    const entries = Array.isArray(payload.entries) ? payload.entries : [];
    const langPair = text(payload.lang_pair, tmLangPair());
    const result = await client.request<{ inserted?: number; skipped?: number; overwritten?: number }>("/api/tm/import", {
      method: "POST",
      body: JSON.stringify({ lang_pair: langPair, mode: "skip", entries }),
    });
    await refreshTm();
    state.modalNotice = { title: "记忆库已导入", message: `已按“跳过重复项”导入：新增 ${number(result.inserted)}，跳过 ${number(result.skipped)}。` };
    state.modal = "notice";
    render();
  };
  input.click();
}

async function exportModelConfig(): Promise<void> {
  const payload = await client.request<JsonObject>("/api/model-config/export");
  downloadJson("translator-model-config.json", payload);
  state.modalNotice = { title: "模型配置已导出", message: "文件可能包含 API Key，请保存到受保护的位置。" };
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
    const payload = JSON.parse(await file.text()) as JsonObject;
    const result = await client.request<{ imported_key_count: number }>("/api/model-config/import", { method: "POST", body: JSON.stringify(payload) });
    await refreshSettings();
    state.modalNotice = { title: "模型配置已导入", message: `已恢复模型设置与 ${result.imported_key_count} 个密钥作用域。` };
    state.modal = "notice";
    render();
  };
  input.click();
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
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
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
  void handleAction(target).catch((error) => showToast(errorMessage(error), true));
});

app.addEventListener("change", (event) => {
  const target = event.target as HTMLInputElement | HTMLSelectElement;
  if (target.id === "engineMode") {
    state.settings = {
      ...(state.settings ?? {}),
      engine: { ...engineSettings(), mode: target.value },
    };
    render();
    return;
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
  if (action === "navigate" && target.dataset.view) { state.view = target.dataset.view as View; if (state.view === "tm") await refreshTm(); render(); return; }
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
  if (action === "tm-search") { state.tmKeyword = inputValue("tmKeyword"); await refreshTm(); render(); return; }
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
  if (action === "tm-export") return exportTm();
  if (action === "close-modal") { state.modal = null; state.sourcePickerSurface = null; state.tmEditing = null; state.customLanguageEditing = null; state.modalNotice = null; render(); return; }
  if (action === "export-model-config") return exportModelConfig();
  if (action === "import-model-config") return importModelConfig();
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
    await refreshTm();
  } catch (error) {
    showToast(`无法连接翻译引擎：${errorMessage(error)}`, true);
  }
  render();
}

void bootstrap();
