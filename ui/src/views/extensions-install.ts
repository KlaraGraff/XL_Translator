// 「扩展」页翻译类型列表下面的两段：「更多文件类型」（添加扩展、制作与分享的入口）和「浏览扩展」
// （扩展列表里审核过的扩展）。翻译类型列表里装进来的类型的「卸载」也在这里。
//
// 装进来的扩展由第三方编写，在单独的子进程里运行：崩溃或卡住只影响那个文件，静态检查拦住明显越界的
// 写法。但这不是沙箱，扩展以用户权限运行、能读写文件，所以每次安装都先弹确认框说清这一点。
// 后端见 api/routes/extensions.py。安装在后台进行，这里轮询进度；装好、卸载后重新拉描述符，
// 注册或移除工作区视图并重排侧栏。轮询不随页面卸载停止：切走了也照样收尾。

import { open } from "@tauri-apps/plugin-dialog";
import { invoke } from "@tauri-apps/api/core";
import {
  ApiClient,
  ApiError,
  type ExtensionInstallProgress,
  type ExtensionPreview,
  type ExtensionRegistry,
  type InstalledExtension,
  type LocalizedText,
  type RegistryExtension,
} from "../api-client";
import { createButton, createCard, createChip, createEmptyState, createProgressBar, openModal, showToast } from "../components";
import { icon } from "../icons";
import { currentLocale, t, tx } from "../i18n";
import { hasView, registerView, unregisterView } from "../router";
import {
  isExtSurface,
  isTypeVisible,
  localizedText,
  refreshTranslateSideItems,
  reloadSurfaceDescriptors,
  surfaceDescriptor,
  surfaceDescriptors,
} from "../surface-descriptors";
import { extensionView } from "./extension";
import "./extensions-install.css";

const TEMPLATE_HOME = "https://github.com/KlaraGraff/XL_Translator-extension-template";
const REGISTRY_HOME = "https://github.com/KlaraGraff/XL_Translator-extensions";

/** 与命令行 `adapter install` 打印的那句相同（core/adapter_install.py CONFIRM_NOTICE）。 */
function confirmNotice(): string {
  return t("扩展由第三方作者编写，以你的用户权限运行，可以读写你的文件；只安装你信任的来源。");
}

export interface InstallHost {
  /** 装好或卸载之后：页面重新读设置、重画翻译类型列表。 */
  onChanged(): Promise<void>;
}

let host: InstallHost | null = null;
let aboutEl: HTMLDivElement | null = null;
let browseEl: HTMLDivElement | null = null;
let browseListEl: HTMLDivElement | null = null;

let installed: InstalledExtension[] = [];
let registry: ExtensionRegistry | null = null;
let registryLoading = false;
let registryError = "";
let query = "";
/** 「浏览扩展」按文件类型筛选；空串为全部。 */
let extFilter = "";

let job: ExtensionInstallProgress | null = null;
/** 正在装的扩展在界面上的名字（进度行和完成提示用）。 */
let jobName = "";
/** 装之前这个类型已经在不在（区分「新装」和「更新」的提示）。 */
let jobWasInstalled = false;
let pollTimer: number | null = null;

async function connected(): Promise<ApiClient> {
  const client = new ApiClient();
  await client.connect();
  return client;
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

async function openExternalUrl(url: string): Promise<void> {
  try {
    await invoke("open_external_url", { url });
  } catch (error) {
    showToast({ message: errorText(error, t("无法打开网页。")), error: true });
  }
}

function linkButton(label: string, url: string): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "linklike";
  button.append(label, icon("ext"));
  button.addEventListener("click", () => void openExternalUrl(url));
  return button;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return t("{0} 字节", bytes);
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function extensionsText(extensions: readonly string[]): string {
  return extensions.map((ext) => `.${ext}`).join(" ");
}

// ── 页面挂载 ────────────────────────────────────────────────────────────────

/** 「扩展」页挂载时调用一次：建两张卡片，读已装的扩展和扩展列表。 */
export function mountInstall(pageHost: InstallHost): void {
  host = pageHost;
  aboutEl = createCard([]);
  aboutEl.classList.add("extensions-about");
  browseEl = createCard([]);
  browseEl.classList.add("extensions-browse");
  renderAbout();
  renderBrowse();
  void loadInstalled();
  void loadRegistry(false);
}

export function unmountInstall(): void {
  host = null;
  aboutEl = null;
  browseEl = null;
  browseListEl = null;
}

/** 两张卡片本身（页面每次重画都接回同一份，搜索框不会因此失焦）。 */
export function installSections(): HTMLElement[] {
  return [aboutEl, browseEl].filter((item): item is HTMLDivElement => item !== null);
}

async function loadInstalled(): Promise<void> {
  try {
    const payload = await (await connected()).installedExtensions();
    installed = payload.extensions;
    // 上次进页时开始的安装还在跑：接着轮询。
    if (payload.install.running && pollTimer === null) {
      job = payload.install;
      jobName = payload.install.label ?? "";
      poll();
    }
  } catch {
    // 读不到时不显示装坏了的那几行，其余照常。
  }
  renderAbout();
}

async function loadRegistry(force: boolean): Promise<void> {
  registryLoading = true;
  renderBrowse();
  try {
    registry = await (await connected()).extensionRegistry(force);
    registryError = "";
  } catch (error) {
    registryError = errorText(error, t("无法获取扩展列表。"));
  } finally {
    registryLoading = false;
    renderBrowse();
  }
}

// ── 「更多文件类型」：说明、添加扩展、制作与分享 ────────────────────────────

function renderAbout(): void {
  if (!aboutEl) return;
  aboutEl.replaceChildren();
  const head = document.createElement("div");
  head.className = "tc-head";
  const title = document.createElement("b");
  title.textContent = t("更多文件类型");
  head.append(title);

  const body = document.createElement("div");
  body.className = "ext-about-body";
  const text = document.createElement("p");
  text.textContent = t("可以自行制作扩展，让本软件支持更多文件类型；制作好的扩展可以上传，分享给其他人使用，也可以下载其他人分享的扩展。");
  const notice = document.createElement("p");
  notice.className = "ext-notice";
  notice.textContent = confirmNotice();
  body.append(text, notice);

  const busy = Boolean(job?.running);
  const actions = document.createElement("div");
  actions.className = "ext-actions";
  const add = createButton({ label: t("添加扩展…"), icon: "plus", variant: "primary", disabled: busy, onClick: () => void addFromFile() });
  const folder = createButton({ label: t("从文件夹添加…"), icon: "folder", disabled: busy, onClick: () => void addFromFolder() });
  const spacer = document.createElement("span");
  spacer.className = "ext-spacer";
  const make = createButton({ label: t("制作扩展"), icon: "wand", onClick: () => showMakeGuide() });
  const share = createButton({ label: t("分享扩展"), icon: "link", onClick: () => void openExternalUrl(shareUrl()) });
  share.title = t("打开扩展列表的提交说明");
  actions.append(add, folder, spacer, make, share);
  body.append(actions);

  if (job?.running) body.append(progressRow(job));

  const broken = installed.filter((item) => !item.ok);
  for (const item of broken) body.append(brokenRow(item));

  aboutEl.append(head, body);
}

function shareUrl(): string {
  return currentLocale() === "zh" ? `${REGISTRY_HOME}#如何提交` : `${REGISTRY_HOME}/blob/main/README.en.md#how-to-submit`; // i18n-ignore 中文 README 的标题锚点
}

function progressRow(progress: ExtensionInstallProgress): HTMLElement {
  const row = document.createElement("div");
  row.className = "ext-progress";
  const label = document.createElement("span");
  const name = jobName || t("扩展");
  label.textContent = progress.phase === "download" ? t("正在下载「{0}」…", name) : t("正在安装「{0}」…", name);
  // 下载阶段按字节走到 90%，之后的检查和安装很快，停在 95%；还不知道大小时先给一小段。
  const percent = progress.phase === "download" ? (progress.total > 0 ? Math.max(8, (progress.done / progress.total) * 90) : 8) : 95;
  row.append(label, createProgressBar({ percent }).root);
  return row;
}

/** 已登记但用不了的扩展（文件丢了、与本版本不兼容）：说明原因，给一个「卸载」。 */
function brokenRow(item: InstalledExtension): HTMLElement {
  const row = document.createElement("div");
  row.className = "ext-broken";
  row.append(icon("warn", { size: "sm" }));
  const text = document.createElement("span");
  text.textContent = t("「{0}」无法使用：{1}", localizedText(item.label, item.id), tx(item.problem));
  const remove = createButton({ label: t("卸载"), size: "mini", disabled: Boolean(job?.running), onClick: () => void confirmUninstall(item.id, localizedText(item.label, item.id)) });
  row.append(text, remove);
  return row;
}

function showMakeGuide(): void {
  const steps = document.createElement("ol");
  steps.className = "ext-steps";
  for (const step of [
    t("在扩展模板的项目主页上点「Use this template」，建一个自己的仓库，或者直接下载模板。"),
    t("按模板里的说明修改 adapter.json 和 adapter.py：一个扩展只需写清从文件里取出哪些文字、译文写回哪里。也可以让 Claude Code、Codex 等编程助手照着模板里的 AGENTS.md 来写。"),
    t("用「从文件夹添加…」把正在制作的扩展装进本软件试用。"),
    t("在仓库里打一个版本标签，模板会自动生成扩展包；按扩展列表的提交说明提交后，经审核即可出现在「浏览扩展」里，供其他人下载。"),
  ]) {
    const li = document.createElement("li");
    li.textContent = step;
    steps.append(li);
  }
  const links = document.createElement("div");
  links.className = "ext-modal-links";
  links.append(linkButton(t("项目主页"), TEMPLATE_HOME), linkButton(t("提交说明"), shareUrl()));
  const modal = openModal({
    tone: "tint",
    icon: "wand",
    sourceLabel: t("扩展 · 制作"),
    title: t("制作扩展"),
    body: [t("扩展用 Python 编写，只能使用 Python 标准库，不能联网，也不调用翻译模型（翻译由本软件完成）。"), steps],
    actions: [
      { label: t("关闭") },
      { label: t("从文件夹添加…"), variant: "primary", onClick: () => void addFromFolder() },
    ],
  });
  modal.element.insertBefore(links, modal.element.querySelector(".macts"));
}

// ── 添加扩展：选 zip 或文件夹 → 检查 → 确认 → 安装 ───────────────────────────

async function addFromFile(): Promise<void> {
  const picked = await open({ multiple: false, directory: false, filters: [{ name: t("扩展包"), extensions: ["zip"] }] }).catch(() => null);
  const path = Array.isArray(picked) ? picked[0] : picked;
  if (typeof path === "string" && path) await previewAndConfirm(path);
}

async function addFromFolder(): Promise<void> {
  const picked = await open({ multiple: false, directory: true }).catch(() => null);
  const path = Array.isArray(picked) ? picked[0] : picked;
  if (typeof path === "string" && path) await previewAndConfirm(path);
}

async function previewAndConfirm(path: string): Promise<void> {
  if (job?.running) return;
  let preview: ExtensionPreview;
  try {
    preview = await (await connected()).previewExtension(path);
  } catch (error) {
    if (error instanceof ApiError && error.status === 422) {
      showProblems(path, error.rawMessage);
    } else {
      showToast({ message: errorText(error, t("无法检查这个扩展。")), error: true });
    }
    return;
  }
  const name = localizedText(preview.label, preview.id);
  const facts: [string, string][] = [
    [t("版本"), preview.installed_version && preview.installed_version !== preview.version ? `${preview.installed_version} → ${preview.version}` : preview.version],
    [t("文件类型"), extensionsText(preview.extensions)],
    [t("许可证"), preview.license || t("未写明")],
    [t("来源"), preview.source],
    [t("内容"), t("{0} 个文件，共 {1}", preview.file_count + preview.sample_count, formatSize(preview.size))],
  ];
  const confirmed = await confirmInstall({
    sourceLabel: preview.kind === "zip" ? t("扩展 · 本机 zip") : t("扩展 · 本机文件夹"),
    title: preview.action === "install" ? t("添加「{0}」", name) : t("更新「{0}」", name),
    facts,
    warnings: preview.warnings.map((item) => tx(item)),
    repo: preview.repo,
    actionLabel: preview.action === "install" ? t("安装") : t("更新"),
  });
  if (!confirmed) return;
  await startJob(name, preview.key, (client) => client.installExtension(path, preview.sha256));
}

/** 检查没通过：逐条列出原因（后端用「；」连接多条），不给「安装」。 */
function showProblems(path: string, rawMessage: string): void {
  const problems = rawMessage.split("；").map((item) => item.trim()).filter(Boolean);
  const list = document.createElement("ul");
  list.className = "ext-problems";
  for (const problem of problems) {
    const li = document.createElement("li");
    li.textContent = tx(problem);
    list.append(li);
  }
  openModal({
    tone: "warn",
    icon: "warn",
    sourceLabel: t("扩展 · 检查未通过"),
    title: t("无法添加这个扩展"),
    body: [t("所选内容：{0}", path), list, t("如果是你自己制作的扩展，改好后可以再添加一次。")],
    actions: [{ label: t("知道了"), variant: "primary" }],
  });
}

interface ConfirmContent {
  sourceLabel: string;
  title: string;
  facts: [string, string][];
  warnings: string[];
  repo: string;
  actionLabel: string;
}

/** 安装确认框：本机添加和扩展列表安装共用。 */
function confirmInstall(content: ConfirmContent): Promise<boolean> {
  return new Promise((resolve) => {
    const facts = document.createElement("dl");
    facts.className = "ext-facts";
    for (const [label, value] of content.facts) {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      facts.append(dt, dd);
    }
    const body: (string | HTMLElement)[] = [facts];
    if (content.warnings.length) {
      const list = document.createElement("ul");
      list.className = "ext-warnings";
      for (const warning of content.warnings) {
        const li = document.createElement("li");
        li.textContent = warning;
        list.append(li);
      }
      body.push(list);
    }
    const notice = document.createElement("span");
    notice.className = "ext-notice";
    notice.textContent = confirmNotice();
    body.push(notice);
    const modal = openModal({
      tone: "tint",
      icon: "down",
      sourceLabel: content.sourceLabel,
      title: content.title,
      body,
      actions: [
        { label: t("取消"), onClick: () => resolve(false) },
        { label: content.actionLabel, variant: "primary", onClick: () => resolve(true) },
      ],
    });
    // 点遮罩外面或按 Esc 关掉时也要给出结果，否则调用方一直等着。
    const observer = new MutationObserver(() => {
      if (!modal.element.isConnected) {
        observer.disconnect();
        resolve(false);
      }
    });
    observer.observe(document.body, { childList: true });
    if (content.repo.startsWith("https://")) {
      const links = document.createElement("div");
      links.className = "ext-modal-links";
      links.append(linkButton(t("项目主页"), content.repo));
      modal.element.insertBefore(links, modal.element.querySelector(".macts"));
    }
  });
}

async function startJob(name: string, key: string, begin: (client: ApiClient) => Promise<ExtensionInstallProgress>): Promise<void> {
  try {
    job = await begin(await connected());
  } catch (error) {
    showToast({ message: errorText(error, t("扩展安装失败。")), error: true });
    return;
  }
  jobName = name;
  jobWasInstalled = Boolean(surfaceDescriptor(key));
  renderAbout();
  renderBrowse();
  poll();
}

function poll(): void {
  if (pollTimer !== null) window.clearTimeout(pollTimer);
  pollTimer = window.setTimeout(async () => {
    pollTimer = null;
    try {
      job = await (await connected()).extensionInstallProgress();
    } catch {
      poll();
      return;
    }
    if (job.running) {
      renderAbout();
      poll();
      return;
    }
    await finishJob(job);
  }, 400);
}

async function finishJob(progress: ExtensionInstallProgress): Promise<void> {
  const name = localizedText(progress.result?.label, jobName);
  if (progress.phase === "done" && progress.result) {
    const key = `ext:${progress.result.id}`;
    const settings = await applyChange();
    if (jobWasInstalled) {
      showToast({ message: t("已更新「{0}」到 {1}。", name, progress.result.version) });
    } else if (isTypeVisible(settings, key)) {
      showToast({ message: t("已添加到侧栏，可在此页调整。"), duration: 4500 });
    } else {
      showToast({ message: t("已安装「{0}」，可在此页设为显示在侧栏。", name), duration: 4500 });
    }
  } else if (progress.error && !progress.cancelled) {
    showToast({ message: tx(progress.error), error: true, duration: 6000 });
  }
  job = null;
  jobName = "";
  await loadInstalled();
  if (registry) await loadRegistry(false);
}

/** 装好或卸载之后：重新拉描述符、补上或移除工作区视图、按设置重排侧栏，再让页面重画。返回最新设置。 */
async function applyChange(): Promise<Record<string, unknown>> {
  const before = surfaceDescriptors().filter((item) => !item.builtin && isExtSurface(item.key)).map((item) => item.key);
  let settings: Record<string, unknown> = {};
  try {
    const items = await reloadSurfaceDescriptors();
    const now = new Set(items.filter((item) => !item.builtin && isExtSurface(item.key)).map((item) => item.key));
    for (const key of now) {
      if (isExtSurface(key) && !hasView(key)) registerView(key, extensionView(key));
    }
    for (const key of before) {
      if (isExtSurface(key) && !now.has(key)) unregisterView(key);
    }
    settings = await (await connected()).request<Record<string, unknown>>("/api/settings");
    refreshTranslateSideItems(settings);
  } catch {
    // 拉不到时侧栏等下次启动再排；扩展本身已经装好或卸掉了。
  }
  await host?.onChanged();
  return settings;
}

// ── 翻译类型列表里的「卸载」 ─────────────────────────────────────────────────

/** 装进来的类型（来源为 installed）在翻译类型列表里的「卸载」按钮；其他类型返回 null。 */
export function uninstallButton(key: string): HTMLButtonElement | null {
  const adapter = surfaceDescriptor(key)?.adapter;
  if (!adapter || adapter.origin !== "installed") return null;
  const name = localizedText(surfaceDescriptor(key)?.label, adapter.id);
  const button = createButton({
    icon: "trash",
    size: "mini",
    title: t("卸载"),
    disabled: Boolean(job?.running),
    onClick: () => void confirmUninstall(adapter.id, name),
  });
  button.setAttribute("aria-label", t("卸载"));
  button.classList.add("ext-uninstall");
  return button;
}

async function confirmUninstall(id: string, name: string): Promise<void> {
  openModal({
    tone: "danger",
    icon: "trash",
    sourceLabel: t("扩展 · 卸载"),
    title: t("卸载「{0}」？", name),
    body: [t("卸载后侧栏和翻译参数里不再有这个类型，已有任务的记录和译文文件不受影响。它的翻译参数会保留，重新安装后恢复。")],
    actions: [
      { label: t("取消") },
      {
        label: t("卸载"),
        variant: "danger-solid",
        onClick: async () => {
          await (await connected()).uninstallExtension(id);
          await applyChange();
          showToast({ message: t("已卸载「{0}」。", name) });
          await loadInstalled();
          if (registry) await loadRegistry(false);
        },
      },
    ],
  });
}

// ── 「浏览扩展」 ─────────────────────────────────────────────────────────────

function renderBrowse(): void {
  if (!browseEl) return;
  if (!browseListEl) {
    browseEl.replaceChildren();
    browseEl.append(browseHead());
    browseListEl = document.createElement("div");
    browseListEl.className = "ext-browse-body";
    browseEl.append(browseListEl);
  }
  const refresh = browseEl.querySelector<HTMLButtonElement>(".ext-refresh");
  if (refresh) refresh.disabled = registryLoading;
  renderBrowseList();
}

function browseHead(): HTMLElement {
  const head = document.createElement("div");
  head.className = "tc-head ext-browse-head";
  const title = document.createElement("b");
  title.textContent = t("浏览扩展");
  const sub = document.createElement("span");
  sub.className = "ext-browse-sub";
  sub.textContent = t("经过审核的扩展，来自公开的扩展列表");
  const search = document.createElement("label");
  search.className = "tk-search ext-search";
  const input = document.createElement("input");
  input.type = "search";
  input.placeholder = t("搜索扩展");
  input.setAttribute("aria-label", t("搜索扩展"));
  input.value = query;
  input.addEventListener("input", () => {
    query = input.value;
    renderBrowseList();
  });
  search.append(icon("search", { size: "sm" }), input);
  const refresh = createButton({ label: t("刷新"), icon: "restart", size: "mini", disabled: registryLoading, onClick: () => void loadRegistry(true) });
  refresh.classList.add("ext-refresh");
  head.append(title, sub, search, refresh);
  return head;
}

function entryName(entry: RegistryExtension): string {
  return localizedText(entry.name, entry.id);
}

function matches(entry: RegistryExtension): boolean {
  if (extFilter && !entry.extensions.includes(extFilter)) return false;
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const haystack = [
    entry.id,
    entry.author,
    ...Object.values(entry.name as LocalizedText),
    ...Object.values(entry.summary as LocalizedText),
    ...entry.extensions.map((ext) => `.${ext}`),
  ].join(" ").toLowerCase();
  return words.every((word) => haystack.includes(word));
}

function renderBrowseList(): void {
  if (!browseListEl) return;
  browseListEl.replaceChildren();
  const entries = registry?.entries ?? [];
  if (registry?.stale && entries.length) {
    const stale = document.createElement("div");
    stale.className = "ext-stale";
    stale.append(icon("warn", { size: "sm" }), t("列表可能不是最新（无法连接）"));
    browseListEl.append(stale);
  }
  if (!entries.length) {
    if (registryLoading) {
      browseListEl.append(createEmptyState({ title: t("正在读取扩展列表…") }));
    } else {
      const reason = registryError || (registry?.error ? tx(registry.error) : "");
      browseListEl.append(
        reason
          ? createEmptyState({ icon: "warn", title: t("无法获取扩展列表"), description: reason })
          : createEmptyState({ icon: "blocks", title: t("扩展列表里还没有扩展") }),
      );
    }
    return;
  }

  const types = [...new Set(entries.flatMap((entry) => entry.extensions))].sort();
  if (extFilter && !types.includes(extFilter)) extFilter = "";
  if (types.length > 1) {
    const filters = document.createElement("div");
    filters.className = "tk-filters ext-filters";
    for (const [value, label] of [["", t("全部")], ...types.map((ext) => [ext, `.${ext}`])] as [string, string][]) {
      const chip = document.createElement("span");
      chip.className = value === extFilter ? "f on" : "f";
      chip.textContent = label;
      chip.addEventListener("click", () => {
        extFilter = value;
        renderBrowseList();
      });
      filters.append(chip);
    }
    browseListEl.append(filters);
  }

  const shown = entries.filter(matches);
  if (!shown.length) {
    browseListEl.append(createEmptyState({ icon: "search", title: t("没有找到匹配的扩展") }));
    return;
  }
  for (const entry of shown) browseListEl.append(entryRow(entry));
}

function entryRow(entry: RegistryExtension): HTMLElement {
  const row = document.createElement("div");
  row.className = "ext-entry";
  row.dataset.id = entry.id;

  const iconBox = document.createElement("div");
  iconBox.className = "types-icon";
  iconBox.append(icon("blocks"));

  const main = document.createElement("div");
  main.className = "ext-entry-main";
  const title = document.createElement("div");
  title.className = "ext-entry-title";
  const name = document.createElement("b");
  name.textContent = entryName(entry);
  title.append(name, createChip({ label: entry.status === "official" ? t("官方") : t("已审核"), tone: entry.status === "official" ? "tint" : "mute" }));
  const summary = document.createElement("p");
  summary.textContent = localizedText(entry.summary);
  const meta = document.createElement("small");
  const parts = [extensionsText(entry.extensions), entry.author, entry.version, formatSize(entry.size), entry.license].filter(Boolean);
  if (entry.state === "update" && entry.installed_version) parts.push(t("已安装 {0}", entry.installed_version));
  meta.textContent = parts.join(" · ");
  main.append(title, summary, meta);

  const actions = document.createElement("div");
  actions.className = "ext-entry-actions";
  actions.append(entryAction(entry), createButton({ label: t("项目主页"), icon: "ext", size: "mini", onClick: () => void openExternalUrl(entry.repo) }));

  row.append(iconBox, main, actions);
  return row;
}

function entryAction(entry: RegistryExtension): HTMLButtonElement {
  const busy = Boolean(job?.running);
  switch (entry.state) {
    case "available":
      return createButton({ label: t("安装"), icon: "down", size: "mini", variant: "primary", disabled: busy, onClick: () => void installEntry(entry) });
    case "update":
      return createButton({ label: t("更新"), icon: "down", size: "mini", variant: "primary", disabled: busy, onClick: () => void installEntry(entry) });
    case "installed":
      return createButton({ label: t("已安装"), icon: "check", size: "mini", disabled: true });
    case "bundled":
      return createButton({ label: t("随应用提供"), size: "mini", disabled: true, title: t("这个类型随应用提供，无需安装。") });
    default:
      return createButton({ label: t("不兼容"), size: "mini", disabled: true, title: tx(entry.incompatible_reason) });
  }
}

async function installEntry(entry: RegistryExtension): Promise<void> {
  if (job?.running) return;
  const name = entryName(entry);
  const update = entry.state === "update";
  const confirmed = await confirmInstall({
    sourceLabel: t("扩展 · 扩展列表"),
    title: update ? t("更新「{0}」", name) : t("安装「{0}」", name),
    facts: [
      [t("版本"), update && entry.installed_version ? `${entry.installed_version} → ${entry.version}` : entry.version],
      [t("作者"), entry.author],
      [t("文件类型"), extensionsText(entry.extensions)],
      [t("许可证"), entry.license],
      [t("审核"), entry.status === "official" ? t("官方：由本软件项目维护") : t("已审核：扩展列表的维护者看过源码")],
      [t("大小"), formatSize(entry.size)],
    ],
    warnings: [],
    repo: entry.repo,
    actionLabel: update ? t("更新") : t("安装"),
  });
  if (!confirmed) return;
  await startJob(name, `ext:${entry.id}`, (client) => client.installRegistryExtension(entry.id));
}
