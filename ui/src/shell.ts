// V9 应用外壳 —— 左侧分组侧栏 + 顶栏容器。对应样张的 .side + .stage(.topbar+.content)，
// 但 .screen 那层「展示用卡片」被拆掉，改成撑满窗口的应用根布局（见 styles/app.css 的
// .app-shell）。这里只负责外壳本身；具体页面内容由 router.ts 挂到 contentContainer 里。

import { icon, type IconName } from "./icons";
import { createPill, createStatus, type StatusTone } from "./components";
import { currentView, navigate, onNavigate, type ViewId } from "./router";
import { t, localized } from "./i18n";

interface SideItemDef {
  id: ViewId;
  label: string;
  icon: IconName;
}

const TRANSLATE_ITEMS: SideItemDef[] = localized(() => ([
  { id: "excel", label: t("Excel 表格"), icon: "excel" },
  { id: "word", label: t("Word 文档"), icon: "word" },
  { id: "pdf", label: t("PDF 与图片"), icon: "pdf" },
  { id: "cad", label: t("CAD 图纸"), icon: "cad" },
]));

const RESOURCE_ITEMS: SideItemDef[] = localized(() => ([
  { id: "tasks", label: t("任务中心"), icon: "tasks" },
  { id: "library", label: t("记忆库"), icon: "book" },
  { id: "plugins", label: t("插件"), icon: "spark" },
]));

const SYSTEM_ITEMS: SideItemDef[] = localized(() => ([
  { id: "settings", label: t("设置"), icon: "gear" },
  { id: "help", label: t("帮助"), icon: "help" },
  { id: "feedback", label: t("反馈"), icon: "message" },
]));

export interface ShellHandle {
  /** router.mountRouter() 应该指向这个容器；对应样张的 .content。 */
  contentContainer: HTMLElement;
}

export interface TopbarStatusConfig {
  label: string;
  tone: StatusTone;
}

export interface TopbarConfig {
  title: string;
  status?: TopbarStatusConfig;
  subtitle?: string;
}

export interface TaskPillConfig {
  /** 活动任务数；0 时顶栏药丸显示静态「任务中心」，>0 时显示 live 态「N 个任务运行中」。 */
  count: number;
}

export interface ModelPillConfig {
  label: string;
  tone?: "ok" | "idle" | "warn";
  /** Settings role opened when the pill is clicked. */
  role?: string;
}

const PILL_TONE_COLOR: Record<NonNullable<ModelPillConfig["tone"]>, string> = {
  ok: "var(--ok)",
  idle: "var(--ink-3)",
  warn: "var(--warn)",
};

let sideItemEls: Map<ViewId, HTMLDivElement> | null = null;
let taskBadgeEl: HTMLSpanElement | null = null;
let settingsDotEl: HTMLSpanElement | null = null;

let topbarTitleEl: HTMLHeadingElement | null = null;
let topbarStatusHost: HTMLDivElement | null = null;
let topbarSubEl: HTMLDivElement | null = null;
let taskPillHost: HTMLDivElement | null = null;
let modelPillHost: HTMLDivElement | null = null;
let toastHost: HTMLDivElement | null = null;
/** 卡片位：每个具名位是栈里的一格，一直存在（空着不占高度），所以更新卡片重画时
 *  不会跳到临时提示的下面去。 */
const toastSlots = new Map<string, HTMLDivElement>();

// 侧栏里要跟着界面语言换的文字节点：分组标题按原文 key、各项按视图 id 记下来，
// relocalizeShell() 原地改文字，不重建侧栏（徽标、红点、选中态都不动）。
const sideLabelNodes: { key: string; el: HTMLDivElement }[] = [];
const sideItemTextNodes = new Map<ViewId, Text>();
let lastTaskPill: TaskPillConfig = { count: 0 };

/** 运行时注册的翻译类型在侧栏「翻译」组里的项：插在「资源」分组标题之前。 */
export interface ExtensionSideItem {
  id: ViewId;
  /** 每次换界面语言都重新取一次（名字来自描述符的三语标签）。 */
  label: () => string;
  icon: IconName;
}

let resourceLabelEl: HTMLDivElement | null = null;
let extensionItems: { def: ExtensionSideItem; el: HTMLDivElement }[] = [];

function buildSideItem(def: SideItemDef): HTMLDivElement {
  const item = document.createElement("div");
  item.className = "side-item";
  item.append(icon(def.icon));
  const text = document.createTextNode(def.label);
  sideItemTextNodes.set(def.id, text);
  item.append(text);
  item.addEventListener("click", () => navigate(def.id));
  return item;
}

function buildSideLabel(key: string): HTMLDivElement {
  const label = document.createElement("div");
  label.className = "side-label";
  label.textContent = t(key);
  sideLabelNodes.push({ key, el: label });
  return label;
}

/** 界面语言换了以后，原地换掉外壳上的文字（侧栏、任务药丸）。模型药丸由调用方刷新。 */
export function relocalizeShell(): void {
  for (const { key, el } of sideLabelNodes) el.textContent = t(key);
  for (const def of [...TRANSLATE_ITEMS, ...RESOURCE_ITEMS, ...SYSTEM_ITEMS]) {
    const node = sideItemTextNodes.get(def.id);
    if (node) node.data = def.label;
  }
  for (const { def } of extensionItems) {
    const node = sideItemTextNodes.get(def.id);
    if (node) node.data = def.label();
  }
  setTaskPill(lastTaskPill);
}

/** 把运行时注册的翻译类型放进侧栏「翻译」组，排在内置四项之后；再次调用时整组替换。 */
export function setExtensionSideItems(items: ExtensionSideItem[]): void {
  if (!sideItemEls || !resourceLabelEl?.parentElement) return;
  for (const { def, el } of extensionItems) {
    el.remove();
    sideItemEls.delete(def.id);
    sideItemTextNodes.delete(def.id);
  }
  extensionItems = items.map((def) => {
    const el = buildSideItem({ id: def.id, label: def.label(), icon: def.icon });
    resourceLabelEl!.before(el);
    sideItemEls!.set(def.id, el);
    return { def, el };
  });
  const current = currentView();
  if (current) highlightSideItem(current.id);
}

/** 构建并挂载整个应用外壳到 root（通常是 #app）。只应调用一次。 */
export function mountShell(root: HTMLElement): ShellHandle {
  root.innerHTML = "";
  root.className = "app-shell";

  const side = document.createElement("aside");
  side.className = "side";

  const brand = document.createElement("div");
  brand.className = "brand";
  const brandMark = document.createElement("span");
  brandMark.className = "brand-mark";
  brandMark.textContent = "文A"; // i18n-ignore 品牌标记，不翻
  brand.append(brandMark, document.createTextNode("Translator"));
  side.append(brand);

  side.append(buildSideLabel("翻译")); // i18n-ignore 词条 key，显示时由 t() 翻译
  const itemMap = new Map<ViewId, HTMLDivElement>();
  for (const def of TRANSLATE_ITEMS) {
    const itemEl = buildSideItem(def);
    itemMap.set(def.id, itemEl);
    side.append(itemEl);
  }

  resourceLabelEl = buildSideLabel("资源"); // i18n-ignore 词条 key，显示时由 t() 翻译
  side.append(resourceLabelEl);
  for (const def of RESOURCE_ITEMS) {
    const itemEl = buildSideItem(def);
    itemMap.set(def.id, itemEl);
    if (def.id === "tasks") {
      const badge = document.createElement("span");
      badge.className = "side-badge";
      badge.style.display = "none";
      itemEl.append(badge);
      taskBadgeEl = badge;
    }
    side.append(itemEl);
  }

  const spacer = document.createElement("div");
  spacer.className = "side-spacer";
  side.append(spacer);

  for (const def of SYSTEM_ITEMS) {
    const itemEl = buildSideItem(def);
    itemMap.set(def.id, itemEl);
    if (def.id === "settings") {
      const dot = document.createElement("span");
      dot.className = "side-dot";
      dot.style.display = "none";
      itemEl.append(dot);
      settingsDotEl = dot;
    }
    side.append(itemEl);
  }

  sideItemEls = itemMap;
  root.append(side);

  const stage = document.createElement("div");
  stage.className = "stage";

  const topbar = document.createElement("header");
  topbar.className = "topbar";

  const titleBlock = document.createElement("div");
  titleBlock.className = "tb-title";
  const titleRow = document.createElement("div");
  titleRow.style.display = "flex";
  titleRow.style.alignItems = "center";
  titleRow.style.gap = "10px";
  topbarTitleEl = document.createElement("h1");
  titleRow.append(topbarTitleEl);
  topbarStatusHost = document.createElement("div");
  topbarStatusHost.style.display = "contents";
  titleRow.append(topbarStatusHost);
  titleBlock.append(titleRow);
  topbarSubEl = document.createElement("div");
  topbarSubEl.className = "tb-sub";
  titleBlock.append(topbarSubEl);
  topbar.append(titleBlock);

  const tbRight = document.createElement("div");
  tbRight.className = "tb-right";
  taskPillHost = document.createElement("div");
  taskPillHost.style.display = "contents";
  modelPillHost = document.createElement("div");
  modelPillHost.style.display = "contents";
  tbRight.append(taskPillHost, modelPillHost);
  topbar.append(tbRight);

  stage.append(topbar);

  // 浮在工作区之上的提示栈（顶部居中）。放在 .stage 里而不是 document.body，
  // 是为了让它相对内容区居中而不是相对整个窗口——否则左边有侧栏时会偏。
  // 用绝对定位不占布局高度：提示出现时工作区不会被往下挤。
  toastHost = document.createElement("div");
  toastHost.className = "toast-stack";
  stage.append(toastHost);
  // 顺序在这里定死：更新卡片在上，临时提示在下。留到各自第一次用时再建的话，
  // 谁先说话谁在上，同一个界面两次打开可能是两个样子。
  for (const name of ["update", "toast"]) toastHost.append(toastStackSlot(name));

  const content = document.createElement("div");
  content.className = "content";
  stage.append(content);

  root.append(stage);

  onNavigate((id) => {
    highlightSideItem(id);
  });

  // 初始默认药丸态，避免视图忘记调用 setTaskPill/setModelPill 时顶栏空着。
  setTaskPill({ count: 0 });
  setModelPill({ label: t("未连接模型"), tone: "idle" });

  return { contentContainer: content };
}

function highlightSideItem(id: ViewId): void {
  if (!sideItemEls) return;
  for (const [itemId, itemEl] of sideItemEls) {
    itemEl.classList.toggle("on", itemId === id);
  }
}

/** 更新任务中心侧栏徽标（活动任务数）。count <= 0 时隐藏徽标。 */
export function setTaskBadge(count: number): void {
  if (!taskBadgeEl) return;
  if (count > 0) {
    taskBadgeEl.textContent = String(count);
    taskBadgeEl.style.display = "";
  } else {
    taskBadgeEl.style.display = "none";
  }
}

/** 更新设置侧栏项的红点（有可用更新时点亮）。 */
export function setSettingsAlert(active: boolean): void {
  if (!settingsDotEl) return;
  settingsDotEl.style.display = active ? "" : "none";
}

/**
 * 取（或建）顶部提示栈里的一格。
 *
 * 更新卡片（update-toast.ts）和临时提示（components.ts 的 showToast）都往这里放，
 * 否则两者都想占「顶部居中」，会直接叠在一起。具名格保证顺序稳定：更新卡片始终在上，
 * 临时提示排在它下面，而且更新卡片自己重画不会因为「先删后插」掉到末尾去。
 *
 * 外壳还没挂载时返回一个游离节点（早期的启动错误提示），它不会被显示，但也不会崩。
 */
export function toastStackSlot(name: string): HTMLDivElement {
  const existing = toastSlots.get(name);
  if (existing) return existing;
  const slot = document.createElement("div");
  slot.className = `toast-slot ts-${name}`;
  toastSlots.set(name, slot);
  toastHost?.append(slot);
  return slot;
}

/** 设置顶栏标题 / 状态徽章 / 副标题。每个视图 mount() 时都应调用一次。 */
export function setTopbar(config: TopbarConfig): void {
  if (!topbarTitleEl || !topbarStatusHost || !topbarSubEl) return;
  topbarTitleEl.textContent = config.title;
  topbarStatusHost.innerHTML = "";
  if (config.status) {
    topbarStatusHost.append(createStatus(config.status));
  }
  topbarSubEl.textContent = config.subtitle ?? "";
  // 英法文副标题常比中文长：一行放不下时省略，悬停看全文。
  topbarSubEl.title = config.subtitle ?? "";
}

/** 更新顶栏右侧「任务中心」药丸。count > 0 时切到 live 态并显示计数。 */
export function setTaskPill(config: TaskPillConfig): void {
  lastTaskPill = config;
  if (!taskPillHost) return;
  taskPillHost.innerHTML = "";
  const pill = config.count > 0
    // 从「N 个任务运行中」进来，用户要看的就是正在跑的那个，而不是上次选中的旧结果。
    ? createPill({ label: t("{0} 个任务运行中", config.count), live: true, onClick: () => navigate("tasks", { focus: "active" }) })
    : createPill({ label: t("任务中心"), onClick: () => navigate("tasks") });
  taskPillHost.append(pill);
}

/** 更新顶栏右侧「当前模型」药丸。 */
export function setModelPill(config: ModelPillConfig): void {
  if (!modelPillHost) return;
  modelPillHost.innerHTML = "";
  const dotColor = PILL_TONE_COLOR[config.tone ?? "ok"];
  modelPillHost.append(
    createPill({ label: config.label, dotColor, onClick: () => navigate("settings", { page: "models", role: config.role }) }),
  );
}
