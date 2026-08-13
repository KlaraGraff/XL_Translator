// V9 应用外壳 —— 左侧分组侧栏 + 顶栏容器。对应样张的 .side + .stage(.topbar+.content)，
// 但 .screen 那层「展示用卡片」被拆掉，改成撑满窗口的应用根布局（见 styles/app.css 的
// .app-shell）。这里只负责外壳本身；具体页面内容由 router.ts 挂到 contentContainer 里。

import { icon, type IconName } from "./icons";
import { createPill, createStatus, type StatusTone } from "./components";
import { navigate, onNavigate, type ViewId } from "./router";

interface SideItemDef {
  id: ViewId;
  label: string;
  icon: IconName;
}

const TRANSLATE_ITEMS: SideItemDef[] = [
  { id: "excel", label: "Excel 表格", icon: "excel" },
  { id: "word", label: "Word 文档", icon: "word" },
  { id: "pdf", label: "PDF 与图片", icon: "pdf" },
];

const RESOURCE_ITEMS: SideItemDef[] = [
  { id: "tasks", label: "任务中心", icon: "tasks" },
  { id: "library", label: "记忆库", icon: "book" },
];

const SYSTEM_ITEMS: SideItemDef[] = [
  { id: "settings", label: "设置", icon: "gear" },
  { id: "help", label: "帮助", icon: "help" },
];

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

function buildSideItem(def: SideItemDef): HTMLDivElement {
  const item = document.createElement("div");
  item.className = "side-item";
  item.append(icon(def.icon));
  item.append(document.createTextNode(def.label));
  item.addEventListener("click", () => navigate(def.id));
  return item;
}

function buildSideLabel(text: string): HTMLDivElement {
  const label = document.createElement("div");
  label.className = "side-label";
  label.textContent = text;
  return label;
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
  brandMark.textContent = "文A";
  brand.append(brandMark, document.createTextNode("Translator"));
  side.append(brand);

  side.append(buildSideLabel("翻译"));
  const itemMap = new Map<ViewId, HTMLDivElement>();
  for (const def of TRANSLATE_ITEMS) {
    const itemEl = buildSideItem(def);
    itemMap.set(def.id, itemEl);
    side.append(itemEl);
  }

  side.append(buildSideLabel("资源"));
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
  setModelPill({ label: "未连接模型", tone: "idle" });

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
}

/** 更新顶栏右侧「任务中心」药丸。count > 0 时切到 live 态并显示计数。 */
export function setTaskPill(config: TaskPillConfig): void {
  if (!taskPillHost) return;
  taskPillHost.innerHTML = "";
  const pill = config.count > 0
    ? createPill({ label: `${config.count} 个任务运行中`, live: true, onClick: () => navigate("tasks") })
    : createPill({ label: "任务中心", onClick: () => navigate("tasks") });
  taskPillHost.append(pill);
}

/** 更新顶栏右侧「当前模型」药丸。 */
export function setModelPill(config: ModelPillConfig): void {
  if (!modelPillHost) return;
  modelPillHost.innerHTML = "";
  const dotColor = PILL_TONE_COLOR[config.tone ?? "ok"];
  modelPillHost.append(
    createPill({ label: config.label, dotColor, onClick: () => navigate("settings") }),
  );
}
