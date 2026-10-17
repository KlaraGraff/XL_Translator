// 应用外壳（V10.3「流光」）—— 灰色画布上一条无边框的导航栏（.side）和一张白色圆角舞台
// （.stage = .topbar + .content）。导航栏的选中块是一块会滑动的高亮（.side-ind），不是每项
// 自己的底色。这里只负责外壳本身；具体页面内容由 router.ts 挂到 contentContainer 里。

import { icon, type IconName } from "./icons";
import { createPill, createStatus, type StatusTone } from "./components";
import { currentView, navigate, onNavigate, type ViewId } from "./router";
import { t, localized } from "./i18n";
import { ariaKeyShortcut, shortcutLabel } from "./platform";
import { version as PACKAGE_VERSION } from "../package.json";

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
  { id: "tasks", label: t("任务"), icon: "tasks" },
  { id: "library", label: t("记忆库"), icon: "book" },
  { id: "extensions", label: t("扩展"), icon: "blocks" },
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
  /** 活动任务数；>0 时顶栏显示 live 态「N 个任务进行中」，0 时不显示（侧栏已有「任务」）。 */
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
let sideNavEl: HTMLDivElement | null = null;
let sideIndicatorEl: HTMLDivElement | null = null;
let translateGroupEl: HTMLDivElement | null = null;
let translateGroupText: Text | null = null;
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

// 侧栏里要跟着界面语言换的文字节点：各项按视图 id 记下来，「翻译」父项单独记，
// relocalizeShell() 原地改文字，不重建侧栏（徽标、红点、选中态都不动）。
const sideItemTextNodes = new Map<ViewId, Text>();
let sideSearchText: HTMLSpanElement | null = null;
let lastTaskPill: TaskPillConfig = { count: 0 };

/** 侧栏「翻译」组里的一项：内置四种和运行时注册的类型都用它，整组按「扩展」页的设置排。 */
export interface TranslateSideItem {
  id: ViewId;
  /** 每次换界面语言都重新取一次（运行时注册的类型名字来自描述符的三语标签）。 */
  label: () => string;
  icon: IconName;
}

/** 「翻译」组的子项都插在它前面（资源区第一项「任务」）。 */
let translateAnchorEl: HTMLDivElement | null = null;
let translateItems: { def: TranslateSideItem; el: HTMLDivElement }[] = [];

/** 内置四种之一在侧栏里的那一项，与启动时画的相同；不是内置类型时为 undefined。 */
export function builtinTranslateSideItem(id: ViewId): TranslateSideItem | undefined {
  const def = TRANSLATE_ITEMS.find((item) => item.id === id);
  if (!def) return undefined;
  return { id, label: () => TRANSLATE_ITEMS.find((item) => item.id === id)?.label ?? def.label, icon: def.icon };
}

function buildSideItem(def: SideItemDef, sub = false): HTMLDivElement {
  const item = document.createElement("div");
  item.className = sub ? "side-item sub" : "side-item";
  item.append(icon(def.icon, { size: "sm" }));
  const text = document.createTextNode(def.label);
  sideItemTextNodes.set(def.id, text);
  item.append(text);
  item.addEventListener("click", () => navigate(def.id));
  return item;
}

/**
 * 侧栏顶部的搜索入口。这里只派发 document 事件 "xl:cmdk"，搜索面板和键盘快捷键由
 * 监听它的模块负责；快捷键文字按平台显示（Mac「⌘K」、Windows「Ctrl+K」），不随界面语言变。
 */
function buildSideSearch(): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "side-search";
  button.setAttribute("aria-keyshortcuts", ariaKeyShortcut("K"));
  const label = document.createElement("span");
  label.className = "lb";
  label.textContent = t("搜索或输入路径");
  sideSearchText = label;
  const kbd = document.createElement("kbd");
  kbd.textContent = shortcutLabel("K");
  button.append(icon("search", { size: "sm" }), label, kbd);
  button.addEventListener("click", () => {
    document.dispatchEvent(new CustomEvent("xl:cmdk", { detail: { source: "side" } }));
  });
  return button;
}

/** 「翻译」分组的父项。阶段 1 还没有「翻译」入口页，点它进入组里的第一项（默认 Excel）。 */
function buildTranslateGroup(): HTMLDivElement {
  const item = document.createElement("div");
  item.className = "side-item side-group";
  item.append(icon("trans", { size: "sm" }));
  translateGroupText = document.createTextNode(t("翻译"));
  item.append(translateGroupText);
  // 「翻译」本身是入口页（拖入或选择文件，按类型分到各工作区）；四个类型页是它的子项。
  item.addEventListener("click", () => navigate("home"));
  return item;
}

/** 界面语言换了以后，原地换掉外壳上的文字（侧栏、任务药丸）。模型药丸由调用方刷新。 */
export function relocalizeShell(): void {
  if (translateGroupText) translateGroupText.data = t("翻译");
  if (sideSearchText) sideSearchText.textContent = t("搜索或输入路径");
  for (const def of [...RESOURCE_ITEMS, ...SYSTEM_ITEMS]) {
    const node = sideItemTextNodes.get(def.id);
    if (node) node.data = def.label;
  }
  for (const { def } of translateItems) {
    const node = sideItemTextNodes.get(def.id);
    if (node) node.data = def.label();
  }
  setTaskPill(lastTaskPill);
}

/** 按给定顺序重画侧栏「翻译」组（内置四种与运行时注册的类型，没列出的不显示）；再次调用时整组替换。 */
export function setTranslateSideItems(items: TranslateSideItem[]): void {
  if (!sideItemEls || !translateAnchorEl?.parentElement) return;
  for (const { def, el } of translateItems) {
    el.remove();
    sideItemEls.delete(def.id);
    sideItemTextNodes.delete(def.id);
  }
  translateItems = items.map((def) => {
    const el = buildSideItem({ id: def.id, label: def.label(), icon: def.icon }, true);
    translateAnchorEl!.before(el);
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
  const brandName = document.createElement("span");
  brandName.className = "brand-name";
  brandName.textContent = "Translator";
  const brandVersion = document.createElement("span");
  brandVersion.className = "brand-ver";
  brandVersion.textContent = PACKAGE_VERSION;
  brand.append(brandMark, brandName, brandVersion);
  side.append(brand);
  side.append(buildSideSearch());

  // 各项和滑动高亮块放在同一个定位容器里，高亮块按选中项的 offsetTop 移动。
  const nav = document.createElement("div");
  nav.className = "side-nav";
  sideNavEl = nav;
  sideIndicatorEl = document.createElement("div");
  sideIndicatorEl.className = "side-ind";
  sideIndicatorEl.setAttribute("aria-hidden", "true");
  nav.append(sideIndicatorEl);

  translateGroupEl = buildTranslateGroup();
  nav.append(translateGroupEl);
  const itemMap = new Map<ViewId, HTMLDivElement>();
  itemMap.set("home", translateGroupEl);
  // 先按默认顺序画内置四种；读到设置后由 setTranslateSideItems 按「扩展」页的设置重排。
  translateItems = [];
  for (const def of TRANSLATE_ITEMS) {
    const item = builtinTranslateSideItem(def.id)!;
    const itemEl = buildSideItem(def, true);
    itemMap.set(def.id, itemEl);
    nav.append(itemEl);
    translateItems.push({ def: item, el: itemEl });
  }

  for (const def of RESOURCE_ITEMS) {
    const itemEl = buildSideItem(def);
    itemMap.set(def.id, itemEl);
    if (def.id === "tasks") {
      const badge = document.createElement("span");
      badge.className = "side-badge";
      badge.style.display = "none";
      itemEl.append(badge);
      taskBadgeEl = badge;
      translateAnchorEl = itemEl;
    }
    nav.append(itemEl);
  }

  const spacer = document.createElement("div");
  spacer.className = "side-spacer";
  nav.append(spacer);

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
    nav.append(itemEl);
  }
  side.append(nav);

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
  // 设置 / 帮助 / 反馈贴在侧栏底部：窗口拉高拉低时它们会移位，高亮块要跟着直接到位（不滑）。
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(() => moveSideIndicator(true)).observe(nav);
  }

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
  moveSideIndicator();
}

/**
 * 把滑动高亮块移到当前选中项上。第一次定位（启动时）不做动画：先放到位，下一帧才加上
 * 带过渡的 .ready，否则启动时高亮块会从侧栏顶端滑下来。instant 为 true 时（窗口尺寸变化）直接到位。当前页没有对应的侧栏项（例如
 * 已在「扩展」页隐藏的翻译类型）时高亮块淡出。
 */
function moveSideIndicator(instant = false): void {
  const ind = sideIndicatorEl;
  if (!ind || !sideNavEl) return;
  const on = sideNavEl.querySelector<HTMLDivElement>(".side-item.on");
  if (!on) {
    ind.style.opacity = "0";
    return;
  }
  ind.style.opacity = "1";
  if (instant) ind.style.transition = "none";
  ind.style.height = `${on.offsetHeight}px`;
  ind.style.transform = `translateY(${on.offsetTop}px)`;
  if (instant) {
    void ind.offsetHeight; // 先按无过渡落位，再还原过渡
    ind.style.transition = "";
  }
  if (!ind.classList.contains("ready")) {
    requestAnimationFrame(() => requestAnimationFrame(() => ind.classList.add("ready")));
  }
}

/** 更新侧栏「任务」徽标（活动任务数）。count <= 0 时隐藏徽标。 */
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

/** 更新顶栏右侧的任务药丸：有任务进行中时显示「N 个任务进行中」，没有时整颗不显示。 */
export function setTaskPill(config: TaskPillConfig): void {
  lastTaskPill = config;
  if (!taskPillHost) return;
  taskPillHost.innerHTML = "";
  if (config.count <= 0) return;
  // 从「N 个任务进行中」进来，用户要看的就是正在跑的那个，而不是上次选中的旧结果。
  taskPillHost.append(
    createPill({ label: t("{0} 个任务进行中", config.count), live: true, onClick: () => navigate("tasks", { focus: "active" }) }),
  );
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
