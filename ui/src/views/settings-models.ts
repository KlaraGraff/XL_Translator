// 设置视图 · 子页①「模型服务」：各模型角色的接入方式、连接池、模型表单（含草稿保存
// 与失焦自动提交）、模型列表获取与测试连接，以及模型配置和 API Key 的导入导出。
// 文档配置导入导出、诊断导出这些「数据与维护」页也在用的工具函数暂时也放在这里。
//
// 从 settings.ts 拆出来。页面状态仍然归 settings.ts 所有：这里只读那边导出的 let
// 绑定（ES 模块是活绑定，读到的永远是最新值），要改状态就调用那边的 setXxx()。
// settings.ts 也反过来引用这里的 renderModelsPage 等函数，形成循环引用；双方的顶层
// 代码都只声明不求值，所以是安全的。不要在本文件顶层读取 settings.ts 导出的值。

import { navigate } from "../router";
import {
  clearElement,
  createChip,
  createButton,
  createDropdown,
  createField,
  createSegmented,
  createSwitch,
  createSelectField,
  createEmptyState,
  openMenu,
  openModal,
  showToast,
  type ModalHandle,
} from "../components";
import { icon } from "../icons";
import { saveJsonFile } from "../save-file";
import { setModelPillSettingsRole } from "../model-pill";
import {
  type ApiKeyExportRow,
  apiKeyPageFor,
  applyProviderDerivedState,
  attachModelNameDropdown,
  client,
  CLOUD_PROVIDERS,
  connRow,
  disabledBaseUrlPlaceholder,
  ensureConnected,
  errorMessage,
  fieldRow,
  fieldWithHint,
  FOLLOW_PREFIX,
  formatCheckedAt,
  formatReleaseDate,
  hintBadge,
  type JsonObject,
  LOCAL_PROVIDER_BASE_URL_DEFAULTS,
  LOCAL_PROVIDERS,
  MODEL_ROLE_LABELS,
  MODEL_ROLE_ORDER,
  modelAccessDraft,
  modelCatalog,
  modelCatalogConnection,
  modelCatalogMessage,
  type ModelConfigExportResponse,
  modelFormDrafts,
  type ModelFormSubmission,
  type ModelImportPreview,
  modelImportPreview,
  modelRole,
  modelRoles,
  mountToken,
  openSettingsPage,
  optRow,
  persistSettings,
  type PoolConnection,
  providerBaseUrlDefaults,
  providerBaseUrlDisabled,
  openExternalUrl,
  providerLabel,
  queueModelFormSave,
  record,
  refreshDataHealth,
  refreshModelRoles,
  refreshSettings,
  renderBody,
  riseBelow,
  reRenderAfter,
  restoreModelFormDraft,
  sec,
  selectedConnection,
  selectField,
  setApplyProviderDerivedState,
  setModelImportPreview,
  setModelRole,
  setSubmitModelForm,
  settings,
  snapshotModelFormDraft,
  statusChip,
  submitModelForm,
  text,
  textField,
} from "./settings";
import { version as PACKAGE_VERSION } from "../../package.json";
import { ApiError } from "../api-client";
import { localeTag, t, tx, localized } from "../i18n";

// ---------------------------------------------------------------------------
// 子页①：模型服务
// ---------------------------------------------------------------------------

// 角色中文原名。settings.ts 的 MODEL_ROLE_LABELS 已随界面语言翻译，只能用于显示；
// 跟随连接的默认名称要存进设置，必须用这份中文原文，显示时再 tx()。
const MODEL_ROLE_LABELS_ZH: Record<string, string> = {
  translation: "文本翻译（Excel / Word / CAD）", // i18n-ignore 存进设置的值
  cleaner: "记忆库清洗", // i18n-ignore 存进设置的值
  image: "图像翻译", // i18n-ignore 存进设置的值
  pdf_review: "PDF 与图片审核", // i18n-ignore 存进设置的值
};

// 「改为转成 Word 翻译」：PDF 改成自动组合，能提取文字的页转成 Word 翻译，不用图像模型；
// 扫描页和保底方式仍交给图像模型——没有可用的图像模型时这些页保留原样，完成后列出来。
// 图纸和大图保持用户原来的选择（默认原位替换）。
const TEXT_ROUTE_PDF_PATCH = { translation_route: "auto", auto_text_route: "word" };
// 工作区缓存着一份 PDF 设置，改完发这个事件让它跟着换（见 workspace.ts）。
const PDF_ROUTES_CHANGED_EVENT = "xlt:pdf-routes-changed";

function pdfUsesTextRoutes(): boolean {
  const pdf = record(settings?.pdf);
  return text(pdf.translation_route) === "auto" && text(pdf.auto_text_route, "word") === "word";
}

function buildUseTextRoutesButton(): HTMLElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "resume-link";
  button.textContent = t("改为转成 Word 翻译");
  button.title = t("PDF 翻译方式改为「自动组合」，可提取文字的页转成 Word 翻译，不使用图像模型。");
  button.addEventListener("click", () => void reRenderAfter(async () => {
    await persistSettings({ pdf: TEXT_ROUTE_PDF_PATCH });
    window.dispatchEvent(new CustomEvent(PDF_ROUTES_CHANGED_EVENT, { detail: record(settings?.pdf) }));
    showToast({ message: t("已改为转成 Word 翻译：PDF 翻译方式为「自动组合」，可提取文字的页转成 Word 翻译。") });
  }));
  return button;
}

function savedAccessMode(role: string): string {
  const selected = activeConnection(role);
  if (selected?.connection_mode) {
    return selected.connection_mode === "follow"
      ? `${FOLLOW_PREFIX}${selected.source_role || "translation"}`
      : selected.connection_mode;
  }
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

// 用途的短名（分段控件、卡片标题、「X 当前使用的连接」里用）；全名带适用范围，见 MODEL_ROLE_LABELS。
const MODEL_ROLE_SHORT_LABELS: Record<string, string> = localized(() => ({
  translation: t("文本翻译"),
  cleaner: t("记忆库清洗"),
  image: t("图像翻译"),
  pdf_review: t("PDF 与图片审核"),
}));
// 用途卡第二行开头的适用范围（跟随别的用途时改写「跟随…」）。
const MODEL_ROLE_SCOPES: Record<string, string> = localized(() => ({
  translation: "Excel / Word / CAD",
  cleaner: t("记忆库词条"),
  image: t("整页图像"),
  pdf_review: t("逐页审核"),
}));

/** 连接方式的选项。label 是完整说法（下拉、悬停提示），short 是分段控件里放得下的短说法。 */
function accessModeOptions(role: string): { value: string; label: string; short: string }[] {
  const saved = record(modelRoles[role]);
  const sources = Array.isArray(saved.source_role_options)
    ? (saved.source_role_options as unknown[]).map((item) => text(item))
    : ["independent"];
  const options = [{ value: "cloud", label: t("云端 API"), short: t("云端 API") }];
  if (saved.supports_local !== false) {
    options.push({ value: "local", label: t("本地模型"), short: t("本地模型") });
  }
  for (const source of sources) {
    if (source === "independent") continue;
    options.push({
      value: `${FOLLOW_PREFIX}${source}`,
      label: t("跟随{0}", MODEL_ROLE_LABELS[source] || source),
      short: t("跟随{0}", MODEL_ROLE_SHORT_LABELS[source] || source),
    });
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

// 当前选中的是不是非主用连接。提交队列需要这个信息保留旧版主连接的密钥回退作用域。
function editableSecondaryConnection(role: string): PoolConnection | null {
  const selected = activeConnection(role);
  return selected && !selected.primary ? selected : null;
}

function modelCatalogConnectionKey(args: { role: string; mode: string; provider: string; baseUrl: string }): string {
  return [args.role, args.mode, args.provider, args.baseUrl.trim().replace(/\/$/, "")].join("|");
}

function modelCatalogConnectionForRole(role: string): string {
  const payload = record(modelRoles[role]);
  const selected = activeConnection(role);
  if (selected) {
    return modelCatalogConnectionKey({
      role,
      mode: selected.connection_mode || "cloud",
      provider: selected.provider,
      baseUrl: selected.base_url,
    });
  }
  return modelCatalogConnectionKey({
    role,
    mode: text(payload.mode, "cloud"),
    provider: text(payload.provider),
    baseUrl: text(payload.base_url),
  });
}

function clearModelCatalog(role: string, message = t("尚未获取当前连接的模型列表。")): void {
  delete modelCatalog[role];
  delete modelCatalogConnection[role];
  modelCatalogMessage[role] = message;
}

export function renderModelsPage(host: HTMLElement): void {
  // 导入导出的是整套模型服务，不属于下面任何一个用途或连接，挂在「用途」这一节的右上角。
  const exportBtn = createButton({ label: t("导出配置"), icon: "upload", size: "mini" });
  exportBtn.addEventListener("click", () => openMenu(exportBtn, [
    { label: t("导出（不含密钥）"), description: t("可放心分享，导入方需自行填写密钥。"), onSelect: () => void exportModelConfig(false) },
    { label: t("导出（含密钥）"), description: t("含密钥的文件将加密，默认有效期 30 天；从他人配置导入的密钥不会再次导出。"), onSelect: () => void exportModelConfig(true) },
  ], { width: 300 }));
  const rolesSec = sec({
    title: t("用途"),
    desc: t("每种用途可以指定不同的模型；「跟随」表示借用另一用途的连接，只需单独填写模型名称。"),
    actions: [exportBtn, createButton({ label: t("导入配置"), icon: "down", size: "mini", onClick: () => importModelConfig() })],
    className: "roles-sec",
  });
  // 用途切换（4 张卡）
  let seg = buildRoleSeg();
  rolesSec.body.append(seg);
  host.append(rolesSec.root);

  const role = modelRole;
  const rolePayload = record(modelRoles[role]);
  const engine = record(settings?.engine);
  const selected = activeConnection(role);
  const access = accessMode(role);
  const sourceRole = accessFollowSource(role);
  const following = sourceRole !== "independent";
  const followConnection = following ? roleConnections(sourceRole)[0] : null;
  const followedProvider = followConnection?.provider || selected?.provider || "";
  const cloudMode = access === "cloud" || (following && !LOCAL_PROVIDERS.includes(followedProvider));
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

  // 和其余表单字段一样在渲染这一刻算死（见 ModelFormSubmission）。以前它是在排队回调
  // **内部**才求值的：前一笔保存还在飞、用户又点了另一行时，第二笔提交会重新读全局状态
  // 拿到 null，本该写进 B 的服务商/Base URL/模型名改走角色级 PUT，被后端同步到 entry 0，
  // 等于把 B 的端点覆盖到主用连接 A 上。
  const secondaryConnection = editableSecondaryConnection(role);
  const secondaryId = secondaryConnection?.id ?? "";
  // 模式切换只是草稿，selected 仍是切换前的已保存行。表单必须从目标模式自己的
  // 记忆区取值；否则 cloud→local→cloud 会把 Ollama 地址和模型写回云端配置。
  const formProvider = following
    ? (followedProvider || provider)
    : cloudMode
      ? (selected?.connection_mode === "cloud" ? selected.provider : selected?.own_cloud_provider || (selected ? "custom_openai" : provider))
      : (selected?.own_local_provider || (selected?.connection_mode === "local" ? selected.own_provider : "") || "ollama");
  const formBaseUrl = following
    ? (followConnection ? followConnection.base_url : selected?.base_url ?? baseUrl)
    : cloudMode
      ? (selected?.connection_mode === "cloud" ? selected.base_url : selected?.own_cloud_base_url ?? baseUrl)
      : (selected?.own_local_base_url || (selected?.connection_mode === "local" ? selected.own_base_url : "") || LOCAL_PROVIDER_BASE_URL_DEFAULTS[formProvider] || "");
  const formModel = following
    ? (selected?.model ?? model)
    : cloudMode
      ? (selected?.connection_mode === "cloud" ? selected.model : selected?.own_cloud_model || (selected ? "" : model))
      : (selected?.own_local_model || (selected?.connection_mode === "local" ? selected.own_model : "") || "");

  // 连接方式草稿只属于当前选中的连接；选中另一行时立即丢弃。失焦自动保存会把草稿
  // 一并交上去，所以它在这一页存活期间会变：要用的地方都现算，不在渲染时算死。
  const accessDirty = () => modelAccessDraft[role] !== undefined && modelAccessDraft[role] !== savedAccessMode(role);

  // ---- 连接：列表在上，所选连接的表单紧接在同一节里 ----
  const connSec = sec({
    title: t("连接"),
    desc: t("{0}当前使用的连接。", MODEL_ROLE_SHORT_LABELS[role] || role),
    actions: [buildAddConnectionButton(role)],
  });
  let listCard = buildConnectionListCard(role);
  connSec.body.append(listCard);
  const detailCard = connSec.root;
  const detailBody = document.createElement("div");
  detailBody.className = "conn-form";
  connSec.body.append(detailBody);

  // 连接方式属于当前选中的这一条连接；切换另一行不会改变它。两三项用分段控件，
  // 跟随的来源多、超过五项时退回下拉。
  const accessOptions = accessModeOptions(role);
  const onAccessChange = (value: string) => {
    modelAccessDraft[role] = value;
    renderBody();
    // 切换即保存，和这一页其它字段一样。以前切完要再点「保存配置」，否则一离开
    // 设置页草稿就被丢掉（见 settings.ts unmount），用户以为切了其实没切。
    // renderBody() 已按新方式重画表单并登记了新的 submitModelForm，交的就是新表单。
    if (!selected) return;
    void reRenderAfter(async () => {
      await ensureFormSavedBeforeCatalog();
      showToast({ message: t("已自动保存 · {0}", new Date().toLocaleTimeString(localeTag(), { hour: "2-digit", minute: "2-digit" })) });
    }, { preserveDraft: false });
  };
  const accessControl: HTMLElement = accessOptions.length <= 5
    ? createSegmented({
      options: accessOptions.map((option) => ({ value: option.value, label: option.short })),
      value: access,
      ariaLabel: t("连接方式"),
      onChange: onAccessChange,
      motionKey: `models.access.${role}`,
    })
    : createDropdown({ options: accessOptions, value: access, onChange: onAccessChange }).root;
  if (accessControl.classList.contains("seg")) {
    const buttons = accessControl.querySelectorAll<HTMLButtonElement>("button");
    accessOptions.forEach((option, index) => { if (buttons[index]) buttons[index].title = option.label; });
  }
  const accessBar = createField(t("连接方式"), accessControl);
  accessBar.classList.add("access-field");
  // 四个以上选项时整行显示，分段里每一格才放得下「跟随…」。
  if (accessOptions.length > 3) accessBar.classList.add("span2");
  const accessLabel = accessBar.querySelector("label")!;
  accessLabel.append(hintBadge(t("选择「跟随」时，将沿用另一用途主用连接的服务商、地址和密钥，只需单独选择模型。")));
  const accessDirtyChip = createChip({ label: t("未保存"), tone: "warn" });
  if (accessDirty()) accessLabel.append(accessDirtyChip);

  const grid = document.createElement("div");
  grid.className = "grid2";
  grid.append(accessBar);

  // onChange 需要拿到下面才创建的 baseUrlField，用可变引用打个转 —— select 的 change
  // 事件只会在用户真的切换了选项时触发一次，天然满足「只在切换动作发生时重填，不覆盖
  // 用户已手填内容」的要求（初次渲染、其他按钮触发的重画都不会 fire change）。
  let onProviderChange: (value: string) => void = () => {};
  setApplyProviderDerivedState(() => {});
  const providerFieldHandle = selectField(
    cloudMode ? t("服务商") : t("本地运行器"),
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
  // 「获取 ↗ API 密钥」：打开所选服务商创建密钥的控制台，注册账号、建密钥都在那里。
  // 自定义配置、跟随别的用途、本地模型没有这一页，不显示。
  const keyLink = document.createElement("button");
  keyLink.type = "button";
  keyLink.className = "resume-link";
  keyLink.textContent = t("获取 ↗ API 密钥");
  providerFieldHandle.root.querySelector("label")?.append(keyLink);
  const keyPage = () => (cloudMode && !following
    ? apiKeyPageFor(providerSelectEl.value, baseUrlField?.input.value ?? formBaseUrl)
    : "");
  const paintKeyLink = () => {
    const url = keyPage();
    keyLink.hidden = !url;
    keyLink.title = url;
  };
  keyLink.addEventListener("click", () => {
    const url = keyPage();
    if (url) void openExternalUrl(url);
  });

  const baseUrlDisabledByProvider = cloudMode && providerBaseUrlDisabled.has(formProvider);
  const baseUrlField = textField(
    "Base URL",
    baseUrlDisabledByProvider ? "" : formBaseUrl,
    () => undefined,
    {
      placeholder: baseUrlDisabledByProvider ? disabledBaseUrlPlaceholder() : "https://.../v1",
      disabled: following || baseUrlDisabledByProvider,
    },
  );
  baseUrlField.input.id = "settings-model-base-url";
  // 只改 Base URL 的可填性和占位符，不动它的值——草稿回填时要按服务商重放这段联动，
  // 但绝不能把用户刚敲的 Base URL 冲掉。
  setApplyProviderDerivedState((value) => {
    if (!cloudMode) return;
    if (providerBaseUrlDisabled.has(value)) {
      baseUrlField.input.disabled = true;
      baseUrlField.input.placeholder = disabledBaseUrlPlaceholder();
      return;
    }
    baseUrlField.input.disabled = following;
    baseUrlField.input.placeholder = "https://.../v1";
  });
  onProviderChange = (value) => {
    if (!cloudMode) {
      // 本地运行器换人时 Base URL 也得跟着换：Ollama 和 LM Studio 监听的是不同端口，
      // 留着旧地址会被失焦自动保存原样存下去，之后每次「测试连接」都失败，而界面上
      // 没有一处提示地址不对。语义和云端那支一致——只在切换动作真的发生时重填，
      // 没有预设的自定义本地服务保持原样，不拿空字符串洗掉用户填的值。
      const localDefault = LOCAL_PROVIDER_BASE_URL_DEFAULTS[value];
      if (localDefault) baseUrlField.input.value = localDefault;
      return;
    }
    applyProviderDerivedState(value);
    baseUrlField.input.value = providerBaseUrlDisabled.has(value)
      ? ""
      : providerBaseUrlDefaults[value] ?? "";
    // 换了服务商就清空模型名称：A 家的模型 ID 拿到 B 家去必定报错。不预填任何型号，
    // 各家换代太快，写死的推荐很快过时；由用户获取模型列表后自己选。换到自定义配置
    // （各家自建网关）时保持原样，不拿空字符串去洗掉用户填的值。
    if (value !== "custom_openai") modelNameField.input.value = "";
    paintKeyLink();
  };

  const modelNameField = textField(t("模型名称"), formModel, () => undefined, { placeholder: cloudMode ? t("输入模型 ID") : t("例如 qwen2.5:7b"), hint: t("点击右侧箭头可展开候选列表；列表中没有的模型也可手动输入。") });
  modelNameField.input.id = "settings-model-name";
  const catalogConnection = modelCatalogConnectionForRole(role);
  const renderedBaseUrl = baseUrlField.input.value;
  // 模型列表属于「服务商 + Base URL」这个端点，和模型名无关：换个模型名不该把刚获取的
  // 列表清掉（以前每次保存都会清，于是从列表里选了一个模型之后，箭头里就只剩一个
  // 「服务商推荐型号」，想换回别的只能再获取一遍）。反过来，表单里换了服务商或地址，
  // 列表就不再属于它，候选必须立刻消失，不能等重画——以前换到 B 家后箭头里列的
  // 还是 A 家的模型。
  const catalogStillMatches = () => modelCatalogConnection[role] === catalogConnection
    && providerSelectEl.value === formProvider
    && baseUrlField.input.value === renderedBaseUrl;
  const currentCatalog = () => (catalogStillMatches() ? modelCatalog[role] || [] : []);
  // 原来挂的是原生 <datalist>：候选只在输入框获得焦点且内容匹配时才冒出来，长得像一
  // 条灰色系统条，跟这一页其余的下拉完全不是一套东西，用户也找不到「怎么把它调出来」。
  // 改成输入框右侧一个箭头按钮，点开走 openMenu，和「浏览」那类锚定菜单同一套外观。
  attachModelNameDropdown(modelNameField.input, currentCatalog);
  baseUrlField.input.addEventListener("input", paintKeyLink);
  paintKeyLink();
  // 两列：连接方式 | 服务商；模型名称 | Base URL；API 密钥 | 文本请求协议；连接名称整行。
  // 都要等 modelNameField 建好之后再挂进 DOM，因为 onProviderChange 的闭包引用它。
  grid.append(modelNameField.root, baseUrlField.root);
  detailBody.append(grid);

  let apiModeSelect: HTMLSelectElement | null = null;
  let apiModeRoot: HTMLElement | null = null;
  if (cloudMode) {
    const apiModeField = selectField(
      t("文本请求协议"),
      [
        { value: "auto", label: t("自动识别") },
        { value: "chat", label: "Chat Completions" },
        { value: "responses", label: "Responses" },
      ],
      text(following ? followConnection?.api_mode || selected?.api_mode : selected?.connection_mode === "cloud" ? selected.api_mode : selected?.own_cloud_api_mode || (selected ? "auto" : rolePayload.api_mode), "auto"),
      () => undefined,
      { disabled: following, hint: t("如不确定，请选择「自动识别」；仅在服务商明确要求时手动指定。") },
    );
    apiModeSelect = apiModeField.select;
    apiModeRoot = apiModeField.root;
  }

  const labelField = textField(t("连接名称"), selected?.label ?? "", () => undefined, { placeholder: t("例如 主账号 / 备用厂商") });
  const connectionLabelField = labelField.input;
  connectionLabelField.id = "settings-model-connection-label";
  labelField.root.classList.add("span2");

  let apiKeyField: HTMLInputElement | null = null;
  if (cloudMode) {
    const preview = text(selected?.api_key_preview);
    const keyInput = document.createElement("input");
    keyInput.type = "password";
    keyInput.autocomplete = "off";
    // 已保存的密钥直接以掩码当占位符摆在框里，不再在下面挂一行注释：注释和输入框
    // 中间隔着一段空白，「已保存」和「这个框是空的」看上去像在说两件互相矛盾的事。
    keyInput.placeholder = apiKeyPlaceholder(selected, following, preview);
    keyInput.disabled = following;
    keyInput.id = "settings-model-api-key";
    apiKeyField = keyInput;
    const keyField = fieldWithHint(t("API 密钥"), keyInput, t("留空表示沿用已保存的密钥；密钥仅保存在本机密钥存储中，不随配置导出（选择「导出（含密钥）」时除外）。"));
    grid.append(keyField);
  }
  if (apiModeRoot) grid.append(apiModeRoot);
  grid.append(labelField.root);

  // 速率与重试不再按模型设置（v38 起在「翻译参数」里按文件类型设置），这里只留一句说明
  // 指过去。清洗记忆库没有对应的设置项，只说自动保存。
  const rateNote = document.createElement("p");
  rateNote.className = "sec-note rate-pointer";
  if (role === "cleaner") {
    rateNote.textContent = t("修改后自动保存。");
  } else {
    rateNote.append(document.createTextNode(t("修改后自动保存。速率与重试在「翻译参数」中按文件类型设置。")));
    const goto = document.createElement("button");
    goto.type = "button";
    goto.className = "resume-link";
    goto.textContent = t("前往");
    goto.addEventListener("click", () => openSettingsPage("params", role === "translation" ? "excel" : "pdf"));
    rateNote.append(goto);
  }
  detailBody.append(rateNote);

  // 状态胶囊。取的是**当前选中那条连接**的测试结果，不是角色级的那份——角色级状态
  // 是主用连接的镜像（core/model_roles.py 里照抄 primary 的三个字段），拿它当详情卡的
  // 状态，新建的第二条连接一挂上来就顶着主用连接的「测试通过」，用户会以为它已经验过。
  // 现在列表始终是本角色拥有的连接，每行都有独立测试结论。
  //
  // 整块做成可重画的：失焦自动保存不重画整页（会抢焦点），但换了模型或密钥之后测试
  // 结论已经被后端打回「未测试」，这里还挂着「测试通过」就是在报告没发生的事。
  // 节底一行：左边状态胶囊和自动保存时间，右边三个按钮；失败原因在这一行下面。
  const formFoot = document.createElement("div");
  formFoot.className = "conn-foot";
  const statusRow = document.createElement("div");
  statusRow.className = "conn-status";
  const autoSaveNote = document.createElement("span");
  autoSaveNote.className = "conn-saved";
  const statusReasonNote = document.createElement("div");
  statusReasonNote.className = "conn-reason";
  const paintStatus = (connection: PoolConnection | null) => {
    const availabilitySource: JsonObject = connection
      ? { availability_status: connection.availability_status, availability_message: connection.availability_message, availability_checked_at: connection.availability_checked_at, config_problem: connection.config_problem ?? "" }
      : rolePayload;
    // 配置本身用不了（多半是跟随的来源换了服务商）和「测过、没通过」是两回事：前者不用
    // 去点「测试连接」，要改的是连接方式或服务商，所以胶囊写「不可用」而不是「测试失败」。
    const configProblem = text(availabilitySource.config_problem);
    const availability = text(availabilitySource.availability_status, "unknown");
    const availabilityTone = availability === "available" ? "done" : availability === "unavailable" ? "error" : "";
    const availabilityLabel = configProblem
      ? t("不可用")
      : availability === "available" ? t("测试通过") : availability === "unavailable" ? t("测试失败") : t("未测试");
    const checkedAt = formatCheckedAt(text(availabilitySource.availability_checked_at));
    const matches = catalogStillMatches();
    const list = currentCatalog();
    const catalogChip = statusChip(list.length ? t("{0} 个可用模型", list.length) : t("未获取列表"), list.length ? "done" : "");
    catalogChip.title = matches ? (tx(modelCatalogMessage[role]) || t("尚未获取当前连接的模型列表。")) : t("当前连接尚未获取模型列表。保存配置后可手动获取。");
    const availChip = statusChip(availabilityLabel, availabilityTone);
    availChip.title = tx(text(availabilitySource.availability_message, t("当前配置尚未测试。"))) + (checkedAt ? ` · ${checkedAt}` : "");
    clearElement(statusRow);
    statusRow.append(catalogChip, availChip, autoSaveNote);

    // 失败原因必须看得见。之前它只挂在胶囊的 title 上：不悬停就永远读不到，用户面对一个
    // 红色的「测试失败」没有任何线索（是密钥错、地址错，还是对方在维护），只能反复点
    // 「测试连接」。「未获取列表」同样——光看这四个字看不出下一步该点哪里。
    // 胶囊上已经写了「测试失败」，原因这里不再重复一遍；只有失败原因那一句和胶囊同用琥珀色，
    // 后面「还没获取模型列表」这类普通说明保持灰色。旧版本存下的原因还带着
    // 「XX连接测试失败：」前缀，显示前去掉。
    const statusParts: HTMLElement[] = [];
    if (availability === "unavailable") {
      const reason = text(availabilitySource.availability_message, t("上次测试未记录原因，请重新测试。"))
        .replace(/^[^：]{0,16}连接测试失败：/, ""); // i18n-ignore 匹配后端原文里的旧前缀，去掉后再 tx()
      const failure = document.createElement("span");
      failure.style.color = "var(--warn)";
      failure.textContent = `${tx(reason)}${checkedAt ? t("（{0}）", checkedAt) : ""}`;
      statusParts.push(failure);
      if (role === "image" && !pdfUsesTextRoutes()) statusParts.push(buildUseTextRoutesButton());
    }
    if (!list.length) {
      const note = document.createElement("span");
      note.textContent = matches
        ? tx(modelCatalogMessage[role]) || t("尚未获取当前连接的模型列表，请点击「获取模型列表」。")
        : t("当前连接尚未获取模型列表。请保存配置后点击「获取模型列表」。");
      statusParts.push(note);
    }
    statusReasonNote.hidden = !statusParts.length;
    clearElement(statusReasonNote);
    statusReasonNote.append(
      ...statusParts.flatMap((part, index) => (index ? [document.createTextNode(" · "), part] : [part])),
    );
  };
  paintStatus(selected);

  // 保存 / 获取模型 / 测试连接 / 导出导入
  // 提交是串行排队的，真正发出去时可能已经隔了一次重画：用户切了角色卡、切了
  // 云端/本地、换了选中的连接。所以「这份表单属于谁、该走哪条路由」必须在渲染这一刻
  // 就定死，随表单一起排队；到执行时再去读全局状态，会把这一份值写到另一个角色头上，
  // 或者带着云端服务商发出一个 mode=local。
  const readModelForm = (): ModelFormSubmission => ({
    role,
    access,
    sourceRole,
    secondaryId,
    selectedId: selected?.id ?? "",
    provider: providerSelectEl.value,
    baseUrl: baseUrlField.input.value,
    model: modelNameField.input.value,
    apiKey: apiKeyField?.value ?? "",
    connectionLabel: connectionLabelField.value,
    apiMode: apiModeSelect?.value ?? "auto",
  });
  setSubmitModelForm(() => {
    const form = readModelForm();
    return queueModelFormSave(() => saveModel(form, { silent: true }));
  });

  // 失焦即保存。用户的原话：「任何离开焦点的行为都应该自动保存」。change 事件只在值
  // 真的变了之后失焦才触发，所以不会出现「点进去又点出来也发一次请求」。这里刻意不
  // 重画：重画会把焦点从用户正要去的下一个字段上抢走。
  //
  // 不重画整页，但保存之后这一页上「别处」显示的东西必须跟上：角色卡上的模型名、连接
  // 列表的名称和「无密钥」标记、测试结论（换模型/密钥后后端已打回未测试）、速率（换
  // 模型后落到另一档）、「未保存」标记、密钥框。以前这些都要等下一次整页重画才变，
  // 用户看到的是一半新一半旧的页面，再一重画数值又「自己变了」。
  const syncAfterSave = (sentApiKey: string) => {
    if (!detailCard.isConnected || modelRole !== role) return;
    const fresh = activeConnection(role);
    // 选中的连接已经换了：下一次整页重画会按新连接画，这里不去改一张过期的表单。
    if ((fresh?.id ?? "") !== (selected?.id ?? "")) return;
    const nextSeg = buildRoleSeg();
    seg.replaceWith(nextSeg);
    seg = nextSeg;
    const nextList = buildConnectionListCard(role);
    listCard.replaceWith(nextList);
    listCard = nextList;
    if (accessDirty()) {
      if (!accessDirtyChip.isConnected) accessLabel.append(accessDirtyChip);
    } else {
      accessDirtyChip.remove();
    }
    paintStatus(fresh);
    if (apiKeyField) {
      // 密钥已经存进本机密钥存储，框里不该继续留着明文；之后每次失焦保存也不会再
      // 把同一把密钥重复提交、把测试结论一次次打回「未测试」。
      if (sentApiKey && apiKeyField.value === sentApiKey) apiKeyField.value = "";
      apiKeyField.placeholder = apiKeyPlaceholder(fresh, following, text(fresh?.api_key_preview));
    }
  };
  const autoSaveOnBlur = (event: Event) => {
    const field = event.currentTarget as HTMLInputElement | HTMLSelectElement;
    if (field.disabled) return;
    // 服务商下拉先派发 change，随后才回调 onProviderChange 重填 Base URL 和推荐模型名。
    // 在这里同步读表单会读到换服务商之前的模型名：换成 DeepSeek 后存下的是空模型名，
    // 刷新后卡片显示「缺少模型名称」。推迟到当前事件处理完再读。
    queueMicrotask(() => saveFormNow());
  };
  const saveFormNow = () => {
    const form = readModelForm();
    void queueModelFormSave(async () => {
      try {
        await saveModel(form, { silent: true });
        syncAfterSave(form.apiKey);
        autoSaveNote.textContent = t("已自动保存 · {0}", new Date().toLocaleTimeString(localeTag(), { hour: "2-digit", minute: "2-digit" }));
      } catch (error) {
        autoSaveNote.textContent = "";
        showToast({ message: t("自动保存失败：{0}", errorMessage(error)), error: true });
      }
    });
  };
  for (const field of [providerSelectEl, baseUrlField.input, modelNameField.input, connectionLabelField, apiKeyField, apiModeSelect]) {
    field?.addEventListener("change", autoSaveOnBlur);
  }

  const doSaveModel = () => {
    const form = readModelForm();
    void reRenderAfter(
      () => queueModelFormSave(() => saveModel(form)),
      { preserveDraft: false },
    );
  };
  const formActions = fieldRow([
    createButton({ label: t("获取模型列表"), size: "mini", onClick: () => void reRenderAfter(async () => {
      await ensureFormSavedBeforeCatalog();
      const result = await client.request<{ ok: boolean; models: string[]; message: string }>(
        `/api/models/catalog/${encodeURIComponent(role)}`,
        { method: "POST", body: JSON.stringify({ refresh: true, connection_id: selected?.id ?? "" }) },
      );
      modelCatalog[role] = result.models;
      modelCatalogMessage[role] = result.message;
      modelCatalogConnection[role] = modelCatalogConnectionForRole(role);
      showToast({ message: result.models.length ? t("已获取 {0} 个模型，可通过模型名称右侧的箭头选择。", result.models.length) : tx(result.message), error: !result.ok });
      // 表单已经被 ensureFormSavedBeforeCatalog 提交了，重画该显示服务端回填的值，
      // 再把草稿盖回去只会让「已保存的密钥」以明文停在框里。
    }, { preserveDraft: false }) }),
    createButton({ label: t("测试连接"), size: "mini", onClick: () => void reRenderAfter(async () => {
      await ensureFormSavedBeforeCatalog();
      // 测的必须是面板正在显示的这条连接：不带 id 时后端一律拨主连接。
      const result = await client.request<{ ok: boolean; message: string }>(
        `/api/models/connectivity/${encodeURIComponent(role)}`,
        { method: "POST", body: JSON.stringify({ connection_id: selected?.id ?? "" }) },
      );
      showToast({ message: tx(result.message), error: !result.ok });
      await refreshModelRoles();
    }, { preserveDraft: false }) }),
    createButton({ label: t("保存配置"), icon: "check", variant: "primary", size: "mini", onClick: doSaveModel }),
  ]);
  formActions.classList.add("acts");
  formFoot.append(statusRow, formActions);
  detailBody.append(formFoot, statusReasonNote);
  host.append(connSec.root);

  // 多个任务同时运行：全局行为，不属于某个用途，放在页底。
  const spreadSw = createSwitch({
    checked: Boolean(settings?.spread_tasks_across_connections),
    onChange: (checked) => void reRenderAfter(() => persistSettings({ spread_tasks_across_connections: checked })),
  });
  spreadSw.setAttribute("aria-label", t("多个任务同时运行时分散到不同连接"));
  const spreadSec = sec();
  spreadSec.body.append(optRow({
    title: t("多个任务同时运行时分散到不同连接"),
    hint: t("关闭时，所有任务均使用主用连接，其他连接仅作故障切换备用；开启后，同时运行的任务各自占用一条空闲连接。"),
    desc: t("多个任务共用同一条连接时，请求会叠加，可能被服务商限速，费用也会增加。"),
    control: spreadSw,
  }));
  host.append(spreadSec.root);
}

function apiKeyPlaceholder(connection: PoolConnection | null, following: boolean, preview: string): string {
  // 跟随行的 has_api_key 来自来源连接。草稿刚从跟随切到云端时，不能把来源的
  // 掩码冒充为当前行已存的独立密钥。
  if (following) return t("跟随时使用来源连接的密钥");
  return connection?.connection_mode === "cloud" && connection.has_api_key
    ? t("{0}（已保存 · 留空则不改）", preview || "••••••")
    : t("粘贴该连接的 API 密钥");
}

// 选中另一条连接（点行、点行内「测试」、新增、删除、设为主用、导入）时调用。
// 「连接方式」草稿只属于原来那条连接：以前只有点行会丢它，点别的行的「测试」或者新增
// 连接后草稿原样留着，新选中的连接一打开就顶着上一条的「未保存 · 本地模型」，下一次
// 失焦自动保存就把它真的切成了本地。
function selectConnection(role: string, connectionId: string | null): void {
  if (connectionId) selectedConnection[role] = connectionId;
  else delete selectedConnection[role];
  delete modelAccessDraft[role];
}

function buildRoleSeg(): HTMLElement {
  const seg = document.createElement("div");
  seg.className = "roles";
  seg.setAttribute("role", "tablist");
  for (const role of MODEL_ROLE_ORDER) {
    const payload = record(modelRoles[role]);
    const card = document.createElement("button");
    card.type = "button";
    card.className = role === modelRole ? "role on" : "role";
    card.setAttribute("role", "tab");
    card.setAttribute("aria-selected", String(role === modelRole));
    const b = document.createElement("b");
    b.textContent = MODEL_ROLE_SHORT_LABELS[role] || role;
    // 四张卡挤在一行，标题只占一行、放不下就省略，完整名字悬停可见。
    b.title = MODEL_ROLE_LABELS[role] || role;
    const primary = roleConnections(role)[0];
    const problem = text(primary?.config_problem ?? payload.config_problem);
    const primarySource = primary?.source_role || text(payload.source_role, "independent");
    const following = primary?.connection_mode === "follow";
    const modelName = primary ? primary.model : text(payload.model);
    const scope = following
      ? t("跟随{0}", MODEL_ROLE_SHORT_LABELS[primarySource] || primarySource)
      : MODEL_ROLE_SCOPES[role] || "";
    const modeSummary = modelName || (following ? "" : primary?.connection_mode === "local" ? t("本地模型") : primary ? t("云端 API") : t("未配置"));
    const span = document.createElement("span");
    span.className = "role-sub";
    const summary = document.createElement("i");
    summary.textContent = [scope, modeSummary].filter(Boolean).join(" · ");
    span.append(summary);
    // 有连接但模型名还空着：跟随行尤其容易漏——卡片写着「跟随…」看起来已经配好，
    // 真用到这个用途时才报错。在卡片上直接标出来；摘要太长时省略的是摘要，不是这个标记。
    if (problem) {
      // 别的用途改了连接，这个用途跟着用不了：卡片上直接标出来，原因放在悬停提示里。
      const unusable = document.createElement("em");
      unusable.textContent = t("不可用");
      span.append(unusable);
    } else if (primary && !modelName) {
      const missing = document.createElement("em");
      missing.textContent = t("缺少模型名称");
      span.append(missing);
    }
    span.title = [
      summary.textContent,
      problem ? tx(problem) : primary && !modelName ? t("缺少模型名称") : "",
    ].filter(Boolean).join(" · ");
    card.append(b, span);
    card.addEventListener("click", () => {
      if (modelRole === role) return;
      const draft = snapshotModelFormDraft();
      if (draft) modelFormDrafts[modelRole] = draft;
      setModelRole(role);
      setModelPillSettingsRole(modelRole);
      renderBody();
      const roleDraft = modelFormDrafts[role];
      // 草稿只用一次。留着它的话，之后每次切回这个角色都会把那一刻的旧值再糊一遍，
      // 盖掉期间已经保存（或被导入、被跟随来源改动）的新值，下一次失焦又把旧值写回去。
      delete modelFormDrafts[role];
      if (roleDraft) restoreModelFormDraft(roleDraft);
      // 用途卡就是这一页的页签：卡片原地不动，下面换成这个用途的连接，和翻译参数换文件类型同一种。
      riseBelow(".roles-sec");
    });
    seg.append(card);
  }
  return seg;
}

function buildAddConnectionButton(role: string): HTMLButtonElement {
  return createButton({
    label: t("新增连接"), size: "mini", icon: "plus",
    onClick: () => void reRenderAfter(async () => {
      const payload = await client.request<JsonObject>(
        `/api/models/roles/${encodeURIComponent(role)}/connections`,
        { method: "POST", body: JSON.stringify({ label: "", connection_mode: "cloud", base_url: "" }) },
      );
      modelRoles[role] = payload;
      const added = Array.isArray(payload.connections) ? payload.connections : [];
      if (added.length) selectConnection(role, text(record(added[added.length - 1]).id));
      showToast({ message: t("已新增一条连接。填写 Base URL、模型和密钥后，点击「保存配置」。") });
    }, { preserveDraft: false }), // 表单切到了新连接的空白字段，不该把旧连接的草稿糊上去
  });
}

function buildConnectionListCard(role: string): HTMLElement {
  const selected = activeConnection(role);
  const connections = roleConnections(role);
  const list = document.createElement("div");
  list.className = "conn-list";
  if (!connections.length) {
    list.append(createEmptyState({ title: t("尚无连接"), description: t("点击右上角的「新增连接」开始配置。"), icon: "gear" }));
    return list;
  }
  for (const [index, connection] of connections.entries()) {
    const tone = connection.availability_status === "available"
      ? "ok" : connection.availability_status === "unavailable" ? "warn" : "mute";
    const dot = document.createElement("span");
    dot.className = `cd ${tone}`;
    const name = document.createElement("b");
    name.textContent = tx(connection.display_label) || t("连接 {0}", index + 1);
    // 灰字「服务商 · 模型」：跟随行显示的是来源连接的服务商。
    const meta = document.createElement("span");
    meta.className = "conn-meta";
    meta.textContent = [providerLabel(connection.provider), connection.model].filter(Boolean).join(" · ");
    meta.title = meta.textContent;
    const chips: HTMLElement[] = [];
    if (index === 0) chips.push(createChip({ label: t("主用"), tone: "tint" }));
    // 没起名的跟随行，名称本身就是「跟随…」（见 saveModel），再挂一个同样文字的胶囊是重复。
    // 两边比的都是显示文字：name 显示的是 tx(display_label)，这里同样是翻译后的说法。
    const followChip = t("跟随{0}", MODEL_ROLE_LABELS[connection.source_role || ""] || connection.source_role || t("其他用途"));
    if (connection.connection_mode === "follow" && name.textContent !== followChip) chips.push(createChip({ label: followChip, tone: "tint" }));
    if (connection.connection_mode === "local") chips.push(createChip({ label: t("本地"), tone: "mute" }));
    if (!connection.has_api_key && !LOCAL_PROVIDERS.includes(connection.provider)) {
      chips.push(createChip({ label: t("无密钥"), tone: "warn" }));
    }
    // 测试结论：通过是绿的，没通过才是需要人处理的橙色；没测过不挂。
    if (connection.availability_status === "available") chips.push(createChip({ label: t("测试通过"), tone: "ok" }));
    else if (connection.availability_status === "unavailable") chips.push(createChip({ label: t("未通过"), tone: "warn" }));
    const actions = document.createElement("div");
    actions.className = "acts";
    actions.append(...chips, createButton({
      label: t("测试"), size: "mini", variant: "ghost",
      onClick: (e) => { e.stopPropagation(); void testConnectionRow(role, connection.id); },
    }));
    if (index > 0) {
      actions.append(createButton({
        label: t("设为主用"), size: "mini", variant: "ghost",
        onClick: (e) => { e.stopPropagation(); void promoteConnection(role, connection.id); },
      }));
    }
    if (connections.length > 1) {
      actions.append(createButton({
        label: t("删除"), size: "mini", variant: "danger",
        onClick: (e) => { e.stopPropagation(); confirmDeleteConnection(role, connection); },
      }));
    }
    const row = connRow([dot, name, meta, actions], {
      selected: Boolean(selected && selected.id === connection.id),
      onClick: () => {
        selectConnection(role, connection.id);
        renderBody();
      },
    });
    list.append(row);
  }
  return list;
}

async function ensureFormSavedBeforeCatalog(): Promise<void> {
  // 「获取模型列表」和「测试连接」两个后端路由都只认**已保存**的配置（api/app.py 里
  // 明写着不接收表单草稿）。所以这里必须先把表单交上去，再去调它们——这个函数以前只
  // 做了 refreshModelRoles()，等于把服务端的旧值又拉了一遍，用户刚换的密钥根本没上去，
  // 「测试连接」测的是旧密钥，永远失败。
  if (submitModelForm) await submitModelForm();
  await refreshModelRoles();
}

// 除 role 以外各用途主用连接眼下用不了的原因（没有问题的用途不列）。
function otherRoleProblems(role: string): Record<string, string> {
  const problems: Record<string, string> = {};
  for (const other of MODEL_ROLE_ORDER) {
    if (other === role) continue;
    const primary = roleConnections(other)[0];
    const problem = text(primary?.config_problem ?? record(modelRoles[other]).config_problem);
    if (problem) problems[other] = problem;
  }
  return problems;
}

async function saveModel(
  form: ModelFormSubmission,
  opts: { silent?: boolean } = {},
): Promise<void> {
  const role = form.role;
  if (!form.selectedId) throw new Error(t("请先新增或选择一条连接。"));
  const following = form.sourceRole !== "independent";
  const connectionMode = following ? "follow" : form.access;
  const body: JsonObject = {
    connection_mode: connectionMode,
    source_role: form.sourceRole,
    label: form.connectionLabel || (following ? `跟随${MODEL_ROLE_LABELS_ZH[form.sourceRole] || form.sourceRole}` : ""), // i18n-ignore 存进设置的连接名用中文原文，显示时 tx()
    model: form.model,
  };
  // 跟随行的端点属于来源连接。把界面上展示的有效端点一并提交，会把它写进本行的
  // 独立配置，并在切回云端时覆盖用户原先保存的服务商与地址。
  if (!following) {
    body.provider = form.provider;
    body.base_url = form.baseUrl;
    body.api_mode = form.apiMode;
    if (form.apiKey && connectionMode === "cloud") body.api_key = form.apiKey;
  }
  const problemsBefore = otherRoleProblems(role);
  modelRoles[role] = await client.request<JsonObject>(
    `/api/models/roles/${encodeURIComponent(role)}/connections/${encodeURIComponent(form.selectedId)}`,
    { method: "PUT", body: JSON.stringify(body) },
  );
  // 这一行可能是别的用途跟随的来源：换了服务商或改成本地模型后，跟随它的用途也许就用
  // 不了了（例如 DeepSeek 不能生成图片，图像翻译模型跟着失效）。这次保存照常生效，但必须当场
  // 告诉用户哪个用途受了影响，不能等他跑任务时才发现。
  await refreshModelRoles();
  for (const [otherRole, problem] of Object.entries(otherRoleProblems(role))) {
    if (problemsBefore[otherRole] === problem) continue;
    showToast({ message: t("已保存。{0}", tx(problem)), duration: 8000 });
  }
  // 只丢掉「这次交上去的那份」连接方式草稿。失焦保存是排队执行的：用户点「本地模型」
  // 单选框时，先触发的是上一个输入框的失焦保存（带着旧的连接方式），它落盘时用户已经
  // 选好了新方式——以前这里不分青红皂白地删草稿，界面上的「本地」就被悄悄撤回成云端。
  if (modelAccessDraft[role] === form.access) delete modelAccessDraft[role];
  if (form.apiKey && connectionMode === "cloud" && !form.secondaryId) {
    // 保留旧版 provider 作用域的回退密钥，供尚未单独存过 Key 的旧角色继续使用。
    await client.request(`/api/keys/${form.provider}`, { method: "PUT", body: JSON.stringify({ api_key: form.apiKey, base_url: form.baseUrl }) });
  }
  // 模型列表不在这里清：它按「服务商 + Base URL」记在 modelCatalogConnection 里，端点
  // 变了自然对不上、不再显示；只改模型名或连接名称时，刚获取的列表应当继续可选。
  await refreshSettings();
  // keys.json 的惰性自愈恰好发生在上面这次保存里；恢复事件落盘后若没人重新拉
  // /api/data/health，本次会话内横幅永远不露面——丢 Key 的提示必须在丢的那一刻
  // 给。失败静默，语义与 bootstrap 那次一致。
  void refreshDataHealth(mountToken);
  if (!opts.silent) showToast({ message: t("连接已保存。密钥仅写入本机密钥存储。") });
}

async function testConnectionRow(role: string, connectionId: string): Promise<void> {
  if (activeConnection(role)?.id !== connectionId) selectConnection(role, connectionId);
  // 走 reRenderAfter 而不是裸 renderBody()：测试这条连接不该把用户刚敲的字冲掉。
  // 若这一下同时切换了连接，草稿的 scope 对不上，会自动丢弃而不是搬到新连接上。
  await reRenderAfter(async () => {
    const result = await client.request<{ ok: boolean; message: string }>(
      `/api/models/connectivity/${encodeURIComponent(role)}`,
      { method: "POST", body: JSON.stringify({ connection_id: connectionId }) },
    );
    showToast({ message: tx(result.message), error: !result.ok });
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
    showToast({ message: t("已设为主用连接。") });
  }, { preserveDraft: false }); // 主用连接换人，表单跟着切到新连接，旧连接的草稿（含刚粘的密钥）绝不能糊过去
}

async function deleteConnection(role: string, connectionId: string): Promise<void> {
  await reRenderAfter(async () => {
    modelRoles[role] = await client.request<JsonObject>(
      `/api/models/roles/${encodeURIComponent(role)}/connections/${encodeURIComponent(connectionId)}`,
      { method: "DELETE" },
    );
    selectConnection(role, null);
    // 删掉的可能是主用连接，跟随它的角色、顶栏模型药丸和翻译角色的旧字段都跟着变。
    await refreshSettings();
    clearModelCatalog(role, t("连接已变更，请重新获取模型列表。"));
    // 删除连接会经由 save_connection_key 写 keys.json，同样可能触发惰性自愈。
    void refreshDataHealth(mountToken);
    showToast({ message: t("连接已删除，其密钥也已从本机移除。") });
  }, { preserveDraft: false }); // 表单会切回默认连接，不该把被删连接的草稿糊上去
}

function confirmDeleteConnection(role: string, connection: PoolConnection): void {
  const name = tx(connection.display_label || connection.label) || t("这条连接");
  openModal({
    tone: "danger", icon: "trash",
    sourceLabel: t("设置 · 模型服务 · {0}", MODEL_ROLE_LABELS[role] || role),
    title: t("删除「{0}」？", name),
    body: [
      t("此操作仅移除当前用途中的「{0}」，不会修改其跟随的来源连接。", name),
      connection.primary ? t("删除主用连接后，列表中的下一条连接会成为主用。") : t("其他连接不受影响。"),
    ],
    actions: [
      { label: t("取消") },
      { label: t("确认删除"), variant: "danger-solid", onClick: () => void deleteConnection(role, connection.id) },
    ],
  });
}

// 后端 core/config_crypto.py 在文件被改过一个字节（AAD 绑定了到期日和正文哈希）时
// 原样抛出的这句话。要求原文照抄，不能换成界面自己的措辞。
const CORRUPT_IMPORT_DETAIL = "此文件在传输过程中已损坏或被修改，未导入任何内容。请让发送方重新发送。"; // i18n-ignore 与后端原文比较，显示时 tx()

// 三条导入路径（正常 / 过期 / 版本太旧）提交的 payload 是同一份，连接列表一律整份
// 替换。别再写「不删除未提及配置」：本机在这些角色下自己加的连接会连同它们保存的
// Key 一起消失，说成「只合并」等于骗人。过期与版本太旧那两条路径的按钮写着「仅导入
// 配置」，更需要这句话——「仅」字很容易被读成「影响有限」。
const replacesConnectionsNotice = (): string =>
  t("文件中所含用途的连接列表将整体替换本机现有连接（本机添加的连接及已保存的密钥将被清除）；文件中未包含的用途保持不变。");

// 有效期选项与 core/config_crypto.py::DEFAULT_VALID_DAYS 对齐：0 = 长期有效。
const VALID_DAYS_OPTIONS: { label: string; value: number }[] = localized(() => ([
  { label: t("7 天"), value: 7 },
  { label: t("30 天（推荐）"), value: 30 },
  { label: t("90 天"), value: 90 },
  { label: t("长期有效"), value: 0 },
]));
const DEFAULT_VALID_DAYS = 30;

/** 路径用等宽字体展示，和「文件」「模型」这类普通文案区分开。 */
function monoText(value: string): HTMLElement {
  const span = document.createElement("span");
  span.textContent = value;
  span.style.fontFamily = "var(--mono)";
  span.style.fontSize = "11.5px";
  span.style.wordBreak = "break-all";
  return span;
}

/** kv 行的「一段文字 + 一个 chip」组合，例如「3 个作用域 [已自动解开]」。 */
function textAndChip(prefix: string, chip: HTMLElement): HTMLElement {
  const span = document.createElement("span");
  span.append(document.createTextNode(prefix), chip);
  return span;
}

/** 复用既有的 dl.kv（app.css 里已有，别处的更新检查弹窗也在用），不引入样张自带的带边框变体。 */
function buildKvList(rows: [string, string | HTMLElement][]): HTMLDListElement {
  const dl = document.createElement("dl");
  dl.className = "kv";
  for (const [key, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    // app.css 的 .kv dd 是单行加省略号，那是给短值设计的。这里的值有保存路径，
    // 省略号从尾部砍，砍掉的正是文件名——用户最需要看到的那一段。改成换行。
    dd.style.whiteSpace = "normal";
    dd.style.overflow = "visible";
    if (typeof value === "string") dd.textContent = value;
    else dd.append(value);
    dl.append(dt, dd);
  }
  return dl;
}

/** 对应样张的 .banner.warn / .banner.tint：图标 + 一段说明文字，没有独立标题。 */
function buildInlineBanner(tone: "warn" | "tint", message: string): HTMLElement {
  // 结构对齐既有的 .banner.warn 用法（workspace.ts::buildOutsideCellBanner）：icon() 自己
  // 带 className "ico"，不用额外包一层 span。样张里的「→」在图标集里没有对应符号，
  // 借「ext」（跳转/指向另一处的箭头）凑近似语义；tint 是这次新增的中性变体。
  const banner = document.createElement("div");
  banner.className = `banner ${tone}`;
  banner.append(icon(tone === "warn" ? "warn" : "ext", { className: "ico" }));
  const tx = document.createElement("span");
  tx.className = "tx";
  tx.textContent = message;
  banner.append(tx);
  return banner;
}

/** 距离某个 ISO 时间点还剩几天，向上取整：只要还没到点就至少算「还剩 1 天」，
 *  不会在还剩 11 小时的时候显示「还剩 0 天」——那读起来像是已经过期了。 */
function daysFromNow(iso: string): number {
  const target = new Date(iso).getTime();
  if (Number.isNaN(target)) return 0;
  return Math.max(0, Math.ceil((target - Date.now()) / 86400000));
}

/** 某个 ISO 时间点已经过去几天，向下取整；不满一天返回 0，由调用方改口说「刚刚」。 */
function daysSince(iso: string): number {
  const target = new Date(iso).getTime();
  if (Number.isNaN(target)) return 0;
  return Math.max(0, Math.floor((Date.now() - target) / 86400000));
}

/** 导出回执上的「在访达中显示」：复用「本地数据目录」按钮同款的 open_local_path 调用。 */
async function revealSavedFile(path: string): Promise<void> {
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("open_local_path", { path, reveal: true });
  } catch (error) {
    showToast({ message: errorMessage(error), error: true });
  }
}

function buildRolesList(preview: ModelImportPreview): HTMLUListElement {
  const list = document.createElement("ul");
  list.style.margin = "0 0 8px";
  list.style.paddingLeft = "18px";
  list.style.fontSize = "12.5px";
  if (preview.roles.length) {
    for (const item of preview.roles) {
      const li = document.createElement("li");
      // fields 是后端的设置键名（base_url、cloud_model 之类），对用户没有意义，只报数量。
      li.textContent = (MODEL_ROLE_LABELS[item.role] || item.role) + t("：") + (item.fields.length ? t("{0} 项设置", item.fields.length) : t("无具体设置"));
      list.append(li);
    }
  } else {
    const li = document.createElement("li");
    li.textContent = t("没有需要导入的模型设置。");
    list.append(li);
  }
  return list;
}

async function exportModelConfig(includeApiKey: boolean): Promise<void> {
  if (includeApiKey) {
    openExportWithKeyConfirmModal();
    return;
  }
  try {
    const response = await client.request<ModelConfigExportResponse>("/api/model-config/export");
    // 写进文件的只有 document。回执（哪条连接的密钥带走了、哪条被扣下了）只给本机
    // 用户看：混进导出文件等于额外附送一份本机连接清单给对方。
    // 成功 toast 必须等真的写完盘：文件里带着密钥，谎报「已导出」比不导出更糟。
    // 不含 Key 的文件仍是纯 JSON，扩展名照旧留 .json。换成 .xltcfg 没有收益，代价却
    // 是实打实的：9.2.4 及更早版本的导入选择器只接受 .json，收到 .xltcfg 的旧版用户
    // 在文件框里根本选不中它，得自己改名——而这条路径正是发给「可能还没升级的人」用的。
    // .xltcfg 留给含 Key 的加密文件：那种文件打开确实只有乱码，值得一个自己的扩展名。
    const saved = await saveJsonFile("translator-model-config.json", response.document);
    if (!saved) return; // 用户在保存框里取消：静默返回，不弹任何提示
    showToast({ message: t("模型配置已导出到 {0}；默认不包含 API 密钥。", saved) });
  } catch (error) {
    showToast({ message: t("导出失败：{0}", errorMessage(error)), error: true });
  }
}

/** 「导出含 Key」确认弹窗：选有效期 + 明确的传播风险警告，替换掉原来那句 window.confirm。 */
function openExportWithKeyConfirmModal(): void {
  let validDays = DEFAULT_VALID_DAYS;
  const selectField = createSelectField({
    label: t("文件有效期"),
    options: VALID_DAYS_OPTIONS.map((option) => ({ value: String(option.value), label: option.label })),
    value: String(DEFAULT_VALID_DAYS),
    onChange: (value) => {
      validDays = Number(value);
    },
  });
  const field = selectField.root;
  field.style.marginTop = "16px";

  const note = document.createElement("p");
  note.className = "note";
  note.style.marginTop = "-4px";
  note.textContent = t("过期后文件中的密钥将无法解开，但配置部分仍可导入。");

  let handle: ModalHandle;
  handle = openModal({
    tone: "tint",
    icon: "gear",
    sourceLabel: t("设置 · 模型服务 · 导出（含密钥）"),
    title: t("导出（含密钥）"),
    body: [
      t("密钥将加密后写入文件。接收方使用本软件导入时会自动解开，无需口令，也无需联网。"),
      buildInlineBanner("warn", t("任何持有本软件的人都能解开此文件。请仅发送给应持有这些密钥的人员，请勿发送至公开群组或网盘。")),
      field,
      note,
    ],
    actions: [
      { label: t("取消"), variant: "default" },
      {
        label: t("选择保存位置"), variant: "primary", keepOpen: true,
        onClick: async () => {
          try {
            const response = await client.request<ModelConfigExportResponse>(
              `/api/model-config/export?include_api_key=true&confirm_sensitive=true&valid_days=${validDays}`,
            );
            const saved = await saveJsonFile("translator-model-config.xltcfg", response.document);
            if (!saved) return; // 用户在保存框里取消：静默返回，不弹任何提示，确认弹窗留在原地
            // 弹窗切换到「导出完成」内容前，先关掉这一层——两个弹窗不叠放。
            handle.close();
            const withheld = (response.api_key_report?.connections ?? []).filter((row) => row.status === "withheld_imported");
            const withheldMemories = response.api_key_report?.provider_memories ?? [];
            // 加密之后回执还多担一件事：告诉用户这份文件哪天到期，这正是他要转告收件人
            // 的信息，一句 toast 放不下。原有的「哪些密钥被扣下」触发条件一并保留——
            // 本机的密钥全都是导入来的时，它们会被全部扣下，文件里一把密钥都不剩，
            // sealed 因此是 false，可这恰恰是最需要逐条列清楚的一次导出。
            if (response.sealed || withheld.length || withheldMemories.length) {
              openApiKeyExportReportModal(saved, response);
              return;
            }
            showToast({ message: t("模型配置已导出到 {0}；本机尚未保存 API 密钥，文件中不含任何密钥。", saved) });
          } catch (error) {
            // 不带前缀的话，写盘失败时弹出来的会是 Rust 那一层的系统错误原文，
            // 用户看不出这是「导出」这件事出的错。
            showToast({ message: t("导出失败：{0}", errorMessage(error)), error: true });
          }
        },
      },
    ],
  });
}

/** 有密钥被扣下、或密钥已加密写入时的导出回执；两种情况分享同一个「已随文件导出/没有导出的
 * 密钥」列表逻辑（这段逻辑本来就有，不动），只在顶部按是否加密加一段 kv 摘要。 */
function openApiKeyExportReportModal(savedPath: string, response: ModelConfigExportResponse): void {
  const report = response.api_key_report;
  const { sealed, expires_at: expiresAt, sealed_key_count: sealedKeyCount } = response;
  const rows = report.connections ?? [];
  const withheld = rows.filter((row) => row.status === "withheld_imported");
  const exported = rows.filter((row) => row.status === "exported");
  const memories = report.provider_memories ?? [];
  const describe = (row: ApiKeyExportRow) => `${tx(row.role_label) || row.role}${t("：")}${tx(row.connection)}`;
  const buildList = (items: ApiKeyExportRow[], describeRow: (row: ApiKeyExportRow) => string, gap: string) => {
    const list = document.createElement("ul");
    list.style.margin = `0 0 ${gap}`;
    list.style.paddingLeft = "18px";
    list.style.fontSize = "12.5px";
    for (const row of items) {
      const li = document.createElement("li");
      li.textContent = describeRow(row);
      list.append(li);
    }
    return list;
  };

  const body: (string | HTMLElement)[] = [];
  if (sealed) {
    body.push(buildKvList([
      [t("文件"), monoText(savedPath)],
      // 数的是后端密封前实际数出来的密钥处数，不是回执里的连接行数：角色「换服务商时
      // 记住的配置」里的密钥也会被一起加密带走，按连接行数报会少算，和收件人导入时
      // 看到的作用域数对不上。
      [t("密钥"), textAndChip(t("{0} 个密钥已加密写入 ", sealedKeyCount), createChip({ label: t("已加密"), tone: "ok" }))],
      [t("有效期至"), expiresAt ? t("{0}（剩余 {1} 天）", formatReleaseDate(expiresAt), daysFromNow(expiresAt)) : t("长期有效")],
    ]));
  } else {
    body.push(t("配置已导出到 {0}。", savedPath));
  }
  if (withheld.length) {
    body.push(
      t("以下 {0} 条连接的密钥未写入文件，因为这些密钥是从他人的配置文件导入本机的：", withheld.length),
      buildList(withheld, describe, "8px"),
    );
  }
  if (memories.length) {
    // 这些不是连接列表里的条目，而是角色换服务商时留下的记忆，用「服务商 + 地址」来指认。
    body.push(
      t("另有 {0} 处「换服务商时记住的配置」中的密钥同样为导入所得，也未写入文件：", memories.length),
      buildList(memories, (row) => `${tx(row.role_label) || row.role}${t("：")}${providerLabel(row.provider ?? "")}${t("（{0}）", tx(row.connection))}`, "8px"),
    );
  }
  if (withheld.length || memories.length) {
    body.push(t("此举是为避免继续传播他人的密钥。接收方导入此配置后，需自行填写上述密钥。如果这些密钥属于您，请在对应位置重新填写并保存，之后导出时将包含这些密钥。"));
  }
  if (exported.length) {
    body.push(t("已随文件导出的密钥（{0} 条连接，请将文件存放在安全位置）：", exported.length), buildList(exported, describe, "0"));
  } else if (!sealed) {
    body.push(t("本次导出的文件不含任何密钥，接收方导入后需自行填写所有连接的密钥。"));
  }

  openModal({
    tone: sealed ? "ok" : "warn",
    icon: sealed ? "check" : "warn",
    wide: true,
    sourceLabel: t("设置 · 模型服务 · 导出（含密钥）"),
    title: sealed ? t("已导出，密钥已加密") : t("部分密钥没有导出"),
    body,
    actions: [
      { label: t("在文件夹中显示"), onClick: () => void revealSavedFile(savedPath), keepOpen: true },
      { label: t("知道了"), variant: "primary" },
    ],
  });
}

function importModelConfig(): void {
  pickJsonFile(async (fileName, payload) => {
    let preview: Omit<ModelImportPreview, "fileName" | "payload">;
    try {
      preview = await client.request<Omit<ModelImportPreview, "fileName" | "payload">>(
        "/api/model-config/import/preview",
        { method: "POST", body: JSON.stringify(payload) },
      );
    } catch (error) {
      // 后端在这句原话上给的是 422：文件被篡改或损坏，AAD 校验没过。这种情况有专门的
      // 弹窗（样张界面 8），不走下面 pickJsonFile 自带的通用红色 toast。
      if ((error instanceof ApiError ? error.rawMessage : errorMessage(error)) === CORRUPT_IMPORT_DETAIL) {
        openImportCorruptModal();
        return;
      }
      throw error;
    }
    setModelImportPreview({ fileName, payload, ...preview });
    openImportPreviewModal();
  }, t("模型配置导入预览失败"), "application/json,.json,.xltcfg");
}

function openImportCorruptModal(): void {
  openModal({
    tone: "danger",
    icon: "close",
    sourceLabel: t("设置 · 模型服务 · 导入配置"),
    title: t("文件校验未通过"),
    // 第二句不是客套话：校验不过意味着文件可能是被人改过的（比如把某条连接的服务
    // 地址换成别人的服务器），「确认文件来源」是这条路径上用户唯一能做的安全动作。
    body: [tx(CORRUPT_IMPORT_DETAIL), t("如重新发送后仍然报错，请向发送方确认文件来源。")],
    actions: [{ label: t("知道了"), variant: "primary" }],
  });
}

/** 实际提交导入。四条分支路径（正常/旧版明文/过期/版本太旧）末尾都调这个，
 * 只是「仅导入配置」两条路径提交的 payload 和「确认替换并导入」是同一份——
 * 有效期只锁密钥，配置部分从不因为过期或版本不支持被拦下（决策见样张底部）。 */
async function runModelConfigImport(preview: ModelImportPreview, handle: ModalHandle): Promise<void> {
  try {
    const result = await client.request<{ imported_key_count: number }>("/api/model-config/import", {
      method: "POST", body: JSON.stringify(preview.payload),
    });
    setModelImportPreview(null);
    // 连接列表整份换掉了：旧连接的选中项、连接方式草稿和表单草稿都不再有主人。
    for (const role of MODEL_ROLE_ORDER) {
      selectConnection(role, null);
      delete modelFormDrafts[role];
    }
    await refreshSettings();
    // 导入写入密钥作用域时同样走 keys.json 的写路径，可能触发惰性自愈。
    void refreshDataHealth(mountToken);
    for (const role of Object.keys(modelRoles)) clearModelCatalog(role, t("导入后请重新获取当前连接的模型列表。"));
    renderBody();
    handle.close();
    const unsealedKeys = preview.seal_status === "unsealed" && result.imported_key_count > 0;
    const keysClause = result.imported_key_count > 0
      ? (unsealedKeys
        ? t("；同时解开并写入 {0} 个密钥", result.imported_key_count)
        : t("；同时写入 {0} 个密钥", result.imported_key_count))
      : "";
    showToast({ message: t("配置已导入。文件中所含用途的连接列表已全部替换{0}。请重新测试这些用途的连接。", keysClause) });
  } catch (error) {
    showToast({ message: errorMessage(error), error: true });
  }
}

function openImportPreviewModal(): void {
  const preview = modelImportPreview;
  if (!preview) return;
  switch (preview.seal_status) {
    case "expired":
      openImportExpiredModal(preview);
      return;
    case "unsupported":
      openImportUnsupportedModal(preview);
      return;
    default:
      // "unsealed" 和 "plaintext" 共用同一个正常预览弹窗，只是 plaintext 且确实带着
      // 密钥时（排除「不含 Key」导出被重新导入、sealed_key_count === 0 的边界情况）
      // 多加一条「明文」警告。
      openImportNormalModal(preview);
  }
}

function openImportNormalModal(preview: ModelImportPreview): void {
  const showPlaintextWarning = preview.legacy_plaintext && preview.sealed_key_count > 0;
  const kvRows: [string, string | HTMLElement][] = [
    [t("文件"), monoText(preview.fileName)],
    [t("包含"), t("{0} 个用途", preview.roles.length)],
  ];
  if (preview.sealed_key_count > 0) {
    kvRows.push([t("密钥"), textAndChip(
      t("{0} 个密钥 ", preview.sealed_key_count),
      showPlaintextWarning
        ? createChip({ label: t("明文"), tone: "warn" })
        : createChip({ label: t("已自动解开"), tone: "ok" }),
    )]);
    if (!showPlaintextWarning) {
      kvRows.push([t("有效期至"), preview.expires_at
        ? t("{0}（剩余 {1} 天）", formatReleaseDate(preview.expires_at), daysFromNow(preview.expires_at))
        : t("长期有效")]);
    }
  }

  const body: (string | HTMLElement)[] = [buildKvList(kvRows)];
  if (showPlaintextWarning) {
    body.push(buildInlineBanner("warn", t("此文件由旧版本导出，密钥以明文保存，任何人用文本编辑器打开即可看到。可正常导入，建议导入后立即删除此文件。")));
  }
  body.push(
    replacesConnectionsNotice(),
    buildRolesList(preview),
    t("导入后，这些用途将标记为「未测试」，不会自动发送任何请求。"),
  );

  let handle: ModalHandle;
  handle = openModal({
    tone: "tint",
    icon: "down",
    wide: true,
    sourceLabel: t("设置 · 模型服务 · 导入配置"),
    title: t("预览导入模型配置"),
    body,
    actions: [
      { label: t("取消"), variant: "default" },
      {
        // 按钮和 toast 都跟着正文的口径走：连接列表是整份替换的，再说「合并」会让用户
        // 以为本机自己加的连接还在，而它们连同已保存的 Key 已经没了。
        label: t("确认替换并导入"), variant: "primary", keepOpen: true,
        onClick: () => runModelConfigImport(preview, handle),
      },
    ],
  });
}

function openImportExpiredModal(preview: ModelImportPreview): void {
  const elapsedDays = preview.expires_at ? daysSince(preview.expires_at) : 0;
  let handle: ModalHandle;
  handle = openModal({
    tone: "warn",
    icon: "warn",
    wide: true,
    sourceLabel: t("设置 · 模型服务 · 导入配置"),
    title: t("此文件已过期"),
    body: [
      t("文件有效期至 {0}（{1}），其中的密钥已无法解开。", preview.expires_at ? formatReleaseDate(preview.expires_at) : t("未知"), elapsedDays > 0 ? t("已过去 {0} 天", elapsedDays) : t("刚刚过期")),
      buildKvList([
        [t("包含"), t("{0} 个用途", preview.roles.length)],
        [t("密钥"), textAndChip(t("{0} 个密钥 ", preview.sealed_key_count), createChip({ label: t("已失效，无法解开"), tone: "warn" }))],
      ]),
      buildInlineBanner("tint", t("连接和模型名称仍可正常导入，导入后需手动填写各连接的密钥。如需密钥，请让发送方重新导出。")),
      // 「仅导入配置」提交的 payload 和正常导入的完全一样，连接列表照样整份替换。
      // 不写这一句，「仅」字会让人以为影响有限，而本机自己加的连接和它们的 Key
      // 已经没了，用户只能在事后的 toast 里第一次看到这件事。
      replacesConnectionsNotice(),
    ],
    actions: [
      { label: t("取消"), variant: "default" },
      { label: t("仅导入配置（替换现有连接）"), variant: "primary", keepOpen: true, onClick: () => runModelConfigImport(preview, handle) },
    ],
  });
}

function openImportUnsupportedModal(preview: ModelImportPreview): void {
  let handle: ModalHandle;
  handle = openModal({
    tone: "warn",
    icon: "restart",
    wide: true,
    sourceLabel: t("设置 · 模型服务 · 导入配置"),
    title: t("需要更新软件才能解开密钥"),
    body: [
      t("此文件由更高版本的 Translator 导出，当前版本（{0}）无法解开其中的密钥。", PACKAGE_VERSION),
      buildInlineBanner("tint", t("请更新到最新版本后重新导入，或先仅导入配置，之后手动填写密钥。")),
      replacesConnectionsNotice(), // 同过期弹窗：「仅」字不代表影响有限，见那边的注释
    ],
    actions: [
      { label: t("仅导入配置（替换现有连接）"), keepOpen: true, onClick: () => runModelConfigImport(preview, handle) },
      { label: t("前往更新"), variant: "primary", onClick: () => navigate("settings", { page: "about" }) },
    ],
  });
}

export async function exportDocumentConfig(): Promise<void> {
  try {
    const payload = await client.request<JsonObject>("/api/document-config/export");
    const saved = await saveJsonFile("translator-document-config.json", payload);
    if (!saved) return; // 用户在保存框里取消：静默返回
    showToast({ message: t("已导出文档翻译配置到 {0}；不含模型、密钥和本机输出目录。", saved) });
  } catch (error) {
    showToast({ message: t("导出失败：{0}", errorMessage(error)), error: true });
  }
}

export function importDocumentConfig(): void {
  pickJsonFile(async (fileName, payload) => {
    const preview = await client.request<{ areas: string[]; app_version: string }>(
      "/api/document-config/import/preview",
      { method: "POST", body: JSON.stringify(payload) },
    );
    openDocumentImportPreviewModal(fileName, payload, preview.areas);
  }, t("文档翻译配置导入预览失败"));
}

export function openDocumentImportPreviewModal(fileName: string, payload: JsonObject, areas: string[]): void {
  const list = document.createElement("ul");
  list.style.margin = "0 0 8px";
  list.style.paddingLeft = "18px";
  list.style.fontSize = "12.5px";
  for (const area of areas.length ? areas : [t("没有可识别的配置变更。")]) {
    const li = document.createElement("li");
    li.textContent = tx(area);
    list.append(li);
  }
  let handle: ModalHandle;
  handle = openModal({
    tone: "warn",
    icon: "gear",
    sourceLabel: t("设置 · 翻译参数 · 导入配置"),
    title: t("预览导入文档翻译配置"),
    body: [
      t("{0} · 仅覆盖文件中列出的设置项，其余设置保持不变。", fileName),
      list,
      t("此配置包不含模型、密钥和本机输出目录，导入后这些内容保持不变。"),
    ],
    actions: [
      { label: t("取消"), variant: "default" },
      {
        label: t("确认导入"), variant: "primary", keepOpen: true,
        onClick: async () => {
          try {
            await client.request("/api/document-config/import", { method: "POST", body: JSON.stringify(payload) });
            await refreshSettings();
            renderBody();
            handle.close();
            showToast({ message: t("文档翻译配置已导入。") });
          } catch (error) {
            showToast({ message: errorMessage(error), error: true });
          }
        },
      },
    ],
  });
}

export function pickJsonFile(
  onPicked: (fileName: string, payload: JsonObject) => Promise<void>,
  failureLabel: string,
  accept = "application/json,.json",
): void {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = accept;
  input.onchange = async () => {
    const file = input.files?.[0];
    if (!file) return;
    try {
      await onPicked(file.name, JSON.parse(await file.text()) as JsonObject);
    } catch (error) {
      showToast({ message: `${failureLabel}${t("：")}${errorMessage(error)}`, error: true });
    }
  };
  input.click();
}

/**
 * 诊断包导出。落盘、取消、报错三种结局都在这里说清楚：
 * 只有真的写进磁盘才弹成功 toast，用户按取消就什么都不弹。
 *
 * 原来这里另有一份自己 invoke `sidecar_info` 的 `fetchWithToken` + `downloadBinary`，
 * 和 ApiClient 里的实现逐行重复；现在统一走 client.saveBinaryDownload()。
 */
export async function exportDiagnostics(path: string, fallbackFilename: string): Promise<void> {
  try {
    await ensureConnected();
    const saved = await client.saveBinaryDownload(path, fallbackFilename);
    if (!saved) return; // 用户取消：静默
    showToast({ message: t("诊断包已保存到：{0}", saved) });
  } catch (error) {
    showToast({ message: t("导出诊断失败：{0}", errorMessage(error)), error: true });
  }
}
