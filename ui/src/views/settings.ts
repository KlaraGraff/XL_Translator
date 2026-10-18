// 设置视图 —— 五个子页：模型服务 / 翻译参数 / 外观与语言 / 数据与维护 / 更新与关于。
// 收编自旧版 ui/src/main.ts 的左侧模型配置抽屉 + 详细设置折叠组 + 维护与诊断页 +
// 更新检查弹层。main.ts 保持只读，仅供本文件对照业务语义，不被 import。
// params 约定：{ page?: "models" | "params" | "appearance" | "data" | "about" }，
// 用于从右栏「编辑 Prompt ↗ 设置」、顶栏模型药丸等处深链到具体子页。

import { PALETTES, applyPalettes, palettesFromAppearance, storePalettes, type PaletteChoice, type PaletteMode } from "../palette";
import { navigate, type ViewParams } from "../router";
import { setTopbar } from "../shell";
import {
  createChip,
  createButton,
  createField,
  createSelectField,
  createDropdown,
  createLanguageField,
  createSwitch,
  createSegmented,
  createFold,
  createHintBadge,
  createEmptyState,
  createBanner,
  createProgressBar,
  closeLanguagePopover,
  closeMenu,
  openMenu,
  hideHint,
  openModal,
  showToast,
  clearElement,
  type ChipTone,
  type LanguageOption,
  type ModalHandle,
} from "../components";
import { icon, type IconName } from "../icons";
import { ApiClient, ApiError } from "../api-client";
import { saveJsonFile } from "../save-file";
import { showQuickStart } from "../quickstart";
import { motionAllowed, riseIn, withColorFade } from "../motion";
import { applyModelPillFromRoles, setModelPillSettingsRole } from "../model-pill";
import { renderReleaseNotes, releaseNotesLineCount } from "../markdown";
// 更新的状态和动作都在 update-controller：顶部的更新提示卡片和这一页显示的是同一件事，
// 各存一份就会出现「卡片在下载、这一页还写着有可用更新」。这一页只负责画和触发。
import {
  canSelfUpdate,
  collapseUpdateReady,
  ensureUpdaterEnvironment,
  ignoreVersion,
  ignoredVersion,
  loadUpdateState,
  notificationsPaused,
  requestRestart,
  requestUpdateInstall,
  runUpdateCheck,
  selfUpdateBlockedCopy,
  setNotificationsPaused,
  subscribeUpdates,
  updateSnapshot,
} from "../update-controller";
import { version as PACKAGE_VERSION } from "../../package.json";
import {
  exportDiagnostics,
  exportDocumentConfig,
  importDocumentConfig,
  openDocumentImportPreviewModal,
  pickJsonFile,
  renderModelsPage,
} from "./settings-models";
import { refreshRateOverview, renderRateSection, renderTmParamsCard } from "./settings-rate";
import "./settings.css";
import { docPromptAnatomy, openPromptAnatomy, pdfPromptAnatomy, promptAnatomyButton, type PromptAnatomy } from "./prompt-anatomy";
import { localizedLanguageOptions } from "./workspace-util";
import {
  extSurfaceLabel,
  isExtSurface,
  loadSurfaceDescriptors,
  orderedTypeKeys,
  surfaceDescriptor,
  surfaceDescriptors,
  type ExtSurface,
} from "../surface-descriptors";
import { LOCALES, currentLocale, fmtNum, isLocale, localeTag, setLocale, storeLocale, t, tx, type Locale, localized } from "../i18n";

// ---------------------------------------------------------------------------
// 类型（本地精简版，对照 main.ts 的同名类型/字段）
// ---------------------------------------------------------------------------

export type JsonObject = Record<string, unknown>;
export type SettingsPage = "models" | "params" | "appearance" | "data" | "about";
// 运行时注册的类型（格式适配器，键为 `ext:<id>`）每种一个页签，标签用类型名，排在哪里跟
// 「扩展」页的顺序一致（被隐藏的也保留页签）。paramsTab 直接记类型键。
type TranslationSurface = "excel" | "word" | "cad" | ExtSurface;
export type ParamsSurface = "excel" | "word" | "pdf" | "cad" | ExtSurface;

export type PoolConnection = {
  id: string;
  label: string;
  display_label: string;
  connection_mode?: "cloud" | "local" | "follow";
  source_role?: string;
  own_provider?: string;
  own_model?: string;
  own_base_url?: string;
  own_api_mode?: string;
  own_cloud_provider?: string;
  own_cloud_model?: string;
  own_cloud_base_url?: string;
  own_cloud_api_mode?: string;
  own_local_provider?: string;
  own_local_model?: string;
  own_local_base_url?: string;
  provider: string;
  model: string;
  base_url: string;
  api_mode?: string;
  availability_status: string;
  availability_message: string;
  availability_checked_at: string;
  // 这条连接眼下用不了的原因（服务商不支持本用途所需能力，或跟随了本地模型），没有问题时为空。
  config_problem?: string;
  has_api_key: boolean;
  api_key_preview: string;
  primary: boolean;
  // 这条连接自己的速率（后端按连接 + 模型分档）。旧版后端不回这两项，界面退回角色级那份。
  throughput?: { profile_key: string; batch_size: number | null; concurrency: number };
  throughput_bounds?: { batch_size: [number, number] | null; concurrency: [number, number] | null };
};

type MaintenanceCategoryInfo = {
  id: string;
  label: string;
  size_bytes?: number;
  count?: number;
  clearable?: boolean;
  contains_user_output?: boolean;
};

type MaintenanceClearCategory = "task_history" | "logs" | "diagnostics" | "keys" | "settings" | "tm" | "full_reset";

// core/config_crypto.py 的五种解封状态：unsealed=正常解开；plaintext=旧版明文文件（含「不含
// Key」导出重新导入的边界情况，此时 sealed_key_count 恒为 0）；expired=密钥已过期；
// unsupported=本机软件版本太旧，解不开内置密钥；corrupt=文件损坏或被篡改（这一种在预览请求
// 阶段就会被后端拒成 422，走 openImportCorruptModal，不会进到这个类型里）。
type SealStatus = "unsealed" | "plaintext" | "expired" | "unsupported" | "corrupt";

export type ModelImportPreview = {
  fileName: string;
  payload: JsonObject;
  roles: { role: string; fields: string[] }[];
  throughput_profile_count: number;
  api_key_count: number;
  seal_status: SealStatus;
  sealed: boolean;
  expires_at: string | null;
  sealed_key_count: number;
  legacy_plaintext: boolean;
};

// GET /api/model-config/export 的回执：逐条说明每个连接的密钥去向。
// exported = 跟着导出了；withheld_imported = 有密钥但它是从别人的配置导入来的，
// 为避免连环传播没有导出；missing = 这条连接本来就没配密钥。
type ApiKeyExportStatus = "exported" | "withheld_imported" | "missing";

export type ApiKeyExportRow = {
  role: string;
  role_label: string;
  connection: string;
  provider?: string;
  status: ApiKeyExportStatus;
};

type ApiKeyExportReport = {
  include_api_key: boolean;
  connections: ApiKeyExportRow[];
  // 角色记住的其他服务商配置里被扣下的密钥。它们在界面上不是一条连接，
  // 但密钥确实少写了一把，回执里不能一个字不提。
  provider_memories?: ApiKeyExportRow[];
  exported_count: number;
  withheld_count: number;
  missing_count: number;
  withheld_provider_memory_count?: number;
};

// GET /api/model-config/export 的整体响应。document 是唯一写进文件的部分；sealed/
// expires_at 只用来渲染本机的导出回执，不影响写盘内容。
export type ModelConfigExportResponse = {
  document: JsonObject;
  sealed: boolean;
  expires_at: string | null;
  // 密封前数出来的密钥处数，和收件人导入时看到的作用域数是同一个数。
  sealed_key_count: number;
  api_key_report: ApiKeyExportReport;
};

// GET /api/data/health 的字段名是另一路改动定死的契约，这里原样对照，不要改名。
type DataHealthEntry = {
  state: "current" | "adopted" | "recreated" | "upgraded" | "unreadable" | string;
  // 全新安装时磁盘上还没有文件，服务端这里给的是 null。
  stored_version: number | null;
  current_version: number;
  backup_path: string;
  reason?: string;
};
type DataHealthPayload = {
  settings: DataHealthEntry;
  tm: DataHealthEntry;
  keys: DataHealthEntry;
  task_history?: DataHealthEntry;
};

// ---------------------------------------------------------------------------
// 小工具（对照 main.ts 的 record/text/number/errorMessage/nestedPatch）
// ---------------------------------------------------------------------------

export function record(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}

export function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function num(value: unknown, fallback = 0): number {
  return typeof value === "number" ? value : fallback;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
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

function formatBytes(value: unknown): string {
  const bytes = Math.max(0, num(value));
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

export function formatCheckedAt(value: string): string {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return t("测试于 {0}", new Intl.DateTimeFormat(localeTag(), { dateStyle: "short", timeStyle: "short" }).format(parsed));
}

// ---------------------------------------------------------------------------
// 内置翻译规则：唯一事实来源是后端 GET /api/domains/builtin-prompts（config.py 的
// CORE_TRANSLATION_PROMPT）。这里绝不能自己存一份硬拷贝——前端那份一过期，设置页
// 显示的就不是真正发给模型的规则（高-14）。corePrompt 由 refreshSettings() 每次刷新，
// 拿不到时保持上一次的值。
// ---------------------------------------------------------------------------

let corePrompt = "";

async function refreshDomainBuiltinPrompts(): Promise<void> {
  try {
    const payload = await client.request<{ core_prompt?: string }>("/api/domains/builtin-prompts");
    corePrompt = typeof payload.core_prompt === "string" ? payload.core_prompt : corePrompt;
  } catch (error) {
    // 保留上一次成功拿到的值：这只是一次刷新失败，不代表内置文本变了。
    console.warn("内置翻译规则读取失败，本次沿用上一次的缓存：", error); // i18n-ignore 控制台日志，不面向用户
  }
}

// 下拉里「＋ 新增补充要求」的占位值。
const ADD_PROMPT_PRESET = "__add_prompt__";
// 补充要求选「无」时存的值（同 config.NO_EXTRA_PROMPT），显示走 tx()。
const NO_EXTRA_PROMPT = "无"; // i18n-ignore 发回后端的值
// 补充要求不能叫这些名字（同 config.RESERVED_EXTRA_PROMPT_NAMES）：「无」是空选项，
// 「自定义」是 V9 旧版的标记值，其余是 V10 以前的场景名，旧数据迁移时会用到。
const RESERVED_EXTRA_NAMES = new Set(["无", "自定义", "自动识别场景", "同步工程场景", "资料管理场景", "行政生活化场景"]); // i18n-ignore 与后端保留名比较

export const MODEL_ROLE_LABELS: Record<string, string> = localized(() => ({
  translation: t("文档翻译（Excel / Word / CAD）"),
  cleaner: t("记忆库清洗"),
  image: t("PDF 与图片翻译"),
  pdf_review: t("PDF 与图片审核"),
}));
export const MODEL_ROLE_ORDER = ["translation", "cleaner", "image", "pdf_review"];
export const FOLLOW_PREFIX = "follow:";

export const CLOUD_PROVIDERS = ["custom_openai", "openai", "claude", "zhipu", "dashscope", "siliconflow", "deepseek"];
export const LOCAL_PROVIDERS = ["ollama", "lm_studio", "custom_local"];
const PROVIDER_LABELS: Record<string, string> = localized(() => ({
  custom_openai: t("OpenAI 兼容"),
  openai: "OpenAI",
  claude: "Claude",
  zhipu: t("智谱 GLM"),
  dashscope: t("阿里百炼"),
  siliconflow: t("硅基流动"),
  deepseek: "DeepSeek",
  ollama: "Ollama",
  lm_studio: "LM Studio",
  custom_local: t("自定义本地服务"),
}));
export function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}

// 服务商切换时的 Base URL 预填表，来自 GET /api/models/provider-defaults。这里不留
// 第二份硬编码：URL 一旦在前后端各存一份，服务商换了端点就会有一边是错的，而错的那
// 一边正好是用户看得见的那份。接口没回来之前预填表为空 —— 只是不预填，不会挡住手填。
export let providerBaseUrlDefaults: Record<string, string> = {};
export let providerModelDefaults: Record<string, string> = {};
export let providerBaseUrlDisabled = new Set<string>();
// 存后端原文，显示时再翻译：切换界面语言后跟着变。
let disabledBaseUrlPlaceholderSource = "当前服务商无需填写 Base URL"; // i18n-ignore 后端原文，显示时由 tx() 翻译
export function disabledBaseUrlPlaceholder(): string {
  return tx(disabledBaseUrlPlaceholderSource);
}

// 本地运行器的 Base URL 预填表。上面「不留第二份硬编码」的约定只对云端成立——
// /api/models/provider-defaults 下发的是 CLOUD_PROVIDER_BASE_URL_DEFAULTS，本地运行器
// 一个都不发。没有这张表，「本地模型」下把运行器从 Ollama 换成 LM Studio 时 Base URL
// 会原样停在 Ollama 的端口上，还会被失焦自动保存原样存进去，之后每次测试连接都失败，
// 而界面上没有任何地方提示地址不对。值照抄 config.py 的 OLLAMA_BASE_URL /
// LM_STUDIO_BASE_URL；custom_local 的端点本来就要用户自己填，没有预设。
export const LOCAL_PROVIDER_BASE_URL_DEFAULTS: Record<string, string> = {
  ollama: "http://localhost:11434",
  lm_studio: "http://localhost:1234/v1",
};

const MAINTENANCE_CONFIRM_COPY: Record<MaintenanceClearCategory, { title: string; message: string; confirm: string }> = localized(() => ({
  task_history: { title: t("清空任务记录？"), message: t("将删除任务中心里的任务记录，不会删除任何翻译结果。"), confirm: t("清空任务记录") },
  logs: { title: t("清空运行日志？"), message: t("将删除 Translator 的运行日志，不会删除诊断记录、源文件或翻译结果。"), confirm: t("清空日志") },
  diagnostics: { title: t("清空全部诊断？"), message: t("将删除所有本地诊断记录。已导出的 ZIP 文件不会被删除。"), confirm: t("清空诊断") },
  keys: { title: t("删除全部 API 密钥？"), message: t("将删除 Translator 保存的所有服务商密钥。有任务在运行时不能执行此操作。"), confirm: t("删除全部密钥") },
  settings: { title: t("重置设置？"), message: t("将恢复默认设置，API 密钥、记忆库和翻译结果都会保留。"), confirm: t("重置设置") },
  tm: { title: t("清空记忆库？"), message: t("此操作不会删除翻译结果。建议先在记忆库页面导出一份完整备份。"), confirm: t("清空记忆库") },
  full_reset: { title: t("完整重置本地数据？"), message: t("仅删除 Translator 自身保存的数据；应用本身、源文件和翻译结果均不会被删除。"), confirm: t("完整重置") },
}));

/** 按钮上的动词要和点开后弹窗的标题一致：以前列表里写「清理」，弹窗却问「重置设置？」。 */
function maintenanceVerb(category: string): string {
  if (category === "settings") return t("重置");
  if (category === "keys") return t("删除");
  return t("清空");
}

// ---------------------------------------------------------------------------
// 模块状态（单例：设置页同一时刻只会挂载一份）
// ---------------------------------------------------------------------------

export const client = new ApiClient();
let connectPromise: Promise<void> | null = null;
export async function ensureConnected(): Promise<void> {
  if (!connectPromise) {
    const attempt = client.connect();
    // 失败不进缓存：connect() 里那记 /health 撞上还没起来的后端就 reject，缓存住之后
    // 这个视图的每一个请求都会立刻失败，而且再也不会自己好，只能重启 app。
    // （library.ts / tasks.ts 里的同名函数本来就是这么写的，这里补齐。）
    attempt.catch(() => {
      if (connectPromise === attempt) connectPromise = null;
    });
    connectPromise = attempt;
  }
  return connectPromise;
}

let bodyHost: HTMLElement | null = null;
let bannerHost: HTMLElement | null = null;
let navEls: Map<SettingsPage, HTMLButtonElement> | null = null;
// 左栏选中项下面那块会滑的淡蓝底，和应用侧栏的 .side-ind 同一种。
let navIndicator: HTMLElement | null = null;
let currentPage: SettingsPage = "models";
export let mountToken = 0;
let dataHealth: DataHealthPayload | null = null;

export let settings: JsonObject | null = null;
export let modelRoles: Record<string, JsonObject> = {};
export let modelCatalog: Record<string, string[]> = {};
export let modelCatalogMessage: Record<string, string> = {};
export let modelCatalogConnection: Record<string, string> = {};
export let selectedConnection: Record<string, string> = {};
export let modelAccessDraft: Record<string, string> = {};
export let modelRole = "translation";
export const modelFormDrafts: Record<string, ModelFormDraft> = {};
export let modelImportPreview: ModelImportPreview | null = null;

// 模型服务子页拆到了 settings-models.ts。ES 模块只能读别的模块的 let 绑定、不能给它
// 赋值，所以那边要改下面这几个状态时走这些 setter。
export function setModelRole(role: string): void {
  modelRole = role;
}
export function setModelImportPreview(preview: ModelImportPreview | null): void {
  modelImportPreview = preview;
}
export function setApplyProviderDerivedState(apply: (value: string) => void): void {
  applyProviderDerivedState = apply;
}
export function setSubmitModelForm(submit: (() => Promise<void>) | null): void {
  submitModelForm = submit;
}

let targetOptions: LanguageOption[] = [];
let sourceOptions: LanguageOption[] = [];

/** update-controller 的退订函数；unmount 时必须调用，否则每挂载一次就多一个订阅者，
 *  下载进度会把已经摘掉的 DOM 重画一遍。 */
let unsubscribeUpdates: (() => void) | null = null;

let maintenanceOverview: JsonObject | null = null;
let diagnostics: JsonObject[] = [];
let diagnosticsOverview: JsonObject | null = null;
let maintenanceLoaded = false;
let activeTaskCount = 0;

// TM 语言对候选：后端没有「列出实际存在 TM 数据的语言对」的端点，
// /api/tm/language-pairs 的 recent 字段就是 settings.recent_tm_lang_pairs
// （main.ts:2204 写入的同一份数据），据此复现「清空所选语言对」下拉。
let tmPairCatalog: { source_options: LanguageOption[]; target_options: LanguageOption[]; recent: string[] } | null = null;
let tmPairCatalogLoaded = false;
let selectedTmClearPair = "";

let paramsTab: ParamsSurface = "excel";
let pendingDomainPreset: string | null = null;
let pendingNewPrompt = false;

// ---------------------------------------------------------------------------
// mount / unmount
// ---------------------------------------------------------------------------

export function mount(container: HTMLElement, params: ViewParams): void {
  const token = ++mountToken;
  const requestedPage = params.page;
  const requestedRole = typeof params.role === "string" && MODEL_ROLE_ORDER.includes(params.role) ? params.role : null;
  const requestedSurface = params.paramsSurface;
  if (requestedSurface === "excel" || requestedSurface === "word" || requestedSurface === "pdf" || requestedSurface === "cad"
    || (typeof requestedSurface === "string" && isExtSurface(requestedSurface))) {
    paramsTab = requestedSurface;
  }
  pendingDomainPreset = typeof params.domainPreset === "string" ? params.domainPreset : null;
  pendingNewPrompt = pendingDomainPreset === NO_EXTRA_PROMPT;
  if (requestedRole) modelRole = requestedRole;
  currentPage = requestedPage === "models" || requestedPage === "params" || requestedPage === "appearance"
    || requestedPage === "data" || requestedPage === "about"
    ? requestedPage
    : "models";
  setModelPillSettingsRole(currentPage === "models" ? modelRole : null);

  setTopbar({
    title: t("设置"),
    subtitle: t("模型、翻译参数、外观与数据"),
  });

  const nav = document.createElement("div");
  nav.className = "set-nav";
  nav.setAttribute("role", "tablist");
  nav.setAttribute("aria-orientation", "vertical");
  const indicator = document.createElement("span");
  indicator.className = "set-ind";
  indicator.setAttribute("aria-hidden", "true");
  nav.append(indicator);
  navIndicator = indicator;
  const navMap = new Map<SettingsPage, HTMLButtonElement>();
  for (const item of NAV_ITEMS) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "si";
    el.setAttribute("role", "tab");
    const label = document.createElement("span");
    label.className = "si-lb";
    label.textContent = item.label;
    // 法文的子页名较长，196px 放不下时省略，悬停看全名。
    label.title = item.label;
    el.append(icon(item.icon), label);
    el.addEventListener("click", () => {
      if (currentPage === item.id) return;
      currentPage = item.id;
      setModelPillSettingsRole(currentPage === "models" ? modelRole : null);
      highlightNav();
      void loadAndRenderPage(token);
    });
    navMap.set(item.id, el);
    nav.append(el);
  }
  navEls = navMap;

  const body = document.createElement("div");
  body.className = "set-body";
  bodyHost = body;

  // .content（父容器的类）本来是一行 flex（nav + body 并排）。数据恢复横幅要
  // 铺满页面顶部、盖在 nav 和 body 之上，所以这里把 container 改成纵向 flex，
  // 原来那行 nav+body 移进一个新的行容器里，横幅作为纵向的第一个子项。
  // unmount 时不用手动复原：router.ts 每次挂载视图前都会先 removeAttribute("style")。
  container.style.display = "flex";
  container.style.flexDirection = "column";
  container.style.gap = "12px";
  container.style.minHeight = "0";

  const banner = document.createElement("div");
  bannerHost = banner;

  // 左 196px 子导航 + 右侧按节排列的正文（流光原型 .set）。版式在 settings.css 的 .set-row。
  const row = document.createElement("div");
  row.className = "set-row";
  row.append(nav, body);

  container.append(banner, row);
  highlightNav();
  // 挂上以后才量得到各项位置；第一次放到位不带过渡（不然会从顶端滑下来），下一帧才加 .ready。
  requestAnimationFrame(() => {
    if (navIndicator !== indicator) return;
    moveNavIndicator();
    if (motionAllowed()) requestAnimationFrame(() => indicator.classList.add("ready"));
  });

  clearElement(body);
  body.append(createEmptyState({ title: t("正在加载设置…"), icon: "gear" }));

  // 更新状态是共享的：顶部卡片上按的「更新」会一路改到这一页要显示的东西（下载进度、
  // 装好等重启、失败）。订阅着重画，才不会出现「卡片在下载、这一页还写着有可用更新」。
  unsubscribeUpdates = subscribeUpdates(renderAboutIfVisible);

  void bootstrap(token);
}

/** 在设置页内跳到另一个子页（模型服务页的「前往翻译参数」用）。 */
export function openSettingsPage(page: SettingsPage, surface?: ParamsSurface): void {
  if (surface) paramsTab = surface;
  currentPage = page;
  setModelPillSettingsRole(currentPage === "models" ? modelRole : null);
  highlightNav();
  void loadAndRenderPage(mountToken);
}

/** 原地重画时回到当前子页（切换界面语言后仍停在「外观与语言」）。 */
export function remountParams(): ViewParams {
  return { page: currentPage, role: modelRole, paramsSurface: paramsTab };
}

export function unmount(): void {
  mountToken += 1; // 让任何仍在飞行中的异步回调失效
  // 同 workspace：这些浮层挂在 document.body 上，视图切走不会自动消失。
  hideHint();
  closeLanguagePopover();
  closeMenu();
  // 离开设置页 = 放弃还没提交的「连接方式」草稿。这份草稿是模块级状态，不清就会跨挂载
  // 活下来：用户切到「本地模型」没保存、走开、过一会儿回来只改了个模型名，失焦自动保存
  // 会连着那份陈年草稿一起交上去，把角色静默切成本地模式——用户从头到尾没同意过这件事。
  // 放在 unmount 而不是 mount：清理必须发生在**离开**的那一刻。放在 mount 里，草稿会在
  // 页面不可见期间一直挂着，此间任何一次从别处触发的保存仍会把它带上去。
  modelAccessDraft = {};
  bodyHost = null;
  bannerHost = null;
  navEls = null;
  navIndicator = null;
  renderedPage = null;
  setModelPillSettingsRole(null);
  unsubscribeUpdates?.();
  unsubscribeUpdates = null;
}

const NAV_ITEMS: { id: SettingsPage; label: string; icon: IconName }[] = localized(() => ([
  { id: "models", label: t("模型服务"), icon: "gear" },
  { id: "params", label: t("翻译参数"), icon: "doc-file" },
  { id: "appearance", label: t("外观与语言"), icon: "book" },
  { id: "data", label: t("数据与维护"), icon: "folder" },
  { id: "about", label: t("更新与关于"), icon: "help" },
]));

function highlightNav(): void {
  if (!navEls) return;
  for (const [id, el] of navEls) {
    el.classList.toggle("on", id === currentPage);
    el.setAttribute("aria-selected", String(id === currentPage));
  }
  moveNavIndicator();
}

function moveNavIndicator(): void {
  const on = navEls?.get(currentPage);
  if (!navIndicator || !on) return;
  navIndicator.style.transform = `translateY(${on.offsetTop}px)`;
  navIndicator.style.height = `${on.offsetHeight}px`;
  navIndicator.style.opacity = "1";
}

/**
 * 页签、用途卡换了以后整页会重画：页签那一节原地不动，只让它下面换掉的内容浮现
 * （同「扩展」页换页签）。带 .rise-skip 的节（翻译参数里不跟页签走的记忆库）也不动。
 */
export function riseBelow(anchorSelector: string): void {
  const anchor = bodyHost?.querySelector(anchorSelector);
  if (!anchor) return;
  const below: Element[] = [];
  for (let el = anchor.nextElementSibling; el; el = el.nextElementSibling) {
    if (!el.classList.contains("rise-skip")) below.push(el);
  }
  riseIn(below);
}

async function bootstrap(token: number): Promise<void> {
  void refreshDataHealth(token); // 不阻塞主流程；接口不存在或失败时静默不显示横幅
  try {
    await ensureConnected();
    await refreshSettings();
    await refreshLanguages();
    await loadUpdateState();
    if (token !== mountToken) return;
    await loadAndRenderPage(token);
  } catch (error) {
    if (token !== mountToken) return;
    if (!bodyHost) return;
    clearElement(bodyHost);
    bodyHost.append(createEmptyState({ title: t("无法连接本地翻译引擎"), description: errorMessage(error), icon: "warn" }));
  }
}

// GET /api/data/health / DELETE /api/data/health/notice：另一路改动的接口契约，字段
// 名称不能改。接口暂时可能还不存在（404/连接失败都算），失败就静默不显示横幅——不弹
// toast，因为这条横幅本来就是「有异常才出现」，接口缺失不算异常。
export async function refreshDataHealth(token: number): Promise<void> {
  try {
    await ensureConnected();
    if (token !== mountToken) return;
    const result = await client.request<DataHealthPayload>("/api/data/health");
    if (token !== mountToken) return;
    dataHealth = result;
  } catch {
    if (token !== mountToken) return;
    dataHealth = null;
  }
  // 挂载令牌变了说明页面已经被换掉，横幅要挂的宿主元素已经不是当前那个了。
  if (token !== mountToken) return;
  renderDataHealthBanner();
}

// 消息带着自己的作用域走：「知道了」只清横幅上真正展示过的作用域，
// 后端按这份清单删事件（scope 字符串与 /api/data/health 的字段名一致）。
type DataHealthMessage = { scope: keyof DataHealthPayload; text: string };

function dataHealthRecreatedMessages(payload: DataHealthPayload | null): DataHealthMessage[] {
  if (!payload) return [];
  const messages: DataHealthMessage[] = [];
  if (payload.task_history?.state === "recreated") {
    messages.push({
      scope: "task_history",
      text: t("旧的任务历史已损坏，原文件已备份到 {0}；已恢复可用记录，无法读取的记录未能保留。", payload.task_history.backup_path || t("备份目录")),
    });
  }
  if (payload.settings?.state === "recreated") {
    messages.push({
      scope: "settings",
      text: t("旧配置无法读取，已备份到 {0}，并已新建可用配置。", payload.settings.backup_path || t("备份目录")),
    });
  }
  if (payload.tm?.state === "recreated") {
    messages.push({
      scope: "tm",
      text: t("旧的翻译记忆库无法读取，已备份到 {0}，并已新建可用的记忆库。", payload.tm.backup_path || t("备份目录")),
    });
  }
  if (payload.keys?.state === "recreated") {
    // 和 settings/tm 不同：这条恢复的代价用户必须自己补——旧文件里其余服务商
    // 的 Key 已经跟着坏文件一起没了，新文件目前只有本次保存的这一把。
    messages.push({
      scope: "keys",
      text: t("旧的 API 密钥文件已损坏，已备份到 {0}；其他服务商的密钥未能保留，请重新填写。", payload.keys.backup_path || t("备份目录")),
    });
  }
  return messages;
}

// unreadable：文件还在，但系统不让打开（权限，或被杀毒/备份软件占着）。什么都没丢，
// 但也什么都存不进去——不报出来的话，用户看到的是一个正常的设置页，然后每次保存都失败。
function dataHealthBlockedMessages(payload: DataHealthPayload | null): string[] {
  if (!payload) return [];
  const messages: string[] = [];
  if (payload.task_history?.state === "unreadable") {
    messages.push(tx(payload.task_history.reason) || t("任务历史无法安全恢复，原文件未被覆盖，新的任务历史暂时无法保存。"));
  }
  if (payload.settings?.state === "unreadable") {
    messages.push(t("配置文件无法读取，本次将以默认设置运行，更改无法保存。原文件未被覆盖。"));
  }
  if (payload.tm?.state === "unreadable") {
    messages.push(t("翻译记忆库无法读取，本次翻译将不使用记忆库。原文件未被删除。"));
  }
  if (messages.length) {
    messages.push(t("通常是权限问题，或文件被杀毒、备份软件占用：请关闭占用文件的程序后重启本应用；如确定放弃旧数据，可在「数据与维护」中重置。"));
  }
  return messages;
}

function renderDataHealthBanner(): void {
  if (!bannerHost) return;
  clearElement(bannerHost);
  const blocked = dataHealthBlockedMessages(dataHealth);
  const recreated = dataHealthRecreatedMessages(dataHealth);
  const messages = blocked.length ? blocked : recreated.map((item) => item.text);
  if (!messages.length) return;
  // blocked 优先在屏时 recreated 一条都没露面，此时一个作用域都不清——
  // 没展示过的恢复事件必须留到它露面的那天，不能被「知道了」顺手抹掉。
  const shownScopes = blocked.length ? [] : recreated.map((item) => item.scope);
  // .banner 默认是任务完成用的绿色底色；这条是警示，挂 .warn 换成 app.css 里那套黄色。
  const banner = createBanner({
    title: blocked.length ? t("有数据暂时无法读取") : t("有数据被重新创建"),
    subtitle: messages.join(" "),
    icon: "warn",
    actions: [
      createButton({
        label: t("知道了"), size: "mini",
        onClick: () => void (async () => {
          const token = mountToken;
          try {
            await client.request("/api/data/health/notice", {
              method: "DELETE", body: JSON.stringify({ scopes: shownScopes }),
            });
          } catch (error) {
            if (token === mountToken) showToast({ message: errorMessage(error), error: true });
            return;
          }
          if (token !== mountToken) return;
          dataHealth = null;
          renderDataHealthBanner();
        })(),
      }),
    ],
  });
  banner.classList.add("warn");
  bannerHost.append(banner);
}

async function loadAndRenderPage(token: number): Promise<void> {
  try {
    // 速率的默认值和上限跟着文档翻译模型是云端还是本地走，每次进这一页都重新取。
    if (currentPage === "params") {
      await refreshRateOverview();
    }
    if (currentPage === "data" && !maintenanceLoaded) {
      await refreshMaintenance();
    }
    if (currentPage === "data" && !tmPairCatalogLoaded) {
      await refreshTmPairCatalog();
    }
    if (currentPage === "data") {
      await refreshActiveTaskCount();
    }
    // 「下载并安装」这个按钮能不能出现，取决于这份安装能不能自己替换自己。探测一次
    // 就够了——安装位置不会在应用运行期间变。
    if (currentPage === "about") {
      await ensureUpdaterEnvironment();
    }
  } catch (error) {
    if (token !== mountToken) return;
    showToast({ message: errorMessage(error), error: true });
  }
  if (token !== mountToken) return;
  renderBody();
}

// 上一次 renderBody 画的是哪个子页，用来判断这次重画要不要保留滚动位置。
let renderedPage: SettingsPage | null = null;

export function renderBody(): void {
  if (!bodyHost) return;
  // 同一子页里的重画（保存、测试、切换连接……）保持滚动位置。以前整页清空再重建后
  // 滚动位置不受控制，点「保存配置」后页面停在别处，连接列表那一行被顶部截掉一半。
  // 换了子页才回到顶部。
  const keepScroll = renderedPage === currentPage ? bodyHost.scrollTop : 0;
  const pageChanged = renderedPage !== currentPage;
  clearElement(bodyHost);
  // 上一轮渲染留下的表单钩子指向的是已经被 clearElement 摘掉的 DOM。模型页会在下面
  // 重新接上；其他页面就该是空的，否则「获取模型列表」那条路径可能提交一份幽灵表单。
  submitModelForm = null;
  applyProviderDerivedState = () => {};
  switch (currentPage) {
    case "models": renderModelsPage(bodyHost); break;
    case "params": renderParamsPage(bodyHost); break;
    case "appearance": renderAppearancePage(bodyHost); break;
    case "data": renderDataPage(bodyHost); break;
    case "about": renderAboutPage(bodyHost); break;
  }
  renderedPage = currentPage;
  bodyHost.scrollTop = keepScroll;
  // 换了子页，正文整块从下方淡入，和应用侧栏换页同一种；同一页里保存、测试后的重画不播，
  // 否则每点一下都闪一遍。以前只有模型服务页的四张用途卡错落浮现，别的子页没有动画。
  if (pageChanged) riseIn(bodyHost, "page");
}

// 模型表单里几个「点了按钮但没提交表单」的输入框——获取模型列表 / 测试连接这类操作
// 之前会靠 renderBody() 把它们连同用户刚敲的字一起重建成服务端状态。这里按 id 存草稿、
// 重画后填回去；表单真正提交时（保存配置）应该改显示服务端回填的最新值，所以那条路径
// 用 { preserveDraft: false } 跳过。
//
// 草稿连同「它属于哪条连接」一起存（scope）。有些按钮（设为主用、新增、删除）会让重画
// 后的表单换成另一条连接，这时把草稿填回去等于把 A 的 Base URL 和刚粘进去的密钥搬到
// B 头上，下一次「保存配置」就真写进 B 了。scope 对不上就整份丢弃。
const MODEL_FORM_DRAFT_FIELD_IDS = [
  "settings-model-provider",
  "settings-model-base-url",
  "settings-model-name",
  "settings-model-connection-label",
  "settings-model-api-key",
];
const MODEL_FORM_SCOPE_FIELD_ID = "settings-model-provider";

// 由模型表单在渲染时接上；页面不是模型页时是空操作。
export let applyProviderDerivedState: (value: string) => void = () => {};

// 同上，由模型表单渲染时接上：把当前表单里的值提交给后端。
// 「获取模型列表」「测试连接」和字段失焦自动保存都走它——这三处以前用的都是**服务端
// 已保存的那份配置**，用户刚改完密钥点「测试连接」，测的是改之前那把旧密钥，永远失败。
export let submitModelForm: (() => Promise<void>) | null = null;
// 表单提交串行化：失焦自动保存和按钮触发的保存可能挨得很近（点按钮先触发 blur 再触发
// click），两个 PUT 并行发出去时后端最后落哪份取决于响应顺序。所有提交排进同一条链。
let modelFormSaveChain: Promise<void> = Promise.resolve();

export function queueModelFormSave(run: () => Promise<void>): Promise<void> {
  const next = modelFormSaveChain.then(run, run);
  // 链上任何一环失败都不能让后续提交全部短路，所以这里吞掉错误只用于排队；
  // 错误照常沿 next 抛给真正的调用方处理。
  modelFormSaveChain = next.catch(() => undefined);
  return next;
}

// 一次提交所需的全部信息：输入框里的值，加上「这份表单是在哪个角色、哪种连接方式、
// 选中哪条连接的情况下填的」。后者在渲染时就固定下来，排队等待期间界面怎么变都不影响
// 这一份该怎么存。
export type ModelFormSubmission = {
  role: string;
  access: string;
  sourceRole: string;
  secondaryId: string;
  selectedId: string;
  provider: string;
  baseUrl: string;
  model: string;
  apiKey: string;
  connectionLabel: string;
  apiMode: string;
};

type ModelFormDraft = { scope: string; values: Record<string, string> };

function modelFormScope(): string | null {
  const el = document.getElementById(MODEL_FORM_SCOPE_FIELD_ID) as HTMLSelectElement | null;
  return el?.dataset.formScope ?? null;
}

export function snapshotModelFormDraft(): ModelFormDraft | null {
  const scope = modelFormScope();
  if (scope === null) return null;
  const values: Record<string, string> = {};
  for (const id of MODEL_FORM_DRAFT_FIELD_IDS) {
    const el = document.getElementById(id) as HTMLInputElement | HTMLSelectElement | null;
    if (el) values[id] = el.value;
  }
  return { scope, values };
}

export function restoreModelFormDraft(draft: ModelFormDraft): void {
  if (modelFormScope() !== draft.scope) return;
  for (const [id, value] of Object.entries(draft.values)) {
    const el = document.getElementById(id) as HTMLInputElement | HTMLSelectElement | null;
    if (el) {
      el.value = value;
      // Shared dropdowns keep a hidden select as the compatibility value
      // bridge. Dispatching change repaints its visible trigger when a draft
      // is restored by assigning the DOM value directly.
      if (el instanceof HTMLSelectElement) el.dispatchEvent(new Event("change"));
    }
  }
  // 服务商是草稿的一部分，Base URL 的禁用态和占位符却是重画时按服务端值算的，
  // 两者会对不上（切到智谱后点「获取模型列表」，回来 Base URL 又变成可填了）。
  // 按填回去的服务商重放一次，让联动状态跟草稿一致。
  const provider = draft.values[MODEL_FORM_SCOPE_FIELD_ID];
  if (provider !== undefined) applyProviderDerivedState(provider);
}

// 注意：调用方几乎都以 `void reRenderAfter(...)` 触发（不等待返回值），因此这里
// 内部吞掉错误并转成 toast，不再向外 rethrow —— 否则会在控制台产生一堆无人处理的
// unhandled promise rejection（toast 已经把错误讲给用户了，rethrow 没有实际接收方）。
export async function reRenderAfter<T>(
  action: () => Promise<T>,
  opts: { preserveDraft?: boolean; rerenderOnError?: boolean } = {},
): Promise<T | undefined> {
  const token = mountToken;
  const draft = opts.preserveDraft === false ? null : snapshotModelFormDraft();
  try {
    // 点按钮的顺序是 blur → click：失焦自动保存已经发出去了，动作必须排在它后面，
    // 否则「改完密钥直接点删除/切连接」会变成两个请求抢同一条记录。
    await modelFormSaveChain;
    const result = await action();
    if (token === mountToken) {
      renderBody();
      if (draft) restoreModelFormDraft(draft);
    }
    return result;
  } catch (error) {
    if (token === mountToken) {
      showToast({ message: friendlyErrorMessage(error), error: true });
      // 失败重绘只对「输入框只装得下已落盘值、重画等于把界面拉回磁盘真值」的
      // 调用点开启（数值框 + PDF 并发框，由调用方显式传 rerenderOnError: true，
      // 中-14）。其余调用点（尤其是领域 Prompt 这类可能装着几百字未提交草稿的
      // 文本框）默认不重绘：重绘会把 renderBody() 读到的模块状态（对文本框来说
      // 就是 current.customPrompt 等旧值）整份糊回输入框，用户刚敲的内容会在
      // 后端短暂不可用时被无声吞掉，这是本函数曾经引入的回归（REG-1），不是
      // 「表单看着像存上但没存」的中-14 场景本身。
      if (opts.rerenderOnError) {
        renderBody();
        if (draft) restoreModelFormDraft(draft);
      }
    }
    return undefined;
  }
}

// 后端把 pydantic ValidationError 直接 str() 塞进 422 的 detail：一整段英文栈式
// 报错，看不出具体是哪个字段、允许范围是多少（中-14）。真正挡住越界提交靠
// numberField/pdf 并发输入框自己的 clamp，这里只是兜底——真撞上了，至少给一句
// 看得懂的中文，而不是原样甩出 pydantic 的内部文本。
function friendlyErrorMessage(error: unknown): string {
  const raw = error instanceof ApiError ? error.rawMessage : errorMessage(error);
  if (/validation error/i.test(raw)) {
    return t("保存失败：填写的值超出允许范围，已恢复为上次保存的值，请检查后重新输入。");
  }
  return errorMessage(error);
}

// ---------------------------------------------------------------------------
// 数据获取 / 持久化（对照 main.ts 的 refreshSettings / persistSettings 等）
// ---------------------------------------------------------------------------

export async function refreshSettings(): Promise<void> {
  settings = await client.request<JsonObject>("/api/settings");
  await refreshProviderDefaults();
  await refreshModelRoles();
  await refreshDomainBuiltinPrompts();
  // 这里是「刚从后端拿到权威值」的唯一入口，顺手把可见主题和 localStorage
  // 缓存按后端值对齐一次（中-17-残留）。不这样做的话，「重置设置」之类不经过
  // 主题分段控件、直接把后端 theme 改回默认值的路径（core/maintenance.py 的
  // reset_settings()）不会触发 applyThemeVisual/commitThemeToLocalStorage，
  // 界面和 localStorage 会永久停在重置前的旧主题上；已经因为历史 bug 分叉过的
  // 旧安装也永远不会自愈。落盘失败的路径不受影响：那条路径上 settings 从未被
  // 赋新值，这里读到的 pref 仍是回滚前的旧值，方向和 applyThemeVisual(previous)
  // 一致，不会把失败的半成品主题写进 localStorage。
  const pref = currentThemePreference();
  applyThemeVisual(pref);
  commitThemeToLocalStorage(pref);
  // 配色方案同理：重置设置会把它改回默认方案，这里跟着对齐。
  const palettes = currentPalettePreference();
  applyPalettes(palettes);
  storePalettes(palettes);
}

async function refreshProviderDefaults(): Promise<void> {
  // 拿不到就不预填，绝不因此把整个设置页变成错误页——预填是便利，手填才是主路径。
  try {
    const payload = await client.request<{
      base_url_defaults: Record<string, string>;
      model_defaults?: Record<string, string>;
      base_url_disabled: string[];
      disabled_placeholder: string;
    }>("/api/models/provider-defaults");
    providerBaseUrlDefaults = payload.base_url_defaults || {};
    providerModelDefaults = payload.model_defaults || {};
    providerBaseUrlDisabled = new Set(payload.base_url_disabled || []);
    if (payload.disabled_placeholder) disabledBaseUrlPlaceholderSource = String(payload.disabled_placeholder);
  } catch (error) {
    providerBaseUrlDefaults = {};
    providerModelDefaults = {};
    providerBaseUrlDisabled = new Set();
    console.warn("服务商预设读取失败，本次不预填 Base URL：", error); // i18n-ignore 控制台日志，不面向用户
  }
}

export async function refreshModelRoles(): Promise<void> {
  const payload = await client.request<{ roles: Record<string, JsonObject> }>("/api/models/roles");
  modelRoles = payload.roles || {};
  // 顶栏药丸跟着同一份数据走：保存、切换连接、测试连通性之后都会经过这里。
  applyModelPillFromRoles(modelRoles);
  // 速率随角色数据一起回来（每条连接各一份），不再为每个角色单独问一次主用连接的速率。
}

async function refreshLanguages(): Promise<void> {
  const payload = await client.request<{
    languages: LanguageOption[];
    source_options: LanguageOption[];
    target_options: LanguageOption[];
  }>("/api/languages");
  targetOptions = Array.isArray(payload.target_options) ? payload.target_options : [];
  sourceOptions = Array.isArray(payload.source_options) ? payload.source_options : [];
}

async function refreshMaintenance(): Promise<void> {
  const [overview, diag] = await Promise.all([
    client.request<JsonObject>("/api/maintenance/overview"),
    client.request<JsonObject>("/api/diagnostics"),
  ]);
  maintenanceOverview = overview;
  diagnostics = Array.isArray(diag.records)
    ? diag.records.filter((item): item is JsonObject => Boolean(item) && typeof item === "object")
    : [];
  diagnosticsOverview = record(diag.overview);
  maintenanceLoaded = true;
}

async function refreshTmPairCatalog(): Promise<void> {
  try {
    const payload = await client.request<{
      source_options: LanguageOption[];
      target_options: LanguageOption[];
      selected?: { source_lang?: string; target_lang?: string };
      recent?: string[];
    }>("/api/tm/language-pairs");
    tmPairCatalog = {
      source_options: Array.isArray(payload.source_options) ? payload.source_options : [],
      target_options: Array.isArray(payload.target_options) ? payload.target_options : [],
      recent: Array.isArray(payload.recent) ? payload.recent.filter((pair): pair is string => typeof pair === "string") : [],
    };
    if (!selectedTmClearPair) {
      const selected = payload.selected;
      const defaultPair = selected?.source_lang && selected?.target_lang ? `${selected.source_lang}-${selected.target_lang}` : "";
      selectedTmClearPair = tmPairCatalog.recent[0] ?? defaultPair;
    }
  } catch {
    tmPairCatalog = null;
  }
  tmPairCatalogLoaded = true;
}

function tmPairLabel(pair: string): string {
  const [source, ...targetParts] = pair.split("-");
  const target = targetParts.join("-");
  const sourceLabel = tx(tmPairCatalog?.source_options.find((option) => option.code === source)?.display_name ?? source);
  const targetLabel = tx(tmPairCatalog?.target_options.find((option) => option.code === target)?.display_name ?? target);
  return `${sourceLabel} → ${targetLabel}`;
}

async function refreshActiveTaskCount(): Promise<void> {
  try {
    const list = await client.listTasks();
    activeTaskCount = list.active.length;
  } catch {
    activeTaskCount = 0;
  }
}

export async function persistSettings(patch: JsonObject): Promise<void> {
  settings = await client.request<JsonObject>("/api/settings", {
    method: "PUT",
    body: JSON.stringify(patch),
  });
}

export async function saveSettingPath(path: string, value: string | number | boolean | null): Promise<void> {
  await persistSettings(nestedPatch(path, value));
}

// ---------------------------------------------------------------------------
// 共享小组件
// ---------------------------------------------------------------------------

export function sectionLabel(labelText: string): HTMLDivElement {
  const div = document.createElement("div");
  div.className = "rp-sec";
  div.textContent = labelText;
  return div;
}

export function hintBadge(hint: string): HTMLSpanElement {
  return createHintBadge(hint, "swrow-hint");
}

export function fieldWithHint(labelText: string, control: HTMLElement, hint?: string): HTMLDivElement {
  const field = createField(labelText, control);
  if (hint) {
    const label = field.querySelector("label");
    if (label) label.append(hintBadge(hint));
  }
  return field;
}

export function selectField(
  labelText: string,
  options: { value: string; label: string }[],
  value: string,
  onChange: (value: string) => void,
  opts: { disabled?: boolean; hint?: string } = {},
): { root: HTMLDivElement; select: HTMLSelectElement } {
  const field = createSelectField({
    label: labelText,
    options,
    value,
    disabled: opts.disabled,
    hint: opts.hint,
    onChange,
  });
  return field;
}

export function textField(
  labelText: string,
  value: string,
  onCommit: (value: string) => void,
  opts: { placeholder?: string; disabled?: boolean; hint?: string; type?: string } = {},
): { root: HTMLDivElement; input: HTMLInputElement } {
  const input = document.createElement("input");
  input.type = opts.type ?? "text";
  input.value = value;
  if (opts.placeholder) input.placeholder = opts.placeholder;
  input.disabled = Boolean(opts.disabled);
  input.addEventListener("change", () => onCommit(input.value));
  const root = fieldWithHint(labelText, input, opts.hint);
  return { root, input };
}

export function numberField(
  labelText: string,
  value: number,
  onCommit: (value: number) => void,
  opts: { min?: number; max?: number; disabled?: boolean; hint?: string; type?: string; integer?: boolean } = {},
): HTMLDivElement {
  const input = document.createElement("input");
  input.type = opts.type ?? "number";
  input.value = String(value);
  if (opts.min !== undefined) input.min = String(opts.min);
  if (opts.max !== undefined) input.max = String(opts.max);
  if (opts.integer && input.type === "number") input.step = "1";
  input.disabled = Boolean(opts.disabled);
  input.addEventListener("change", () => {
    const parsed = Number(input.value);
    if (Number.isNaN(parsed) || (opts.integer && !Number.isInteger(parsed))) return;
    // 越界的值贴着边界夹一下再提交：HTML 的 min/max 属性只影响浏览器的上下箭头
    // 和表单自带校验提示，手动输入或粘贴超界数字照样能触发 change 事件。不夹
    // 的话，这个数字会原样发去后端，换来一段 pydantic 英文 422（中-14）。这里
    // 用的 min/max 就是调用方按 config.py 传进来的后端真实边界，夹完立即回填
    // 输入框，让界面显示的值和即将落盘的值一致。
    // 边界从输入框自己的 min/max 属性现读，不用创建时的 opts：调用方可能在保存
    // 后改上限（模型页换了连接方式，云端 20 变本地 5；解锁后上限取消）。
    let clamped = parsed;
    if (input.min !== "") clamped = Math.max(Number(input.min), clamped);
    if (input.max !== "") clamped = Math.min(Number(input.max), clamped);
    if (clamped !== parsed) input.value = String(clamped);
    onCommit(clamped);
  });
  return fieldWithHint(labelText, input, opts.hint);
}

export function fieldRow(children: HTMLElement[]): HTMLDivElement {
  const row = document.createElement("div");
  row.className = "field-row";
  row.append(...children);
  return row;
}

export function connRow(children: (HTMLElement | string)[], opts: { onClick?: () => void; selected?: boolean } = {}): HTMLDivElement {
  const row = document.createElement("div");
  row.className = opts.selected ? "conn selected" : "conn";
  for (const child of children) {
    row.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  if (opts.onClick) {
    row.style.cursor = "pointer";
    row.addEventListener("click", opts.onClick);
  }
  return row;
}

export function tcHead(title: string, tools: HTMLElement[] = []): HTMLDivElement {
  const head = document.createElement("div");
  head.className = "tc-head";
  const b = document.createElement("b");
  b.textContent = title;
  head.append(b);
  if (tools.length) {
    const toolsEl = document.createElement("div");
    toolsEl.className = "tc-tools";
    toolsEl.append(...tools);
    head.append(toolsEl);
  }
  return head;
}

/**
 * 设置页的一节（流光原型 .sec）：节与节之间只有一条发丝线，不再是一张张卡片。
 * 有标题、说明或右上角按钮时画 .sh 头；body 是这一节的内容区，调用方往里放。
 */
export function sec(opts: { title?: string; desc?: string; actions?: HTMLElement[]; className?: string } = {}): { root: HTMLDivElement; body: HTMLDivElement } {
  const root = document.createElement("div");
  root.className = opts.className ? `sec ${opts.className}` : "sec";
  if (opts.title || opts.desc || opts.actions?.length) {
    const head = document.createElement("div");
    head.className = "sh";
    const copy = document.createElement("div");
    copy.className = "sh-copy";
    if (opts.title) {
      const h3 = document.createElement("h3");
      h3.textContent = opts.title;
      copy.append(h3);
    }
    if (opts.desc) {
      const desc = document.createElement("div");
      desc.className = "desc";
      desc.textContent = opts.desc;
      copy.append(desc);
    }
    head.append(copy);
    if (opts.actions?.length) {
      const acts = document.createElement("div");
      acts.className = "acts";
      acts.append(...opts.actions);
      head.append(acts);
    }
    root.append(head);
  }
  const body = document.createElement("div");
  body.className = "sec-b";
  root.append(body);
  return { root, body };
}

/** 一行选项（流光原型 .opt-row）：左边标题和一句灰色说明，右边开关、分段或按钮。 */
export function optRow(opts: { title: string; desc?: string; hint?: string; control: HTMLElement | HTMLElement[]; className?: string }): HTMLDivElement {
  const row = document.createElement("div");
  row.className = opts.className ? `opt-row ${opts.className}` : "opt-row";
  const lb = document.createElement("div");
  lb.className = "lb";
  const title = document.createElement("span");
  title.className = "lb-t";
  title.textContent = opts.title;
  if (opts.hint) title.append(hintBadge(opts.hint));
  lb.append(title);
  if (opts.desc) {
    const small = document.createElement("small");
    small.textContent = opts.desc;
    lb.append(small);
  }
  const ctl = document.createElement("div");
  ctl.className = "ctl";
  ctl.append(...(Array.isArray(opts.control) ? opts.control : [opts.control]));
  row.append(lb, ctl);
  return row;
}

/** 一行灰色说明小字（「修改后自动保存。」这类）。 */
export function secNote(textContent: string): HTMLParagraphElement {
  const note = document.createElement("p");
  note.className = "sec-note";
  note.textContent = textContent;
  return note;
}

// 折叠块的开合状态。这一页几乎每次保存都会整页重画，折叠块的局部状态一重建就丢；
// 按 key 记在这里，用户展开的「内置规则」不会在改了一下补充要求后自己收起来。
const foldOpen: Record<string, boolean> = {};

export function settingsFold(key: string, opts: { title: string; meta?: string; content: HTMLElement; open?: boolean }): HTMLDivElement {
  return createFold({
    ...opts,
    open: foldOpen[key] ?? Boolean(opts.open),
    onToggle: (open) => { foldOpen[key] = open; },
  }).root;
}

/**
 * 把「模型名称」输入框包成一个可下拉的组合框：输入框照旧可以随便打字，右侧箭头把候选
 * 展开成锚定菜单。候选优先用刚获取到的模型列表；还没获取时退回服务商的推荐型号，让这
 * 个箭头在新建连接的第一分钟就有东西可给——否则它只是个点了没反应的装饰。
 */
export function attachModelNameDropdown(
  input: HTMLInputElement,
  // 每次点开时现取：表单里换了服务商或地址，之前获取的列表就不再属于它。
  currentCatalog: () => string[],
  currentProvider: () => string,
): void {
  const host = input.parentElement;
  if (!host) return;
  const wrap = document.createElement("div");
  wrap.className = "combo";
  host.insertBefore(wrap, input);
  wrap.append(input);

  const caret = document.createElement("button");
  caret.type = "button";
  caret.className = "combo-caret";
  caret.setAttribute("aria-label", t("展开模型候选"));
  caret.append(icon("chev", { size: "sm" }));
  wrap.append(caret);

  const candidates = (): { list: string[]; fallback: boolean } => {
    const catalog = currentCatalog();
    if (catalog.length) return { list: catalog, fallback: false };
    const recommended = providerModelDefaults[currentProvider()];
    return recommended ? { list: [recommended], fallback: true } : { list: [], fallback: false };
  };

  const paint = () => {
    const { list } = candidates();
    caret.disabled = input.disabled || !list.length;
    caret.title = caret.disabled
      ? t("尚无候选模型。请点击下方的「获取模型列表」从服务商获取。")
      : t("从候选中选择");
  };
  paint();
  // 服务商换了、输入框禁用态变了，可选项跟着变；没有事件能覆盖全部来源，直接在每次
  // 点开前重算一遍最省事，也不会漏。
  caret.addEventListener("pointerenter", paint);

  caret.addEventListener("click", () => {
    const { list, fallback } = candidates();
    if (!list.length) return;
    openMenu(
      // 锚在整条输入框而不是箭头上：openMenu 按 anchor 的左边缘定位，锚在右侧的小
      // 箭头上会把一条和输入框同宽的菜单顶到窗口外面去。
      wrap,
      list.map((item) => ({
        label: item,
        description: fallback ? t("服务商推荐型号") : undefined,
        onSelect: () => {
          input.value = item;
          input.dispatchEvent(new Event("change", { bubbles: true }));
        },
      })),
      { width: input.getBoundingClientRect().width, maxHeight: 280 },
    );
  });
}

export function statusChip(label: string, tone: "done" | "error" | ""): HTMLSpanElement {
  const chipTone: ChipTone | undefined = tone === "done" ? "ok" : tone === "error" ? "warn" : "mute";
  return createChip({ label, tone: chipTone });
}


// ---------------------------------------------------------------------------
// 子页②：翻译参数
// ---------------------------------------------------------------------------

function excelReviewSettings(): JsonObject {
  return record(settings?.excel_review);
}
function wordReviewSettings(): JsonObject {
  return record(settings?.word_review);
}
function pdfParamsSettings(): JsonObject {
  return record(settings?.pdf);
}

/** settings.extensions[<id>]：运行时注册的类型的那一份设置；没存过时为空对象（按后端默认值显示）。 */
function extParams(surface: ExtSurface): JsonObject {
  return record(record(settings?.extensions)[surface.slice("ext:".length)]);
}

function domainSettingsFor(surface: TranslationSurface): {
  smartContext: boolean;
  preset: string;
  customPrompts: Record<string, Record<string, string>>;
} {
  // 内置页面是平铺的 `<页面>_smart_context` 等字段；运行时注册的类型在 extensions[<id>] 里，字段名不带前缀。
  const read = (name: string): unknown => (isExtSurface(surface) ? extParams(surface)[name] : settings?.[`${surface}_${name}`]);
  // 补充要求是「名称 → {_base: 正文}」；这里保留嵌套读取，兼容旧版按目标语言保存的
  // 配置，保存时由后端统一折叠回 `_base`。
  return {
    smartContext: read("smart_context") !== false,
    preset: text(read("domain_preset"), NO_EXTRA_PROMPT),
    customPrompts: Object.fromEntries(
      Object.entries(record(read("domain_custom_prompts"))).map(([name, langs]) => [
        name,
        Object.fromEntries(
          Object.entries(record(langs)).filter((e): e is [string, string] => typeof e[1] === "string"),
        ),
      ]),
    ),
  };
}

function targetLangForDomain(surface: TranslationSurface): string {
  if (isExtSurface(surface)) return text(extParams(surface).target_lang, "en");
  const key = surface === "excel" ? "excel_target_lang" : surface === "word" ? "word_target_lang" : "target_lang";
  return text(settings?.[key], surface === "word" ? "fr" : surface === "cad" ? "zh" : "en");
}

// 提示词单独导入/导出仍使用文档翻译配置的安全格式，但只携带语言和领域提示词字段，
// 不把批次、输出目录或模型连接混进用户只想分享的提示词文件。
const PROMPT_CONFIG_FIELDS = [
  "source_lang", "target_lang", "excel_source_lang", "excel_target_lang", "word_source_lang", "word_target_lang",
  "custom_target_langs", "domain_preset", "domain_smart_context", "custom_prompt", "domain_name_overrides", "domain_prompt_overrides",
  "excel_domain_preset", "excel_smart_context", "excel_custom_prompt", "excel_domain_name_overrides", "excel_domain_prompt_overrides",
  "excel_domain_custom_prompts", "excel_domain_disabled_presets", "word_domain_preset", "word_smart_context", "word_custom_prompt",
  "word_domain_name_overrides", "word_domain_prompt_overrides", "word_domain_custom_prompts", "word_domain_disabled_presets",
  "cad_domain_preset", "cad_smart_context", "cad_custom_prompt", "cad_domain_name_overrides", "cad_domain_prompt_overrides",
  "cad_domain_custom_prompts", "cad_domain_disabled_presets",
] as const;
// 运行时注册的类型的语言和提示词在 extensions[<id>] 里，提示词文件只带这几项（不带侧栏显示、顺序和开关）。
const EXT_PROMPT_CONFIG_FIELDS = new Set(["source_lang", "target_lang", "domain_preset", "smart_context", "domain_custom_prompts"]);

async function exportPromptConfig(): Promise<void> {
  try {
    const payload = await client.request<JsonObject>("/api/document-config/export");
    const source = record(payload.document);
    const document: JsonObject = {};
    for (const field of PROMPT_CONFIG_FIELDS) {
      if (field in source) document[field] = source[field];
    }
    const extensions = Object.entries(record(source.extensions)).flatMap(([id, entry]) => {
      const kept = Object.entries(record(entry)).filter(([field]) => EXT_PROMPT_CONFIG_FIELDS.has(field));
      return kept.length ? [[id, Object.fromEntries(kept)] as const] : [];
    });
    if (extensions.length) document.extensions = Object.fromEntries(extensions);
    const promptPayload = {
      type: "translator_document_config",
      version: num(payload.version, 2),
      app: text(payload.app, "XL_Translator"),
      app_version: text(payload.app_version, PACKAGE_VERSION),
      document,
    };
    const saved = await saveJsonFile("translator-prompt-config.json", promptPayload);
    if (!saved) return;
    showToast({ message: t("已导出提示词配置到 {0}。", saved) });
  } catch (error) {
    showToast({ message: t("提示词配置导出失败：{0}", errorMessage(error)), error: true });
  }
}

function importPromptConfig(): void {
  pickJsonFile(async (fileName, payload) => {
    const preview = await client.request<{ areas: string[]; app_version: string }>(
      "/api/document-config/import/preview",
      { method: "POST", body: JSON.stringify(payload) },
    );
    openDocumentImportPreviewModal(fileName, payload, preview.areas);
  }, t("提示词配置导入预览失败"));
}

// 页签下划线上一次停在哪：切页签会整页重画，新画出的下划线先放在旧位置，下一帧再滑到
// 新页签下面，看起来就是同一条线滑过去的。
let paramsTabUnderline: { left: number; top: number; width: number } | null = null;

function renderParamsPage(host: HTMLElement): void {
  // 导入导出是整个「翻译参数」页的入口，放在页首，避免用户滚到页面底部才发现。
  // 配置包同时包含各页面参数、语言和领域提示词，因此这里也就是提示词的一键导入/导出。
  const bundleTitle = t("一键备份或导入 Excel、Word、PDF、CAD 和扩展类型的翻译参数、语言设置和领域提示词。不含模型、密钥和本机输出目录。");
  const head = sec({
    title: t("翻译参数"),
    desc: t("按文件类型设置；记忆库对所有类型生效。"),
    actions: [
      createButton({ label: t("导出配置"), icon: "upload", size: "mini", title: bundleTitle, onClick: () => void exportDocumentConfig() }),
      createButton({ label: t("导入配置"), icon: "down", size: "mini", title: bundleTitle, onClick: () => importDocumentConfig() }),
    ],
    className: "params-head",
  });
  const tabs = document.createElement("div");
  tabs.className = "tabs";
  tabs.setAttribute("role", "tablist");
  // 页签：内置四种和每个运行时注册的类型各一个，顺序同「扩展」页。描述符一般在启动时就拉好了；
  // 还没拉到时先画内置四种，拉到后重画。
  if (!surfaceDescriptors().length) {
    void loadSurfaceDescriptors().then((items) => {
      if (currentPage === "params" && items.some((item) => isExtSurface(item.key))) renderBody();
    }, () => undefined);
  }
  const BUILTIN_TAB_LABELS: Record<string, string> = { excel: "Excel", word: "Word", pdf: "PDF", cad: "CAD" };
  const tabDefs: { id: ParamsSurface; label: string }[] = orderedTypeKeys(settings).flatMap((key) => {
    if (isExtSurface(key)) return [{ id: key, label: extSurfaceLabel(key) }];
    return key in BUILTIN_TAB_LABELS ? [{ id: key as ParamsSurface, label: BUILTIN_TAB_LABELS[key] }] : [];
  });
  if (!tabDefs.some((def) => def.id === paramsTab)) paramsTab = tabDefs[0]?.id ?? "excel";
  let activeTab: HTMLButtonElement | null = null;
  for (const def of tabDefs) {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = def.id === paramsTab ? "tab on" : "tab";
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", String(def.id === paramsTab));
    tab.textContent = def.label;
    if (def.id === paramsTab) activeTab = tab;
    tab.addEventListener("click", () => {
      if (paramsTab === def.id) return;
      paramsTab = def.id;
      renderBody();
      riseBelow(".params-head");
    });
    tabs.append(tab);
  }
  const underline = document.createElement("span");
  underline.className = "ul";
  underline.setAttribute("aria-hidden", "true");
  tabs.append(underline);
  head.body.append(tabs);
  host.append(head.root);
  placeTabUnderline(tabs, underline, activeTab);

  // 本类型选项：只有这种文件类型自己有的项，没有就不画这一节。
  const options = sec({ title: t("本类型选项") });
  if (paramsTab === "excel") {
    const review = excelReviewSettings();
    options.body.append(selectField(
      t("已有底色处理"), [
        { value: "skip", label: t("不覆盖已有底色") },
        { value: "red_font", label: t("保留底色并使用红字（默认）") },
        { value: "overwrite", label: t("以复核色覆盖底色") },
      ],
      text(review.existing_fill_policy, "red_font"),
      (value) => void reRenderAfter(() => saveSettingPath("excel_review.existing_fill_policy", value)),
      { hint: t("单元格本身已有底色时，复核标记的处理方式。") },
    ).root);
    options.body.append(renderReviewColorGroup(record(review.mark_colors), (mark, color) => void reRenderAfter(async () => {
      const colors = record(excelReviewSettings().mark_colors);
      await persistSettings({ excel_review: { mark_colors: { ...colors, [mark]: color.replace("#", "").toUpperCase() } } });
    })));
  } else if (paramsTab === "word") {
    const review = wordReviewSettings();
    options.body.append(selectField(
      t("已有高亮处理"), [
        { value: "skip", label: t("不覆盖已有高亮") },
        { value: "red_underline", label: t("保留已有高亮并使用红字下划线（默认）") },
        { value: "overwrite", label: t("以复核色覆盖已有高亮") },
      ],
      text(review.existing_highlight_policy, "red_underline"),
      (value) => void reRenderAfter(() => saveSettingPath("word_review.existing_highlight_policy", value)),
      { hint: t("段落本身已有高亮时，复核标记的处理方式。") },
    ).root);
    options.body.append(renderReviewColorGroup(record(review.mark_colors), (mark, color) => void reRenderAfter(async () => {
      const colors = record(wordReviewSettings().mark_colors);
      await persistSettings({ word_review: { mark_colors: { ...colors, [mark]: color.replace("#", "").toUpperCase() } } });
    })));
  } else if (paramsTab === "cad") {
    // CAD 在这一页只有提示词、速率与重试，其余选项在 CAD 页右栏。
    options.body.append(secNote(t("CAD 图纸的其他翻译选项在「CAD 图纸」页右侧设置；提示词可以在下方设置。")));
  } else if (isExtSurface(paramsTab)) {
    options.body.append(...renderExtParams(paramsTab));
  }
  if (options.body.childElementCount) host.append(options.root);

  if (paramsTab === "excel" || paramsTab === "word" || paramsTab === "cad" || isExtSurface(paramsTab)) {
    host.append(renderDomainPromptCard(paramsTab));
  } else if (paramsTab === "pdf") {
    host.append(renderPdfPromptCard());
  }

  if (isExtSurface(paramsTab)) {
    host.append(sec({
      title: t("速率与重试"),
      desc: t("速率与重试：扩展类型不单独设置，按文档翻译模型（云端或本地）使用默认值。"),
    }).root);
  } else {
    const rate = renderRateSection(paramsTab);
    const rateSec = sec({ title: t("速率与重试"), desc: t("按文件类型设置同时发送的请求数、每批大小与重试次数。"), actions: rate.actions });
    rateSec.body.append(...rate.parts);
    host.append(rateSec.root);
  }

  // 记忆库对每种文件类型都生效，不跟着上面的页签切换。
  const tmCard = renderTmParamsCard();
  tmCard.classList.add("rise-skip");
  host.append(tmCard);
}

/** 把下划线放到当前页签下面；上一次的位置还在时先停在那里，再滑过来。 */
function placeTabUnderline(tabs: HTMLElement, underline: HTMLElement, active: HTMLElement | null): void {
  // 页签多到换行时，下划线跟着落到所在那一行的底边。
  const target = () => (active ? { left: active.offsetLeft, top: active.offsetTop + active.offsetHeight - 1, width: active.offsetWidth } : null);
  const apply = (pos: { left: number; top: number; width: number }) => {
    underline.style.width = `${pos.width}px`;
    underline.style.transform = `translate(${pos.left}px, ${pos.top}px)`;
  };
  requestAnimationFrame(() => {
    if (!tabs.isConnected) return;
    const next = target();
    if (!next) return;
    const previous = paramsTabUnderline;
    paramsTabUnderline = next;
    if (!previous || (previous.left === next.left && previous.top === next.top && previous.width === next.width)) {
      underline.classList.add("still");
      apply(next);
      void underline.offsetWidth;
      underline.classList.remove("still");
      return;
    }
    underline.classList.add("still");
    apply(previous);
    void underline.offsetWidth;
    underline.classList.remove("still");
    apply(next);
  });
}

/** 运行时注册的类型的页签：目标语言和源语言（速率与重试那一节只放一句说明，这类类型按文档翻译模型取默认值）。 */
function renderExtParams(surface: ExtSurface): HTMLElement[] {
  const id = surface.slice("ext:".length);
  const saved = extParams(surface);
  const label = extSurfaceLabel(surface);
  // 与类型页右栏一样按对象写补丁，不拼点路径。
  const persistExt = (name: string, value: string) => void reRenderAfter(() => persistSettings({ extensions: { [id]: { [name]: value } } }));
  const parts: HTMLElement[] = [];
  const languages = document.createElement("div");
  languages.className = "grid2";
  const target = createLanguageField({
    label: t("目标语言"),
    options: localizedLanguageOptions(targetOptions),
    value: text(saved.target_lang, "en"),
    recentKey: `${surface}-target`,
    onChange: (value) => persistExt("target_lang", value),
  });
  target.root.querySelector("label")?.append(hintBadge(t("与「{0}」页右侧的目标语言是同一项设置，在任一处修改均可。默认：英文。", label)));
  // /api/languages 的 source_options 头一项本来就是「自动识别」，只有后端没给时才补。
  const sourceList: LanguageOption[] = sourceOptions.some((option) => option.code === "auto")
    ? sourceOptions
    : [{ code: "auto", display_name: t("自动识别"), aliases: ["auto", "自动识别", "自动检测", t("自动识别")] }, ...sourceOptions]; // i18n-ignore 中文别名供搜索匹配
  const source = createLanguageField({
    label: t("源语言"),
    options: localizedLanguageOptions(sourceList),
    value: text(saved.source_lang, "auto"),
    recentKey: `${surface}-source`,
    onChange: (value) => persistExt("source_lang", value),
  });
  source.root.querySelector("label")?.append(hintBadge(t("与「{0}」页右侧的源语言是同一项设置，在任一处修改均可。默认：自动识别。", label)));
  languages.append(target.root, source.root);
  parts.push(languages);
  return parts;
}

function renderReviewColorGroup(colors: JsonObject, onSave: (mark: string, color: string) => void): HTMLDivElement {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const label = document.createElement("label");
  label.textContent = t("复核标记颜色");
  wrap.append(label);
  const row = document.createElement("div");
  row.style.display = "flex";
  row.style.gap = "18px";
  row.style.alignItems = "center";
  row.style.flexWrap = "wrap";
  // 只有这两种。曾经还有一格「语义校验接受」，但那一类是程序自己判过没问题的，
  // 不再上色，格子也一并撤掉——留着一个永远不会被涂上的颜色，只会让人以为
  // 改了它就能改变什么。
  const marks: { key: string; label: string; fallback: string }[] = [
    { key: "unresolved", label: t("保留原文复核"), fallback: "FCE4D6" },
    { key: "foreign_noise", label: t("疑似原文异常"), fallback: "F4CCCC" },
  ];
  for (const mark of marks) {
    const item = document.createElement("div");
    item.style.display = "flex";
    item.style.flexDirection = "column";
    item.style.alignItems = "center";
    item.style.gap = "4px";
    const value = text(colors[mark.key], mark.fallback).replace(/^#/, "");
    const input = document.createElement("input");
    input.type = "color";
    input.className = "swatch-input";
    input.value = `#${value}`;
    input.addEventListener("change", () => onSave(mark.key, input.value));
    const span = document.createElement("span");
    span.style.fontSize = "11px";
    span.style.color = "var(--ink-3)";
    span.textContent = mark.label;
    item.append(input, span);
    row.append(item);
  }
  wrap.append(row);
  return wrap;
}

// ---------------------------------------------------------------------------
// 提示词设置：「自动识别场景」开关行
// ---------------------------------------------------------------------------

/** 「自动识别场景」开关行。保存失败时开关退回原状并提示。 */
function smartContextRow(checked: boolean, note: string, persist: (checked: boolean) => Promise<unknown>): HTMLDivElement {
  const sw = createSwitch({
    checked,
    onChange: (next) => {
      void persist(next).then(
        () => showToast({ message: next ? t("已开启自动识别场景。") : t("已关闭自动识别场景。") }),
        (error) => {
          showToast({ message: friendlyErrorMessage(error), error: true });
          sw.classList.toggle("on", !next);
          sw.setAttribute("aria-checked", String(!next));
        },
      );
    },
  });
  sw.setAttribute("aria-label", t("自动识别场景"));
  return optRow({ title: t("自动识别场景"), desc: note, control: sw, className: "prompt-smart-row" });
}

const PARAMS_SURFACE_LABELS: Record<"excel" | "word" | "pdf" | "cad", string> = { excel: "Excel", word: "Word", pdf: "PDF", cad: "CAD" };

function paramsSurfaceLabel(surface: ParamsSurface): string {
  return isExtSurface(surface) ? extSurfaceLabel(surface) : PARAMS_SURFACE_LABELS[surface];
}

/** 按已保存的设置写出这一页的提示词组成（「?」弹窗的内容）。 */
function buildPromptAnatomy(surface: ParamsSurface): PromptAnatomy {
  if (surface === "pdf") {
    const pdf = pdfParamsSettings();
    return pdfPromptAnatomy({
      smartContext: pdf.smart_context !== false,
      reviewEnabled: pdf.review_enabled === true,
      route: text(pdf.translation_route) === "auto" ? "auto" : "image",
      scanRoute: text(pdf.auto_scan_route, "image"),
      textRoute: text(pdf.auto_text_route, "word"),
      drawingRoute: text(pdf.auto_drawing_route, "inplace"),
      fallback: text(pdf.auto_fallback, "image"),
    });
  }
  const domain = domainSettingsFor(surface);
  const entry = domain.preset !== NO_EXTRA_PROMPT ? domain.customPrompts[domain.preset] : undefined;
  const extraText = entry
    ? (entry._base || entry[targetLangForDomain(surface)] || Object.values(entry).find((value) => value.trim()) || "")
    : "";
  return docPromptAnatomy({
    surface: isExtSurface(surface) ? "ext" : surface,
    corePrompt: isExtSurface(surface) ? extCoreRules(surface) : corePrompt,
    extraName: domain.preset,
    extraText,
    smartContext: domain.smartContext,
    termRetries: num(record(settings?.tm).term_retry_attempts, 2),
  });
}

/** 运行时注册的类型的默认内置规则：清单自带的，没有则是通用的那份。 */
function extDefaultCoreRules(surface: ExtSurface): string {
  return (surfaceDescriptor(surface)?.prompt || corePrompt).trim();
}

/** 运行时注册的类型这一次会发的内置规则：用户改过的优先，否则是默认的。 */
function extCoreRules(surface: ExtSurface): string {
  return text(extParams(surface).prompt, "").trim() || extDefaultCoreRules(surface);
}

/**
 * 运行时注册的类型的「内置规则」：可以改，也可以恢复默认（清单自带的，没有则是通用的那份）。
 * 失焦保存；与默认相同或清空都存成空，即跟着默认走（以后默认改了也跟着改）。
 */
function renderExtCoreRules(surface: ExtSurface, enqueueSave: (task: () => Promise<unknown>) => Promise<unknown>): HTMLDivElement {
  const id = surface.slice("ext:".length);
  const defaults = extDefaultCoreRules(surface);
  const saved = text(extParams(surface).prompt, "").trim();
  const area = document.createElement("textarea");
  area.className = "domain-prompt core-rules";
  area.rows = 7;
  area.maxLength = 4000;
  area.value = saved || defaults;
  const store = (value: string, message: string): void => {
    void enqueueSave(() => reRenderAfter(async () => {
      await persistSettings({ extensions: { [id]: { prompt: value } } });
      showToast({ message });
    }));
  };
  area.addEventListener("change", () => {
    const value = area.value.trim();
    const next = !value || value === defaults ? "" : value;
    if (next === saved) {
      if (!value) area.value = defaults;
      return;
    }
    store(next, next ? t("已保存内置规则。") : t("已恢复默认内置规则。"));
  });
  const field = document.createElement("div");
  field.className = "core-rules-box";
  field.append(area);
  const foot = document.createElement("div");
  foot.className = "core-rules-foot";
  const note = document.createElement("span");
  note.textContent = saved ? t("已修改，与默认规则不同。") : t("当前使用默认规则。");
  foot.append(note, createButton({
    label: t("恢复默认"),
    size: "mini",
    disabled: !saved,
    onClick: () => store("", t("已恢复默认内置规则。")),
  }));
  field.append(foot, secNote(t("每次翻译都会发送这些规则，补充要求接在它们后面。这种文件类型的内置规则可以修改；清空或点「恢复默认」即改回默认规则。返回格式（只回译文、条数与原文一致）另由程序固定附上。")));
  return field;
}

/** 提示词设置的卡片标题：标题旁一个「?」，点开组成说明弹窗。 */
function promptCardHead(surface: ParamsSurface, tools: HTMLElement[] = []): HTMLDivElement {
  const head = tcHead(t("提示词设置"), tools);
  head.querySelector("b")?.after(promptAnatomyButton(() => {
    openPromptAnatomy(paramsSurfaceLabel(surface), buildPromptAnatomy(surface));
  }));
  return head;
}

/** PDF 的提示词设置：PDF 只有「自动识别场景」一项可调，内置规则由程序按页面固定生成。 */
function renderPdfPromptCard(): HTMLDivElement {
  const { root: card, body } = sec({ className: "prompt-sec" });
  body.append(smartContextRow(
    pdfParamsSettings().smart_context !== false,
    t("开启后，翻译前先让文档翻译模型读一遍 PDF 里的文字，写一段简短的文档说明：这是什么文档、属于哪个专业，以及「甲」「FM」这类单独出现的短词在这里指什么。每页翻译时附上这段说明。每份文件多调用一次模型；扫描件没有可读文字时自动跳过。"),
    (checked) => saveSettingPath("pdf.smart_context", checked),
  ));
  card.append(promptCardHead("pdf"), body);
  return card;
}

function renderDomainPromptCard(surface: TranslationSurface): HTMLDivElement {
  const current = domainSettingsFor(surface);
  const targetLang = targetLangForDomain(surface);
  const jumpPreset = pendingDomainPreset;
  pendingDomainPreset = null;
  const forceAdd = pendingNewPrompt;
  pendingNewPrompt = false;

  const extraNames = Object.keys(current.customPrompts);
  const nextExtraName = (): string => {
    let index = 1;
    while (extraNames.includes(t("补充要求{0}", index))) index += 1;
    return t("补充要求{0}", index);
  };
  const options: { value: string; label: string }[] = [
    { value: NO_EXTRA_PROMPT, label: tx(NO_EXTRA_PROMPT) },
    ...extraNames.map((name) => ({ value: name, label: name })),
    { value: ADD_PROMPT_PRESET, label: t("＋ 新增补充要求") },
  ];
  const optionValues = new Set(options.map((option) => option.value));
  const selected = forceAdd
    ? ADD_PROMPT_PRESET
    : jumpPreset && optionValues.has(jumpPreset)
      ? jumpPreset
      : optionValues.has(current.preset) ? current.preset : NO_EXTRA_PROMPT;
  // 正常数据只有一份 `_base`；旧版按目标语言存的映射仍按当前语言读，保存时由后端折叠。
  const extraTextFor = (name: string): string => current.customPrompts[name]?._base
    || current.customPrompts[name]?.[targetLang]
    || Object.values(current.customPrompts[name] || {}).find((value) => value.trim())
    || "";
  const putDomain = async (payload: { smart_context: boolean; preset: string; custom_prompts: Record<string, Record<string, string>> }): Promise<true> => {
    await client.request(`/api/domains/${encodeURIComponent(surface)}`, { method: "PUT", body: JSON.stringify(payload) });
    await refreshSettings();
    return true;
  };
  const basePayload = () => ({
    smart_context: current.smartContext,
    preset: current.preset,
    custom_prompts: { ...current.customPrompts },
  });

  const { root: card, body } = sec({ className: "prompt-sec" });
  const tools: HTMLElement[] = [];

  // 失焦保存和开关、下拉的保存可能前后脚：排成一队，每一次都从上一次存完的 current 出发。
  let saveQueue: Promise<unknown> = Promise.resolve();
  const enqueueSave = (task: () => Promise<unknown>): Promise<unknown> => {
    saveQueue = saveQueue.then(task, task);
    return saveQueue;
  };

  // ① 自动识别场景：开关。翻译前先通读文档写一段文档说明，随每批内容一起发送。
  body.append(smartContextRow(
    current.smartContext,
    t("开启后，每次翻译前先让文档翻译模型通读这份文档，写一段简短的文档说明：这是什么文档、属于哪个专业，以及「甲」「FM」这类单独出现的短词在这里指什么。这段说明随每批内容一起发送，所以同一个词在不同文档里会按各自的意思翻译。内容较多的文件，还会为文中反复出现的词定下统一译法，各批一致采用。每份文件多调用一到两次模型；某批未采用统一译法时，再重新翻译一次。"),
    (checked) => enqueueSave(async () => {
      await putDomain({ ...basePayload(), smart_context: checked });
      Object.assign(current, domainSettingsFor(surface));
    }),
  ));

  // ② 内置规则：内置四种每次都发、谁也改不了，所以只给看，不给框；运行时注册的类型可以改。
  // 规则正文很长又不常看，默认收起；标题右侧的灰字说明它能不能改。
  if (isExtSurface(surface)) {
    body.append(settingsFold(`core-rules:${surface}`, {
      title: t("内置规则"),
      meta: t("可修改"),
      content: renderExtCoreRules(surface, enqueueSave),
    }));
  } else {
    const coreBox = document.createElement("div");
    coreBox.className = "prompt-readonly";
    coreBox.textContent = corePrompt || t("内置规则读取失败，请稍后重新打开此页。");
    const coreField = document.createElement("div");
    coreField.className = "core-rules-box";
    coreField.append(coreBox, secNote(t("每次翻译都会发送这些规则，不能修改。补充要求写什么都只会追加在它们后面。返回格式（只回译文、条数与原文一致）另由程序固定附上。")));
    body.append(settingsFold("core-rules", { title: t("内置规则"), meta: t("只读"), content: coreField }));
  }

  // ③ 补充要求：用户自己的，可以有好几条，选一条用；只追加，不替换内置规则。
  let selectedExtra = selected;
  let savedPrompt = "";
  let savedName = "";
  let rejectedDraft = "";
  let optimizeBtn: HTMLButtonElement | null = null;
  const deleteExtra = (name: string): void => {
    void enqueueSave(() => reRenderAfter(async () => {
      const payload = basePayload();
      delete payload.custom_prompts[name];
      if (payload.preset === name) payload.preset = NO_EXTRA_PROMPT;
      await putDomain(payload);
      showToast({ message: t("已删除补充要求“{0}”。", name) });
    }));
  };
  const extraDropdown = createDropdown({
    options,
    value: selectedExtra,
    actionFor: (option) => {
      if (option.value === NO_EXTRA_PROMPT || option.value === ADD_PROMPT_PRESET) return undefined;
      return { label: t("删除"), onClick: () => deleteExtra(option.value) };
    },
    onChange: (value) => {
      selectedExtra = value;
      updateEditor(value);
      if (value !== ADD_PROMPT_PRESET) selectExtra(value);
    },
  });
  body.append(sectionLabel(t("补充要求")));
  const extraGrid = document.createElement("div");
  extraGrid.className = "grid2";
  extraGrid.append(fieldWithHint(t("当前使用"), extraDropdown.root, t("可选。选一条自己写的补充要求，或选择「＋ 新增补充要求」新建一条。补充要求接在内置规则后面一起发送，只能追加要求，不能替换内置规则。")));

  // 名称和「当前使用」并排：先看清是哪一条、叫什么，再看具体内容。
  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.placeholder = t("例如：电气图纸");
  const nameField = fieldWithHint(t("补充要求名称"), nameInput, t("名称可修改，不能与「无」、原来的场景名称或其他补充要求重复。"));
  extraGrid.append(nameField);
  body.append(extraGrid);

  const promptArea = document.createElement("textarea");
  promptArea.className = "domain-prompt";
  promptArea.rows = 5;
  promptArea.placeholder = t("例如：这是电气施工图，保留电气专业术语，不要增加解释");
  const promptField = fieldWithHint(t("补充要求内容"), promptArea, undefined);
  body.append(promptField);

  // 「整理」按钮那一行在正文下面，整理结果的对比区接在它后面（见下方 actionsRow）。
  const actionsHost = document.createElement("div");
  body.append(actionsHost);

  const optimizationCompare = document.createElement("div");
  optimizationCompare.className = "prompt-optimization-compare";
  optimizationCompare.style.display = "none";
  body.append(optimizationCompare);

  // 整理时的额外说明不常用，收进默认关闭的「高级」。
  const refinementInput = document.createElement("textarea");
  refinementInput.className = "domain-prompt prompt-refinement";
  refinementInput.rows = 2;
  refinementInput.placeholder = t("例如：保留电气专业术语，不要增加解释");
  const refinementField = settingsFold("prompt-advanced", {
    title: t("高级"),
    content: fieldWithHint(t("整理时的额外说明（可选）"), refinementInput, t("整理时，这些说明将一并发送给文档翻译模型。整理结果需经您确认后保存。")),
  });
  body.append(refinementField);

  let optimizationOriginal = "";
  let optimizationDraft = "";
  const clearOptimization = (): void => {
    optimizationOriginal = "";
    optimizationDraft = "";
    clearElement(optimizationCompare);
    optimizationCompare.style.display = "none";
  };

  const renderOptimizationCompare = (): void => {
    clearElement(optimizationCompare);
    optimizationCompare.style.display = "";
    const heading = document.createElement("div");
    heading.className = "prompt-optimization-heading";
    const title = document.createElement("b");
    title.textContent = t("整理结果对比");
    const hint = document.createElement("span");
    hint.textContent = t("两侧均可直接修改，确认后写回上方的补充要求。");
    heading.append(title, hint);

    const columns = document.createElement("div");
    columns.className = "prompt-optimization-columns";
    const originalBox = document.createElement("div");
    originalBox.className = "prompt-optimization-column";
    const originalLabel = document.createElement("label");
    originalLabel.textContent = t("您的版本");
    const originalArea = document.createElement("textarea");
    originalArea.className = "domain-prompt";
    originalArea.rows = 7;
    originalArea.value = optimizationOriginal;
    originalArea.addEventListener("input", () => { optimizationOriginal = originalArea.value; });
    const useOriginal = createButton({
      label: t("使用您的版本"), size: "mini",
      onClick: () => {
        promptArea.value = optimizationOriginal;
        saveDraft();
      },
    });
    originalBox.append(originalLabel, originalArea, useOriginal);

    const resultBox = document.createElement("div");
    resultBox.className = "prompt-optimization-column result";
    const resultLabel = document.createElement("label");
    resultLabel.textContent = t("整理后");
    const resultArea = document.createElement("textarea");
    resultArea.className = "domain-prompt";
    resultArea.rows = 7;
    resultArea.value = optimizationDraft;
    resultArea.addEventListener("input", () => { optimizationDraft = resultArea.value; });
    const useResult = createButton({
      label: t("使用这一版"), icon: "check", variant: "primary", size: "mini",
      onClick: () => {
        promptArea.value = optimizationDraft;
        saveDraft();
      },
    });
    resultBox.append(resultLabel, resultArea, useResult);
    columns.append(originalBox, resultBox);

    const compareActions = document.createElement("div");
    compareActions.className = "prompt-optimization-actions";
    const note = document.createElement("span");
    note.textContent = t("整理结果不会自动保存");
    compareActions.append(note);
    optimizationCompare.append(heading, columns, compareActions);
  };

  const updateEditor = (value: string): void => {
    const none = value === NO_EXTRA_PROMPT;
    const add = value === ADD_PROMPT_PRESET;
    clearOptimization();
    refinementInput.value = "";
    refinementField.style.display = none ? "none" : "";
    promptArea.value = none || add ? "" : extraTextFor(value);
    nameInput.value = add ? nextExtraName() : none ? "" : value;
    nameField.style.display = none ? "none" : "";
    promptField.style.display = none ? "none" : "";
    savedPrompt = promptArea.value;
    savedName = nameInput.value;
    if (optimizeBtn) {
      optimizeBtn.style.display = none ? "none" : "";
      optimizeBtn.disabled = false;
    }
  };

  // 把编辑器里的内容折进一份完整的提示词配置。返回 null 表示没有可存的内容（新增的
  // 还没写正文）；名称、正文不合规时抛错，由调用方提示，草稿原样留在输入框里。
  const buildPayload = (): { payload: ReturnType<typeof basePayload>; structural: boolean } | null => {
    const value = selectedExtra;
    const add = value === ADD_PROMPT_PRESET;
    if (add && !promptArea.value.trim()) return null;
    const name = nameInput.value.trim();
    const original = add ? "" : value;
    if (!name || RESERVED_EXTRA_NAMES.has(name) || name === ADD_PROMPT_PRESET || (name !== original && extraNames.includes(name))) {
      throw new Error(t("名称不能为空，也不能与「无」、原来的场景名称或其他补充要求重复。"));
    }
    if (!promptArea.value.trim()) throw new Error(t("补充要求内容不能为空。不需要时请在上方选择「无」，或在下拉中删除这一条。"));
    const payload = basePayload();
    if (original && name !== original) delete payload.custom_prompts[original];
    payload.custom_prompts[name] = { ...(current.customPrompts[original] ?? {}), _base: promptArea.value };
    payload.preset = name;
    // 新建、改名会改变下拉里的选项，必须整张卡重画；只改正文不用，免得把光标抢走。
    return { payload, structural: add || name !== original };
  };
  const isDirty = (): boolean => promptArea.value !== savedPrompt || nameInput.value !== savedName;
  // 改动即保存：离开正文或名称输入框（以及采用整理结果）时自动落盘。
  const saveDraft = (): void => {
    if (selectedExtra === NO_EXTRA_PROMPT || !isDirty()) return;
    // 草稿在这里同步取走：排队期间用户可能已在下拉里换了别的补充要求，编辑器内容随之被替换。
    let built: ReturnType<typeof buildPayload>;
    const draftKey = `${promptArea.value}\u0000${nameInput.value}`;
    try {
      built = buildPayload();
    } catch (error) {
      // 正文和名称之间来回点也会各触发一次失焦；同一份没改过的草稿只提示一次。
      if (rejectedDraft !== draftKey) showToast({ message: errorMessage(error), error: true });
      rejectedDraft = draftKey;
      return;
    }
    rejectedDraft = "";
    if (!built) return;
    const { payload, structural } = built;
    const draftPrompt = promptArea.value;
    const draftName = nameInput.value;
    void enqueueSave(async () => {
      if (structural) {
        pendingDomainPreset = payload.preset;
        if (!await reRenderAfter(() => putDomain(payload))) return;
      } else {
        try {
          await putDomain(payload);
        } catch (error) {
          showToast({ message: friendlyErrorMessage(error), error: true });
          return;
        }
        Object.assign(current, domainSettingsFor(surface));
        if (promptArea.value === draftPrompt && nameInput.value === draftName) {
          savedPrompt = draftPrompt;
          savedName = draftName;
        }
      }
      showToast({ message: t("补充要求已自动保存。") });
    });
  };
  promptArea.addEventListener("blur", saveDraft);
  nameInput.addEventListener("blur", saveDraft);
  // 选中即生效：下拉选哪条，翻译就带哪条，跟这一页其它参数一样不用另点保存。
  const selectExtra = (value: string): void => {
    void enqueueSave(() => reRenderAfter(async () => {
      await putDomain({ ...basePayload(), preset: value });
      showToast({ message: value === NO_EXTRA_PROMPT ? t("已不使用补充要求，只发送内置规则。") : t("已改用补充要求「{0}」。", value) });
    }));
  };
  optimizeBtn = createButton({
    label: t("整理"), icon: "wand", size: "mini",
    onClick: () => void (async () => {
      if (selectedExtra === NO_EXTRA_PROMPT) return;
      optimizeBtn!.disabled = true;
      try {
        const extra = refinementInput.value.trim();
        const prompt = extra
          ? `${promptArea.value}\n\n用户补充整理要求：${extra}` // i18n-ignore 发给模型的提示词正文
          : promptArea.value;
        const result = await client.request<{ prompt: string }>(`/api/domains/${encodeURIComponent(surface)}/optimize`, {
          method: "POST",
          body: JSON.stringify({
            preset: selectedExtra === ADD_PROMPT_PRESET ? "自定义提示词" : selectedExtra, // i18n-ignore 发回后端的占位名
            target_lang: "zh",
            prompt,
          }),
        });
        if (result.prompt) {
          optimizationOriginal = promptArea.value;
          optimizationDraft = result.prompt;
          renderOptimizationCompare();
          showToast({ message: t("模型已生成优化建议，请选择要采用的版本。") });
        }
      } catch (error) {
        showToast({ message: t("提示词优化失败：{0}", errorMessage(error)), error: true });
      } finally {
        optimizeBtn!.disabled = false;
      }
    }),
  });
  updateEditor(selectedExtra);
  const autoSaveNote = secNote(t("修改后自动保存。"));
  const actionsRow = fieldRow([autoSaveNote, optimizeBtn]);
  actionsRow.classList.add("prompt-actions");
  actionsHost.replaceWith(actionsRow);

  tools.push(
    createButton({ label: t("导出提示词"), size: "mini", onClick: () => void exportPromptConfig() }),
    createButton({ label: t("导入提示词"), size: "mini", onClick: () => importPromptConfig() }),
  );
  card.append(promptCardHead(surface, tools), body);
  return card;
}

// ---------------------------------------------------------------------------
// 子页③：外观与语言
// ---------------------------------------------------------------------------

const THEME_STORAGE_KEY = "translator.theme";
type ThemePreference = "light" | "dark" | "system";

function resolveTheme(preference: ThemePreference): "light" | "dark" {
  if (preference === "system") {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  return preference;
}

// 只改可见主题，不碰 localStorage。localStorage 只是「首屏免等后端」的缓存
// （见 app.ts 的 initTheme 注释），后端 settings.appearance.theme 才是权威值。
// 落盘前先用这个把界面切过去，是为了点了就有反馈，不用等一轮网络请求。
function applyThemeVisual(preference: ThemePreference): void {
  document.documentElement.dataset.theme = resolveTheme(preference);
}

// 真正落盘成功之后才写 localStorage。此前的实现是先写 localStorage 再落后端，
// 后端失败时 localStorage 已经改了、界面也已经切了，两者从此和后端权威值永久
// 分叉——重启后界面按 localStorage 显示，设置页却按后端高亮另一个选项（中-17）。
function commitThemeToLocalStorage(preference: ThemePreference): void {
  window.localStorage.setItem(THEME_STORAGE_KEY, preference);
}

function currentThemePreference(): ThemePreference {
  const value = text(record(settings?.appearance).theme, "light");
  return value === "dark" || value === "system" ? value : "light";
}

function currentPalettePreference(): PaletteChoice {
  return palettesFromAppearance(record(settings?.appearance));
}

function paletteSwatch(palette: (typeof PALETTES)[number], mode: PaletteMode): HTMLElement {
  // 小色样：背景条上放一张卡片，卡片里两行字色和一个主色按钮，画的是该方案在这一行对应
  // 模式（浅色 / 深色）下的样子，与当前页面的配色无关。
  const colors = palette.swatch[mode];
  const frame = document.createElement("span");
  frame.className = "pal-swatch";
  frame.style.background = colors.ground;
  const card = document.createElement("span");
  card.className = "pal-card";
  card.style.background = colors.surface;
  card.style.borderColor = colors.line;
  const line1 = document.createElement("i");
  line1.style.background = colors.ink;
  const line2 = document.createElement("i");
  line2.style.background = colors.ink3;
  const button = document.createElement("i");
  button.className = "pal-btn";
  button.style.background = colors.accent;
  card.append(line1, line2, button);
  frame.append(card);
  return frame;
}

// 配色方案：浅色模式、深色模式各选一个，可以跨方案搭配。和主题分段控件同样的顺序——
// 先切可见颜色给即时反馈，落盘成功后才写 localStorage，落盘失败就把颜色回滚。
function renderPaletteCard(host: HTMLElement): void {
  const { root: card, body } = sec({ title: t("配色"), desc: t("「流光」是新的默认配色；原有方案保留，可随时换回。") });
  const active = currentPalettePreference();
  const rows: { mode: PaletteMode; label: string }[] = [
    { mode: "light", label: t("浅色模式使用") },
    { mode: "dark", label: t("深色模式使用") },
  ];
  for (const row of rows) {
    const rowLabel = document.createElement("div");
    rowLabel.className = "pal-row-label";
    rowLabel.textContent = row.label;
    const grid = document.createElement("div");
    grid.className = "pal-grid";
    for (const palette of PALETTES) {
      const selected = palette.id === active[row.mode];
      const option = document.createElement("button");
      option.type = "button";
      option.className = selected ? "pal-opt on" : "pal-opt";
      option.setAttribute("aria-pressed", String(selected));
      option.setAttribute("aria-label", `${row.label} · ${palette.label()}`);
      const b = document.createElement("b");
      b.textContent = palette.label();
      const span = document.createElement("span");
      span.textContent = palette.hint();
      option.append(paletteSwatch(palette, row.mode), b, span);
      option.addEventListener("click", () => void reRenderAfter(async () => {
        const previous = currentPalettePreference();
        if (palette.id === previous[row.mode]) return;
        const next: PaletteChoice = { ...previous, [row.mode]: palette.id };
        withColorFade(() => applyPalettes(next));
        try {
          await persistSettings({
            appearance: { ...record(settings?.appearance), palette_light: next.light, palette_dark: next.dark },
          });
        } catch (error) {
          applyPalettes(previous);
          throw error;
        }
        storePalettes(next);
      }));
      grid.append(option);
    }
    body.append(rowLabel, grid);
  }
  body.append(secNote(t("界面处于浅色还是深色由上面的主题决定；两种模式可以选不同的方案，例如白天用素雅灰、晚上用琥珀护眼。")));
  host.append(card);
}

function currentLanguagePreference(): Locale {
  const value = record(settings?.appearance).ui_language;
  return isLocale(value) ? value : "zh";
}

// 界面语言：先落盘再写 localStorage，然后原地换语言（setLocale，见 i18n/index.ts），
// app.ts 收到通知后重画侧栏和本页，停在「外观与语言」原处，不重新加载。
// 选项名用各自语言的原名（中文 / English / Français），不跟着当前语言翻译——
// 看不懂当前语言的人也要能找到自己的语言。
function renderLanguageCard(host: HTMLElement): void {
  const { root: card, body } = sec({ title: t("界面语言"), desc: t("正在运行的任务不受影响，翻译结果的语言仍按各页选择的目标语言。") });
  const active = currentLanguagePreference();
  const hints: Record<Locale, string> = {
    zh: "界面、运行日志和提示显示为中文", // i18n-ignore 各语言用自己的原文
    en: "Interface, activity log and messages in English",
    fr: "Interface, journal d'activité et messages en français",
  };
  const seg = createSegmented({
    options: LOCALES.map((option) => ({ value: option.value, label: option.label })),
    value: active,
    ariaLabel: t("界面语言"),
    motionKey: "appearance.language",
  });
  seg.classList.add("seg-narrow");
  const buttons = Array.from(seg.querySelectorAll<HTMLButtonElement>("button"));
  for (const [index, button] of buttons.entries()) {
    const option = LOCALES[index];
    button.lang = option.value;
    button.title = hints[option.value];
    button.addEventListener("click", () => {
      if (option.value === currentLanguagePreference() && option.value === currentLocale()) return;
      void reRenderAfter(async () => {
        await persistSettings({ appearance: { ...record(settings?.appearance), ui_language: option.value } });
        storeLocale(option.value);
        setLocale(option.value);
      });
    });
  }
  body.append(seg);
  host.append(card);
}

// 动效：完整 / 减少。只存在本机（localStorage），不写后端设置：这是这台电脑上的观感偏好，
// settings.py 没有这一项。选「减少」时 <html data-motion="reduced">，motion.ts 和 app.css 已认
// 这个属性；系统的「减弱动态效果」不管这里怎么选都照样生效。
const MOTION_STORAGE_KEY = "translator.motion";
type MotionPreference = "full" | "reduced";

function storedMotionPreference(): MotionPreference {
  try {
    return window.localStorage.getItem(MOTION_STORAGE_KEY) === "reduced" ? "reduced" : "full";
  } catch {
    return "full";
  }
}

function applyMotionPreference(preference: MotionPreference): void {
  if (preference === "reduced") document.documentElement.dataset.motion = "reduced";
  else delete document.documentElement.dataset.motion;
}

/** 启动时按本机保存的动效偏好设置 <html data-motion>（app.ts 的 initTheme 调用）。 */
export function applyStoredMotionPreference(): void {
  applyMotionPreference(storedMotionPreference());
}

function renderMotionRow(host: HTMLElement): void {
  const seg = createSegmented({
    options: [
      { value: "full", label: t("完整") },
      { value: "reduced", label: t("减少") },
    ],
    value: storedMotionPreference(),
    ariaLabel: t("动效"),
    onChange: (value) => {
      const next: MotionPreference = value === "reduced" ? "reduced" : "full";
      applyMotionPreference(next);
      try {
        window.localStorage.setItem(MOTION_STORAGE_KEY, next);
      } catch {
        // 本机存不下时只在这次运行里生效，下次启动回到「完整」。
      }
    },
  });
  seg.classList.add("seg-motion");
  const { root, body } = sec();
  body.append(optRow({
    title: t("动效"),
    desc: t("选择「减少」后，页面切换与面板展开不再使用过渡动画；系统开启「减弱动态效果」时自动生效。"),
    control: seg,
  }));
  host.append(root);
}

function renderAppearancePage(host: HTMLElement): void {
  const { root: themeCard, body: themeBody } = sec({ title: t("主题"), desc: t("跟随系统时，浅色与深色各用自己选定的配色。") });
  const options: { value: ThemePreference; label: string; hint: string }[] = [
    { value: "system", label: t("跟随系统"), hint: t("跟随系统的浅色 / 深色外观自动切换") },
    { value: "light", label: t("浅色"), hint: t("始终使用浅色界面") },
    { value: "dark", label: t("深色"), hint: t("始终使用深色界面") },
  ];
  const active = currentThemePreference();
  // 分段控件自己负责滑块和选中态；落盘、回滚和写 localStorage 挂在每个按钮上。
  const seg = createSegmented({ options: options.map(({ value, label }) => ({ value, label })), value: active, ariaLabel: t("主题"), motionKey: "appearance.theme" });
  seg.classList.add("seg-narrow");
  const segButtons = Array.from(seg.querySelectorAll<HTMLButtonElement>("button"));
  for (const [index, segc] of segButtons.entries()) {
    const option = options[index];
    segc.title = option.hint;
    segc.addEventListener("click", () => void reRenderAfter(async () => {
      const previous = currentThemePreference();
      if (previous === option.value) return;
      withColorFade(() => applyThemeVisual(option.value));
      try {
        await persistSettings({ appearance: { ...record(settings?.appearance), theme: option.value } });
      } catch (error) {
        // 落盘失败：把可见主题回滚到落盘前的状态，不留下「界面已经切了、
        // 后端还是旧值」的分叉。reRenderAfter 外层会接着重绘整页 + 弹 toast。
        applyThemeVisual(previous);
        throw error;
      }
      commitThemeToLocalStorage(option.value);
    }));
  }
  themeBody.append(seg);
  host.append(themeCard);
  // 界面语言紧跟主题（maple 指定）：两项都是一排三选一，配色的大网格放在它们后面。
  renderLanguageCard(host);
  renderPaletteCard(host);
  renderMotionRow(host);

  const addBtn = createButton({
    label: t("新增自定义语言"), size: "mini", icon: "plus",
    onClick: () => openCustomLanguageModal(null),
  });
  const { root: langCard, body: langBody } = sec({ title: t("自定义语言"), desc: t("自定义语言只能作为目标语言使用。"), actions: [addBtn] });
  const customLanguages = targetOptions.filter((option) => option.builtin === false);
  if (!customLanguages.length) {
    langBody.append(createEmptyState({ title: t("尚无自定义目标语言"), icon: "book" }));
  } else {
    const list = document.createElement("div");
    list.className = "conn-list";
    for (const option of customLanguages) {
      const name = document.createElement("b");
      name.textContent = option.display_name;
      const desc = document.createElement("span");
      desc.className = "conn-meta";
      desc.textContent = option.description || "";
      const actions = document.createElement("div");
      actions.className = "acts";
      actions.append(
        createButton({ label: t("编辑"), size: "mini", variant: "ghost", onClick: () => openCustomLanguageModal(option) }),
        createButton({ label: t("删除"), size: "mini", variant: "danger", onClick: () => openCustomLanguageModal(option, true) }),
      );
      list.append(connRow([name, desc, actions]));
    }
    langBody.append(list);
  }
  host.append(langCard);
}

function openCustomLanguageModal(editing: LanguageOption | null, deleteMode = false): void {
  if (deleteMode && editing) {
    let handle: ModalHandle;
    handle = openModal({
      tone: "danger", icon: "trash",
      sourceLabel: t("设置 · 外观与语言 · 自定义语言"),
      title: t("删除「{0}」？", editing.display_name),
      body: [t("删除后该语言不再出现在目标语言列表中；已使用该语言完成的翻译输出不受影响。")],
      actions: [
        { label: t("取消") },
        {
          label: t("删除"), variant: "danger-solid", keepOpen: true,
          onClick: async () => {
            try {
              await client.request(`/api/languages/custom/${encodeURIComponent(editing.code)}`, { method: "DELETE" });
              await refreshLanguages();
              renderBody();
              handle.close();
              showToast({ message: t("自定义目标语言已删除。") });
            } catch (error) {
              showToast({ message: errorMessage(error), error: true });
            }
          },
        },
      ],
    });
    return;
  }

  const nameField = textField(t("显示名称"), editing?.display_name ?? "", () => undefined, { disabled: Boolean(editing), placeholder: t("例如 越南语") });
  const descArea = document.createElement("textarea");
  descArea.className = "domain-prompt";
  descArea.rows = 3;
  descArea.value = editing?.description ?? "";
  const descField = createField(t("语言说明"), descArea);

  const body = document.createElement("div");
  body.style.display = "flex";
  body.style.flexDirection = "column";
  body.style.gap = "8px";
  body.append(nameField.root, descField);

  let handle: ModalHandle;
  handle = openModal({
    tone: "warn", icon: "book",
    sourceLabel: t("设置 · 外观与语言 · 自定义语言"),
    title: editing ? t("编辑自定义语言") : t("新增自定义目标语言"),
    body: [t("自定义语言只能作为目标语言使用；名称创建后不可修改，说明可随时修改。"), body],
    actions: [
      { label: t("取消") },
      {
        label: t("保存"), variant: "primary", keepOpen: true,
        onClick: async () => {
          try {
            if (editing) {
              await client.request(`/api/languages/custom/${encodeURIComponent(editing.code)}`, {
                method: "PUT", body: JSON.stringify({ name: editing.display_name, description: descArea.value.trim() }),
              });
              await refreshLanguages();
              renderBody();
              handle.close();
              showToast({ message: t("自定义语言说明已更新。") });
            } else {
              const name = nameField.input.value.trim();
              if (!name) throw new Error(t("请输入自定义语言名称。"));
              await client.request("/api/languages/custom", {
                method: "POST", body: JSON.stringify({ name, description: descArea.value.trim() }),
              });
              await refreshLanguages();
              renderBody();
              handle.close();
              showToast({ message: t("自定义目标语言已添加。") });
            }
          } catch (error) {
            showToast({ message: errorMessage(error), error: true });
          }
        },
      },
    ],
  });
}

// ---------------------------------------------------------------------------
// 子页④：数据与维护
// ---------------------------------------------------------------------------

function maintenanceCategories(): MaintenanceCategoryInfo[] {
  const categories = maintenanceOverview?.categories;
  return Array.isArray(categories) ? categories.filter((item): item is MaintenanceCategoryInfo => Boolean(item) && typeof item === "object") : [];
}
function maintenanceCategory(id: string): MaintenanceCategoryInfo | undefined {
  return maintenanceCategories().find((item) => item.id === id);
}

/**
 * 「自动保留上限」里可调的一行：一类任务过程文件最多留几次、最多占多大（core/process_artifacts.py）。
 * 上下限对齐 config.py 的 PROCESS_RETENTION_*；体积按 GB 填、按 MB 存。留空 = 这一项不设上限（存 null）。
 */
function processRetentionRow(kind: "succeeded" | "failed", labelText: string): HTMLDivElement {
  const rule = record(record(settings?.process_retention)[kind]);
  const usage = record(record(maintenanceOverview?.process_retention)[kind === "succeeded" ? "normal" : "errors"]);
  // 设置里显式存了 null 就是「不限」；设置还没读到时退回维护概览里生效中的值（字节换成 MB）。
  const limitOf = (saved: unknown, effective: unknown, fallback: number): number | null => {
    const value = saved !== undefined ? saved : effective;
    return value === null ? null : num(value, fallback);
  };
  const effectiveMb = usage.max_bytes === null || usage.max_bytes === undefined ? usage.max_bytes : num(usage.max_bytes, 0) / (1024 * 1024);
  const maxRuns = limitOf(rule.max_runs, usage.max_runs, kind === "succeeded" ? 20 : 30);
  const maxMb = limitOf(rule.max_mb, effectiveMb, 1024);

  const row = document.createElement("div");
  row.className = "ret-row";
  const name = document.createElement("span");
  name.className = "ret-name";
  const label = document.createElement("span");
  label.textContent = labelText;
  const meta = document.createElement("small");
  meta.textContent = t("当前 {0} 次 · {1}", num(usage.count, 0), formatBytes(num(usage.size_bytes, 0)));
  name.append(label, meta);

  const save = (field: "max_runs" | "max_mb", value: number | null) =>
    void reRenderAfter(async () => {
      await saveSettingPath(`process_retention.${kind}.${field}`, value);
      await refreshMaintenance();
    }, { rerenderOnError: true });
  const runsInput = retentionInput(maxRuns, { min: 1, max: 1000, step: 1 }, (value) => save("max_runs", value === null ? null : Math.round(value)));
  runsInput.setAttribute("aria-label", t("{0}：最多保留次数", labelText));
  const gbValue = maxMb === null ? null : Math.round((maxMb / 1024) * 10) / 10;
  const gbInput = retentionInput(gbValue, { min: 0.1, max: 100, step: 0.1 }, (value) =>
    save("max_mb", value === null ? null : Math.min(100 * 1024, Math.max(100, Math.round(value * 1024)))),
  );
  gbInput.setAttribute("aria-label", t("{0}：最多占用空间（GB）", labelText));
  const controls = document.createElement("span");
  controls.className = "ret-ctl";
  const word = (value: string) => {
    const span = document.createElement("span");
    span.textContent = value;
    return span;
  };
  controls.append(word(t("最多")), runsInput, word(t("次，或")), gbInput, word("GB"));
  row.append(name, controls);
  return row;
}

/** 保留上限的数字框。留空提交 null（不设上限），占位符写「不限」；越界的贴着边界夹住，和 numberField 一致。 */
function retentionInput(
  value: number | null,
  bounds: { min: number; max: number; step: number },
  onCommit: (value: number | null) => void,
): HTMLInputElement {
  const input = document.createElement("input");
  input.type = "number";
  input.min = String(bounds.min);
  input.max = String(bounds.max);
  input.step = String(bounds.step);
  input.placeholder = t("不限");
  input.value = value === null ? "" : String(value);
  input.addEventListener("change", () => {
    if (input.value.trim() === "") {
      if (value !== null) onCommit(null);
      return;
    }
    const parsed = Number(input.value);
    if (Number.isNaN(parsed)) {
      input.value = value === null ? "" : String(value);
      return;
    }
    const clamped = Math.min(bounds.max, Math.max(bounds.min, parsed));
    if (clamped !== parsed) input.value = String(clamped);
    if (clamped !== value) onCommit(clamped);
  });
  return input;
}

function renderDataPage(host: HTMLElement): void {
  const overview = maintenanceOverview;
  const appDataDir = text(overview?.app_data_dir, t("正在读取本地数据目录…"));
  const limits = record(overview?.limits);

  const location = sec({
    title: t("数据位置"),
    desc: t("设置、密钥、记忆库与任务记录保存在本机的应用数据目录；数据格式升级前会自动备份旧文件。"),
    actions: [
      createButton({ label: t("刷新"), size: "mini", icon: "restart", onClick: () => void reRenderAfter(async () => { await refreshMaintenance(); await refreshActiveTaskCount(); }) }),
      createButton({ label: t("打开目录"), size: "mini", icon: "folder", onClick: () => void openAppDataDirectory() }),
    ],
  });
  // 等宽块整段显示：Windows 上是很长的反斜杠路径，不省略、不横向滚动，放不下就折行。
  const path = document.createElement("div");
  path.className = "code";
  path.textContent = appDataDir;
  location.body.append(path);
  host.append(location.root);

  if (activeTaskCount > 0) {
    const warn = createBanner({
      title: t("有 {0} 个任务正在运行", activeTaskCount),
      subtitle: t("任务运行期间，无法清空任务记录、日志和诊断，也无法删除全部密钥或完整重置。请待任务结束后操作。"),
      icon: "warn",
      tone: "warn",
    });
    warn.classList.add("set-banner");
    host.append(warn);
  }

  // 本地数据：以前这里一张「分类」卡、下面再一张「单类重置」卡，把任务记录、日志、密钥、
  // 设置列了两遍，按钮一处叫「清理」一处叫「清空 / 重置」。现在只留这一节，每类一行。
  const local = sec({ title: t("本地数据"), desc: t("各项仅清理 Translator 自身保存的数据，不会删除源文件和翻译结果文件。") });
  const guarded = new Set(Array.isArray(maintenanceOverview?.activity_guarded_categories) ? maintenanceOverview.activity_guarded_categories as string[] : []);
  const categoryNotes: Record<string, string> = {
    settings: t("恢复默认设置，密钥和记忆库会保留"),
    keys: t("只显示数量，不显示密钥内容"),
  };
  const categories = maintenanceCategories();
  if (!categories.length) {
    local.body.append(createEmptyState({ title: t("尚未读取本地数据概览"), description: t("点击上方的「刷新」查看本地数据的位置和占用空间。"), icon: "folder" }));
  } else {
    for (const category of categories) {
      const count = category.id === "keys" ? record(category).key_count ?? category.count : category.count;
      const sizeText = `${formatBytes(category.size_bytes)}${count !== undefined ? ` · ${t("{0} 项", count)}` : ""}`;
      const action = category.contains_user_output
        ? createChip({ label: t("受保护"), tone: "mute" })
        : category.clearable
          ? createButton({
            label: maintenanceVerb(category.id), size: "mini", variant: "danger",
            disabled: guarded.has(category.id) && activeTaskCount > 0,
            onClick: () => requestMaintenanceClear(category.id as MaintenanceClearCategory),
          })
          : createChip({ label: t("自动维护"), tone: "mute" });
      local.body.append(optRow({
        title: tx(category.label),
        desc: categoryNotes[category.id] ? `${sizeText} · ${categoryNotes[category.id]}` : sizeText,
        control: action,
      }));
    }
  }
  local.body.append(optRow({
    title: t("快速开始"),
    desc: t("重新显示欢迎引导"),
    control: createButton({ label: t("重新显示"), size: "mini", onClick: () => void showQuickStart() }),
  }));
  host.append(local.root);

  // 留存边界
  const taskHistory = maintenanceCategory("task_history");
  const logs = maintenanceCategory("logs");
  const historyLimit = num(record(taskHistory).retention_limit, num(limits.task_history_max_records, 200));
  const logsLimit = record(record(logs).retention);
  const diagLimit = diagnosticsOverview ?? record(limits.diagnostics);
  const retention = sec({ title: t("自动保留上限") });
  const limitRows: [string, string][] = [
    [t("任务记录"), t("最近 {0} 条", historyLimit)],
    [t("运行日志"), t("{0} 个 × {1}", num(logsLimit.max_files, 5), formatBytes(logsLimit.max_file_bytes || 5 * 1024 * 1024))],
    [t("诊断"), t("{0} 条 / {1}", num(diagLimit.max_records, 80), formatBytes(diagLimit.max_total_bytes || 256 * 1024 * 1024))],
  ];
  for (const [label, value] of limitRows) {
    const row = document.createElement("div");
    row.className = "limit-row";
    const l = document.createElement("span");
    l.textContent = label;
    const v = document.createElement("b");
    v.textContent = value;
    row.append(l, v);
    retention.body.append(row);
  }
  retention.body.append(
    processRetentionRow("succeeded", t("正常完成的过程文件")),
    processRetentionRow("failed", t("需复核或出错的过程文件")),
    secNote(t("过程文件用于查看与修改译文、重新生成单页和排查问题。次数或体积任一达到上限时，从最早的开始清理；留空表示不设上限。最近一次和可续译的任务始终保留。")),
    secNote(t("维护操作不会删除源文件、用户输出目录或已生成的翻译文件。")),
  );
  host.append(retention.root);

  // 诊断
  const diag = sec({
    title: t("诊断"),
    desc: t("诊断仅记录版本、系统、任务进度、已隐去密钥的连接信息、计数和错误代码，不会自动上传。"),
    actions: [
      createButton({ label: t("导出全部诊断"), size: "mini", onClick: () => void exportDiagnostics("/api/diagnostics/history.zip", "translator-diagnostics.zip") }),
      createButton({ label: t("清空诊断"), size: "mini", variant: "danger", disabled: activeTaskCount > 0, onClick: () => requestMaintenanceClear("diagnostics") }),
    ],
  });
  const tableWrap = document.createElement("div");
  tableWrap.className = "diag-table";
  const table = document.createElement("table");
  table.className = "tbl";
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const heading of [t("记录"), t("类型"), t("大小"), ""]) {
    const th = document.createElement("th");
    th.textContent = heading;
    headRow.append(th);
  }
  thead.append(headRow);
  table.append(thead);
  const tbody = document.createElement("tbody");
  if (!diagnostics.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.className = "num";
    td.style.textAlign = "left";
    td.style.color = "var(--ink-3)";
    td.textContent = t("暂无诊断记录。");
    tr.append(td);
    tbody.append(tr);
  } else {
    for (const item of diagnostics) {
      const tr = document.createElement("tr");
      const id = text(item.record_id);
      const title = text(item.created_at, t("诊断记录"));
      const type = [tx(text(item.surface)), tx(text(item.phase))].filter(Boolean).join(" · ") || t("应用");
      const tdTitle = document.createElement("td");
      tdTitle.textContent = title;
      const tdType = document.createElement("td");
      tdType.textContent = type;
      const tdSize = document.createElement("td");
      tdSize.textContent = formatBytes(item.size_bytes);
      const tdActions = document.createElement("td");
      tdActions.append(fieldRow([
        createButton({ label: t("导出"), size: "mini", onClick: () => void exportDiagnostics(`/api/diagnostics/${encodeURIComponent(id)}.zip`, "translator-diagnostic.zip") }),
        createButton({
          label: t("删除"), size: "mini", variant: "danger", disabled: activeTaskCount > 0,
          onClick: () => void reRenderAfter(async () => {
            await client.request(`/api/diagnostics/${encodeURIComponent(id)}`, { method: "DELETE" });
            await refreshMaintenance();
            showToast({ message: t("诊断记录已删除。") });
          }),
        }),
      ]));
      tr.append(tdTitle, tdType, tdSize, tdActions);
      tbody.append(tr);
    }
  }
  table.append(tbody);
  tableWrap.append(table);
  diag.body.append(tableWrap);
  host.append(diag.root);

  // 翻译记忆库
  const tm = maintenanceCategory("tm");
  const tmSec = sec({
    title: t("记忆库"),
    desc: t("建议先导出备份再清空；此操作不会删除任何翻译结果。当前总占用：{0}。", tm ? formatBytes(tm.size_bytes) : "--"),
    actions: [createButton({ label: t("前往记忆库导出"), size: "mini", onClick: () => navigate("library") })],
  });
  const tmPairOptions = tmPairCatalog?.recent ?? [];
  if (tmPairOptions.length) {
    if (!tmPairOptions.includes(selectedTmClearPair)) {
      selectedTmClearPair = tmPairOptions[0];
    }
    const pairSelect = selectField(
      t("清空所选语言对"),
      tmPairOptions.map((pair) => ({ value: pair, label: tmPairLabel(pair) })),
      selectedTmClearPair,
      (value) => { selectedTmClearPair = value; renderBody(); },
      { hint: t("候选来自最近使用过的语言对，与记忆库页面的「最近使用」一致。") },
    );
    const pairGrid = document.createElement("div");
    pairGrid.className = "grid2";
    pairGrid.append(pairSelect.root);
    tmSec.body.append(pairGrid);
    tmSec.body.append(fieldRow([
      createButton({
        label: t("清空 {0}", tmPairLabel(selectedTmClearPair)), size: "mini", variant: "danger",
        disabled: activeTaskCount > 0,
        onClick: () => requestMaintenanceClear("tm", selectedTmClearPair),
      }),
      createButton({ label: t("清空全部记忆库"), size: "mini", variant: "danger", disabled: activeTaskCount > 0, onClick: () => requestMaintenanceClear("tm") }),
    ]));
  } else {
    tmSec.body.append(fieldRow([
      createButton({ label: t("清空全部记忆库"), size: "mini", variant: "danger", disabled: activeTaskCount > 0, onClick: () => requestMaintenanceClear("tm") }),
    ]));
  }
  host.append(tmSec.root);

  // 完整本地重置：整节浅红底，和上面可以随手点的清理分开。
  const danger = sec({
    title: t("完整本地重置"),
    desc: t("仅删除 Translator 自身保存的数据，不会删除应用本身、源文件和翻译结果。完成后需重新打开应用。"),
    className: "danger-sec",
  });
  danger.body.append(fieldRow([createButton({
    label: t("完整重置"), variant: "danger-solid", size: "mini", disabled: activeTaskCount > 0,
    onClick: () => requestMaintenanceClear("full_reset"),
  })]));
  host.append(danger.root);
}

function requestMaintenanceClear(category: MaintenanceClearCategory, langPair?: string): void {
  if (category !== "settings" && activeTaskCount > 0) {
    showToast({ message: t("有任务正在运行，暂时无法执行此操作。"), error: true });
    return;
  }
  const copy = MAINTENANCE_CONFIRM_COPY[category];
  const memoryCount = maintenanceCategory("tm")?.count;
  if (category === "full_reset") {
    let handle: ModalHandle;
    handle = openModal({
      tone: "danger", icon: "trash",
      sourceLabel: t("设置 · 数据与维护 · 完整本地重置"),
      title: t("完整本地重置"),
      body: [
        memoryCount !== undefined
          ? t("将永久删除所有设置、API 密钥、记忆库（{0} 条）与任务历史，应用回到首次启动状态。此操作无法恢复。", fmtNum(memoryCount))
          : t("将永久删除所有设置、API 密钥、记忆库与任务历史，应用回到首次启动状态。此操作无法恢复。"),
      ],
      confirmInput: { placeholder: "RESET", matchValue: "RESET" },
      actions: [
        { label: t("取消") },
        {
          label: t("永久删除"), variant: "danger-solid", keepOpen: true,
          onClick: async () => {
            try {
              await client.request<JsonObject>("/api/maintenance/reset-full", {
                method: "POST", body: JSON.stringify({ confirmation: true, phrase: "RESET" }),
              });
              handle.close();
              openModal({
                tone: "warn", icon: "check",
                title: t("本地数据已重置"),
                body: [t("本地数据已删除。请关闭并重新打开 Translator。")],
                actions: [{ label: t("知道了"), variant: "primary" }],
              });
            } catch (error) {
              showToast({ message: errorMessage(error), error: true });
            }
          },
        },
      ],
    });
    return;
  }
  const body: (string | HTMLElement)[] = [copy.message];
  if (category === "tm" && langPair) {
    body.push(t("范围：仅清空语言对 {0}（{1}）。", tmPairLabel(langPair), langPair));
  }
  openModal({
    tone: "danger", icon: "trash",
    sourceLabel: t("设置 · 数据与维护 · {0}", copy.title.replace(/\s*[？?]\s*$/, "")) + (langPair ? ` · ${langPair}` : ""),
    title: copy.title,
    body,
    actions: [
      { label: t("取消") },
      {
        label: langPair ? t("清空 {0}", tmPairLabel(langPair)) : copy.confirm, variant: "danger-solid",
        onClick: () => void reRenderAfter(async () => {
          await client.request<JsonObject>("/api/maintenance/clear", {
            method: "POST", body: JSON.stringify({ category, lang_pair: langPair, confirmation: true }),
          });
          if (category === "settings") await refreshSettings();
          await refreshMaintenance();
          // 维护清理可能改变数据健康状态（比如清空 Key 后旧横幅不再成立），
          // 拉一次让横幅与后端真相对齐。
          void refreshDataHealth(mountToken);
          showToast({ message: t("已完成，翻译结果未受影响。") });
        }),
      },
    ],
  });
}

async function openAppDataDirectory(): Promise<void> {
  const path = text(maintenanceOverview?.app_data_dir);
  if (!path) {
    showToast({ message: t("本地数据目录尚未加载。"), error: true });
    return;
  }
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("open_local_path", { path, reveal: false });
  } catch (error) {
    showToast({ message: errorMessage(error), error: true });
  }
}

async function openExternalUrl(url: string): Promise<void> {
  if (!url) return;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("open_external_url", { url });
  } catch (error) {
    showToast({ message: errorMessage(error), error: true });
  }
}

// ---------------------------------------------------------------------------
// 子页⑤：更新与关于
// ---------------------------------------------------------------------------

// 「更新与关于」在用户点击「检查更新」前默认停在这个未检查状态，此时唯一能显示的版本号
// 就是这个兜底值——它必须和真实构建版本一致，因此从 ui/package.json 的 version 字段在
// 编译期读入，而不是手抄一个字符串。package.json 的版本号由
// scripts/verify_release_metadata.py 与 app_meta.py / tauri.conf.json / Cargo.toml
// 一并做发布前一致性校验。检查更新之后，真实来源始终是后端返回的
// updateResult.current_version，这个常量只在那之前或那个字段缺失时兜底。
const APP_VERSION_FALLBACK = PACKAGE_VERSION;

// 这一页自己的显示态：更新说明默认收起。属于「这一页怎么显示」，不是更新流程的一部分，
// 所以留在本地——顶部的提示卡片有它自己的展开状态，两边互不影响。
let updateNotesExpanded = false;

/** ISO 时间戳 → 「今天 14:32」/「2026-08-10 14:32」。解析不了就返回空串（调用方不显示）。 */
function formatUpdateCheckedAt(iso: string): string {
  const when = new Date(iso);
  if (!iso || Number.isNaN(when.getTime())) return "";
  const pad = (value: number) => String(value).padStart(2, "0");
  const clock = `${pad(when.getHours())}:${pad(when.getMinutes())}`;
  const today = new Date();
  const sameDay = when.getFullYear() === today.getFullYear()
    && when.getMonth() === today.getMonth()
    && when.getDate() === today.getDate();
  if (sameDay) return t("今天 {0}", clock);
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())} ${clock}`;
}

/** GitHub 的 published_at 是完整 ISO 时间戳，界面上只要日期。 */
export function formatReleaseDate(iso: string): string {
  const when = new Date(iso);
  if (!iso || Number.isNaN(when.getTime())) return iso || t("未提供");
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`;
}

/** 正在下载/安装那一屏上会变的部分。下载期间每涨 1% 就有一次重画通知，整页重建的话
 *  按钮会掉焦点、进度条和百分比每跳一格都要闪一下；所以这一屏只改文字和宽度。 */
let updateProgressView: { root: HTMLElement; apply: () => void } | null = null;

/** 下载/安装过程中的重画。用户中途切到别的子页时不要把那一页顶掉。 */
function renderAboutIfVisible(): void {
  if (currentPage !== "about") return;
  const view = updateProgressView;
  const phase = updateSnapshot().flow.phase;
  if (view && view.root.isConnected && (phase === "downloading" || phase === "installing")) {
    view.apply();
    return;
  }
  renderBody();
}

function renderAboutPage(host: HTMLElement): void {
  updateProgressView = null;
  const snapshot = updateSnapshot();
  const paused = notificationsPaused();
  const ignored = ignoredVersion();

  const { root: card, body } = sec({ title: t("版本"), className: "about-version" });

  if (snapshot.flow.phase === "ready") {
    renderUpdateReady(body);
  } else if (snapshot.flow.phase === "downloading" || snapshot.flow.phase === "installing") {
    renderUpdateProgress(body);
  } else if (snapshot.result) {
    // 「正在检查」不在这里画：顶部的提示卡片已经在转圈了，这一页再来一个空态，
    // 同一件事说两遍，而且原来的内容会整块消失一下再回来。检查期间按钮禁用即可。
    renderUpdateResult(body, { ignored });
  } else {
    renderUpdateUnchecked(body, { paused });
  }
  host.append(card);

  // 更新提醒：以前是版本区里一颗「暂停 / 恢复后台提醒」按钮，现在是一行开关。
  // 更新装好之后到重启为止可能隔着一整天的工作，这一行在任何更新阶段都在。
  const reminder = sec();
  reminder.body.append(optRow({
    title: t("启动时后台检查更新"),
    desc: t("发现新版本时，左侧「设置」上将显示红点；关闭后仅在手动检查时联网查询新版本。"),
    control: createPauseToggle(paused),
  }));
  host.append(reminder.root);

  const { root: licenseCard, body: licenseBody } = sec({ title: t("开源许可") });
  const licenseText = document.createElement("p");
  licenseText.className = "license-name";
  licenseText.textContent = "MIT License";
  licenseBody.append(licenseText, secNote(t("完整许可文本见项目仓库中的 LICENSE 文件。")));
  licenseBody.append(optRow({
    title: t("项目主页"),
    desc: PROJECT_HOME.replace(/^https:\/\//, ""),
    control: createButton({ label: t("打开"), size: "mini", variant: "ghost", icon: "ext", onClick: () => void openExternalUrl(PROJECT_HOME) }),
  }));
  host.append(licenseCard);
}

const PROJECT_HOME = "https://github.com/KlaraGraff/XL_Translator";

/** 屏①：还没有任何检查结果。进这一页不自动发起检查——启动时后台已经跑过一次了。 */
function renderUpdateUnchecked(body: HTMLElement, options: { paused: boolean }): void {
  const info = document.createElement("p");
  info.style.fontSize = "13px";
  info.textContent = t("当前版本：{0}", APP_VERSION_FALLBACK);
  body.append(info);

  const hint = document.createElement("p");
  hint.className = "note";
  hint.textContent = options.paused
    ? t("后台提醒已暂停，仅在手动检查时联网查询新版本。")
    : t("应用每次启动时会在后台检查更新；发现新版本时，左侧「设置」上将显示红点。");
  body.append(hint);

  body.append(fieldRow([
    createButton({
      label: updateSnapshot().checking ? t("检查中…") : t("检查更新"),
      variant: "primary", size: "mini",
      disabled: updateSnapshot().checking,
      onClick: () => void runUpdateCheck(),
    }),
  ]));
}

/** 屏②③⑥⑦：有检查结果时的四种落点。安装失败态叠加在这一层上。 */
function renderUpdateResult(body: HTMLElement, options: { ignored: string }): void {
  const snapshot = updateSnapshot();
  const flow = snapshot.flow;
  const result = snapshot.result ?? {};
  const status = text(result.status);
  const available = status === "available";
  const failed = flow.phase === "failed";
  const currentVersion = text(result.current_version, APP_VERSION_FALLBACK);
  const latestVersion = text(result.latest_version, text(result.latest_tag));
  const releaseUrl = text(result.release_url);
  const downloadUrl = text(result.download_url);
  const selfUpdate = canSelfUpdate();

  const info = document.createElement("p");
  info.style.fontSize = "13px";
  info.append(document.createTextNode(t("当前版本：{0}", currentVersion)));
  if (latestVersion) {
    info.append(document.createTextNode(" · " + t("最新版本：")));
    if (available) {
      const strong = document.createElement("b");
      strong.textContent = latestVersion;
      info.append(strong);
    } else {
      info.append(document.createTextNode(latestVersion));
    }
  }
  body.append(info);

  if (failed) {
    body.append(
      createChip({ label: tx(flow.failureTitle) || t("安装失败"), tone: "warn", icon: "warn" }),
    );
  } else if (available) {
    body.append(createChip({ label: t("有可用更新"), tone: "tint", icon: "down" }));
  } else if (status === "current") {
    body.append(createChip({ label: t("已是最新"), tone: "ok", icon: "check" }));
  } else if (status === "release_not_ready") {
    body.append(createChip({ label: t("发布包未就绪"), tone: "warn", icon: "warn" }));
  } else if (status === "unsupported_platform") {
    body.append(createChip({ label: t("不支持自动更新"), tone: "warn", icon: "warn" }));
  } else {
    body.append(createChip({ label: t("检查失败"), tone: "warn", icon: "warn" }));
  }

  if (available && !failed) {
    appendReleaseNotes(body, text(result.release_notes));

    const dl = document.createElement("dl");
    dl.className = "kv";
    const size = num(result.asset_size);
    const rows: [string, string][] = [
      [t("安装包"), text(result.asset_name, "--")],
      [t("大小"), size > 0 ? formatBytes(size) : "--"],
      [t("发布日期"), formatReleaseDate(text(result.release_date))],
    ];
    for (const [key, value] of rows) {
      const dt = document.createElement("dt");
      dt.textContent = key;
      const dd = document.createElement("dd");
      dd.textContent = value || "--";
      dl.append(dt, dd);
    }
    body.append(dl);

    if (!selfUpdate) {
      // 屏⑦：按钮不能假装能用。说清是哪一种情况，并退回手动下载。
      const blocked = document.createElement("p");
      blocked.className = "note";
      blocked.style.color = "var(--warn)";
      blocked.append(icon("warn", { size: "sm" }));
      blocked.append(document.createTextNode(` ${selfUpdateBlockedCopy(text(snapshot.env?.reason, "unknown"))}`));
      body.append(blocked);
    }
  } else if (failed) {
    body.append(diagnosticNote(tx(flow.message), flow.code));
  } else if (status === "current") {
    const checked = formatUpdateCheckedAt(snapshot.checkedAt);
    if (checked) {
      const note = document.createElement("p");
      note.className = "note";
      note.textContent = t("上次检查：{0}", checked);
      body.append(note);
    }
  } else {
    body.append(diagnosticNote(tx(text(result.message, t("检查更新失败，请稍后重试。"))), text(result.diagnostic_code)));
  }

  const actions: HTMLElement[] = [];
  if (available && !failed) {
    actions.push(selfUpdate
      ? createButton({
        label: t("下载并安装"), variant: "primary", size: "mini", icon: "down",
        onClick: () => void requestUpdateInstall(),
      })
      : createButton({
        label: t("下载安装包"), variant: "primary", size: "mini", icon: "down",
        disabled: !downloadUrl,
        onClick: () => void openExternalUrl(downloadUrl),
      }));
  } else {
    actions.push(createButton({
      label: failed ? t("重试") : snapshot.checking ? t("检查中…") : t("重新检查"),
      variant: failed || status !== "current" ? "primary" : "default",
      size: "mini",
      disabled: snapshot.checking,
      onClick: () => void (failed ? requestUpdateInstall() : runUpdateCheck()),
    }));
    if (failed && downloadUrl) {
      actions.push(createButton({
        label: t("改为下载安装包"), size: "mini", icon: "down",
        onClick: () => void openExternalUrl(downloadUrl),
      }));
    }
  }
  if (available && latestVersion && options.ignored !== latestVersion) {
    actions.push(createButton({
      label: t("忽略此版本"), size: "mini",
      onClick: () => void ignoreVersion(latestVersion),
    }));
  }
  if (releaseUrl) {
    actions.push(createButton({ label: t("查看 Release"), size: "mini", icon: "ext", onClick: () => void openExternalUrl(releaseUrl) }));
  }
  body.append(fieldRow(actions));
}

/** 屏④：下载有确定进度，解包安装没有——两段必须换文案，否则会被读成「重来了」。
 *
 *  这一屏建一次、改很多次：节点全部先摆好（包括安装阶段用不上的百分比），之后每次进度
 *  变化只走 apply()。增删节点会让卡片改高度，那正是用户看见的「跳一下」。 */
function renderUpdateProgress(body: HTMLElement): void {
  const heading = document.createElement("p");
  heading.style.fontSize = "13px";
  heading.append(document.createTextNode(t("正在更新到") + " "));
  const version = document.createElement("b");
  heading.append(version);
  body.append(heading);

  const stage = document.createElement("div");
  stage.style.display = "flex";
  stage.style.alignItems = "baseline";
  stage.style.gap = "10px";
  const label = document.createElement("span");
  label.style.fontSize = "12.5px";
  label.style.color = "var(--ink-2)";
  const pct = document.createElement("b");
  pct.style.marginLeft = "auto";
  pct.style.fontSize = "17px";
  pct.style.fontVariantNumeric = "tabular-nums";
  pct.style.color = "var(--tint-ink)";
  stage.append(label, pct);
  body.append(stage);

  const progress = createProgressBar({ percent: 0 });
  body.append(progress.root);

  const note = document.createElement("p");
  note.className = "note";
  body.append(note);

  const button = createButton({ label: t("下载中…"), size: "mini", disabled: true });
  body.append(fieldRow([button]));

  const apply = () => {
    const snapshot = updateSnapshot();
    const flow = snapshot.flow;
    const downloading = flow.phase === "downloading";
    const determinate = downloading && flow.percent !== null;

    version.textContent = flow.version || text(snapshot.result?.latest_version, t("新版本"));
    label.textContent = downloading ? t("正在下载安装包") : t("正在校验签名并安装");
    pct.textContent = determinate ? `${Math.floor(flow.percent ?? 0)}%` : "";

    const barClass = determinate ? "bar" : "bar indet";
    if (progress.root.className !== barClass) progress.root.className = barClass;
    // 不确定态的宽度归 CSS 管（.bar.indet i 是固定 34% 的滑块），内联宽度必须清掉。
    if (determinate) progress.setPercent(flow.percent ?? 0);
    else (progress.root.firstElementChild as HTMLElement).style.width = "";

    note.textContent = downloading
      ? (flow.total
        ? `${formatBytes(flow.received)} / ${formatBytes(flow.total)}`
        : t("已下载 {0}", formatBytes(flow.received)))
      : t("此步骤通常需要几秒钟，请勿关闭窗口。");
    button.textContent = downloading ? t("下载中…") : t("安装中…");
  };
  apply();
  updateProgressView = { root: body, apply };
}

/** 屏⑤：装好了但不自动重启——翻译任务动辄几十分钟，替用户做这个决定的代价太大。 */
function renderUpdateReady(body: HTMLElement): void {
  const snapshot = updateSnapshot();
  const flow = snapshot.flow;
  if (snapshot.readyCollapsed) {
    // 点过「稍后」。横幅收起来了，但更新包已经装在磁盘上：这里必须留一个重启入口，
    // 否则这一页会退回去重新提供「下载并安装」，等于让人把同一份包再下一遍。
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = t("{0} 已安装完成，重启 Translator 后生效。", flow.version);
    body.append(note);
    // 后台提醒的开关在下面「启动时后台检查更新」那一行，一直够得着。
    body.append(fieldRow([
      createButton({
        label: t("立即重启"), size: "mini", icon: "restart",
        onClick: () => void requestRestart(),
      }),
    ]));
    return;
  }
  body.append(createBanner({
    title: t("{0} 已安装完成", flow.version),
    subtitle: t("重启 Translator 后生效，也可稍后手动退出并重新打开。"),
    icon: "check",
    actions: [
      createButton({ label: t("立即重启"), variant: "primary", size: "mini", icon: "restart", onClick: () => void requestRestart() }),
      createButton({ label: t("稍后"), size: "mini", onClick: collapseUpdateReady }),
    ],
  }));
}

/** 「启动时后台检查更新」开关：开 = 后台提醒没有暂停。 */
function createPauseToggle(paused: boolean): HTMLButtonElement {
  const sw = createSwitch({
    checked: !paused,
    onChange: (checked) => void setNotificationsPaused(!checked),
  });
  sw.setAttribute("aria-label", t("启动时后台检查更新"));
  return sw;
}

/** 一句人话 + 一行诊断码。诊断码没有时只留人话。 */
function diagnosticNote(message: string, code: string): HTMLParagraphElement {
  const note = document.createElement("p");
  note.className = "note";
  note.append(document.createTextNode(message));
  if (code) {
    note.append(document.createElement("br"));
    const codeEl = document.createElement("span");
    codeEl.className = "diag-code";
    // 代码本身是给反馈问题时用的，单独一行写成「诊断代码：xxx」，不然像是界面漏出来的乱码。
    codeEl.textContent = t("诊断代码：{0}", code);
    note.append(codeEl);
  }
  return note;
}

/** 更新说明：渲染 Markdown，超过四行默认收起。 */
function appendReleaseNotes(body: HTMLElement, source: string): void {
  const rendered = renderReleaseNotes(source);
  if (!rendered) {
    const empty = document.createElement("p");
    empty.className = "note";
    empty.textContent = t("此版本未提供更新说明。");
    body.append(empty);
    return;
  }
  const collapsible = releaseNotesLineCount(source) > 4;
  if (collapsible && !updateNotesExpanded) {
    rendered.classList.add("collapsed");
  }
  body.append(rendered);
  if (!collapsible) return;

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "rn-more";
  toggle.textContent = updateNotesExpanded ? t("收起更新说明") : t("展开完整更新说明");
  toggle.addEventListener("click", () => {
    updateNotesExpanded = !updateNotesExpanded;
    renderAboutIfVisible();
  });
  body.append(toggle);
}
