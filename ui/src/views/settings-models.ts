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
  createCard,
  createChip,
  createButton,
  createSwitchRow,
  createSelectField,
  createEmptyState,
  openModal,
  showToast,
  type ModalHandle,
} from "../components";
import { icon } from "../icons";
import { saveJsonFile } from "../save-file";
import { setModelPillSettingsRole } from "../model-pill";
import {
  type ApiKeyExportRow,
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
  num,
  numberField,
  persistSettings,
  type PoolConnection,
  providerBaseUrlDefaults,
  providerBaseUrlDisabled,
  providerLabel,
  providerModelDefaults,
  queueModelFormSave,
  record,
  refreshDataHealth,
  refreshModelRoles,
  refreshSettings,
  renderBody,
  reRenderAfter,
  restoreModelFormDraft,
  sectionLabel,
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
  tcHead,
  text,
  textField,
} from "./settings";
import { version as PACKAGE_VERSION } from "../../package.json";

// ---------------------------------------------------------------------------
// 子页①：模型服务
// ---------------------------------------------------------------------------

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

function clearModelCatalog(role: string, message = "尚未获取当前连接的模型列表。"): void {
  delete modelCatalog[role];
  delete modelCatalogConnection[role];
  modelCatalogMessage[role] = message;
}

export function renderModelsPage(host: HTMLElement): void {
  // 这张卡导出的是整套模型服务，不属于下面任何一个角色或连接，放在角色切换之前。
  host.append(bundleCard({
    title: "模型服务配置导入与导出",
    description: "备份或分享整套模型配置，供他人一键导入。含密钥的文件会加密，默认 30 天有效；从别人配置导入的密钥不会再次转出。",
    buttons: [
      createButton({ label: "导出（不含 Key）", size: "mini", onClick: () => void exportModelConfig(false) }),
      createButton({ label: "导出含 Key", size: "mini", onClick: () => void exportModelConfig(true) }),
      createButton({ label: "导入配置", size: "mini", onClick: () => importModelConfig() }),
    ],
  }));

  // 并发提醒紧跟模型配置导入导出，和它描述的全局连接行为保持同一上下文。
  const spreadCard = createCard([]);
  const spreadBody = document.createElement("div");
  spreadBody.style.padding = "14px 16px";
  spreadBody.append(sectionLabel("多个任务同时运行"));
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
  spreadNote.textContent = "几个任务同时用同一条连接时，请求会叠在一起，可能被服务商限速，费用也会增加。"
  spreadBody.append(spreadNote);
  spreadCard.append(spreadBody);
  host.append(spreadCard);

  // 角色切换（4 张卡）
  let seg = buildRoleSeg();
  host.append(seg);

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

  // ---- 连接列表卡 ----
  let listCard = buildConnectionListCard(role);
  host.append(listCard);

  // ---- 连接详情卡 ----
  const detailCard = createCard([]);
  const detailBody = document.createElement("div");
  detailBody.style.padding = "14px 16px";
  detailBody.style.display = "flex";
  detailBody.style.flexDirection = "column";
  detailBody.style.gap = "10px";

  // 连接方式属于当前选中的这一条连接；切换另一行不会改变它。
  const accessBar = document.createElement("div");
  accessBar.className = "accessbar";
  const accessLabel = document.createElement("span");
  accessLabel.className = "accessbar-label";
  accessLabel.textContent = "当前连接方式";
  accessLabel.append(hintBadge("选「跟随」时，直接用另一个用途主用连接的服务商、地址和密钥，只需要单独选模型。"));
  accessBar.append(accessLabel);
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
  accessBar.append(radioRow);
  const accessDirtyChip = createChip({ label: "未保存", tone: "warn" });
  if (accessDirty()) accessBar.append(accessDirtyChip);
  detailBody.append(accessBar);

  const grid = document.createElement("div");
  grid.className = "grid2";

  // onChange 需要拿到下面才创建的 baseUrlField，用可变引用打个转 —— select 的 change
  // 事件只会在用户真的切换了选项时触发一次，天然满足「只在切换动作发生时重填，不覆盖
  // 用户已手填内容」的要求（初次渲染、其他按钮触发的重画都不会 fire change）。
  let onProviderChange: (value: string) => void = () => {};
  setApplyProviderDerivedState(() => {});
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
  // 只改 Base URL 的可填性和占位符，不动它的值——草稿回填时要按服务商重放这段联动，
  // 但绝不能把用户刚敲的 Base URL 冲掉。
  setApplyProviderDerivedState((value) => {
    if (!cloudMode) return;
    if (providerBaseUrlDisabled.has(value)) {
      baseUrlField.input.disabled = true;
      baseUrlField.input.placeholder = disabledBaseUrlPlaceholder;
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
    // 模型名称跟着服务商一起重填：A 家的模型 ID 拿到 B 家去必定报错，留着它比清掉更坏。
    // 没有预设的服务商（OpenAI 兼容、各家自建网关）保持原样，不拿空字符串去洗掉用户填的值。
    const recommended = providerModelDefaults[value];
    if (recommended) modelNameField.input.value = recommended;
  };

  const modelNameField = textField("模型名称", formModel, () => undefined, { placeholder: cloudMode ? "输入模型 ID" : "例如 qwen2.5:7b", hint: "右侧箭头可以展开候选；列表里没有的模型也可以手动填写。" });
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
  attachModelNameDropdown(modelNameField.input, currentCatalog, () => providerSelectEl.value);
  // 服务商和模型名称并排；Base URL 长，独占一行摆在它们下面。三者都要等 modelNameField
  // 建好之后再挂进 DOM，因为 onProviderChange 的闭包引用它。
  grid.append(modelNameField.root);
  detailBody.append(grid);
  detailBody.append(baseUrlField.root);

  let apiModeSelect: HTMLSelectElement | null = null;
  if (cloudMode) {
    const apiModeField = selectField(
      "文本请求协议",
      [
        { value: "auto", label: "自动识别" },
        { value: "chat", label: "Chat Completions" },
        { value: "responses", label: "Responses" },
      ],
      text(following ? followConnection?.api_mode || selected?.api_mode : selected?.connection_mode === "cloud" ? selected.api_mode : selected?.own_cloud_api_mode || (selected ? "auto" : rolePayload.api_mode), "auto"),
      () => undefined,
      { disabled: following, hint: "不确定就选「自动识别」；只有服务商明确要求时才手动指定。" },
    );
    apiModeSelect = apiModeField.select;
    detailBody.append(apiModeField.root);
  }

  const labelField = textField("连接名称", selected?.label ?? "", () => undefined, { placeholder: "例如 主账号 / 备用厂商" });
  const connectionLabelField = labelField.input;
  connectionLabelField.id = "settings-model-connection-label";
  detailBody.append(labelField.root);

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
    const keyField = fieldWithHint("API 密钥", keyInput, "留空表示沿用已保存的密钥；密钥只写入本机密钥存储，不随配置导出（除非选择“导出含 Key”）。");
    detailBody.append(keyField);
  }

  // 速率设置。读写的是**当前选中那条连接**自己的速率（后端按连接 + 模型分档记忆）。
  // 以前这里读的是角色级 throughput，也就是主用连接那一份：选中第二条连接改并发，
  // 改的其实是主用连接，第二条连接自己的速率在界面上根本看不到。
  const connectionThroughput = (connection: PoolConnection | null) => ({
    values: record(connection?.throughput ?? rolePayload.throughput),
    bounds: record(connection?.throughput_bounds ?? rolePayload.throughput_bounds),
  });
  const initialThroughput = connectionThroughput(selected);
  const throughput = initialThroughput.values;
  const bounds = initialThroughput.bounds;
  const batchBounds = Array.isArray(bounds.batch_size) ? (bounds.batch_size as unknown[]) : [];
  const concurrencyBounds = Array.isArray(bounds.concurrency) ? (bounds.concurrency as unknown[]) : [];
  const throughputUnlocked = Boolean(record(settings).engine && record(record(settings).engine).concurrency_unlocked);
  const throughputGrid = document.createElement("div");
  throughputGrid.className = "grid2";
  let batchInput: HTMLInputElement | null = null;
  const needsBatch = role === "translation" || role === "cleaner";
  if (needsBatch) {
    const batchField = numberField("每批段数", num(throughput.batch_size, 8), () => undefined, {
      hint: "一次发给模型几段文字。调大更快，但一批出错要重试的也更多。",
      min: 1, max: throughputUnlocked ? undefined : num(batchBounds[1], 128), integer: true,
    });
    batchInput = batchField.querySelector("input");
    throughputGrid.append(batchField);
  }
  const concurrencyField = numberField("同时请求数", num(throughput.concurrency, 1), () => undefined, {
    hint: "同时向模型发几个请求。调大更快，但容易被服务商限速。",
    min: 1, max: throughputUnlocked ? undefined : num(concurrencyBounds[1], 32), type: "text", integer: true,
  });
  const concurrencyInput = concurrencyField.querySelector("input") as HTMLInputElement;
  if (role === "translation" && !throughputUnlocked) {
    concurrencyInput.addEventListener("input", () => {
      const nextType = /^\d*$/.test(concurrencyInput.value) ? "text" : "password";
      if (concurrencyInput.type !== nextType) concurrencyInput.type = nextType;
    });
    concurrencyInput.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || concurrencyInput.value.trim().toUpperCase() !== "OA") return;
      event.preventDefault();
      concurrencyInput.value = String(throughput.concurrency || 1);
      concurrencyInput.type = "text";
      void (async () => {
        try {
          // 解锁前先把排队中的失焦保存跑完：解锁后的重画读的是服务端状态，
          // 还在飞的那笔保存落盘晚于重画，界面就会停在旧值上。
          await queueModelFormSave(async () => {
            await client.request("/api/models/throughput/unlock", {
              method: "POST", body: JSON.stringify({ code: "OA" }),
            });
          });
          await refreshSettings();
          renderBody();
        } catch {
          // Hidden code entry has no separate feedback surface.
        }
      })();
    });
  }
  throughputGrid.append(concurrencyField);
  detailBody.append(sectionLabel("速率设置"));
  // 速率原来有自己的「保存速率」按钮，是这张卡上唯一还要手动点一下才生效的一块。
  // 速率本来就属于模型配置的一部分，现在和同卡其余字段一样失焦即保存（见下面的
  // autoSaveThroughput）。按钮消失后，原来那句 toast 里的「运行中的任务不受影响」
  // 没地方说了，改挂成这行常驻说明——它回答的是用户改速率时最担心的那件事。
  const throughputNote = document.createElement("p");
  throughputNote.className = "note";
  throughputNote.style.margin = "0";
  throughputNote.style.fontSize = "12px";
  throughputNote.style.color = "var(--ink-3)";
  // 摆在两个输入框**下面**，和「并发提醒」里 spreadNote 相对开关的位置是同一套：
  // 夹在小标题和输入框中间会把这一组撑开，看着像是两块无关的东西。
  detailBody.append(throughputGrid);
  detailBody.append(throughputNote);
  const resetThroughputButton = createButton({ label: "恢复推荐值", size: "mini", onClick: () => void reRenderAfter(async () => {
    await client.request<JsonObject>(
      `/api/models/throughput/${encodeURIComponent(role)}${connectionQuery(activeConnection(role))}`,
      { method: "DELETE" },
    );
    await refreshModelRoles();
    showToast({ message: "已恢复推荐速率。运行中的任务不受影响。" });
  }) });
  detailBody.append(fieldRow([
    // 「恢复推荐值」留着：它是重置动作（DELETE 路由），失焦语义表达不了「把我存的那份
    // 丢掉、退回推荐值」。它照旧走 reRenderAfter 重画——重置本来就要把两个框的值整个换掉。
    resetThroughputButton,
  ]));
  // 速率的上限、说明和可编辑状态都跟着「这条连接现在存的是什么」走。保存了别的字段
  // （换模型、切本地）之后要重算一次，见下面的 syncAfterSave。
  const paintThroughput = (connection: PoolConnection | null) => {
    const { values, bounds: nextBounds } = connectionThroughput(connection);
    const concurrencyMax = Array.isArray(nextBounds.concurrency) ? num((nextBounds.concurrency as unknown[])[1], 0) : 0;
    const batchMax = Array.isArray(nextBounds.batch_size) ? num((nextBounds.batch_size as unknown[])[1], 0) : 0;
    const pending = accessDirty();
    for (const [input, value, max] of [
      [concurrencyInput, values.concurrency, concurrencyMax],
      [batchInput, values.batch_size, batchMax],
    ] as [HTMLInputElement | null, unknown, number][]) {
      if (!input) continue;
      if (throughputUnlocked || !max) input.removeAttribute("max");
      else input.max = String(max);
      // 速率属于已保存的那种连接方式：切到本地但还没保存时改并发，会存进云端那一档，
      // 范围也是云端的。先锁住，保存连接方式后再放开。
      input.disabled = pending;
      if (document.activeElement !== input && typeof value === "number") input.value = String(value);
    }
    resetThroughputButton.disabled = pending;
    const range = throughputUnlocked || !concurrencyMax ? "" : `并发上限 ${concurrencyMax}。`;
    throughputNote.textContent = pending
      ? "连接方式还没保存。点「保存配置」后再调整这条连接的速率。"
      : `每个模型分别记忆；换到没调过的模型时沿用当前数值。${range}改动即时保存，运行中的任务不受影响。`;
  };
  paintThroughput(selected);

  // 状态胶囊。取的是**当前选中那条连接**的测试结果，不是角色级的那份——角色级状态
  // 是主用连接的镜像（core/model_roles.py 里照抄 primary 的三个字段），拿它当详情卡的
  // 状态，新建的第二条连接一挂上来就顶着主用连接的「测试通过」，用户会以为它已经验过。
  // 现在列表始终是本角色拥有的连接，每行都有独立测试结论。
  //
  // 整块做成可重画的：失焦自动保存不重画整页（会抢焦点），但换了模型或密钥之后测试
  // 结论已经被后端打回「未测试」，这里还挂着「测试通过」就是在报告没发生的事。
  const statusRow = document.createElement("div");
  statusRow.style.display = "flex";
  statusRow.style.gap = "8px";
  statusRow.style.alignItems = "center";
  statusRow.style.flexWrap = "wrap";
  const autoSaveNote = document.createElement("span");
  autoSaveNote.style.fontSize = "12px";
  autoSaveNote.style.color = "var(--ink-3)";
  const statusReasonNote = document.createElement("div");
  statusReasonNote.style.fontSize = "12px";
  statusReasonNote.style.lineHeight = "1.5";
  statusReasonNote.style.marginTop = "6px";
  detailBody.append(statusRow, statusReasonNote);
  const paintStatus = (connection: PoolConnection | null) => {
    const availabilitySource: JsonObject = connection
      ? { availability_status: connection.availability_status, availability_message: connection.availability_message, availability_checked_at: connection.availability_checked_at }
      : rolePayload;
    const availability = text(availabilitySource.availability_status, "unknown");
    const availabilityTone = availability === "available" ? "done" : availability === "unavailable" ? "error" : "";
    const availabilityLabel = availability === "available" ? "测试通过" : availability === "unavailable" ? "测试失败" : "未测试";
    const checkedAt = formatCheckedAt(text(availabilitySource.availability_checked_at));
    const matches = catalogStillMatches();
    const list = currentCatalog();
    const catalogChip = statusChip(list.length ? `${list.length} 个可用模型` : "未获取列表", list.length ? "done" : "");
    catalogChip.title = matches ? (modelCatalogMessage[role] || "尚未获取当前连接的模型列表。") : "当前连接尚未获取模型列表。保存配置后可手动获取。";
    const availChip = statusChip(availabilityLabel, availabilityTone);
    availChip.title = `${text(availabilitySource.availability_message, "当前配置尚未测试。")}${checkedAt ? ` · ${checkedAt}` : ""}`;
    statusRow.replaceChildren(catalogChip, availChip, autoSaveNote);

    // 失败原因必须看得见。之前它只挂在胶囊的 title 上：不悬停就永远读不到，用户面对一个
    // 红色的「测试失败」没有任何线索（是密钥错、地址错，还是对方在维护），只能反复点
    // 「测试连接」。「未获取列表」同样——光看这四个字看不出下一步该点哪里。
    // 胶囊上已经写了「测试失败」，原因这里不再重复一遍；只有失败原因那一句和胶囊同用琥珀色，
    // 后面「还没获取模型列表」这类普通说明保持灰色。旧版本存下的原因还带着
    // 「XX连接测试失败：」前缀，显示前去掉。
    const statusParts: HTMLElement[] = [];
    if (availability === "unavailable") {
      const reason = text(availabilitySource.availability_message, "上一次测试没有留下原因，请再测一次。")
        .replace(/^[^：]{0,16}连接测试失败：/, "");
      const failure = document.createElement("span");
      failure.style.color = "var(--warn)";
      failure.textContent = `${reason}${checkedAt ? `（${checkedAt}）` : ""}`;
      statusParts.push(failure);
    }
    if (!list.length) {
      const note = document.createElement("span");
      note.textContent = matches
        ? modelCatalogMessage[role] || "尚未获取当前连接的模型列表，可点「获取模型」拉取。"
        : "当前连接尚未获取模型列表。保存配置后可点「获取模型」拉取。";
      statusParts.push(note);
    }
    statusReasonNote.hidden = !statusParts.length;
    statusReasonNote.style.color = "var(--ink-3)";
    statusReasonNote.replaceChildren(
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
      if (!accessDirtyChip.isConnected) accessBar.append(accessDirtyChip);
    } else {
      accessDirtyChip.remove();
    }
    paintStatus(fresh);
    paintThroughput(fresh);
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
    const form = readModelForm();
    void queueModelFormSave(async () => {
      try {
        await saveModel(form, { silent: true });
        syncAfterSave(form.apiKey);
        autoSaveNote.textContent = `已自动保存 · ${new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`;
      } catch (error) {
        autoSaveNote.textContent = "";
        showToast({ message: `自动保存失败：${errorMessage(error)}`, error: true });
      }
    });
  };
  for (const field of [providerSelectEl, baseUrlField.input, modelNameField.input, connectionLabelField, apiKeyField, apiModeSelect]) {
    field?.addEventListener("change", autoSaveOnBlur);
  }

  // 速率两个框也失焦即保存。写在这里而不是上面那一段，只是因为 autoSaveNote 要到这一行
  // 才存在——两处共用同一行「已自动保存 · HH:MM」，用户不必分辨「哪个字段属于哪个提示」。
  //
  // 三件事是刻意的：
  // 1) 和上面共用 queueModelFormSave 这一条队列，不另起一条。「点『测试连接』会先触发
  //    blur 再触发 click」那条时序（见 ensureFormSavedBeforeCatalog）靠的就是所有提交
  //    排在同一条链上；吞吐另开一条队列，两个 PUT 就会并行发出去，抢的还是同一份 settings。
  // 2) 吞吐走的是独立路由 PUT /api/models/throughput/{role}，不能塞进 saveModel 的 payload
  //    ——后端是分开的两条路由（api/app.py::put_throughput 与 put_role）。
  // 3) 不重画。理由和上面那段一样：重画会把焦点从用户正要去的下一个字段上抢走
  //    （典型路径就是「改完批次大小按 Tab 去改并发数」）。
  const writeBackThroughput = (input: HTMLInputElement, submitted: number, value: unknown) => {
    // 回填后端返回的值。number input 的 min/max 只是浏览器给的提示，用户手打 999 照样提交
    // 得出去，后端 core/model_throughput.py::set_model_throughput 会按 bounds 夹紧（clamp）
    // 到 32 再存。不写回来，界面就停在 999、实际存的是 32——正是「界面报告没发生的事」
    // （提交 cd97a4f 修的那一类）。
    if (typeof value !== "number" || Number.isNaN(value)) return;
    // 焦点还在这个框里、而且值已经和这次提交出去的不一样了 → 用户在等响应的这段时间里
    // 又改了，写回去等于抢他正在敲的输入，不动。
    // 焦点在但值没动（典型：在框里直接按回车触发 change，焦点根本没离开）仍然要回填，
    // 否则最需要看到夹紧结果的那条路径反而看不到。
    if (document.activeElement === input && Number(input.value) !== submitted) return;
    input.value = String(value);
  };
  const autoSaveThroughput = (event: Event) => {
    const field = event.currentTarget as HTMLInputElement;
    if (field.disabled) return;
    const rawConcurrency = concurrencyInput.value.trim();
    const rawBatch = batchInput?.value.trim() ?? "";
    if (!/^\d+$/.test(rawConcurrency) || (needsBatch && !/^\d+$/.test(rawBatch))) return;
    void queueModelFormSave(async () => {
      try {
        const sentConcurrency = Number(concurrencyInput.value || "1");
        const sentBatch = Number(batchInput?.value || "8");
        const payload: JsonObject = { concurrency: sentConcurrency };
        if (needsBatch && batchInput) payload.batch_size = sentBatch;
        const saved = await client.request<JsonObject>(`/api/models/throughput/${encodeURIComponent(role)}${connectionQuery(selected)}`, {
          method: "PUT", body: JSON.stringify(payload),
        });
        storeConnectionThroughput(role, selected?.id ?? "", saved);
        writeBackThroughput(concurrencyInput, sentConcurrency, saved.concurrency);
        if (needsBatch && batchInput) writeBackThroughput(batchInput, sentBatch, saved.batch_size);
        autoSaveNote.textContent = `已自动保存 · ${new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`;
      } catch (error) {
        autoSaveNote.textContent = "";
        showToast({ message: `自动保存失败：${errorMessage(error)}`, error: true });
      }
    });
  };
  for (const field of [batchInput, concurrencyInput]) {
    field?.addEventListener("change", autoSaveThroughput);
  }

  const doSaveModel = () => {
    const form = readModelForm();
    void reRenderAfter(
      () => queueModelFormSave(() => saveModel(form)),
      { preserveDraft: false },
    );
  };
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
      // 测的必须是面板正在显示的这条连接：不带 id 时后端一律拨主连接。
      const result = await client.request<{ ok: boolean; message: string }>(
        `/api/models/connectivity/${encodeURIComponent(role)}`,
        { method: "POST", body: JSON.stringify({ connection_id: selected?.id ?? "" }) },
      );
      showToast({ message: result.message, error: !result.ok });
      await refreshModelRoles();
    }, { preserveDraft: false }) }),
  ]));

  detailCard.append(detailBody);
  host.append(detailCard);

}

function apiKeyPlaceholder(connection: PoolConnection | null, following: boolean, preview: string): string {
  // 跟随行的 has_api_key 来自来源连接。草稿刚从跟随切到云端时，不能把来源的
  // 掩码冒充为当前行已存的独立密钥。
  if (following) return "跟随时使用来源连接的密钥";
  return connection?.connection_mode === "cloud" && connection.has_api_key
    ? `${preview || "••••••"}（已保存 · 留空则不改）`
    : "粘贴该连接的 API 密钥";
}

// 速率路由不带 connection_id 时一律落到主用连接。
function connectionQuery(connection: PoolConnection | null): string {
  return connection?.id ? `?connection_id=${encodeURIComponent(connection.id)}` : "";
}

// PUT 速率不回整份角色数据；把结果写回那条连接，下一次重画才不会显示保存前的旧值。
function storeConnectionThroughput(role: string, connectionId: string, saved: JsonObject): void {
  const connection = roleConnections(role).find((item) => item.id === connectionId);
  if (!connection) return;
  connection.throughput = {
    profile_key: text(saved.profile_key),
    batch_size: typeof saved.batch_size === "number" ? saved.batch_size : null,
    concurrency: num(saved.concurrency, 1),
  };
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
  seg.className = "seg seg-roles";
  for (const role of MODEL_ROLE_ORDER) {
    const payload = record(modelRoles[role]);
    const segc = document.createElement("div");
    segc.className = role === modelRole ? "segc on" : "segc";
    const b = document.createElement("b");
    b.textContent = MODEL_ROLE_LABELS[role] || role;
    // 窄窗口下四张卡挤在一行，标题只占一行、放不下就省略，完整名字悬停可见。
    b.title = b.textContent;
    const primary = roleConnections(role)[0];
    const primarySource = primary?.source_role || text(payload.source_role, "independent");
    const following = primary?.connection_mode === "follow";
    const modelName = primary ? primary.model : text(payload.model);
    const modeSummary = following
      ? `跟随${MODEL_ROLE_LABELS[primarySource] || primarySource}`
      : modelName || (primary?.connection_mode === "local" ? "本地模型" : "云端 API");
    const span = document.createElement("span");
    span.className = "segc-sub";
    const summary = document.createElement("i");
    summary.textContent = modeSummary || "未配置";
    span.append(summary);
    // 有连接但模型名还空着：跟随行尤其容易漏——卡片写着「跟随…」看起来已经配好，
    // 真用到这个用途时才报错。在卡片上直接标出来；摘要太长时省略的是摘要，不是这个标记。
    if (primary && !modelName) {
      const missing = document.createElement("em");
      missing.textContent = "缺模型名";
      span.append(missing);
    }
    span.title = [summary.textContent, primary && !modelName ? "缺模型名" : ""].filter(Boolean).join(" · ");
    segc.append(b, span);
    segc.addEventListener("click", () => {
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
    });
    seg.append(segc);
  }
  return seg;
}

function buildConnectionListCard(role: string): HTMLElement {
  const selected = activeConnection(role);
  const connections = roleConnections(role);
  const addBtn = createButton({
    label: "新增连接", size: "mini", icon: "plus",
    onClick: () => void reRenderAfter(async () => {
      const payload = await client.request<JsonObject>(
        `/api/models/roles/${encodeURIComponent(role)}/connections`,
        { method: "POST", body: JSON.stringify({ label: "", connection_mode: "cloud", base_url: "" }) },
      );
      modelRoles[role] = payload;
      const added = Array.isArray(payload.connections) ? payload.connections : [];
      if (added.length) selectConnection(role, text(record(added[added.length - 1]).id));
      showToast({ message: "已新增一条连接，填好 Base URL、模型和密钥后点“保存配置”。" });
    }, { preserveDraft: false }), // 表单切到了新连接的空白字段，不该把旧连接的草稿糊上去
  });
  const listCard = createCard([tcHead("连接列表", [addBtn])]);
  if (!connections.length) {
    listCard.append(createEmptyState({ title: "还没有连接", description: "点右上角“新增连接”开始配置。", icon: "gear" }));
    return listCard;
  }
  for (const [index, connection] of connections.entries()) {
    const tone = connection.availability_status === "available"
      ? "ok" : connection.availability_status === "unavailable" ? "warn" : "mute";
    const dot = document.createElement("span");
    dot.className = "cdot";
    // 测试失败用琥珀色，和详情卡里「测试失败」胶囊、失败原因同一个颜色。
    dot.style.background = tone === "ok" ? "var(--ok)" : tone === "warn" ? "var(--warn)" : "var(--ink-3)";
    const name = document.createElement("span");
    name.style.flex = "1";
    name.style.fontWeight = "600";
    name.textContent = connection.display_label || `连接 ${index + 1}`;
    const chips: HTMLElement[] = [];
    if (index === 0) chips.push(createChip({ label: "主用", tone: "tint" }));
    // 没起名的跟随行，名称本身就是「跟随…」（见 saveModel），再挂一个同样文字的胶囊是重复。
    const followChip = `跟随${MODEL_ROLE_LABELS[connection.source_role || ""] || connection.source_role || "其他用途"}`;
    if (connection.connection_mode === "follow" && name.textContent !== followChip) chips.push(createChip({ label: followChip, tone: "tint" }));
    if (connection.connection_mode === "local") chips.push(createChip({ label: "本地", tone: "mute" }));
    if (!connection.has_api_key && !LOCAL_PROVIDERS.includes(connection.provider)) {
      chips.push(createChip({ label: "无密钥", tone: "warn" }));
    }
    const actions = document.createElement("span");
    actions.style.display = "flex";
    actions.style.gap = "6px";
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
        onClick: (e) => { e.stopPropagation(); confirmDeleteConnection(role, connection); },
      }));
    }
    const row = connRow([dot, name, ...chips, actions], {
      selected: Boolean(selected && selected.id === connection.id),
      onClick: () => {
        selectConnection(role, connection.id);
        renderBody();
      },
    });
    listCard.append(row);
  }
  return listCard;
}

export function bundleCard(opts: { title: string; description: string; buttons: HTMLElement[] }): HTMLElement {
  const card = createCard([tcHead(opts.title, opts.buttons)]);
  const note = document.createElement("p");
  note.className = "note";
  note.style.margin = "0";
  note.style.padding = "0 16px 14px";
  note.style.fontSize = "12px";
  note.style.color = "var(--ink-3)";
  // 模型服务打包卡片的说明分三段（\n 分隔），其余调用方都是单行，pre-line 对它们是无操作。
  note.style.whiteSpace = "pre-line";
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
  if (!form.selectedId) throw new Error("请先新增或选择一条连接。");
  const following = form.sourceRole !== "independent";
  const connectionMode = following ? "follow" : form.access;
  const body: JsonObject = {
    connection_mode: connectionMode,
    source_role: form.sourceRole,
    label: form.connectionLabel || (following ? `跟随${MODEL_ROLE_LABELS[form.sourceRole] || form.sourceRole}` : ""),
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
  modelRoles[role] = await client.request<JsonObject>(
    `/api/models/roles/${encodeURIComponent(role)}/connections/${encodeURIComponent(form.selectedId)}`,
    { method: "PUT", body: JSON.stringify(body) },
  );
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
  if (!opts.silent) showToast({ message: "连接已保存。密钥仅写入本机密钥存储。" });
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
    selectConnection(role, null);
    // 删掉的可能是主用连接，跟随它的角色、顶栏模型药丸和翻译角色的旧字段都跟着变。
    await refreshSettings();
    clearModelCatalog(role, "连接已变更，请重新获取模型列表。");
    // 删除连接会经由 save_connection_key 写 keys.json，同样可能触发惰性自愈。
    void refreshDataHealth(mountToken);
    showToast({ message: "连接已删除，其密钥也已从本机移除。" });
  }, { preserveDraft: false }); // 表单会切回默认连接，不该把被删连接的草稿糊上去
}

function confirmDeleteConnection(role: string, connection: PoolConnection): void {
  const name = connection.display_label || connection.label || "这条连接";
  openModal({
    tone: "danger", icon: "trash",
    sourceLabel: `设置 · 模型服务 · ${MODEL_ROLE_LABELS[role] || role}`,
    title: `删除「${name}」？`,
    body: [
      `这只会移除当前用途中的「${name}」，不会修改它跟随的来源连接。`,
      connection.primary ? "删除主用连接后，列表中的下一条连接会成为主用。" : "其他连接不受影响。",
    ],
    actions: [
      { label: "取消" },
      { label: "确认删除", variant: "danger-solid", onClick: () => void deleteConnection(role, connection.id) },
    ],
  });
}

// 后端 core/config_crypto.py 在文件被改过一个字节（AAD 绑定了到期日和正文哈希）时
// 原样抛出的这句话。要求原文照抄，不能换成界面自己的措辞。
const CORRUPT_IMPORT_DETAIL = "这份文件在传输过程中损坏，或者被修改过，没有导入任何内容。请让发送方重新发一次。";

// 三条导入路径（正常 / 过期 / 版本太旧）提交的 payload 是同一份，连接列表一律整份
// 替换。别再写「不删除未提及配置」：本机在这些角色下自己加的连接会连同它们保存的
// Key 一起消失，说成「只合并」等于骗人。过期与版本太旧那两条路径的按钮写着「仅导入
// 配置」，更需要这句话——「仅」字很容易被读成「影响有限」。
const REPLACES_CONNECTIONS_NOTICE =
  "文件里包含的用途，其连接列表会整份替换本机现有连接（本机自己加的连接和保存的密钥会被清除）；文件里没有的用途保持不变。";

// 有效期选项与 core/config_crypto.py::DEFAULT_VALID_DAYS 对齐：0 = 长期有效。
const VALID_DAYS_OPTIONS: { label: string; value: number }[] = [
  { label: "7 天", value: 7 },
  { label: "30 天（推荐）", value: 30 },
  { label: "90 天", value: 90 },
  { label: "长期有效", value: 0 },
];
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
      li.textContent = `${MODEL_ROLE_LABELS[item.role] || item.role}：${item.fields.length ? `${item.fields.length} 项设置` : "没有具体设置"}`;
      list.append(li);
    }
  } else {
    const li = document.createElement("li");
    li.textContent = "没有需要导入的模型设置。";
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
    showToast({ message: `已导出模型配置到 ${saved}；默认不包含 API Key。` });
  } catch (error) {
    showToast({ message: `导出失败：${errorMessage(error)}`, error: true });
  }
}

/** 「导出含 Key」确认弹窗：选有效期 + 明确的传播风险警告，替换掉原来那句 window.confirm。 */
function openExportWithKeyConfirmModal(): void {
  let validDays = DEFAULT_VALID_DAYS;
  const selectField = createSelectField({
    label: "文件有效期",
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
  note.textContent = "过期后文件里的密钥无法再解开，配置部分仍可导入。";

  let handle: ModalHandle;
  handle = openModal({
    tone: "tint",
    icon: "gear",
    sourceLabel: "设置 · 模型服务 · 导出含 Key",
    title: "导出含 Key",
    body: [
      "密钥会加密后写进文件。对方用本软件导入即可自动解开，不需要口令，也不需要联网。",
      buildInlineBanner("warn", "任何持有本软件的人都能解开这份文件。请只发给应当拿到这些密钥的人，不要发到公开群或网盘。"),
      field,
      note,
    ],
    actions: [
      { label: "取消", variant: "default" },
      {
        label: "选择保存位置", variant: "primary", keepOpen: true,
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
            showToast({ message: `已导出模型配置到 ${saved}；本机目前没有已保存的 API Key，文件不含任何密钥。` });
          } catch (error) {
            // 不带前缀的话，写盘失败时弹出来的会是 Rust 那一层的系统错误原文，
            // 用户看不出这是「导出」这件事出的错。
            showToast({ message: `导出失败：${errorMessage(error)}`, error: true });
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
  const describe = (row: ApiKeyExportRow) => `${row.role_label || row.role}：${row.connection}`;
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
      ["文件", monoText(savedPath)],
      // 数的是后端密封前实际数出来的密钥处数，不是回执里的连接行数：角色「换服务商时
      // 记住的配置」里的密钥也会被一起加密带走，按连接行数报会少算，和收件人导入时
      // 看到的作用域数对不上。
      ["密钥", textAndChip(`${sealedKeyCount} 个密钥已加密写入 `, createChip({ label: "已加密", tone: "ok" }))],
      ["有效期至", expiresAt ? `${formatReleaseDate(expiresAt)}（还剩 ${daysFromNow(expiresAt)} 天）` : "长期有效"],
    ]));
  } else {
    body.push(`配置已导出到 ${savedPath}。`);
  }
  if (withheld.length) {
    body.push(
      `以下 ${withheld.length} 条连接的密钥没有写进文件，因为它们是从别人的配置文件导入到这台电脑的：`,
      buildList(withheld, describe, "8px"),
    );
  }
  if (memories.length) {
    // 这些不是连接列表里的条目，而是角色换服务商时留下的记忆，用「服务商 + 地址」来指认。
    body.push(
      `另有 ${memories.length} 处「换服务商时记住的配置」里的密钥同样是导入来的，也没有写进文件：`,
      buildList(memories, (row) => `${row.role_label || row.role}：${providerLabel(row.provider ?? "")}（${row.connection}）`, "8px"),
    );
  }
  if (withheld.length || memories.length) {
    body.push("这样做是为了不把别人的密钥继续传出去。对方导入这份配置后，需要自己填写上面这些密钥。如果这些密钥本来就是你的，在对应位置重新填写并保存一次，以后导出就会带上。");
  }
  if (exported.length) {
    body.push(`已随文件导出的密钥（${exported.length} 条连接，请把文件放到受保护的位置）：`, buildList(exported, describe, "0"));
  } else if (!sealed) {
    body.push("这次导出的文件里没有任何密钥，对方导入后需要自己填写全部连接的密钥。");
  }

  openModal({
    tone: sealed ? "ok" : "warn",
    icon: sealed ? "check" : "warn",
    wide: true,
    sourceLabel: "设置 · 模型服务 · 导出含 Key",
    title: sealed ? "已导出，密钥已加密" : "部分密钥没有导出",
    body,
    actions: [
      { label: "在文件夹中显示", onClick: () => void revealSavedFile(savedPath), keepOpen: true },
      { label: "知道了", variant: "primary" },
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
      if (errorMessage(error) === CORRUPT_IMPORT_DETAIL) {
        openImportCorruptModal();
        return;
      }
      throw error;
    }
    setModelImportPreview({ fileName, payload, ...preview });
    openImportPreviewModal();
  }, "模型配置导入预览失败", "application/json,.json,.xltcfg");
}

function openImportCorruptModal(): void {
  openModal({
    tone: "danger",
    icon: "close",
    sourceLabel: "设置 · 模型服务 · 导入配置",
    title: "文件校验未通过",
    // 第二句不是客套话：校验不过意味着文件可能是被人改过的（比如把某条连接的服务
    // 地址换成别人的服务器），「确认文件来源」是这条路径上用户唯一能做的安全动作。
    body: [CORRUPT_IMPORT_DETAIL, "如果重发后仍然报错，请联系发送方确认文件来源。"],
    actions: [{ label: "知道了", variant: "primary" }],
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
    for (const role of Object.keys(modelRoles)) clearModelCatalog(role, "导入后请重新获取当前连接的模型列表。");
    renderBody();
    handle.close();
    const unsealedKeys = preview.seal_status === "unsealed" && result.imported_key_count > 0;
    const keysClause = result.imported_key_count > 0
      ? `；同时${unsealedKeys ? "解开并写入" : "写入"} ${result.imported_key_count} 个密钥`
      : "";
    showToast({ message: `已导入配置，文件里包含的用途，其连接列表已整份替换${keysClause}。这些用途都需要重新测试连接。` });
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
    ["文件", monoText(preview.fileName)],
    ["包含", `${preview.roles.length} 个用途 · ${preview.throughput_profile_count} 项速率设置`],
  ];
  if (preview.sealed_key_count > 0) {
    kvRows.push(["密钥", textAndChip(
      `${preview.sealed_key_count} 个密钥 `,
      showPlaintextWarning
        ? createChip({ label: "明文", tone: "warn" })
        : createChip({ label: "已自动解开", tone: "ok" }),
    )]);
    if (!showPlaintextWarning) {
      kvRows.push(["有效期至", preview.expires_at
        ? `${formatReleaseDate(preview.expires_at)}（还剩 ${daysFromNow(preview.expires_at)} 天）`
        : "长期有效"]);
    }
  }

  const body: (string | HTMLElement)[] = [buildKvList(kvRows)];
  if (showPlaintextWarning) {
    body.push(buildInlineBanner("warn", "这是旧版本导出的文件，密钥以明文保存在文件里，任何人用记事本打开都能看到。可以正常导入，建议导入后立刻删除这个文件。"));
  }
  body.push(
    REPLACES_CONNECTIONS_NOTICE,
    buildRolesList(preview),
    "导入后这些用途都会变成“未测试”，不会自动发出任何请求。",
  );

  let handle: ModalHandle;
  handle = openModal({
    tone: "tint",
    icon: "down",
    wide: true,
    sourceLabel: "设置 · 模型服务 · 导入配置",
    title: "预览导入模型配置",
    body,
    actions: [
      { label: "取消", variant: "default" },
      {
        // 按钮和 toast 都跟着正文的口径走：连接列表是整份替换的，再说「合并」会让用户
        // 以为本机自己加的连接还在，而它们连同已保存的 Key 已经没了。
        label: "确认替换并导入", variant: "primary", keepOpen: true,
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
    sourceLabel: "设置 · 模型服务 · 导入配置",
    title: "这份文件已过期",
    body: [
      `文件的有效期是 ${preview.expires_at ? formatReleaseDate(preview.expires_at) : "未知"}，${elapsedDays > 0 ? `已经过去 ${elapsedDays} 天` : "刚刚过期"}，里面的密钥无法再解开。`,
      buildKvList([
        ["包含", `${preview.roles.length} 个用途 · ${preview.throughput_profile_count} 项速率设置`],
        ["密钥", textAndChip(`${preview.sealed_key_count} 个密钥 `, createChip({ label: "已失效，无法解开", tone: "warn" }))],
      ]),
      buildInlineBanner("tint", "连接、模型名和速率设置仍可正常导入，导入后各连接的密钥需要自己填写。想要密钥的话，请让发送方重新导出一份。"),
      // 「仅导入配置」提交的 payload 和正常导入的完全一样，连接列表照样整份替换。
      // 不写这一句，「仅」字会让人以为影响有限，而本机自己加的连接和它们的 Key
      // 已经没了，用户只能在事后的 toast 里第一次看到这件事。
      REPLACES_CONNECTIONS_NOTICE,
    ],
    actions: [
      { label: "取消", variant: "default" },
      { label: "仅导入配置（替换现有连接）", variant: "primary", keepOpen: true, onClick: () => runModelConfigImport(preview, handle) },
    ],
  });
}

function openImportUnsupportedModal(preview: ModelImportPreview): void {
  let handle: ModalHandle;
  handle = openModal({
    tone: "warn",
    icon: "restart",
    wide: true,
    sourceLabel: "设置 · 模型服务 · 导入配置",
    title: "需要更新软件才能解开密钥",
    body: [
      `这份文件由更新版本的 Translator 导出，当前版本（${PACKAGE_VERSION}）无法解开其中的密钥。`,
      buildInlineBanner("tint", "更新到最新版后重新导入即可。也可以先只导入配置，之后自己填写密钥。"),
      REPLACES_CONNECTIONS_NOTICE, // 同过期弹窗：「仅」字不代表影响有限，见那边的注释
    ],
    actions: [
      { label: "仅导入配置（替换现有连接）", keepOpen: true, onClick: () => runModelConfigImport(preview, handle) },
      { label: "前往更新", variant: "primary", onClick: () => navigate("settings", { page: "about" }) },
    ],
  });
}

export async function exportDocumentConfig(): Promise<void> {
  try {
    const payload = await client.request<JsonObject>("/api/document-config/export");
    const saved = await saveJsonFile("translator-document-config.json", payload);
    if (!saved) return; // 用户在保存框里取消：静默返回
    showToast({ message: `已导出文档翻译配置到 ${saved}；不含模型、密钥和本机输出目录。` });
  } catch (error) {
    showToast({ message: `导出失败：${errorMessage(error)}`, error: true });
  }
}

export function importDocumentConfig(): void {
  pickJsonFile(async (fileName, payload) => {
    const preview = await client.request<{ areas: string[]; app_version: string }>(
      "/api/document-config/import/preview",
      { method: "POST", body: JSON.stringify(payload) },
    );
    openDocumentImportPreviewModal(fileName, payload, preview.areas);
  }, "文档翻译配置导入预览失败");
}

export function openDocumentImportPreviewModal(fileName: string, payload: JsonObject, areas: string[]): void {
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
      showToast({ message: `${failureLabel}：${errorMessage(error)}`, error: true });
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
    showToast({ message: `诊断包已保存到：${saved}` });
  } catch (error) {
    showToast({ message: `导出诊断失败：${errorMessage(error)}`, error: true });
  }
}
