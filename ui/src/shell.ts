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
let noticeHost: HTMLDivElement | null = null;

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

  // 顶栏与工作区之间的通栏提示位（目前只有「发现新版」用它）。常态为空，
  // 不占高度；setUpdateNotice() 填内容时才把 .upnotice 插进来。
  noticeHost = document.createElement("div");
  noticeHost.className = "notice-host";
  stage.append(noticeHost);

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

export interface UpdateNoticeConfig {
  /** 加粗的前半句，例如「Translator 9.3.0 可用」。 */
  title: string;
  /** 紧随其后的说明短句。 */
  detail?: string;
  /** 「查看详情」的落点；不传则不显示该按钮。 */
  onDetails?: () => void;
  /** 用户点 ✕ 之后的回调；提示条本身会先被移除。 */
  onDismiss?: () => void;
}

/**
 * 顶栏下方的通栏更新提示条（样张屏⑧）。传 null 收起。
 *
 * 只做「轻量告知」：不弹窗、不抢焦点，用户可能正在翻一份两百页的 PDF。
 * 关掉它不清侧栏红点——红点是「有事没办」，提示条是「现在打断你一下」。
 */
export function setUpdateNotice(config: UpdateNoticeConfig | null): void {
  if (!noticeHost) return;
  noticeHost.innerHTML = "";
  if (!config) return;

  const bar = document.createElement("div");
  bar.className = "upnotice";
  bar.append(icon("down", { size: "sm", className: "ico" }));

  const copy = document.createElement("span");
  const strong = document.createElement("b");
  strong.textContent = config.title;
  copy.append(strong);
  if (config.detail) {
    copy.append(document.createTextNode(` —— ${config.detail}`));
  }
  bar.append(copy);

  const acts = document.createElement("span");
  acts.className = "acts";
  if (config.onDetails) {
    const details = document.createElement("button");
    details.type = "button";
    details.className = "btn mini";
    details.textContent = "查看详情";
    details.addEventListener("click", () => config.onDetails?.());
    acts.append(details);
  }
  const close = document.createElement("button");
  close.type = "button";
  close.className = "x";
  close.setAttribute("aria-label", "关闭");
  close.append(icon("close", { size: "sm" }));
  close.addEventListener("click", () => {
    setUpdateNotice(null);
    config.onDismiss?.();
  });
  acts.append(close);
  bar.append(acts);

  noticeHost.append(bar);
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
