// 「扩展」页（V10.3）：原来的「扩展」「插件」「MCP」三页合成一页三个页签——
// 文件类型（extensions-page）、组件（plugins）、智能体接入（mcp）。三个面板仍是原来的模块，
// 以嵌入模式挂在页签下面的面板里（顶栏由这里设），同一时刻只挂一个。
// 旧 id 由路由别名转过来：navigate("plugins") → 「组件」，navigate("mcp") → 「智能体接入」。

import { clearElement, type View, type ViewParams } from "../router";
import { setTopbar } from "../shell";
import { t } from "../i18n";
import { EASE_OUT, motionAllowed } from "../motion";
import * as extensionsPageView from "./extensions-page";
import * as pluginsView from "./plugins";
import * as mcpView from "./mcp";
import "./extensions-hub.css";

type HubTab = "types" | "components" | "agents";

const TABS: HubTab[] = ["types", "components", "agents"];

const PANELS: Record<HubTab, { view: View; label: () => string }> = {
  types: { view: extensionsPageView, label: () => t("文件类型") },
  components: { view: pluginsView, label: () => t("组件") },
  agents: { view: mcpView, label: () => t("智能体接入") },
};

/** 会话内记住上次的页签：从侧栏再进来时停在原处。 */
let activeTab: HubTab = "types";
let tabEls = new Map<HubTab, HTMLButtonElement>();
let underlineEl: HTMLSpanElement | null = null;
let panelEl: HTMLDivElement | null = null;
let panelAnimation: Animation | null = null;

function isHubTab(value: unknown): value is HubTab {
  return typeof value === "string" && (TABS as string[]).includes(value);
}

export function mount(container: HTMLElement, params: ViewParams): void {
  if (isHubTab(params.tab)) activeTab = params.tab;
  setTopbar({ title: t("扩展"), subtitle: t("文件类型、可选组件与智能体接入都在这里管理") });
  container.classList.add("xhub");

  const tabs = document.createElement("div");
  tabs.className = "xhub-tabs";
  tabs.setAttribute("role", "tablist");
  tabEls = new Map();
  for (const tab of TABS) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "xhub-tab";
    button.id = `xhub-tab-${tab}`;
    button.dataset.tab = tab;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-controls", "xhub-panel");
    button.textContent = PANELS[tab].label();
    button.addEventListener("click", () => selectTab(tab));
    button.addEventListener("keydown", (event) => onTabKey(event, tab));
    tabEls.set(tab, button);
    tabs.append(button);
  }
  underlineEl = document.createElement("span");
  underlineEl.className = "xhub-ul";
  underlineEl.setAttribute("aria-hidden", "true");
  tabs.append(underlineEl);

  panelEl = document.createElement("div");
  panelEl.className = "xhub-panel";
  panelEl.id = "xhub-panel";
  panelEl.setAttribute("role", "tabpanel");

  container.append(tabs, panelEl);
  syncTabs();
  mountPanel(activeTab, false);
  // 挂载后才量得到页签宽度；第一次定位不带过渡，下一帧才加 .ready（同侧栏高亮块）。
  const underline = underlineEl;
  requestAnimationFrame(() => {
    if (underline !== underlineEl) return;
    moveUnderline();
    if (motionAllowed()) requestAnimationFrame(() => underline.classList.add("ready"));
  });
}

export function unmount(): void {
  // 「组件」面板靠 unmount 停掉检测轮询，切走时必须调到。
  PANELS[activeTab].view.unmount?.();
  panelAnimation?.cancel();
  panelAnimation = null;
  tabEls = new Map();
  underlineEl = null;
  panelEl = null;
}

/** 换界面语言后停在同一页签。 */
export function remountParams(): ViewParams {
  return { tab: activeTab };
}

function selectTab(tab: HubTab): void {
  if (tab === activeTab || !panelEl) return;
  PANELS[activeTab].view.unmount?.();
  activeTab = tab;
  syncTabs();
  moveUnderline();
  mountPanel(tab, motionAllowed());
}

function mountPanel(tab: HubTab, animate: boolean): void {
  const panel = panelEl;
  if (!panel) return;
  panelAnimation?.cancel();
  panelAnimation = null;
  clearElement(panel);
  // 面板会被嵌入的视图加上自己的类（plugins-content 等）；每次换页签回到基类，免得叠在一起。
  panel.className = "xhub-panel";
  panel.setAttribute("aria-labelledby", `xhub-tab-${tab}`);
  PANELS[tab].view.mount(panel, { embedded: true });
  if (animate) {
    panelAnimation = panel.animate(
      [{ opacity: 0, transform: "translateY(6px)" }, { opacity: 1, transform: "none" }],
      { duration: 160, easing: EASE_OUT },
    );
  }
}

function syncTabs(): void {
  for (const [tab, button] of tabEls) {
    const on = tab === activeTab;
    button.setAttribute("aria-selected", on ? "true" : "false");
    button.tabIndex = on ? 0 : -1;
  }
}

function moveUnderline(): void {
  const underline = underlineEl;
  const on = tabEls.get(activeTab);
  if (!underline || !on) return;
  if (!motionAllowed()) underline.classList.remove("ready");
  underline.style.width = `${on.offsetWidth}px`;
  underline.style.transform = `translateX(${on.offsetLeft}px)`;
}

/** ←/→ 在页签之间移动（Home/End 到两头），移到哪个就选中哪个。 */
function onTabKey(event: KeyboardEvent, tab: HubTab): void {
  const index = TABS.indexOf(tab);
  let next = index;
  if (event.key === "ArrowRight") next = (index + 1) % TABS.length;
  else if (event.key === "ArrowLeft") next = (index - 1 + TABS.length) % TABS.length;
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = TABS.length - 1;
  else return;
  event.preventDefault();
  const target = TABS[next];
  selectTab(target);
  tabEls.get(target)?.focus();
}
