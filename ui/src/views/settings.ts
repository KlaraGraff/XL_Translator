// 设置视图 —— 五个子页：模型服务 / 翻译参数 / 外观与语言 / 数据与维护 / 更新与关于。
// 收编自旧版 ui/src/main.ts 的左侧模型配置抽屉 + 详细设置折叠组 + 维护与诊断页 +
// 更新检查弹层。main.ts 保持只读，仅供本文件对照业务语义，不被 import。
// params 约定：{ page?: "models" | "params" | "appearance" | "data" | "about" }，
// 用于从右栏「编辑 Prompt ↗ 设置」、顶栏模型药丸等处深链到具体子页。

import type { ViewParams } from "../router";
import { setTopbar, setSettingsAlert } from "../shell";
import {
  createCard,
  createChip,
  createButton,
  createSwitchRow,
  createField,
  createHintBadge,
  createEmptyState,
  createBanner,
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
import { showQuickStart } from "../quickstart";
import { applyModelPillFromRoles } from "../model-pill";
import { version as PACKAGE_VERSION } from "../../package.json";
import "./settings.css";

// ---------------------------------------------------------------------------
// 类型（本地精简版，对照 main.ts 的同名类型/字段）
// ---------------------------------------------------------------------------

type JsonObject = Record<string, unknown>;
type SettingsPage = "models" | "params" | "appearance" | "data" | "about";
type TranslationSurface = "excel" | "word";
type ParamsSurface = "excel" | "word" | "pdf";

type PoolConnection = {
  id: string;
  label: string;
  display_label: string;
  provider: string;
  model: string;
  base_url: string;
  availability_status: string;
  availability_message: string;
  availability_checked_at: string;
  has_api_key: boolean;
  api_key_preview: string;
  primary: boolean;
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

type ModelImportPreview = {
  fileName: string;
  payload: JsonObject;
  roles: { role: string; fields: string[] }[];
  throughput_profile_count: number;
  api_key_count: number;
};

// GET /api/data/health 的字段名是另一路改动定死的契约，这里原样对照，不要改名。
type DataHealthEntry = {
  state: "current" | "adopted" | "recreated" | "upgraded" | "unreadable" | string;
  // 全新安装时磁盘上还没有文件，服务端这里给的是 null。
  stored_version: number | null;
  current_version: number;
  backup_path: string;
};
type DataHealthPayload = {
  settings: DataHealthEntry;
  tm: DataHealthEntry;
};

// ---------------------------------------------------------------------------
// 小工具（对照 main.ts 的 record/text/number/errorMessage/nestedPatch）
// ---------------------------------------------------------------------------

function record(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" ? value : fallback;
}

function errorMessage(error: unknown): string {
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

function formatCheckedAt(value: string): string {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return `测试于 ${new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(parsed)}`;
}

// ---------------------------------------------------------------------------
// 内置领域 Prompt（原样迁移自 main.ts；服务端仍是最终 Prompt 的权威来源，
// 这里只保留可见、可编辑的内置文本，占位符/协议追加逻辑不在前端暴露）。
// ---------------------------------------------------------------------------

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

function domainBuiltInPrompt(preset: string, targetLang: string): string {
  const prompts = BUILTIN_DOMAIN_PROMPTS[preset];
  if (!prompts) return "";
  return prompts[targetLang] || prompts._base || "";
}

// 顺序必须和 config.py 的 DOMAIN_PRESETS 一致：「无」排第一，表示不注入任何领域提示词。
// 少列一项不是「少个选项」而已——下拉里找不到当前值时浏览器会静默选中第 0 项，保存时
// 就把用户当前的领域改掉，还会把空白 Prompt 当成覆盖写进另一个领域里。
const DOMAIN_PRESET_OPTIONS = ["无", "同步工程场景", "资料管理场景", "行政生活化场景", "自定义"];

const MODEL_ROLE_LABELS: Record<string, string> = {
  translation: "文档翻译（Excel / Word）",
  cleaner: "记忆库清洗",
  image: "PDF 翻译（图像生成）",
  pdf_review: "PDF 译文审核",
};
const MODEL_ROLE_ORDER = ["translation", "cleaner", "image", "pdf_review"];
const FOLLOW_PREFIX = "follow:";

const CLOUD_PROVIDERS = ["custom_openai", "openai", "claude", "zhipu", "dashscope", "siliconflow", "deepseek"];
const LOCAL_PROVIDERS = ["ollama", "lm_studio", "custom_local"];
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
function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}

// 服务商切换时的 Base URL 预填表，来自 GET /api/models/provider-defaults。这里不留
// 第二份硬编码：URL 一旦在前后端各存一份，服务商换了端点就会有一边是错的，而错的那
// 一边正好是用户看得见的那份。接口没回来之前预填表为空 —— 只是不预填，不会挡住手填。
let providerBaseUrlDefaults: Record<string, string> = {};
let providerModelDefaults: Record<string, string> = {};
let providerBaseUrlDisabled = new Set<string>();
let disabledBaseUrlPlaceholder = "当前服务商无需填写 Base URL";

const MAINTENANCE_CONFIRM_COPY: Record<MaintenanceClearCategory, { title: string; message: string; confirm: string }> = {
  task_history: { title: "清空任务摘要？", message: "将删除当前应用保存的任务摘要，不删除任何输出文件。", confirm: "清空任务摘要" },
  logs: { title: "清空结构化日志？", message: "将删除当前应用日志，不删除诊断、源文件或翻译输出。", confirm: "清空日志" },
  diagnostics: { title: "清空全部诊断？", message: "将删除所有本地诊断记录。导出过的 ZIP 不会被删除。", confirm: "清空诊断" },
  keys: { title: "删除全部 API Key？", message: "将删除当前应用保存的所有服务商 Key。活动任务期间不能执行此操作。", confirm: "删除全部 Key" },
  settings: { title: "重置设置？", message: "将恢复设置默认值，但保留 API Key、记忆库和翻译输出。", confirm: "重置设置" },
  tm: { title: "清空翻译记忆库？", message: "此操作不会删除翻译输出。建议先在记忆库页面导出 JSON 备份。", confirm: "清空 TM" },
  full_reset: { title: "完整重置本地数据？", message: "只删除 Translator 当前应用数据。应用、DMG、源码、旧版目录、源文件和输出目录不会被触及。", confirm: "完整重置" },
};

// ---------------------------------------------------------------------------
// 模块状态（单例：设置页同一时刻只会挂载一份）
// ---------------------------------------------------------------------------

const client = new ApiClient();
let connectPromise: Promise<void> | null = null;
async function ensureConnected(): Promise<void> {
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
let mountToken = 0;
let dataHealth: DataHealthPayload | null = null;

let settings: JsonObject | null = null;
let modelRoles: Record<string, JsonObject> = {};
let modelThroughput: Record<string, JsonObject> = {};
let modelCatalog: Record<string, string[]> = {};
let modelCatalogMessage: Record<string, string> = {};
let modelCatalogConnection: Record<string, string> = {};
let selectedConnection: Record<string, string> = {};
let modelAccessDraft: Record<string, string> = {};
let modelRole = "translation";
let modelImportPreview: ModelImportPreview | null = null;

let targetOptions: LanguageOption[] = [];

let updateState: JsonObject | null = null;
let updateResult: JsonObject | null = null;
let updateChecking = false;

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

// ---------------------------------------------------------------------------
// mount / unmount
// ---------------------------------------------------------------------------

export function mount(container: HTMLElement, params: ViewParams): void {
  const token = ++mountToken;
  const requestedPage = params.page;
  currentPage = requestedPage === "models" || requestedPage === "params" || requestedPage === "appearance"
    || requestedPage === "data" || requestedPage === "about"
    ? requestedPage
    : "models";

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

  void bootstrap(token);
}

export function unmount(): void {
  mountToken += 1; // 让任何仍在飞行中的异步回调失效
  // 同 workspace：这些浮层挂在 document.body 上，视图切走不会自动消失。
  hideHint();
  closeLanguagePopover();
  closeMenu();
  bodyHost = null;
  bannerHost = null;
  navEls = null;
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
    await refreshUpdateState();
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
async function refreshDataHealth(token: number): Promise<void> {
  try {
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

function dataHealthRecreatedMessages(payload: DataHealthPayload | null): string[] {
  if (!payload) return [];
  const messages: string[] = [];
  if (payload.settings?.state === "recreated") {
    messages.push(
      `旧的配置无法读取，已备份到 ${payload.settings.backup_path || "备份目录"}，已新建一份可用的。`,
    );
  }
  if (payload.tm?.state === "recreated") {
    messages.push(
      `旧的翻译记忆库无法读取，已备份到 ${payload.tm.backup_path || "备份目录"}，已新建一份可用的。`,
    );
  }
  return messages;
}

// unreadable：文件还在，但系统不让打开（权限，或被杀毒/备份软件占着）。什么都没丢，
// 但也什么都存不进去——不报出来的话，用户看到的是一个正常的设置页，然后每次保存都失败。
function dataHealthBlockedMessages(payload: DataHealthPayload | null): string[] {
  if (!payload) return [];
  const messages: string[] = [];
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
  const messages = blocked.length ? blocked : dataHealthRecreatedMessages(dataHealth);
  if (!messages.length) return;
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
            await client.request("/api/data/health/notice", { method: "DELETE" });
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
  } catch (error) {
    if (token !== mountToken) return;
    showToast({ message: errorMessage(error), error: true });
  }
  if (token !== mountToken) return;
  renderBody();
}

function renderBody(): void {
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
let applyProviderDerivedState: (value: string) => void = () => {};

// 同上，由模型表单渲染时接上：把当前表单里的值提交给后端。
// 「获取模型列表」「测试连接」和字段失焦自动保存都走它——这三处以前用的都是**服务端
// 已保存的那份配置**，用户刚改完密钥点「测试连接」，测的是改之前那把旧密钥，永远失败。
let submitModelForm: (() => Promise<void>) | null = null;
// 表单提交串行化：失焦自动保存和按钮触发的保存可能挨得很近（点按钮先触发 blur 再触发
// click），两个 PUT 并行发出去时后端最后落哪份取决于响应顺序。所有提交排进同一条链。
let modelFormSaveChain: Promise<void> = Promise.resolve();

function queueModelFormSave(run: () => Promise<void>): Promise<void> {
  const next = modelFormSaveChain.then(run, run);
  // 链上任何一环失败都不能让后续提交全部短路，所以这里吞掉错误只用于排队；
  // 错误照常沿 next 抛给真正的调用方处理。
  modelFormSaveChain = next.catch(() => undefined);
  return next;
}

// 一次提交所需的全部信息：输入框里的值，加上「这份表单是在哪个角色、哪种连接方式、
// 选中哪条连接的情况下填的」。后者在渲染时就固定下来，排队等待期间界面怎么变都不影响
// 这一份该怎么存。
type ModelFormSubmission = {
  role: string;
  access: string;
  sourceRole: string;
  secondaryId: string;
  selectedId: string;
  selectedLabel: string;
  provider: string;
  baseUrl: string;
  model: string;
  apiKey: string;
  connectionLabel: string;
};

type ModelFormDraft = { scope: string; values: Record<string, string> };

function modelFormScope(): string | null {
  const el = document.getElementById(MODEL_FORM_SCOPE_FIELD_ID) as HTMLSelectElement | null;
  return el?.dataset.formScope ?? null;
}

function snapshotModelFormDraft(): ModelFormDraft | null {
  const scope = modelFormScope();
  if (scope === null) return null;
  const values: Record<string, string> = {};
  for (const id of MODEL_FORM_DRAFT_FIELD_IDS) {
    const el = document.getElementById(id) as HTMLInputElement | HTMLSelectElement | null;
    if (el) values[id] = el.value;
  }
  return { scope, values };
}

function restoreModelFormDraft(draft: ModelFormDraft): void {
  if (modelFormScope() !== draft.scope) return;
  for (const [id, value] of Object.entries(draft.values)) {
    const el = document.getElementById(id) as HTMLInputElement | HTMLSelectElement | null;
    if (el) el.value = value;
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
async function reRenderAfter<T>(
  action: () => Promise<T>,
  opts: { preserveDraft?: boolean } = {},
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
    if (token === mountToken) showToast({ message: errorMessage(error), error: true });
    return undefined;
  }
}

// ---------------------------------------------------------------------------
// 数据获取 / 持久化（对照 main.ts 的 refreshSettings / persistSettings 等）
// ---------------------------------------------------------------------------

async function refreshSettings(): Promise<void> {
  settings = await client.request<JsonObject>("/api/settings");
  await refreshProviderDefaults();
  await refreshModelRoles();
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

async function refreshModelRoles(): Promise<void> {
  const payload = await client.request<{ roles: Record<string, JsonObject> }>("/api/models/roles");
  modelRoles = payload.roles || {};
  // 顶栏药丸跟着同一份数据走：保存、切换连接、测试连通性之后都会经过这里。
  applyModelPillFromRoles(modelRoles);
  await Promise.all(Object.keys(modelRoles).map((role) => refreshModelThroughput(role)));
}

async function refreshModelThroughput(role: string): Promise<void> {
  try {
    modelThroughput[role] = await client.request<JsonObject>(`/api/models/throughput/${encodeURIComponent(role)}`);
  } catch {
    modelThroughput[role] = record(modelRoles[role]?.throughput);
  }
}

async function refreshLanguages(): Promise<void> {
  const payload = await client.request<{
    languages: LanguageOption[];
    source_options: LanguageOption[];
    target_options: LanguageOption[];
  }>("/api/languages");
  targetOptions = payload.target_options;
}

async function refreshUpdateState(): Promise<void> {
  updateState = await client.request<JsonObject>("/api/updates/state");
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

async function persistSettings(patch: JsonObject): Promise<void> {
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

function sectionLabel(labelText: string): HTMLDivElement {
  const div = document.createElement("div");
  div.className = "rp-sec";
  div.textContent = labelText;
  return div;
}

function hintBadge(hint: string): HTMLSpanElement {
  return createHintBadge(hint, "swrow-hint");
}

function fieldWithHint(labelText: string, control: HTMLElement, hint?: string): HTMLDivElement {
  const field = createField(labelText, control);
  if (hint) {
    const label = field.querySelector("label");
    if (label) label.append(hintBadge(hint));
  }
  return field;
}

function selectField(
  labelText: string,
  options: { value: string; label: string }[],
  value: string,
  onChange: (value: string) => void,
  opts: { disabled?: boolean; hint?: string } = {},
): { root: HTMLDivElement; select: HTMLSelectElement } {
  const select = document.createElement("select");
  select.disabled = Boolean(opts.disabled);
  for (const opt of options) {
    const optionEl = document.createElement("option");
    optionEl.value = opt.value;
    optionEl.textContent = opt.label;
    if (opt.value === value) optionEl.selected = true;
    select.append(optionEl);
  }
  select.addEventListener("change", () => onChange(select.value));
  const root = fieldWithHint(labelText, select, opts.hint);
  return { root, select };
}

function textField(
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

function numberField(
  labelText: string,
  value: number,
  onCommit: (value: number) => void,
  opts: { min?: number; max?: number; disabled?: boolean; hint?: string } = {},
): HTMLDivElement {
  const input = document.createElement("input");
  input.type = "number";
  input.value = String(value);
  if (opts.min !== undefined) input.min = String(opts.min);
  if (opts.max !== undefined) input.max = String(opts.max);
  input.disabled = Boolean(opts.disabled);
  input.addEventListener("change", () => {
    const parsed = Number(input.value);
    if (!Number.isNaN(parsed)) onCommit(parsed);
  });
  return fieldWithHint(labelText, input, opts.hint);
}

function fieldRow(children: HTMLElement[]): HTMLDivElement {
  const row = document.createElement("div");
  row.className = "field-row";
  row.append(...children);
  return row;
}

function connRow(children: (HTMLElement | string)[], opts: { onClick?: () => void; selected?: boolean } = {}): HTMLDivElement {
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

function tcHead(title: string, tools: HTMLElement[] = []): HTMLDivElement {
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
function attachModelNameDropdown(
  input: HTMLInputElement,
  catalog: string[],
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

function statusChip(label: string, tone: "done" | "error" | ""): HTMLSpanElement {
  const chipTone: ChipTone | undefined = tone === "done" ? "ok" : tone === "error" ? "warn" : "mute";
  return createChip({ label, tone: chipTone });
}

// ---------------------------------------------------------------------------
// 子页①：模型服务
// ---------------------------------------------------------------------------

function savedAccessMode(role: string): string {
  const saved = record(modelRoles[role]);
  const source = text(saved.source_role, "independent");
  if (source !== "independent") return `${FOLLOW_PREFIX}${source}`;
  return text(saved.mode, "cloud");
}

function accessMode(role: string): string {
  return modelAccessDraft[role] ?? savedAccessMode(role);
}

function accessFollowSource(role: string): string {
  const value = accessMode(role);
  return value.startsWith(FOLLOW_PREFIX) ? value.slice(FOLLOW_PREFIX.length) : "independent";
}

function accessModeOptions(role: string): { value: string; label: string }[] {
  const saved = record(modelRoles[role]);
  const sources = Array.isArray(saved.source_role_options)
    ? (saved.source_role_options as unknown[]).map((item) => text(item))
    : ["independent"];
  const options = [{ value: "cloud", label: "云端 API" }];
  if (saved.supports_local !== false) {
    options.push({ value: "local", label: "本地模型" });
  }
  for (const source of sources) {
    if (source === "independent") continue;
    options.push({ value: `${FOLLOW_PREFIX}${source}`, label: `跟随${MODEL_ROLE_LABELS[source] || source}` });
  }
  return options;
}

function roleConnections(role: string): PoolConnection[] {
  const raw = record(modelRoles[role]).connections;
  return Array.isArray(raw) ? (raw as PoolConnection[]) : [];
}

function activeConnection(role: string): PoolConnection | null {
  const connections = roleConnections(role);
  if (!connections.length) return null;
  const wanted = selectedConnection[role];
  return connections.find((item) => item.id === wanted) ?? connections[0];
}

// 面板此刻编辑的是不是「本角色自己池子里的一条非主用连接」。三个条件缺一不可：
// 本地模式没有连接列表（activeConnection 会兜底返回云端主连接）；跟随时列表里摆的是
// 来源角色的池子，往那上面写等于改翻译模型的连接；主用连接走的是角色级路由。
// 渲染和保存必须用同一个判断，否则会出现「看到的是 A，存进去的是 B」。
function editableSecondaryConnection(role: string): PoolConnection | null {
  const following = accessFollowSource(role) !== "independent";
  const cloudMode = following || accessMode(role) === "cloud";
  if (!cloudMode || following) return null;
  if (text(record(modelRoles[role]).connection_pool_role, role) !== role) return null;
  const selected = activeConnection(role);
  return selected && !selected.primary ? selected : null;
}

function modelCatalogConnectionKey(args: { role: string; mode: string; provider: string; baseUrl: string }): string {
  return [args.role, args.mode, args.provider, args.baseUrl.trim().replace(/\/$/, "")].join("|");
}

function modelCatalogConnectionForRole(role: string): string {
  const payload = record(modelRoles[role]);
  // 模型列表是按端点缓存的，而拉列表用的是**面板选中的那条连接**（connection_id 一路
  // 传到后端）。键里只写主用连接的端点，第二条连接拉回来的列表就会被当成主用连接的，
  // 切回主用时照样展示——用户会拿 B 家的模型名去填 A 家的连接。
  const secondary = editableSecondaryConnection(role);
  if (secondary) {
    return modelCatalogConnectionKey({
      role,
      mode: "cloud",
      provider: secondary.provider,
      baseUrl: secondary.base_url,
    });
  }
  return modelCatalogConnectionKey({
    role,
    mode: text(payload.mode, "cloud"),
    provider: text(payload.provider),
    baseUrl: text(payload.base_url),
  });
}

function clearModelCatalog(role: string, message = "尚未获取当前连接的模型列表。"): void {
  delete modelCatalog[role];
  delete modelCatalogConnection[role];
  modelCatalogMessage[role] = message;
}

function renderModelsPage(host: HTMLElement): void {
  // 角色切换（4 张卡）
  const seg = document.createElement("div");
  seg.className = "seg";
  for (const role of MODEL_ROLE_ORDER) {
    const payload = record(modelRoles[role]);
    const segc = document.createElement("div");
    segc.className = role === modelRole ? "segc on" : "segc";
    const b = document.createElement("b");
    b.textContent = MODEL_ROLE_LABELS[role] || role;
    const savedSourceRole = text(payload.source_role, "independent");
    const modeSummary = savedAccessMode(role).startsWith(FOLLOW_PREFIX)
      ? `跟随${MODEL_ROLE_LABELS[savedSourceRole] || savedSourceRole}`
      : text(payload.model) || (text(payload.mode, "cloud") === "cloud" ? "云端 API" : "本地模型");
    const span = document.createElement("span");
    span.textContent = modeSummary || "未配置";
    segc.append(b, span);
    segc.addEventListener("click", () => {
      if (modelRole === role) return;
      modelRole = role;
      renderBody();
    });
    seg.append(segc);
  }
  host.append(seg);

  const role = modelRole;
  const rolePayload = record(modelRoles[role]);
  const engine = record(settings?.engine);
  const access = accessMode(role);
  const sourceRole = accessFollowSource(role);
  const following = sourceRole !== "independent";
  const cloudMode = following || access === "cloud";
  const provider = role === "translation"
    ? (cloudMode ? text(engine.cloud_provider, "custom_openai") : text(engine.local_provider, "ollama"))
    : text(rolePayload.provider, cloudMode ? "custom_openai" : "ollama");
  const baseUrl = role === "translation"
    ? (cloudMode ? text(engine.cloud_base_url) : text(engine.local_base_url))
    : text(rolePayload.base_url);
  const model = role === "translation"
    ? (cloudMode ? text(engine.cloud_model) : text(engine.local_model))
    : text(rolePayload.model);
  const providers = cloudMode ? CLOUD_PROVIDERS : LOCAL_PROVIDERS;

  const selected = activeConnection(role);
  const editingSecondary = editableSecondaryConnection(role) !== null;
  const formProvider = editingSecondary ? selected!.provider : provider;
  const formBaseUrl = editingSecondary ? selected!.base_url : baseUrl;
  const formModel = editingSecondary ? selected!.model : model;
  const borrowedPool = following;
  // 这个池子到底是不是本角色自己的。两个条件都要看：草稿刚切到「跟随」时后端返回的
  // connection_pool_role 还停在旧值，草稿刚切回「云端」时它又还指着来源角色。只看一边，
  // 就会出现「界面上写着跟随，却顶着自己池子的测试通过」这类对不上的状态。
  const ownsPool = !following && text(rolePayload.connection_pool_role, role) === role;

  // ---- 连接列表卡 ----
  if (cloudMode) {
    const ownerRole = text(rolePayload.connection_pool_role, role);
    const borrowed = ownerRole !== role;
    const connections = roleConnections(role);
    const addBtn = createButton({
      label: "新增连接", size: "mini", icon: "plus",
      disabled: borrowed,
      onClick: () => void reRenderAfter(async () => {
        const payload = await client.request<JsonObject>(
          `/api/models/roles/${encodeURIComponent(role)}/connections`,
          { method: "POST", body: JSON.stringify({ label: "", base_url: "" }) },
        );
        modelRoles[role] = payload;
        const added = Array.isArray(payload.connections) ? payload.connections : [];
        if (added.length) selectedConnection[role] = text(record(added[added.length - 1]).id);
        showToast({ message: "已新增一条连接，填好 Base URL、模型和密钥后点“保存配置”。" });
      }, { preserveDraft: false }), // 表单切到了新连接的空白字段，不该把旧连接的草稿糊上去
    });
    const listCard = createCard([tcHead("连接列表", [addBtn])]);
    if (!connections.length) {
      listCard.append(createEmptyState({ title: "还没有连接", description: "点右上角“新增连接”开始配置。", icon: "gear" }));
    } else {
      for (const [index, connection] of connections.entries()) {
        const tone = connection.availability_status === "available"
          ? "ok" : connection.availability_status === "unavailable" ? "warn" : "mute";
        const dot = document.createElement("span");
        dot.className = "cdot";
        dot.style.background = tone === "ok" ? "var(--ok)" : tone === "warn" ? "var(--danger)" : "var(--ink-3)";
        const name = document.createElement("span");
        name.style.flex = "1";
        name.style.fontWeight = "600";
        name.textContent = connection.display_label || `连接 ${index + 1}`;
        const chips: HTMLElement[] = [];
        if (index === 0) chips.push(createChip({ label: "主用", tone: "tint" }));
        if (!connection.has_api_key) chips.push(createChip({ label: "无密钥", tone: "warn" }));
        const actions = document.createElement("span");
        actions.style.display = "flex";
        actions.style.gap = "6px";
        if (!borrowed) {
          actions.append(createButton({
            label: "测试", size: "mini",
            onClick: (e) => { e.stopPropagation(); void testConnectionRow(role, connection.id); },
          }));
          if (index > 0) {
            actions.append(createButton({
              label: "设为主用", size: "mini",
              onClick: (e) => { e.stopPropagation(); void promoteConnection(role, connection.id); },
            }));
          }
          if (connections.length > 1) {
            actions.append(createButton({
              label: "删除", size: "mini", variant: "danger",
              onClick: (e) => { e.stopPropagation(); void deleteConnection(role, connection.id); },
            }));
          }
        }
        const row = connRow([dot, name, ...chips, actions], {
          selected: Boolean(selected && selected.id === connection.id),
          // 借来的池子不许选：跟随角色只有一份测试结论的存放位置，按行去测会把某一行
          // 的签名写进去，而面板回读的是这个角色实际会拨的那条，两边对不上，测完仍旧
          // 显示「未测试」。这里的行只是告诉用户「来源有哪些连接」。
          onClick: borrowed ? undefined : () => {
            selectedConnection[role] = connection.id;
            renderBody();
          },
        });
        listCard.append(row);
      }
    }
    host.append(listCard);
  }

  // ---- 连接详情卡 ----
  const detailCard = createCard([]);
  const detailBody = document.createElement("div");
  detailBody.style.padding = "14px 16px";
  detailBody.style.display = "flex";
  detailBody.style.flexDirection = "column";
  detailBody.style.gap = "10px";

  const grid = document.createElement("div");
  grid.className = "grid2";

  // 连接方式
  const radioWrap = document.createElement("div");
  radioWrap.className = "field";
  const radioLabel = document.createElement("label");
  radioLabel.textContent = "连接方式";
  radioLabel.append(hintBadge("云端 API 通过服务商接口调用；本地模型直接连接本机运行器；跟随只共享服务商、Base URL 和 Key，模型名称、吞吐和测试状态始终独立。"));
  radioWrap.append(radioLabel);
  const radioRow = document.createElement("div");
  radioRow.className = "radio-row";
  for (const option of accessModeOptions(role)) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "accessMode";
    input.checked = access === option.value;
    input.addEventListener("change", () => {
      modelAccessDraft[role] = option.value;
      renderBody();
    });
    label.append(input, document.createTextNode(option.label));
    radioRow.append(label);
  }
  radioWrap.append(radioRow);
  grid.append(radioWrap);

  // onChange 需要拿到下面才创建的 baseUrlField，用可变引用打个转 —— select 的 change
  // 事件只会在用户真的切换了选项时触发一次，天然满足「只在切换动作发生时重填，不覆盖
  // 用户已手填内容」的要求（初次渲染、其他按钮触发的重画都不会 fire change）。
  let onProviderChange: (value: string) => void = () => {};
  applyProviderDerivedState = () => {};
  const providerFieldHandle = selectField(
    cloudMode ? "服务商" : "本地运行器",
    providers.map((item) => ({ value: item, label: providerLabel(item) })),
    formProvider,
    (value) => onProviderChange(value),
    { disabled: following },
  );
  const providerSelectEl = providerFieldHandle.select;
  providerSelectEl.id = "settings-model-provider";
  // Whose form this is.  The draft restore compares it, so a re-render that
  // lands on a different connection drops the draft instead of transplanting
  // it — see MODEL_FORM_DRAFT_FIELD_IDS.
  providerSelectEl.dataset.formScope = `${role}|${cloudMode ? "cloud" : "local"}|${selected?.id ?? ""}`;
  grid.append(providerFieldHandle.root);

  detailBody.append(grid);

  const baseUrlDisabledByProvider = cloudMode && providerBaseUrlDisabled.has(formProvider);
  const baseUrlField = textField(
    "Base URL",
    baseUrlDisabledByProvider ? "" : formBaseUrl,
    () => undefined,
    {
      placeholder: baseUrlDisabledByProvider ? disabledBaseUrlPlaceholder : "https://.../v1",
      disabled: following || baseUrlDisabledByProvider,
    },
  );
  baseUrlField.input.id = "settings-model-base-url";
  detailBody.append(baseUrlField.root);
  // 只改 Base URL 的可填性和占位符，不动它的值——草稿回填时要按服务商重放这段联动，
  // 但绝不能把用户刚敲的 Base URL 冲掉。
  applyProviderDerivedState = (value) => {
    if (!cloudMode) return;
    if (providerBaseUrlDisabled.has(value)) {
      baseUrlField.input.disabled = true;
      baseUrlField.input.placeholder = disabledBaseUrlPlaceholder;
      return;
    }
    baseUrlField.input.disabled = following;
    baseUrlField.input.placeholder = "https://.../v1";
  };
  onProviderChange = (value) => {
    if (!cloudMode) return;
    applyProviderDerivedState(value);
    baseUrlField.input.value = providerBaseUrlDisabled.has(value)
      ? ""
      : providerBaseUrlDefaults[value] ?? "";
    // 模型名称跟着服务商一起重填：A 家的模型 ID 拿到 B 家去必定报错，留着它比清掉更坏。
    // 没有预设的服务商（OpenAI 兼容、各家自建网关）保持原样，不拿空字符串去洗掉用户填的值。
    const recommended = providerModelDefaults[value];
    if (recommended) modelNameField.input.value = recommended;
  };

  const modelNameField = textField("模型名称", formModel, () => undefined, { placeholder: cloudMode ? "输入模型 ID" : "例如 qwen2.5:7b", hint: "右侧箭头可以展开候选；列表里没有的模型也可以手动填写。" });
  modelNameField.input.id = "settings-model-name";
  const catalogConnection = modelCatalogConnectionForRole(role);
  const catalogMatches = modelCatalogConnection[role] === catalogConnection;
  const catalog = catalogMatches ? modelCatalog[role] || [] : [];
  // 原来挂的是原生 <datalist>：候选只在输入框获得焦点且内容匹配时才冒出来，长得像一
  // 条灰色系统条，跟这一页其余的下拉完全不是一套东西，用户也找不到「怎么把它调出来」。
  // 改成输入框右侧一个箭头按钮，点开走 openMenu，和「浏览」那类锚定菜单同一套外观。
  attachModelNameDropdown(modelNameField.input, catalog, () => providerSelectEl.value);
  detailBody.append(modelNameField.root);

  let connectionLabelField: HTMLInputElement | null = null;
  if (cloudMode) {
    const labelField = textField("连接名称", selected?.label ?? "", () => undefined, { placeholder: "例如 主账号 / 备用厂商", disabled: borrowedPool });
    connectionLabelField = labelField.input;
    connectionLabelField.id = "settings-model-connection-label";
    detailBody.append(labelField.root);
  }

  let apiKeyField: HTMLInputElement | null = null;
  if (cloudMode) {
    const preview = text(selected?.api_key_preview);
    const keyInput = document.createElement("input");
    keyInput.type = "password";
    keyInput.autocomplete = "off";
    // 已保存的密钥直接以掩码当占位符摆在框里，不再在下面挂一行注释：注释和输入框
    // 中间隔着一段空白，「已保存」和「这个框是空的」看上去像在说两件互相矛盾的事。
    keyInput.placeholder = borrowedPool
      ? "跟随时使用来源角色的密钥"
      : selected?.has_api_key ? `${preview || "••••••"}（已保存 · 留空则不改）` : "粘贴该连接的 API 密钥";
    keyInput.disabled = borrowedPool;
    keyInput.id = "settings-model-api-key";
    apiKeyField = keyInput;
    const keyField = fieldWithHint("API 密钥", keyInput, "留空表示沿用已保存的密钥；密钥只写入本机密钥存储，不随配置导出（除非选择“导出含 Key”）。");
    detailBody.append(keyField);
  }

  // 吞吐设置
  const throughput = record(modelThroughput[role] || rolePayload.throughput);
  const bounds = record(rolePayload.throughput_bounds);
  const batchBounds = Array.isArray(bounds.batch_size) ? (bounds.batch_size as unknown[]) : [];
  const concurrencyBounds = Array.isArray(bounds.concurrency) ? (bounds.concurrency as unknown[]) : [];
  const throughputGrid = document.createElement("div");
  throughputGrid.className = "grid2";
  let batchInput: HTMLInputElement | null = null;
  const needsBatch = role === "translation" || role === "cleaner";
  if (needsBatch) {
    const batchField = numberField("批次大小", num(throughput.batch_size, 8), () => undefined, {
      min: num(batchBounds[0], 1), max: num(batchBounds[1], 128),
    });
    batchInput = batchField.querySelector("input");
    throughputGrid.append(batchField);
  }
  const concurrencyField = numberField("并发数", num(throughput.concurrency, 1), () => undefined, {
    min: num(concurrencyBounds[0], 1), max: num(concurrencyBounds[1], 32),
  });
  const concurrencyInput = concurrencyField.querySelector("input") as HTMLInputElement;
  throughputGrid.append(concurrencyField);
  detailBody.append(sectionLabel("速率设置"));
  detailBody.append(throughputGrid);
  detailBody.append(fieldRow([
    createButton({ label: "保存速率", size: "mini", onClick: () => void reRenderAfter(async () => {
      const payload: JsonObject = { concurrency: Number(concurrencyInput.value || "1") };
      if (needsBatch && batchInput) payload.batch_size = Number(batchInput.value || "8");
      modelThroughput[role] = await client.request<JsonObject>(`/api/models/throughput/${encodeURIComponent(role)}`, {
        method: "PUT", body: JSON.stringify(payload),
      });
      showToast({ message: "速率设置已保存。运行中的任务不受影响。" });
    }) }),
    createButton({ label: "恢复推荐值", size: "mini", onClick: () => void reRenderAfter(async () => {
      modelThroughput[role] = await client.request<JsonObject>(`/api/models/throughput/${encodeURIComponent(role)}`, { method: "DELETE" });
      showToast({ message: "已恢复推荐吞吐值。运行中的任务不受影响。" });
    }) }),
  ]));

  // 状态胶囊。取的是**当前选中那条连接**的测试结果，不是角色级的那份——角色级状态
  // 是主用连接的镜像（core/model_roles.py 里照抄 primary 的三个字段），拿它当详情卡的
  // 状态，新建的第二条连接一挂上来就顶着主用连接的「测试通过」，用户会以为它已经验过。
  // 两种情况必须回到角色自己那份状态，不能读连接行：跟随时列表里摆的是**来源角色**的
  // 池子（后端 list_effective_role_connections），照抄就会让一次都没测过的跟随角色顶着
  // 翻译模型的「测试通过」；本地模式下压根没有连接列表，activeConnection 兜底给出的是
  // 云端主连接，读它等于把云端的结论安到本地运行器头上。
  const connectionOwnsVerdict = cloudMode && ownsPool && Boolean(selected);
  const availabilitySource: JsonObject = connectionOwnsVerdict
    ? { availability_status: selected!.availability_status, availability_message: selected!.availability_message, availability_checked_at: selected!.availability_checked_at }
    : rolePayload;
  const availability = text(availabilitySource.availability_status, "unknown");
  const availabilityTone = availability === "available" ? "done" : availability === "unavailable" ? "error" : "";
  const availabilityLabel = availability === "available" ? "测试通过" : availability === "unavailable" ? "测试失败" : "未测试";
  const checkedAt = formatCheckedAt(text(availabilitySource.availability_checked_at));
  const statusRow = document.createElement("div");
  statusRow.style.display = "flex";
  statusRow.style.gap = "8px";
  statusRow.style.alignItems = "center";
  statusRow.style.flexWrap = "wrap";
  const catalogChip = statusChip(catalog.length ? `${catalog.length} 个可用模型` : "未获取列表", catalog.length ? "done" : "");
  catalogChip.title = catalogMatches ? (modelCatalogMessage[role] || "尚未获取当前连接的模型列表。") : "当前连接尚未获取模型列表。保存配置后可手动获取。";
  const availChip = statusChip(availabilityLabel, availabilityTone);
  availChip.title = `${text(availabilitySource.availability_message, "当前配置尚未测试。")}${checkedAt ? ` · ${checkedAt}` : ""}`;
  statusRow.append(catalogChip, availChip);
  detailBody.append(statusRow);

  // 保存 / 获取模型 / 测试连接 / 导出导入
  // 提交是串行排队的，真正发出去时可能已经隔了一次重画：用户切了角色卡、切了
  // 云端/本地、换了选中的连接。所以「这份表单属于谁、该走哪条路由」必须在渲染这一刻
  // 就定死，随表单一起排队；到执行时再去读全局状态，会把这一份值写到另一个角色头上，
  // 或者带着云端服务商发出一个 mode=local。
  const readModelForm = (): ModelFormSubmission => ({
    role,
    access,
    sourceRole,
    secondaryId: editableSecondaryConnection(role)?.id ?? "",
    selectedId: selected?.id ?? "",
    selectedLabel: selected?.label ?? "",
    provider: providerSelectEl.value,
    baseUrl: baseUrlField.input.value,
    model: modelNameField.input.value,
    apiKey: apiKeyField?.value ?? "",
    connectionLabel: connectionLabelField?.value ?? "",
  });
  submitModelForm = () => queueModelFormSave(() => saveModel(readModelForm(), { silent: true }));

  const autoSaveNote = document.createElement("span");
  autoSaveNote.style.fontSize = "12px";
  autoSaveNote.style.color = "var(--ink-3)";
  statusRow.append(autoSaveNote);

  // 失焦即保存。用户的原话：「任何离开焦点的行为都应该自动保存」。change 事件只在值
  // 真的变了之后失焦才触发，所以不会出现「点进去又点出来也发一次请求」。这里刻意不
  // 重画：重画会把焦点从用户正要去的下一个字段上抢走。
  const autoSaveOnBlur = (event: Event) => {
    const field = event.currentTarget as HTMLInputElement | HTMLSelectElement;
    if (field.disabled) return;
    void queueModelFormSave(async () => {
      try {
        await saveModel(readModelForm(), { silent: true });
        autoSaveNote.textContent = `已自动保存 · ${new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`;
      } catch (error) {
        autoSaveNote.textContent = "";
        showToast({ message: `自动保存失败：${errorMessage(error)}`, error: true });
      }
    });
  };
  for (const field of [providerSelectEl, baseUrlField.input, modelNameField.input, connectionLabelField, apiKeyField]) {
    field?.addEventListener("change", autoSaveOnBlur);
  }

  const doSaveModel = () => void reRenderAfter(
    () => queueModelFormSave(() => saveModel(readModelForm())),
    { preserveDraft: false }, // 表单确实提交了，重画后应显示服务端回填的最新值
  );
  detailBody.append(fieldRow([
    createButton({ label: "保存配置", icon: "check", onClick: doSaveModel }),
    createButton({ label: "获取模型列表", onClick: () => void reRenderAfter(async () => {
      await ensureFormSavedBeforeCatalog();
      const result = await client.request<{ ok: boolean; models: string[]; message: string }>(
        `/api/models/catalog/${encodeURIComponent(role)}`,
        { method: "POST", body: JSON.stringify({ refresh: true, connection_id: selected?.id ?? "" }) },
      );
      modelCatalog[role] = result.models;
      modelCatalogMessage[role] = result.message;
      modelCatalogConnection[role] = modelCatalogConnectionForRole(role);
      showToast({ message: result.models.length ? `已获取 ${result.models.length} 个模型，可从模型名称右侧的箭头里选择。` : result.message, error: !result.ok });
      // 表单已经被 ensureFormSavedBeforeCatalog 提交了，重画该显示服务端回填的值，
      // 再把草稿盖回去只会让「已保存的密钥」以明文停在框里。
    }, { preserveDraft: false }) }),
    createButton({ label: "测试连接", onClick: () => void reRenderAfter(async () => {
      await ensureFormSavedBeforeCatalog();
      // 测的必须是面板正在显示的这条连接：不带 id 时后端一律拨主连接，
      // 用户看着新加的连接，拿到的却是主连接的结论。跟随角色例外——它只有一份结论
      // 的存放位置，测哪条就得回读哪条，否则测完还是「未测试」；它实际拨的就是来源
      // 的主连接，所以不带 id 正是「测它真正在用的那条」。
      const result = await client.request<{ ok: boolean; message: string }>(
        `/api/models/connectivity/${encodeURIComponent(role)}`,
        { method: "POST", body: JSON.stringify({ connection_id: ownsPool ? (selected?.id ?? "") : "" }) },
      );
      showToast({ message: result.message, error: !result.ok });
      await refreshModelRoles();
    }, { preserveDraft: false }) }),
  ]));

  detailCard.append(detailBody);
  host.append(detailCard);

  // 并发提醒
  const spreadCard = createCard([]);
  const spreadBody = document.createElement("div");
  spreadBody.style.padding = "14px 16px";
  spreadBody.append(sectionLabel("并发提醒"));
  spreadBody.append(createSwitchRow({
    label: "并行任务分散到不同连接",
    hint: "关闭时所有任务都用主用连接，多余的连接只作故障切换备用。打开后同时运行的任务会各自占用一条空闲连接。",
    checked: Boolean(settings?.spread_tasks_across_connections),
    onChange: (checked) => void reRenderAfter(() => persistSettings({ spread_tasks_across_connections: checked })),
  }));
  const spreadNote = document.createElement("p");
  spreadNote.className = "note";
  spreadNote.style.fontSize = "12px";
  spreadNote.style.color = "var(--ink-3)";
  spreadNote.textContent = "同一 API 连接的并发会叠加，可能触发限流或费用增长。";
  spreadBody.append(spreadNote);
  spreadCard.append(spreadBody);
  host.append(spreadCard);

  // 整包导出导入。以前这三个按钮挂在每个模型的详情卡里，看起来像是「这个模型的配置」，
  // 实际上导的一直是四个角色的全部配置。挪到页面底部单独成卡，名字也照实写。
  host.append(bundleCard({
    title: "整个模型服务打包",
    description: "一次导出翻译、清洗、PDF 翻译、PDF 审阅四个模型的全部连接、模型名和速率设置，对方一键导入即可复现整套模型服务。密钥默认不导出，导入后所有连接都要重新测试。",
    buttons: [
      createButton({ label: "导出（不含 Key）", size: "mini", onClick: () => void exportModelConfig(false) }),
      createButton({ label: "导出含 Key", size: "mini", onClick: () => void exportModelConfig(true) }),
      createButton({ label: "导入配置", size: "mini", onClick: () => importModelConfig() }),
    ],
  }));
}

function bundleCard(opts: { title: string; description: string; buttons: HTMLElement[] }): HTMLElement {
  const card = createCard([tcHead(opts.title, opts.buttons)]);
  const note = document.createElement("p");
  note.className = "note";
  note.style.margin = "0";
  note.style.padding = "0 16px 14px";
  note.style.fontSize = "12px";
  note.style.color = "var(--ink-3)";
  note.textContent = opts.description;
  card.append(note);
  return card;
}

async function ensureFormSavedBeforeCatalog(): Promise<void> {
  // 「获取模型列表」和「测试连接」两个后端路由都只认**已保存**的配置（api/app.py 里
  // 明写着不接收表单草稿）。所以这里必须先把表单交上去，再去调它们——这个函数以前只
  // 做了 refreshModelRoles()，等于把服务端的旧值又拉了一遍，用户刚换的密钥根本没上去，
  // 「测试连接」测的是旧密钥，永远失败。
  if (submitModelForm) await submitModelForm();
  await refreshModelRoles();
}

async function saveModel(
  form: ModelFormSubmission,
  opts: { silent?: boolean } = {},
): Promise<void> {
  const role = form.role;
  // 只有「自己池子里的非主用连接」才走连接路由。以前这里只看 !primary：切到本地模型
  // 后失焦自动保存，会把本机运行器的地址 PUT 到那条云端连接上（连名字一起清掉），
  // 而「改成本地」这件事本身一次都没存进去。
  if (form.secondaryId) {
    modelRoles[role] = await client.request<JsonObject>(
      `/api/models/roles/${encodeURIComponent(role)}/connections/${encodeURIComponent(form.secondaryId)}`,
      { method: "PUT", body: JSON.stringify({ label: form.connectionLabel, provider: form.provider, model: form.model, base_url: form.baseUrl, api_key: form.apiKey }) },
    );
    clearModelCatalog(role, "连接已变更，请重新获取模型列表。");
    if (!opts.silent) showToast({ message: "连接已保存。密钥仅写入本机密钥存储。" });
    return;
  }
  const access = form.access;
  const sourceRole = form.sourceRole;
  const following = sourceRole !== "independent";
  const mode = following ? text(record(modelRoles[role]).mode, "cloud") : access;
  const payload = following
    ? { source_role: sourceRole, model: form.model }
    : { source_role: "independent", mode, provider: form.provider, base_url: form.baseUrl, model: form.model };
  await client.request(`/api/models/roles/${role}`, { method: "PUT", body: JSON.stringify(payload) });
  delete modelAccessDraft[role];
  if (form.apiKey && mode === "cloud") {
    await client.request(`/api/keys/${form.provider}`, { method: "PUT", body: JSON.stringify({ api_key: form.apiKey, base_url: form.baseUrl }) });
  }
  // 连接名称输入框只在云端且不跟随时才渲染；其余情况 form.connectionLabel 恒为空串，
  // 拿它去比对就会把主用连接的名字清掉。
  if (!following && mode === "cloud" && form.selectedId && form.connectionLabel !== form.selectedLabel) {
    await client.request(`/api/models/roles/${encodeURIComponent(role)}/connections/${encodeURIComponent(form.selectedId)}`, {
      method: "PUT", body: JSON.stringify({ label: form.connectionLabel }),
    });
  }
  clearModelCatalog(role, "连接已变更，请重新获取模型列表。");
  await refreshSettings();
  if (!opts.silent) showToast({ message: "模型配置已保存。密钥仅写入本机密钥存储。" });
}

async function testConnectionRow(role: string, connectionId: string): Promise<void> {
  selectedConnection[role] = connectionId;
  // 走 reRenderAfter 而不是裸 renderBody()：测试这条连接不该把用户刚敲的字冲掉。
  // 若这一下同时切换了连接，草稿的 scope 对不上，会自动丢弃而不是搬到新连接上。
  await reRenderAfter(async () => {
    const result = await client.request<{ ok: boolean; message: string }>(
      `/api/models/connectivity/${encodeURIComponent(role)}`,
      { method: "POST", body: JSON.stringify({ connection_id: connectionId }) },
    );
    showToast({ message: result.message, error: !result.ok });
    await refreshModelRoles();
  });
}

async function promoteConnection(role: string, connectionId: string): Promise<void> {
  await reRenderAfter(async () => {
    const current = roleConnections(role).map((item) => item.id);
    const ordered = [connectionId, ...current.filter((item) => item !== connectionId)];
    modelRoles[role] = await client.request<JsonObject>(
      `/api/models/roles/${encodeURIComponent(role)}/connections/reorder`,
      { method: "POST", body: JSON.stringify({ ordered_ids: ordered }) },
    );
    await refreshSettings();
    showToast({ message: "已设为主用连接。" });
  }, { preserveDraft: false }); // 主用连接换人，表单跟着切到新连接，旧连接的草稿（含刚粘的密钥）绝不能糊过去
}

async function deleteConnection(role: string, connectionId: string): Promise<void> {
  await reRenderAfter(async () => {
    modelRoles[role] = await client.request<JsonObject>(
      `/api/models/roles/${encodeURIComponent(role)}/connections/${encodeURIComponent(connectionId)}`,
      { method: "DELETE" },
    );
    delete selectedConnection[role];
    clearModelCatalog(role, "连接已变更，请重新获取模型列表。");
    showToast({ message: "连接已删除，其密钥也已从本机移除。" });
  }, { preserveDraft: false }); // 表单会切回默认连接，不该把被删连接的草稿糊上去
}

async function exportModelConfig(includeApiKey: boolean): Promise<void> {
  if (includeApiKey && !window.confirm("导出的文件将包含 API Key。请确认只保存到受保护的位置。")) return;
  const query = includeApiKey ? "?include_api_key=true&confirm_sensitive=true" : "";
  const payload = await client.request<JsonObject>(`/api/model-config/export${query}`);
  downloadJson("translator-model-config.json", payload);
  showToast({ message: includeApiKey ? "已导出模型配置和明确勾选的 API 密钥，请立即移入受保护的位置。" : "已导出模型配置；默认不包含 API Key。" });
}

function importModelConfig(): void {
  pickJsonFile(async (fileName, payload) => {
    const preview = await client.request<Omit<ModelImportPreview, "fileName" | "payload">>("/api/model-config/import/preview", { method: "POST", body: JSON.stringify(payload) });
    modelImportPreview = { fileName, payload, ...preview };
    openImportPreviewModal();
  }, "模型配置导入预览失败");
}

function openImportPreviewModal(): void {
  const preview = modelImportPreview;
  if (!preview) return;
  const list = document.createElement("ul");
  list.style.margin = "0 0 8px";
  list.style.paddingLeft = "18px";
  list.style.fontSize = "12.5px";
  if (preview.roles.length) {
    for (const item of preview.roles) {
      const li = document.createElement("li");
      li.textContent = `${item.role}：${item.fields.join("、") || "无显式字段"}`;
      list.append(li);
    }
  } else {
    const li = document.createElement("li");
    li.textContent = "没有角色配置变更。";
    list.append(li);
  }
  let handle: ModalHandle;
  handle = openModal({
    tone: "warn",
    icon: "gear",
    sourceLabel: "设置 · 模型服务 · 导入配置",
    title: "预览导入模型配置",
    body: [
      // 别再写「不删除未提及配置」：连接池是整份替换的，本机在这些角色下自己加的
      // 连接会连同它们保存的 Key 一起消失，说成「只合并」等于骗人。
      `${preview.fileName} · 文件提到的角色，其连接列表会整份替换本机现有连接（本机自己加的连接及其已保存 Key 将被清除）；文件没提到的配置保持不变。`,
      list,
      `吞吐档案：${preview.throughput_profile_count} 项；文件中包含的密钥作用域：${preview.api_key_count} 个。导入后受影响角色全部变为“未测试”，不会自动请求服务。`,
    ],
    actions: [
      { label: "取消", variant: "default" },
      {
        label: "确认合并", variant: "primary", keepOpen: true,
        onClick: async () => {
          try {
            const result = await client.request<{ imported_key_count: number }>("/api/model-config/import", {
              method: "POST", body: JSON.stringify(preview.payload),
            });
            modelImportPreview = null;
            await refreshSettings();
            for (const role of Object.keys(modelRoles)) clearModelCatalog(role, "导入后请重新获取当前连接的模型列表。");
            renderBody();
            handle.close();
            showToast({ message: `已合并配置与 ${result.imported_key_count} 个密钥作用域；所有受影响角色均需重新测试。` });
          } catch (error) {
            showToast({ message: errorMessage(error), error: true });
          }
        },
      },
    ],
  });
}

async function exportDocumentConfig(): Promise<void> {
  const payload = await client.request<JsonObject>("/api/document-config/export");
  downloadJson("translator-document-config.json", payload);
  showToast({ message: "已导出文档翻译配置；不含模型、密钥和本机输出目录。" });
}

function importDocumentConfig(): void {
  pickJsonFile(async (fileName, payload) => {
    const preview = await client.request<{ areas: string[]; app_version: string }>(
      "/api/document-config/import/preview",
      { method: "POST", body: JSON.stringify(payload) },
    );
    openDocumentImportPreviewModal(fileName, payload, preview.areas);
  }, "文档翻译配置导入预览失败");
}

function openDocumentImportPreviewModal(fileName: string, payload: JsonObject, areas: string[]): void {
  const list = document.createElement("ul");
  list.style.margin = "0 0 8px";
  list.style.paddingLeft = "18px";
  list.style.fontSize = "12.5px";
  for (const area of areas.length ? areas : ["没有可识别的配置变更。"]) {
    const li = document.createElement("li");
    li.textContent = area;
    list.append(li);
  }
  let handle: ModalHandle;
  handle = openModal({
    tone: "warn",
    icon: "gear",
    sourceLabel: "设置 · 翻译参数 · 导入配置",
    title: "预览导入文档翻译配置",
    body: [
      `${fileName} · 只覆盖文件里写明的项，没提到的设置保持不变。`,
      list,
      "模型、密钥和本机输出目录不在这个包里，导入后不会被改动。",
    ],
    actions: [
      { label: "取消", variant: "default" },
      {
        label: "确认导入", variant: "primary", keepOpen: true,
        onClick: async () => {
          try {
            await client.request("/api/document-config/import", { method: "POST", body: JSON.stringify(payload) });
            await refreshSettings();
            renderBody();
            handle.close();
            showToast({ message: "文档翻译配置已导入。" });
          } catch (error) {
            showToast({ message: errorMessage(error), error: true });
          }
        },
      },
    ],
  });
}

function pickJsonFile(onPicked: (fileName: string, payload: JsonObject) => Promise<void>, failureLabel: string): void {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "application/json,.json";
  input.onchange = async () => {
    const file = input.files?.[0];
    if (!file) return;
    try {
      await onPicked(file.name, JSON.parse(await file.text()) as JsonObject);
    } catch (error) {
      showToast({ message: `${failureLabel}：${errorMessage(error)}`, error: true });
    }
  };
  input.click();
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
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

async function fetchWithToken(path: string): Promise<Response> {
  const { invoke } = await import("@tauri-apps/api/core");
  const info = await invoke<{ port: number; token: string }>("sidecar_info");
  return fetch(`http://127.0.0.1:${info.port}${path}`, { headers: { "X-Translator-Token": info.token } });
}

async function downloadBinary(path: string, fallbackFilename: string): Promise<void> {
  const response = await fetchWithToken(path);
  if (!response.ok) throw new Error("导出失败。");
  const disposition = response.headers.get("Content-Disposition") || "";
  const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || fallbackFilename;
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
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
  preset: string; customPrompt: string; promptOverrides: Record<string, string>; nameOverrides: Record<string, string>;
} {
  const prefix = surface;
  return {
    preset: text(settings?.[`${prefix}_domain_preset`], "同步工程场景"),
    customPrompt: text(settings?.[`${prefix}_custom_prompt`]),
    promptOverrides: Object.fromEntries(
      Object.entries(record(settings?.[`${prefix}_domain_prompt_overrides`])).filter((e): e is [string, string] => typeof e[1] === "string"),
    ),
    nameOverrides: Object.fromEntries(
      Object.entries(record(settings?.[`${prefix}_domain_name_overrides`])).filter((e): e is [string, string] => typeof e[1] === "string"),
    ),
  };
}

function targetLangForDomain(surface: TranslationSurface): string {
  const key = surface === "excel" ? "excel_target_lang" : "word_target_lang";
  return text(settings?.[key], text(settings?.target_lang, "en"));
}

function renderParamsPage(host: HTMLElement): void {
  const card = createCard([]);
  const tabs = document.createElement("div");
  tabs.className = "tabs";
  const tabDefs: { id: ParamsSurface; label: string }[] = [
    { id: "excel", label: "Excel" }, { id: "word", label: "Word" }, { id: "pdf", label: "PDF" },
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
    grid.append(numberField("每批最大段落数", num(batch.max_paragraphs_per_batch, 30), (v) => void reRenderAfter(() => saveSettingPath("word_batch.max_paragraphs_per_batch", v)), { min: 1, hint: "单次模型请求最多包含的段落数量。" }));
    grid.append(numberField("每批字符上限", num(batch.max_chars_per_batch, 3000), (v) => void reRenderAfter(() => saveSettingPath("word_batch.max_chars_per_batch", v)), { min: 1, hint: "单次模型请求的字符上限，超出会自动分批。" }));
    grid.append(numberField("长段拆分阈值", num(batch.split_paragraph_chars, 3000), (v) => void reRenderAfter(() => saveSettingPath("word_batch.split_paragraph_chars", v)), { min: 1, hint: "超过该长度的段落只在模型请求层拆分，响应后按原顺序回写，不会新增段落或破坏编号、数字和单位。" }));
    grid.append(numberField("单段严格重试次数", num(batch.strict_retry_attempts, 3), (v) => void reRenderAfter(() => saveSettingPath("word_batch.strict_retry_attempts", v)), { min: 1, max: 8, hint: "仅对空译文、明显不完整或质量校验失败的段落重试。" }));
    body.append(grid);
  } else {
    const pdf = pdfParamsSettings();
    const grid = document.createElement("div");
    grid.className = "grid2";
    grid.append(numberField("单页重试次数", num(pdf.page_retry_attempts, 2), (v) => void reRenderAfter(() => saveSettingPath("pdf.page_retry_attempts", v)), { min: 0, max: 10, hint: "单页翻译失败后的重试次数。" }));
    const concurrencyValue = pdf.page_generation_concurrency === null || pdf.page_generation_concurrency === undefined
      ? "" : String(pdf.page_generation_concurrency);
    const concurrencyInput = document.createElement("input");
    concurrencyInput.type = "number";
    concurrencyInput.min = "1";
    concurrencyInput.value = concurrencyValue;
    concurrencyInput.placeholder = "留空自动";
    concurrencyInput.addEventListener("change", () => {
      const raw = concurrencyInput.value.trim();
      void reRenderAfter(() => saveSettingPath("pdf.page_generation_concurrency", raw ? Number(raw) : null));
    });
    grid.append(fieldWithHint("页图并发（留空自动）", concurrencyInput, "同时生成页图的并发数；留空由应用按机器性能决定。"));
    body.append(grid);
  }

  card.append(body);
  host.append(card);

  if (paramsTab === "excel" || paramsTab === "word") {
    host.append(renderDomainPromptCard(paramsTab));
  }

  // 一个大包覆盖三个页面：分页签导出会让「换台电脑继续用」变成来回导三次。
  host.append(bundleCard({
    title: "文档翻译打包",
    description: "一次导出 Excel、Word、PDF 三个页面的全部翻译参数、语言设置和领域提示词。不含模型和密钥，也不含本机的输出目录。",
    buttons: [
      createButton({ label: "导出配置", size: "mini", onClick: () => void exportDocumentConfig() }),
      createButton({ label: "导入配置", size: "mini", onClick: () => importDocumentConfig() }),
    ],
  }));
}

function renderReviewColorGroup(colors: JsonObject, onSave: (mark: string, color: string) => void): HTMLDivElement {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const label = document.createElement("label");
  label.textContent = "复核三色";
  wrap.append(label);
  const row = document.createElement("div");
  row.style.display = "flex";
  row.style.gap = "18px";
  row.style.alignItems = "center";
  row.style.flexWrap = "wrap";
  const marks: { key: string; label: string; fallback: string }[] = [
    { key: "semantic", label: "语义校验接受", fallback: "FFF2CC" },
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
  const builtInPrompt = domainBuiltInPrompt(current.preset, targetLang);
  const isCustom = current.preset === "自定义";
  const isNone = current.preset === "无";
  const hasOverride = !isCustom && !isNone && Object.prototype.hasOwnProperty.call(current.promptOverrides, current.preset);
  const prompt = isCustom ? current.customPrompt : hasOverride ? current.promptOverrides[current.preset] : builtInPrompt;

  const card = createCard([]);
  const tools: HTMLElement[] = [];
  const body = document.createElement("div");
  body.style.padding = "14px 16px";
  body.style.display = "flex";
  body.style.flexDirection = "column";
  body.style.gap = "10px";

  const surfaceLabel = surface === "excel" ? "Excel" : "Word";
  body.append(sectionLabel(`专业领域 Prompt（${surfaceLabel} 独立）`));

  const presetSelect = document.createElement("select");
  for (const option of DOMAIN_PRESET_OPTIONS) {
    const opt = document.createElement("option");
    opt.value = option;
    opt.textContent = option;
    if (option === current.preset) opt.selected = true;
    presetSelect.append(opt);
  }
  body.append(fieldWithHint("领域预设", presetSelect, "领域 Prompt 只决定用词风格。固定输出 JSON、格式与占位符保护、目标语言和逐条原文语言回报由应用追加，不能被覆盖。"));

  const promptArea = document.createElement("textarea");
  promptArea.className = "domain-prompt";
  promptArea.value = prompt;
  promptArea.rows = 6;
  promptArea.placeholder = isCustom ? "请输入完整领域 Prompt" : "内置 Prompt 会在此显示";
  const promptField = fieldWithHint(isCustom ? "自定义领域 Prompt" : hasOverride ? "当前领域覆盖 Prompt" : "内置领域 Prompt（可查看、可编辑为覆盖）", promptArea);
  body.append(promptField);

  // 「无」没有可编辑的 Prompt——它的全部含义就是一个字都不追加。把编辑框留在那里，
  // 用户改两句话再点保存，只会存下一段谁都不会用到的文本。
  const applyNoneState = (preset: string) => {
    const none = preset === "无";
    promptField.style.display = none ? "none" : "";
    return none;
  };
  applyNoneState(current.preset);

  presetSelect.addEventListener("change", () => {
    // 切换预设时刷新文本框内容（不立即保存，需点“保存”）。
    const nextPreset = presetSelect.value;
    const nextIsNone = applyNoneState(nextPreset);
    const nextIsCustom = nextPreset === "自定义";
    const nextOverride = !nextIsCustom && !nextIsNone && Object.prototype.hasOwnProperty.call(current.promptOverrides, nextPreset);
    promptArea.value = nextIsCustom
      ? current.customPrompt
      : nextOverride ? current.promptOverrides[nextPreset] : domainBuiltInPrompt(nextPreset, targetLang);
    saveBtn.textContent = nextIsNone ? "保存领域选择" : nextIsCustom ? "保存自定义 Prompt" : "保存覆盖";
  });

  const doSave = () => void reRenderAfter(async () => {
    const preset = presetSelect.value;
    const promptOverrides = { ...current.promptOverrides };
    let customPrompt = current.customPrompt;
    if (preset === "无") {
      // 只改「当前选哪个领域」，其他领域已存的覆盖原样带回去。
    } else if (preset === "自定义") {
      if (!promptArea.value.trim()) throw new Error("自定义领域必须填写完整 Prompt，不能保存空配置。");
      customPrompt = promptArea.value;
    } else {
      const defaultPrompt = domainBuiltInPrompt(preset, targetLang);
      if (promptArea.value === defaultPrompt) delete promptOverrides[preset];
      else promptOverrides[preset] = promptArea.value;
    }
    await client.request(`/api/domains/${surface}`, {
      method: "PUT",
      body: JSON.stringify({ preset, custom_prompt: customPrompt, prompt_overrides: promptOverrides, name_overrides: current.nameOverrides }),
    });
    await refreshSettings();
    showToast({
      message: preset === "无"
        ? "已改为不使用领域提示词。"
        : preset === "自定义" ? "自定义领域 Prompt 已保存。" : "当前页面的领域 Prompt 覆盖已保存。",
    });
  });
  const saveBtn = createButton({
    label: isNone ? "保存领域选择" : isCustom ? "保存自定义 Prompt" : "保存覆盖",
    variant: "primary", size: "mini", onClick: doSave,
  });
  const actions = [saveBtn];
  if (!isCustom && !isNone) {
    actions.push(createButton({
      label: "恢复内置默认", size: "mini",
      onClick: () => void reRenderAfter(async () => {
        const preset = presetSelect.value;
        if (preset === "自定义") return;
        const promptOverrides = { ...current.promptOverrides };
        delete promptOverrides[preset];
        await client.request(`/api/domains/${surface}`, {
          method: "PUT",
          body: JSON.stringify({ preset, custom_prompt: current.customPrompt, prompt_overrides: promptOverrides, name_overrides: current.nameOverrides }),
        });
        await refreshSettings();
        showToast({ message: "已恢复该页面与领域的内置 Prompt。" });
      }),
    }));
  }
  body.append(fieldRow(actions));

  card.append(tcHead("专业领域 Prompt", tools), body);
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

function applyThemePreference(preference: ThemePreference): void {
  document.documentElement.dataset.theme = resolveTheme(preference);
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
    { value: "system", label: "跟随系统", hint: "跟随 macOS 外观设置自动切换" },
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
      applyThemePreference(option.value);
      await persistSettings({ appearance: { ...record(settings?.appearance), theme: option.value } });
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
    langCard.append(createEmptyState({ title: "还没有自定义目标语言", description: "自定义语言只能作为目标语言使用，创建后内部代码不可更改。", icon: "book" }));
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
    body: ["自定义语言只能作为目标语言使用；内部代码创建后不可变。", body],
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
  span.textContent = "当前应用数据";
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
    createButton({ label: "在 Finder 中显示", size: "mini", onClick: () => void openAppDataDirectory() }),
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
    strong.textContent = `有 ${activeTaskCount} 个活动任务`;
    const p = document.createElement("p");
    p.style.margin = "2px 0 0";
    p.style.fontSize = "12.5px";
    p.style.color = "var(--ink-2)";
    p.textContent = "会影响任务记录或认证状态的清理、删除全部 Key 和完整重置当前不可用。任务完成后再执行这些操作。";
    text_.append(strong, p);
    warnBody.append(text_);
    warnCard.append(warnBody);
    host.append(warnCard);
  }

  // 分类总览
  const overviewCard = createCard([tcHead("本地数据分类")]);
  const categories = maintenanceCategories();
  if (!categories.length) {
    overviewCard.append(createEmptyState({ title: "尚未读取本地数据概览", description: "点击上方“刷新”查看当前应用数据目录和各类别占用。", icon: "folder" }));
  } else {
    for (const category of categories) {
      const name = document.createElement("span");
      name.style.flex = "1";
      name.style.fontWeight = "600";
      name.textContent = category.label;
      const meta = document.createElement("span");
      meta.style.color = "var(--ink-3)";
      meta.style.fontSize = "12px";
      meta.textContent = `${formatBytes(category.size_bytes)}${category.count !== undefined ? ` · ${category.count} 项` : ""}`;
      const action = category.contains_user_output
        ? createChip({ label: "受保护", tone: "mute" })
        : category.clearable
          ? createButton({ label: "清理", size: "mini", variant: "danger", onClick: () => requestMaintenanceClear(category.id as MaintenanceClearCategory) })
          : createChip({ label: "自动维护", tone: "mute" });
      overviewCard.append(connRow([name, meta, action]));
    }
  }
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
  limitsBody.append(sectionLabel("自动留存边界"));
  const limitRows: [string, string][] = [
    ["任务摘要", `最近 ${historyLimit} 条`],
    ["应用日志", `${num(logsLimit.max_files, 5)} 个 × ${formatBytes(logsLimit.max_file_bytes || 5 * 1024 * 1024)}`],
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
    createButton({ label: "导出全部诊断", size: "mini", onClick: () => void downloadBinary("/api/diagnostics/history.zip", "translator-diagnostics.zip").catch((e) => showToast({ message: errorMessage(e), error: true })) }),
    createButton({ label: "清空诊断", size: "mini", variant: "danger", disabled: activeTaskCount > 0, onClick: () => requestMaintenanceClear("diagnostics") }),
  ])]);
  const diagDesc = document.createElement("p");
  diagDesc.className = "note";
  diagDesc.style.padding = "0 16px";
  diagDesc.style.fontSize = "12px";
  diagDesc.style.color = "var(--ink-3)";
  diagDesc.textContent = "诊断仅含版本、系统、任务阶段、脱敏连接摘要、计数和错误码，不会自动上传。";
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
        createButton({ label: "导出", size: "mini", onClick: () => void downloadBinary(`/api/diagnostics/${encodeURIComponent(id)}.zip`, "translator-diagnostic.zip").catch((e) => showToast({ message: errorMessage(e), error: true })) }),
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

  // 单类重置
  const resetCard = createCard([tcHead("单类重置")]);
  const resetDesc = document.createElement("p");
  resetDesc.className = "note";
  resetDesc.style.padding = "0 16px";
  resetDesc.style.fontSize = "12px";
  resetDesc.style.color = "var(--ink-3)";
  resetDesc.textContent = "每一项只作用于当前应用数据基线，输出文件始终受保护。";
  resetCard.append(resetDesc);
  const keyCategory = maintenanceCategory("keys");
  const resetItems: { label: string; meta: string; category: MaintenanceClearCategory; blockOnActive: boolean }[] = [
    { label: "任务摘要", meta: taskHistory ? `${formatBytes(taskHistory.size_bytes)} · ${num(taskHistory.count)} 项` : "", category: "task_history", blockOnActive: true },
    { label: "结构化日志", meta: logs ? formatBytes(logs.size_bytes) : "", category: "logs", blockOnActive: true },
    { label: "保存的 API Key", meta: keyCategory ? `${num(keyCategory.count)} 个作用域` : "仅显示作用域，不显示 Key 值", category: "keys", blockOnActive: true },
    { label: "设置", meta: "保留 Key 与记忆库", category: "settings", blockOnActive: false },
  ];
  for (const item of resetItems) {
    const name = document.createElement("span");
    name.style.flex = "1";
    const b = document.createElement("b");
    b.textContent = item.label;
    const small = document.createElement("small");
    small.style.display = "block";
    small.style.color = "var(--ink-3)";
    small.style.fontSize = "11px";
    small.textContent = item.meta;
    name.append(b, small);
    const btn = createButton({
      label: item.category === "settings" ? "重置" : "清空", size: "mini", variant: "danger",
      disabled: item.blockOnActive && activeTaskCount > 0,
      onClick: () => requestMaintenanceClear(item.category),
    });
    resetCard.append(connRow([name, btn]));
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
  resetCard.append(connRow([
    quickStartName,
    createButton({ label: "重新显示", size: "mini", onClick: () => void showQuickStart() }),
  ]));
  host.append(resetCard);

  // 翻译记忆库
  const tm = maintenanceCategory("tm");
  const tmCard = createCard([]);
  const tmBody = document.createElement("div");
  tmBody.style.padding = "14px 16px";
  tmBody.style.display = "flex";
  tmBody.style.flexDirection = "column";
  tmBody.style.gap = "8px";
  tmBody.append(sectionLabel("翻译记忆库"));
  const tmDesc = document.createElement("p");
  tmDesc.className = "note";
  tmDesc.style.fontSize = "12px";
  tmDesc.style.color = "var(--ink-3)";
  tmDesc.textContent = `先导出再删除，不会删除任何翻译输出。当前总占用：${tm ? formatBytes(tm.size_bytes) : "--"}。`;
  tmBody.append(tmDesc);
  tmBody.append(fieldRow([
    createButton({ label: "前往记忆库导出", size: "mini", onClick: () => showToast({ message: "请从左侧「记忆库」进入导出。" }) }),
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
      createButton({ label: "清空全部 TM", size: "mini", variant: "danger", disabled: activeTaskCount > 0, onClick: () => requestMaintenanceClear("tm") }),
    ]));
  } else {
    tmBody.append(fieldRow([
      createButton({ label: "清空全部 TM", size: "mini", variant: "danger", disabled: activeTaskCount > 0, onClick: () => requestMaintenanceClear("tm") }),
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
  dangerDesc.textContent = "只删除 Translator 当前应用数据，不删除应用、DMG、源码、旧版目录、源文件或输出目录。完成后需重新打开应用。";
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
    showToast({ message: "存在活动任务时不能执行该维护操作。", error: true });
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
                body: ["当前应用数据已删除。请关闭并重新打开 Translator，以全新状态继续使用。"],
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
          showToast({ message: "维护操作已完成；翻译输出未受影响。" });
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

function renderAboutPage(host: HTMLElement): void {
  const prefs = record(updateState?.preferences);
  const paused = Boolean(prefs.notifications_paused);
  const ignored = text(prefs.ignored_release_version);

  const card = createCard([]);
  const body = document.createElement("div");
  body.style.padding = "14px 16px";
  body.style.display = "flex";
  body.style.flexDirection = "column";
  body.style.gap = "10px";
  body.append(sectionLabel("版本"));

  if (updateChecking) {
    body.append(createEmptyState({ title: "正在检查更新…", icon: "help" }));
  } else if (updateResult) {
    const status = text(updateResult.status);
    const available = status === "available";
    const currentVersion = text(updateResult.current_version, APP_VERSION_FALLBACK);
    const latestVersion = text(updateResult.latest_version, text(updateResult.tag));
    const info = document.createElement("p");
    info.style.fontSize = "13px";
    info.textContent = `当前版本：${currentVersion}${latestVersion ? ` · 最新版本：${latestVersion}` : ""}`;
    body.append(info);

    const statusChipEl = createChip({
      label: available ? "有可用更新" : status === "current" ? "已是最新" : status === "release_not_ready" ? "发布包未就绪" : "检查失败",
      tone: available ? "tint" : status === "current" ? "ok" : "warn",
    });
    body.append(statusChipEl);

    if (available) {
      const dl = document.createElement("dl");
      dl.className = "kv";
      const rows: [string, string][] = [
        ["安装包", text(updateResult.asset_name)],
        ["发布日期", text(updateResult.release_date, "未提供")],
        ["SHA-256", text(updateResult.sha256)],
      ];
      for (const [k, v] of rows) {
        const dt = document.createElement("dt");
        dt.textContent = k;
        const dd = document.createElement("dd");
        dd.textContent = v || "--";
        dl.append(dt, dd);
      }
      body.append(dl);
    }

    const notes = document.createElement("p");
    notes.className = "note";
    notes.style.fontSize = "12.5px";
    notes.style.color = "var(--ink-3)";
    notes.textContent = text(updateResult.release_notes, text(updateResult.detail, "没有可用的更新说明。"));
    body.append(notes);

    const releaseUrl = text(updateResult.release_url);
    const downloadUrl = text(updateResult.download_url);
    const actions: HTMLElement[] = [
      createButton({ label: "重新检查", size: "mini", onClick: () => void runUpdateCheck() }),
      createButton({
        label: paused ? "恢复后台提醒" : "暂停后台提醒", size: "mini",
        onClick: () => void reRenderAfter(async () => {
          updateState = await client.request<JsonObject>("/api/updates/preferences", { method: "PUT", body: JSON.stringify({ notifications_paused: !paused }) });
          showToast({ message: !paused ? "已暂停后台更新提醒；手动检查仍然可用。" : "已恢复后台更新提醒。" });
        }),
      }),
    ];
    if (available && latestVersion && ignored !== latestVersion) {
      actions.push(createButton({
        label: "忽略此版本", size: "mini",
        onClick: () => void reRenderAfter(async () => {
          updateState = await client.request<JsonObject>("/api/updates/preferences", { method: "PUT", body: JSON.stringify({ ignored_release_version: latestVersion }) });
          showToast({ message: `已忽略版本 ${latestVersion}；后续版本仍会提示。` });
        }),
      }));
    }
    if (releaseUrl) actions.push(createButton({ label: "查看 Release", size: "mini", icon: "ext", onClick: () => void openExternalUrl(releaseUrl) }));
    if (available && downloadUrl) actions.push(createButton({ label: "下载 DMG", variant: "primary", size: "mini", onClick: () => void openExternalUrl(downloadUrl) }));
    body.append(fieldRow(actions));
  } else {
    const info = document.createElement("p");
    info.style.fontSize = "13px";
    info.textContent = `当前版本：${APP_VERSION_FALLBACK}`;
    body.append(info);
    body.append(fieldRow([
      createButton({ label: "检查更新", variant: "primary", size: "mini", onClick: () => void runUpdateCheck() }),
      createButton({
        label: paused ? "恢复后台提醒" : "暂停后台提醒", size: "mini",
        onClick: () => void reRenderAfter(async () => {
          updateState = await client.request<JsonObject>("/api/updates/preferences", { method: "PUT", body: JSON.stringify({ notifications_paused: !paused }) });
          showToast({ message: !paused ? "已暂停后台更新提醒；手动检查仍然可用。" : "已恢复后台更新提醒。" });
        }),
      }),
    ]));
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
  licenseText.textContent = "MIT License · © 2026 KlaraGraff";
  const licenseNote = document.createElement("p");
  licenseNote.className = "note";
  licenseNote.style.fontSize = "12px";
  licenseNote.style.color = "var(--ink-3)";
  licenseNote.textContent = "完整许可文本见应用安装包内的 LICENSE 文件。";
  licenseBody.append(licenseText, licenseNote);
  licenseCard.append(licenseBody);
  host.append(licenseCard);
}

async function runUpdateCheck(): Promise<void> {
  updateChecking = true;
  renderBody();
  try {
    const result = await client.request<JsonObject>("/api/updates/check?mode=manual");
    updateResult = result;
    setSettingsAlert(text(result.status) === "available");
  } catch (error) {
    showToast({ message: errorMessage(error), error: true });
  } finally {
    updateChecking = false;
    renderBody();
  }
}
