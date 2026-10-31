// 全局搜索面板（V10.3「流光」）：Mac 按 ⌘K、Windows 按 Ctrl+K，或点侧栏顶部的搜索入口
// （shell.ts 派发 document 事件 "xl:cmdk"）。搜页面、任务和记忆库词条；输入文件路径回车，
// 交给 dispatchPaths 按类型进对应工作区并读取。
//
// 面板挂在 document.body 上（换页会清空 .content），同一时刻只有一个；换页时关闭。
// 分组顺序：路径 → 页面 → 任务 → 词条。前三组每次输入同步算，词条走接口、防抖后异步补上。
// 任务列表和当前语言对在打开时各拉一次，开着期间不轮询。
//
// 平台差异只经 platform.ts：修饰键 Mac 看 ⌘、其余看 Ctrl。输入法合成中（isComposing 或
// keyCode 229）的回车、Esc 和快捷键一律不处理，免得确认候选词的那下回车被当成「打开」。

import { closeLanguagePopover, closeMenu, clearElement } from "./components";
import { icon, type IconName } from "./icons";
import { onLocaleChange, t, tx } from "./i18n";
import { EASE_IN, EASE_OUT, motionAllowed } from "./motion";
import { isModKey } from "./platform";
import { currentView, hasView, navigate, onNavigate, type ViewId } from "./router";
import { builtinTranslateSideItem } from "./shell";
import { dispatchPaths } from "./drop-dispatch";
import { surfaceDescriptors } from "./surface-descriptors";
import { getClient, surfaceIcon, surfaceLabel, type Surface } from "./views/workspace";
import { taskRowSummary } from "./views/tasks";
import type { TaskStatus } from "./api-client";
import "./cmdk.css";
import { FIT_CLIP } from "./text-fit";

type Group = "path" | "pages" | "tasks" | "entries";

interface Item {
  group: Group;
  icon: IconName;
  /** 主文字；`code` 给出时 label 里的 {0} 位置换成等宽显示的这段（路径）。 */
  label: string;
  code?: string;
  sub?: string;
  run: () => void;
}

interface TmEntryHit {
  id: number;
  source_text: string;
  target_text: string;
  lang_pair?: string;
  origin?: string;
}

/** 空查询时「任务」组列几条（同入口页「最近」），有查询时最多几条。 */
const RECENT_TASKS = 4;
const MAX_TASK_HITS = 8;
const MAX_ENTRY_HITS = 8;
const ENTRY_DEBOUNCE_MS = 150;

interface PanelState {
  root: HTMLDivElement;
  box: HTMLDivElement;
  input: HTMLInputElement;
  list: HTMLDivElement;
  items: Item[];
  /** 当前项在 items 里的下标；没有条目时为 -1。 */
  active: number;
  /** 打开前有焦点的元素，关闭后还给它。 */
  previousFocus: Element | null;
  tasks: TaskStatus[] | null;
  tasksLoading: boolean;
  langPair: string | null;
  /** 词条结果所属的查询（归一化后）；与当前查询不同就不显示。 */
  entriesQuery: string;
  entries: TmEntryHit[];
  entriesPending: boolean;
  entryTimer: number | null;
  /** 每发一次词条请求（或查询变了）加一：晚到的旧响应对不上号就丢掉。 */
  entrySeq: number;
}

/** 开着的面板；关闭后置空，在途请求回来时对不上就丢掉（每次打开都是新的一份）。 */
let panel: PanelState | null = null;
let installed = false;

// ---------------------------------------------------------------------------
// 安装与开关
// ---------------------------------------------------------------------------

/** 装上全局快捷键与 "xl:cmdk" 事件监听（app.ts 启动时调用一次）。 */
export function installCmdk(): void {
  if (installed) return;
  installed = true;
  // 捕获阶段：焦点在输入框、可输入下拉里也能触发。
  document.addEventListener("keydown", (event) => {
    if (!isModKey(event) || event.altKey || event.shiftKey) return;
    if (event.key.toLowerCase() !== "k" || isComposing(event)) return;
    event.preventDefault();
    if (panel) closeCmdk();
    else openCmdk();
  }, true);
  document.addEventListener("xl:cmdk", (event) => {
    const query = (event as CustomEvent<{ query?: unknown } | undefined>).detail?.query;
    openCmdk(typeof query === "string" ? query : undefined);
  });
  // 换到另一页才关；同一页原地重挂（切换界面语言时 app.ts 会 remountCurrentView）不关，
  // 面板按当前输入换成新语言（见下面的 onLocaleChange）。
  let lastView = currentView()?.id ?? null;
  onNavigate((id) => {
    const samePage = id === lastView;
    lastView = id;
    if (!samePage) closeCmdk();
  });
  onLocaleChange(() => {
    if (!panel) return;
    localizeChrome(panel);
    rebuild(panel, false);
  });
}

/** 打开搜索面板；已经开着就只聚焦（给了 query 时换成它）。 */
export function openCmdk(query?: string): void {
  if (panel) {
    if (query !== undefined) {
      panel.input.value = query;
      onQueryChanged(panel);
    }
    focusInput(panel.input);
    return;
  }
  const previousFocus = document.activeElement;
  closeMenu();
  closeLanguagePopover();

  const root = el("div", "cmdk");
  const box = el("div", "box");
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  const bar = el("div", "in");
  const input = document.createElement("input");
  input.type = "text";
  input.className = "cmdk-q";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-expanded", "true");
  input.setAttribute("aria-controls", "cmdk-list");
  input.setAttribute("aria-autocomplete", "list");
  const esc = el("kbd", undefined, "Esc");
  bar.append(icon("search"), input, esc);
  const list = el("div", "ls");
  list.id = "cmdk-list";
  list.setAttribute("role", "listbox");
  const hint = el("div", "ck-keys");
  box.append(bar, list, hint);
  root.append(box);

  const state: PanelState = {
    root, box, input, list,
    items: [],
    active: -1,
    previousFocus,
    tasks: null,
    tasksLoading: true,
    langPair: null,
    entriesQuery: "",
    entries: [],
    entriesPending: false,
    entryTimer: null,
    entrySeq: 0,
  };
  panel = state;
  localizeChrome(state);

  root.addEventListener("pointerdown", (event) => {
    if (event.target === root) closeCmdk();
  });
  root.addEventListener("keydown", (event) => onKeydown(state, event));
  input.addEventListener("input", () => onQueryChanged(state));
  list.addEventListener("pointermove", (event) => {
    const button = (event.target as Element | null)?.closest<HTMLElement>(".it");
    const index = button ? Number(button.dataset.index) : -1;
    if (index >= 0 && index !== state.active) setActive(state, index, false);
  });
  list.addEventListener("click", (event) => {
    const button = (event.target as Element | null)?.closest<HTMLElement>(".it");
    if (button) runItem(state, Number(button.dataset.index));
  });

  if (query) input.value = query;
  document.body.append(root);
  scheduleEntries(state);
  rebuild(state, true);
  focusInput(input);
  if (motionAllowed()) {
    root.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 120, easing: EASE_OUT });
    box.animate(
      [{ opacity: 0, transform: "scale(.97) translateY(-6px)" }, { opacity: 1, transform: "none" }],
      { duration: 200, easing: EASE_OUT },
    );
  }
  void loadTasks(state);
  void loadLangPair(state);
}

/** 关闭搜索面板；没开着时是空操作。焦点还给打开前的元素（它还在页面上的话）。 */
export function closeCmdk(): void {
  const state = panel;
  if (!state) return;
  panel = null;
  state.entrySeq += 1;
  if (state.entryTimer !== null) window.clearTimeout(state.entryTimer);
  const { root, box, previousFocus } = state;
  root.style.pointerEvents = "none";
  if (previousFocus instanceof HTMLElement && document.contains(previousFocus)) {
    previousFocus.focus({ preventScroll: true });
  }
  if (!motionAllowed()) {
    root.remove();
    return;
  }
  const fade = root.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 120, easing: EASE_IN, fill: "forwards" });
  box.animate(
    [{ opacity: 1, transform: "none" }, { opacity: 0, transform: "scale(.97) translateY(-6px)" }],
    { duration: 120, easing: EASE_IN, fill: "forwards" },
  );
  fade.onfinish = () => root.remove();
  fade.oncancel = () => root.remove();
}

// ---------------------------------------------------------------------------
// 键盘与执行
// ---------------------------------------------------------------------------

function isComposing(event: KeyboardEvent): boolean {
  return event.isComposing || event.keyCode === 229;
}

function onKeydown(state: PanelState, event: KeyboardEvent): void {
  if (panel !== state) return;
  switch (event.key) {
    case "ArrowDown":
    case "ArrowUp": {
      event.preventDefault();
      const count = state.items.length;
      if (!count) return;
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActive(state, (state.active + step + count) % count, true);
      return;
    }
    case "Enter":
      if (isComposing(event)) return;
      event.preventDefault();
      runItem(state, state.active);
      return;
    case "Escape":
      if (isComposing(event)) return;
      event.preventDefault();
      event.stopPropagation();
      closeCmdk();
      return;
    case "Tab":
      // 面板是模态的：Tab 不出面板，焦点一直留在输入框。
      event.preventDefault();
      return;
    default:
  }
}

function runItem(state: PanelState, index: number): void {
  const item = state.items[index];
  if (!item || panel !== state) return;
  closeCmdk();
  item.run();
}

function setActive(state: PanelState, index: number, scroll: boolean): void {
  const previous = state.list.querySelector<HTMLElement>(".it.on");
  if (previous) {
    previous.classList.remove("on");
    previous.setAttribute("aria-selected", "false");
  }
  state.active = index;
  const next = state.list.querySelector<HTMLElement>(`.it[data-index="${index}"]`);
  if (!next) {
    state.input.removeAttribute("aria-activedescendant");
    return;
  }
  next.classList.add("on");
  next.setAttribute("aria-selected", "true");
  state.input.setAttribute("aria-activedescendant", next.id);
  if (scroll) next.scrollIntoView({ block: "nearest" });
}

function focusInput(input: HTMLInputElement): void {
  input.focus({ preventScroll: true });
  const end = input.value.length;
  input.setSelectionRange(end, end);
}

// ---------------------------------------------------------------------------
// 数据
// ---------------------------------------------------------------------------

/** 与任务页搜索同一套归一化（tasks.ts normalizeSearch）：NFKC + 小写 + 合并空白。 */
function normalize(value: string): string {
  return value.normalize("NFKC").toLocaleLowerCase().replace(/\s+/g, " ").trim();
}

function terms(query: string): string[] {
  const normalized = normalize(query);
  return normalized ? normalized.split(" ") : [];
}

/** 空格隔开的多个词要全部命中。 */
function matches(words: string[], ...fields: (string | undefined)[]): boolean {
  const haystack = normalize(fields.filter(Boolean).join(" "));
  return words.every((word) => haystack.includes(word));
}

/** 去掉首尾空白和成对引号（Windows「复制文件地址」带引号）；看起来是路径才返回。 */
function pathFromQuery(query: string): string | null {
  let value = query.trim();
  const quoted = /^(["'])(.*)\1$/.exec(value);
  if (quoted) value = quoted[2].trim();
  if (!value) return null;
  // 与 app.ts 拖放路径识别同一条：/ 开头、盘符、~ 开头；或者干脆含路径分隔符。
  if (/[\\/]/.test(value) || /^(\/|[A-Za-z]:[\\/]|~)/.test(value)) return value;
  return null;
}

async function loadTasks(state: PanelState): Promise<void> {
  try {
    const list = await (await getClient()).listTasks();
    if (panel !== state) return;
    state.tasks = [...list.active, ...list.recent].filter((task) => !["cleaner", "tm_clean"].includes(task.surface));
  } catch {
    if (panel !== state) return;
    state.tasks = [];
  }
  state.tasksLoading = false;
  rebuild(state, false);
}

async function loadLangPair(state: PanelState): Promise<void> {
  try {
    const pairs = await (await getClient()).request<{ selected?: { source_lang?: string; target_lang?: string } }>("/api/tm/language-pairs");
    if (panel !== state) return;
    const source = pairs.selected?.source_lang;
    const target = pairs.selected?.target_lang;
    // 没选过语言对就没有「词条」组，不报错。
    state.langPair = source && target ? `${source}-${target}` : null;
  } catch {
    if (panel !== state) return;
    state.langPair = null;
  }
  if (!state.langPair) {
    state.entriesPending = false;
    rebuild(state, false);
    return;
  }
  if (normalize(state.input.value)) fetchEntries(state);
}

function onQueryChanged(state: PanelState): void {
  state.entries = [];
  state.entriesQuery = "";
  scheduleEntries(state);
  rebuild(state, true);
}

function scheduleEntries(state: PanelState): void {
  if (state.entryTimer !== null) window.clearTimeout(state.entryTimer);
  state.entryTimer = null;
  state.entrySeq += 1;
  if (!normalize(state.input.value)) {
    state.entriesPending = false;
    return;
  }
  // 语言对还没拿到时也算「在途」：拿到后 loadLangPair 会接着发请求。
  state.entriesPending = true;
  if (!state.langPair) return;
  state.entryTimer = window.setTimeout(() => {
    state.entryTimer = null;
    fetchEntries(state);
  }, ENTRY_DEBOUNCE_MS);
}

function fetchEntries(state: PanelState): void {
  const pair = state.langPair;
  const keyword = state.input.value.trim();
  const query = normalize(keyword);
  if (!pair || !query) return;
  state.entrySeq += 1;
  const token = state.entrySeq;
  state.entriesPending = true;
  void (async () => {
    let hits: TmEntryHit[] = [];
    try {
      const payload = await (await getClient()).request<{ entries?: TmEntryHit[] }>(
        `/api/tm/entries?lang_pair=${encodeURIComponent(pair)}&keyword=${encodeURIComponent(keyword)}&page=1&page_size=${MAX_ENTRY_HITS}&entry_filter=all`,
      );
      hits = Array.isArray(payload.entries) ? payload.entries.slice(0, MAX_ENTRY_HITS) : [];
    } catch {
      // 词条搜不到就没有这组，不打扰。
    }
    if (panel !== state || token !== state.entrySeq) return;
    state.entries = hits;
    state.entriesQuery = query;
    state.entriesPending = false;
    rebuild(state, false);
  })();
}

// ---------------------------------------------------------------------------
// 条目
// ---------------------------------------------------------------------------

function go(id: ViewId, params?: Record<string, unknown>): () => void {
  return () => {
    if (hasView(id) || id === "plugins" || id === "mcp") navigate(id, params);
  };
}

function pageItems(words: string[]): Item[] {
  const page = (id: ViewId, label: string, iconName: IconName, run = go(id)): { item: Item; text: string } => ({
    item: { group: "pages", icon: iconName, label, run },
    text: `${label} ${id}`,
  });
  const surfacePage = (id: Surface) => page(id, builtinTranslateSideItem(id)?.label() ?? surfaceLabel(id), surfaceIcon(id));
  const top = [
    page("home", t("翻译"), "trans"),
    surfacePage("excel"),
    surfacePage("word"),
    surfacePage("pdf"),
    surfacePage("cad"),
    ...surfaceDescriptors()
      .filter((descriptor) => !descriptor.builtin && hasView(descriptor.key))
      .map((descriptor) => surfacePage(descriptor.key as Surface)),
    page("tasks", t("任务"), "tasks"),
    page("library", t("记忆库"), "book"),
    page("extensions", t("扩展"), "blocks"),
    page("settings", t("设置"), "gear"),
    page("help", t("帮助"), "help"),
    page("feedback", t("反馈"), "message"),
  ];
  if (!words.length) return top.map((entry) => entry.item);
  // 有查询时再加子页：扩展的三个页签、设置的五个子页（路由 id 跟 settings.ts 的 NAV_ITEMS）。
  // 「组件」「智能体接入」走旧 id：main 上是独立视图，合页之后是别名，两种都通。
  const sub = [
    page("extensions", t("扩展 · 文件类型"), "blocks", go("extensions", { tab: "types" })),
    page("plugins", t("扩展 · 组件"), "spark"),
    page("mcp", t("扩展 · 智能体接入"), "link"),
    ...([
      ["models", t("模型服务")],
      ["params", t("翻译参数")],
      ["appearance", t("外观与语言")],
      ["data", t("数据与维护")],
      ["about", t("更新与关于")],
    ] as const).map(([id, name]) => {
      const label = t("设置 · {0}", name);
      return { item: { group: "pages" as const, icon: "gear" as IconName, label, run: go("settings", { page: id }) }, text: `${label} settings ${id}` };
    }),
  ];
  return [...top, ...sub].filter((entry) => matches(words, entry.text)).map((entry) => entry.item);
}

function taskItems(state: PanelState, words: string[]): Item[] {
  const all = state.tasks ?? [];
  const picked: Item[] = [];
  for (const task of all) {
    if (picked.length >= (words.length ? MAX_TASK_HITS : RECENT_TASKS)) break;
    const info = taskRowSummary(task);
    if (words.length && !matches(words, info.title, info.meta, tx(task.source_label ?? ""))) continue;
    picked.push({
      group: "tasks",
      icon: info.icon,
      label: info.title,
      sub: info.meta,
      run: () => navigate("tasks", { taskId: task.task_id }),
    });
  }
  return picked;
}

function entryItems(state: PanelState, query: string): Item[] {
  if (!query || state.entriesQuery !== query || !state.langPair) return [];
  const pair = state.langPair;
  return state.entries.map((entry) => ({
    group: "entries" as const,
    icon: "book" as IconName,
    label: `${entry.source_text} → ${entry.target_text}`,
    sub: entry.origin === "manual" ? t("手动维护") : t("自动保存"),
    run: () => navigate("library", { focusEntry: { id: entry.id, term: entry.source_text, langPair: entry.lang_pair ?? pair } }),
  }));
}

function collect(state: PanelState): Item[] {
  const raw = state.input.value;
  const words = terms(raw);
  const items: Item[] = [];
  const path = pathFromQuery(raw);
  if (path) {
    items.push({
      group: "path",
      icon: "play",
      label: t("开始翻译 {0}", "\u0000"),
      code: path,
      sub: t("按类型进入对应工作区并读取"),
      run: () => { void dispatchPaths([path]); },
    });
  }
  items.push(...pageItems(words), ...taskItems(state, words), ...entryItems(state, normalize(raw)));
  return items;
}

// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------

function groupLabel(group: Group): string {
  switch (group) {
    case "path": return t("路径");
    case "pages": return t("页面");
    case "tasks": return t("任务");
    case "entries": return t("词条");
  }
}

/** 面板外框上随语言变化的文字：标题、占位、底部提示。 */
function localizeChrome(state: PanelState): void {
  state.box.setAttribute("aria-label", t("搜索"));
  state.input.placeholder = t("搜索页面、任务、词条，或输入文件路径直接开始翻译");
  state.input.setAttribute("aria-label", t("搜索"));
  const hint = state.box.querySelector<HTMLElement>(".ck-keys");
  if (!hint) return;
  clearElement(hint);
  hint.append(el("span", undefined, t("↑↓ 选择")), el("span", undefined, t("↵ 打开")), el("span", undefined, t("输入路径回车即可开始翻译")));
}

/**
 * 重画列表。resetActive：查询变了，选中回到第一项；否则（任务、词条晚到）尽量留在原来那项上，
 * 词条组排在最后，前面的下标不会因为它变。
 */
function rebuild(state: PanelState, resetActive: boolean): void {
  const previous = state.items[state.active];
  state.items = collect(state);
  let active = resetActive ? 0 : state.items.findIndex((item) => previous && item.group === previous.group && item.label === previous.label && item.sub === previous.sub);
  if (active < 0) active = Math.min(Math.max(state.active, 0), state.items.length - 1);
  if (!state.items.length) active = -1;
  state.active = active;

  const query = state.input.value.trim();
  const words = terms(query);
  const list = state.list;
  clearElement(list);
  let lastGroup: Group | null = null;
  state.items.forEach((item, index) => {
    if (item.group !== lastGroup) {
      lastGroup = item.group;
      list.append(el("div", "ck-grp", groupLabel(item.group)));
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = index === active ? "it on" : "it";
    button.id = `cmdk-it-${index}`;
    button.dataset.index = String(index);
    button.tabIndex = -1;
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", index === active ? "true" : "false");
    const main = el("span", `lb ${FIT_CLIP}`);
    if (item.code !== undefined) {
      const [before, after = ""] = item.label.split("\u0000");
      main.append(document.createTextNode(before), el("code", undefined, item.code), document.createTextNode(after));
    } else {
      appendWithMark(main, item.label, words);
    }
    button.append(icon(item.icon, { size: "sm" }), main);
    if (item.sub) button.append(el("span", `sub ${FIT_CLIP}`, item.sub));
    if (item.code !== undefined) button.title = item.code;
    else if (item.group === "tasks" || item.group === "entries") button.title = item.label;
    list.append(button);
  });
  // 还有请求在途（任务列表、词条）时不显示空态，免得先闪一下「没有匹配」。
  const waiting = state.tasksLoading || (words.length > 0 && state.entriesPending);
  if (!state.items.length && !waiting) list.append(el("div", "ck-empty", t("没有匹配的内容")));
  if (active >= 0) state.input.setAttribute("aria-activedescendant", `cmdk-it-${active}`);
  else state.input.removeAttribute("aria-activedescendant");
  if (resetActive) list.scrollTop = 0;
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/**
 * 把 text 里第一处与 query 相同（不分大小写）的片段包成 <mark>，用真实节点而不是 innerHTML。
 * 照抄 components.ts 的 appendWithMark（那里没导出）；多个词时标第一个出现在 text 里的词。
 */
function appendWithMark(host: HTMLElement, text: string, words: string[]): void {
  const lower = text.toLowerCase();
  const query = words.find((word) => lower.includes(word)) ?? "";
  const index = query ? lower.indexOf(query) : -1;
  if (index < 0) {
    host.append(document.createTextNode(text));
    return;
  }
  host.append(document.createTextNode(text.slice(0, index)));
  host.append(el("mark", undefined, text.slice(index, index + query.length)));
  host.append(document.createTextNode(text.slice(index + query.length)));
}
