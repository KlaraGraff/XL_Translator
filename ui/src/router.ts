// V9 视图路由 —— 极简版：一个挂载容器 + 一张视图注册表，负责 mount/unmount 切换。
// 不做历史记录、不做 URL 同步；侧栏点击与「查看完整报告」之类的深链都直接调用 navigate()。

/** 侧栏可切换的七个顶层视图。 */
export type ViewId = "excel" | "word" | "pdf" | "tasks" | "library" | "settings" | "help";

export const VIEW_IDS: ViewId[] = ["excel", "word", "pdf", "tasks", "library", "settings", "help"];

/** 跳转参数：例如 settings 的子页 id、tasks 的任务 id。各视图自行约定 key。 */
export type ViewParams = Record<string, unknown>;

export interface View {
  /** 视图被选中时调用；container 是路由统一提供、已清空的挂载点。 */
  mount(container: HTMLElement, params: ViewParams): void;
  /** 切走前调用，用于取消订阅、清理定时器/SSE 连接等。可选。 */
  unmount?(): void;
}

type NavigateListener = (id: ViewId, params: ViewParams) => void;

let mountContainer: HTMLElement | null = null;
const registry = new Map<ViewId, View>();
const listeners = new Set<NavigateListener>();
let current: { id: ViewId; params: ViewParams } | null = null;

/** 注册一个视图实现。通常在 app.ts 启动时对全部 7 个视图各调用一次。 */
export function registerView(id: ViewId, view: View): void {
  registry.set(id, view);
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
  for (const listener of listeners) {
    listener(id, params);
  }
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
