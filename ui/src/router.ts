// V9 视图路由 —— 极简版：一个挂载容器 + 一张视图注册表，负责 mount/unmount 切换。
// 不做历史记录、不做 URL 同步；侧栏点击与「查看完整报告」之类的深链都直接调用 navigate()。
// V10.3 起换页带过渡（旧页上移淡出、新页从下方淡入），但 navigate() 仍是同步的：
// 返回时新视图已经挂好，currentView() 已经是新页，动画只是叠在上面的一层皮。

import { EASE_IN, EASE_OUT, motionAllowed } from "./motion";

/** 随应用发布的顶层视图。 */
export type BuiltinViewId = "home" | "excel" | "word" | "pdf" | "cad" | "tasks" | "library" | "plugins" | "extensions" | "mcp" | "settings" | "help" | "feedback";

/** 侧栏可切换的顶层视图；`ext:<id>` 是运行时注册的翻译类型，描述符到了之后才注册（见 app.ts）。 */
export type ViewId = BuiltinViewId | `ext:${string}`;

export const VIEW_IDS: BuiltinViewId[] = ["home", "excel", "word", "pdf", "cad", "tasks", "library", "plugins", "extensions", "mcp", "settings", "help", "feedback"];

/** 跳转参数：例如 settings 的子页 id、tasks 的任务 id。各视图自行约定 key。 */
export type ViewParams = Record<string, unknown>;

export interface View {
  /** 视图被选中时调用；container 是路由统一提供、已清空的挂载点。 */
  mount(container: HTMLElement, params: ViewParams): void;
  /** 切走前调用，用于取消订阅、清理定时器/SSE 连接等。可选。 */
  unmount?(): void;
  /**
   * 原地重画（切换界面语言）时用来回到同一处的参数，例如设置页当前的子页。
   * 不提供就沿用上次 navigate() 的参数。可选。
   */
  remountParams?(): ViewParams;
}

type NavigateListener = (id: ViewId, params: ViewParams) => void;

let mountContainer: HTMLElement | null = null;
const registry = new Map<ViewId, View>();
const listeners = new Set<NavigateListener>();
let current: { id: ViewId; params: ViewParams } | null = null;
/** 正在淡出的旧页快照，和新页的进场动画；连续快速换页时先把上一轮的收掉。 */
let leavingSnapshot: HTMLElement | null = null;
let enterAnimation: Animation | null = null;

/** 注册一个视图实现。通常在 app.ts 启动时对全部 7 个视图各调用一次。 */
export function registerView(id: ViewId, view: View): void {
  registry.set(id, view);
}

/** 移除一个运行时注册的视图（卸载扩展之后）；正显示着的不动，切走后才不再能打开。 */
export function unregisterView(id: ViewId): void {
  if (current?.id === id) return;
  registry.delete(id);
}

/** 这个视图注册过没有。运行时注册的翻译类型在描述符到达之前、或已被移除时没有视图。 */
export function hasView(id: string): id is ViewId {
  return registry.has(id as ViewId);
}

/** 告诉路由把视图挂载到哪个容器（shell.ts 暴露的 contentContainer）。 */
export function mountRouter(container: HTMLElement): void {
  mountContainer = container;
}

/** 切换到某个视图，可携带参数。会先 unmount 当前视图，再清空容器、mount 新视图。 */
export function navigate(id: ViewId, params: ViewParams = {}): void {
  if (!mountContainer) {
    throw new Error("Router: mountRouter(container) must be called before navigate().");
  }
  const view = registry.get(id);
  if (!view) {
    throw new Error(`Router: no view registered for "${id}".`);
  }
  // 换到另一页才做过渡；同一页重新挂载（切换界面语言、刷新参数）原地换，不闪。
  const animate = current !== null && current.id !== id && mountContainer.firstChild !== null && motionAllowed();
  finishTransition();
  if (animate) leavingSnapshot = snapshotForLeave(mountContainer);
  if (current) {
    registry.get(current.id)?.unmount?.();
  }
  clearElement(mountContainer);
  // Views may restyle the shared container (e.g. library switches it to a
  // column stack). Reset to the baseline `.content` class/inline-style before
  // each mount so a previous view's overrides never leak into the next one.
  mountContainer.removeAttribute("style");
  mountContainer.className = "content";
  current = { id, params };
  view.mount(mountContainer, params);
  if (animate) playTransition(mountContainer);
  for (const listener of listeners) {
    listener(id, params);
  }
}

/**
 * 旧页的静态快照：克隆整个内容容器，原位叠在新页上面淡出。克隆只是一层画面
 * （不可点、不可聚焦、读屏忽略），旧视图本身照常 unmount。滚动位置要手动带过去，
 * 否则快照会跳回顶端闪一下。
 */
function snapshotForLeave(container: HTMLElement): HTMLElement | null {
  const host = container.parentElement;
  if (!host) return null;
  const snapshot = container.cloneNode(true) as HTMLElement;
  // 快照和新页会在文档里并存一小会儿：去掉 id 和表单 name，免得新页按 id 查到快照里的旧节点，
  // 或者快照里选中的单选框把新页同名的那组改掉。
  for (const el of snapshot.querySelectorAll("[id]")) el.removeAttribute("id");
  for (const el of snapshot.querySelectorAll("[name]")) el.removeAttribute("name");
  snapshot.classList.add("route-leaving");
  snapshot.setAttribute("aria-hidden", "true");
  snapshot.setAttribute("inert", "");
  snapshot.style.position = "absolute";
  snapshot.style.left = `${container.offsetLeft}px`;
  snapshot.style.top = `${container.offsetTop}px`;
  snapshot.style.width = `${container.offsetWidth}px`;
  snapshot.style.height = `${container.offsetHeight}px`;
  snapshot.style.pointerEvents = "none";
  snapshot.style.overflow = "hidden";
  const scrollTop = container.scrollTop;
  host.insertBefore(snapshot, container.nextSibling);
  snapshot.scrollTop = scrollTop;
  return snapshot;
}

/** 旧页快照上移 4px 淡出（110ms），新页稍后从下方 8px 淡入（240ms），两段部分重叠。 */
function playTransition(container: HTMLElement): void {
  const snapshot = leavingSnapshot;
  if (snapshot) {
    const leave = snapshot.animate(
      [{ opacity: 1, transform: "none" }, { opacity: 0, transform: "translateY(-4px)" }],
      { duration: 110, easing: EASE_IN, fill: "forwards" },
    );
    leave.onfinish = () => {
      if (leavingSnapshot === snapshot) leavingSnapshot = null;
      snapshot.remove();
    };
  }
  const enter = container.animate(
    [{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "none" }],
    { duration: 240, delay: 50, easing: EASE_OUT, fill: "backwards" },
  );
  enterAnimation = enter;
  enter.onfinish = () => {
    if (enterAnimation === enter) enterAnimation = null;
  };
}

/** 上一轮过渡还没放完就又换页：快照直接拿掉，进场动画直接跳到结尾。 */
function finishTransition(): void {
  leavingSnapshot?.remove();
  leavingSnapshot = null;
  enterAnimation?.finish();
  enterAnimation = null;
}

/** 按当前语言重画当前视图，停在同一页（切换界面语言时用）。 */
export function remountCurrentView(): void {
  if (!current) return;
  const params = registry.get(current.id)?.remountParams?.() ?? current.params;
  navigate(current.id, params);
}

/** 当前视图 id 与参数；启动前尚未 navigate 过时为 null。 */
export function currentView(): { id: ViewId; params: ViewParams } | null {
  return current;
}

/** 订阅视图切换（shell.ts 用它来高亮侧栏当前项）。返回取消订阅函数。 */
export function onNavigate(listener: NavigateListener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * 清空一个元素的子节点。
 * 注意：刻意不用 Element.replaceChildren()——目标运行时含 macOS Monterey 的
 * WKWebView（Safari 15.1），该 API 要到 Safari 15.4 才可用。
 */
export function clearElement(el: HTMLElement): void {
  while (el.firstChild) {
    el.removeChild(el.firstChild);
  }
}
