// 设置视图 —— 五个子页：模型服务 / 翻译参数 / 外观与语言 / 数据与维护 / 更新与关于。
// 收编自旧版 ui/src/main.ts 的左侧模型配置抽屉 + 详细设置折叠组 + 维护与诊断页 +
// 更新检查弹层。main.ts 保持只读，仅供本文件对照业务语义，不被 import。
// params 约定：{ page?: "models" | "params" | "appearance" | "data" | "about" }，
// 用于从右栏「编辑 Prompt ↗ 设置」、顶栏模型药丸等处深链到具体子页。

import { navigate, type ViewParams } from "../router";
import { setTopbar } from "../shell";
import {
  createCard,
  createChip,
  createButton,
  createField,
  createSelectField,
  createDropdown,
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
import { ApiClient } from "../api-client";
import { saveJsonFile } from "../save-file";
import { showQuickStart } from "../quickstart";
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
  bundleCard,
  exportDiagnostics,
  exportDocumentConfig,
  importDocumentConfig,
  openDocumentImportPreviewModal,
  pickJsonFile,
  renderModelsPage,
} from "./settings-models";
import "./settings.css";

// ---------------------------------------------------------------------------
// 类型（本地精简版，对照 main.ts 的同名类型/字段）
// ---------------------------------------------------------------------------

export type JsonObject = Record<string, unknown>;
type SettingsPage = "models" | "params" | "appearance" | "data" | "about";
type TranslationSurface = "excel" | "word" | "cad";
type ParamsSurface = "excel" | "word" | "pdf" | "cad";

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
  return `测试于 ${new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(parsed)}`;
}

// ---------------------------------------------------------------------------
// 内置领域 Prompt：唯一事实来源是后端 GET /api/domains/builtin-prompts
// （config.py 的 DOMAIN_PRESETS）。这里绝不能再自己维护一份硬拷贝——旧版本
// 就是这么翻的车：前端那份文本一过期，用户在陈旧文本上随手一改保存，就会把
// 「缺规则的旧文本」当成覆盖永久写进 prompt_overrides，从此追不上 config.py
// 的更新（高-14）。domainBuiltinPrompts 由 refreshSettings() 每次刷新，
// 拿不到时保持上一次的值，实在从没拿到过就退化成空字符串——空字符串会让
// 「跟内置默认比对」这一步天然把用户的任何输入都存成覆盖，而不是悄悄拿旧文
// 本去顶替，属于更安全的退化方向。
// ---------------------------------------------------------------------------

let domainBuiltinPrompts: Record<string, Record<string, string>> = {};

function domainBuiltInPrompt(preset: string, targetLang: string): string {
  const prompts = domainBuiltinPrompts[preset];
  if (!prompts) return "";
  // 统一主提示词优先；旧版按目标语言保存的内置文本仅作为兼容回退。
  return prompts._base || prompts[targetLang] || "";
}

async function refreshDomainBuiltinPrompts(): Promise<void> {
  try {
    const payload = await client.request<{ presets: Record<string, Record<string, string>> }>(
      "/api/domains/builtin-prompts",
    );
    domainBuiltinPrompts = payload.presets || {};
  } catch (error) {
    // 保留上一次成功拿到的值：这只是一次刷新失败，不代表内置文本变了。
    console.warn("内置领域 Prompt 读取失败，本次沿用上一次的缓存：", error);
  }
}

// 顺序必须和 config.py 的 DOMAIN_PRESETS 一致：「无」排第一，表示不注入任何领域提示词。
// 少列一项不是「少个选项」而已——下拉里找不到当前值时浏览器会静默选中第 0 项，保存时
// 就把用户当前的领域改掉，还会把空白 Prompt 当成覆盖写进另一个领域里。
const ADD_PROMPT_PRESET = "__add_prompt__";
const LEGACY_CUSTOM_PROMPT = "__legacy_custom__";

export const MODEL_ROLE_LABELS: Record<string, string> = {
  translation: "文档翻译（Excel / Word / CAD）",
  cleaner: "记忆库清洗",
  image: "PDF 与图片翻译",
  pdf_review: "PDF 与图片审核",
};
export const MODEL_ROLE_ORDER = ["translation", "cleaner", "image", "pdf_review"];
export const FOLLOW_PREFIX = "follow:";

export const CLOUD_PROVIDERS = ["custom_openai", "openai", "claude", "zhipu", "dashscope", "siliconflow", "deepseek"];
export const LOCAL_PROVIDERS = ["ollama", "lm_studio", "custom_local"];
const PROVIDER_LABELS: Record<string, string> = {
  custom_openai: "OpenAI 兼容",
  openai: "OpenAI",
  claude: "Claude",
  zhipu: "智谱 GLM",
  dashscope: "阿里百炼",
  siliconflow: "硅基流动",
  deepseek: "DeepSeek",
  ollama: "Ollama",
  lm_studio: "LM Studio",
  custom_local: "自定义本地服务",
};
export function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}

// 服务商切换时的 Base URL 预填表，来自 GET /api/models/provider-defaults。这里不留
// 第二份硬编码：URL 一旦在前后端各存一份，服务商换了端点就会有一边是错的，而错的那
// 一边正好是用户看得见的那份。接口没回来之前预填表为空 —— 只是不预填，不会挡住手填。
export let providerBaseUrlDefaults: Record<string, string> = {};
export let providerModelDefaults: Record<string, string> = {};
export let providerBaseUrlDisabled = new Set<string>();
export let disabledBaseUrlPlaceholder = "当前服务商无需填写 Base URL";

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

const MAINTENANCE_CONFIRM_COPY: Record<MaintenanceClearCategory, { title: string; message: string; confirm: string }> = {
  task_history: { title: "清空任务记录？", message: "将删除任务中心里的任务记录，不会删除任何翻译结果。", confirm: "清空任务记录" },
  logs: { title: "清空运行日志？", message: "将删除 Translator 的运行日志，不会删除诊断记录、源文件或翻译结果。", confirm: "清空日志" },
  diagnostics: { title: "清空全部诊断？", message: "将删除所有本地诊断记录。导出过的 ZIP 不会被删除。", confirm: "清空诊断" },
  keys: { title: "删除全部 API 密钥？", message: "将删除 Translator 保存的所有服务商密钥。有任务在运行时不能执行此操作。", confirm: "删除全部密钥" },
  settings: { title: "重置设置？", message: "将恢复默认设置，API 密钥、记忆库和翻译结果都会保留。", confirm: "重置设置" },
  tm: { title: "清空记忆库？", message: "此操作不会删除翻译结果。建议先在记忆库页面导出一份完整备份。", confirm: "清空记忆库" },
  full_reset: { title: "完整重置本地数据？", message: "只删除 Translator 自己保存的数据。应用本身、你的源文件和翻译结果都不会被删除。", confirm: "完整重置" },
};

/** 按钮上的动词要和点开后弹窗的标题一致：以前列表里写「清理」，弹窗却问「重置设置？」。 */
function maintenanceVerb(category: string): string {
  if (category === "settings") return "重置";
  if (category === "keys") return "删除";
  return "清空";
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
let navEls: Map<SettingsPage, HTMLDivElement> | null = null;
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
  if (requestedSurface === "excel" || requestedSurface === "word" || requestedSurface === "pdf" || requestedSurface === "cad") {
    paramsTab = requestedSurface;
  }
  pendingDomainPreset = typeof params.domainPreset === "string" ? params.domainPreset : null;
  pendingNewPrompt = pendingDomainPreset === "无";
  if (requestedRole) modelRole = requestedRole;
  currentPage = requestedPage === "models" || requestedPage === "params" || requestedPage === "appearance"
    || requestedPage === "data" || requestedPage === "about"
    ? requestedPage
    : "models";
  setModelPillSettingsRole(currentPage === "models" ? modelRole : null);

  setTopbar({
    title: "设置",
    subtitle: "模型、参数、外观与数据都在这里",
  });

  const nav = document.createElement("div");
  nav.className = "set-nav";
  const navMap = new Map<SettingsPage, HTMLDivElement>();
  for (const item of NAV_ITEMS) {
    const el = document.createElement("div");
    el.className = "si";
    el.append(icon(item.icon), document.createTextNode(item.label));
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

  const row = document.createElement("div");
  row.style.display = "flex";
  row.style.gap = "16px";
  row.style.flex = "1";
  row.style.minHeight = "0";
  row.append(nav, body);

  container.append(banner, row);
  highlightNav();

  clearElement(body);
  body.append(createCard([createEmptyState({ title: "正在加载设置…", icon: "gear" })]));

  // 更新状态是共享的：顶部卡片上按的「更新」会一路改到这一页要显示的东西（下载进度、
  // 装好等重启、失败）。订阅着重画，才不会出现「卡片在下载、这一页还写着有可用更新」。
  unsubscribeUpdates = subscribeUpdates(renderAboutIfVisible);

  void bootstrap(token);
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
  setModelPillSettingsRole(null);
  unsubscribeUpdates?.();
  unsubscribeUpdates = null;
}

const NAV_ITEMS: { id: SettingsPage; label: string; icon: IconName }[] = [
  { id: "models", label: "模型服务", icon: "gear" },
  { id: "params", label: "翻译参数", icon: "doc-file" },
  { id: "appearance", label: "外观与语言", icon: "book" },
  { id: "data", label: "数据与维护", icon: "folder" },
  { id: "about", label: "更新与关于", icon: "help" },
];

function highlightNav(): void {
  if (!navEls) return;
  for (const [id, el] of navEls) {
    el.classList.toggle("on", id === currentPage);
  }
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
    bodyHost.append(createCard([
      createEmptyState({ title: "无法连接本地翻译引擎", description: errorMessage(error), icon: "warn" }),
    ]));
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
      text: `旧的任务历史已损坏，原文件已备份到 ${payload.task_history.backup_path || "备份目录"}；已恢复可用记录，无法读取的记录未能保留。`,
    });
  }
  if (payload.settings?.state === "recreated") {
    messages.push({
      scope: "settings",
      text: `旧的配置无法读取，已备份到 ${payload.settings.backup_path || "备份目录"}，已新建一份可用的。`,
    });
  }
  if (payload.tm?.state === "recreated") {
    messages.push({
      scope: "tm",
      text: `旧的翻译记忆库无法读取，已备份到 ${payload.tm.backup_path || "备份目录"}，已新建一份可用的。`,
    });
  }
  if (payload.keys?.state === "recreated") {
    // 和 settings/tm 不同：这条恢复的代价用户必须自己补——旧文件里其余服务商
    // 的 Key 已经跟着坏文件一起没了，新文件目前只有本次保存的这一把。
    messages.push({
      scope: "keys",
      text:
        `旧的 API Key 文件已损坏，已备份到 ${payload.keys.backup_path || "备份目录"}；` +
        "其余服务商的 Key 未能保留，请重新填写各服务商的 Key。",
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
    messages.push(payload.task_history.reason || "任务历史无法安全恢复，原文件未被覆盖，新的任务历史暂时无法保存。");
  }
  if (payload.settings?.state === "unreadable") {
    messages.push("配置文件读不出来，本次按默认设置运行，改动无法保存。原文件没有被覆盖。");
  }
  if (payload.tm?.state === "unreadable") {
    messages.push("翻译记忆库读不出来，本次翻译不会读写它。原文件没有被删除。");
  }
  if (messages.length) {
    messages.push("多为权限问题或被杀毒/备份软件占用：关掉占用它的程序后重启本应用即可；确定要放弃旧数据时，可在「数据与维护」里明确重置。");
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
    title: blocked.length ? "有数据暂时无法读取" : "有数据被重新创建",
    subtitle: messages.join(" "),
    icon: "warn",
    actions: [
      createButton({
        label: "知道了", size: "mini",
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

export function renderBody(): void {
  if (!bodyHost) return;
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
  const raw = errorMessage(error);
  if (/validation error/i.test(raw)) {
    return "保存失败：填写的值超出允许范围，已恢复为上次保存的值，请检查后重新输入。";
  }
  return raw;
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
    if (payload.disabled_placeholder) disabledBaseUrlPlaceholder = payload.disabled_placeholder;
  } catch (error) {
    providerBaseUrlDefaults = {};
    providerModelDefaults = {};
    providerBaseUrlDisabled = new Set();
    console.warn("服务商预设读取失败，本次不预填 Base URL：", error);
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
  const sourceLabel = tmPairCatalog?.source_options.find((option) => option.code === source)?.display_name ?? source;
  const targetLabel = tmPairCatalog?.target_options.find((option) => option.code === target)?.display_name ?? target;
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

async function saveSettingPath(path: string, value: string | number | boolean | null): Promise<void> {
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
  caret.setAttribute("aria-label", "展开模型候选");
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
      ? "还没有候选模型。点下面的「获取模型列表」向服务商要一份。"
      : "从候选里选一个";
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
        description: fallback ? "服务商推荐型号" : undefined,
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
function wordBatchSettings(): JsonObject {
  return record(settings?.word_batch);
}
function wordReviewSettings(): JsonObject {
  return record(settings?.word_review);
}
function pdfParamsSettings(): JsonObject {
  return record(settings?.pdf);
}

function domainSettingsFor(surface: TranslationSurface): {
  preset: string;
  customPrompt: string;
  promptOverrides: Record<string, Record<string, string>>;
  nameOverrides: Record<string, string>;
  customPrompts: Record<string, Record<string, string>>;
  disabledPresets: string[];
} {
  const prefix = surface;
  // 新数据是「预设名 → {_base: Prompt}」；这里保留嵌套读取，兼容旧版按目标语言
  // 保存的配置，保存时由后端统一折叠回 `_base`。
  return {
    preset: text(settings?.[`${prefix}_domain_preset`], surface === "excel" ? "无" : "同步工程场景"),
    customPrompt: text(settings?.[`${prefix}_custom_prompt`]),
    promptOverrides: Object.fromEntries(
      Object.entries(record(settings?.[`${prefix}_domain_prompt_overrides`])).map(([preset, langs]) => [
        preset,
        Object.fromEntries(
          Object.entries(record(langs)).filter((e): e is [string, string] => typeof e[1] === "string"),
        ),
      ]),
    ),
    nameOverrides: Object.fromEntries(
      Object.entries(record(settings?.[`${prefix}_domain_name_overrides`])).filter((e): e is [string, string] => typeof e[1] === "string"),
    ),
    customPrompts: Object.fromEntries(
      Object.entries(record(settings?.[`${prefix}_domain_custom_prompts`])).map(([name, langs]) => [
        name,
        Object.fromEntries(
          Object.entries(record(langs)).filter((e): e is [string, string] => typeof e[1] === "string"),
        ),
      ]),
    ),
    disabledPresets: Array.isArray(settings?.[`${prefix}_domain_disabled_presets`])
      ? (settings?.[`${prefix}_domain_disabled_presets`] as unknown[]).filter((item): item is string => typeof item === "string")
      : [],
  };
}

function targetLangForDomain(surface: TranslationSurface): string {
  const key = surface === "excel" ? "excel_target_lang" : surface === "word" ? "word_target_lang" : "target_lang";
  return text(settings?.[key], surface === "word" ? "fr" : surface === "cad" ? "zh" : "en");
}

// 提示词单独导入/导出仍使用文档翻译配置的安全格式，但只携带语言和领域提示词字段，
// 不把批次、输出目录或模型连接混进用户只想分享的提示词文件。
const PROMPT_CONFIG_FIELDS = [
  "source_lang", "target_lang", "excel_source_lang", "excel_target_lang", "word_source_lang", "word_target_lang",
  "custom_target_langs", "domain_preset", "custom_prompt", "domain_name_overrides", "domain_prompt_overrides",
  "excel_domain_preset", "excel_custom_prompt", "excel_domain_name_overrides", "excel_domain_prompt_overrides",
  "excel_domain_custom_prompts", "excel_domain_disabled_presets", "word_domain_preset", "word_custom_prompt",
  "word_domain_name_overrides", "word_domain_prompt_overrides", "word_domain_custom_prompts", "word_domain_disabled_presets",
  "cad_domain_preset", "cad_custom_prompt", "cad_domain_name_overrides", "cad_domain_prompt_overrides",
  "cad_domain_custom_prompts", "cad_domain_disabled_presets",
] as const;

async function exportPromptConfig(): Promise<void> {
  try {
    const payload = await client.request<JsonObject>("/api/document-config/export");
    const source = record(payload.document);
    const document: JsonObject = {};
    for (const field of PROMPT_CONFIG_FIELDS) {
      if (field in source) document[field] = source[field];
    }
    const promptPayload = {
      type: "translator_document_config",
      version: num(payload.version, 2),
      app: text(payload.app, "XL_Translator"),
      app_version: text(payload.app_version, PACKAGE_VERSION),
      document,
    };
    const saved = await saveJsonFile("translator-prompt-config.json", promptPayload);
    if (!saved) return;
    showToast({ message: `已导出提示词配置到 ${saved}。` });
  } catch (error) {
    showToast({ message: `提示词配置导出失败：${errorMessage(error)}`, error: true });
  }
}

function importPromptConfig(): void {
  pickJsonFile(async (fileName, payload) => {
    const preview = await client.request<{ areas: string[]; app_version: string }>(
      "/api/document-config/import/preview",
      { method: "POST", body: JSON.stringify(payload) },
    );
    openDocumentImportPreviewModal(fileName, payload, preview.areas);
  }, "提示词配置导入预览失败");
}

function renderParamsPage(host: HTMLElement): void {
  // 配置包是整个「翻译参数」页的入口，放在页首，避免用户滚到页面底部才发现。
  // 包内同时包含各页面参数、语言和领域提示词，因此这里也就是提示词的一键导入/导出。
  host.append(bundleCard({
    title: "翻译参数配置导入与导出",
    description: "一键备份或导入 Excel、Word、PDF、CAD 的翻译参数、语言设置和领域提示词。不含模型、密钥和本机输出目录。",
    buttons: [
      createButton({ label: "导出配置", size: "mini", onClick: () => void exportDocumentConfig() }),
      createButton({ label: "导入配置", size: "mini", onClick: () => importDocumentConfig() }),
    ],
  }));
  const card = createCard([]);
  const tabs = document.createElement("div");
  tabs.className = "tabs";
  const tabDefs: { id: ParamsSurface; label: string }[] = [
    { id: "excel", label: "Excel" }, { id: "word", label: "Word" }, { id: "pdf", label: "PDF" },
    { id: "cad", label: "CAD" },
  ];
  for (const def of tabDefs) {
    const tab = document.createElement("div");
    tab.className = def.id === paramsTab ? "tab on" : "tab";
    tab.textContent = def.label;
    tab.addEventListener("click", () => {
      if (paramsTab === def.id) return;
      paramsTab = def.id;
      renderBody();
    });
    tabs.append(tab);
  }
  card.append(tabs);

  const body = document.createElement("div");
  body.style.padding = "14px 16px";
  body.style.display = "flex";
  body.style.flexDirection = "column";
  body.style.gap = "12px";

  if (paramsTab === "excel") {
    const review = excelReviewSettings();
    body.append(selectField(
      "已有底色处理", [
        { value: "skip", label: "不覆盖已有底色" },
        { value: "red_font", label: "保留底色并使用红字（默认）" },
        { value: "overwrite", label: "以复核色覆盖底色" },
      ],
      text(review.existing_fill_policy, "red_font"),
      (value) => void reRenderAfter(() => saveSettingPath("excel_review.existing_fill_policy", value)),
      { hint: "单元格本身已有底色时，复核标记的处理方式。" },
    ).root);
    body.append(renderReviewColorGroup(record(review.mark_colors), (mark, color) => void reRenderAfter(async () => {
      const colors = record(excelReviewSettings().mark_colors);
      await persistSettings({ excel_review: { mark_colors: { ...colors, [mark]: color.replace("#", "").toUpperCase() } } });
    })));
  } else if (paramsTab === "word") {
    const review = wordReviewSettings();
    const batch = wordBatchSettings();
    body.append(selectField(
      "已有高亮处理", [
        { value: "skip", label: "不覆盖已有高亮" },
        { value: "red_underline", label: "保留已有高亮并使用红字下划线（默认）" },
        { value: "overwrite", label: "以复核色覆盖已有高亮" },
      ],
      text(review.existing_highlight_policy, "red_underline"),
      (value) => void reRenderAfter(() => saveSettingPath("word_review.existing_highlight_policy", value)),
      { hint: "段落本身已有高亮时，复核标记的处理方式。" },
    ).root);
    body.append(renderReviewColorGroup(record(review.mark_colors), (mark, color) => void reRenderAfter(async () => {
      const colors = record(wordReviewSettings().mark_colors);
      await persistSettings({ word_review: { mark_colors: { ...colors, [mark]: color.replace("#", "").toUpperCase() } } });
    })));
    body.append(sectionLabel("批次与重试"));
    const grid = document.createElement("div");
    grid.className = "grid2";
    const throughputUnlocked = Boolean(record(settings).engine && record(record(settings).engine).concurrency_unlocked);
    // 批次字符数同时决定自动分批和长段拆分；段落数在后台最多限制为 8。
    grid.append(numberField("每批字符上限", num(batch.max_chars_per_batch, 800), (v) => void reRenderAfter(() => saveSettingPath("word_batch.max_chars_per_batch", v), { rerenderOnError: true }), { min: throughputUnlocked ? 1 : 800, max: throughputUnlocked ? undefined : 12000, integer: true, hint: throughputUnlocked ? "一次发给模型最多多少字；超出会自动分批，特别长的段落会自动拆开。" : "一次发给模型最多多少字；超出会自动分批，特别长的段落会自动拆开。范围 800–12000。" }));
    grid.append(numberField("单段最多重试次数", num(batch.strict_retry_attempts, 3), (v) => void reRenderAfter(() => saveSettingPath("word_batch.strict_retry_attempts", v), { rerenderOnError: true }), { min: 1, max: 8, hint: "译文为空、明显不完整或没通过质量检查的段落，最多自动重翻几次。" }));
    body.append(grid);
  } else if (paramsTab === "pdf") {
    const pdf = pdfParamsSettings();
    const grid = document.createElement("div");
    grid.className = "grid2";
    // 上下限对齐 config.py：PDF_PAGE_RETRY_ATTEMPTS_MIN/MAX(0–8)、
    // PDF_PAGE_CONCURRENCY_SAFETY_CAP(1–20)。max: 10 曾经比后端的 8 宽，落进
    // 那道夹缝的值只会在保存时被后端 422 打回（中-14）。
    grid.append(numberField("单页重试次数", num(pdf.page_retry_attempts, 1), (v) => void reRenderAfter(() => saveSettingPath("pdf.page_retry_attempts", v), { rerenderOnError: true }), { min: 0, max: 8, hint: "一页翻译失败后最多重试几次，0 到 8。" }));
    const concurrencyValue = pdf.page_generation_concurrency === null || pdf.page_generation_concurrency === undefined
      ? "" : String(pdf.page_generation_concurrency);
    const concurrencyInput = document.createElement("input");
    concurrencyInput.type = "number";
    concurrencyInput.min = "1";
    const throughputUnlocked = Boolean(record(settings).engine && record(record(settings).engine).concurrency_unlocked);
    if (!throughputUnlocked) concurrencyInput.max = "20";
    concurrencyInput.value = concurrencyValue;
    concurrencyInput.placeholder = "留空自动";
    concurrencyInput.addEventListener("change", () => {
      const raw = concurrencyInput.value.trim();
      let parsed: number | null = raw ? Number(raw) : null;
      // 留空＝自动；普通状态按页图并发范围收敛，解锁后保留正整数校验。
      if (parsed !== null && !Number.isNaN(parsed)) {
        if (!Number.isInteger(parsed)) return;
        parsed = Math.min(throughputUnlocked ? 2**31 - 1 : 20, Math.max(1, parsed));
        concurrencyInput.value = String(parsed);
      }
      void reRenderAfter(() => saveSettingPath("pdf.page_generation_concurrency", parsed), { rerenderOnError: true });
    });
    grid.append(fieldWithHint("同时生成页数（留空自动）", concurrencyInput, throughputUnlocked
      ? "同时翻译几页；留空则按图像模型的速率设置自动决定。"
      : "同时翻译几页，1 到 20；留空则按图像模型的速率设置自动决定。"));
    body.append(grid);
  } else if (paramsTab === "cad") {
    // CAD 没有放在这一页的参数；以前这个标签下是一块空白卡片，看着像没加载出来。
    const note = document.createElement("p");
    note.className = "note";
    note.style.margin = "0";
    note.style.fontSize = "12.5px";
    note.style.color = "var(--ink-3)";
    note.textContent = "CAD 图纸的翻译选项在「CAD 图纸」页右侧设置；专业领域提示词可以在下方编辑。";
    body.append(note);
  }

  card.append(body);
  host.append(card);

  if (paramsTab === "excel" || paramsTab === "word" || paramsTab === "cad") {
    host.append(renderDomainPromptCard(paramsTab));
  }

}

function renderReviewColorGroup(colors: JsonObject, onSave: (mark: string, color: string) => void): HTMLDivElement {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const label = document.createElement("label");
  label.textContent = "复核标记颜色";
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
    { key: "unresolved", label: "保留原文复核", fallback: "FCE4D6" },
    { key: "foreign_noise", label: "疑似原文异常", fallback: "F4CCCC" },
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

function renderDomainPromptCard(surface: TranslationSurface): HTMLDivElement {
  const current = domainSettingsFor(surface);
  const targetLang = targetLangForDomain(surface);
  const jumpPreset = pendingDomainPreset;
  pendingDomainPreset = null;
  const forceAdd = pendingNewPrompt;
  pendingNewPrompt = false;

  const allBuiltIns = Object.keys(domainBuiltinPrompts).filter((name) => name !== "无" && name !== "自定义");
  const builtIns = allBuiltIns.filter((name) => !current.disabledPresets.includes(name) || name === current.preset || name === jumpPreset);
  const customNames = Object.keys(current.customPrompts);
  const presetDisplayName = (name: string): string => current.nameOverrides[name]?.trim() || name;
  const nextCustomPromptName = (): string => {
    let index = 1;
    while (customNames.includes(`自定义提示词${index}`) || allBuiltIns.some((name) => presetDisplayName(name) === `自定义提示词${index}`)) index += 1;
    return `自定义提示词${index}`;
  };
  const options: { value: string; label: string }[] = [
    { value: "无", label: "无" },
    ...builtIns.map((name) => ({ value: name, label: current.disabledPresets.includes(name) ? `${presetDisplayName(name)}（已停用）` : presetDisplayName(name) })),
    ...customNames.map((name) => ({ value: name, label: name })),
    { value: ADD_PROMPT_PRESET, label: "＋ 新增提示词" },
  ];
  if (current.preset === "自定义" && !customNames.includes(current.preset)) {
    options.splice(options.length - 1, 0, { value: LEGACY_CUSTOM_PROMPT, label: "当前提示词（旧版）" });
  }
  const optionValues = new Set(options.map((option) => option.value));
  const selected = forceAdd
    ? ADD_PROMPT_PRESET
    : jumpPreset && optionValues.has(jumpPreset)
      ? jumpPreset
      : current.preset === "自定义" ? LEGACY_CUSTOM_PROMPT : optionValues.has(current.preset) ? current.preset : "无";

  // 新版本只维护一份统一 Prompt。读取时仍兼容旧的按目标语言存储，优先使用
  // `_base`，这样升级后不会丢失已有语言版本，后续保存会逐步收敛到统一版本。
  const overrideFor = (preset: string): string | undefined => {
    const values = current.promptOverrides[preset] || {};
    return values._base || values[targetLang] || Object.values(values).find((value) => value.trim());
  };
  const customPromptFor = (preset: string): string => current.customPrompts[preset]?._base
    || current.customPrompts[preset]?.[targetLang]
    || Object.values(current.customPrompts[preset] || {}).find((value) => value.trim())
    || "";
  const promptFor = (preset: string): string => {
    if (preset === "无" || preset === ADD_PROMPT_PRESET) return "";
    if (preset === LEGACY_CUSTOM_PROMPT) return current.customPrompt;
    if (customNames.includes(preset)) return customPromptFor(preset);
    return overrideFor(preset) ?? domainBuiltInPrompt(preset, "zh");
  };
  const isBuiltIn = (preset: string): boolean => allBuiltIns.includes(preset);

  const card = createCard([]);
  const tools: HTMLElement[] = [];
  const body = document.createElement("div");
  body.style.padding = "14px 16px";
  body.style.display = "flex";
  body.style.flexDirection = "column";
  body.style.gap = "10px";

  let selectedPreset = selected;
  let saveBtn: HTMLButtonElement | null = null;
  let optimizeBtn: HTMLButtonElement | null = null;
  let restoreBtn: HTMLButtonElement | null = null;
  const deletePreset = (name: string): void => {
    void reRenderAfter(async () => {
      const disabledPresets = current.disabledPresets.filter((item) => item !== name);
      const customPrompts = { ...current.customPrompts };
      let preset = current.preset;
      if (allBuiltIns.includes(name)) disabledPresets.push(name);
      else delete customPrompts[name];
      if (preset === name) preset = "无";
      await client.request(`/api/domains/${surface}`, {
        method: "PUT",
        body: JSON.stringify({
          preset,
          custom_prompt: current.customPrompt,
          prompt_overrides: current.promptOverrides,
          name_overrides: current.nameOverrides,
          custom_prompts: customPrompts,
          disabled_presets: disabledPresets,
        }),
      });
      await refreshSettings();
      showToast({ message: `已删除提示词“${name}”。` });
    });
  };
  const presetDropdown = createDropdown({
    options,
    value: selectedPreset,
    actionFor: (option) => {
      if (option.value === "无" || option.value === ADD_PROMPT_PRESET || option.value === LEGACY_CUSTOM_PROMPT) return undefined;
      return { label: "删除", onClick: () => deletePreset(option.value) };
    },
    onChange: (value) => {
      selectedPreset = value;
      updateEditor(value);
    },
  });
  body.append(fieldWithHint("提示词名称", presetDropdown.root, "选择已有提示词，或选择“＋ 新增提示词”创建自己的提示词。名称可以在下方编辑。删除内置项只会停用它，之后仍可重新启用。"));

  let promptArea: HTMLTextAreaElement;

  promptArea = document.createElement("textarea");
  promptArea.className = "domain-prompt";
  promptArea.rows = 6;
  const promptField = fieldWithHint("提示词内容", promptArea, undefined);
  body.append(promptField);

  const refinementInput = document.createElement("textarea");
  refinementInput.className = "domain-prompt prompt-refinement";
  refinementInput.rows = 2;
  refinementInput.placeholder = "例如：保留电气专业术语，不要增加解释";
  const refinementField = fieldWithHint("补充要求（可选）", refinementInput, "整理时会把这段要求一起交给文档翻译模型。整理结果仍需你确认后保存。");
  body.append(refinementField);

  const optimizationCompare = document.createElement("div");
  optimizationCompare.className = "prompt-optimization-compare";
  optimizationCompare.style.display = "none";
  body.append(optimizationCompare);

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
    title.textContent = "整理结果对比";
    const hint = document.createElement("span");
    hint.textContent = "两侧都可以直接修改，确认后再写回上面的提示词。";
    heading.append(title, hint);

    const columns = document.createElement("div");
    columns.className = "prompt-optimization-columns";
    const originalBox = document.createElement("div");
    originalBox.className = "prompt-optimization-column";
    const originalLabel = document.createElement("label");
    originalLabel.textContent = "你的版本";
    const originalArea = document.createElement("textarea");
    originalArea.className = "domain-prompt";
    originalArea.rows = 7;
    originalArea.value = optimizationOriginal;
    originalArea.addEventListener("input", () => { optimizationOriginal = originalArea.value; });
    const useOriginal = createButton({
      label: "使用你的版本", size: "mini",
      onClick: () => {
        promptArea.value = optimizationOriginal;
        showToast({ message: "已保留你的版本，请确认后保存。" });
      },
    });
    originalBox.append(originalLabel, originalArea, useOriginal);

    const resultBox = document.createElement("div");
    resultBox.className = "prompt-optimization-column result";
    const resultLabel = document.createElement("label");
    resultLabel.textContent = "整理后";
    const resultArea = document.createElement("textarea");
    resultArea.className = "domain-prompt";
    resultArea.rows = 7;
    resultArea.value = optimizationDraft;
    resultArea.addEventListener("input", () => { optimizationDraft = resultArea.value; });
    const useResult = createButton({
      label: "使用这一版", icon: "check", variant: "primary", size: "mini",
      onClick: () => {
        promptArea.value = optimizationDraft;
        showToast({ message: "已将整理版放回提示词内容，请确认后保存。" });
      },
    });
    resultBox.append(resultLabel, resultArea, useResult);
    columns.append(originalBox, resultBox);

    const compareActions = document.createElement("div");
    compareActions.className = "prompt-optimization-actions";
    const note = document.createElement("span");
    note.textContent = "整理结果不会自动保存";
    compareActions.append(note);
    optimizationCompare.append(heading, columns, compareActions);
  };

  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.placeholder = "例如：电气图纸翻译";
  const nameField = fieldWithHint("编辑名称", nameInput, "名称可以修改；保存时会保留当前提示词内容。不能与内置提示词或其他自定义提示词重名。");
  body.append(nameField);

  const updateEditor = (preset: string): void => {
    const custom = customNames.includes(preset);
    const legacy = preset === LEGACY_CUSTOM_PROMPT;
    const none = preset === "无";
    const add = preset === ADD_PROMPT_PRESET;
    clearOptimization();
    refinementInput.value = "";
    promptArea.value = promptFor(preset);
    promptArea.placeholder = add ? "请输入完整提示词" : isBuiltIn(preset) ? "内置提示词会在此显示" : "请输入提示词内容";
    promptField.style.display = none ? "none" : "";
    nameField.style.display = none ? "none" : "";
    nameInput.value = add
      ? nextCustomPromptName()
      : isBuiltIn(preset)
        ? presetDisplayName(preset)
        : preset === LEGACY_CUSTOM_PROMPT ? "自定义" : preset;
    refinementField.style.display = "none";
    const label = promptField.querySelector("label");
    if (label) label.firstChild && (label.firstChild.textContent = add ? "提示词内容" : legacy ? "旧版提示词内容" : custom ? "提示词内容" : isBuiltIn(preset) ? "内置提示词（可编辑覆盖）" : "提示词内容");
    if (saveBtn) saveBtn.textContent = "保存";
    if (optimizeBtn) {
      optimizeBtn.style.display = none ? "none" : "";
      optimizeBtn.disabled = false;
    }
    if (restoreBtn) restoreBtn.style.display = isBuiltIn(preset) ? "" : "none";
  };
  updateEditor(selectedPreset);

  const doSave = () => void reRenderAfter(async () => {
    const preset = selectedPreset;
    const promptOverrides = { ...current.promptOverrides };
    const nameOverrides = { ...current.nameOverrides };
    let customPrompt = current.customPrompt;
    const customPrompts = { ...current.customPrompts };
    const disabledPresets = [...current.disabledPresets];
    let persistedPreset = preset;
    const invalidName = (name: string, original?: string): boolean => {
      if (original && name === original) return false;
      if (isBuiltIn(preset) && name === presetDisplayName(preset)) return false;
      if (!name || name === "无" || name === "自定义" || name === ADD_PROMPT_PRESET || name === LEGACY_CUSTOM_PROMPT) return true;
      if (allBuiltIns.some((builtIn) => name === builtIn || name === presetDisplayName(builtIn))) return true;
      return customNames.some((customName) => customName !== original && customName === name);
    };
    if (preset === "无") {
      // 只改「当前选哪个领域」，其他领域已存的覆盖原样带回去。
    } else if (preset === ADD_PROMPT_PRESET) {
      const name = nameInput.value.trim();
      if (invalidName(name)) throw new Error("提示词名称不能为空，且不能与内置选项或已有提示词重名。");
      if (!promptArea.value.trim()) throw new Error("新提示词不能保存为空。");
      customPrompts[name] = { ...(customPrompts[name] ?? {}), _base: promptArea.value };
      persistedPreset = name;
    } else if (preset === LEGACY_CUSTOM_PROMPT) {
      if (!promptArea.value.trim()) throw new Error("提示词不能保存为空。");
      const name = nameInput.value.trim();
      if (invalidName(name)) throw new Error("提示词名称不能为空，且不能与内置选项或已有提示词重名。");
      customPrompts[name] = { _base: promptArea.value };
      customPrompt = "";
      persistedPreset = name;
    } else if (customNames.includes(preset)) {
      if (!promptArea.value.trim()) throw new Error("提示词不能保存为空。");
      const name = nameInput.value.trim();
      if (invalidName(name, preset)) throw new Error("提示词名称不能为空，且不能与内置选项或已有提示词重名。");
      customPrompts[name] = { ...(customPrompts[preset] ?? {}), _base: promptArea.value };
      if (name !== preset) delete customPrompts[preset];
      persistedPreset = name;
    } else {
      // 统一维护一份 `_base` Prompt；旧语言版本保留在 payload 中以便兼容读取。
      const defaultPrompt = domainBuiltInPrompt(preset, "zh");
      const langOverrides = { ...(promptOverrides[preset] ?? {}) };
      if (promptArea.value === defaultPrompt) delete langOverrides._base;
      else langOverrides._base = promptArea.value;
      if (Object.keys(langOverrides).length) promptOverrides[preset] = langOverrides;
      else delete promptOverrides[preset];
      const name = nameInput.value.trim();
      const defaultName = preset;
      if (invalidName(name, preset)) throw new Error("提示词名称不能为空，且不能与内置选项或已有提示词重名。");
      if (name === defaultName) delete nameOverrides[preset];
      else nameOverrides[preset] = name;
    }
    await client.request(`/api/domains/${surface}`, {
      method: "PUT",
      body: JSON.stringify({
        preset: persistedPreset,
        custom_prompt: customPrompt,
        prompt_overrides: promptOverrides,
        name_overrides: nameOverrides,
        custom_prompts: customPrompts,
        disabled_presets: disabledPresets,
      }),
    });
    await refreshSettings();
    showToast({
      message: persistedPreset === "无" ? "已改为不使用专业领域提示词。"
        : preset === ADD_PROMPT_PRESET ? "新提示词已保存。"
          : customNames.includes(preset) || preset === LEGACY_CUSTOM_PROMPT ? "提示词已保存。"
            : "提示词已保存。",
    });
  });
  saveBtn = createButton({
    label: "保存选择",
    variant: "primary", size: "mini", onClick: doSave,
  });
  optimizeBtn = createButton({
    label: "整理", icon: "wand", size: "mini",
    onClick: () => void (async () => {
      if (selectedPreset === "无") return;
      optimizeBtn!.disabled = true;
      try {
        const extra = refinementInput.value.trim();
        const prompt = extra
          ? `${promptArea.value}\n\n用户补充整理要求：${extra}`
          : promptArea.value;
        const result = await client.request<{ prompt: string }>(`/api/domains/${surface}/optimize`, {
          method: "POST",
          body: JSON.stringify({
            preset: selectedPreset === ADD_PROMPT_PRESET || selectedPreset === LEGACY_CUSTOM_PROMPT ? "自定义提示词" : selectedPreset,
            target_lang: "zh",
            prompt,
          }),
        });
        if (result.prompt) {
          optimizationOriginal = promptArea.value;
          optimizationDraft = result.prompt;
          refinementField.style.display = "";
          renderOptimizationCompare();
          showToast({ message: "模型已生成优化建议，请确认后保存。" });
        }
      } catch (error) {
        showToast({ message: `提示词优化失败：${errorMessage(error)}`, error: true });
      } finally {
        optimizeBtn!.disabled = false;
      }
    }),
  });
  if (selectedPreset === "无") optimizeBtn.style.display = "none";
  // 按钮是在编辑器首次填充后创建的，再同步一次初始文案和显隐状态。
  updateEditor(selectedPreset);
  const actions: HTMLButtonElement[] = [saveBtn, optimizeBtn];
  if (isBuiltIn(selectedPreset)) {
    restoreBtn = createButton({
      label: "恢复内置默认", size: "mini",
      onClick: () => void reRenderAfter(async () => {
        const preset = selectedPreset;
        if (!isBuiltIn(preset)) return;
        // 只清统一主 Prompt 覆盖，保留旧语言版本以便兼容旧数据。
        const promptOverrides = { ...current.promptOverrides };
        const langOverrides = { ...(promptOverrides[preset] ?? {}) };
        delete langOverrides._base;
        if (Object.keys(langOverrides).length) promptOverrides[preset] = langOverrides;
        else delete promptOverrides[preset];
        await client.request(`/api/domains/${surface}`, {
          method: "PUT",
          body: JSON.stringify({ preset, custom_prompt: current.customPrompt, prompt_overrides: promptOverrides, name_overrides: current.nameOverrides, custom_prompts: current.customPrompts, disabled_presets: current.disabledPresets }),
        });
        await refreshSettings();
        showToast({ message: "已恢复内置提示词。" });
      }),
    });
    actions.push(restoreBtn);
  }
  const actionsRow = fieldRow(actions);
  actionsRow.style.justifyContent = "flex-end";
  body.append(actionsRow);

  tools.push(
    createButton({ label: "导出提示词", size: "mini", onClick: () => void exportPromptConfig() }),
    createButton({ label: "导入提示词", size: "mini", onClick: () => importPromptConfig() }),
  );
  card.append(tcHead("提示词设置", tools), body);
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
  const value = text(record(settings?.appearance).theme, "system");
  return value === "light" || value === "dark" ? value : "system";
}

function renderAppearancePage(host: HTMLElement): void {
  const themeCard = createCard([]);
  const themeBody = document.createElement("div");
  themeBody.style.padding = "14px 16px";
  themeBody.append(sectionLabel("主题"));
  const seg = document.createElement("div");
  seg.className = "seg";
  const options: { value: ThemePreference; label: string; hint: string }[] = [
    { value: "light", label: "浅色", hint: "始终使用浅色界面" },
    { value: "dark", label: "深色", hint: "始终使用深色界面" },
    { value: "system", label: "跟随系统", hint: "跟随系统的浅色 / 深色外观自动切换" },
  ];
  const active = currentThemePreference();
  for (const option of options) {
    const segc = document.createElement("div");
    segc.className = option.value === active ? "segc on" : "segc";
    const b = document.createElement("b");
    b.textContent = option.label;
    const span = document.createElement("span");
    span.textContent = option.hint;
    segc.append(b, span);
    segc.addEventListener("click", () => void reRenderAfter(async () => {
      const previous = currentThemePreference();
      applyThemeVisual(option.value);
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
    seg.append(segc);
  }
  themeBody.append(seg);
  themeCard.append(themeBody);
  host.append(themeCard);

  const langCard = createCard([]);
  const addBtn = createButton({
    label: "新增自定义语言", size: "mini", icon: "plus",
    onClick: () => openCustomLanguageModal(null),
  });
  langCard.append(tcHead("自定义语言管理", [addBtn]));
  const customLanguages = targetOptions.filter((option) => option.builtin === false);
  if (!customLanguages.length) {
    langCard.append(createEmptyState({ title: "还没有自定义目标语言", description: "自定义语言只能作为目标语言使用。", icon: "book" }));
  } else {
    for (const option of customLanguages) {
      const name = document.createElement("span");
      name.style.flex = "1";
      name.style.fontWeight = "600";
      name.textContent = option.display_name;
      const desc = document.createElement("span");
      desc.style.color = "var(--ink-3)";
      desc.style.fontSize = "12px";
      desc.textContent = option.description || "";
      const actions = document.createElement("span");
      actions.style.display = "flex";
      actions.style.gap = "6px";
      actions.append(
        createButton({ label: "编辑", size: "mini", onClick: () => openCustomLanguageModal(option) }),
        createButton({ label: "删除", size: "mini", variant: "danger", onClick: () => openCustomLanguageModal(option, true) }),
      );
      langCard.append(connRow([name, desc, actions]));
    }
  }
  host.append(langCard);
}

function openCustomLanguageModal(editing: LanguageOption | null, deleteMode = false): void {
  if (deleteMode && editing) {
    let handle: ModalHandle;
    handle = openModal({
      tone: "danger", icon: "trash",
      sourceLabel: "设置 · 外观与语言 · 自定义语言",
      title: `删除「${editing.display_name}」？`,
      body: ["删除后该语言不再出现在目标语言列表中；已使用该语言完成的翻译输出不受影响。"],
      actions: [
        { label: "取消" },
        {
          label: "删除", variant: "danger-solid", keepOpen: true,
          onClick: async () => {
            try {
              await client.request(`/api/languages/custom/${encodeURIComponent(editing.code)}`, { method: "DELETE" });
              await refreshLanguages();
              renderBody();
              handle.close();
              showToast({ message: "自定义目标语言已删除。" });
            } catch (error) {
              showToast({ message: errorMessage(error), error: true });
            }
          },
        },
      ],
    });
    return;
  }

  const nameField = textField("显示名称", editing?.display_name ?? "", () => undefined, { disabled: Boolean(editing), placeholder: "例如 越南语" });
  const descArea = document.createElement("textarea");
  descArea.className = "domain-prompt";
  descArea.rows = 3;
  descArea.value = editing?.description ?? "";
  const descField = createField("语言说明", descArea);

  const body = document.createElement("div");
  body.style.display = "flex";
  body.style.flexDirection = "column";
  body.style.gap = "8px";
  body.append(nameField.root, descField);

  let handle: ModalHandle;
  handle = openModal({
    tone: "warn", icon: "book",
    sourceLabel: "设置 · 外观与语言 · 自定义语言",
    title: editing ? "编辑自定义语言" : "新增自定义目标语言",
    body: ["自定义语言只能作为目标语言使用；名称创建后不能修改，说明可以随时改。", body],
    actions: [
      { label: "取消" },
      {
        label: "保存", variant: "primary", keepOpen: true,
        onClick: async () => {
          try {
            if (editing) {
              await client.request(`/api/languages/custom/${encodeURIComponent(editing.code)}`, {
                method: "PUT", body: JSON.stringify({ name: editing.display_name, description: descArea.value.trim() }),
              });
              await refreshLanguages();
              renderBody();
              handle.close();
              showToast({ message: "自定义语言说明已更新。" });
            } else {
              const name = nameField.input.value.trim();
              if (!name) throw new Error("请输入自定义语言名称。");
              await client.request("/api/languages/custom", {
                method: "POST", body: JSON.stringify({ name, description: descArea.value.trim() }),
              });
              await refreshLanguages();
              renderBody();
              handle.close();
              showToast({ message: "自定义目标语言已添加。" });
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

function renderDataPage(host: HTMLElement): void {
  const overview = maintenanceOverview;
  const appDataDir = text(overview?.app_data_dir, "正在读取本地数据目录…");
  const limits = record(overview?.limits);

  const headerCard = createCard([]);
  const headerBody = document.createElement("div");
  headerBody.style.padding = "14px 16px";
  headerBody.style.display = "flex";
  headerBody.style.alignItems = "center";
  headerBody.style.gap = "12px";
  const headerText = document.createElement("div");
  headerText.style.flex = "1";
  headerText.style.minWidth = "0";
  const span = document.createElement("div");
  span.className = "rp-sec";
  span.style.margin = "0 0 4px";
  span.textContent = "本地数据位置";
  const path = document.createElement("p");
  path.style.fontFamily = "var(--mono)";
  path.style.fontSize = "12px";
  path.style.color = "var(--ink-2)";
  path.style.overflow = "hidden";
  path.style.textOverflow = "ellipsis";
  path.style.whiteSpace = "nowrap";
  path.textContent = appDataDir;
  headerText.append(span, path);
  headerBody.append(headerText, fieldRow([
    createButton({ label: "刷新", size: "mini", onClick: () => void reRenderAfter(async () => { await refreshMaintenance(); await refreshActiveTaskCount(); }) }),
    createButton({ label: "在文件夹中显示", size: "mini", onClick: () => void openAppDataDirectory() }),
  ]));
  headerCard.append(headerBody);
  host.append(headerCard);

  if (activeTaskCount > 0) {
    const warnCard = createCard([], "danger-section");
    const warnBody = document.createElement("div");
    warnBody.style.padding = "12px 16px";
    warnBody.style.display = "flex";
    warnBody.style.gap = "8px";
    warnBody.style.alignItems = "flex-start";
    warnBody.append(icon("warn", { size: "sm" }));
    const text_ = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = `有 ${activeTaskCount} 个任务正在运行`;
    const p = document.createElement("p");
    p.style.margin = "2px 0 0";
    p.style.fontSize = "12.5px";
    p.style.color = "var(--ink-2)";
    p.textContent = "任务运行期间，清空任务记录、日志、诊断，删除全部密钥和完整重置暂时不能用。等任务结束后再操作。";
    text_.append(strong, p);
    warnBody.append(text_);
    warnCard.append(warnBody);
    host.append(warnCard);
  }

  // 本地数据：以前这里一张「分类」卡、下面再一张「单类重置」卡，把任务记录、日志、密钥、
  // 设置列了两遍，按钮一处叫「清理」一处叫「清空 / 重置」。现在只留这一张。
  const overviewCard = createCard([tcHead("本地数据")]);
  const overviewDesc = document.createElement("p");
  overviewDesc.className = "note";
  overviewDesc.style.padding = "0 16px";
  overviewDesc.style.fontSize = "12px";
  overviewDesc.style.color = "var(--ink-3)";
  overviewDesc.textContent = "每一项只清理 Translator 自己保存的数据，源文件和翻译结果文件不会被删除。";
  overviewCard.append(overviewDesc);
  const guarded = new Set(Array.isArray(maintenanceOverview?.activity_guarded_categories) ? maintenanceOverview.activity_guarded_categories as string[] : []);
  const categoryNotes: Record<string, string> = {
    settings: "恢复默认设置，密钥和记忆库会保留",
    keys: "只显示数量，不显示密钥内容",
  };
  const categories = maintenanceCategories();
  if (!categories.length) {
    overviewCard.append(createEmptyState({ title: "尚未读取本地数据概览", description: "点击上方“刷新”查看本地数据的位置和各类占用。", icon: "folder" }));
  } else {
    for (const category of categories) {
      const name = document.createElement("span");
      name.style.flex = "1";
      const b = document.createElement("b");
      b.textContent = category.label;
      const small = document.createElement("small");
      small.style.display = "block";
      small.style.color = "var(--ink-3)";
      small.style.fontSize = "11px";
      const count = category.id === "keys" ? record(category).key_count ?? category.count : category.count;
      const sizeText = `${formatBytes(category.size_bytes)}${count !== undefined ? ` · ${count} 项` : ""}`;
      small.textContent = categoryNotes[category.id] ? `${sizeText} · ${categoryNotes[category.id]}` : sizeText;
      name.append(b, small);
      const action = category.contains_user_output
        ? createChip({ label: "受保护", tone: "mute" })
        : category.clearable
          ? createButton({
            label: maintenanceVerb(category.id), size: "mini", variant: "danger",
            disabled: guarded.has(category.id) && activeTaskCount > 0,
            onClick: () => requestMaintenanceClear(category.id as MaintenanceClearCategory),
          })
          : createChip({ label: "自动维护", tone: "mute" });
      overviewCard.append(connRow([name, action]));
    }
  }
  const quickStartName = document.createElement("span");
  quickStartName.style.flex = "1";
  const quickStartLabel = document.createElement("b");
  quickStartLabel.textContent = "快速开始";
  const quickStartMeta = document.createElement("small");
  quickStartMeta.style.display = "block";
  quickStartMeta.style.color = "var(--ink-3)";
  quickStartMeta.style.fontSize = "11px";
  quickStartMeta.textContent = "重新显示欢迎引导";
  quickStartName.append(quickStartLabel, quickStartMeta);
  overviewCard.append(connRow([
    quickStartName,
    createButton({ label: "重新显示", size: "mini", onClick: () => void showQuickStart() }),
  ]));
  host.append(overviewCard);

  // 留存边界
  const taskHistory = maintenanceCategory("task_history");
  const logs = maintenanceCategory("logs");
  const historyLimit = num(record(taskHistory).retention_limit, num(limits.task_history_max_records, 200));
  const logsLimit = record(record(logs).retention);
  const diagLimit = diagnosticsOverview ?? record(limits.diagnostics);
  const limitsCard = createCard([]);
  const limitsBody = document.createElement("div");
  limitsBody.style.padding = "14px 16px";
  limitsBody.style.display = "flex";
  limitsBody.style.flexDirection = "column";
  limitsBody.style.gap = "6px";
  limitsBody.append(sectionLabel("自动保留上限"));
  const limitRows: [string, string][] = [
    ["任务记录", `最近 ${historyLimit} 条`],
    ["运行日志", `${num(logsLimit.max_files, 5)} 个 × ${formatBytes(logsLimit.max_file_bytes || 5 * 1024 * 1024)}`],
    ["诊断", `${num(diagLimit.max_records, 80)} 条 / ${formatBytes(diagLimit.max_total_bytes || 256 * 1024 * 1024)}`],
  ];
  for (const [label, value] of limitRows) {
    const row = document.createElement("div");
    row.style.display = "flex";
    row.style.justifyContent = "space-between";
    row.style.fontSize = "12.5px";
    const l = document.createElement("span");
    l.style.color = "var(--ink-3)";
    l.textContent = label;
    const v = document.createElement("b");
    v.textContent = value;
    row.append(l, v);
    limitsBody.append(row);
  }
  const note = document.createElement("p");
  note.className = "note";
  note.style.fontSize = "12px";
  note.style.color = "var(--ink-3)";
  note.textContent = "维护操作不会删除源文件、用户输出目录或已生成的翻译文件。";
  limitsBody.append(note);
  limitsCard.append(limitsBody);
  host.append(limitsCard);

  // 诊断
  const diagCard = createCard([tcHead("诊断", [
    createButton({ label: "导出全部诊断", size: "mini", onClick: () => void exportDiagnostics("/api/diagnostics/history.zip", "translator-diagnostics.zip") }),
    createButton({ label: "清空诊断", size: "mini", variant: "danger", disabled: activeTaskCount > 0, onClick: () => requestMaintenanceClear("diagnostics") }),
  ])]);
  const diagDesc = document.createElement("p");
  diagDesc.className = "note";
  diagDesc.style.padding = "0 16px";
  diagDesc.style.fontSize = "12px";
  diagDesc.style.color = "var(--ink-3)";
  diagDesc.textContent = "诊断只记录版本、系统、任务进行到哪一步、隐去密钥的连接信息、计数和错误代码，不会自动上传。";
  diagCard.append(diagDesc);
  const tableWrap = document.createElement("div");
  tableWrap.style.overflowX = "auto";
  tableWrap.style.padding = "8px 0 4px";
  const table = document.createElement("table");
  table.className = "tbl";
  const thead = document.createElement("thead");
  thead.innerHTML = "<tr><th>记录</th><th>类型</th><th>大小</th><th></th></tr>";
  table.append(thead);
  const tbody = document.createElement("tbody");
  if (!diagnostics.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.className = "num";
    td.style.textAlign = "left";
    td.style.color = "var(--ink-3)";
    td.textContent = "没有诊断记录。";
    tr.append(td);
    tbody.append(tr);
  } else {
    for (const item of diagnostics) {
      const tr = document.createElement("tr");
      const id = text(item.record_id);
      const title = text(item.created_at, "诊断记录");
      const type = [text(item.surface), text(item.phase)].filter(Boolean).join(" · ") || "应用";
      const tdTitle = document.createElement("td");
      tdTitle.textContent = title;
      const tdType = document.createElement("td");
      tdType.textContent = type;
      const tdSize = document.createElement("td");
      tdSize.textContent = formatBytes(item.size_bytes);
      const tdActions = document.createElement("td");
      tdActions.append(fieldRow([
        createButton({ label: "导出", size: "mini", onClick: () => void exportDiagnostics(`/api/diagnostics/${encodeURIComponent(id)}.zip`, "translator-diagnostic.zip") }),
        createButton({
          label: "删除", size: "mini", variant: "danger", disabled: activeTaskCount > 0,
          onClick: () => void reRenderAfter(async () => {
            await client.request(`/api/diagnostics/${encodeURIComponent(id)}`, { method: "DELETE" });
            await refreshMaintenance();
            showToast({ message: "诊断记录已删除。" });
          }),
        }),
      ]));
      tr.append(tdTitle, tdType, tdSize, tdActions);
      tbody.append(tr);
    }
  }
  table.append(tbody);
  tableWrap.append(table);
  diagCard.append(tableWrap);
  host.append(diagCard);


  // 翻译记忆库
  const tm = maintenanceCategory("tm");
  const tmCard = createCard([]);
  const tmBody = document.createElement("div");
  tmBody.style.padding = "14px 16px";
  tmBody.style.display = "flex";
  tmBody.style.flexDirection = "column";
  tmBody.style.gap = "8px";
  tmBody.append(sectionLabel("记忆库"));
  const tmDesc = document.createElement("p");
  tmDesc.className = "note";
  tmDesc.style.fontSize = "12px";
  tmDesc.style.color = "var(--ink-3)";
  tmDesc.textContent = `建议先导出备份再清空，不会删除任何翻译结果。当前总占用：${tm ? formatBytes(tm.size_bytes) : "--"}。`;
  tmBody.append(tmDesc);
  tmBody.append(fieldRow([
    createButton({ label: "前往记忆库导出", size: "mini", onClick: () => navigate("library") }),
  ]));

  const tmPairOptions = tmPairCatalog?.recent ?? [];
  if (tmPairOptions.length) {
    if (!tmPairOptions.includes(selectedTmClearPair)) {
      selectedTmClearPair = tmPairOptions[0];
    }
    const pairSelect = selectField(
      "清空所选语言对",
      tmPairOptions.map((pair) => ({ value: pair, label: tmPairLabel(pair) })),
      selectedTmClearPair,
      (value) => { selectedTmClearPair = value; renderBody(); },
      { hint: "候选来自最近使用过的语言对，与记忆库页面的「最近使用」一致。" },
    );
    tmBody.append(pairSelect.root);
    tmBody.append(fieldRow([
      createButton({
        label: `清空 ${tmPairLabel(selectedTmClearPair)}`, size: "mini", variant: "danger",
        disabled: activeTaskCount > 0,
        onClick: () => requestMaintenanceClear("tm", selectedTmClearPair),
      }),
      createButton({ label: "清空全部记忆库", size: "mini", variant: "danger", disabled: activeTaskCount > 0, onClick: () => requestMaintenanceClear("tm") }),
    ]));
  } else {
    tmBody.append(fieldRow([
      createButton({ label: "清空全部记忆库", size: "mini", variant: "danger", disabled: activeTaskCount > 0, onClick: () => requestMaintenanceClear("tm") }),
    ]));
  }
  tmCard.append(tmBody);
  host.append(tmCard);

  // 完整本地重置
  const dangerCard = createCard([], "danger-section");
  const dangerBody = document.createElement("div");
  dangerBody.style.padding = "14px 16px";
  dangerBody.style.display = "flex";
  dangerBody.style.flexDirection = "column";
  dangerBody.style.gap = "8px";
  dangerBody.append(sectionLabel("完整本地重置"));
  const dangerDesc = document.createElement("p");
  dangerDesc.style.fontSize = "12.5px";
  dangerDesc.style.color = "var(--ink-2)";
  dangerDesc.textContent = "只删除 Translator 自己保存的数据，不会删除应用本身、你的源文件和翻译结果。完成后需要重新打开应用。";
  dangerBody.append(dangerDesc);
  dangerBody.append(createButton({
    label: "完整重置", variant: "danger-solid", disabled: activeTaskCount > 0,
    onClick: () => requestMaintenanceClear("full_reset"),
  }));
  dangerCard.append(dangerBody);
  host.append(dangerCard);
}

function requestMaintenanceClear(category: MaintenanceClearCategory, langPair?: string): void {
  if (category !== "settings" && activeTaskCount > 0) {
    showToast({ message: "有任务正在运行，暂时不能执行这项操作。", error: true });
    return;
  }
  const copy = MAINTENANCE_CONFIRM_COPY[category];
  const memoryCount = maintenanceCategory("tm")?.count;
  if (category === "full_reset") {
    let handle: ModalHandle;
    handle = openModal({
      tone: "danger", icon: "trash",
      sourceLabel: "设置 · 数据与维护 · 完整本地重置",
      title: "完整本地重置",
      body: [
        `将永久删除所有设置、API Key、记忆库${memoryCount !== undefined ? `（${memoryCount} 条）` : ""}与任务历史，应用回到首次启动状态。此操作无法恢复。`,
      ],
      confirmInput: { placeholder: "RESET", matchValue: "RESET" },
      actions: [
        { label: "取消" },
        {
          label: "永久删除", variant: "danger-solid", keepOpen: true,
          onClick: async () => {
            try {
              await client.request<JsonObject>("/api/maintenance/reset-full", {
                method: "POST", body: JSON.stringify({ confirmation: true, phrase: "RESET" }),
              });
              handle.close();
              openModal({
                tone: "warn", icon: "check",
                title: "本地数据已重置",
                body: ["本地数据已删除。请关闭并重新打开 Translator，从头开始使用。"],
                actions: [{ label: "知道了", variant: "primary" }],
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
    body.push(`范围：仅清空语言对 ${tmPairLabel(langPair)}（${langPair}）。`);
  }
  openModal({
    tone: "danger", icon: "trash",
    sourceLabel: `设置 · 数据与维护 · ${copy.title.replace("？", "")}${langPair ? ` · ${langPair}` : ""}`,
    title: copy.title,
    body,
    actions: [
      { label: "取消" },
      {
        label: langPair ? `清空 ${tmPairLabel(langPair)}` : copy.confirm, variant: "danger-solid",
        onClick: () => void reRenderAfter(async () => {
          await client.request<JsonObject>("/api/maintenance/clear", {
            method: "POST", body: JSON.stringify({ category, lang_pair: langPair, confirmation: true }),
          });
          if (category === "settings") await refreshSettings();
          await refreshMaintenance();
          // 维护清理可能改变数据健康状态（比如清空 Key 后旧横幅不再成立），
          // 拉一次让横幅与后端真相对齐。
          void refreshDataHealth(mountToken);
          showToast({ message: "已完成，翻译结果没有受影响。" });
        }),
      },
    ],
  });
}

async function openAppDataDirectory(): Promise<void> {
  const path = text(maintenanceOverview?.app_data_dir);
  if (!path) {
    showToast({ message: "本地数据目录尚未加载。", error: true });
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
  if (sameDay) return `今天 ${clock}`;
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())} ${clock}`;
}

/** GitHub 的 published_at 是完整 ISO 时间戳，界面上只要日期。 */
export function formatReleaseDate(iso: string): string {
  const when = new Date(iso);
  if (!iso || Number.isNaN(when.getTime())) return iso || "未提供";
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

  const card = createCard([]);
  const body = document.createElement("div");
  body.style.padding = "14px 16px";
  body.style.display = "flex";
  body.style.flexDirection = "column";
  body.style.gap = "10px";
  body.append(sectionLabel("版本"));

  if (snapshot.flow.phase === "ready") {
    renderUpdateReady(body, { paused });
  } else if (snapshot.flow.phase === "downloading" || snapshot.flow.phase === "installing") {
    renderUpdateProgress(body);
  } else if (snapshot.result) {
    // 「正在检查」不在这里画：顶部的提示卡片已经在转圈了，这一页再来一个空态，
    // 同一件事说两遍，而且原来的内容会整块消失一下再回来。检查期间按钮禁用即可。
    renderUpdateResult(body, { paused, ignored });
  } else {
    renderUpdateUnchecked(body, { paused });
  }

  card.append(body);
  host.append(card);

  const licenseCard = createCard([]);
  const licenseBody = document.createElement("div");
  licenseBody.style.padding = "14px 16px";
  licenseBody.style.display = "flex";
  licenseBody.style.flexDirection = "column";
  licenseBody.style.gap = "6px";
  licenseBody.append(sectionLabel("开源许可"));
  const licenseText = document.createElement("p");
  licenseText.style.fontSize = "12.5px";
  licenseText.style.color = "var(--ink-2)";
  licenseText.textContent = "MIT License";
  const licenseNote = document.createElement("p");
  licenseNote.className = "note";
  licenseNote.style.fontSize = "12px";
  licenseNote.style.color = "var(--ink-3)";
  licenseNote.textContent = "完整许可文本见项目仓库中的 LICENSE 文件。";
  licenseBody.append(licenseText, licenseNote);
  licenseCard.append(licenseBody);
  host.append(licenseCard);
}

/** 屏①：还没有任何检查结果。进这一页不自动发起检查——启动时后台已经跑过一次了。 */
function renderUpdateUnchecked(body: HTMLElement, options: { paused: boolean }): void {
  const info = document.createElement("p");
  info.style.fontSize = "13px";
  info.textContent = `当前版本：${APP_VERSION_FALLBACK}`;
  body.append(info);

  const hint = document.createElement("p");
  hint.className = "note";
  hint.textContent = options.paused
    ? "后台提醒已暂停，只有手动检查才会联网查询新版本。"
    : "应用每次启动会在后台检查一次更新；发现新版时会在左侧「设置」上点亮红点。";
  body.append(hint);

  body.append(fieldRow([
    createButton({
      label: updateSnapshot().checking ? "检查中…" : "检查更新",
      variant: "primary", size: "mini",
      disabled: updateSnapshot().checking,
      onClick: () => void runUpdateCheck(),
    }),
    createPauseToggle(options.paused),
  ]));
}

/** 屏②③⑥⑦：有检查结果时的四种落点。安装失败态叠加在这一层上。 */
function renderUpdateResult(body: HTMLElement, options: { paused: boolean; ignored: string }): void {
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
  info.append(document.createTextNode(`当前版本：${currentVersion}`));
  if (latestVersion) {
    info.append(document.createTextNode(" · 最新版本："));
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
      createChip({ label: flow.failureTitle || "安装失败", tone: "warn", icon: "warn" }),
    );
  } else if (available) {
    body.append(createChip({ label: "有可用更新", tone: "tint", icon: "down" }));
  } else if (status === "current") {
    body.append(createChip({ label: "已是最新", tone: "ok", icon: "check" }));
  } else if (status === "release_not_ready") {
    body.append(createChip({ label: "发布包未就绪", tone: "warn", icon: "warn" }));
  } else if (status === "unsupported_platform") {
    body.append(createChip({ label: "不支持自动更新", tone: "warn", icon: "warn" }));
  } else {
    body.append(createChip({ label: "检查失败", tone: "warn", icon: "warn" }));
  }

  if (available && !failed) {
    appendReleaseNotes(body, text(result.release_notes));

    const dl = document.createElement("dl");
    dl.className = "kv";
    const size = num(result.asset_size);
    const rows: [string, string][] = [
      ["安装包", text(result.asset_name, "--")],
      ["大小", size > 0 ? formatBytes(size) : "--"],
      ["发布日期", formatReleaseDate(text(result.release_date))],
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
    body.append(diagnosticNote(flow.message, flow.code));
  } else if (status === "current") {
    const checked = formatUpdateCheckedAt(snapshot.checkedAt);
    if (checked) {
      const note = document.createElement("p");
      note.className = "note";
      note.textContent = `上次检查：${checked}`;
      body.append(note);
    }
  } else {
    body.append(diagnosticNote(text(result.message, "检查更新失败，请稍后重试。"), text(result.diagnostic_code)));
  }

  const actions: HTMLElement[] = [];
  if (available && !failed) {
    actions.push(selfUpdate
      ? createButton({
        label: "下载并安装", variant: "primary", size: "mini", icon: "down",
        onClick: () => void requestUpdateInstall(),
      })
      : createButton({
        label: "下载安装包", variant: "primary", size: "mini", icon: "down",
        disabled: !downloadUrl,
        onClick: () => void openExternalUrl(downloadUrl),
      }));
  } else {
    actions.push(createButton({
      label: failed ? "重试" : snapshot.checking ? "检查中…" : "重新检查",
      variant: failed || status !== "current" ? "primary" : "default",
      size: "mini",
      disabled: snapshot.checking,
      onClick: () => void (failed ? requestUpdateInstall() : runUpdateCheck()),
    }));
    if (failed && downloadUrl) {
      actions.push(createButton({
        label: "改为下载安装包", size: "mini", icon: "down",
        onClick: () => void openExternalUrl(downloadUrl),
      }));
    }
  }
  if (available && latestVersion && options.ignored !== latestVersion) {
    actions.push(createButton({
      label: "忽略此版本", size: "mini",
      onClick: () => void ignoreVersion(latestVersion),
    }));
  }
  if (!failed) {
    actions.push(createPauseToggle(options.paused));
  }
  if (releaseUrl) {
    actions.push(createButton({ label: "查看 Release", size: "mini", icon: "ext", onClick: () => void openExternalUrl(releaseUrl) }));
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
  heading.append(document.createTextNode("正在更新到 "));
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

  const button = createButton({ label: "下载中…", size: "mini", disabled: true });
  body.append(fieldRow([button]));

  const apply = () => {
    const snapshot = updateSnapshot();
    const flow = snapshot.flow;
    const downloading = flow.phase === "downloading";
    const determinate = downloading && flow.percent !== null;

    version.textContent = flow.version || text(snapshot.result?.latest_version, "新版本");
    label.textContent = downloading ? "正在下载安装包" : "正在校验签名并安装";
    pct.textContent = determinate ? `${Math.floor(flow.percent ?? 0)}%` : "";

    const barClass = determinate ? "bar" : "bar indet";
    if (progress.root.className !== barClass) progress.root.className = barClass;
    // 不确定态的宽度归 CSS 管（.bar.indet i 是固定 34% 的滑块），内联宽度必须清掉。
    if (determinate) progress.setPercent(flow.percent ?? 0);
    else (progress.root.firstElementChild as HTMLElement).style.width = "";

    note.textContent = downloading
      ? (flow.total
        ? `${formatBytes(flow.received)} / ${formatBytes(flow.total)}`
        : `已下载 ${formatBytes(flow.received)}`)
      : "这一步通常几秒钟，请勿关闭窗口。";
    button.textContent = downloading ? "下载中…" : "安装中…";
  };
  apply();
  updateProgressView = { root: body, apply };
}

/** 屏⑤：装好了但不自动重启——翻译任务动辄几十分钟，替用户做这个决定的代价太大。 */
function renderUpdateReady(body: HTMLElement, opts: { paused: boolean }): void {
  const snapshot = updateSnapshot();
  const flow = snapshot.flow;
  if (snapshot.readyCollapsed) {
    // 点过「稍后」。横幅收起来了，但更新包已经装在磁盘上：这里必须留一个重启入口，
    // 否则这一页会退回去重新提供「下载并安装」，等于让人把同一份包再下一遍。
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = `${flow.version} 已安装完成，重启 Translator 后生效。`;
    body.append(note);
    // 更新装好之后到重启为止可能隔着一整天的工作，这一页不能只剩一个重启按钮：
    // 后台提醒的开关得一直够得着。
    body.append(fieldRow([
      createButton({
        label: "立即重启", size: "mini", icon: "restart",
        onClick: () => void requestRestart(),
      }),
      createPauseToggle(opts.paused),
    ]));
    return;
  }
  body.append(createBanner({
    title: `${flow.version} 已安装完成`,
    subtitle: "重启 Translator 后生效。也可以稍后手动退出再打开。",
    icon: "check",
    actions: [
      createButton({ label: "立即重启", variant: "primary", size: "mini", icon: "restart", onClick: () => void requestRestart() }),
      createButton({ label: "稍后", size: "mini", onClick: collapseUpdateReady }),
    ],
  }));
}

function createPauseToggle(paused: boolean): HTMLButtonElement {
  return createButton({
    label: paused ? "恢复后台提醒" : "暂停后台提醒", size: "mini",
    onClick: () => void setNotificationsPaused(!paused),
  });
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
    codeEl.textContent = `诊断代码：${code}`;
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
    empty.textContent = "这一版没有提供更新说明。";
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
  toggle.textContent = updateNotesExpanded ? "收起更新说明" : "展开完整更新说明";
  toggle.addEventListener("click", () => {
    updateNotesExpanded = !updateNotesExpanded;
    renderAboutIfVisible();
  });
  body.append(toggle);
}
